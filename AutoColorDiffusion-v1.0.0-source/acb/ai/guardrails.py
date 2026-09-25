# -*- coding: utf-8 -*-
"""输出侧的程序级护栏（实施步骤 3）。

【为什么要有这一层】用户 2026-09-23 观察到的两个病症，都不能只靠提示词祈祷：

1. **照抄统计中位数**
   实测：同一风格跑一批图，写出的 18 份侧车里 `Exposure2012` 全是 +0.30、
   `Saturation` 全是 -8、`SaturationAdjustmentAqua` 全是 -52、`Temperature` 有 13 张是 +5500
   —— 全部等于训练统计出来的中位数/均值。提示词里已经写了「禁止照抄」，
   但提示词是"软约束"，所以这里再加一道**可观测的硬检查**：`detect_copying()`。

2. **过饱和 / 偏色（两种风格相反的失败方向）**
   样本少的风格容易偏色（某些字段 n=1、std 巨大），样本多的风格容易过饱和
   （所有面板一起往前推）。提示词解决不了"总量"问题，所以这里给
   **分组预算**：`apply_group_budgets()` 把饱和度类字段的加权总量、以及
   镜头晕影 + 裁剪后晕影的合计暗角量都压在上限内。

3. **极端值必须有依据**
   `clamp_extremes()`：单字段幅度超过该字段的"无规则上限"、而风格的规则里
   又没提到它，就夹回上限并记一条说明。这治的是"少数离群字段把整张图带跑"
   （实测危险例子：`LuminanceAdjustmentGreen +41`（n=2, std=59）、
   `SaturationAdjustmentAqua -52`（n=1）、`Temperature` 均值 5360 而 std 602.8）。

所有函数都是**纯函数**（输入 params / 风格 / 统计量，返回新 params 与人类可读说明），
这样自检可以直接断言，不需要联网或开界面。
"""

from __future__ import annotations

from typing import Any

from ..xmp import fields as F

# ---------------------------------------------------------------------------
# 一、分组预算
# ---------------------------------------------------------------------------
# 饱和度是"总量"概念：每个滑块单独看都在 -100..100 之内，十个滑块一起推就会过饱和。
# 权重是**可解释的**工程取值，不是审美判断：
#   · Saturation 一视同仁地推所有颜色 → 权重 1.0；
#   · Vibrance 保护肤色 → 0.7；
#   · HSL 分区饱和度只影响一种颜色 → 0.5；
#   · 分离色调 / 颜色分级 / 校准的饱和度也各只影响一段或一个通道 → 0.4 / 0.4 / 0.5。
SATURATION_WEIGHTS: dict[str, float] = {
    "Saturation": 1.0,
    "Vibrance": 0.7,
    "SplitToningShadowSaturation": 0.4,
    "SplitToningHighlightSaturation": 0.4,
    "ColorGradeShadowSat": 0.4,
    "ColorGradeMidtoneSat": 0.4,
    "ColorGradeHighlightSat": 0.4,
    "ColorGradeGlobalSat": 0.4,
    "RedSaturation": 0.5,
    "GreenSaturation": 0.5,
    "BlueSaturation": 0.5,
}
for _color in ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"):
    SATURATION_WEIGHTS[f"SaturationAdjustment{_color}"] = 0.5

# 加权总量上限：80 ≈ "大半个饱和度滑块" 的额外饱和度。
# 用户实测的失败方向是过饱和（样本多的风格所有面板一起推），所以这个上限故意取得保守；
# 它是**程序侧的工程边界**（可调），不是审美方向。
SATURATION_BUDGET_POSITIVE = 80.0
SATURATION_BUDGET_NEGATIVE = 80.0

# 晕影总预算：镜头晕影（VignetteAmount）与裁剪后晕影（PostCropVignetteAmount）
# 是**两个都会压暗四角**的滑块，一起用就是双重暗角。合计压暗量不超过 -35。
VIGNETTE_FIELDS: tuple[str, ...] = ("VignetteAmount", "PostCropVignetteAmount")
VIGNETTE_BUDGET = -35.0


def apply_group_budgets(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把"分组总量"压进预算，返回 (新 params, 说明列表)。

    只在**超预算**时改动，且按比例缩放（不改变各字段之间的相对关系）。
    """
    notes: list[str] = []
    result = dict(params)

    # --- 饱和度总量 ---
    for sign, budget, label in ((1.0, SATURATION_BUDGET_POSITIVE, "提饱和"),
                                (-1.0, SATURATION_BUDGET_NEGATIVE, "降饱和")):
        total = 0.0
        active: list[str] = []
        for name, weight in SATURATION_WEIGHTS.items():
            value = result.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if sign * float(value) <= 0:
                continue
            total += abs(float(value)) * weight
            active.append(name)
        if total <= budget or not active:
            continue
        factor = budget / total
        for name in active:
            result[name] = _round_value(name, float(result[name]) * factor)
        notes.append(
            f"{label}总量 {total:.0f} 超过预算 {budget:.0f}（涉及 {len(active)} 个字段），"
            f"已按比例缩到 {factor:.2f} 倍"
        )

    # --- 晕影总预算 ---
    darkening = {
        name: float(result[name])
        for name in VIGNETTE_FIELDS
        if isinstance(result.get(name), (int, float)) and not isinstance(result.get(name), bool)
        and float(result[name]) < 0
    }
    total_dark = sum(darkening.values())
    if total_dark < VIGNETTE_BUDGET:
        # 优先保镜头校正（它通常来自配置文件、也是用户"跟随机内"的习惯），
        # 先压裁剪后晕影；仍不够再动镜头晕影。
        for name in ("PostCropVignetteAmount", "VignetteAmount"):
            if name not in darkening:
                continue
            excess = VIGNETTE_BUDGET - total_dark
            new_value = max(VIGNETTE_BUDGET, darkening[name] + excess)
            result[name] = _round_value(name, new_value)
            total_dark = sum(float(result[n]) for n in darkening)
            if total_dark >= VIGNETTE_BUDGET:
                break
        notes.append(
            f"晕影合计 {sum(darkening.values()):.0f} 超过预算 {VIGNETTE_BUDGET:.0f}"
            "（镜头晕影 + 裁剪后晕影会双重压暗四角），已按预算收紧"
        )

    return result, notes


def _round_value(name: str, value: float) -> Any:
    """按字段类型回整/保留小数（写 XMP 时的格式化由 writer 负责，这里保证类型合理）。"""
    spec = F.get_field(name)
    if spec is not None and spec.kind == F.KIND_INT:
        return int(round(value))
    return round(value, 2)


# ---------------------------------------------------------------------------
# 二、极端值需要有依据
# ---------------------------------------------------------------------------
# 每个字段的"无规则上限"：超过它就必须在风格的规则里被提到过，否则夹回。
# 这些上限是**程序侧的保守边界**（把用户观察到的危险幅度收一半左右），
# 不是审美方向；用户若确实需要更大的幅度，会在规则里写出来（规则由训练得出）。
EXTREME_LIMITS: dict[str, float] = {
    "Exposure2012": 0.7,
    "Contrast2012": 30,
    "Highlights2012": 50,
    "Shadows2012": 50,
    "Whites2012": 40,
    "Blacks2012": 40,
    "Vibrance": 35,
    "Saturation": 30,
    "Texture": 40,
    "Clarity2012": 40,
    "Dehaze": 30,
    "Glow": 50,
    "GlowRange": 60,
    "GlowSpread": 60,
    "GlowWarmth": 60,
    "Sharpness": 50,
    "LuminanceSmoothing": 50,
    "ColorNoiseReduction": 50,
    "PostCropVignetteAmount": 30,
    "VignetteAmount": 30,
    "ShadowTint": 40,
    "RedHue": 30, "RedSaturation": 30,
    "GreenHue": 30, "GreenSaturation": 30,
    "BlueHue": 30, "BlueSaturation": 30,
    "SplitToningShadowSaturation": 50,
    "SplitToningHighlightSaturation": 50,
    "ColorGradeShadowSat": 50,
    "ColorGradeMidtoneSat": 50,
    "ColorGradeHighlightSat": 50,
    "ColorGradeGlobalSat": 50,
    "GrainAmount": 60,
}
for _color in ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"):
    for _kind in ("Hue", "Saturation", "Luminance"):
        EXTREME_LIMITS[f"{_kind}Adjustment{_color}"] = 45


# 颜色与调整种类的中文别名：风格规则是模型写的自然语言，
# 它很可能写"Aqua 去饱和""青绿降饱和"而不是写字段名，所以匹配要认得这些写法。
_COLOR_ALIASES: dict[str, tuple[str, ...]] = {
    "Red": ("红",), "Orange": ("橙", "橘"), "Yellow": ("黄",), "Green": ("绿",),
    "Aqua": ("Aqua", "青绿", "青"), "Blue": ("蓝",), "Purple": ("紫",),
    "Magenta": ("洋红", "品红", "紫红"),
}
_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "Hue": ("色相",), "Saturation": ("饱和",), "Luminance": ("明亮度", "亮度"),
    "Sat": ("饱和",), "Lum": ("明亮度", "亮度"),
}


def _rule_keywords(name: str) -> set[str]:
    """字段名 → 在规则文本里可能出现的写法（字段名、面板名及其片段、分组名、中文别名）。"""
    keywords: set[str] = {name}
    spec = F.get_field(name)
    if spec is not None:
        keywords.add(spec.panel.replace(" · ", ""))
        keywords.add(spec.panel)
        # 面板名是「基本 · 高光」这种带分隔的写法，而规则里通常只写「高光」。
        # 所以把每个片段也当关键词（实测：漏了这一步，“高光一律压到 -80”这句规则
        # 认不出 Highlights2012，于是本该放行的极端值被夹，自检直接变红）。
        for piece in spec.panel.replace("·", " ").split():
            if len(piece) >= 2:
                keywords.add(piece)
        label = F.GROUP_LABELS.get(spec.group)
        if label:
            keywords.add(label)
            head = label.split()[0] if label.split() else ""
            if len(head) >= 3:
                keywords.add(head)
    for color, aliases in _COLOR_ALIASES.items():
        if color in name:
            keywords.update(aliases)
    for kind, aliases in _KIND_ALIASES.items():
        if kind in name:
            keywords.update(aliases)
    return {k for k in keywords if k}


def _confidence_factor(name: str, style: dict[str, Any] | None) -> tuple[float, str]:
    """按训练统计的**置信度**收紧上限：样本太少或离散太大时，宁可少给幅度。

    依据（用户实测的危险例子）：
        LuminanceAdjustmentGreen +41（n=2, std=59）、SaturationAdjustmentAqua -52（n=1）
        —— 这些值不是我发明的，但支撑它们的样本太少，把它当成"每张图的目标值"
        就是少样本风格偏色的直接原因。
    """
    if not isinstance(style, dict):
        return 1.0, ""
    stat = (style.get("param_ranges") or {}).get(name)
    if not isinstance(stat, dict):
        return 1.0, ""
    try:
        count = int(stat.get("count") or 0)
        std = abs(float(stat.get("std") or 0.0))
    except (TypeError, ValueError):
        return 1.0, ""
    reasons: list[str] = []
    factor = 1.0
    if 0 < count <= 2:
        factor *= 0.5
        reasons.append(f"训练样本只有 {count} 张")
    if std >= 40:
        factor *= 0.7
        reasons.append(f"离散度 std={std:.0f} 很大")
    return factor, "；".join(reasons)


def _covered_by_rules(name: str, rule_texts: list[str]) -> bool:
    """这个字段有没有被风格规则"提到"（启发式：字段名/面板名/分组名出现在规则文本里）。

    为什么用启发式：规则是模型写的自然语言，不可能要求它逐字写字段名。
    所以这里只做"宽松匹配"，宁可判为"有依据"（不夹），因为过度夹紧会抹掉用户的风格；
    真正被夹的情况会写进日志，用户可以据此在训练素材里补一条规则。
    """
    keywords = _rule_keywords(name)
    for text in rule_texts:
        lowered = str(text)
        if any(keyword and keyword in lowered for keyword in keywords):
            return True
    return False


def clamp_extremes(params: dict[str, Any], style: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """幅度超过"无规则上限"且风格规则没提到的字段，夹回上限。

    上限还会按该字段在训练统计里的**置信度**自动收紧（样本 ≤2 张 → 减半；
    std ≥ 40 → 再乘 0.7），理由见 _confidence_factor。
    """
    rule_texts: list[str] = []
    if isinstance(style, dict):
        rule_texts.extend(style.get("text_rules") or [])
        for rule in style.get("scene_rules") or []:
            if isinstance(rule, dict):
                rule_texts.append(str(rule.get("text") or ""))
                rule_texts.append(str(rule.get("scene") or ""))

    notes: list[str] = []
    result = dict(params)
    for name, value in params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        limit = EXTREME_LIMITS.get(name)
        if limit is None:
            continue
        factor, confidence_note = _confidence_factor(name, style)
        limit *= factor
        if abs(float(value)) <= limit:
            continue
        if _covered_by_rules(name, rule_texts):
            continue
        clamped = _round_value(name, limit if float(value) > 0 else -limit)
        result[name] = clamped
        reason = f"超过无规则上限 ±{limit:g}"
        if confidence_note:
            reason += f"，上限已因置信度收紧（{confidence_note}）"
        else:
            reason += "，风格规则里没有提到它"
        notes.append(f"{name} 由 {value} 夹到 {clamped}（{reason}）")
    return result, notes


# ---------------------------------------------------------------------------
# 三、照抄检测
# ---------------------------------------------------------------------------
# 判据（两个条件同时成立才算"照抄"）：
#   1. 同一字段在**多数图**上取值完全一样（同一值覆盖 ≥ COPY_MIN_RATIO 的图）；
#   2. 那个值等于（或非常接近）风格训练统计里的中位数/均值。
# 只满足第 1 条不算：有些字段本来就该整批一致（例如"启用配置文件校正"）。
COPY_MIN_RATIO = 0.8
COPY_MIN_IMAGES = 4
COPY_TOLERANCE = 0.005


def detect_copying(
    params_list: list[dict[str, Any]],
    style: dict[str, Any] | None,
) -> list[str]:
    """检测"把训练统计值当目标值照抄"的字段，返回人类可读的说明列表。"""
    if not params_list or not isinstance(style, dict):
        return []
    ranges = style.get("param_ranges") or {}
    if not isinstance(ranges, dict) or not ranges:
        return []

    total = len(params_list)
    if total < COPY_MIN_IMAGES:
        return []

    values_by_field: dict[str, list[float]] = {}
    for params in params_list:
        for name, value in params.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values_by_field.setdefault(name, []).append(float(value))

    offenders: list[str] = []
    for name, values in sorted(values_by_field.items()):
        if len(values) < total * COPY_MIN_RATIO:
            continue
        same_value = values[0]
        identical = sum(1 for v in values if abs(v - same_value) <= 1e-9)
        if identical < max(COPY_MIN_IMAGES, int(total * COPY_MIN_RATIO)):
            continue
        stat = ranges.get(name)
        if not isinstance(stat, dict):
            continue
        centers = [stat.get("median"), stat.get("mean")]
        near_center = any(
            isinstance(center, (int, float)) and abs(same_value - float(center)) <= COPY_TOLERANCE
            for center in centers
        )
        if near_center:
            offenders.append(
                f"{name}：{identical}/{total} 张都是 {same_value:g}，"
                f"正好等于训练统计的中位数/均值（{stat.get('median')}/{stat.get('mean')}）"
            )
    return offenders


def recap_for_log(copying: list[str], budget_notes: list[str], clamp_notes: list[str]) -> str:
    """把三类护栏结果压成一行日志（供"画像"行使用）。"""
    parts: list[str] = []
    if copying:
        parts.append(f"照抄告警 {len(copying)} 项")
    if budget_notes:
        parts.append(f"预算调整 {len(budget_notes)} 项")
    if clamp_notes:
        parts.append(f"极端夹紧 {len(clamp_notes)} 项")
    return "、".join(parts) if parts else ""
