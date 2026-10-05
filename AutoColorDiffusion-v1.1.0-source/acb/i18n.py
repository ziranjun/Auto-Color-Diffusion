# -*- coding: utf-8 -*-
"""界面语言（简体中文 / English）。

【为什么单独做一层，而不是直接用 Qt 的 .qm】
    `acb/ui/i18n.py` 只解决"Qt **自带**文案"（输入框右键菜单、确定/取消按钮）。
    本程序自己的文案是硬编码的中文字面量，Qt 的翻译机制**碰不到**它们
    —— QTranslator 只会被 `tr()` 调用看到，而我们从没写过 tr()。
    所以这里自己做一层：以"中文原文"为键查英文表（见 `acb/i18n_catalog.py`），
    代码里在**显示边界**调用 `T(...)`。用中文原文当键的好处：
      · 不会出现"改了中文忘了改英文"—— 键变了，`tools/i18n_scan.py` 门禁立刻报缺失；
      · 不需要给每条文案另起一个 key（那种写法在本项目的历史里从没活过一轮重构）。

【只翻"给人看的"，不翻"给机器看的"】
    · 参与判断比较的字符串（风格名、状态值、正则片段）**绝不**经过 T()，
      否则切换语言会改变程序行为；
    · AI 提示词与风格规则文本保持中文：它们是发给模型的，不是给人读的，
      翻译它们会引入行为回归（模型对中文规则的表现已被反复实测过）；
    · 正则里用于匹配服务端中文报错的片段（如"余额不足"）保持原样。

【为什么切换语言要重启】
    Qt 没有"整体重译现有界面"的机制：控件文本在构造时就烘死了。
    逐控件重译要为上百个控件维护重译路径（漏一个就是半中半英），
    所以 V1.1.0 采用"保存偏好 + 提示重启"，并给出「立即重启」一步到位。

【本模块刻意不依赖 Qt】
    管线、常量、异常、CLI 都要用 T()，而这些模块不该被迫 import PyQt6
    （`tools/dump_crs_fields.py` 这类纯逻辑脚本也在用它们）。
    Qt 相关的那部分（装 .qm）留在 `acb/ui/i18n.py`。
"""

from __future__ import annotations

from typing import Any, Final

LANG_ZH: Final[str] = "zh"
LANG_EN: Final[str] = "en"

# 默认中文：老用户的 preferences.json 里没有 language 键，读出来必须还是中文界面。
DEFAULT_LANGUAGE: Final[str] = LANG_ZH

# 语言菜单里的显示名。这两个字符串**在任何语言下都保持原样**
# （用户 2026-09-26 明确要求），所以它们不进翻译表、也不经过 T()。
LANGUAGE_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    (LANG_ZH, "简体中文"),
    (LANG_EN, "English"),
)

# 语言菜单的标题也**在任何语言下都保持原样**：它叫 "Language"。
LANGUAGE_MENU_TITLE: Final[str] = "Language"

_current: str = DEFAULT_LANGUAGE

# 英文模式下查不到的键（只用于自检：`tools/i18n_scan.py` 门禁 + GUI 用例断言"渲染时没缺词"）。
_missing: set[str] = set()

_EN_CATALOG: dict[str, str] | None = None


def catalog() -> dict[str, str]:
    """英文文案表（惰性加载：中文模式下整个程序不会碰它）。"""
    global _EN_CATALOG
    if _EN_CATALOG is None:
        from .i18n_catalog import EN

        _EN_CATALOG = EN
    return _EN_CATALOG


def normalize_language(value: Any) -> str:
    """把任意输入规整成受支持的语言代码；不认识的一律回退中文。"""
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in (LANG_ZH, LANG_EN):
            return cleaned
    return DEFAULT_LANGUAGE


def set_language(value: Any) -> str:
    """设置当前语言，返回规整后的值。**不写偏好文件**（写盘由界面负责）。"""
    global _current
    _current = normalize_language(value)
    return _current


def get_language() -> str:
    return _current


def is_english() -> bool:
    return _current == LANG_EN


def language_display_name(value: Any) -> str:
    """"简体中文" / "English"（恒定显示名，与当前语言无关）。"""
    normalized = normalize_language(value)
    for code, label in LANGUAGE_CHOICES:
        if code == normalized:
            return label
    return LANGUAGE_CHOICES[0][1]


def init_language_from_preferences() -> str:
    """启动时从 `preferences.json` 读语言。

    偏好文件缺失/损坏 → 中文（`load_preferences` 自己就容错，返回 {}）。
    读盘失败也不该让程序起不来，所以整段包在 try 里。
    """
    try:
        from .preferences import get_str

        return set_language(get_str("language", DEFAULT_LANGUAGE))
    except Exception:  # noqa: BLE001 - 语言读不到就用默认值，绝不拦住启动
        return set_language(DEFAULT_LANGUAGE)


def T(text: str) -> str:
    """把界面文案翻成当前语言；中文模式（或查不到）原样返回。

    ⚠ 只用在**显示边界**：setText / 日志 / 弹窗 / 打印。
      绝不能用在 if 比较、字典键、正则、文件名上。
    """
    if _current == LANG_ZH or not text:
        return text
    translated = catalog().get(text)
    if translated is None:
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            # 只把**含中文**的未命中记成缺词：像 `<span style=…>`、`{0} @ {1}`
            # 这类模板本来就不需要条目（T 查不到即原样返回）。
            _missing.add(text)
        return text
    return translated


def T_display(name: str) -> str:
    """显示名专用翻译（风格名 / 连接名 / 服务商名）。

    与 T() 的区别：**未命中不记缺词**。因为用户名下的风格名、连接名是用户自己起的，
    本来就不该有条目（"海边暖调" 在英文界面下也应原样显示）。
    而内置名（「AI自主决策」、「通义千问」）在表里有条目，会正常翻。
    """
    if _current == LANG_ZH or not name:
        return name
    return catalog().get(name, name)


def caption_sep() -> str:
    """说明文字里的分隔符：中文用全角空格（视觉平衡），英文用半角空格。

    滑块旁那一行是**按实测宽度**排版的（见 widgets._caption_required_width），
    全角空格在英文里既难看也会多占一点宽度，所以随语言切换。
    """
    return " " if _current == LANG_EN else "　"


def TF(template: str, *args: Any, **kwargs: Any) -> str:
    """"带占位符的文案"的翻译 + 填充（f-string 在编译期就定型了，没法查表）。

    用法：TF("已完成 {0} 张", n) / TF("共 {total} 个文件", total=n)
    先查表再 format —— 英文模板里的占位符名必须与调用处一致（门禁脚本会核对）。
    """
    translated = T(template)
    if not args and not kwargs:
        return translated
    return translated.format(*args, **kwargs)


def T_in(language: Any, text: str) -> str:
    """按**指定语言**取文案（不看当前语言）。

    存在的理由只有一个：「重启后生效」那个对话框必须用**目标语言**显示。
    用户 2026-09-26 明确要求：切到 English 时，提示与 "Restart now / Later"
    也要是英文；切回简体中文时又要是中文 —— 而那一刻进程的界面语言还是旧的，
    所以必须有"绕过当前语言查表"的入口。

    ⚠ 与 T() 一样：只用在显示边界，不能拿它的结果参与判断。
    """
    if normalize_language(language) == LANG_ZH or not text:
        return text
    return catalog().get(text, text)


def TF_in(language: Any, template: str, *args: Any, **kwargs: Any) -> str:
    """`T_in()` + 填充占位符（对应 `TF()`）。"""
    translated = T_in(language, template)
    if not args and not kwargs:
        return translated
    return translated.format(*args, **kwargs)


def missing_keys() -> frozenset[str]:
    """英文模式下查表失败过的键（自检用）。"""
    return frozenset(_missing)


def reset_missing() -> None:
    _missing.clear()
