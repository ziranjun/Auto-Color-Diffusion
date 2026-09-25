# -*- coding: utf-8 -*-
"""离线调试适配器（原「离线模拟」）：不发任何网络请求，直接返回写死的分析结果。

为什么需要它
------------
真实链路（取预览 → 请求视觉模型 → 校验 JSON → 写 XMP → 生成 jsx → 调 Photoshop
导出）需要密钥可用、模型真的支持视觉。在"密钥是占位的 / 中转站不通 / 根本没配
模型"的情况下，这条链路一步都跑不完，只能靠读代码判断对错。

本模块提供一个与 `ModelAdapter` **接口一致**的替身，直接返回一份写死的、结构完整
的分析结果。pipeline 只依赖 adapter 的这几个成员：

    describe() / supports_vision / supports_json_schema / spec /
    client.budget / client.usage_summary() / analyze_group() / analyze_training()

因此换掉 adapter 就能把整条链路跑通，**pipeline / xmp / ps 各层一行都不用改**。
这也是当初把模型调用收敛到 adapter 这一个抽象边界的回报。

设计要点：假结果也要过真校验
----------------------------
`MOCK_PARAMS` / `MOCK_CURVES` 里的值**不是直接塞进 ItemResult 的**，而是先经过
生产代码用的同一个 `xmp.validator.validate_params()`。好处有两个：

    1. 假结果的结构与真实 AI 输出完全同构（连 warnings 的产生方式都一致），
       所以"能跑通模拟"确实等价于"结构与校验链路是对的"；
    2. 万一以后有人把字段名写错、或把取值改到越界，校验器会立刻报错，
       而不是让一个"假通过"的链路把真问题掩盖过去。

注意：这份数据刻意**不覆盖全部约 90 个可写字段**。原因是"每个字段都有一个
随意的值"对排查问题没有帮助，反而让日志难以阅读；这里按 ACR 面板分组覆盖了
14 个组的代表性字段（约 45 个），足以让 XMP 写入、曲线联动、白平衡联动等
关键分支都被走到。
"""

from __future__ import annotations

from typing import Any

from ..errors import SchemaValidationError
from ..logging_setup import get_logger
from ..xmp import validator as V
from .adapter import ItemResult, PreviewItem

log = get_logger("ai.offline")

# 出现在日志与 notes 里的标记。存在的意义是让用户**一眼看出这批产物是模拟数据**，
# 避免把它当成真实分析结果直接拿去交付。
MOCK_NOTE_MARKER = "[离线调试]"

# ---------------------------------------------------------------------------
# 写死的假分析结果（字段名 = ACR 的 crs 字段名，不带前缀）
# ---------------------------------------------------------------------------
# 取值依据：这是一套"人像向、通透感"的常见调法，落在各字段的正常工作区间内，
# 目的在于让产物看起来像真的、且能明显看出差异（方便肉眼核对 XMP 是否被写入）。
#
# 【不能出现 WhiteBalance】
#     它的 ai_writable=False（属于机器/版本相关字段，AI 不许产出）。
#     写 Temperature/Tint 时由 writer 自动补 WhiteBalance="Custom"，
#     否则 ACR 会继续按 "As Shot" 渲染而**忽略**这两个值。
# 【不能出现 PostCropVignetteStyle】
#     同样是 ai_writable=False。
MOCK_PARAMS: dict[str, Any] = {
    # --- 白平衡 ---------------------------------------------------------
    "Temperature": 5600,          # 日光偏暖一点，落在 ACR 常用区间 2000–50000 内
    "Tint": 8,                    # 轻微偏洋红，抵消绿色滤镜造成的偏色
    # --- 基本影调 -------------------------------------------------------
    "Exposure2012": 0.35,         # +0.35 EV：欠曝补偿的典型值（范围 ±5 EV）
    "Contrast2012": 12,           # 轻提对比，避免高光压死后显得灰
    "Highlights2012": -25,        # 压高光保天空细节
    "Shadows2012": 18,            # 提暗部找回阴影层次
    "Whites2012": 8,              # 白场微提，增加通透感
    "Blacks2012": -10,            # 黑场微压，保证不飘
    "Vibrance": 15,               # 自然饱和度：优先提低饱和色，不冲肤色
    "Saturation": -5,             # 总饱和度略降，防止 Vibrance 叠加后过艳
    # --- 参数曲线 -------------------------------------------------------
    "ParametricShadows": -10,
    "ParametricDarks": 5,
    "ParametricLights": 8,
    "ParametricHighlights": -12,
    # --- 效果 -----------------------------------------------------------
    "Texture": 8,                 # 纹理：人像加太多会显毛孔，给一点点
    "Clarity2012": 10,            # 清晰度：中等对比增强
    "Dehaze": 5,                  # 去薄雾：轻度，过量会发灰
    "Glow": 0,                    # 光晕：人像一般不开，显式写 0 表示"确认过"
    "PostCropVignetteAmount": -15,  # 暗角 -15：轻度压边
    "GrainAmount": 0,             # 颗粒：交付件不加
    # --- 曲线名称（必须与点曲线联动）------------------------------------
    # 写点曲线时必须把它置为 Custom，否则 ACR 会继续按预设名渲染而忽略控制点。
    # 这个字段本身是 ai_writable=True 的枚举，所以由"AI"显式给出是合理的。
    "ToneCurveName2012": "Custom",
    # --- HSL：提橙（肤色）、压绿（植被/环境反光）----------------------------
    "HueAdjustmentOrange": 5,
    "SaturationAdjustmentOrange": 8,
    "LuminanceAdjustmentOrange": 10,   # 提亮橙色=提亮肤色
    "HueAdjustmentGreen": -20,         # 绿色偏黄，减少刺眼的纯绿
    "SaturationAdjustmentGreen": -30,  # 压绿是最常见的"日系/人像"手法
    "LuminanceAdjustmentGreen": -10,
    "SaturationAdjustmentBlue": 5,
    # --- 分离色调（旧面板）----------------------------------------------
    "SplitToningShadowHue": 204,       # 暗部 204° = 偏青蓝（与明部暖色成对比）
    "SplitToningShadowSaturation": 12,
    "SplitToningHighlightHue": 45,     # 明部 45° = 暖黄
    "SplitToningHighlightSaturation": 8,
    "SplitToningBalance": 0,
    # --- 颜色分级（新面板）----------------------------------------------
    "ColorGradeShadowHue": 210,
    "ColorGradeShadowSat": 12,
    "ColorGradeMidtoneHue": 35,
    "ColorGradeMidtoneSat": 10,
    # --- 细节 -----------------------------------------------------------
    "Sharpness": 40,              # 锐化量（上限 150）
    "SharpenRadius": 1.0,         # 半径（0.5–3.0，一位小数）
    "SharpenDetail": 25,
    # 降噪字段（全局 6 个 + 局部 LocalLuminanceNoise）**都不再由 AI 产出**：
    # 这是技术校正而不是调色方向（该降多少取决于 ISO/机身/曝光时长），
    # 用户 2026-09-23 明确要求"降噪不要让 AI 动"。
    # 假数据也必须遵守 —— 否则会真撞上校验器（这条以前踩过一次：镜头开关）。
    # --- 镜头校正 -------------------------------------------------------
    # 开关类字段（LensProfileEnable / AutoLateralCA / 去边 / 镜头晕影 VignetteAmount）
    # **都不再由 AI 产出**：它们由程序按每张照片自己的基线补缺
    # （见 acb/raw/lens.py，实施步骤 4；镜头晕影由用户 2026-09-23 裁决收进程序组）。
    # 假数据也必须遵守这条，否则会真的撞上校验器
    # （这里曾经踩过：两个字段被判为"机器相关字段，AI 不允许产出"，离线自检直接变红）。
    # 四角压暗的表达方式是「效果」面板里的裁剪后晕影：
    "PostCropVignetteAmount": -12,
    # --- 校准 -----------------------------------------------------------
    "ShadowTint": 0,
    "RedHue": 0,
    "RedSaturation": 0,
}

# 写死的假点曲线。
# 坐标是提示词里约定的 **0..1 归一化域**（不是 ACR 的 0..255），
# writer.normalize_curve_points() 会按 255 放大并落成 "x, y" 文本。
# 形状是一条轻微 S 曲线：暗部稍压、亮部微提，即所谓"通透感"最常用的形状。
MOCK_CURVES: dict[str, list] = {
    "ToneCurvePV2012": [
        [0.0, 0.0],
        [0.25, 0.22],
        [0.5, 0.5],
        [0.75, 0.78],
        [1.0, 1.0],
    ],
}

# 写死的假风格归纳（训练模式用）。
# 字段必须与 schema.build_training_schema() 的 required 完全一致，
# 且三个枚举字段的取值必须落在各自的 enum 里——否则 train_mode 的解析器会抛错。
MOCK_TRAINING: dict[str, Any] = {
    "style_summary": (
        "整体偏暖调、中等对比，暗部带轻微青蓝、明部带暖黄的分离色调；"
        "色相上习惯压绿提橙以改善肤色，饱和度克制、不过度渲染。"
    ),
    "text_rules": [
        "人像优先保证肤色准确：橙色相微偏黄、亮度略提，避免发红发灰",
        "绿色系一律降饱和并偏黄，减少环境中刺眼的纯绿",
        "暗部加轻微青蓝、明部加暖黄，用分离色调制造冷暖对比",
        "优先用自然饱和度而非总饱和度提色，避免肤色过饱和",
        "高光保守：宁可压高光保细节，也不要为了通透把高光推到溢出",
    ],
    "saturation_tendency": "中性",
    "contrast_tendency": "中性",
    "shadow_color_cast": "偏冷（蓝青）",
    "hsl_habits": "倾向压绿、提橙红亮度以改善肤色",
    "lens_correction_preference": "通常启用配置文件校正，并保留轻微暗角作为氛围",
    "excluded_reasoning": (
        "样本中 ProcessVersion / CameraProfile / LensProfile* 等字段在全部文件里"
        "取值一致，属于机身与版本的机器默认值，不构成个人偏好，故排除在统计之外。"
    ),
    # 场景标签（实施步骤 1c）：键是 file_id，值是 2–8 字场景名。
    # 假数据也要带上，否则离线调试跑不到"按场景统计规则"那条链路。
    "scene_labels": {
        "mock-01": "风景（日出日落）",
        "mock-02": "人像（环境）",
        "mock-03": "夜景弱光",
    },
}


def build_mock_verdict() -> V.ValidationResult:
    """把写死的假结果拼成"模型返回的原始 JSON"形状并过一遍**真**校验器。

    刻意给键加上 `crs:` 前缀：真实模型返回的就是带前缀的名字，
    这样假结果走的是与真实链路完全相同的归一化路径（去前缀 → 查白名单 →
    pydantic 强校验 → 夹取 → 曲线归一化），而不是绕过它。
    """
    merged: dict[str, Any] = {
        f"crs:{name}": value for name, value in MOCK_PARAMS.items()
    }
    merged.update({f"crs:{name}": points for name, points in MOCK_CURVES.items()})
    return V.validate_params(merged)


def mock_item_result(item: PreviewItem) -> ItemResult:
    """为一张图构造一份通过真校验的假分析结果。"""
    verdict = build_mock_verdict()
    if not verdict.ok:
        # 写死的假数据自己没过校验 = 代码缺陷（不是用户输入问题）。
        # 直接抛而不是降级，否则会把"模拟数据写错了"伪装成"链路跑通了"。
        raise SchemaValidationError(
            "离线调试数据未通过校验器，这属于代码缺陷而非用户输入问题："
            + verdict.summary()
        )

    return ItemResult(
        file_id=item.file_id,
        params=dict(verdict.params),
        curves=dict(verdict.curves),
        notes=(
            f"{MOCK_NOTE_MARKER} 写死的假结果，用于在不联网的情况下验证链路。"
            f"目标文件：{item.filename}"
        ),
        warnings=list(verdict.warnings),
    )


class OfflineClient:
    """占位 client：只为满足 pipeline 对 `adapter.client` 的两处用法。

    pipeline 用到的是：
        adapter.client.budget = budget     —— output_mode 会用真实文件数重建预算
        adapter.client.usage_summary()     —— 结果汇总里报告用量
    """

    def __init__(self, budget: Any = None) -> None:
        self.budget = budget

    def usage_summary(self) -> str:
        return "离线调试：未发出任何网络请求，token 用量为 0"

    def close(self) -> None:
        """与 ApiClient 接口对齐（这里没有连接需要释放）。"""


class OfflineAdapter:
    """与 `ModelAdapter` 接口一致的离线替身，不做任何网络 I/O。

    spec 仍使用真实加载出来的配置（只为拿到 display 与 images_per_request），
    因此分批、并发、日志展示等行为与真实运行一致。
    """

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.client = OfflineClient()

    @property
    def supports_vision(self) -> bool:
        """报 True：走"直接看图"的路径，避免顺带触发 caption 降级分支。"""
        return True

    @property
    def supports_json_schema(self) -> bool:
        """报 True：与声明了 json_schema 的真实条目一致。"""
        return True

    def describe(self) -> str:
        return (
            f"{self.spec.display}；**离线调试模式**"
            "（不发出任何网络请求，返回写死的假结果）"
        )

    def analyze_group(
        self,
        items: list[PreviewItem],
        *,
        style_block: str,
        user_prompt: str,
        batch_stats: dict[str, float] | None = None,
        auto_match: bool = False,
        temperature: float | None = None,
    ) -> list[ItemResult]:
        """返回写死的假结果。

        与真实实现一致，**不抛异常**（失败只写进 ItemResult.error），
        保证单张失败不中断整批——这样调用方的容错分支也能被覆盖到。
        """
        results = [mock_item_result(item) for item in items]
        log.warning(
            "离线调试：已为 %d 张图注入写死的假分析结果（未调用任何 API）。",
            len(results),
        )
        return results

    def analyze_training(
        self, samples: list[PreviewItem], style_name: str
    ) -> dict[str, Any]:
        """返回写死的假风格归纳。"""
        if not samples:
            raise SchemaValidationError("没有可用的训练样本")

        log.warning(
            "离线调试：返回写死的假风格归纳「%s」（%d 个样本，未调用任何 API）。",
            style_name,
            len(samples),
        )
        return dict(MOCK_TRAINING)


def build_offline_adapter(spec: Any) -> OfflineAdapter:
    """构造离线适配器。单独提供工厂函数是为了让调用方的意图在日志里可见。"""
    log.warning(
        "已启用离线调试：不会发出任何网络请求，所有分析结果都是写死的假数据。"
    )
    return OfflineAdapter(spec)
