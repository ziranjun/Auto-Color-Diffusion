# -*- coding: utf-8 -*-
"""风格档案生成（硬约束 #15）。

`style_profile.json` 的固定结构（硬约束要求"至少包含"，故本实现含扩展字段）：
    name            风格名（用户命名）
    version         档案结构版本 + 程序版本
    created_at      生成时间（ISO 8601，含时区）
    sample_count    参与统计的样本数
    param_ranges    各 crs 字段的 mean / median / min / max / count / std
    text_rules      自然语言描述的风格倾向与禁区
    ignore_fields   判定为相机或镜头默认值而排除的字段（每项附 reason）
    --- 以下为扩展 ---
    style_summary / saturation_tendency / contrast_tendency /
    shadow_color_cast / hsl_habits / lens_correction_preference /
    mask_usage / generator

核心难点：区分「用户实际调整的参数」与「相机/镜头默认值」（硬约束 #15）
------------------------------------------------------------------
关键认知：**ACR 会写出全量字段表**（实测样本里含大量 =0 的字段），
所以"字段存在"绝不等于"用户调整过它"。若直接把所有字段拿去算均值，
得到的结果会是一堆默认值，毫无参考价值。

本实现用三层差分（方案中的 A + C 组合）：
    第 1 层 —— crd: 差分（最权威）
        XMP 里的 crd: 命名空间是 "camera-raw-defaults"（相机出厂默认基线），
        实测与 crs: 同名同义字段并存（crd:CameraProfile / crd:LensProfileEnable）。
        判据：crs:X == crd:X → 用户没改过 X → 排除。
        这比"猜默认值"可靠得多，因为它是相机自己写下的基线。
    第 2 层 —— ACR 出厂默认值表差分
        用 ACR_DEFAULT_VALUES 比对该字段是否等于 ACR 的出厂默认。
    第 3 层 —— 批内不变项剔除
        整批取值完全一致的字段判为默认（例如 8/8 都是 ColorNoiseReduction=25）。
        这一层能抓住"相机写入的非标准默认值"，是纯静态默认表抓不到的。

三个来源综合后的每一项都写入 ignore_fields 并附 reason，
让用户能审计"为什么这个字段不算我的偏好"——否则统计结果不可信。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from .. import __version__
from ..constants import AUTOPILOT_STYLE_NAME
from ..logging_setup import get_logger
from ..paths import copy_tree_missing, resource_root, styles_dir
from ..xmp import fields as F

log = get_logger("style_profile")

# 档案结构版本。将来结构变化时用它做迁移判断（与程序版本分开，各自独立演进）。
PROFILE_STRUCTURE_VERSION = 1

# 参与统计所需的最小样本数（低于此值在界面上提示"样本不足，结果可能不稳定"）。
# 常量定义在 constants.TRAIN_MIN_SAMPLES_WARN（=10），此处仅作引用说明。

# --- ACR 出厂默认值表 -------------------------------------------------------
# 来源：ACR 各面板的滑块初值。表中只列**非零/非平凡默认值**，
# 未列出的**数值型**字段一律按 0 处理（ACR 绝大多数滑块默认就是 0）。
# 这些默认值全部在 8 个真实样本中得到交叉验证（整批恒定）。
#
# 值类型是 Any 而不是 float|bool：枚举型字段的默认值是字符串
# （例如 ToneCurveName2012 的默认是 "Linear"）。
# 这一点很关键——早期版本漏了枚举默认值，导致 ToneCurveName2012 在
# 8/8 样本里都被判成"用户调整"，因为它永远不等于数值 0 而被放行。
ACR_DEFAULT_VALUES: dict[str, Any] = {
    # 参数曲线的分区位置（ACR 默认把色调范围按 25/50/75 切开）
    "ParametricShadowSplit": 25,
    "ParametricMidtoneSplit": 50,
    "ParametricHighlightSplit": 75,
    # 细节面板默认值
    "Sharpness": 40,
    "SharpenRadius": 1.0,
    "SharpenDetail": 25,
    "ColorNoiseReduction": 25,
    "ColorNoiseReductionDetail": 50,
    "ColorNoiseReductionSmoothness": 50,
    # 镜头校正
    "AutoLateralCA": 1,
    "LensProfileDistortionScale": 100,
    "LensProfileVignettingScale": 100,
    # 去边的默认色相窗口
    "DefringePurpleHueLo": 30,
    "DefringePurpleHueHi": 70,
    "DefringeGreenHueLo": 40,
    "DefringeGreenHueHi": 60,
    # 颜色分级
    "ColorGradeBlending": 50,
    # 变换
    "PerspectiveScale": 100,
    # --- 枚举型默认值 ---
    # 曲线名称：只有用户手动拖过点曲线才会变成 "Custom"，
    # 否则 ACR 会写 "Linear"（或某个对比度预设）。
    "ToneCurveName2012": "Linear",
    # 镜头校正配置来源：跟随机内默认
    "LensProfileSetup": "LensDefaults",
}

# 批内不变项剔除的最小样本数。
# 依据：只有 2 个样本时"两者相同"可能纯属巧合，剔除会误伤真实偏好；
#       3 个以上相同才具备"这大概是默认值"的说服力。
BATCH_INVARIANT_MIN_SAMPLES = 3

# 判定"该字段属于默认值、应写入 ignore_fields"的样本比例阈值。
# 用 1.0（即**全部**样本都被排除）而不是常见的 0.8：
#   理由是这个列表会被直接塞进提示词，并以"已判定为相机/镜头默认值
#   （不反映用户偏好，请不要据此推断）"的措辞呈现。
#   若把一个"10 张里有 8 张是默认、但有 2 张被用户明确调过"的字段放进去，
#   等于在骗模型忽略用户的真实偏好——这比"漏报"严重得多。
#   因此这里取最保守的 1.0：**只有从未被调整过的字段**才是"默认值"。
#   部分被排除的字段会单独记入 partially_default_fields（不参与提示词），
#   信息不丢失，但不会误导统计。
FULLY_DEFAULT_RATIO = 1.0

# 参与统计的字段值域跨度小于此值时，不纳入 param_ranges 的报告。
# 依据：像 ParametricShadowSplit（0..100）这类字段即使被改动也很轻微，
#       在统计表里会淹没真正重要的字段（Exposure、Highlights 等）。
#       这里不删数据，只是不在 style_profile 里报告，避免提示词过长。
REPORT_MIN_SPAN = 0.0  # 0 表示全都报告；保留常量便于将来收紧

# 风格名 → 文件名的安全化：Windows 文件名非法字符替换为下划线。
# 非法字符集来自 Windows 的保留字符（< > : " / \ | ? * 与控制字符）。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_style_name(name: str) -> str:
    """把用户命名的风格名转成安全的文件名。"""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")  # Windows 不允许文件名以点或空格结尾
    if not cleaned:
        cleaned = "unnamed_style"
    # 限制长度，避免超出 Windows 的 260 字符路径限制。
    return cleaned[:80]


# ============================================================================
# 差分：区分用户调整与默认值
# ============================================================================

def acr_default_for(name: str) -> Any:
    """取某字段的 ACR 出厂默认值。"""
    if name in ACR_DEFAULT_VALUES:
        return ACR_DEFAULT_VALUES[name]
    spec = F.get_field(name)
    if spec is None:
        return None
    if spec.kind == F.KIND_BOOL:
        return False
    if spec.kind in (F.KIND_INT, F.KIND_REAL):
        return 0
    return None


def _lens_is_default(crs_raw: dict[str, str]) -> bool:
    """判断镜头校正是否处于"跟随机内默认"状态。

    实测判据（来自真实样本对比）：
        默认：LensProfileName="Camera Settings" + LensProfileIsEmbedded="True"
        用户显式选择：LensProfileName="Adobe (Canon EF-S 18-135mm f/3.5-5.6 IS USM)"
                      + 有 LensProfileFilename（对应的 .lcp 文件）
    只有用户真正指定了镜头配置文件，才算一次"镜头校正取舍"。
    """
    setup = (crs_raw.get("LensProfileSetup") or "").strip()
    name = (crs_raw.get("LensProfileName") or "").strip()
    embedded = (crs_raw.get("LensProfileIsEmbedded") or "").strip().lower()
    if setup == "LensDefaults":
        return True
    if name.lower() in ("camera settings", ""):
        if embedded in ("true", "1"):
            return True
    return False


def compute_adjustments(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """对单张样本做差分，返回 (用户实际调整的字段, 排除项列表)。

    排除项每项形如 {"field": ..., "reason": ..., "detail": ...}，
    reason 取值：
        crd_equals_crs      与相机出厂默认基线一致
        acr_factory_default 等于 ACR 出厂默认值
        lens_default        镜头校正处于跟随机内默认状态
        machine_field       机器/版本相关字段，本就不参与审美统计
    """
    params: dict[str, Any] = snapshot.get("params") or {}
    crs_raw: dict[str, str] = snapshot.get("crs_raw") or {}
    crd_raw: dict[str, str] = snapshot.get("crd_raw") or {}

    adjusted: dict[str, Any] = {}
    excluded: list[dict[str, str]] = []

    for name, value in params.items():
        spec = F.get_field(name)
        if spec is None:
            continue

        # 机器相关字段直接排除（它们本就不是审美参数）。
        if not spec.ai_writable:
            excluded.append({"field": name, "reason": "machine_field", "detail": spec.panel})
            continue

        # 第 1 层：crd: 差分。这是最权威的判据。
        crd_value = crd_raw.get(name)
        if crd_value is not None and str(value) == str(crd_value):
            excluded.append(
                {
                    "field": name,
                    "reason": "crd_equals_crs",
                    "detail": f"crs 与 crd(相机默认) 均为 {crd_value}",
                }
            )
            continue

        # 第 2 层：ACR 出厂默认值差分。
        default_value = acr_default_for(name)
        if default_value is not None and value == default_value:
            excluded.append(
                {
                    "field": name,
                    "reason": "acr_factory_default",
                    "detail": f"等于 ACR 默认值 {default_value}",
                }
            )
            continue

        # 第 2b 层：镜头校正的默认状态（跨多个字段的语义判据）。
        if name.startswith("LensProfile") and _lens_is_default(crs_raw):
            excluded.append(
                {
                    "field": name,
                    "reason": "lens_default",
                    "detail": "镜头校正处于「跟随机内默认」状态，未体现用户取舍",
                }
            )
            continue

        # 白平衡的绝对量陷阱：WhiteBalance != Custom 时，
        # Temperature/Tint 是被 ACR 忽略的记录值，不能计入偏好统计。
        if name in ("Temperature", "Tint") and not snapshot.get("wb_effective", False):
            excluded.append(
                {
                    "field": name,
                    "reason": "wb_not_effective",
                    "detail": "WhiteBalance 非 Custom，该数值被 ACR 忽略，不反映用户偏好",
                }
            )
            continue

        adjusted[name] = value

    return adjusted, excluded


def apply_batch_invariance(
    per_sample_adjusted: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """第 3 层：批内不变项剔除。

    若某字段在所有样本中取值完全一致，且样本数 ≥ BATCH_INVARIANT_MIN_SAMPLES，
    则判为默认值（相机写入的非标准默认值是静态默认表抓不到的，
    但"整批完全一样"是很强的信号）。

    为什么这条规则是安全的：
        它只影响"整批都相同"的字段。真实偏好极少表现为"所有照片分毫不差地
        用同一个数值"——那更可能是设备/软件的固定值。
        即使偶有误伤（用户确实给每一张都设了同一个值），
        该字段的 mean/median/range 也退化为一个点，对提示词的贡献本就很低。
    """
    exclusions: list[dict[str, str]] = []
    if len(per_sample_adjusted) < BATCH_INVARIANT_MIN_SAMPLES:
        return per_sample_adjusted, exclusions

    all_names: set[str] = set()
    for adjusted in per_sample_adjusted:
        all_names.update(adjusted.keys())

    invariants: dict[str, Any] = {}
    for name in sorted(all_names):
        values = [adjusted.get(name) for adjusted in per_sample_adjusted]
        if any(v is None for v in values):
            continue
        first = values[0]
        if all(v == first for v in values):
            invariants[name] = first
            exclusions.append(
                {
                    "field": name,
                    "reason": "batch_invariant",
                    "detail": f"{len(values)} 个样本取值完全一致（{first}），判为默认值",
                }
            )

    cleaned: list[dict[str, Any]] = []
    for adjusted in per_sample_adjusted:
        cleaned.append({k: v for k, v in adjusted.items() if k not in invariants})
    return cleaned, exclusions


# ============================================================================
# 统计
# ============================================================================

def compute_param_ranges(per_sample_adjusted: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """计算各字段的 mean / median / min / max / count / std。

    数值全部在本地用 statistics 计算，**不交给模型心算**——
    统计量必须可复现、可审计，这是把 AI 定位在"归纳语义"而非"做算术"的分工原则。

    分区纪律（问题 d 的"单位与比例换算"要求）：
        - HueAdjustment*（-100..100 相对量）与 SplitToning*Hue（0..360 绝对角）
          量纲不同，本函数按字段独立统计，绝不跨字段合并；
        - Temperature（开尔文，绝对量）只在 WhiteBalance=Custom 时才进入，
          且不与其他字段混合平均。
    """
    buckets: dict[str, list[float]] = {}
    for adjusted in per_sample_adjusted:
        for name, value in adjusted.items():
            spec = F.get_field(name)
            if spec is None or not spec.is_numeric:
                continue
            try:
                buckets.setdefault(name, []).append(float(value))
            except (TypeError, ValueError):
                continue

    ranges: dict[str, dict[str, float]] = {}
    for name, values in buckets.items():
        if len(values) < 2:
            # 单个样本无法算标准差，但仍给出该值本身，便于用户参考。
            ranges[name] = {
                "mean": round(values[0], 4),
                "median": round(values[0], 4),
                "min": round(values[0], 4),
                "max": round(values[0], 4),
                "count": 1,
                "std": 0.0,
            }
            continue
        ranges[name] = {
            "mean": round(mean(values), 4),
            "median": round(median(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "count": len(values),
            "std": round(pstdev(values), 4),
        }
    return ranges


# ============================================================================
# 维度力度档位（1..7 档）—— 实施步骤 1b
# ============================================================================
# 用户 2026-09-23 的裁决：**风格只决定方向与力度，不提供数值**。
#
# 为什么必须有这一层（实测证据）：同一个风格跑一批图，写出的 18 份侧车里，
#   Exposure2012 全是 +0.30、Saturation 全是 -8、SaturationAdjustmentAqua 全是 -52、
#   Temperature 有 13 张是 +5500……全部等于训练统计出来的中位数/均值。
# 也就是说模型把"统计出来的典型值"当成了"每张图的目标值"。后果正是用户观察到的两条：
#   · 样本少的风格（12 张、35 字段）容易偏色 —— 有些字段 n=1 或 n=2、std 极大；
#   · 样本多的风格（25 张、57 字段）容易过饱和 —— 面板一起往前推，没有总量约束。
# 所以训练阶段要把"力度"压成一个档位；提示词里只给**档位 + 方向**，
# 具体数值必须由模型按当前这张照片重新判断。
DIMENSION_LEVELS = 7
# 分档阈值（力度 0..1 → 1..7 档）。它是一把"尺子"，不是场景规则：
# 尺子的刻度按用户自己训练集里的实际分布校准（见 test_dimension_levels 的说明），
# 与"某类场景该怎么调"无关 —— 后者只允许来自用户的训练素材。
INTENSITY_THRESHOLDS: tuple[float, ...] = (0.03, 0.07, 0.12, 0.20, 0.30, 0.45)
INTENSITY_LABELS: tuple[str, ...] = ("极轻", "很轻", "轻", "中等", "中偏重", "重", "很重")

# 维度 → 参与的字段。字段清单直接取自 fields.py 的分组（唯一权威），
# 避免这里手抄一份名单后在改动时两处不一致。
DIMENSIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tone", "影调",
     ("Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
      "Whites2012", "Blacks2012") + F.FIELD_GROUPS["parametric"]),
    ("saturation", "饱和度与偏好", ("Vibrance", "Saturation")),
    ("hsl", "HSL 分区", F.FIELD_GROUPS["hsl"]),
    ("split", "分离色调", F.FIELD_GROUPS["split"]),
    ("colorgrade", "颜色分级", F.FIELD_GROUPS["colorgrade"]),
    ("calibration", "相机校准", F.FIELD_GROUPS["calibration"]),
    ("detail", "锐化与降噪", F.FIELD_GROUPS["detail"]),
    ("effects", "纹理/清晰度/去薄雾/光晕", ("Texture", "Clarity2012", "Dehaze", "Glow")),
    ("vignette", "晕影", ("PostCropVignetteAmount", "VignetteAmount")),
)


def _field_strength(name: str, statistic: float) -> float | None:
    """单个字段的"力度"（0..1）：|统计值| ÷ 该字段的面板半程。

    跳过两类字段：
      · **绝对色相**（面板 0..360，如 SplitToningShadowHue / ColorGrade*Hue）——
        它们表达的是"往哪种颜色偏"（方向），不是"偏多少"（力度）；
      · 非数值字段（枚举/字符串）与没有登记范围的字段。
    """
    spec = F.get_field(name)
    if spec is None or not spec.is_numeric:
        return None
    if spec.minimum is None or spec.maximum is None:
        return None
    if float(spec.minimum) == 0.0 and float(spec.maximum) == 360.0:
        return None
    half = max(abs(float(spec.minimum)), abs(float(spec.maximum)))
    if half <= 0:
        return None
    return min(1.0, abs(float(statistic)) / half)


def compute_dimension_levels(param_ranges: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把各维度的力度压成 1..7 档，并留下可审计的证据。

    做法：该维度下每个字段取统计中位数 → 按各自面板半程归一化到 0..1 →
    取这些字段力度的 **75 分位**作为该维度力度 → 查 DIMENSION_LEVELS 分档表。

    为什么用 75 分位而不是中位数/均值：
        统计表里出现的字段都是**用户真的动过**的字段，所以力度分布天然集中在低位
        （实测用户两套风格的各维度中位力度都在 0.10 上下，用中位数会把所有维度
        压成同一档 3~4，档位就失去了分辨力）。75 分位回答的是
        「这套风格在这个维度上，动得最狠的那几个滑块大概有多狠」，
        这正是"力度"要传达的意思；同时它又不像取最大值那样被单个离群字段带跑。
        中位数仍然写进证据里，供审计对比。

    输出同时带上证据（用了哪些字段、最多多少样本、力度多少、阈值是多少），
    这样用户能在风格文件里直接看到"这个 5/7 是怎么来的"，而不是信一个黑箱数字。
    """
    result: dict[str, dict[str, Any]] = {}
    for key, label, names in DIMENSIONS:
        strengths: list[float] = []
        used: list[str] = []
        samples = 0
        for name in names:
            stat = param_ranges.get(name)
            if not isinstance(stat, dict):
                continue
            try:
                center = float(stat.get("median"))
            except (TypeError, ValueError):
                continue
            value = _field_strength(name, center)
            if value is None:
                continue
            strengths.append(value)
            used.append(name)
            samples = max(samples, int(stat.get("count") or 0))
        if not strengths:
            continue
        strengths.sort()
        count = len(strengths)
        median_value = (
            strengths[count // 2]
            if count % 2
            else (strengths[count // 2 - 1] + strengths[count // 2]) / 2
        )
        upper = strengths[min(count - 1, int(round(0.75 * (count - 1))))]
        level = 1
        for bound in INTENSITY_THRESHOLDS:
            if upper >= bound:
                level += 1
        result[key] = {
            "label": label,
            "level": level,
            "level_text": f"{level}/{DIMENSION_LEVELS}",
            "intensity_label": INTENSITY_LABELS[level - 1],
            "strength": round(upper, 4),
            "median_strength": round(median_value, 4),
            "fields": used,
            "samples": samples,
            "evidence": (
                f"{len(used)} 个字段、最多 {samples} 个样本；"
                f"力度（75 分位）{upper:.3f}、中位数 {median_value:.3f}；"
                f"分档阈值 {list(INTENSITY_THRESHOLDS)}"
            ),
        }
    return result


def compute_local_param_ranges(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """从用户素材里算出**局部参数**的实测范围（面板值），写进风格文件。

    为什么必须由训练算：用户 2026-09-23 明确说他的风格文件只是测试用、改完他会重新训练。
    如果范围写死在代码里，那些数字就永远是我从某次测试样本里抄的；
    由训练算出来，重新训练一次就自动变成他当前的真实用法。
    输出格式：{字段名: {"min": .., "max": .., "count": .., "std": ..}}（面板值）
    `std` 是给写盘侧的“两层范围”用的：离散度越大，留的余量越大（见 masks.expand_range）。
    """
    from ..xmp import masks as masks_mod

    buckets: dict[str, list[float]] = {}
    for snapshot in snapshots:
        stats = snapshot.get("masks")
        local_values = getattr(stats, "local_values", None) or {}
        for name, stored_values in local_values.items():
            if name not in masks_mod.AI_LOCAL_RANGES:
                # 不在"AI 可提"清单里的字段（例如降噪、颗粒、机器字段）不参与统计，
                # 也不写进风格文件——否则以后放开清单时会混进不可控的口径。
                continue
            scale = masks_mod._LOCAL_SCALES.get(name, 0.01)
            for stored in stored_values:
                try:
                    value = float(stored) / scale if scale else float(stored)
                except (TypeError, ValueError):
                    continue
                buckets.setdefault(name, []).append(value)

    ranges: dict[str, dict[str, float]] = {}
    for name, values in buckets.items():
        if not values:
            continue
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        ranges[name] = {
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "count": len(values),
            "std": round(variance ** 0.5, 2),
        }
    return ranges


def describe_mask_usage(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """统计蒙版使用情况（用户裁决：忽略几何，统计使用频率写进 text_rules）。

    输出的自然语言会进入 text_rules 与 style_summary，
    保持硬约束 #15 的固定结构不变（不新增 param_ranges 里的蒙版字段）。
    """
    total_corrections = 0
    total_masks = 0
    total_retouch = 0
    manual = 0
    subject = 0
    obj = 0
    samples_with_masks = 0
    local_totals: dict[str, list[float]] = {}

    for snapshot in snapshots:
        stats = snapshot.get("masks")
        if stats is None:
            continue
        total_corrections += getattr(stats, "total_corrections", 0)
        total_masks += getattr(stats, "total_masks", 0)
        total_retouch += getattr(stats, "total_retouch_areas", 0)
        manual += getattr(stats, "manual_count", 0)
        subject += getattr(stats, "subject_count", 0)
        obj += getattr(stats, "object_count", 0)
        if getattr(stats, "used", False):
            samples_with_masks += 1
        for name, values in (getattr(stats, "local_values", {}) or {}).items():
            local_totals.setdefault(name, []).extend(values)

    sample_count = len(snapshots) or 1
    local_means = {
        name: round(mean(values), 3) for name, values in local_totals.items() if values
    }

    return {
        "samples_total": len(snapshots),
        "samples_with_masks": samples_with_masks,
        "mask_usage_ratio": round(samples_with_masks / sample_count, 3),
        "total_corrections": total_corrections,
        "corrections_per_sample": round(total_corrections / sample_count, 2),
        "total_masks": total_masks,
        "total_retouch_areas": total_retouch,
        "manual_count": manual,
        "subject_count": subject,
        "object_count": obj,
        "local_field_means": local_means,
    }


def mask_usage_to_text_rules(mask_summary: dict[str, Any]) -> list[str]:
    """把蒙版统计转成自然语言规则（用户裁决 #4 的落地点）。"""
    from ..constants import LOCAL_ADJUST_NOTABLE_THRESHOLD

    rules: list[str] = []
    ratio = mask_summary.get("mask_usage_ratio", 0.0)
    if ratio <= 0:
        return rules

    percent = round(ratio * 100)
    rules.append(f"该用户有 {percent}% 的照片使用了局部调整（平均每张 {mask_summary.get('corrections_per_sample')} 个蒙版）")

    subject = mask_summary.get("subject_count", 0)
    manual = mask_summary.get("manual_count", 0)
    obj = mask_summary.get("object_count", 0)
    kinds: list[str] = []
    if subject:
        kinds.append(f"{subject} 个 AI 主体蒙版（人物/面部/皮肤/头发）")
    if obj:
        kinds.append(f"{obj} 个 AI 对象选择蒙版")
    if manual:
        kinds.append(f"{manual} 个手动画笔或渐变蒙版")
    if kinds:
        rules.append("局部调整类型分布：" + "、".join(kinds))

    if subject and subject >= manual:
        rules.append(
            "局部调整以「人像主体」为主：批量处理人像时，"
            "可考虑对人物面部/皮肤做局部提亮与柔化，而不是只调全局参数"
        )

    local_means = mask_summary.get("local_field_means") or {}
    notable = {
        name: value
        for name, value in local_means.items()
        if abs(value) >= LOCAL_ADJUST_NOTABLE_THRESHOLD
    }
    if notable:
        parts = [f"{name} 均值 {value:+.1f}" for name, value in sorted(notable.items())]
        rules.append("局部调整的显著倾向（绝对值超过 ±10 才列出）：" + "、".join(parts))

    if mask_summary.get("total_retouch_areas", 0) > 0:
        rules.append(
            f"使用过污点修复（共 {mask_summary['total_retouch_areas']} 处），"
            "批量处理时可考虑保留该习惯，但本工具的 AI 不产出修复区域"
        )

    return rules


# ============================================================================
# 场景规则与白平衡偏移（实施步骤 1c）
# ============================================================================
# 【用户 2026-09-23 的硬约束】scene_rules **只能来自训练素材**：
# 不接受"长曝水面要锐化"这种由我举的例子/凭经验写的规则。
# 所以这里不写任何场景知识，只做一件事：
#   把训练样本按**模型看图为它们打的场景标签**分组，然后用已有的差分机制
#   （已排除相机/镜头默认值）统计"这个用户在同类场景下习惯怎么调"，
#   并用样本数 + 中位数 + 出现张数作为证据写进风格文件。
# 样本太少的场景（默认 <3 张）**不出规则** —— 宁可不说，也不要拿单张样本编规律。
SCENE_RULE_MIN_SAMPLES = 3
SCENE_RULE_MAX_FIELDS = 8
# 场景规则里**不列**绝对色温：它是每张现场光的绝对值（实测不同照片的 as-shot 在 4743–4977K
# 之间），把它写成"这个场景习惯 5800K"只会让模型在批量里照抄这个开尔文数。
# 白平衡的习惯另用 compute_wb_offset() 统计"相对 as-shot 的偏移"，那才是有意义的量。
SCENE_RULE_EXCLUDED_FIELDS: frozenset[str] = frozenset({"Temperature"})


def compute_scene_rules(by_scene: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按场景统计"同类场景下用户习惯怎么调"，附带样本数证据。

    参数 by_scene：场景名 → 该场景下每个样本**差分后**的参数（已剔除默认值）。
    只保留在**至少一半样本**里都出现的字段：只出现一次的多半是单张的偶然处理，
    把它写成"规则"会让模型在批量里无条件套用。
    """
    rules: list[dict[str, Any]] = []
    for scene, adjusted_list in sorted(by_scene.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(adjusted_list) < SCENE_RULE_MIN_SAMPLES:
            continue
        ranges = compute_param_ranges(adjusted_list)
        min_count = max(2, len(adjusted_list) // 2)
        entries = {
            name: stat
            for name, stat in ranges.items()
            if stat.get("count", 0) >= min_count and name not in SCENE_RULE_EXCLUDED_FIELDS
        }
        if not entries:
            continue

        def weight(item: tuple[str, dict[str, Any]]) -> float:
            field, stat = item
            spec = F.get_field(field)
            if spec is None or spec.minimum is None or spec.maximum is None:
                return 0.0
            half = max(abs(float(spec.minimum)), abs(float(spec.maximum)))
            return abs(float(stat.get("median", 0.0))) / half if half else 0.0

        chosen = sorted(entries.items(), key=weight, reverse=True)[:SCENE_RULE_MAX_FIELDS]
        parts = [
            f"{field} 中位 {stat['median']:+.2f}（{stat['count']}/{len(adjusted_list)} 张，"
            f"范围 {stat['min']:+.2f}~{stat['max']:+.2f}）"
            for field, stat in chosen
        ]
        rules.append(
            {
                "scene": scene,
                "samples": len(adjusted_list),
                "fields": {field: stat for field, stat in chosen},
                "text": f"【{scene}】（{len(adjusted_list)} 张训练样本）习惯：" + "；".join(parts),
                "evidence": (
                    f"来自你训练素材里 {len(adjusted_list)} 张标为「{scene}」的照片；"
                    f"只列了在至少一半样本里出现过的字段（中位数与出现张数已写在上面）"
                ),
            }
        )
    return rules


def compute_wb_offset(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """白平衡习惯：只统计**相对 as-shot 的偏移**。

    为什么不能用绝对色温：crs:Temperature 是绝对开尔文，而每张照片现场色温本来就不同。
    实测三张样本的 as-shot 色温是 4743 / 4955 / 4977K，而侧车里写的是 5500 / 5600 / 5050 ——
    把它们平均起来得不到任何"习惯"；用户真正稳定的是"相对 as-shot 偏暖多少 K"：
        IMG_2509：5500 - 4743 = **+757K**（偏暖）
        _G4A1392：5600 - 4955 = **+645K**（偏暖）
    另外只统计 WhiteBalance="Custom" 的样本（As Shot 时 ACR 忽略这两个数值，
    把它们计入统计会把"没生效的记录值"当成偏好）。
    """
    offsets: list[float] = []
    tints: list[float] = []
    for snapshot in snapshots:
        if not snapshot.get("wb_effective"):
            continue
        params = snapshot.get("params") or {}
        as_shot = snapshot.get("as_shot_cct")
        try:
            if as_shot and params.get("Temperature") is not None:
                offsets.append(float(params["Temperature"]) - float(as_shot))
            if params.get("Tint") is not None:
                tints.append(float(params["Tint"]))
        except (TypeError, ValueError):
            continue
    if not offsets and not tints:
        return None

    result: dict[str, Any] = {
        "count": len(offsets),
        "kelvin": (
            {
                "mean": round(mean(offsets), 1),
                "median": round(median(offsets), 1),
                "min": round(min(offsets), 1),
                "max": round(max(offsets), 1),
                "std": round(pstdev(offsets), 1) if len(offsets) > 1 else 0.0,
            }
            if offsets
            else None
        ),
        "tint": (
            {
                "mean": round(mean(tints), 1),
                "median": round(median(tints), 1),
                "min": round(min(tints), 1),
                "max": round(max(tints), 1),
            }
            if tints
            else None
        ),
    }
    pieces: list[str] = []
    if offsets:
        kelvin = result["kelvin"]
        direction = "偏暖" if kelvin["median"] > 60 else ("偏冷" if kelvin["median"] < -60 else "基本不动")
        pieces.append(
            f"相对 as-shot 色温中位 {kelvin['median']:+.0f}K（{direction}，"
            f"范围 {kelvin['min']:+.0f}~{kelvin['max']:+.0f}K，{len(offsets)} 张）"
        )
        result["direction"] = direction
    if tints:
        tint_stat = result["tint"]
        pieces.append(f"色调 Tint 中位 {tint_stat['median']:+.0f}（{len(tints)} 张）")
    result["text"] = "；".join(pieces) if pieces else "样本里没有可用的白平衡数据"
    return result


# ============================================================================
# 组装与持久化
# ============================================================================

def build_style_profile(
    *,
    name: str,
    snapshots: list[dict[str, Any]],
    model_output: dict[str, Any],
) -> dict[str, Any]:
    """组装完整的 style_profile.json（硬约束 #15 的固定结构）。"""
    per_sample_adjusted: list[dict[str, Any]] = []
    # 场景名 → 该场景每个样本差分后的参数（用于 scene_rules）
    by_scene: dict[str, list[dict[str, Any]]] = {}
    # 每个字段被排除的样本数（用于判断"是默认值"还是"偶尔是默认值"）
    exclusion_counts: dict[str, int] = {}
    exclusion_reason: dict[str, dict[str, str]] = {}

    for snapshot in snapshots:
        adjusted, excluded = compute_adjustments(snapshot)
        per_sample_adjusted.append(adjusted)
        scene = str(snapshot.get("scene_label") or "").strip()
        if scene:
            by_scene.setdefault(scene, []).append(adjusted)
        for entry in excluded:
            field_name = entry["field"]
            exclusion_counts[field_name] = exclusion_counts.get(field_name, 0) + 1
            # 同一字段在不同样本里可能因不同原因被排除（例如 Temperature：
            # 在 "As Shot" 的片子里因 wb_not_effective 排除，
            # 在 "Custom" 的片子里却根本不会进排除列表）。
            # 保留**首次**出现的原因作为代表，用于向用户解释；
            # 真正决定是否计入 ignore_fields 的是 exclusion_counts。
            exclusion_reason.setdefault(field_name, entry)

    total_samples = len(snapshots)

    # 批内不变项剔除：这些字段在整批中取值完全一致，按定义在全部样本里都算默认值。
    per_sample_adjusted, invariant_exclusions = apply_batch_invariance(per_sample_adjusted)
    for entry in invariant_exclusions:
        field_name = entry["field"]
        exclusion_counts[field_name] = total_samples
        exclusion_reason.setdefault(field_name, entry)

    param_ranges = compute_param_ranges(per_sample_adjusted)
    mask_summary = describe_mask_usage(snapshots)

    # --- 关键：只有"在全部样本中都被排除"的字段才算默认值 -------------------
    # 若某个字段在 10 个样本里有 8 个是默认、2 个被用户明确调过，
    # 把它写进 ignore_fields 会导致提示词里出现"请不要据此推断"的指令，
    # 从而让 AI 忽略用户的真实偏好——这比"不排除"严重得多。
    # 因此这里用最保守的判定，并把部分排除的字段单独记到
    # partially_default_fields（供人查看，不参与提示词）。
    fully_default: list[dict[str, Any]] = []
    partially_default: list[dict[str, Any]] = []

    threshold = total_samples * FULLY_DEFAULT_RATIO
    for field_name, count in exclusion_counts.items():
        # 显式标注 dict[str, Any]：默认值全是字符串，否则类型推断会把 entry
        # 定成 dict[str, str]，后面写入 int 就会被静态检查判为类型错误。
        entry: dict[str, Any] = dict(
            exclusion_reason.get(field_name, {"field": field_name, "reason": "?", "detail": ""})
        )
        entry["excluded_samples"] = count
        entry["total_samples"] = total_samples
        if count >= threshold:
            fully_default.append(entry)
        else:
            partially_default.append(entry)

    # 模型给出的自然语言规则 + 本地算出的蒙版规则。
    text_rules: list[str] = []
    raw_rules = model_output.get("text_rules") or []
    if isinstance(raw_rules, list):
        text_rules.extend(str(rule).strip() for rule in raw_rules if str(rule).strip())
    text_rules.extend(mask_usage_to_text_rules(mask_summary))

    # 排序：先按原因分组，组内按字段名，便于用户阅读与人工核对。
    reason_order = {
        "crd_equals_crs": 0,
        "acr_factory_default": 1,
        "batch_invariant": 2,
        "lens_default": 3,
        "wb_not_effective": 4,
        "machine_field": 5,
    }

    def _sort_key(entry: dict[str, Any]):
        return (reason_order.get(str(entry.get("reason", "")), 99), str(entry.get("field", "")))

    ignore_fields = sorted(fully_default, key=_sort_key)
    partially_default_fields = sorted(partially_default, key=_sort_key)

    profile: dict[str, Any] = {
        # --- 硬约束 #15 要求的最小字段集 ---
        "name": name,
        "version": {
            "structure": PROFILE_STRUCTURE_VERSION,
            "generator": __version__,
        },
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sample_count": len(snapshots),
        "param_ranges": param_ranges,
        "text_rules": text_rules,
        "ignore_fields": ignore_fields,
        # --- 扩展信息 ---
        # partially_default_fields：在**部分**样本中被判为默认、但在另一些样本里
        # 确实被用户调整过的字段。它们**不会**进提示词的"请勿据此推断"清单
        # （否则等于让 AI 忽略用户的真实偏好），仅作为可审计信息保留。
        "partially_default_fields": partially_default_fields,
        "style_summary": str(model_output.get("style_summary") or ""),
        "saturation_tendency": str(model_output.get("saturation_tendency") or ""),
        "contrast_tendency": str(model_output.get("contrast_tendency") or ""),
        "shadow_color_cast": str(model_output.get("shadow_color_cast") or ""),
        "hsl_habits": str(model_output.get("hsl_habits") or ""),
        "lens_correction_preference": str(model_output.get("lens_correction_preference") or ""),
        "excluded_reasoning": str(model_output.get("excluded_reasoning") or ""),
        "mask_usage": mask_summary,
        # 各维度的力度档位（1..7）。提示词只发档位与方向，不发"照抄用"的数值，
        # 理由见本文件里 compute_dimension_levels 的说明（用户 2026-09-23 的裁决）。
        "dimension_levels": compute_dimension_levels(param_ranges),
        # 场景规则：**只来自你的训练素材**（按模型打的场景标签分组统计），
        # 每条都带样本数证据；样本太少的场景不出规则。
        "scene_rules": compute_scene_rules(by_scene),
        # 局部参数的**实测范围**（面板值）：写盘前用它复核 AI 提的局部参数，
        # 这样"范围来自用户素材"是机制保证的，不靠代码里写死常量。
        "local_param_ranges": compute_local_param_ranges(snapshots),
        # 白平衡习惯：相对 as-shot 的偏移（绝对值平均没有意义，见函数说明）。
        "wb_offset": compute_wb_offset(snapshots),
    }
    return profile


_profile_lock = threading.RLock()


def save_style_profile(profile: dict[str, Any], *, overwrite: bool = True) -> Path:
    """把风格档案写入 styles/ 目录。

    目标目录是**运行时数据目录**（%APPDATA%/AutoColorDiffusion/styles 或便携模式下的
    <exe目录>/data/styles），符合打包要求 #3：运行时数据不得写入 _internal。
    """
    name = str(profile.get("name") or "unnamed_style")
    target = styles_dir() / f"{sanitize_style_name(name)}.json"
    with _profile_lock:
        styles_dir().mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise FileExistsError(f"风格文件已存在：{target}")
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    log.info("风格档案已保存：%s（%d 个样本）", target, profile.get("sample_count", 0))
    return target


def list_styles() -> list[str]:
    """列出可用风格名（界面下拉用），按名称排序。"""
    directory = styles_dir()
    if not directory.is_dir():
        return []
    names: list[str] = []
    for item in sorted(directory.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
            name = str(data.get("name") or item.stem)
        except (OSError, ValueError):
            name = item.stem
        names.append(name)
    return names


def load_style_profile(name: str) -> dict[str, Any] | None:
    """按名称装载风格档案。找不到返回 None。

    ⚠ 内置风格的**打包内副本**也要能装载，即使它还没被 ensure_seed_styles()
    释放到用户目录。实测踩到：CLI 用 `--style AI自主决策` 时，用户目录里
    还没有这个文件（种子是**在这次运行的后面某一步**才释放的），于是打印
    「找不到风格「AI自主决策」，将按未选择风格处理」——用户会以为这个内置风格不存在。
    所以这里退一步读打包内那份（**只读，不写盘**）。
    """
    target = styles_dir() / f"{sanitize_style_name(name)}.json"
    if not target.is_file():
        bundled = resource_root() / "styles" / f"{sanitize_style_name(name)}.json"
        if bundled.is_file():
            try:
                data = json.loads(bundled.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    log.info("风格「%s」在用户目录里还没有，先用打包内的副本（%s）。", name, bundled)
                    return data
            except (OSError, ValueError) as exc:
                log.error("打包内的风格文件无法读取（%s）：%s", bundled, exc)
        # 容错：用户可能手改过文件名，退化为遍历匹配 name 字段。
        for item in styles_dir().glob("*.json") if styles_dir().is_dir() else []:
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(data.get("name")) == name:
                return data
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("风格文件损坏，无法装载 %s：%s", target, exc)
        return None
    return data if isinstance(data, dict) else None


def style_file_for(name: str) -> Path | None:
    """按名称找到风格文件的实际路径。找不到返回 None。

    存在的意义：界面的"删除风格"必须知道**到底该删哪个文件**。
    名字与文件名不一定一致（用户可能手改过文件名，或名字里有非法字符被
    sanitize 过），所以先按 sanitize 后的文件名找，找不到再遍历匹配 name 字段。
    """
    direct = styles_dir() / f"{sanitize_style_name(name)}.json"
    if direct.is_file():
        return direct
    directory = styles_dir()
    if not directory.is_dir():
        return None
    for item in directory.glob("*.json"):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(data.get("name")) == name:
            return item
    return None


def is_autopilot_style(style: dict[str, Any] | None) -> bool:
    """这份风格是不是内置的「AI自主决策」。

    判定只看 name 字段（不猜文件路径）：提示词与界面都要用它做分支，
    而调用方手上通常只有已经装载好的 dict。
    """
    if not isinstance(style, dict):
        return False
    return str(style.get("name") or "").strip() == AUTOPILOT_STYLE_NAME


def is_autopilot_style_name(name: str | None) -> bool:
    """按风格名判断（界面里拿到的是名字，不是 dict）。"""
    return str(name or "").strip() == AUTOPILOT_STYLE_NAME


def is_seed_style(path: Path) -> bool:
    """是否内置种子风格。

    种子会在每次启动时被 ensure_seed_styles() 补齐，所以"删掉它"是徒劳的 ——
    界面据此**拒绝删除并说明原因**，而不是让用户白删一次、下次启动它又出现。
    """
    try:
        return (resource_root() / "styles" / path.name).is_file()
    except OSError:
        return False


def ensure_seed_styles() -> list[Path]:
    """首次运行时把打包内的 styles 种子释放到运行时目录。

    必须这样做（打包要求 #3）：打包内的 styles/ 位于 _internal（只读），
    用户新训练的风格必须写到可写目录，否则换个用户/无写权限时会失败。
    """
    bundled = resource_root() / "styles"
    if not bundled.is_dir():
        return []
    copied = copy_tree_missing(bundled, styles_dir())
    if copied:
        log.info("已释放内置风格种子 %d 个到 %s", len(copied), styles_dir())
    return copied
