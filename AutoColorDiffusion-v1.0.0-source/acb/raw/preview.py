# -*- coding: utf-8 -*-
"""嵌入式预览提取（问题 b 的实现）。

提取顺序（对应硬约束 #2、#10）：
    1. exiftool -b -PreviewImage      （DNG 改为 -b -JpgFromRaw）
    2. exiftool -b -ThumbnailImage
    3. rawpy 全解码（half_size=True 半分辨率）—— 慢，但在日志中明确提示

硬性纪律：
    - 任一张 RAW 预览提取失败不得中断整批任务（硬约束 #10）；
      本模块内部逐张 try/except，把失败转成 PreviewExtractionError 由上层计数。
    - 白名单中 .nrw/.srf/.sr2/.rw2 等冷门格式支持度不一，必须逐张 try/except。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..constants import PREVIEW_LOW_CONFIDENCE_RATIO, THUMB_LONG_EDGE
from ..errors import PreviewExtractionError
from ..logging_setup import get_logger
from .exiftool import ExiftoolRunner
from ..errors import IccProfileMissingError
from .icc import build_srgb_thumbnail, build_srgb_thumbnail_from_array

log = get_logger("preview")

# 预览来源标记。会写进 job.json，并随请求一起告知模型，
# 因为"来源是相机 JPEG 还是 rawpy 解码"会影响模型对色彩的可信度判断。
SOURCE_PREVIEW = "preview"        # exiftool -PreviewImage
SOURCE_JPG_FROM_RAW = "jpgfromraw"  # exiftool -JpgFromRaw（DNG 首选）
SOURCE_THUMBNAIL = "thumbnail"    # exiftool -ThumbnailImage
SOURCE_RAW_DECODE = "rawpy"       # rawpy 全解码兜底


@dataclass
class PreviewResult:
    """单张 RAW 的缩略图提取结果。"""

    jpeg_bytes: bytes
    source: str
    color_reason: str
    width: int = 0
    height: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_low_confidence(self) -> bool:
        """是否为低可信度来源。

        两个来源都要算进去（只算前者会漏一类）：
          1. `ThumbnailImage` 这类“就是缩略图”的标签（典型 160×120）；
          2. **实测尺寸明显小于目标长边**的预览 —— 小预览不等于没有预览。
             有的机器把 320×213 的预览放在 `PreviewImage` 里，
             只看标签名会把它当成正常来源，放大到 1024 后细节全是插值产物。
             “明显”的阀值是 PREVIEW_LOW_CONFIDENCE_RATIO（0.5）：
             960×720 这种几乎达标的预览**不算**低可信（强行标上会白白
             抑制模型对细节的判断），320×213 才算。
        放大到目标长边后细节不可信，AI 仍能判断整体曝光/色偏，
        但无法判断噪点与局部细节；这个标记会进提示词（提醒模型降低
        细节相关参数的调整幅度）。
        """
        if self.source == SOURCE_THUMBNAIL:
            return True
        if self.width and self.height:
            return max(self.width, self.height) < THUMB_LONG_EDGE * PREVIEW_LOW_CONFIDENCE_RATIO
        return False


def _decode_and_measure(jpeg_bytes: bytes) -> tuple[int, int]:
    """解码校验并返回尺寸。

    为什么要"解码校验"：exiftool 偶尔会吐出非 JPEG 的垃圾字节
    （例如某些机型的 PreviewImage 实际是 TIFF 或加密数据），
    直接喂给 Pillow 会在下游报错。在这里提前验证能把错误
    规约到"该标签不可用"，从而正确降级到下一个来源。
    """
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        img.verify()  # verify 后对象不可再用，因此重新 open 取尺寸
    with Image.open(io.BytesIO(jpeg_bytes)) as img:
        return img.size


def _try_exiftool(exiftool: ExiftoolRunner | None, raw_path: Path) -> tuple[bytes, str] | None:
    """尝试通过 exiftool 提取任意可用预览。

    允许 exiftool 为 None：调用方（extract_thumbnail）的签名就是可选的，
    表示"用户环境里没有 exiftool"，此时直接返回 None 走 rawpy 兜底，
    而不是在 `exiftool.available` 上抛 AttributeError。
    """
    if exiftool is None or not exiftool.available:
        return None
    try:
        return exiftool.extract_preview_image(raw_path)
    except Exception as exc:
        # 逐张 try/except（硬约束 #10）：单个文件/单个格式的问题绝不能外溢。
        log.debug("exiftool 提取失败 %s：%s", raw_path.name, exc)
        return None


def _source_label(tag: str) -> str:
    """把 exiftool 标签名映射为内部来源标记。"""
    if tag == "JpgFromRaw":
        return SOURCE_JPG_FROM_RAW
    if tag == "ThumbnailImage":
        return SOURCE_THUMBNAIL
    return SOURCE_PREVIEW


def _try_rawpy(raw_path: Path, long_edge: int) -> tuple[bytes, str]:
    """rawpy 全解码兜底。

    参数选择说明：
        half_size=True  —— 只解半分辨率。依据：本路径产出的是给 AI 判断用的
                           1024 长边缩略图，全分辨率解码（如 6000×4000）后
                           再缩小，信息量没有区别，但耗时是半分辨率的约 4 倍。
        use_camera_wb=True —— 用机内白平衡。依据：与 ACR 默认的 "As Shot"
                           行为一致，避免 AI 在一个灰蒙蒙的"未校准"画面上
                           误判需要大幅修正白平衡。
        output_bps=8    —— 8 位/通道。依据：JPEG 本身就是 8 位，
                           16 位解码只是浪费内存带宽。
        output_color 默认 SRGB —— 已应用相机矩阵与 sRGB gamma，
                           因此后续不做 ICC 变换（见 icc.build_srgb_thumbnail_from_array）。
    """
    import numpy as np  # noqa: F401  （确保依赖存在，缺失时错误信息更清晰）
    import rawpy

    with rawpy.imread(str(raw_path)) as raw:
        rgb = raw.postprocess(
            half_size=True,
            use_camera_wb=True,
            output_bps=8,
        )
    jpeg = build_srgb_thumbnail_from_array(rgb, long_edge)
    return jpeg, "rawpy postprocess(output_color=sRGB 默认)"


def extract_thumbnail(
    raw_path: Path,
    exiftool: ExiftoolRunner | None,
    long_edge: int = THUMB_LONG_EDGE,
    prefer_raw_decode: bool = False,
    allow_raw_decode_fallback: bool = True,
) -> PreviewResult:
    """提取单张 RAW 的 sRGB 缩略图。

    参数：
        prefer_raw_decode —— "高保真模式"开关（Further Considerations #2）。
            打开时跳过嵌入式预览，直接用 rawpy 解码。
            适用场景：相机机内 JPEG 配方与 ACR 渲染差异很大（如富士胶片模拟、
            佳能 Picture Style 设为"风光"），导致 AI 在"机内风格色"上判断，
            给出的参数与 ACR 中的实际观感错位。
            代价：每张慢 1–5 秒，且 rawpy 的解码风格本身也与 ACR 不同源，
            只是比机内 JPEG 更接近"中性"。
        allow_raw_decode_fallback —— 是否允许在前两条路径都失败后走 rawpy。
            CLI 的 --dry-run 若想快速验证链路可关掉它。

    失败时抛 PreviewExtractionError，消息里带完整尝试记录，
    方便用户判断是格式不支持还是文件损坏。
    """
    warnings: list[str] = []
    attempts: list[str] = []

    # --- 路径 1：嵌入式预览（默认） ------------------------------------------
    if not prefer_raw_decode:
        hit = None
        if exiftool is None or not exiftool.available:
            # 必须与"标签缺失"分开说：两者的修法完全不同（一个装 exiftool，
            # 一个是文件本身没有嵌入式预览）。以前两者共用一句"未找到标签"，
            # 用户看到满屏"回退 rawpy"却不知道到底是哪一环出的问题。
            attempts.append("exiftool 不可用（未安装或未检测到）")
            log.warning("%s：exiftool 不可用，无法读嵌入式预览。", raw_path.name)
        else:
            hit = _try_exiftool(exiftool, raw_path)
        if hit is not None:
            data, tag = hit
            try:
                width, height = _decode_and_measure(data)
            except Exception as exc:
                attempts.append(f"{tag}: 字节流无法解码为图像（{exc}）")
                log.debug("%s 的 %s 解码失败：%s", raw_path.name, tag, exc)
            else:
                source = _source_label(tag)
                if source == SOURCE_THUMBNAIL:
                    warnings.append(
                        f"仅找到缩略图（{width}×{height}），放大到长边 {long_edge} 后细节不可信，"
                        "AI 对噪点/细节类判断的可靠性下降"
                    )
                elif max(width, height) < long_edge * PREVIEW_LOW_CONFIDENCE_RATIO:
                    # 小预览 ≠ 没有预览：拿到的图明显小于目标长边，
                    # 说明它本身就不是为“看图判断细节”准备的。
                    # 能用，但要告诉用户与模型“细节不可信”。
                    # 阈值取 0.5 而不是“小于目标就算”：960×720（目标的 0.94）
                    # 这类预览完全够用，把它标成不可信反而会白白抑制模型的细节判断。
                    warnings.append(
                        f"嵌入式预览偏小（{width}×{height}，不足目标长边 {long_edge} 的一半），"
                        "细节经放大后不可信，AI 对噪点/细节类判断的可靠性下降"
                    )
                try:
                    jpeg, reason = build_srgb_thumbnail(data, long_edge)
                except IccProfileMissingError as exc:
                    # 源色空间已知但缺 profile：**不拿别的 profile 凑合**，
                    # 改走 rawpy（直接输出 sRGB，颜色正确）。
                    # 必须单独接住并写进 warnings —— 否则用户只会看到
                    # "回退 rawpy（较慢）"，完全不知道是色彩管理的问题。
                    attempts.append(f"{tag}: 缺少源色空间 profile（{exc}）")
                    warnings.append(
                        "该文件的预览声明为 Adobe RGB 但系统里找不到它的 ICC profile，"
                        "已改用 rawpy 全解码以避免按 sRGB 误判颜色（每张慢 1–5 秒）。"
                    )
                    log.warning(
                        "%s：预览声明 Adobe RGB 但缺 ICC profile，改走 rawpy 全解码。",
                        raw_path.name,
                    )
                except Exception as exc:
                    attempts.append(f"{tag}: ICC/缩放/编码失败（{exc}）")
                else:
                    log.info(
                        "%s：预览来源=%s（%d×%d → 长边 %d），色彩判定=%s",
                        raw_path.name,
                        source,
                        width,
                        height,
                        long_edge,
                        reason,
                    )
                    return PreviewResult(
                        jpeg_bytes=jpeg,
                        source=source,
                        color_reason=reason,
                        width=width,
                        height=height,
                        warnings=warnings,
                    )
        else:
            attempts.append("exiftool: 未找到 PreviewImage / JpgFromRaw / ThumbnailImage")
    else:
        attempts.append("高保真模式：已跳过嵌入式预览")

    # --- 路径 2：rawpy 全解码兜底 --------------------------------------------
    if not allow_raw_decode_fallback:
        raise PreviewExtractionError(
            f"{raw_path.name}：嵌入式预览不可用，且本次运行已禁用 rawpy 兜底。\n"
            + "；".join(attempts)
        )

    warnings.append("嵌入式预览不可用，已回退 rawpy 全解码（该操作较慢）")
    try:
        jpeg, reason = _try_rawpy(raw_path, long_edge)
    except Exception as exc:
        attempts.append(f"rawpy: {exc}")
        raise PreviewExtractionError(
            f"{raw_path.name}：所有预览提取路径均失败。\n"
            f"尝试记录：{'；'.join(attempts)}\n"
            "可能原因：该 RAW 为冷门变体格式（如部分 NRW/SRF/SR2）、文件损坏，"
            "或相机型号过新尚未被 libraw 支持。"
        ) from exc

    # 把原因写进日志与文件级警告：这一条以前只有"较慢"没有"为什么"，
    # 用户拿着日志也查不下去（真实反馈）。
    reason_text = "；".join(attempts) if attempts else "未记录到具体原因"
    log.warning("%s：使用 rawpy 兜底解码（较慢）。原因：%s", raw_path.name, reason_text)
    warnings[-1] = f"{warnings[-1]}。原因：{reason_text}"
    try:
        width, height = _decode_and_measure(jpeg)
    except Exception:
        # 尺寸只用于日志/UI 展示，量不到不影响使用。
        width = height = 0
    return PreviewResult(
        jpeg_bytes=jpeg,
        source=SOURCE_RAW_DECODE,
        color_reason=reason,
        width=width,
        height=height,
        warnings=warnings,
    )
