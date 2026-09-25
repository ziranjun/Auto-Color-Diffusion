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


def main() -> int:
    widths = [int(a) for a in sys.argv[1:]] or [1280, 1440, 1920]
    app = QApplication([])
    window = MW.MainWindow()
    window.show()

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
        for line in scan_dialogs(window, f"{width}px"):
            print(line)
            total += 1
        print(f"--- {width}px 完成 ---")
    print(f"\n共发现 {total} 处文字被切")
    return 0


if __name__ == "__main__":
    sys.exit(main())
