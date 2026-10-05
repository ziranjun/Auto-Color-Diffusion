# -*- coding: utf-8 -*-
"""界面中文化：装上 Qt 自带的中文翻译。

【为什么需要这个模块】
    有一些文案**不在我们的代码里**，是 Qt 自己提供的：

      - `QLineEdit` / `QPlainTextEdit` 的右键菜单（Undo / Redo / Cut / Copy /
        Paste / Delete / Select All）—— 密钥输入框、尾缀输入框、输出目录输入框、
        提示词框全都会用到；
      - `QInputDialog` 的「OK / Cancel」（例如索取 API 密钥那个弹窗）；
      - `QMessageBox` 的标准按钮。

    这些字符串写在 Qt 的 .qm 翻译文件里，不装 `QTranslator` 就永远是英文
    （用户反馈过：密钥输入框右键出来的是英文菜单）。
    逐个控件去自己写一份中文右键菜单是重复劳动，而且 Qt 版本一升级就会漏掉
    新增项；装一次翻译，所有标准控件与标准对话框一起变中文。

【为什么找不到文件也不报错】
    翻译文件是"锦上添花"：缺了最多是英文菜单，绝不能让程序起不来
    （打包漏带文件、用户删了 _internal 里的目录都可能发生）。

【V1.1.0 起：语言是"可选的"】
    英文模式下**不装**中文 .qm —— Qt 的源语言就是英文，什么都不装即英文界面。
    所以这里提供 `configure_qt_translations(app)`（按当前语言分流）与
    `ensure_ui_language()`（任何建界面的入口调它，等价于过去的 ensure_chinese_ui）。
    语言状态本体住在 `acb/i18n.py`（那一层不依赖 Qt）。
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QLibraryInfo, QTranslator
from PyQt6.QtWidgets import QApplication

from .. import i18n as _lang

# QTranslator 必须保活：Qt 只持有指针，被 Python 回收后翻译立刻失效
# （现象是"装了翻译但仍然是英文"，而且很难查）。
_TRANSLATORS: list[QTranslator] = []

# qtbase 覆盖控件与标准对话框；qt 覆盖其余杂项（两个都要装）。
_QT_TRANSLATIONS = ("qtbase", "qt")


def _translation_dirs() -> list[str]:
    """Qt 翻译文件可能所在的两个目录。

    第一个是 Qt 自己认为的位置（开发环境就是 .venv 里那份）；
    打包后 QLibraryInfo 偶尔指不到，于是回落到 PyQt6 包内的 translations 目录
    —— acb.spec 已经把需要的那两个 .qm 放进去了。
    """
    dirs: list[str] = []
    try:
        dirs.append(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    except Exception:
        pass
    try:
        import PyQt6

        dirs.append(str(Path(PyQt6.__file__).parent / "Qt6" / "translations"))
    except Exception:
        pass
    return [d for d in dirs if d]


def install_chinese_translations(app: QApplication) -> bool:
    """给整个应用装上中文翻译，返回是否至少装上一个。

    幂等：重复调用不会重复安装（同一个 QTranslator 只装一次）。
    """
    if _TRANSLATORS:
        return True
    dirs = _translation_dirs()
    installed = False
    for name in _QT_TRANSLATIONS:
        for directory in dirs:
            translator = QTranslator(app)
            if translator.load(f"{name}_zh_CN", directory):
                app.installTranslator(translator)
                _TRANSLATORS.append(translator)
                installed = True
                break
    return installed


def ensure_chinese_ui() -> bool:
    """确保当前 QApplication 已装上中文翻译（幂等，没有 app 时返回 False）。

    给"任何会建界面的入口"用的：主窗口构造时就调一次，
    这样不论是 app.py、自检脚本还是以后新增的入口，界面都是中文，
    不会出现"新入口忘了装翻译 → 右键菜单是英文"这种漏项。

    ⚠ V1.1.0 起它只是 `ensure_ui_language()` 的旧名字（保留给第三方脚本），
      新代码请用 `ensure_ui_language()`：英文模式下不该再装中文 .qm。
    """
    return ensure_ui_language()


def remove_translators(app: QApplication) -> None:
    """摘掉已装上的翻译（切到英文时用）。

    必须同时从保活列表里移除：留在列表里会被当作"已安装"，
    于是 `install_chinese_translations` 的幂等判断会直接把中文装回来。
    """
    while _TRANSLATORS:
        translator = _TRANSLATORS.pop()
        try:
            app.removeTranslator(translator)
        except Exception:  # noqa: BLE001 - 摘翻译失败不该拦住启动
            pass


def configure_qt_translations(app: QApplication) -> bool:
    """按**当前语言**配置 Qt 自带文案，返回是否有翻译被装上。

    · 简体中文 → 装 `qtbase_zh_CN.qm` / `qt_zh_CN.qm`（历史行为不变）；
    · English  → 不装（Qt 源语言即英文），并摘掉可能已装的旧翻译。
    """
    if _lang.get_language() == _lang.LANG_ZH:
        return install_chinese_translations(app)
    remove_translators(app)
    return False


def ensure_ui_language() -> bool:
    """按当前语言配置 Qt 自带文案（幂等，没有 app 时返回 False）。

    任何"会建界面"的入口都该在构造界面前调一次：这样不管是 app.py、
    自检脚本还是以后新增的入口，都不会漏装（或误装）翻译。
    """
    app = QApplication.instance()
    if app is None:
        return False
    return configure_qt_translations(app)
