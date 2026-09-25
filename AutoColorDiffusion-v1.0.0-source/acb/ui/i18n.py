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
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QLibraryInfo, QTranslator
from PyQt6.QtWidgets import QApplication

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
    """
    app = QApplication.instance()
    if app is None:
        return False
    return install_chinese_translations(app)
