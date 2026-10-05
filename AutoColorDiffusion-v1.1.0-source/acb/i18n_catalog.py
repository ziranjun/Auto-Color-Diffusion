# -*- coding: utf-8 -*-
"""英文界面文案表：中文原文 → English。

规则（改这张表之前先读一遍）
--------------------------
1. **键 = 代码里出现的中文原文**，必须逐字符一致（含全角标点、省略号「…」、
   破折号「——」）。键写错的表现是"英文模式下这句还是中文"，
   `tools/i18n_scan.py` 会把缺失的键直接列出来。
2. **只收"给人看的"文案**：界面控件、日志消息、弹窗、CLI 输出、显示名。
   下列内容**不进这张表**：
     · AI 提示词与风格规则文本（发给模型的，翻译会改变行为）；
     · 参与判断比较的字符串（风格名、状态值、正则片段、`ACB_*` 标记）；
     · 文件名/目录名（`styles/AI自主决策.json` 这类是数据，不是文案）。
3. **带占位符的模板**用 `{0}` / `{name}` 形式（配 `i18n.TF()` 使用）。
   英文模板里的占位符集合必须与中文一致，否则 `TF().format()` 会抛错。
4. 英文文案不追求"直译"，追求"英文用户看得懂、且不比中文长太多"
   （太长会撞上底栏那种按实测宽度排版的区域）。
"""

from __future__ import annotations

from typing import Final

from .i18n_catalog_cli import CLI
from .i18n_catalog_msg import MSG
from .i18n_catalog_ui import UI

# 按区域分组维护：新增文案时放进对应分组，便于评审与查重。
#   · i18n_catalog_ui.py  —— 界面控件、弹窗、菜单、连接对话框、日志面板、工作线程
#   · i18n_catalog_msg.py —— 日志与管线消息（AI / 管线 / RAW / XMP / PS / 清理）
#   · i18n_catalog_cli.py —— 配置文件报错、密钥链、CLI 输出
EN: Final[dict[str, str]] = {
    **UI,
    **MSG,
    **CLI,
}
