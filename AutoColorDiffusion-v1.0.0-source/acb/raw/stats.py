# -*- coding: utf-8 -*-
"""缩略图统计量计算。

两处用途：
    1. **批内一致性模式**（问题 e 的默认策略）：算出本批的亮度/对比度/色温
       中位数，注入提示词，让模型"把每张图向批内中位数靠拢"。
       这是让"用户没给提示词也没选风格"从"无信息"变成有明确物理目标的关键。
    2. **caption 降级通道**（硬约束 #19）：vision=false 的模型看不到图，
       只能靠一段文字描述。本模块把缩略图转成结构化文字（亮度分布、
       通道均值、饱和度、裁切比例），作为 caption 的素材。

所有统计都在 sRGB 缩略图上做，因此与 AI 看到的是同一份数据
（硬约束 #2 的因果链：AI 看的是 sRGB 缩略图，XMP 参数作用于原始 RAW）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, asdict
from statistics import median

import numpy as np
from PIL import Image

from ..logging_setup import get_logger

log = get_logger("stats")

# 亮度权重（Rec. 709 / sRGB 的亮度系数）。
# 依据：Rec.709 是 sRGB 的母标准，Y = 0.2126R + 0.7152G + 0.0722B。
# 用 0.299/0.587/0.114（Rec.601）会让绿色偏亮，
# 在植被/人像场景下与 ACR 的亮度感知不一致。
LUMA_R = 0.2126
LUMA_G = 0.7152
LUMA_B = 0.0722

# 裁切判定阈值。
# 低于 2 视为"接近纯黑"，高于 253 视为"接近纯白"。
# 依据：JPEG 有损编码在极值附近会有 ±2 的振铃，取 2/253 能避免把
# 编码噪声误判为真实的死黑/死白。
SHADOW_CLIP_LEVEL = 2
HIGHLIGHT_CLIP_LEVEL = 253

# 色温估算：用 R/B 比值的对数线性经验式。
# 说明：这不是真正的 CCT 反解（那需要 D65 白点与色适应矩阵），
# 而是一个供"批内相对比较"用的单调量。它的用途是让模型知道
# "这批图整体偏暖还是偏冷、哪张明显偏离"，而不是给出精确开尔文值。
# 因此刻意保持简单、可解释、无参数拟合。
CCT_NEUTRAL_RB_RATIO = 1.0     # R/B 均值比 = 1 视为中性
CCT_KELVIN_PER_LOG_RB = 2200.0  # 对数比每变化 1.0 对应的开尔文变化量
CCT_BASE_KELVIN = 5500.0        # 中性白点对应的估算值


@dataclass
class ImageStats:
    """单张缩略图的统计量。"""

    width: int
    height: int
    luma_mean: float          # 平均亮度 0..255
    luma_std: float           # 亮度标准差，作为"对比度"的代理量
    luma_p05: float           # 亮度 5 分位
    luma_p95: float           # 亮度 95 分位
    r_mean: float
    g_mean: float
    b_mean: float
    saturation_mean: float    # HSV S 通道均值 0..255
    shadow_clip_ratio: float  # 接近纯黑的像素占比 0..1
    highlight_clip_ratio: float  # 接近纯白的像素占比 0..1
    estimated_cct: float      # 估算色温（K），仅供批内相对比较

    def as_dict(self) -> dict:
        return asdict(self)

    def to_caption_text(self) -> str:
        """转成给纯文本模型的自然语言描述（caption 降级通道）。

        刻意用"人话 + 具体数字"的混合形式：
        纯数字模型不好理解，纯形容词又丢失可量化信息，
        两者并列能让模型既有语义锚点又有比较基准。
        """
        brightness = "偏暗" if self.luma_mean < 90 else ("偏亮" if self.luma_mean > 150 else "中等")
        contrast = "低对比" if self.luma_std < 42 else ("高对比" if self.luma_std > 68 else "中等对比")
        tone = "偏冷" if self.estimated_cct > 6200 else ("偏暖" if self.estimated_cct < 5000 else "中性")
        sat = "低饱和" if self.saturation_mean < 70 else ("高饱和" if self.saturation_mean > 130 else "饱和度适中")

        channel_bias = "整体偏红" if self.r_mean > self.b_mean * 1.08 else (
            "整体偏蓝" if self.b_mean > self.r_mean * 1.08 else "红蓝通道基本平衡"
        )

        parts = [
            f"分辨率 {self.width}×{self.height}。",
            f"画面整体{brightness}，平均亮度 {self.luma_mean:.0f}/255，",
            f"{contrast}（亮度标准差 {self.luma_std:.0f}）。",
            f"色温观感{tone}（估算 {self.estimated_cct:.0f}K），{channel_bias}",
            f"（R/G/B 均值 = {self.r_mean:.0f}/{self.g_mean:.0f}/{self.b_mean:.0f}）。",
            f"{sat}，平均饱和度 {self.saturation_mean:.0f}/255。",
            f"死黑像素占比 {self.shadow_clip_ratio * 100:.1f}%，",
            f"死白像素占比 {self.highlight_clip_ratio * 100:.1f}%。",
        ]
        if self.highlight_clip_ratio > 0.02:
            parts.append("高光有可见裁切，建议压低 Highlights 或 Whites。")
        if self.shadow_clip_ratio > 0.05:
            parts.append("暗部有可见裁切，建议提升 Shadows 或 Blacks。")
        return "".join(parts)


def compute_stats(thumbnail_jpeg: bytes) -> ImageStats:
    """在 sRGB 缩略图上计算统计量。

    为了速度，先把图缩到长边 256 再做统计——统计量对分辨率不敏感，
    而 256 长边只有约 6.5 万像素，numpy 运算在毫秒级完成。
    （这是本模块唯一的"魔法数字"，依据就是"统计量不需要高分辨率"。）
    """
    with Image.open(io.BytesIO(thumbnail_jpeg)) as img:
        img = img.convert("RGB")
        w0, h0 = img.size
        if max(w0, h0) > 256:
            scale = 256.0 / max(w0, h0)
            img = img.resize((max(1, int(w0 * scale)), max(1, int(h0 * scale))),
                             Image.Resampling.LANCZOS)
        arr = np.asarray(img, dtype=np.float32)

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    luma = LUMA_R * r + LUMA_G * g + LUMA_B * b

    # 饱和度用 HSV 的 S 通道公式：S = (max - min) / max（max != 0 时）。
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(maxc > 0, (maxc - minc) / np.maximum(maxc, 1.0) * 255.0, 0.0)

    r_mean = float(r.mean())
    b_mean = float(b.mean())

    # 色温估算（对数比值，仅供批内相对比较，见 CCT_NEUTRAL_RB_RATIO 处说明）
    rb_ratio = (r_mean + 1e-6) / (b_mean + 1e-6)
    estimated_cct = CCT_BASE_KELVIN - CCT_KELVIN_PER_LOG_RB * float(np.log(rb_ratio))
    # 夹到物理上合理的区间，避免极端比值（如全黑图）产出荒谬数值。
    estimated_cct = float(max(2000.0, min(15000.0, estimated_cct)))

    total = float(luma.size)

    return ImageStats(
        width=w0,
        height=h0,
        luma_mean=float(luma.mean()),
        luma_std=float(luma.std()),
        luma_p05=float(np.percentile(luma, 5)),
        luma_p95=float(np.percentile(luma, 95)),
        r_mean=r_mean,
        g_mean=float(g.mean()),
        b_mean=b_mean,
        saturation_mean=float(sat.mean()),
        shadow_clip_ratio=float((luma <= SHADOW_CLIP_LEVEL).sum() / total),
        highlight_clip_ratio=float((luma >= HIGHLIGHT_CLIP_LEVEL).sum() / total),
        estimated_cct=estimated_cct,
    )


def batch_medians(all_stats: list[ImageStats]) -> dict[str, float]:
    """算一批图的统计量中位数（问题 e 的"向批内中位数靠拢"用）。

    为什么用中位数而不是均值：批量调色场景里经常混入一两张极端片
    （如夜景与日光同批），均值会被极端值拖偏，中位数则稳定得多。
    """
    if not all_stats:
        return {"luma": 128.0, "contrast": 55.0, "cct": 5500.0, "sat": 100.0, "count": 0.0}

    return {
        "luma": float(median(s.luma_mean for s in all_stats)),
        "contrast": float(median(s.luma_std for s in all_stats)),
        "cct": float(median(s.estimated_cct for s in all_stats)),
        "sat": float(median(s.saturation_mean for s in all_stats)),
        "count": float(len(all_stats)),
    }
