# -*- coding: utf-8 -*-
"""提示词模板（硬约束 #4 的"json_schema=false 时的降级" + #19 的 caption 通道）。

三类提示词：
    1. 输出模式：看图 → 产出 ACR 参数。
    2. 训练模式：看（图 + 已转成 JSON 的 XMP）→ 归纳用户偏好。
    3. caption 降级：模型无视觉能力时，用本地统计量生成的文字描述替代图片。

最关键的一段话（必须出现在每个输出模式提示词里）
------------------------------------------------
"AI 看的是 sRGB 缩略图，XMP 参数作用于原始 RAW，二者不可混用"
（硬约束 #2 规定的因果链）。这不是套话：嵌入式预览是**相机 JPEG 配方**的产物，
AI 看到的是机内已经加工过的色彩，而它输出的参数会作用在 ACR 对 RAW 的
**中性渲染**上。若不告知模型这一点，它会系统性地把机内风格当成"照片本身"，
从而做出错误判断（例如富士胶片模拟让画面偏青，模型会以为需要加暖）。
"""

from __future__ import annotations

import re
from typing import Any

from ..constants import ABSOLUTE_KELVIN_PATTERN
from ..constants import AUTO_MATCH_INSTRUCTION_TEMPLATE, AUTOPILOT_STYLE_NAME
from ..xmp import fields as F
from ..xmp import masks as masks_mod
from ..xmp.masks import STYLE_MASK_USAGE_MIN_RATIO as MASK_USAGE_MIN_RATIO
from ..xmp.validator import CRS_PREFIX

# 训练产出里那些"人话特征块" → 提示词里的标题。
# 顺序有意义：HSL 习惯是这个用户最鲜明的特征，排前面。
_STYLE_CHARACTER_BLOCKS: tuple[tuple[str, str], ...] = (
    ("hsl_habits", "分区色彩习惯（HSL / 相机校准）"),
    ("saturation_tendency", "饱和度倾向"),
    ("contrast_tendency", "对比度倾向"),
    ("shadow_color_cast", "阴影色彩倾向"),
    ("lens_correction_preference", "镜头校正与暗角习惯"),
)

# 绝对色温**不给模型看统计**（提示词里的"历史偏好统计"表会跳过它）。
# 理由（2026-09-26 实测）：Temperature 是绝对开尔文，它的 mean/median/min/max
# 是"训练素材里那些照片各自的现场色温"，对当前这张图没有任何含义；
# 而它恰好是最容易被当成目标值照抄的一个数 —— DSC03646.ARW 机内 as-shot 4600K，
# 模型照风格文本里的"色温控制在 5300–6350K"写出 6100K，人像肤色整片偏黄。
# 白平衡只剩两条证据通道：wb_offset（相对 as-shot 的偏移习惯）＋ 逐图 as-shot 实测。
ABSOLUTE_WB_FIELDS: frozenset[str] = frozenset({"Temperature"})

# 风格文本里的**绝对色温**写法（训练素材的历史取值记录）。
# 两条正则：先匹配带范围符号的（5300～6350K），再匹配单个的（5300K）。
_ABS_KELVIN_RANGE_RE = re.compile(r"\d{4,5}\s*[~～\-–—到至]\s*\d{4,5}\s*[KＫ]")
_ABS_KELVIN_RE = re.compile(ABSOLUTE_KELVIN_PATTERN)
# 改写用**一趟**匹配（区间优先）。为什么不能先替区间、再拿单值那条扫一遍：
# 替换文本自己也会被第二条规则命中，结果是嵌套的
# 「相对 as-shot 偏冷（最冷时往回收 相对 as-shot 偏冷（…））」—— 实测踩过。
_ABS_KELVIN_ANY_RE = re.compile(rf"{_ABS_KELVIN_RANGE_RE.pattern}|{_ABS_KELVIN_RE.pattern}")


# 方向词：改写绝对色温时**优先沿用原句自己说过的话**。
# 为什么必须有这一条（实测踩到）：`默认风格-qwen3-vl训练.json` 里写着
# 「白平衡多设为 5850K（**偏暖**日光）」，而按"该数字与风格绝对中位比大小"
# 推出来的方向是"偏冷" —— 于是改写结果与原句意思相反，
# 等于把一份别人训好的风格改成另一个意思。**原句的自述永远优先于统计推断。**
_WARM_WORDS = ("偏暖", "更暖", "加暖", "提暖", "暖一些", "暖一点", "暖调", "暖色")
_COOL_WORDS = ("偏冷", "更冷", "压冷", "往冷", "冷一些", "冷一点", "冷调", "冷色")


def _direction_from_text(window: str, anchor: int) -> str | None:
    """在被替换数字**附近**找方向词，取离它最近的那个。

    只看窗口（±24 字）而不是整段：一条规则里可能同时写"雪景偏冷、日落偏暖"，
    按整段找会张冠李戴。
    """
    candidates: list[tuple[int, str]] = []
    for word in _WARM_WORDS:
        start = window.find(word)
        while start >= 0:
            candidates.append((abs(start + len(word) / 2 - anchor), "偏暖"))
            start = window.find(word, start + 1)
    for word in _COOL_WORDS:
        start = window.find(word)
        while start >= 0:
            candidates.append((abs(start + len(word) / 2 - anchor), "偏冷"))
            start = window.find(word, start + 1)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _relative_wb_phrase(
    style: dict[str, Any] | None, matched: str = "", window: str = "", anchor: float = 0.0
) -> str:
    """把"绝对色温"改写成它真正对应的量：**相对 as-shot 的偏移方向**。

    方向来源有三档，按优先级：
      1. **原句自己写的方向词**（`偏暖日光` / `压冷` …）—— 最可靠，绝不与原意相反；
      2. 该数字与风格自己的绝对中位数比大小（`param_ranges["Temperature"]["median"]`）；
      3. 风格自己的相对偏移中位数（`wb_offset`）。
    具体偏移量只在风格真的统计过 `wb_offset` 时才附上（否则那句话是凭空捏的）。
    """
    direction = _direction_from_text(window, anchor)

    numbers = [float(n) for n in re.findall(r"\d{4,5}", matched or "")]
    target = sum(numbers) / len(numbers) if numbers else None

    base_median = None
    if isinstance(style, dict):
        stat = (style.get("param_ranges") or {}).get("Temperature")
        if isinstance(stat, dict) and isinstance(stat.get("median"), (int, float)):
            base_median = float(stat["median"])

    wb = (style or {}).get("wb_offset")
    kelvin = (wb or {}).get("kelvin") if isinstance(wb, dict) else None
    offset_median = None
    if isinstance(kelvin, dict) and isinstance(kelvin.get("median"), (int, float)):
        offset_median = float(kelvin["median"])

    if direction is None:
        if target is not None and base_median is not None:
            direction = "偏冷" if target < base_median else "偏暖"
        elif offset_median is not None:
            direction = "偏暖" if offset_median > 60 else ("偏冷" if offset_median < -60 else "基本不动")
        else:
            return "相对这张图自己的 as-shot 判断"

    if direction == "基本不动":
        return "相对 as-shot 基本不动"
    if direction == "偏暖" and offset_median is not None:
        return f"相对 as-shot 偏暖（这套风格中位 {offset_median:+.0f}K）"
    # ⚠ 这里**不能**写成"往回收 1202K"之类：四位数字+K 又会命中我们自己的规则，
    # 把改写结果再改一次（这就是下面"一趟完成"的由来）。
    return f"相对 as-shot {direction}"


def scrub_absolute_kelvin(text: str, style: dict[str, Any] | None) -> str:
    """把风格文本里的绝对色温改写成"相对 as-shot"的说法。

    【为什么必须在渲染时做，而不是只在训练时禁止】
    用户现有的风格档案里就写着「色温控制在 5300–6350K」（2026-09-26 实测
    `常规风格-DSF训练.json`），而模型看到绝对区间就会把它当目标值 ——
    实测 `DSC03646.ARW` 机内 4600K 被写成 6100K（+1500K），人像肤色整片偏黄。
    让用户"重新训练一次才能修好"是不够的：**手上已存在的风格必须当场修好**，
    所以训练侧（不再产出绝对区间）与渲染侧（出现就改写）两层都要做。

    这一层是**通用**的：它只认"四/五位数字 + K"，与风格是哪台机器、哪个模型、
    什么时候训的都无关 —— 别人的老风格同样当场生效，不必重训。
    """
    if not text:
        return text

    def _fix(match: re.Match[str]) -> str:
        start, end = match.span()
        window = text[max(0, start - 24): min(len(text), end + 24)]
        return _relative_wb_phrase(style, match.group(0), window, (start + end) / 2 - max(0, start - 24))

    return _ABS_KELVIN_ANY_RE.sub(_fix, text)


def render_field_reference() -> str:
    """生成字段参考表（按 ACR 面板分组）。

    字段名、类型、取值范围全部来自 fields.py —— 唯一权威来源。
    刻意把"面板位置"也写进去：模型对 Exposure/Clarity/Dehaze 这些英文名的
    理解主要来自它们在各家修图软件里的位置语义，面板名能把语义锚定住。
    """
    lines: list[str] = []
    for group, names in F.FIELD_GROUPS.items():
        # 只列出 AI 能写的字段。若某个分组下全是"只保留不生成"的机器字段
        # （例如 machine 分组），就整组跳过——否则提示词里会出现一个
        # 只有标题没有内容的空分组，既浪费 token 又会让模型困惑。
        writable = [
            name
            for name in names
            if (spec := F.get_field(name)) is not None and spec.ai_writable
        ]
        if not writable:
            continue

        label = F.GROUP_LABELS.get(group, group)
        lines.append(f"\n【{label}】")
        for name in writable:
            spec = F.get_field(name)
            if spec is None:
                continue
            desc = f"  - {CRS_PREFIX}{name}（{spec.kind}，{spec.range_text()}）"
            if spec.description:
                desc += f"：{spec.description}"
            lines.append(desc)
    return "\n".join(lines)


def render_machine_field_warning() -> str:
    """列出 AI 禁止产出的字段。

    必须显式告诉模型"不要碰这些"，否则模型经常会自作聪明地返回
    crs:CameraProfile 或 crs:ProcessVersion（因为它在训练数据里见过）。
    虽然本地校验会拦住，但每次拦截都意味着一次重试，是纯浪费。
    """
    names = sorted(F.PRESERVE_ONLY_FIELDS)
    names = [n for n in names if (spec := F.get_field(n)) is None or spec.group != "mask"]
    return (
        "以下字段属于机器/版本/相机配置文件相关，**绝对不要返回**"
        "（返回它们会导致跨机器渲染不一致，本地校验会直接拒收并重试）：\n  "
        + "、".join(CRS_PREFIX + n for n in names)
        + "\n（蒙版/局部调整的那一组 **crs 字段**（Mask… 系列）也一律不要返回 —— 那组字段由程序写入；"
        "但**响应结构里的 `masks` 数组不受此限制**：风格约束里说明该风格会用局部调整时，"
        "请按那里的写法与示例把蒙版放进 `masks`，不要写 Mask 相关的 crs 字段名。）"
    )


# ============================================================================
# 输出模式
# ============================================================================

OUTPUT_SYSTEM_ROLE = """你是一位资深的 Adobe Camera Raw 调色师，负责为一批照片批量生成 ACR 调整参数。

你的输出会被程序直接写入每张 RAW 的 XMP 旁侧文件，供 Camera Raw / Lightroom 读取。
因此你的输出必须是严格的 JSON，且只能使用 ACR 的真实字段名。"""

OUTPUT_COLOR_CHAIN_WARNING = """【最关键的前提，请务必理解】
你看到的是**相机内嵌 JPEG 预览**（已做 ICC 色彩管理转换到 sRGB，长边 1024）。
而你输出的参数会作用在**原始 RAW 数据经 ACR 渲染的中性结果**上。二者不可混用：
  - 嵌入式预览带有相机机内风格（如佳能 Picture Style、富士胶片模拟），
    它反映的是"相机当时的加工结果"，不是 RAW 的原始状态；
  - 所以不要把预览里呈现的色彩倾向直接当成"需要修正的偏差"。
    例如：预览明显偏青，可能是相机设了"风光"风格，而非白平衡错误；
    此时应当谨慎给出小幅修正，而不是大幅加暖。
  - 若某张图的预览来源被标注为 thumbnail（极小缩略图），
    说明细节不可信，请只做曝光/白平衡层面的保守判断，
    不要给出针对噪点、锐化、局部细节的参数。"""

OUTPUT_OUTPUT_RULES = """【输出的第一原则：先这张照片，再这套风格】
0. **风格只提供方向，永远排在"这张照片本身需要什么"之后**（用户 2026-09-26 的原话：
   风格方向不许影响照片本身的色彩 —— 「本来就黄，方向让调黄，结果更黄」）。
   逐张按下面的顺序判断，顺序不能颠倒：
   ① 先看这张照片自己：曝光是否合理、白平衡是否偏离现场光、主体与肤色颜色是否正确；
   ② 再问一句"风格方向在这张图上成不成立"：**只有当照片没有朝同一方向偏离时**才采用风格方向；
   ③ 照片已经朝那个方向偏了 → **不要采用该方向**（宁可不动），甚至略往回拉。
   举例：这张图整体已经偏暖/偏黄，风格习惯又是"偏暖"→ 这时不要给偏暖的色温或暖色分级，
   最多保持不动；反过来，画面偏冷时才可以加暖。
   ⚠ 判断依据是你看到的画面 + 每张图实测指标（含「观感已经比 as-shot 更暖」这类提示），
   不是风格里的数字。

【输出规则】
1. 只返回**需要改动**的字段。保持默认的字段请直接省略，不要填 0 或默认值。
   若某张图不需要任何调整，params 返回空对象 {}。
2. 字段名必须使用 crs: 前缀的 ACR 字段名，例如 "crs:Exposure2012"。
3. 数值必须落在给定范围内。整数型字段给整数，浮点型字段给浮点数。
4. 白平衡（这条最容易把整张照片做坏，务必看清）：
   "crs:Temperature" 是**目标绝对色温（K）**，"crs:Tint" 是目标色调值，
   它们的基准是**每张图自己的 as-shot**——见每张图实测指标里的「机身 as-shot」那一项。
   先看这张图的起点是多少 K，再决定要不要动、动多少。
   ⚠ 风格约束里若出现「色温控制在 XXXX～XXXX K」这类**绝对区间**，
   那是训练素材里的**历史取值记录**（每张照片现场光不同），**不是**每张图的目标值。
   允许偏离多少，看风格约束里的「白平衡习惯（相对 as-shot）」那一行。
   ⚠ **偏暖的偏移是最容易出错的方向**：风格统计里的"偏暖 +N K"是他有时会做的事，
   不是每张都该做。拿不准就给 0（不动白平衡）；指标行里出现
   「观感已经比 as-shot 更暖」时，**不许**再给偏暖的 Temperature。
   程序会按这套风格的白平衡习惯把偏离 as-shot 太多的取值夹回并记一条说明；
   读不到 as-shot 的图，整组白平衡都不会被写出去（宁可不动，也不猜绝对值）。
   你不需要也不应该返回 WhiteBalance（程序会自动置为 Custom 使它们生效）。
5. 点曲线（curves）：坐标用 0..1 归一化浮点，至少两个点、最多 16 个点，
   x 必须严格递增。只有在确实需要 S 型影调或单通道调色时才使用；
   能用 Basic 面板参数解决的问题就不要动曲线（曲线的批次一致性更难保证）。
6. 人像场景优先使用 crs:Vibrance 而不是 crs:Saturation，
   因为 Saturation 会让肤色过饱和。
   画面里有人像/肤色时，"把橙色相推向红"（HueAdjustmentOrange 取负）与
   "提高橙/红明度"（LuminanceAdjustmentOrange/Red 取正）这两件事叠在一起
   会把肤色推离肤色指示线（用户实测反馈的原话是「根本不符合肤色指示线」）：
   有肤色时不要给 HueAdjustmentOrange 负值，橙/红明度也要克制。
7. 批量一致性：同一批照片应当追求**视觉观感统一**。
   先确定批内基准，再让每张向基准靠拢；不要每张都按"单张最佳"来调，
   那样会让整批看起来杂乱。
   但**统一的是观感，不是数值**：每张照片的亮度、色温、裁切情况不同，
   允许（也应该）取不同的数值；如果发现自己给每张图都写了同一组数值，
   请停下来按每张图自己的实测指标重新判断。
8. 局部调整（每一项回复里的 `masks` 数组，0–3 个）：若风格约束里说明
   「这位用户平时会用局部调整」，请按那里的写法**真的**给出蒙版，
   不要一律返回空数组；确实不需要时才返回 []。
   坐标用显示帧（你看到的方向，天空在上方）；局部参数写在 local 对象里。"""


def build_output_system_prompt() -> str:
    """组装输出模式的 system 提示词。"""
    return "\n\n".join(
        [
            OUTPUT_SYSTEM_ROLE,
            OUTPUT_COLOR_CHAIN_WARNING,
            OUTPUT_OUTPUT_RULES,
            "【可用字段参考（请严格使用下列字段名与范围）】" + render_field_reference(),
            render_machine_field_warning(),
        ]
    )


def _style_panels(ranges: dict[str, Any]) -> list[str]:
    """从 param_ranges 反推"这份风格实际用了哪些 ACR 面板"。

    为什么由数据推导而不是写死一份名单：用户手写的风格文件可能只有基本面板，
    而训练出来的可能用上 HSL / 颜色分级。写死名单会在其中一边说假话。
    """
    panels: list[str] = []
    for group, names in F.FIELD_GROUPS.items():
        if any(name in ranges for name in names):
            panels.append(F.GROUP_LABELS.get(group, group))
    return panels


def render_style_block(style: dict[str, Any] | None, style_name: str | None) -> str:
    """把风格文件渲染成提示词里的风格约束块。

    风格文件既可能来自训练模式（含 text_rules 与 param_ranges），
    也可能是用户手写的。因此对结构做宽容处理：有什么就用什么。

    【2026-09 修一个真缺陷：算出来了却不发】
    训练阶段会算出 `hsl_habits` / `saturation_tendency` / `contrast_tendency` /
    `shadow_color_cast` / `lens_correction_preference` / `mask_usage`，
    但旧版本**从没把它们送进输出模式的提示词**（只送了 param_ranges 的数字表）。
    后果：用户训练完跑输出，只得到基本面板上几个数（用户原话「只调整了亮和颜色」），
    而他风格里最鲜明的习惯（“全局压绿”“冷阴影+暖高光”）压根没被提到。
    """
    if not style:
        return "【风格约束】未指定风格。请按「保守、自然、不引入个人偏好」的原则处理。"

    if str(style.get("name") or "").strip() == AUTOPILOT_STYLE_NAME:
        # 自主决策风格：**没有**偏好规则可遵守，所以绝不能走下面那套
        # "风格规则（请逐条遵守）"的渲染 —— 那会让模型去找一份并不存在的规则，
        # 并且把"逐条遵守"这个要求挂空。
        #
        # 文案在这里而不是风格文件里：文件只负责"出现在下拉里 + 不可删除 + 承载名字"，
        # 提示词只有这一处真值来源（改文案不会出现两处不一致）。
        return "\n".join(
            [
                f"【风格约束：{AUTOPILOT_STYLE_NAME}】",
                "本次没有需要遵守的个人风格偏好：请按照片本身决定每张的调整方向与幅度。",
                "可以做任何面板上的调整（基本 / 曲线 / HSL / 分离色调 / 颜色分级 / 效果 / 相机校准），"
                "只要你能说明它是为了这张照片更好。",
                "以下两条不是审美选择，必须遵守：",
                "  · 只返回**需要改动**的字段，字段名与取值范围见上面的字段参考表（越界会被本地校验拒收）；",
                "  · 不要返回机器/版本字段与相机配置（见上面的禁用字段说明）。",
                "本程序**可以写入几何蒙版**（线性渐变 / 径向渐变 / 画笔）："
                "需要局部效果时用响应里的 masks 字段提出（最多 3 个），"
                "坐标用显示帧（你看到的方向），局部参数只能用受限的那几个；"
                "不需要就返回空数组。",
                "同一批照片保持基本一致的观感：先判断这批判什么，再让每张按自己的需要调整。",
            ]
        )

    lines = [f"【风格约束：{style_name or style.get('name', '未命名风格')}】"]

    summary = style.get("style_summary") or style.get("description")
    if summary:
        lines.append(f"风格概述：{scrub_absolute_kelvin(str(summary), style)}")

    # 训练时一并算出的"特征块"——比 param_ranges 的纯数字更像人话，
    # 模型对它们的遵循度明显高于一张统计表（实测：只给表 → 只调基本面板）。
    for key, label in _STYLE_CHARACTER_BLOCKS:
        block = style.get(key)
        if isinstance(block, str) and block.strip():
            lines.append(f"{label}：{scrub_absolute_kelvin(block.strip(), style)}")

    # 各维度的力度档位（实施步骤 1b，用户 2026-09-23 的裁决：风格只定方向与力度）。
    levels = style.get("dimension_levels")
    if isinstance(levels, dict) and levels:
        shown = [
            f"{entry.get('label') or key} {entry.get('level_text')}（{entry.get('intensity_label')}）"
            for key, entry in levels.items()
            if isinstance(entry, dict)
        ]
        if shown:
            lines.append("各维度力度档位（1=极轻 … 7=很重）：" + "、".join(shown))
            lines.append(
                "档位只说明**这套风格在某个维度上习惯用多大力**，它**不是**数值："
                "具体值必须按当前这张照片重新判断。同一维度的不同照片允许不同，"
                "也不应该给出同一个值；禁止把下面的历史统计值当作每张图的目标值照抄——"
                "实测这么做会让样本少的风格偏色、样本多的风格过饱和。"
            )

    rules = style.get("text_rules") or []
    if rules:
        lines.append(
            "风格倾向（**方向参考**：只在这张照片的情况与它相符时才采用，"
            "不是'每张都必须执行'的清单；方向和幅度都要按本图重新判断）："
        )
        for rule in rules:
            lines.append(f"  · {scrub_absolute_kelvin(str(rule), style)}")

    # 场景规则（实施步骤 1c）：**只来自用户的训练素材**（按场景标签分组统计），
    # 每条都带样本数；训练素材里没有的场景就不会出现在这里。
    scene_rules = style.get("scene_rules")
    if isinstance(scene_rules, list) and scene_rules:
        lines.append("场景规则（依据你的训练素材统计，括号里是样本数）：")
        for rule in scene_rules[:12]:
            if isinstance(rule, dict) and rule.get("text"):
                lines.append(f"  · {scrub_absolute_kelvin(str(rule['text']), style)}")
        lines.append(
            "  这些规则**只在对应场景成立**；遇到训练素材里没有的场景，"
            "请按上面的方向与力度档位自行判断，不要硬套其他场景的规则。"
        )

    # 白平衡习惯：相对 as-shot 的偏移（绝对值平均没有意义，见 style_profile 的说明）。
    wb = style.get("wb_offset")
    if isinstance(wb, dict) and wb.get("text"):
        lines.append(f"白平衡习惯（相对 as-shot）：{wb['text']}")
        lines.append(
            "  ⚠ 白平衡必须**逐张独立判断**：as-shot 只是起点，偏暖/偏冷是方向，"
            "禁止把上面的偏移量直接加到每张照片上。"
        )
    lines.append(
        "  ⚠ 风格文本里若出现「色温控制在 XXXX～XXXX K」这类**绝对区间**，"
        "那是训练素材里的历史取值记录（每张照片现场色温本就不同），"
        "**不是**每张图的目标值；基准永远是每张图自己的 as-shot。"
    )
    ranges = style.get("param_ranges") or {}
    if ranges:
        lines.append("历史偏好统计（mean 为均值、median 为中位数、min/max 为观察到的范围）：")
        lines.append(
            "⚠ 这些数字只是**历史参考**（用来看这套风格习惯的力度落在哪个量级）。"
            "**禁止**把 mean/median 当成每张图的目标值：他每张照片的力度本来就不一样"
            "（同一字段能从 min 到 max），照抄中位数会让整批照片变成同一套预设，"
            "既不像他的作品，也会出现「每张都像 HDR」的观感。"
        )
        lines.append(
            "  输出前自检：如果你打算给某个字段的取值正好等于上表的中位数，"
            "请停下来按本图的实测指标重新判断一次；同一批里不同照片的数值**应当不同**。"
        )
        # 只挑有实际统计意义的字段展示，避免把 100 个字段全塞进提示词。
        for field_name, stat in sorted(ranges.items()):
            if not isinstance(stat, dict):
                continue
            # 绝对色温**不进表**：它的 mean/median 是"训练素材里那些场景各自的现场色温"，
            # 对当前这张图没有任何含义，却是最容易被照抄的一个数。
            # 实测教训（2026-09-26 人像偏色）：风格文本里的"色温控制在 5300–6350K"
            # ＋ 这里的绝对统计，把机内 4600K 的片子（DSC03646.ARW）写成了 6100K。
            # 白平衡的证据走 wb_offset（相对 as-shot）＋ 逐图 as-shot 两条路。
            if field_name in ABSOLUTE_WB_FIELDS:
                continue
            spec = F.get_field(field_name)
            if spec is None or not spec.ai_writable:
                continue
            try:
                count = int(stat.get("count") or 0)
                suffix = f"（样本 {count}）"
                if 0 < count <= 2:
                    # 1–2 张样本的"风格"最容易被当成目标值照抄：它不是习惯，
                    # 只是某一张照片的偶然取值（实测最刺眼的例子：Vibrance 只有
                    # 1 个样本、范围 19~19，模型就给每一张都写 19）。
                    suffix += "——样本太少，只是个别照片用过，**不能**当成风格特征"
                lines.append(
                    f"  - {CRS_PREFIX}{field_name}：均值 {stat.get('mean'):.2f}，"
                    f"中位数 {stat.get('median'):.2f}，范围 {stat.get('min'):.2f} ~ {stat.get('max'):.2f}"
                    + suffix
                )
            except (TypeError, ValueError):
                continue

    used_panels = _style_panels(ranges)
    if used_panels:
        lines.append(
            "本风格出现过的面板（不要只调「基本」；但**只在某张图确实需要时**才动对应面板，"
            "不要为了'用全面板'而给每张图都加分离色调/晕影）："
            + "、".join(used_panels)
        )

    # 镜头与去边由程序逐图处理（步骤 4）：模型无需也不允许返回这些字段。
    lines.append(
        "镜头校正、去色差、以及**镜头晕影**都由程序按每张照片自己的基线处理"
        "（配置文件匹配、畸变、晕影缩放、去边开关也是）：**不要返回这些字段**。"
        "需要压暗四角请只用「裁剪后晕影」（PostCropVignetteAmount）；"
        "需要消除紫/绿色边请在风格倾向里说明意图，而不是直接写去边参数。"
    )

    mask = style.get("mask_usage")
    if isinstance(mask, dict) and mask.get("samples_with_masks"):
        ratio = float(mask.get("mask_usage_ratio") or 0.0)
        per = float(mask.get("corrections_per_sample") or 0.0)
        lines.append(
            f"局部调整习惯：{mask['samples_with_masks']}/{mask.get('samples_total', '?')} 张"
            f"（{ratio * 100:.0f}%）带蒙版，平均每张 {per:.2f} 个。"
        )
        if ratio >= MASK_USAGE_MIN_RATIO:
            # 本程序现在**能**写几何蒙版了（线性/径向/画笔），见 acb/xmp/masks.py。
            lines.append(
                "本风格**确实会用局部调整**：需要时请用响应里的 masks 字段提出来（最多 3 个）："
                "写清 kind（linear/radial/brush）、target（对哪里）、intent（干什么）、"
                "**显示帧**坐标（就是你在画面上看到的位置，天空在上方），"
                "以及至少一个局部参数。"
                f"可用局部参数（括号里是**你训练素材里实测到的习惯范围**）："
                f"{masks_mod.ai_local_params_text(masks_mod.ranges_from_style(style))}。"
                "尽量落在习惯范围内；确有必要可以**略微**超出（程序允许有限放宽并会记一笔），"
                "但超出太多会被夹回 —— 所以不要为了“更明显”而把幅度拉满。"
                "典型用法：全局滑块解决不了、又不想牺牲别处时的局部问题"
                "（例如逆光下为了保背景人脸欠曝又偏黄：局部提亮 + 把肤色按肤色线调正 + 略降纹理磨皮），"
                "或只是给某一块区域加减明暗。不需要就返回空数组；不要为了凑数每个都加一个。"
                "⚠ **画面里出现肤色时，请真的用上局部色温 / 局部色调**："
                "这是这位用户自己的习惯（他的蒙版里就出现过 local 色温 −7~+17、色调 −6~+7、饱和度），"
                "而全局白平衡已经由程序管住了 —— 只靠全局滑块修人脸会让背景一起变色。"
                "指标行里给出肤色实测值（占比 / 肤色 a*/b*）：肤色偏黄就给**负**的局部色温"
                "（变冷、减黄），偏红就给对应的局部色调；幅度先小一点（±3~8）就够，"
                "不要动辄拉满。"
                "⚠ 给几何之前先自检一次：这个矩形/两个点真的只框住你说的那个目标吗？"
                "把它套在整张画面上想一遍（脸不会占画面好大一块；天空渐变的起点应在你要保护的那条边界上），"
                "不要顺手把旁边的窗子/墙面/背景也框进去。"
                "**坐标必须按 kind 用对应的键名**（不要混用，混用会整条被丢弃）：\n"
                "  · linear（渐变）→ zero/full，各是 [x, y]（0..1）。"
                "例：{\"kind\": \"linear\", \"target\": \"天空\", \"intent\": \"压暗\", "
                "\"zero\": [0.5, 0.75], \"full\": [0.5, 0.05], "
                "\"local\": {\"LocalHighlights2012\": -30}}\n"
                "  · radial（径向）→ rect = [上, 左, 下, 右]（0..1），可加 feather。"
                "例：{\"kind\": \"radial\", \"target\": \"人物面部\", \"intent\": \"提亮并轻微磨皮\", "
                "\"rect\": [0.32, 0.40, 0.58, 0.66], \"feather\": 60, "
                "\"local\": {\"LocalExposure2012\": 0.35, \"LocalTexture\": -12}}\n"
                "  · brush（画笔）→ dabs = [[x, y], …]（最多 60 个点），可加 radius（0.001..0.5）。"
                "例：{\"kind\": \"brush\", \"target\": \"人物面部\", \"intent\": \"磨皮\", "
                "\"dabs\": [[0.50, 0.50], [0.55, 0.52], [0.45, 0.53]], \"radius\": 0.08, "
                "\"local\": {\"LocalTexture\": -15}}"
            )
        else:
            lines.append(
                "但比例很低：**默认不要用蒙版**（masks 返回空数组），"
                "优先用全局参数表达，除非画面确实有必须单独处理的区域。"
            )

    ignored = style.get("ignore_fields") or []
    if ignored:
        lines.append(
            "已判定为相机/镜头默认值（不反映用户偏好，请不要据此推断）："
            + "、".join(CRS_PREFIX + str(item.get("field", item)) if isinstance(item, dict) else CRS_PREFIX + str(item)
                        for item in ignored[:40])
        )

    return "\n".join(lines)


def build_output_user_text(
    *,
    style_block: str,
    user_prompt: str,
    items: list[dict[str, Any]],
    batch_stats: dict[str, float] | None = None,
    auto_match: bool = False,
) -> str:
    """组装输出模式的 user 文本部分。

    items 每项含：file_id / filename / preview_source / preview_note / caption（可选）
    batch_stats 非空时注入"向批内中位数靠拢"的指令（问题 e 的默认策略）。
    """
    parts: list[str] = [style_block]

    # --- 用户提示词 / 默认策略 ---------------------------------------------
    if user_prompt and user_prompt.strip():
        parts.append("【本批提示词】\n" + user_prompt.strip())
    else:
        # 问题 e：用户不给提示词时的明确默认策略，绝不空等或报错。
        parts.append(
            "【本批提示词】\n（用户未提供提示词，已启用内置默认策略）\n"
            "请做保守的全局一致性校正：统一曝光与白平衡观感，"
            "曝光改动不超过 ±0.5 EV，对比度/高光/阴影/白色/黑色不超过 ±20，"
            "自然饱和度/饱和度不超过 ±20；不做 HSL 分区偏移与颜色分级，"
            "不改动镜头校正与相机配置文件。"
        )

    # --- 批内统计（自动一致性校正模式的核心） ------------------------------
    if auto_match and batch_stats:
        parts.append(
            "【批内一致性目标】\n"
            + AUTO_MATCH_INSTRUCTION_TEMPLATE.format(
                count=int(batch_stats.get("count", len(items))),
                luma=batch_stats.get("luma", 128.0),
                contrast=batch_stats.get("contrast", 55.0),
                cct=batch_stats.get("cct", 5500.0),
                sat=batch_stats.get("sat", 100.0),
            )
        )

    # --- 图片清单 -----------------------------------------------------------
    manifest_lines = ["【本批图片清单】"]
    for item in items:
        line = f"  - file_id=\"{item['file_id']}\"  文件名=\"{item.get('filename', '')}\""
        source = item.get("preview_source")
        if source:
            line += f"  预览来源={source}"
        if item.get("preview_note"):
            line += f"（{item['preview_note']}）"
        manifest_lines.append(line)
        caption = item.get("caption")
        if caption:
            # caption 降级通道：模型看不到图，用本地统计生成的文字描述代替。
            manifest_lines.append(f"      画面描述：{caption}")
        metrics = item.get("metrics")
        if metrics:
            # 逐图指标（步骤 3）：让模型有"这张图现在长什么样"的量化锚点。
            # 实测过的毛病是模型把风格统计的中位数当成每张图的目标值，
            # 所以这句必须把"按本图重新判断"的意思说透。
            manifest_lines.append(f"      本图实测指标：{metrics}")
            manifest_lines.append(
                "      （这些数字只描述这张图**当前**的样子。请据此重新判断本图要调多少，"
                "不要套用风格统计里的数值，也不要让同一批里每张图取值相同。）"
            )
    parts.append("\n".join(manifest_lines))

    parts.append(
        "请为清单中的**每一个** file_id 各返回一项，"
        "并严格按给定的 JSON Schema 输出（items 数组）。"
        "若某张图需要局部处理，请在对应项的 masks 数组里给出"
        "（写法与示例见风格约束里的「局部调整」段落）。"
    )
    return "\n\n".join(parts)


# ============================================================================
# 训练模式
# ============================================================================

TRAINING_SYSTEM_ROLE = """你是一位资深的摄影后期分析师，负责归纳某位用户的调色偏好。

程序已经把用户每张照片的 ACR 设置从 XMP 解析成了 JSON 交给你。
你的任务是分析这些**用户实际做过的手动调整**，总结出可复用的风格规则，
供后续批量调色时复用。"""

TRAINING_KEY_WARNING = """【最重要的判断前提：区分"用户调整"与"相机默认"】
ACR 会把**全部字段**写入 XMP（包括大量值为 0 的默认项），
所以"字段出现在设置里"**不等于**"用户调整过它"。
程序已经做了三层差分并把结果放在每一张的 excluded_fields 里：
  1. 与相机出厂默认基线（XMP 的 crd: 命名空间）比对；
  2. 与 ACR 出厂默认值比对；
  3. 批内不变项剔除（整批取值完全一致的字段判为默认）。
**请以 excluded_fields 为准**，不要把这些字段当成用户偏好。
典型例子：整批都是 crs:ColorNoiseReduction=25、crs:ParametricShadowSplit=25、
crs:LensProfileDistortionScale=100 —— 这些是 ACR/机内的默认值，不是偏好。

同时注意区分两类"看起来一样"的字段：
  - crs:HueAdjustment*（范围 -100..100）是**相对偏移量**；
  - crs:SplitToning*Hue（范围 0..360）是**绝对色相角**。
二者量纲不同，不要混起来比较。"""


TRAINING_SCENE_LABEL_INSTRUCTION = """【必须为每张样本打一个场景标签】
输出里的 scene_labels 是「file_id → 2–8 字场景名」，例如：
  {"a1b2c3": "风景（日出日落）", "d4e5f6": "人像（环境）", ...}
为什么必须做：程序会用这些标签去统计**「同类场景下用户习惯怎么调」**，
再把结论写回这份风格档案（只用于该场景）。没有标签，这一层就拿不到依据。
用词要求：
  · 同一批里的同类照片必须用**同一个词**（否则统计会碎成一堆 n=1 的类别）；
  · 优先用这批候选：人像（正面/半身）、人像（环境）、风景（日出日落）、风景（山野）、
    风景（水景/海景）、城市建筑、夜景弱光、街拍人文、花卉微距、静物、动物、运动、
    室内、逆光剪影、雪景、雾天/阴天、航拍/俯拍；
  · 没有合适的就自拟一个短名（2–8 字）。
  ⚠ 标签只能说"这是什么场景"，**不能带调色指令**（写「落日海景」可以，
    写「需要提亮的落日海景」不行）——调色结论由程序按参数统计得出，不靠标签文字。"""


TRAINING_NO_ABSOLUTE_INSTRUCTION = """【写规则时：只写方向与相对量，禁止绝对数值】
text_rules / scene_rules / 各特征块里的每一句话，都是**给下一次调色当方向参考**的，
不是给某一张照片的答案。所以：
  · **绝不写绝对色温**（「色温控制在 5300–6350K」「色温 5500K」这类）。
    每张照片现场光不同，5300K 在那张照片上可能正合适、在这张上就是整片偏黄。
    白平衡只能写成**相对 as-shot 的偏移方向**（「习惯略偏暖」/「冷一点」），
    程序另有一套从样本统计出来的偏移量，会自己带上；
  · 同理不要写其它"绝对刻度"当目标（「亮度固定在 +0.3」「饱和度拉到 -8」），
    要写"往哪个方向、大概多大幅度"（叠加「轻/中/重」这类量级词）；
  · 每条规则尽量带上**适用条件**（什么场景/什么光线下才这么做），
    没有条件的规则到了别的场景就是硬套。
  · 涉及颜色的通道级规则必须写清代价：例如"把橙色相推向红"会同时移动**肤色**，
    如果样本里这么做只针对大片暖色背景/秋叶，就写明这一点，
    不要写成一条不分场景的全局规则（否则人像肤色会被推离肤色指示线）。"""


def build_training_system_prompt() -> str:
    """组装训练模式的 system 提示词。"""
    return "\n\n".join(
        [
            TRAINING_SYSTEM_ROLE,
            TRAINING_KEY_WARNING,
            TRAINING_SCENE_LABEL_INSTRUCTION,
            TRAINING_NO_ABSOLUTE_INSTRUCTION,
            "【可用字段参考】" + render_field_reference(),
        ]
    )


def build_training_user_text(samples: list[dict[str, Any]], style_name: str) -> str:
    """组装训练模式的 user 文本。

    samples 每项含：file_id / filename / params（用户实际调整）/ curves /
    excluded_fields（判为默认而排除的字段及理由）/ mask_usage / caption
    """
    parts: list[str] = [
        f"以下是 {len(samples)} 个训练样本。请归纳用户的调色偏好，命名意向：{style_name}",
        "【约定】params 中只列出了**用户实际调整过**的字段；"
        "excluded_fields 中是判定为相机/镜头默认值的字段，请忽略它们。",
    ]

    for sample in samples:
        block = [
            f"\n=== 样本 file_id=\"{sample['file_id']}\" 文件名=\"{sample.get('filename', '')}\" ===",
        ]
        caption = sample.get("caption")
        if caption:
            block.append(f"画面描述：{caption}")

        params = sample.get("params") or {}
        if params:
            block.append("用户实际调整的参数：")
            for name, value in sorted(params.items()):
                spec = F.get_field(name)
                unit = f"（{spec.panel}）" if spec else ""
                block.append(f"  - {CRS_PREFIX}{name} = {value}{unit}")
        else:
            block.append("用户实际调整的参数：无（该样本仅有相机默认值）")

        curves = sample.get("curves") or {}
        if curves:
            block.append("点曲线控制点（0..1 域）：")
            for curve_name, points in curves.items():
                block.append(f"  - {CRS_PREFIX}{curve_name} = {points}")

        mask_usage = sample.get("mask_usage")
        if mask_usage:
            block.append(f"局部调整使用情况：{mask_usage}")

        excluded = sample.get("excluded_fields") or []
        if excluded:
            block.append(
                "判定为相机/镜头默认值而排除（不要计入偏好）："
                + "、".join(str(e) for e in excluded[:60])
            )

        parts.append("\n".join(block))

    parts.append(
        "\n请输出 JSON：整体风格概括、可执行的规则列表、以及饱和度/对比度/暗部色偏等维度倾向。"
        "规则要具体可执行（例如「人像时把橙色的明亮度提高 10–20 以提亮肤色」），"
        "不要写「调得好看」这类无法执行的话。"
        "统计数值（均值/中位数/范围）由程序本地计算，你不需要给出。"
    )
    return "\n".join(parts)


def build_caption_system_prompt() -> str:
    """caption 降级通道的 system 提示词（硬约束 #19）。

    当模型 supports_vision=false 时，程序无法把图片交给它，
    只能给出本地计算的统计描述。必须让模型明确知道
    "你看不到图，只有统计量" —— 否则它会编造画面内容（幻觉）。
    """
    return "\n\n".join(
        [
            OUTPUT_SYSTEM_ROLE.replace(
                "你看到的是", "程序会用数字统计描述画面（你没有看图能力，请勿编造画面内容）。原本你看到的是"
            ),
            "【你无法看到图片】\n"
            "你只能看到由程序在 sRGB 缩略图上用 numpy 计算的统计量"
            "（平均亮度、亮度标准差、RGB 均值、平均饱和度、死黑/死白比例、估算色温）。\n"
            "严禁编造画面内容（例如「画面中有两个人」）。"
            "请只依据统计量做保守判断：曝光、白平衡、对比度、饱和度这四类。\n"
            "对于需要理解画面语义的参数（HSL 分区、蒙版、局部调整），不要输出。",
            OUTPUT_OUTPUT_RULES,
            "【可用字段参考】" + render_field_reference(),
            render_machine_field_warning(),
        ]
    )
