# -*- coding: utf-8 -*-
"""几何蒙版的生成（A1 第一步：线性渐变 / 径向渐变 / 大面积画笔）。

【结构来源 —— 唯一权威】用户的实测样本 `test_data/IMG_2509.xmp`
（ACR 18.6 写出，2026-09-23）。层级是：

    crs:MaskGroupBasedCorrections
      └ rdf:Seq > rdf:li > rdf:Description crs:What="Correction"   ← 一组局部调整
            （整套 41 个 crs:Local* 参数都写在它身上，缺省为 0）
          └ crs:CorrectionMasks > rdf:Seq > rdf:li > 几何蒙版
                线性渐变 = Mask/Gradient         （ZeroX/ZeroY + FullX/FullY）
                径向渐变 = Mask/CircularGradient （Top/Left/Bottom/Right + Angle/Midpoint/Roundness/Feather）
                画笔     = Mask/Aggregate > crs:Masks > Mask/Paint（Radius/Flow/CenterWeight + crs:Dabs 点列）

【从样本里读出来的三条硬事实（不是猜的）】
  1. 几何蒙版**不带** `MaskDigest / InputDigest / ModelVersion` —— 也就是说程序生成的
     几何蒙版不会被 ACR 的"图像指纹"卡住（范围蒙版与主体蒙版才带这些）；
  2. 局部滑块的量纲（2026-09-23 由用户在 ACR 里实测确认）：
     普通 `Local*`：面板 ±100 ↔ 存储 ±1（×0.01）—— 我们写 -0.4，ACR 面板显示高光 -40；
     `LocalExposure2012`：面板 ±4.00 EV ↔ 存储 ±1（×0.25）—— 我们写 1，ACR 显示曝光 +4.00 并过曝；
     颜色分级族与 `LocalCurveRefineSaturation`：直接写面板值。
     所以本模块**对外统一用面板值**（人能看懂的单位），写入前用 `_LOCAL_SCALES` 换算。
  3. 画笔必须包一层 `Mask/Aggregate`，且 Aggregate 的属性要写在**嵌套的
     `rdf:Description`** 上（属性直接写在 `rdf:li` 上时 ACR 里画笔完全不出现，已实测踩坑）；
     点序列是 `r 半径` / `f 流量` / `d x y` 三种前缀，逐点成对。
  4. **坐标是存储帧**（未按 EXIF 旋转的那一版）—— 详见文件中部 `display_to_stored_spec`。

【明确不支持（交给 ACR 自己做）】
  · `Mask/RangeMask`（颜色范围 / 明亮度范围）：依赖 ACR 用图像内容算出的
    `crs:RangeMaskMapInfo`（含 RGBMin/Max、LabMin/Max、LumEq 映射表），程序造不出来；
  · `Mask/Image`（主体 / 对象选择）：依赖 ACR 的图像理解与 `InputDigest` 指纹。
  这两类若已存在于既有侧车，走"整包保留"，我们绝不覆盖。

【为什么要它】用户 2026-09-23 的原话：输出里"没有蒙版"是与模板最大的一处不像；
而他的三份风格样本（IMG_2509 / _G4A1392 / _G4A1644）里有 7 个蒙版、5 种类型。
"""

from __future__ import annotations

import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger
from . import fields as F
from . import namespaces as NS

log = get_logger("xmp.masks")

# 局部参数在 Correction 上是分两段写的（样本事实，不是猜的）：
#   · 基础 27 项：ACR 在"有调整的局部"上**总是**全部列出（缺省 0）；
#   · 颜色分级 14 项：ACR **只在真的动过它们时才写** —— 样本第 1 组有、第 2~5 组一个都没有。
#     所以我们也只在明确给值时写，不凭空多出 14 个属性。
_LOCAL_BASE_FIELDS: tuple[str, ...] = (
    "LocalExposure", "LocalHue", "LocalSaturation", "LocalContrast", "LocalClarity",
    "LocalSharpness", "LocalBrightness", "LocalToningHue", "LocalToningSaturation",
    "LocalExposure2012", "LocalContrast2012", "LocalHighlights2012", "LocalShadows2012",
    "LocalWhites2012", "LocalBlacks2012", "LocalClarity2012", "LocalDehaze",
    "LocalLuminanceNoise", "LocalMoire", "LocalDefringe", "LocalTemperature", "LocalTint",
    "LocalTexture", "LocalGrain", "LocalCorrectedDepth", "LocalGlow",
    "LocalCurveRefineSaturation",
)

_LOCAL_COLORGRADE_FIELDS: tuple[str, ...] = (
    "LocalColorGradeShadowHue", "LocalColorGradeShadowSat", "LocalColorGradeHighlightHue",
    "LocalColorGradeHighlightSat", "LocalColorGradeBalance", "LocalColorGradeMidtoneHue",
    "LocalColorGradeMidtoneSat", "LocalColorGradeShadowLum", "LocalColorGradeMidtoneLum",
    "LocalColorGradeHighlightLum", "LocalColorGradeBlending", "LocalColorGradeGlobalHue",
    "LocalColorGradeGlobalSat", "LocalColorGradeGlobalLum",
)

# 完整书写顺序（基础段在前，与样本一致）。
_LOCAL_FIELD_ORDER: tuple[str, ...] = _LOCAL_BASE_FIELDS + _LOCAL_COLORGRADE_FIELDS

# 基础段的缺省值：样本里只有 LocalCurveRefineSaturation 不是 0（写 100）。
_LOCAL_DEFAULTS: dict[str, str] = {name: "0" for name in _LOCAL_BASE_FIELDS}
_LOCAL_DEFAULTS["LocalCurveRefineSaturation"] = "100"

# 值域（**面板值**，越界一律夹回并给警告，不让 ACR 去猜）。
#
# 范围的**唯一来源是 fields.py**（group="mask" 的登记表）——这里不手抄第二份，
# 因为"两处各写一半"是这类映射最常见的 bug：范围改了、校验没跟上，
# 结果就是值被静默夹到边界。缺失登记会当场抛错（宁可在启动时炸，也不要在写盘时悄悄写错）。
from . import fields as _F  # noqa: E402 —— 放在这里是为了让上面的常量先定义好

_LOCAL_RANGES: dict[str, tuple[float, float]] = {}
for _name in _LOCAL_FIELD_ORDER:
    _spec = _F.get_field(_name)
    if _spec is None or _spec.minimum is None or _spec.maximum is None:
        raise RuntimeError(
            f"局部参数 {_name} 没有在 acb/xmp/fields.py 里登记（group=\"mask\"），"
            "无法校验范围。请先补登记，不要在 masks.py 里再手写一份。"
        )
    _LOCAL_RANGES[_name] = (float(_spec.minimum), float(_spec.maximum))


# 面板值 → 存储值 的换算系数（用户 2026-09-23 在 ACR 里实测得到）：
#   · 普通 Local* 滑块：面板 ±100 ↔ 存储 ±1（×0.01）—— 实测：我们写 -0.4，ACR 面板显示高光 -40；
#   · 局部曝光：面板 ±4.00 EV ↔ 存储 ±1（×0.25）—— 实测：我们写 1，ACR 面板显示曝光 +4.00 并过曝；
#   · 颜色分级族：直接写面板值（样本里就是 +38 / -37 / +50 / 0..360 的色相）；
#   · LocalCurveRefineSaturation：也是直接写面板值（样本里写 100）。
_LOCAL_SCALES: dict[str, float] = {name: 0.01 for name in _LOCAL_FIELD_ORDER}
_LOCAL_SCALES["LocalExposure2012"] = 0.25
_LOCAL_SCALES["LocalExposure"] = 0.25
for _name in _LOCAL_COLORGRADE_FIELDS:
    _LOCAL_SCALES[_name] = 1.0
_LOCAL_SCALES["LocalCurveRefineSaturation"] = 1.0

# 风格里"带蒙版样本占比"低于这个值时，默认不给这张图生成局部调整。
# 依据：用户反馈里"少样本风格"容易出问题，而蒙版是比全局参数更激进的手；
# 只有训练素材里确实有相当比例的照片用了局部调整，我们才认为这是他的习惯。
STYLE_MASK_USAGE_MIN_RATIO = 0.4

# 给提示词用的中文标签（避免提示词与允许表两处不一致：清单由 ai_local_params_text() 生成）
_LOCAL_LABELS: dict[str, str] = {
    "LocalExposure2012": "曝光（EV）",
    "LocalHighlights2012": "高光",
    "LocalShadows2012": "阴影",
    "LocalWhites2012": "白色",
    "LocalBlacks2012": "黑色",
    "LocalContrast2012": "对比度",
    "LocalTemperature": "色温（**相对偏移**，不是开尔文）",
    "LocalTint": "色调（**相对偏移**）",
    "LocalSaturation": "饱和度",
    "LocalTexture": "纹理（负值即磨皮/柔化）",
    "LocalClarity2012": "清晰度（负值即柔焦）",
    "LocalDehaze": "去霾",
}


def ai_local_params_text(ranges: dict[str, Any] | None = None) -> str:
    """拼出"AI 可提的局部参数"清单（供提示词使用）。

    `ranges` 传该风格的两层范围时，括号里写的是**这套风格自己的实测习惯范围**
    （而不是模块兜底表）—— 否则提示词说的范围与写盘时裁决的范围会是两套数字。
    """
    source = ranges if ranges is not None else AI_LOCAL_RANGES
    parts = []
    for name, value in source.items():
        label = _LOCAL_LABELS.get(name, name)
        if isinstance(value, LocalRange):
            lo, hi = value.soft_min, value.soft_max
        else:
            lo, hi = value
        parts.append(f"{label}（{lo:g}..{hi:g}）")
    return "、".join(parts)


MASK_KINDS = ("linear", "radial", "brush")
MAX_MASKS_PER_IMAGE = 8
# 程序侧上限（比 ACR 能接受的上限低）：一张图最多 3 个局部调整。
# 为什么收这么紧：用户反馈过"局部太多太乱反而看不出效果"，
# 而且每个蒙版都要模型给出可信的几何与意图，超过 3 个基本就是在凑数。
PROGRAM_MAX_MASKS = 3
# 画笔落点上限：模型给的落点越多越像"随手涂"，粗画笔反而更稳。
MAX_BRUSH_DABS = 60

# 用户 2026-09-23 的补充裁决：
#   · 局部**降噪不要让 AI 碰**（降噪是"技术参数"，不是审美方向）；
#   · 他的风格文件与样本**只是用来测软件**的，改完他会重新训练 ——
#     所以这张表**不是最终真相**：训练时会用 `compute_local_param_ranges()` 从
#     他自己的素材里算出真范围写进风格文件，读取时优先用那一份（见 ranges_from_style）。
#     下面这张表只是"风格文件里没有该字段数据时"的兜底，保持保守。
# 几何与参数范围一旦来自训练，就不再需要我猜（这正是用户反复强调的那条原则）。
AI_LOCAL_RANGES: dict[str, tuple[float, float]] = {
    "LocalExposure2012": (-0.9, 1.2),        # EV
    "LocalHighlights2012": (-100.0, 82.0),
    "LocalShadows2012": (-62.0, 64.0),
    "LocalWhites2012": (-30.0, 19.0),
    "LocalBlacks2012": (-32.0, 53.0),
    "LocalContrast2012": (-11.0, 76.0),
    "LocalTemperature": (-7.0, 17.0),        # 相对偏移（不是开尔文）
    "LocalTint": (-6.0, 7.0),
    "LocalSaturation": (-28.0, 10.0),
    "LocalTexture": (-61.0, 16.0),
    "LocalClarity2012": (-26.0, 23.0),
    "LocalDehaze": (3.0, 20.0),
    # 局部**颜色分级** 6 项（阴影/中间调/高光的色相与饱和度）也收掉了（用户 2026-09-23 裁决）：
    # 它既是审美方向又是绝对色相/饱和度数值，模型很容易拿训练里的色相值当目标值照抄；
    # 而且人脸的“肤色线”靠局部色温/色调/饱和度已经能表达（这正是他当初提的那个用例）。
    # 字段本身仍登记在 fields.py（程序照旧能读、能写、能原样保留）。
}


def ranges_from_style(style: dict[str, Any] | None) -> dict[str, tuple[float, float]]:
    """取该风格自己的局部参数范围（面板值）。

    优先用风格文件里的 `local_param_ranges`（训练时从用户素材里算出来的），
    没有才退回模块里的保守兜底表。这样：
      · "范围来自用户素材" 这条原则是**机制**在保证，不靠我写死常量；
      · 用户重新训练一次，范围就自动变成他的真实用法。
    """
    if isinstance(style, dict):
        trained = style.get("local_param_ranges")
        if isinstance(trained, dict):
            result: dict[str, tuple[float, float]] = {}
            for name, entry in trained.items():
                if name not in AI_LOCAL_RANGES:
                    continue          # 机器学习时没算过的字段，仍不允许 AI 提
                if not isinstance(entry, dict):
                    continue
                try:
                    lo = float(entry.get("min"))
                    hi = float(entry.get("max"))
                except (TypeError, ValueError):
                    continue
                if lo <= hi:
                    # 注意这里是 <= ：
                    # “所有样本都是同一个值”是合法信息（例如他每张都恰好把局部高光压到 -100），
                    # 写成 < 会让这个字段直接掉出允许清单 —— 那就变成“把他一直在用的手法丢掉”了。
                    # 退化范围的硬范围靠 expand_range 的最小余量撑开。
                    result[name] = (lo, hi)
            if result:
                return result
    return dict(AI_LOCAL_RANGES)


# 两层范围（用户 2026-09-23 裁决：“留余量（软范围+硬范围双层）”）——
#   软范围 = 训练素材实测 min..max，是发给 AI 的“你的习惯范围”，也是日志里的“习惯”；
#   硬范围 = 软范围外扩一个“余量”，再与字段自己的面板范围取交集，是**写盘时的裁决线**。
# 为什么必须有硬范围：样本少时实测范围会偏窄（只有 2 张样本时，“习惯范围”最多只能
# 说明他这 2 张怎么用），拿它当硬边界会把合理用法夹掉（这正是用户提的“留余量”）。
# 余量按**样本数**分档（数据越少余量越大），并要求至少两个“面板步长”，
# 这样“所有样本同值”（跨度 0）的字段也不会退化成完全不能动。
# 注意：这是工程边界，不是审美判断；具体数值都写在这里，日志里会如实报出实际区间。
MARGIN_RATIOS: tuple[tuple[int, float], ...] = (
    (2, 0.5),      # ≤2 张：只能说明“他有这个习惯”，余量给足
    (5, 0.3),
    (15, 0.2),
)
MARGIN_RATIO_MANY = 0.1     # >15 张：样本够多，不往外扩太多
MIN_MARGIN_STEPS = 2.0      # 至少给两个面板步长，防止“跨度 0 → 余量 0”


@dataclass(frozen=True)
class LocalRange:
    """一个局部字段的两层范围（面板值）。"""

    soft_min: float
    soft_max: float
    hard_min: float
    hard_max: float
    count: int = 0
    std: float = 0.0

    @property
    def soft_text(self) -> str:
        return f"{self.soft_min:g}..{self.soft_max:g}"

    @property
    def hard_text(self) -> str:
        return f"{self.hard_min:g}..{self.hard_max:g}"

    def contains_soft(self, value: float) -> bool:
        return self.soft_min <= value <= self.soft_max

    def contains_hard(self, value: float) -> bool:
        return self.hard_min <= value <= self.hard_max


def _field_step(name: str) -> float:
    """字段的面板步长（整数字段 1；小数字段按它自己登记的小数位）。"""
    spec = F.get_field(name)
    if spec is None or not spec.decimals:
        return 1.0
    return 10.0 ** (-int(spec.decimals))


def expand_range(
    name: str,
    lo: float,
    hi: float,
    count: int = 0,
    std: float = 0.0,
) -> LocalRange:
    """软范围 → 两层范围（外扩余量，再与字段面板范围取交集）。"""
    span = max(0.0, float(hi) - float(lo))
    ratio = MARGIN_RATIO_MANY
    for limit, value in MARGIN_RATIOS:
        if int(count) <= limit:
            ratio = value
            break
    margin = max(span * ratio, MIN_MARGIN_STEPS * _field_step(name), 2.0 * float(std or 0.0))
    hard_lo, hard_hi = float(lo) - margin, float(hi) + margin
    spec = F.get_field(name)
    if spec is not None and spec.minimum is not None and spec.maximum is not None:
        hard_lo = max(hard_lo, float(spec.minimum))
        hard_hi = min(hard_hi, float(spec.maximum))
    # 兜底：硬范围绝不能比软范围更窄（否则实测值反而会被夹）
    hard_lo = min(hard_lo, float(lo))
    hard_hi = max(hard_hi, float(hi))
    return LocalRange(float(lo), float(hi), hard_lo, hard_hi, int(count), float(std or 0.0))


def local_range_layers(style: dict[str, Any] | None) -> dict[str, LocalRange]:
    """该风格的局部参数两层范围（写盘时的裁决线）。

    软范围来自 `ranges_from_style()`（训练算出，拿不到就用兜底表）；
    样本数与离散度从风格文件的 `local_param_ranges` 条目里读。
    """
    trained = style.get("local_param_ranges") if isinstance(style, dict) else None
    layers: dict[str, LocalRange] = {}
    for name, limits in ranges_from_style(style).items():
        entry = trained.get(name) if isinstance(trained, dict) else None
        count = 0
        std = 0.0
        if isinstance(entry, dict):
            try:
                count = int(entry.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                std = float(entry.get("std") or 0.0)
            except (TypeError, ValueError):
                std = 0.0
        layers[name] = expand_range(name, limits[0], limits[1], count, std)
    return layers


def _as_local_range(name: str, value: Any) -> LocalRange:
    """兼容旧调用：传 (lo, hi) 元组时按“没有样本数信息”扩余量。"""
    if isinstance(value, LocalRange):
        return value
    lo, hi = value
    return expand_range(name, float(lo), float(hi))


def clamp_local_to_ranges(
    specs: list[dict[str, Any]],
    ranges: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """按“这套风格自己的两层范围”复核局部参数（超出习惯期=放行但记录，超出硬范围=夹回）。

    为什么要再夹一次：`parse_specs()` 用的是通用兜底范围（因为它在适配器里跑，
    那时还拿不到风格文件），而写盘前的这一层才有风格。两层的分工：
        适配器内 = 结构校验（是不是蒙版、坐标合不合法、字段在不在清单里）
        写盘前   = **风格级别**的范围校验（软=习惯、硬=留余量后的裁决线）
    """
    warnings: list[str] = []
    result: list[dict[str, Any]] = []
    for spec in specs:
        local = spec.get("local") or {}
        cleaned: dict[str, float] = {}
        for name, value in local.items():
            raw_limits = ranges.get(name)
            if raw_limits is None:
                warnings.append(f"{spec.get('name')}：局部参数 {name} 不在该风格的允许清单里，已丢弃")
                continue
            layer = _as_local_range(name, raw_limits)
            number = float(value)
            if not layer.contains_soft(number):
                if layer.contains_hard(number):
                    # 超出“习惯范围”但没超出“留余量后的硬范围”：**放行**，只记一笔。
                    warnings.append(
                        f"{spec.get('name')}：{name}={number:g} 超出该风格的习惯范围"
                        f"（{layer.soft_text}），在留余量后的硬范围内（{layer.hard_text}）已放行"
                    )
                else:
                    clamped = min(layer.hard_max, max(layer.hard_min, number))
                    warnings.append(
                        f"{spec.get('name')}：{name}={number:g} 超出硬范围 {layer.hard_text}"
                        f"（习惯 {layer.soft_text} ± 余量），已夹到 {clamped:g}"
                    )
                    number = clamped
            cleaned[name] = number
        if not cleaned:
            warnings.append(f"{spec.get('name')}：局部参数全部被剔除，该蒙版已丢弃")
            continue
        new_spec = dict(spec)
        new_spec["local"] = cleaned
        result.append(new_spec)
    return result, warnings
# 每张图**所有蒙版合起来**的局部总量上限，同样取自他的样本（单图合计的最大绝对值）。
# 意义：三个蒙版都往同一方向推、单看每个都在范围内，合起来照样会过头。
AI_LOCAL_IMAGE_TOTALS: dict[str, float] = {
    "LocalExposure2012": 2.7,      # 实测单图合计 -0.2 .. +2.7 EV
    "LocalHighlights2012": 210.0,  # 实测 -210 .. +126
    "LocalSaturation": 61.0,       # 实测 -61 .. +10
    "LocalTemperature": 20.0,      # 实测 -3 .. +20
    "LocalTint": 7.0,              # 实测 -6 .. +7
}

# 坐标帧换算的映射表与取值校验都在 `acb/raw/orientation.py`（那里有完整的实测说明）。
# 本模块只负责"几何规格"层面的搬运（线性两点 / 径向矩形 / 画笔落点）。


def display_to_stored_point(point: tuple[float, float], orientation: str | None) -> tuple[float, float]:
    """显示帧归一化坐标 → 存储帧归一化坐标（实现见 acb/raw/orientation.py）。"""
    from ..raw import orientation as orientation_mod

    return orientation_mod.display_to_stored_point(point, orientation)


def _convert_spec(spec: dict[str, Any], orientation: str | None, to_stored: bool) -> dict[str, Any]:
    """线性/径向/画笔三种几何的坐标帧换算（to_stored=True 显示→存储）。"""
    from ..raw import orientation as orientation_mod

    conv = orientation_mod.display_to_stored_point if to_stored else orientation_mod.stored_to_display_point
    label = orientation_mod.normalize(orientation)
    if label == "Horizontal (normal)":
        return dict(spec)
    out = dict(spec)
    if spec.get("type") == "linear":
        out["zero"] = conv(spec["zero"], label)
        out["full"] = conv(spec["full"], label)
    elif spec.get("type") == "radial":
        top, left, bottom, right = spec["rect"]
        corners = [conv((left, top), label), conv((right, bottom), label)]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        out["rect"] = (min(ys), min(xs), max(ys), max(xs))
        # 旋转 90 度后椭圆的长短轴互换，倾角也跟着转 90 度。
        delta = -90.0 if to_stored else 90.0
        out["angle"] = (float(spec.get("angle") or 0.0) + delta) % 360.0
    elif spec.get("type") == "brush":
        out["dabs"] = [conv(p, label) for p in spec.get("dabs") or []]
    return out


def display_to_stored_spec(spec: dict[str, Any], orientation: str | None) -> dict[str, Any]:
    """把一份"显示帧"几何的蒙版规格换算成"存储帧"（ACR 要写的那一版）。"""
    return _convert_spec(spec, orientation, to_stored=True)


def stored_to_display_spec(spec: dict[str, Any], orientation: str | None) -> dict[str, Any]:
    """反向：把侧车里读到的（存储帧）几何换算成"显示帧"，供分析与训练使用。"""
    return _convert_spec(spec, orientation, to_stored=False)


def new_sync_id() -> str:
    """生成 ACR 用的同步 ID（32 位十六进制大写）。

    样本里 CorrectionSyncID / MaskSyncID 都是这个形态（随机、无校验位）。
    每次生成都用系统随机源，避免两份文件撞 ID。
    """
    return secrets.token_hex(16).upper()


def _fmt(value: float) -> str:
    """数值格式化：整数不带小数点，小数最多 6 位（ACR 自己也是这么写的）。"""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text


def _fmt_signed(value: float) -> str:
    """颜色分级族的写法：正数带加号（样本里是 +38 / +81 / -37 / +50）。"""
    text = _fmt(abs(value))
    return f"-{text}" if value < 0 else f"+{text}"


def _check_unit(value: Any, what: str, warnings: list[str]) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        warnings.append(f"{what} 不是数字（{value!r}），按 0 处理")
        return 0.0
    if not (0.0 <= number <= 1.0):
        clamped = min(1.0, max(0.0, number))
        warnings.append(f"{what} 超出 0..1（{number}），已夹到 {clamped}")
        return clamped
    return number


def normalize_local(local: dict[str, Any] | None, warnings: list[str]) -> dict[str, str]:
    """校验并归一化局部参数，返回"属性名 → 存储字符串值"（基础段含全部缺省项）。

    **对外一律用面板值**（就是你在 ACR 面板上看到的那个数）：
        普通 Local* 滑块  -100..100（高光/阴影/饱和度/纹理…）
        局部曝光          -4.00..4.00（单位 EV）
        颜色分级色相      0..360；色分族的饱和度/亮度/平衡  -100..100
        局部曲线饱和度    0..100
    写入时再乘 `_LOCAL_SCALES` 换成 ACR 的存储值。
    """
    result = dict(_LOCAL_DEFAULTS)
    for name, raw_value in (local or {}).items():
        if name not in _LOCAL_RANGES:
            warnings.append(f"未知局部参数 {name}，已忽略")
            continue
        lo, hi = _LOCAL_RANGES[name]
        try:
            panel = float(raw_value)
        except (TypeError, ValueError):
            warnings.append(f"局部参数 {name} 不是数字（{raw_value!r}），已忽略")
            continue
        if not (lo <= panel <= hi):
            clamped = min(hi, max(lo, panel))
            warnings.append(f"局部参数 {name}={panel} 超出面板范围 {lo}..{hi}，已夹到 {clamped}")
            panel = clamped
        stored = panel * _LOCAL_SCALES[name]
        result[name] = _fmt_signed(stored) if name in _LOCAL_COLORGRADE_FIELDS else _fmt(stored)
    return result


def _add_common_mask_attrs(element: ET.Element, *, name: str, inverted: bool, sync_id: str,
                           value: str = "1") -> None:
    element.set(NS.qname("crs", "MaskActive"), "true")
    element.set(NS.qname("crs", "MaskName"), name)
    element.set(NS.qname("crs", "MaskBlendMode"), "0")
    element.set(NS.qname("crs", "MaskInverted"), "true" if inverted else "false")
    element.set(NS.qname("crs", "MaskSyncID"), sync_id)
    element.set(NS.qname("crs", "MaskValue"), value)


def _gradient_element(spec: dict[str, Any], warnings: list[str]) -> ET.Element:
    element = ET.Element(NS.LI_TAG)
    _add_common_mask_attrs(
        element,
        name=str(spec.get("name") or "线性渐变"),
        inverted=bool(spec.get("inverted")),
        sync_id=new_sync_id(),
    )
    # What 是"这个 li 是什么"的类型标记，样本里写成属性。
    element.set(NS.qname("crs", "What"), "Mask/Gradient")
    zero = spec.get("zero") or (0.5, 1.0)
    full = spec.get("full") or (0.5, 0.0)
    element.set(NS.qname("crs", "ZeroX"), _fmt(_check_unit(zero[0], "线性渐变 ZeroX", warnings)))
    element.set(NS.qname("crs", "ZeroY"), _fmt(_check_unit(zero[1], "线性渐变 ZeroY", warnings)))
    element.set(NS.qname("crs", "FullX"), _fmt(_check_unit(full[0], "线性渐变 FullX", warnings)))
    element.set(NS.qname("crs", "FullY"), _fmt(_check_unit(full[1], "线性渐变 FullY", warnings)))
    return element


def _radial_element(spec: dict[str, Any], warnings: list[str]) -> ET.Element:
    element = ET.Element(NS.LI_TAG)
    _add_common_mask_attrs(
        element,
        name=str(spec.get("name") or "径向渐变"),
        inverted=bool(spec.get("inverted")),
        sync_id=new_sync_id(),
    )
    element.set(NS.qname("crs", "What"), "Mask/CircularGradient")
    top, left, bottom, right = spec.get("rect") or (0.25, 0.25, 0.75, 0.75)
    for attr, value, what in (("Top", top, "径向 Top"), ("Left", left, "径向 Left"),
                              ("Bottom", bottom, "径向 Bottom"), ("Right", right, "径向 Right")):
        element.set(NS.qname("crs", attr), _fmt(_check_unit(value, what, warnings)))
    try:
        angle = float(spec.get("angle") or 0.0)
    except (TypeError, ValueError):
        angle = 0.0
    element.set(NS.qname("crs", "Angle"), _fmt(angle))
    try:
        midpoint = min(100.0, max(0.0, float(spec.get("midpoint", 50.0))))
        feather = min(100.0, max(0.0, float(spec.get("feather", 50.0))))
        roundness = min(100.0, max(-100.0, float(spec.get("roundness", 0.0))))
    except (TypeError, ValueError):
        midpoint, feather, roundness = 50.0, 50.0, 0.0
    element.set(NS.qname("crs", "Midpoint"), _fmt(midpoint))
    element.set(NS.qname("crs", "Roundness"), _fmt(roundness))
    element.set(NS.qname("crs", "Feather"), _fmt(feather))
    element.set(NS.qname("crs", "Flipped"), "true" if spec.get("flipped") else "false")
    element.set(NS.qname("crs", "Version"), "2")
    return element


def _brush_element(spec: dict[str, Any], warnings: list[str]) -> ET.Element:
    """画笔：Mask/Aggregate 包一层 Mask/Paint + Dabs。

    【2026-09-23 实测定罪】图标/径向渐变这两种"叶子"蒙版，ACR 是把属性**直接写在
    `<rdf:li>` 上**的；但画笔（Aggregate，带子元素）在样本里是
    `<rdf:li>` → `<rdf:Description What="Mask/Aggregate" …>` → `<crs:Masks>`。
    我们第一版图省事把 Aggregate 的属性也写在 `rdf:li` 上 —— ACR 里**画笔完全不出现**
    （你在 A/B 试验里报告的"没有画笔蒙版"）。所以这里严格照样本：多套一层 Description。
    """
    aggregate = ET.Element(NS.LI_TAG)
    holder = ET.SubElement(aggregate, NS.DESCRIPTION_TAG)
    _add_common_mask_attrs(
        holder,
        name=str(spec.get("name") or "画笔"),
        inverted=False,
        sync_id=new_sync_id(),
    )
    holder.set(NS.qname("crs", "What"), "Mask/Aggregate")

    masks = ET.SubElement(holder, NS.qname("crs", "Masks"))
    seq = ET.SubElement(masks, NS.SEQ_TAG)
    li = ET.SubElement(seq, NS.LI_TAG)

    paint = ET.SubElement(li, NS.DESCRIPTION_TAG)
    # 样本里 Paint 上没有 MaskName，属性就这几个（CenterWeight 是 0）。
    paint.set(NS.qname("crs", "What"), "Mask/Paint")
    paint.set(NS.qname("crs", "MaskActive"), "true")
    paint.set(NS.qname("crs", "MaskBlendMode"), "0")
    paint.set(NS.qname("crs", "MaskInverted"), "false")
    paint.set(NS.qname("crs", "MaskSyncID"), new_sync_id())
    paint.set(NS.qname("crs", "MaskValue"), "1")

    try:
        radius = min(0.5, max(0.001, float(spec.get("radius", 0.1))))
    except (TypeError, ValueError):
        radius = 0.1
    try:
        flow = min(1.0, max(0.05, float(spec.get("flow", 0.6))))
    except (TypeError, ValueError):
        flow = 0.6
    try:
        center_weight = float(spec.get("center_weight", 0.0) or 0.0)
    except (TypeError, ValueError):
        center_weight = 0.0
    paint.set(NS.qname("crs", "Radius"), _fmt(radius))
    paint.set(NS.qname("crs", "Flow"), _fmt(flow))
    paint.set(NS.qname("crs", "CenterWeight"), _fmt(center_weight))

    dabs = spec.get("dabs") or []
    clean_dabs: list[tuple[float, float]] = []
    for index, point in enumerate(dabs):
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            warnings.append(f"画笔第 {index + 1} 个落点不合法，已跳过")
            continue
        clean_dabs.append((_check_unit(x, f"画笔落点 {index + 1} X", warnings),
                           _check_unit(y, f"画笔落点 {index + 1} Y", warnings)))
    if not clean_dabs:
        warnings.append("画笔没有有效落点，已退化为「画一个中心点」")
        clean_dabs = [(0.5, 0.5)]

    dabs_element = ET.SubElement(paint, NS.qname("crs", "Dabs"))
    dabs_seq = ET.SubElement(dabs_element, NS.SEQ_TAG)
    # 样本的写法是**逐点成对**：r 半径 → d x y → r 半径 → d x y …，
    # 流量 f 只出现在第一个点之后（样本：r 0.127519 / f 0.6484 / d … / r … / d …）。
    first = True
    for x, y in clean_dabs:
        r_li = ET.SubElement(dabs_seq, NS.LI_TAG)
        r_li.text = f"r {_fmt(radius)}"
        if first:
            f_li = ET.SubElement(dabs_seq, NS.LI_TAG)
            f_li.text = f"f {_fmt(flow)}"
            first = False
        d_li = ET.SubElement(dabs_seq, NS.LI_TAG)
        # 落点用固定 6 位小数（样本：d 0.492500 0.203529 —— 末尾的 0 不删）。
        d_li.text = f"d {x:.6f} {y:.6f}"
    return aggregate


def build_masks(specs: list[dict[str, Any]] | None) -> tuple[ET.Element | None, list[str], list[str]]:
    """把蒙版规格构造成 `<crs:MaskGroupBasedCorrections>` 元素。

    返回 (元素或 None, 写进去的描述清单, 警告)。specs 为空时返回 (None, [], [])。
    """
    warnings: list[str] = []
    written: list[str] = []
    if not specs:
        return None, written, warnings
    if len(specs) > MAX_MASKS_PER_IMAGE:
        warnings.append(
            f"蒙版数量 {len(specs)} 超过上限 {MAX_MASKS_PER_IMAGE}，只保留前 {MAX_MASKS_PER_IMAGE} 个"
        )
        specs = specs[:MAX_MASKS_PER_IMAGE]

    container = ET.Element(NS.qname("crs", "MaskGroupBasedCorrections"))
    seq = ET.SubElement(container, NS.SEQ_TAG)

    for index, spec in enumerate(specs):
        kind = str(spec.get("type") or "").strip().lower()
        if kind not in MASK_KINDS:
            warnings.append(f"第 {index + 1} 个蒙版类型 {kind!r} 不支持（只支持 {MASK_KINDS}），已跳过")
            continue
        li = ET.SubElement(seq, NS.LI_TAG)
        correction = ET.SubElement(li, NS.DESCRIPTION_TAG)
        correction.set(NS.qname("crs", "What"), "Correction")
        correction.set(NS.qname("crs", "CorrectionAmount"), "1")
        correction.set(NS.qname("crs", "CorrectionActive"), "true")
        display_name = str(spec.get("correction_name") or spec.get("name") or f"蒙版 {index + 1}")
        correction.set(NS.qname("crs", "CorrectionName"), display_name)
        correction.set(NS.qname("crs", "CorrectionSyncID"), new_sync_id())
        for name, value in normalize_local(spec.get("local"), warnings).items():
            correction.set(NS.qname("crs", name), value)

        # 可选的局部曲线（样本第 1 组带 crs:MainCurve，形如 "84,55"）。
        curve_points = spec.get("main_curve") or []
        if curve_points:
            curve_element = ET.SubElement(correction, NS.qname("crs", "MainCurve"))
            curve_seq = ET.SubElement(curve_element, NS.SEQ_TAG)
            for point in curve_points:
                point_li = ET.SubElement(curve_seq, NS.LI_TAG)
                point_li.text = str(point)

        masks_element = ET.SubElement(correction, NS.qname("crs", "CorrectionMasks"))
        masks_seq = ET.SubElement(masks_element, NS.SEQ_TAG)
        if kind == "linear":
            masks_seq.append(_gradient_element(spec, warnings))
        elif kind == "radial":
            masks_seq.append(_radial_element(spec, warnings))
        else:
            masks_seq.append(_brush_element(spec, warnings))
        written.append(f"{kind}:{display_name}")

    if not written:
        return None, written, warnings
    return container, written, warnings


def _pair(value: Any) -> tuple[float, float] | None:
    """把 [x, y] / (x, y) 解析成两个浮点；不合法返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _quad(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None


def _gradient_from_rect(
    rect: tuple[float, float, float, float] | None,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """从 [上, 左, 下, 右] 推 zero/full（线性渐变的容错入口）。

    为什么需要：实测 qwen 会把**所有**蒙版的几何都写成 rect（即使说明里写了
    linear 要 zero/full）—— 结果三个蒙版全被丢弃。但方向不能猜：
    规则是「区域贴着哪条画框边缘，就从那条边出发朝对侧衰减」；
    不贴任何边就返回 None（方向不明的渐变是坏数据，宁可丢弃）。
    渐变轴取区域较长的那一边：横长就左右渐变，纵长或正方形就上下渐变。
    """
    if rect is None:
        return None
    top, left, bottom, right = (min(1.0, max(0.0, v)) for v in rect)
    if bottom < top:
        top, bottom = bottom, top
    if right < left:
        left, right = right, left
    mid_x = (left + right) / 2
    mid_y = (top + bottom) / 2
    edge = 0.02
    if (right - left) >= (bottom - top):        # 横长 → 左右渐变
        if left <= edge:
            return (right, mid_y), (left, mid_y)
        if right >= 1 - edge:
            return (left, mid_y), (right, mid_y)
    else:                                       # 纵长 → 上下渐变
        if top <= edge:
            return (mid_x, bottom), (mid_x, top)
        if bottom >= 1 - edge:
            return (mid_x, top), (mid_x, bottom)
    return None


def _dabs_from_rect(
    rect: tuple[float, float, float, float] | None,
    cols: int = 5,
    rows: int = 5,
) -> list[tuple[float, float]]:
    """把 [上, 左, 下, 右] 区域铺成网格落点（画笔的容错入口）。

    “涂这块地方”用网格落点表达与模型的原意一致，比丢弃更接近它想干的活；
    5×5=25 个落点在 MAX_BRUSH_DABS（60）以内。
    """
    if rect is None:
        return []
    top, left, bottom, right = rect
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    if right - left <= 0 or bottom - top <= 0:
        return []
    xs = [left + (right - left) * (i + 0.5) / cols for i in range(cols)]
    ys = [top + (bottom - top) * (j + 0.5) / rows for j in range(rows)]
    return [(round(x, 4), round(y, 4)) for y in ys for x in xs]


def parse_specs(
    raw: Any,
    *,
    max_masks: int = PROGRAM_MAX_MASKS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把**模型返回的**蒙版意图解析成内部规格（显示帧几何 + 受限的局部参数）。

    这是"AI 提意图、程序定实现"的接口。三条硬规矩：

    1. **几何必须是显示帧坐标**（人看到的"天空在上方"），写入前由
       `display_to_stored_spec()` 按每张照片自己的 EXIF 方向换算 ——
       这是本会话踩过的最贵的一个坑（方向差 90°，蒙版整体跑到别处）。
    2. **局部参数只开一小扇门**（见 `AI_LOCAL_RANGES`）：只允许方向性的明暗/质感，
       且上限比面板值域更紧；未知字段一律丢弃并记警告，不静默放行。
    3. **数量与落点都有上限**：一张图最多 `max_masks` 个，画笔最多 `MAX_BRUSH_DABS` 个落点。

    返回 (规格列表, 警告列表)。任何一条不合法都只丢那一条，不影响其余。
    """
    warnings: list[str] = []
    specs: list[dict[str, Any]] = []
    if raw is None:
        return specs, warnings
    if not isinstance(raw, list):
        return specs, [f"masks 不是数组（实际 {type(raw).__name__}），已忽略"]

    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            warnings.append(f"第 {index} 个蒙版不是对象，已忽略")
            continue
        kind = str(entry.get("kind") or entry.get("type") or "").strip().lower()
        if kind not in MASK_KINDS:
            warnings.append(f"第 {index} 个蒙版的类型 {kind!r} 不支持（只支持 {MASK_KINDS}），已忽略")
            continue

        target = str(entry.get("target") or "").strip()
        intent = str(entry.get("intent") or "").strip()
        focus = target or f"局部 {index}"
        spec: dict[str, Any] = {
            "type": kind,
            "name": focus,
            "correction_name": f"{focus}（{intent}）" if intent else focus,
        }

        if kind == "linear":
            zero, full = _pair(entry.get("zero")), _pair(entry.get("full"))
            if zero is None or full is None:
                derived = _gradient_from_rect(_quad(entry.get("rect")))
                if derived is None:
                    warnings.append(f"第 {index} 个线性渐变缺少 zero/full 坐标（显示帧 0..1），已忽略")
                    continue
                zero, full = derived
                warnings.append(
                    f"第 {index} 个线性渐变只给了 rect，已按「从贴边的一侧朝对侧衰减」"
                    f"解释为 zero={tuple(round(v, 2) for v in zero)} / "
                    f"full={tuple(round(v, 2) for v in full)}"
                )
            spec["zero"], spec["full"] = zero, full
        elif kind == "radial":
            rect = _quad(entry.get("rect"))
            if rect is None:
                warnings.append(f"第 {index} 个径向渐变缺少 rect（显示帧 [上, 左, 下, 右]），已忽略")
                continue
            spec["rect"] = rect
            for key in ("angle", "midpoint", "roundness", "feather"):
                if entry.get(key) is not None:
                    spec[key] = entry[key]
            if entry.get("flipped") is not None:
                spec["flipped"] = bool(entry["flipped"])
        else:  # brush
            raw_dabs = entry.get("dabs")
            dabs: list[tuple[float, float]] = []
            if isinstance(raw_dabs, list):
                for point in raw_dabs[:MAX_BRUSH_DABS]:
                    pair = _pair(point)
                    if pair is not None:
                        dabs.append(pair)
            if not dabs:
                dabs = _dabs_from_rect(_quad(entry.get("rect")))
                if dabs:
                    warnings.append(
                        f"第 {index} 个画笔只给了 rect，已按该区域生成 {len(dabs)} 个落点"
                    )
            if not dabs:
                warnings.append(f"第 {index} 个画笔没有有效落点（显示帧 0..1），已忽略")
                continue
            spec["dabs"] = dabs
            for key in ("radius", "flow", "center_weight"):
                if entry.get(key) is not None:
                    spec[key] = entry[key]

        local_raw = entry.get("local")
        local: dict[str, float] = {}
        if isinstance(local_raw, dict):
            for name, value in local_raw.items():
                limits = AI_LOCAL_RANGES.get(name)
                if limits is None:
                    warnings.append(f"第 {index} 个蒙版里的局部参数 {name} 不在允许清单里，已忽略")
                    continue
                lo, hi = limits
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    warnings.append(f"第 {index} 个蒙版的 {name} 不是数字（{value!r}），已忽略")
                    continue
                if not (lo <= number <= hi):
                    clamped = min(hi, max(lo, number))
                    warnings.append(
                        f"第 {index} 个蒙版的 {name}={number} 超出允许范围 {lo:g}..{hi:g}，已夹到 {clamped:g}"
                    )
                    number = clamped
                local[name] = number
        if not local:
            warnings.append(f"第 {index} 个蒙版没有任何有效的局部参数，已忽略（空蒙版没有意义）")
            continue
        spec["local"] = local
        specs.append(spec)

    if len(specs) > max_masks:
        warnings.append(f"蒙版数量 {len(specs)} 超过上限 {max_masks}，只保留前 {max_masks} 个")
        specs = specs[:max_masks]

    # 每图总量：三个蒙版各推一点、方向一致时合起来会过头（肤色最明显）。
    for name, total_cap in AI_LOCAL_IMAGE_TOTALS.items():
        signed_total = sum(float(spec["local"].get(name) or 0.0) for spec in specs)
        if abs(signed_total) <= total_cap:
            continue
        factor = total_cap / abs(signed_total)
        for spec in specs:
            if name in spec["local"]:
                spec["local"][name] = round(float(spec["local"][name]) * factor, 2)
        warnings.append(
            f"本图 {name} 合计 {signed_total:.1f} 超过总量上限 ±{total_cap:g}，"
            f"已按比例缩到 {factor:.2f} 倍"
        )
    return specs, warnings


def apply_masks(doc, specs: list[dict[str, Any]] | None) -> tuple[list[str], list[str]]:
    """把蒙版写进文档（追加到主 Description 末尾，与样本顺序一致）。

    返回 (写进去的蒙版清单, 警告)。已有蒙版**不动**：同文件里已存在
    crs:MaskGroupBasedCorrections 时直接返回并给警告（这一版不做合并）。
    """
    from . import writer as W  # 延迟导入，避免循环

    container, written, warnings = build_masks(specs)
    if container is None:
        return written, warnings

    primary = W._primary_description(doc)
    existing = primary.find(NS.qname("crs", "MaskGroupBasedCorrections"))
    if existing is not None:
        warnings.append("该文件已有蒙版组，本次跳过蒙版写入（不覆盖你的手工蒙版）")
        return [], warnings
    primary.append(container)
    log.info("已写入 %d 个几何蒙版：%s", len(written), "、".join(written))
    return written, warnings


def clear_masks(doc) -> int:
    """删掉文档里所有的蒙版组，返回删掉的个数（用户明确要求"清空重来"时才用）。

    为什么单独做一个开关：默认铁律是"用户的蒙版一个字节不碰"，
    但做蒙版试验时正相反 —— 旧蒙版太多会把新写的挡住看不见。
    所以清空必须是**显式**调用（write_sidecar(clear_masks=True)），不设默认值。
    """
    if doc.root is None:
        return 0
    tag = NS.qname("crs", "MaskGroupBasedCorrections")
    removed = 0
    for parent in list(doc.root.iter()):
        for child in list(parent):
            if child.tag == tag:
                parent.remove(child)
                removed += 1
    if removed:
        log.info("已清除 %d 个蒙版组（按调用方要求）", removed)
    return removed
