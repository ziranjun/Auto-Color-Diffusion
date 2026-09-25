# -*- coding: utf-8 -*-
"""实时日志面板。

硬约束 #13：界面日志区只显示 INFO 及以上，DEBUG 仅落文件。
这条规则的落实分两处：
    1. logging_setup.UiLogSignalHandler 在 handler 层过滤掉 DEBUG（源头）；
    2. 本面板再次按级别着色，并且对 DEBUG 直接忽略（双保险，
       防止将来有人误加了其它 DEBUG 来源的 handler）。
"""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 日志区最大行数。
# 依据：QPlainTextEdit 在数万行时滚动与重绘会明显卡顿；
# 20000 行足以覆盖一整天的重度批量工作（按每张 10 行估算 ≈ 2000 张），
# 超出后从头部丢弃最老的日志——文件日志仍然保留完整记录。
MAX_LOG_LINES = 20000

# 各级别的显示颜色（白底黑字主题下，靠颜色区分严重程度）。
# 用深色系，保证在白底上可读。
LEVEL_COLORS = {
    logging.DEBUG: "#808080",
    logging.INFO: "#000000",
    logging.WARNING: "#B26A00",
    logging.ERROR: "#B00020",
    logging.CRITICAL: "#7A0018",
}

LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT",
}


class LogPanel(QWidget):
    """带清空/复制按钮的日志面板。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lines = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(MAX_LOG_LINES)
        # 等宽字体让"字段名=值"这类内容对齐，便于扫读。
        self.text.setStyleSheet("QPlainTextEdit { font-family: Consolas, 'Courier New', monospace; }")
        self.text.setPlaceholderText("运行日志会显示在这里（只显示 INFO 及以上级别，DEBUG 详情请查看日志文件）")

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_copy = QPushButton("复制全部")
        self.btn_clear = QPushButton("清空")
        buttons.addWidget(self.btn_copy)
        buttons.addWidget(self.btn_clear)

        layout.addWidget(self.text, 1)
        layout.addLayout(buttons)

        self.btn_clear.clicked.connect(self.clear)
        self.btn_copy.clicked.connect(self._copy_all)

    def append(self, message: str, level: int = logging.INFO) -> None:
        """追加一行日志（主线程调用）。

        本方法**只能**在主线程调用。工作线程通过 pyqtSignal 把消息
        发到主线程后再调用它（硬约束 #9）。
        """
        if level < logging.INFO:
            # 双保险：DEBUG 不进界面。
            return
        color = LEVEL_COLORS.get(level, "#000000")
        name = LEVEL_NAMES.get(level, "INFO")
        # HTML 转义，避免日志里的 < > & 破坏显示（用户路径里可能有这些字符）。
        safe = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        self.text.appendHtml(
            f'<span style="color:{color};">[{name}] {safe}</span>'
        )
        self._lines += 1

    def clear(self) -> None:
        self.text.clear()
        self._lines = 0

    def _copy_all(self) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text.toPlainText())

    def line_count(self) -> int:
        return self._lines
