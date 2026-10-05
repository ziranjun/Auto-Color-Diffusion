# -*- coding: utf-8 -*-
"""扫全窗口：哪些控件的文字被切掉了。

为什么需要它：用户反馈"很多地方的文字都被切掉一部分"，靠肉眼一个一个找不可靠，
而且改完还容易漏。这里按控件类型分别算"文字需要多宽"，再和**真实可见宽度**
（沿父链求交集，含父级裁剪）比。

判定依据：
    QLabel / QAbstractButton / QComboBox → 用 QStyle.sizeFromContents 算需要多大，
    这样 QSS 里的 padding / 图标 / 箭头宽度都算进去了（手写 text+24 会漏）。
如果 sizeFromContents 不可用，退化为"文字宽度 + 固定内边距"。

用法：python tools/text_clip_scan.py [窗口宽度...]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = __import__("tempfile").mkdtemp(prefix="acb_clip_")

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print 中文或 ✓/✗ 会抛
# UnicodeEncodeError —— 那会把"未通过清单"整段变成 traceback，等于把最关键的信息
# 藏起来（本项目真的踩到过）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QPoint, QRect, QSize, Qt          # noqa: E402
from PyQt6.QtGui import QFontMetrics                        # noqa: E402
from PyQt6.QtWidgets import (                               # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QDialog,
    QHeaderView,
    QLabel,
    QStyle,
    QStyleOptionButton,
    QStyleOptionComboBox,
    QWidget,
)

import acb.ui.main_window as MW                              # noqa: E402


def visible_width(widget: QWidget) -> int:
    """控件真正能被看到的宽度（沿父链求交集，含父级裁剪）。"""
    window = widget.window()
    rect = QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())
    parent = widget.parentWidget()
    while parent is not None:
        rect = rect.intersected(QRect(parent.mapTo(window, QPoint(0, 0)), parent.size()))
        parent = parent.parentWidget()
    return max(0, rect.width())


def needed_width(widget: QWidget) -> int | None:
    """控件里的文字需要多宽？算不出来时返回 None。"""
    style = widget.style()
    if isinstance(widget, QLabel):
        if widget.wordWrap():
            # 开了 wordWrap 的标签会自己折行，不存在"被截"（只是这一行量不了）
            return None
        text = widget.text()
        if not text or "\n" in text:      # 多行的不算（会自动换行）
            return None
        return QFontMetrics(widget.font()).horizontalAdvance(text)
    if isinstance(widget, QComboBox):
        opt = QStyleOptionComboBox()
        opt.initFrom(widget)
        opt.currentText = widget.currentText()
        opt.iconSize = widget.iconSize()
        return style.sizeFromContents(
            QStyle.ContentsType.CT_ComboBox, opt, QSize(0, 0), widget
        ).width()
    if isinstance(widget, QAbstractButton):
        text = widget.text()
        if not text:
            return None
        opt = QStyleOptionButton()
        opt.initFrom(widget)
        opt.text = text
        opt.icon = widget.icon()
        opt.iconSize = widget.iconSize()
        opt.features = QStyleOptionButton.ButtonFeature.None_
        content = QStyle.ContentsType.CT_CheckBox if widget.isCheckable() else (
            QStyle.ContentsType.CT_PushButton
        )
        return style.sizeFromContents(content, opt, QSize(0, 0), widget).width()
    return None


def scan(window: QWidget, label: str) -> list[str]:
    findings: list[str] = []
    for widget in window.findChildren(QWidget):
        if not widget.isWidgetType() or not widget.isVisibleTo(window):
            continue
        need = needed_width(widget)
        if need is None:
            continue
        have = visible_width(widget)
        if need > have:
            text = ""
            if isinstance(widget, QLabel):
                text = widget.text()
            elif isinstance(widget, QAbstractButton):
                text = widget.text()
            elif isinstance(widget, QComboBox):
                text = f"[{widget.currentText()}]"
            findings.append(
                f"{label}  {type(widget).__name__:<12} {text[:22]!r:<26} "
                f"需要={need:4} 可见={have:4} 差={need - have:3}"
            )

    # 表头：QHeaderView 的多段标题也会被切，而且不属于普通子控件
    for header in window.findChildren(QHeaderView):
        metrics = QFontMetrics(header.font())
        for section in range(header.count()):
            text = header.model().headerData(
                section, header.orientation(), Qt.ItemDataRole.DisplayRole
            )
            if not text:
                continue
            need = metrics.horizontalAdvance(str(text))
            have = header.sectionSize(section)
            if need > have:
                findings.append(
                    f"{label}  QHeaderView   {str(text)[:22]!r:<26} "
                    f"需要={need:4} 可见={have:4} 差={need - have:3}"
                )
    return findings


def scan_dialogs(window: MW.MainWindow, label: str) -> list[str]:
    """把隐藏对话框 show() 出来再扫 —— 用户打开它们时同样会看到文字被切。"""
    findings: list[str] = []
    for name, dialog in (
        ("高级设置", window._adv_dialog),
        ("日志", window._log_dialog),
    ):
        if not isinstance(dialog, QDialog):
            continue
        dialog.show()
        dialog.adjustSize()
        QApplication.processEvents()
        for line in scan(dialog, f"{label}/{name}"):
            findings.append(line)
        dialog.hide()
    return findings


def scan_menu_bar(window: QWidget, label: str) -> list[str]:
    """菜单栏本身会不会溢出（英文标题更长，挤到放不下时 Qt 会塞成 "»"）。

    菜单弹出后的**条目**不用查：Qt 会把弹窗按内容自动定宽，条目不会被裁；
    会被挤的是**菜单栏这一行**（窗口宽度固定）。
    """
    from PyQt6.QtWidgets import QMenuBar

    findings: list[str] = []
    bars = window.findChildren(QMenuBar)
    if not bars:
        return findings
    bar = bars[0]
    metrics = QFontMetrics(bar.font())
    total = sum(
        metrics.horizontalAdvance(action.text()) + 22
        for action in bar.actions() if action.text()
    )
    available = window.width()
    if total > available:
        titles = [a.text() for a in bar.actions() if a.text()]
        findings.append(
            f"{label}  QMenuBar      {str(titles)[:30]!r:<32} 需要={total:4} 可见={available:4} "
            f"差={total - available:3}"
        )
    return findings


def scan_connection_dialog(window: MW.MainWindow, label: str) -> list[str]:
    """连接对话框（英文文案明显更长，且它是用户最常打开的对话框）。"""
    from acb.ui.connection_dialog import ConnectionDialog

    findings: list[str] = []
    try:
        config = window._models_config
        presets = config.providers if config is not None else {}
        dialog = ConnectionDialog(presets, window)
    except Exception as exc:  # noqa: BLE001 - 扫描失败不该让工具崩
        return [f"{label}/连接对话框 构造失败：{type(exc).__name__}: {exc}"]
    dialog.show()
    dialog.adjustSize()
    QApplication.processEvents()
    for line in scan(dialog, f"{label}/连接对话框"):
        findings.append(line)
    dialog.hide()
    dialog.deleteLater()
    return findings


def main() -> int:
    # 本工具会真实构造主窗口，而主窗口启动时会跑一遍**启动清理**
    # （删过期的侧车备份，见 acb/cleanup.py::run_startup_cleanup）。
    # 自检/扫描工具不该有副作用，所以这里冻结清理：
    # 不删任何东西（包括用户的备份）。
    os.environ.setdefault("ACB_NO_CLEANUP", "1")

    parser = argparse.ArgumentParser(description="扫描界面文字被裁切的位置")
    parser.add_argument("widths", nargs="*", type=int, default=[], help="窗口宽度（默认 1280 1440 1920）")
    parser.add_argument("--lang", choices=("zh", "en"), default="zh",
                        help="扫描语言（英文文案更长，验收必跑）")
    args = parser.parse_args()

    from acb import i18n, preferences

    # 语言偏好：扫描结束时必须还原。
    # 【为什么】本工具是写在**真实**数据目录里的（它要看用户的真实字体/风格），
    # 以前它直接用 update_preferences 改了语言却不还原 —— 用户跑一次
    # `--lang en` 验收，自己的界面语言就被静默改成英文了。
    saved_language = preferences.load_preferences().get("language")
    preferences.update_preferences(language=args.lang)
    i18n.set_language(args.lang)

    widths = args.widths or [1280, 1366, 1440, 1920]
    app = QApplication([])
    window = MW.MainWindow()
    window.show()
    print(f"语言={args.lang}（菜单标题：{[a.text() for a in window.menuBar().actions()]}）")

    total = 0
    for width in widths:
        window.resize(width, MW.WINDOW_HEIGHT)
        app.processEvents()
        for page in (0, 1):                     # 输出页 / 训练页
            window.option_stack.setCurrentIndex(page)
            app.processEvents()
            name = "输出页" if page == 0 else "训练页"
            for line in scan(window, f"{width}px/{name}"):
                print(line)
                total += 1
        window.option_stack.setCurrentIndex(0)
        app.processEvents()
        for line in scan_menu_bar(window, f"{width}px"):
            print(line)
            total += 1
        for line in scan_dialogs(window, f"{width}px"):
            print(line)
            total += 1
        for line in scan_connection_dialog(window, f"{width}px"):
            print(line)
            total += 1
        print(f"--- {width}px 完成 ---")
    print(f"\n共发现 {total} 处文字被切")

    # 还原语言偏好（见上面的说明）：扫描是"看一眼"，不该改用户的设置。
    if saved_language is None:
        data = preferences.load_preferences()
        data.pop("language", None)
        preferences.save_preferences(data)
    else:
        preferences.update_preferences(language=saved_language)
    return 0


if __name__ == "__main__":
    sys.exit(main())
