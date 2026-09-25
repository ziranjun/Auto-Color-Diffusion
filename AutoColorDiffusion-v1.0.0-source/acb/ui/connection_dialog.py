# -*- coding: utf-8 -*-
"""新建连接对话框。

【为什么需要它 —— 这是设计反转的落点】
旧设计让用户在「服务商」下拉里选一家厂商，然后用**那家的** base_url 和
**那家的**密钥槽位。这对"我只用 DeepSeek 官方端点"是够用的，但现实里绝大多数
用户用的不是官方端点：

    SiliconFlow、one-api / new-api 中转、本地 Ollama / vLLM、公司内网网关 ——
    它们全都走 OpenAI 兼容格式、key 全是 sk- 开头，而且**一把 key 能调十几家的模型**。
    让中转站用户"选一家厂商"，他一选就错：base_url 是错的，密钥还被存进了
    别人家的槽位。

所以主从关系反过来：**base_url 是事实来源，厂商名只是从它推导出来的显示标签**。
这个对话框问的第一件事就是"你的端点在哪"，预设（12 家官方端点）只是省你打字的模板。

对话框只负责**收集输入**，不写盘、不碰密钥链 —— 那些在
`MainWindow._create_connection()` 里做，这样自检可以绕开模态框直接测业务逻辑
（`QDialog.exec()` 会阻塞，在 offscreen 自检里必挂死，本项目栽过这类跟头）。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..config import PROVIDER_LABELS, ProviderSpec, detect_provider_from_key
from .widgets import SecretLineEdit

# 预设下拉里"不用预设"那一项的 data 值。
NO_PRESET = ""


class ConnectionDialog(QDialog):
    """收集「连接名 + base_url + 密钥（+ 可选预设）」三件事。"""

    def __init__(
        self,
        presets: dict[str, ProviderSpec],
        parent: Any = None,
        *,
        initial_key: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建连接")
        self.setMinimumWidth(520)
        self._presets = presets
        self._auto_base_url = ""   # 上一次由预设自动填入的地址（用于判断能否覆盖）

        layout = QVBoxLayout(self)
        intro = QLabel(
            "连接 = 端点 + 密钥 + 一个你起的名字。\n"
            "用官方端点？从「预设」里挑一家，自动填好地址。\n"
            "用中转站 / 本地网关？预设留空，自己填 base_url 即可 —— 它们都是 OpenAI 兼容接口。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItem("（不选预设，自己填地址）", NO_PRESET)
        for pid, preset in presets.items():
            self.cmb_preset.addItem(PROVIDER_LABELS.get(pid, preset.label), pid)
        self.cmb_preset.setToolTip(
            "预设只是模板：选一家会把它的官方 base_url 与能力参数填进来。\n"
            "中转站 / 本地服务请留空，然后在下面手填地址。"
        )
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("预设", self.cmb_preset)

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("例如：DeepSeek-主力 / 中转-A / 本地-Ollama")
        self.edit_name.setToolTip(
            "你自己起的名字，用来区分同一家的多把密钥。\n"
            "它同时决定密钥在凭据管理器里的账户名：conn:<这个名字>，\n"
            "所以「DeepSeek-主力」和「DeepSeek-备用」是两把互不覆盖的钥匙。"
        )
        form.addRow("连接名", self.edit_name)

        self.edit_base_url = QLineEdit()
        self.edit_base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.edit_base_url.setToolTip(
            "API 根地址（不含 /chat/completions）。\n"
            "这一项是**事实来源**：请求实际就发到这里。\n"
            "官方端点可在 config/models.yaml 里抄；中转站看它自己的文档。"
        )
        form.addRow("base_url", self.edit_base_url)

        self.edit_key = SecretLineEdit()
        self.edit_key.setPlaceholderText("sk-…（可留空，稍后在主界面填）")
        self.edit_key.editingFinished.connect(self._on_key_edited)
        form.addRow("密钥", self.edit_key)

        layout.addLayout(form)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #B00020;")
        layout.addWidget(self.lbl_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons

        if initial_key:
            self.edit_key.setText(initial_key)
            self._on_key_edited()

    # --- 交互 -------------------------------------------------------------

    def _on_preset_changed(self) -> None:
        """选预设 → 填地址；只有"上一次自动填的"才能被覆盖，不冲掉用户手改的值。"""
        pid = self.cmb_preset.currentData()
        current = self.edit_base_url.text().strip()
        if current and current != self._auto_base_url:
            return                     # 用户自己填过，别动它
        preset = self._presets.get(str(pid)) if pid else None
        if preset is None:
            self._auto_base_url = ""
            return
        self.edit_base_url.setText(preset.base_url)
        self._auto_base_url = preset.base_url
        if not self.edit_name.text().strip():
            self.edit_name.setText(PROVIDER_LABELS.get(str(pid), preset.label))

    def _on_key_edited(self) -> None:
        """填了形态确定的密钥 → 自动挑好预设（认不出来就什么都不做，不猜）。"""
        key = self.edit_key.text().strip()
        if not key or self.cmb_preset.currentData() != NO_PRESET:
            return
        pid = detect_provider_from_key(key)
        if pid is None or pid not in self._presets:
            # 认不出属于哪家时**不猜**：中转站的 key 就是 sk- 开头，
            # 猜错会把 base_url 填成别人家的官方端点（这正是旧设计的错法）。
            return
        index = self.cmb_preset.findData(pid)
        if index >= 0:
            self.cmb_preset.setCurrentIndex(index)
            self._auto_base_url = ""      # 允许它把地址填上
            self._on_preset_changed()

    # --- 结果 -------------------------------------------------------------

    def validation_error(self) -> str:
        """返回第一条错误信息（空串 = 通过）。拆出来是为了让自检不必弹窗。"""
        if not self.edit_name.text().strip():
            return "请填连接名（用来区分同一家的多把密钥）。"
        url = self.edit_base_url.text().strip()
        if not url:
            return "请填 base_url —— 请求实际就发到这里，它是这一页最重要的一项。"
        if not url.lower().startswith(("http://", "https://")):
            return "base_url 必须以 http:// 或 https:// 开头。"
        if "::" in self.edit_name.text():
            return "连接名不能包含「::」（模型键用 :: 分隔连接与模型）。"
        return ""

    def accept(self) -> None:  # noqa: D102 - 覆写
        problem = self.validation_error()
        if problem:
            self.lbl_hint.setText(problem)
            return
        self.lbl_hint.clear()
        super().accept()

    def values(self) -> dict[str, str]:
        """收集结果（供 `MainWindow._create_connection()` 使用）。"""
        pid = str(self.cmb_preset.currentData() or "")
        return {
            "name": self.edit_name.text().strip(),
            "base_url": self.edit_base_url.text().strip(),
            "api_key": self.edit_key.text().strip(),
            "preset": pid,
        }
