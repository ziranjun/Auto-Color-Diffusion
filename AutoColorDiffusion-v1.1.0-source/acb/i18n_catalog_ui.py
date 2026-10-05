# -*- coding: utf-8 -*-
"""英文文案表 · 界面层（主窗口 / 控件 / 对话框 / 日志面板 / 工作线程 / 图标）。

键 = 代码里的中文原文（逐字符一致）；带占位符的用 `{0}` / `%s` 形式。
规则见 `acb/i18n_catalog.py`。
"""

from __future__ import annotations

UI: dict[str, str] = {
    # --- 主窗口：容器与标签 ------------------------------------------------
    "清空": "Clear",
    "窗口标题": "Auto Color Diffusion — AI Batch Grading Assistant  v{0}",
    "Auto Color Diffusion —— AI 批量调色辅助工具  v{0}": (
        "Auto Color Diffusion — AI Batch Grading Assistant  v{0}"
    ),
    # 内置风格名与服务商显示名：**只在显示边界**翻（身份名/文件名不变，见 i18n.T_display）。
    "AI自主决策": "AI Autopilot",
    # 质量说明的定性描述（滑条旁那一行 + 日志 + CLI 提示共用）
    # ⚠ 英文刻意用**单词**：底栏那一行在 1280 窗口下只有 ~736px，
    #    "Extreme compression / Medium quality" 这类长句会把滑块说明挤到被裁。
    #    完整句子在悬停提示与日志里（describe_ps_quality 的非 compact 版）。
    "极限压缩": "Extreme",
    "低质量": "Low",
    "中等质量": "Medium",
    "良好质量": "Good",
    "高质量": "High",
    "最高质量（Photoshop 上限）": "Top",
    "⚠ 极低画质": "⚠ Very low quality",
    "通义千问": "Qwen (Alibaba)",
    "智谱 GLM": "Zhipu GLM",
    "豆包（火山方舟）": "Doubao (Volcano Ark)",
    "百度千帆": "Baidu Qianfan",
    "Auto Color Diffusion v%s 启动（Python %s）": "Auto Color Diffusion v%s started (Python %s)",
    "日志目录：%s": "Log folder: %s",
    "界面约定：缩略图长边 %d px、q%d（仅供 AI 判断，不参与出片）；默认输出质量 %s": (
        "UI convention: thumbnails %d px long edge, q%d (for AI review only, not used for "
        "output); default output quality %s"
    ),
    "添加照片": "Add Photos",
    "添加文件夹": "Add Folder",
    "添加训练样本": "Add Training Samples",
    "移除选中": "Remove Selected",
    "把列表里选中的照片移出待处理队列（按住 Ctrl / Shift 可多选）。\n"
    "只改列表，**不会删除或修改磁盘上的任何文件**。": (
        "Take the selected photos out of the queue (Ctrl / Shift to multi-select).\n"
        "This only changes the list and never deletes or modifies any file on disk."
    ),
    "尚未添加文件。": "No files added yet.",
    "尚未添加文件。支持的后缀：{0}": "No files added yet. Supported extensions: {0}",
    "待处理": "Pending",
    "就绪": "Ready",
    "环境自检中…": "Checking environment…",
    "{0}　v{1}": "{0}  v{1}",
    "{0} / {1}": "{0} / {1}",
    "{0}: {1}": "{0}: {1}",
    "版本号。升级时只需改 acb/__init__.py 里的 __version__ 一处。": (
        "Version. To bump a release, edit only __version__ in acb/__init__.py."
    ),
    "输出模式": "Output Mode",
    "训练模式": "Training Mode",
    "输出": "Export",
    "文件": "File",
    "外观": "Appearance",
    "安全": "Security",
    "日志": "Log",
    "帮助": "Help",
    "打开输出目录": "Open Output Folder",
    "打开风格文件夹": "Open Styles Folder",
    "浅色模式": "Light Mode",
    "深色模式": "Dark Mode",
    "删除已保存的密钥…": "Delete Saved API Keys…",
    "打开高级设置…": "Open Advanced Settings…",
    "恢复出厂设置…": "Restore Factory Settings…",
    "重新自检": "Run Check Again",
    "查看运行日志…": "View Run Log…",
    "打开日志目录": "Open Log Folder",
    "清除失败记录": "Clear Failure Records",
    "清除缩略图缓存": "Clear Thumbnail Cache",
    "界面语言": "Interface Language",
    "界面语言已改为 {0}，重新启动后生效。": (
        "Interface language changed to {0}. It takes effect after restart."
    ),
    "立即重启": "Restart Now",
    "稍后": "Later",
    "无法自动重启，请手动重新打开程序。": (
        "Could not restart automatically. Please reopen the app manually."
    ),
    "输出格式": "Format",
    "风格": "Style",
    "想要什么效果（可与风格叠加；留空 = 按风格）": (
        "What do you want (stackable with the style; empty = follow the style)"
    ),
    "给这个风格起个名字": "Name this style",
    "同时处理几张": "Parallel",
    "最大请求数": "Max requests",
    "关闭": "Close",
    "运行日志": "Run Log",
    "确定": "OK",
    "取消": "Cancel",
    "刷新": "Refresh",
    "保存": "Save",
    "显示": "Show",
    "删除": "Delete",
    "出错": "Error",
    "完成": "Done",
    "未输入": "No input",
    "未选中": "Nothing selected",
    "无法保存": "Cannot save",
    "无法删除": "Cannot delete",
    "无法新建": "Cannot create",
    "无法新建连接": "Cannot create connection",
    "无法添加": "Cannot add",
    "删除失败": "Delete failed",
    "密钥": "API Key",
    "模型": "Model",
    "连接": "Connection",
    "高级设置": "Advanced Settings",
    "关于": "About",

    # --- 主窗口：底栏与运行按钮 --------------------------------------------
    "开始": "Start",
    "停止": "Stop",
    "续跑": "Resume",
    "只跑失败": "Failed Only",
    "跳过已完成的文件（不重复请求 API），只处理尚未完成的。\n想连已完成的也重跑一遍，请用「开始」。": (
        "Skip finished files (no duplicate API calls) and process only what is left.\n"
        "To reprocess everything, use Start."
    ),
    "只处理上次失败过、且没达到永久跳过阈值的文件。": (
        "Process only files that failed before and have not hit the permanent-skip limit."
    ),
    "全部重新处理：已完成的文件也会重新请求 API。\n"
    "想跳过上次已完成的、只补跑缺的，请用右边的「续跑」。": (
        "Reprocess everything: already finished files are requested again.\n"
        "To skip finished files and only fill the gaps, use Resume on the right."
    ),
    "勾选 = 写完 XMP 后自动调用 Photoshop 导出照片。\n"
    "取消勾选 = 只写 XMP、不导出（想稍后用 Camera Raw 自己处理时用），\n"
    "后面的输出位置 / 色彩空间 / 质量 / 格式 / 尾缀会一并变灰。": (
        "Checked = write XMP and then drive Photoshop to export the photos.\n"
        "Unchecked = write XMP only, no export (for grading later in Camera Raw yourself);\n"
        "the output folder / color space / quality / format / suffix below turn grey."
    ),
    "留空 = 导出的照片与源 RAW 放在同一个目录（默认）。\n"
    "填写 = 全部导出到该目录；点「浏览…」选目录。\n"
    "离线调试跑批也一样以此为准（那是你亲手点的目录）。": (
        "Empty = exported photos go next to each source RAW (default).\n"
        "Filled = export everything into that folder; use Browse… to pick it.\n"
        "Offline debug runs use it too (that is the folder you picked yourself)."
    ),
    "输出到 _export 子目录": "Export into an _export subfolder",
    "默认把导出的照片和 RAW 放在同一个目录，方便直接对比。\n"
    "勾上此项改为放进 <源目录>/_export/ 子目录（此时左边的输入框会被忽略）。": (
        "By default exported photos sit next to the RAW files for easy comparison.\n"
        "Check this to put them in <source folder>/_export/ instead (the folder box on the "
        "left is then ignored)."
    ),
    "输出色彩空间": "Color space",
    "导出图用的色彩空间。sRGB 兼容性最好；Adobe RGB / Display P3 色域更广。": (
        "Color space for exported images. sRGB is the most compatible; Adobe RGB / Display P3 "
        "are wider gamuts."
    ),
    "输出质量": "Quality",
    "输出文件格式。\nJPG：体积最小、最通用，画质由「输出质量」滑块决定；\n"
    "PNG：无损格式，画质滑块不参与（文件更大）。": (
        "Output file format.\nJPG: smallest and most compatible; quality comes from the "
        "Output Quality slider.\nPNG: lossless, the quality slider does not apply (larger files)."
    ),
    "{0} 是无损格式，画质设置对它没有影响（文件体积由压缩方式决定）。": (
        "{0} is a lossless format, so the quality setting has no effect "
        "(file size depends on the compression method)."
    ),
    "留空 = -1": "Empty = -1",
    "尾缀 {0}": "Suffix {0}",
    "导出格式：%s": "Export format: %s",
    "导出格式：%s%s": "Export format: %s%s",
    "Photoshop 的 JPEG 质量刻度，范围 {0}–{1}；数值越大画质越高、文件越大。{2} 是 Photoshop 的上限。": (
        "Photoshop's JPEG quality scale, {0}–{1}; higher means better quality and larger files. "
        "{2} is Photoshop's maximum."
    ),

    # --- 主窗口：风格 / 模型 / 密钥 ----------------------------------------
    "模仿哪种调色风格；不选则用内置的保守默认策略。": (
        "Copy which grading style; when nothing is selected the built-in conservative default "
        "policy is used."
    ),
    "（不选，使用内置默认策略）": "(None — use the built-in default policy)",
    "{0} = 已置顶，排在列表最前面。\n在列表里右键任意一条，可以置顶/取消置顶或删除。": (
        "{0} = pinned, shown first in the list.\nRight-click any entry in the list to pin, "
        "unpin or delete it."
    ),
    "「不选，使用内置默认策略」这一项不能置顶。": (
        "The “(None — use the built-in default policy)” entry cannot be pinned."
    ),
    "请先在下拉里选一个要删除的风格。": "Select a style to delete first.",
    "这是「不选」占位项，不能置顶或删除": (
        "This is the “None” placeholder; it cannot be pinned or deleted."
    ),
    "这是程序自带的风格": "This is a built-in style",
    "「{0}」是程序内置的风格（不是训练产物），删掉后下次启动会被自动恢复，所以这里不提供删除。\n\n"
    "它的用途：让模型按每张照片本身决定调整方向，适合想先看看「没有任何个人偏好时模型会怎么调」的场景。\n"
    "想有自己的风格，用训练模式产出一份即可（那份可以删）。": (
        "“{0}” is a built-in style (not a training result). Deleting it would only bring it back "
        "on the next start, so deletion is not offered here.\n\n"
        "Its purpose: let the model decide the direction for each photo on its own — useful for "
        "seeing what the model does when you have no personal preference yet.\n"
        "Want your own style? Produce one with Training Mode (that one can be deleted)."
    ),
    "「{0}」是程序自带的内置基准风格（调试用）：删掉后下次启动会被自动恢复，所以这里不提供删除。\n\n"
    "它只在勾选「离线调试」时可选；日常使用请选「（不选，使用内置默认策略）」，或者训练一个自己的风格。": (
        "“{0}” is the built-in baseline style (for debugging): deleting it would only bring it "
        "back on the next start, so deletion is not offered here.\n\n"
        "It is selectable only while Offline debug is checked; for everyday use pick "
        "“(None — use the built-in default policy)”, or train a style of your own."
    ),
    "内置的中性基准风格（调试用），只在勾选「离线调试」时可选。\n"
    "日常使用请选「（不选，使用内置默认策略）」——效果一样，而且不会被误当成训练出来的风格。": (
        "The built-in neutral baseline style (for debugging); selectable only while Offline debug "
        "is checked.\nFor everyday use pick “(None — use the built-in default policy)” — same "
        "result, and it will not be mistaken for a trained style."
    ),
    "程序内置的「AI自主决策」：不套用任何个人风格，由模型按每张照片本身决定调整方向与幅度。\n"
    "它的\"不限制\"只针对**审美方向**：字段白名单、取值范围、机器字段与相机配置的禁令照旧生效"
    "（越界会被本地校验拒收）。\n这是内置风格，删不掉（删了下次启动也会补回来）。": (
        "Built-in “AI Autopilot”: no personal style is applied; the model decides direction and "
        "strength for each photo on its own.\nIts “no limits” applies only to the aesthetic "
        "direction: the field whitelist, value ranges and the bans on machine fields and "
        "camera profiles still apply (out-of-range values are rejected by local validation).\n"
        "This is a built-in style and cannot be deleted (it would come back on the next start)."
    ),
    "已加载 %d 个风格文件（目录：%s）。": "Loaded %d style files (folder: %s).",
    "风格「%s」%s。": "Style “%s” %s.",
    "确认删除风格": "Confirm style deletion",
    "永久删除这个风格文件？\n\n{0}\n\n此操作不可撤销（要恢复只能重新训练）。": (
        "Permanently delete this style file?\n\n{0}\n\nThis cannot be undone "
        "(the only way back is to train it again)."
    ),
    "已删除风格文件：%s": "Deleted style file: %s",
    "找不到文件": "File not found",
    "没有找到风格「{0}」对应的文件，可能已被手动删除。": (
        "No file found for style “{0}”; it may have been deleted manually."
    ),
    "无法删除文件：\n{0}": "Cannot delete the file:\n{0}",
    "未选择风格": "No style selected",

    # --- 主窗口：连接 / 模型 / 密钥 ----------------------------------------
    "连接 = 端点（base_url）+ 密钥 + 一个备注名。\n"
    "显示成「名字 @ 域名」—— 先看域名，就知道请求实际发给谁。\n"
    "内置的 12 家是**出厂连接**（用它们的官方端点与官方密钥账户名）；\n"
    "中转站 / 本地网关 / 同一家的第二把密钥，点右边的「＋」新建一条连接即可，\n"
    "各自的密钥互不覆盖。\n在列表里右键任意一条，可以删除该连接。": (
        "A connection = endpoint (base_url) + API key + a label you choose.\n"
        "Shown as “name @ domain” — the domain tells you who actually receives the request.\n"
        "The 12 built-ins are factory connections (their official endpoints and official key "
        "account names).\nFor a gateway / local server / a second key for the same vendor, click "
        "“+” to create another connection; their keys never overwrite each other.\n"
        "Right-click any entry in the list to delete that connection."
    ),
    "新建连接。\n用官方端点：从「预设」里挑一家，地址自动填好。\n"
    "用中转站 / 本地 Ollama / vLLM / 内网网关：预设留空，自己填 base_url ——\n"
    "这些都是 OpenAI 兼容接口，一把 key 能调十几家的模型。": (
        "Create a connection.\nFor official endpoints: pick a preset and the address is filled "
        "in for you.\nFor gateways / local Ollama / vLLM / intranet servers: leave the preset "
        "empty and type the base_url yourself —\nthey are all OpenAI-compatible, and one key "
        "can often call a dozen vendors' models."
    ),
    "AI 模型。下拉里的每一项都是这条连接**真实接受**的模型 id。\n"
    "预设写在：{0}（程序提供，升级时自动补齐）\n你的连接写在：{1}（只由你自己增删）\n"
    "{2} = 已置顶，排在列表最前面。\n在列表里右键任意一条，可以置顶/取消置顶或从下拉移除。": (
        "AI model. Every entry in the list is a model id this connection really accepts.\n"
        "Presets live in: {0} (shipped with the app, kept up to date on upgrade)\n"
        "Your connections live in: {1} (only you add or remove them)\n"
        "{2} = pinned, shown first.\nRight-click any entry to pin, unpin or remove it from the "
        "dropdown."
    ),
    "手动添加模型 id。\n适用于：服务商没有 /models 接口，或想临时用列表外的模型。\n"
    "填写的 id 会原样传给 API，并记进 discovered_models.json（下次启动还在）。": (
        "Add a model id manually.\nUse it when the provider has no /models endpoint, or when you "
        "want a model that is not in the list.\nThe id is passed to the API as-is and stored in "
        "discovered_models.json (it stays after a restart)."
    ),
    "给连接「{0}」填一个模型 id：": "Enter a model id for connection “{0}”:",
    "（会原样传给 API；填错会得到 404 之类的报错）": (
        "(passed to the API as-is; a typo usually comes back as a 404-style error)"
    ),
    "已添加模型「{0}」并选中。": "Added and selected model “{0}”.",
    "模型「%s」%s。": "Model “%s” %s.",
    "确认移除模型": "Confirm model removal",
    "从下拉列表里移除「{0}」？\n\n• models.yaml **不会被修改**，随时可以用「高级设置 → 恢复出厂设置」找回；\n"
    "• 已保存的 API 密钥也会保留。": (
        "Remove “{0}” from the dropdown?\n\n• models.yaml is not modified; you can always bring "
        "it back with Advanced Settings → Restore Factory Settings;\n• saved API keys are kept."
    ),
    "模型「%s」已从下拉中移除（配置未改动，可恢复）。": (
        "Model “%s” removed from the dropdown (config unchanged, it can be restored)."
    ),
    "已经移除过了": "Already removed",
    "模型「{0}」已在隐藏列表里。": "Model “{0}” is already in the hidden list.",
    "已恢复 %d 个被隐藏的模型。": "Restored %d hidden models.",
    "已按你的设置隐藏 %d 个模型（高级设置 → 恢复出厂设置 可找回）。": (
        "Hid %d models as you asked (Advanced Settings → Restore Factory Settings brings them back)."
    ),
    "未选择模型": "No model selected",
    "请先在下拉里选一个模型。": "Select a model in the dropdown first.",
    "尚未加载模型配置，请先检查 config/models.yaml。": (
        "Model configuration is not loaded yet; check config/models.yaml first."
    ),
    "尚未加载模型配置。": "Model configuration is not loaded yet.",
    "未加载模型配置": "Model configuration not loaded",
    "还没有可用模型：在「连接」里选一条、填上密钥保存，程序会自动拉取它能调用的模型；"
    "也可以点右边的「＋」新建连接。": (
        "No models available yet: pick a connection, enter and save its API key, and the app will "
        "fetch the models it can call; you can also click “+” to create a connection."
    ),
    "模型列表拉取线程未在 8 秒内结束，关闭时不再等待。": (
        "The model-list fetch thread did not finish within 8 seconds; not waiting any longer on close."
    ),
    "拉取模型列表失败：%s": "Fetching the model list failed: %s",
    "拉取模型列表失败（{0}）。已用配置里的兜底模型；若服务商没有 /models 接口，"
    "点「模型」右侧的「＋」手动添加。": (
        "Fetching the model list failed ({0}). Falling back to the models in the config; if the "
        "provider has no /models endpoint, click “+” next to Model to add one manually."
    ),
    "已拉取连接「{0}」可调用的模型（{1} 个），下拉已更新。": (
        "Fetched the models connection “{0}” can call ({1} of them); the dropdown was updated."
    ),
    "应用拉取到的模型列表失败：%s": "Applying the fetched model list failed: %s",
    "模型列表已拉到，但应用失败：{0}": "The model list was fetched but could not be applied: {0}",
    "回退兜底模型失败：%s": "Falling back to the configured models failed: %s",
    "拉取模型列表后自动选中「%s」（你还没选过这条连接的模型，或原来选中的已不在列表里）。"
    "想改回自己选：在下拉里点一下即可。": (
        "Auto-selected “%s” after fetching the model list (you had not picked a model for this "
        "connection yet, or the previous one is gone). To choose your own, just click an entry."
    ),
    "在此粘贴 API 密钥（不会写入任何文件，仅存入系统密钥链）": (
        "Paste the API key here (never written to any file; stored only in the system keychain)"
    ),
    "已保存密钥：{0}（留空表示继续使用）": "Saved key: {0} (leave empty to keep using it)",
    "把输入的密钥保存到系统密钥链（Windows 凭据管理器）。\n"
    "密钥绝不写入任何配置文件；程序只能掩码显示它，你自己也无法从界面读出明文。": (
        "Save the key into the system keychain (Windows Credential Manager).\n"
        "Keys are never written to any config file; the app can only show a masked form, and the "
        "plaintext cannot be read back from the UI either."
    ),
    "删除当前连接已保存的密钥（会先弹确认框）。\n"
    "删除的是系统密钥链里该账户的条目 + 本次会话内存里的值；\n"
    "⚠ 删除**不影响**你在系统里设置的同名环境变量 —— 若存在，程序会继续读它，\n"
    "  此时提示里会明确告诉你「要彻底停用请去删那个环境变量」。": (
        "Delete the saved key of the current connection (a confirmation dialog comes first).\n"
        "This removes the entry in the system keychain plus the value in this session's memory;\n"
        "⚠ it does not affect an environment variable with the same name — if one exists the app "
        "keeps reading it,\n  and the app will tell you to delete that variable to disable it for good."
    ),
    "确认删除密钥": "Confirm key deletion",
    "选择要删除密钥的连接（每一把都会先弹确认框）：": (
        "Pick the connection whose key to delete (each one asks for confirmation first):"
    ),
    "没有可删除的密钥": "No saved keys to delete",
    "连接「{0}」（账户 {1}）目前没有已保存的密钥。": (
        "Connection “{0}” (account {1}) has no saved key."
    ),
    "已取消：没有删除任何密钥。": "Cancelled: no key was deleted.",
    "没有已保存的密钥": "No saved keys",
    "当前没有任何连接保存了密钥，无需删除。": (
        "No connection has a saved key, so there is nothing to delete."
    ),
    "未选择连接": "No connection selected",
    "请先在「连接」下拉里选一条，再删除它的密钥。": (
        "Select a connection in the dropdown before deleting its key."
    ),
    "请先在下拉里选一条连接。": "Select a connection in the dropdown first.",
    "没有找到连接「{0}」。": "Connection “{0}” was not found.",
    "无法删除": "Cannot delete",
    "至少保留一条连接": "Keep at least one connection",
    "这是最后一条连接了。想换端点的话，直接新建一条新的即可。": (
        "This is the last connection. To switch endpoints, just create a new one."
    ),
    "确认删除连接": "Confirm connection deletion",
    "删除连接「{0}」（{1}）？\n\n• 只删这一条连接，其它连接不变；\n"
    "• 已经保存在系统凭据管理器里的密钥**不会**被删除；\n"
    "• 想回到出厂的连接列表（并一并重置密钥/失败记录/缓存/界面偏好）？"
    "用菜单「高级设置 → 恢复出厂设置」。": (
        "Delete connection “{0}” ({1})?\n\n• Only this connection is removed; others stay;\n"
        "• Keys already stored in the system credential manager are not deleted;\n"
        "• To go back to the factory connection list (and reset keys / failure records / cache / "
        "UI preferences), use Advanced Settings → Restore Factory Settings."
    ),
    "已删除连接「{0}」。": "Deleted connection “{0}”.",
    "删除这条连接…": "Delete this connection…",
    "无法写入连接文件": "Cannot write the connections file",
    "连接名与 base_url 都必须填写。": "Both the connection name and base_url are required.",
    "信息不全": "Incomplete information",
    "名字重复": "Duplicate name",
    "已经有叫「{0}」的连接了。换一个名字 ——\n同名会覆盖掉原来那条连接的设置。": (
        "A connection named “{0}” already exists. Pick another name — reusing a name overwrites "
        "the existing connection's settings."
    ),
    "已新建连接「{0}」（{1}，账户名 {2}）。正在拉取它能调用的模型……": (
        "Created connection “{0}” ({1}, account {2}). Fetching the models it can call…"
    ),
    "已新建连接「{0}」（{1}）。填上密钥保存后即可拉取模型列表。": (
        "Created connection “{0}” ({1}). Save a key to fetch its model list."
    ),
    "连接「%s」密钥已保存到 %s（账户名 %s）%s": (
        "Key for connection “%s” saved to %s (account %s)%s"
    ),
    "连接「%s」密钥已保存，模型切换到「%s」。": (
        "Key for connection “%s” saved; switched to model “%s”."
    ),
    "连接「%s」的密钥已删除（账户 %s）。": "Deleted the key for connection “%s” (account %s).",
    "已新建连接「%s」→ %s（账户名 %s）": "Created connection “%s” → %s (account %s)",
    "新建连接时清理了粘贴杂质：%s": "Cleaned pasted clutter while creating the connection: %s",
    "保存密钥时清理了粘贴杂质（%s）。": "Cleaned pasted clutter while saving the key (%s).",
    "保存密钥前清理了粘贴杂质：%s": "Cleaned pasted clutter before saving the key: %s",
    "密钥看起来不对": "The key does not look right",
    "密钥无法保存": "Cannot save the key",
    "密钥已保存到系统密钥链（%s = %s）。": "Key saved to the system keychain (%s = %s).",
    "密钥未能写入系统密钥链，仅在本次会话内存中保存（%s = %s）。": (
        "The key could not be written to the system keychain; it is kept in this session's memory "
        "only (%s = %s)."
    ),
    "未找到密钥（账户名 {0}）。请填写后保存。{1}": (
        "No key found (account {0}). Enter one and save. {1}"
    ),
    "模型「{0}」需要密钥（账户名 {1}）。\n密钥只会存入系统密钥链，绝不写入任何文件。": (
        "Model “{0}” needs a key (account {1}).\nThe key is stored only in the system keychain "
        "and never written to any file."
    ),
    "账户 %s 的密钥已删除，但仍由%s提供。": (
        "The key for account %s was deleted, but it is still provided by %s."
    ),
    "未选择服务商": "No provider selected",
    "无法判断这个密钥属于哪家服务商。\n请在「服务商」下拉里选择一家，再保存。": (
        "Could not tell which provider this key belongs to.\nPick one in the Provider dropdown "
        "and save again."
    ),
    "没有该服务商的模型": "No models for that provider",
    "模型配置里没有服务商「{0}」的模型。": "The model config has no models for provider “{0}”.",
    "已自动识别为「{0}」，{1}正在从 {2} 拉取可调用的模型……": (
        "Detected as “{0}”; {1}fetching the models it can call from {2}…"
    ),
    "连接「{0}」：{1}正在从 {2} 拉取可调用的模型……": (
        "Connection “{0}”: {1}fetching the models it can call from {2}…"
    ),
    "Kimi 限速已改为 %s（并发 %d / 每分钟 %d 次），写入 %s。": (
        "Kimi rate limit set to %s (concurrency %d / %d per minute), written to %s."
    ),
    "Kimi 档位：当前按 %s 限速（并发 %s / 每分钟 %s 次）。": (
        "Kimi tier: currently rate-limited as %s (concurrency %s / %s per minute)."
    ),
    "写入 Kimi 限速失败：%s": "Failed to write the Kimi rate limit: %s",
    "写入限速失败：{0}": "Failed to write the rate limit: {0}",
    "忽略了未知的提示链接：%s": "Ignored an unknown hint link: %s",

    # --- 主窗口：高级设置 --------------------------------------------------
    "以下选项都有安全默认值，日常使用无需改动。": (
        "All options below have safe defaults; no need to change them for everyday use."
    ),
    "性能": "Performance",
    "默认 {0}。带图请求每张要上万 token：并发越高越容易撞上限流或超时"
    "（qwen / GLM / Kimi 这类限制严、响应慢的模型尤其明显），表现为「跑完有几张红」而不是更快。\n"
    "程序遇到限流或超时也会**自动降一半**，但那是事后补救；要快可以自己调大，要稳就保持默认或调到 1。": (
        "Default {0}. Requests with images cost tens of thousands of tokens each: the higher the "
        "concurrency, the easier it is to hit rate limits or timeouts (especially on strict, slow "
        "models such as qwen / GLM / Kimi) — you just get more red rows, not speed.\n"
        "The app also halves concurrency automatically after a rate limit or timeout, but that is "
        "a repair, not a plan; raise it for speed, keep the default (or use 1) for stability."
    ),
    "0 表示自动：文件数 × {0}（含重试）。超出上限会停止并告警，防止失控耗费。": (
        "0 = automatic: file count × {0} (including retries). Exceeding the limit stops the run "
        "with a warning to prevent runaway cost."
    ),
    "使用缩略图缓存（命中则跳过提取与压缩）": (
        "Use the thumbnail cache (a hit skips extraction and compression)"
    ),
    "高保真模式：跳过嵌入式预览，直接用 rawpy 解码（慢，但更接近 ACR 的中性观感；"
    "TIFF/HEIF 本来就直解，此开关对它们无影响）": (
        "High-fidelity mode: skip the embedded preview and decode with rawpy (slower, but closer "
        "to ACR's neutral rendering; TIFF/HEIF are always decoded directly, so this switch does "
        "not affect them)"
    ),
    "行为": "Behavior",
    "DNG 也生成旁侧 .xmp 文件（默认写入文件内部；勾选此项可能被 ACR 忽略）": (
        "Also write a sidecar .xmp for DNG (default is writing inside the file; ACR may ignore "
        "the sidecar)"
    ),
    "离线调试：不调用 AI、只处理前 {0} 张，用写死的假结果验证链路": (
        "Offline debug: no AI calls, only the first {0} files, hard-coded fake results to verify "
        "the pipeline"
    ),
    "调试用：勾选后不会发出任何网络请求，也不需要 API 密钥或连接。\n"
    "只处理前 {0} 张，出片落到 _export_dryrun（与正式产物分开）。\n"
    "仍然会写 XMP，但里面的参数全是假数据，切勿用于交付。\n"
    "日志与每条结果的说明里都会标注「[离线调试]」。\n"
    "勾选期间，「连接 / 模型 / 密钥」整组会停用 —— 它们对调试结果没有任何影响。": (
        "For debugging: no network requests at all, and no API key or connection needed.\n"
        "Only the first {0} files are processed; output goes to _export_dryrun (kept apart from "
        "real output).\nXMP is still written, but every value in it is fake — never deliver it.\n"
        "Log lines and per-item notes are marked “[offline debug]”.\n"
        "While checked, the whole Connection / Model / Key group is disabled — it has no effect "
        "on debug results."
    ),
    "离线调试：不调用 AI、不需要连接与密钥，所以「连接 / 模型 / 密钥」已停用。": (
        "Offline debug: no AI calls, so no connection or key is needed — the Connection / Model / "
        "Key group is disabled."
    ),
    "本次只处理前 {0} 张，出片落到 _export_dryrun；XMP 照写，但内容全是假数据，别用于交付。": (
        "This run processes only the first {0} files; output goes to _export_dryrun. XMP is still "
        "written but its values are fake — do not deliver it."
    ),

    # --- 主窗口：脱机/跑批状态与对话框 --------------------------------------
    "任务进行中": "Task running",
    "已有任务正在运行，请先停止或等待完成。": (
        "A task is already running; stop it or wait for it to finish."
    ),
    "请先停止当前任务，再恢复出厂设置。": (
        "Stop the running task before restoring factory settings."
    ),
    "任务出错": "Task error",
    "未添加文件": "No files added",
    "请先点击「添加照片」或「添加文件夹」。": "Click Add Photos or Add Folder first.",
    "需要风格命名": "Style name required",
    "训练模式需要给风格起个名字（用于保存 style_profile.json）。": (
        "Training mode needs a name for the style (used for style_profile.json)."
    ),
    "无法开始": "Cannot start",
    "正在停止…": "Stopping…",
    "本次共有 %d 条告警，请查看上方日志或日志文件。": (
        "There were %d warnings in this run; check the log above or the log file."
    ),
    "失败清单（最多显示 5 条）：%s。同一文件累计失败 %d 次将永久跳过（**任何模式下都不会再处理**），"
    "可用「清除失败记录」重置。": (
        "Failure list (showing at most 5): %s. A file that fails %d times is skipped permanently "
        "(it will not be processed in any mode); use Clear Failure Records to reset this."
    ),
    "断点状态：已完成 %d 个，失败 %d 个（其中永久跳过 %d 个）。": (
        "Checkpoint state: %d finished, %d failed (%d of them skipped permanently)."
    ),
    "已添加 %d 个路径（当前共 %d 个）。": "Added %d paths (%d in total).",
    "已从列表移除 %d 个文件（当前共 %d 个；磁盘上的文件未做任何改动）。": (
        "Removed %d files from the list (%d in total; nothing on disk was touched)."
    ),
    "已清空文件列表。": "Cleared the file list.",
    "共 {0} 个路径。支持的后缀：{1}（大小写不敏感，文件夹会递归扫描）": (
        "{0} paths. Supported extensions: {1} (case-insensitive; folders are scanned recursively)"
    ),
    "请先在列表里选中要移除的照片（按住 Ctrl 或 Shift 可以多选）。": (
        "Select the photos to remove first (Ctrl or Shift to multi-select)."
    ),
    "样本不足": "Not enough samples",
    "样本不足，结果可能不稳定。": "Not enough samples; results may be unstable.",
    "建议至少提供 {0} 个样本后重新训练。": "Provide at least {0} samples and train again.",
    "确认清除": "Confirm clearing",
    "将清除 {0} 条失败记录（其中 {1} 条已永久跳过）。\n清除后这些文件会重新进入待处理队列。是否继续？": (
        "This clears {0} failure records ({1} of them were skipped permanently).\nThose files go "
        "back into the pending queue. Continue?"
    ),
    "已清除 %d 条失败记录（用户操作：日志 → 清除失败记录）。": (
        "Cleared %d failure records (user action: Log → Clear Failure Records)."
    ),
    "无需清除": "Nothing to clear",
    "当前没有失败记录。": "There are no failure records.",
    "缩略图缓存为空。": "The thumbnail cache is empty.",
    "确认清除缩略图缓存": "Confirm clearing the thumbnail cache",
    "将删除 {0} 个缓存文件（约 {1} MB）。\n清除后下次运行需要重新提取并压缩缩略图（更慢，但结果不变）。": (
        "This deletes {0} cache files (about {1} MB).\nThe next run must extract and compress "
        "thumbnails again (slower, same results)."
    ),
    "已清除 %d 条失败记录（用户操作：日志 → 清除失败记录）。": (
        "Cleared %d failure records (user action: Log → Clear Failure Records)."
    ),
    "恢复出厂设置": "Restore Factory Settings",
    "确认恢复到出厂设置？\n\n连接、隐藏模型、界面偏好、失败记录与缩略图缓存都会被重置；\n"
    "已保存的 API 密钥会被删除且无法恢复（之后需要重新粘贴）。\n此操作不能撤销。": (
        "Restore factory settings?\n\nConnections, hidden models, UI preferences, failure records "
        "and the thumbnail cache are reset;\nsaved API keys are deleted and cannot be recovered "
        "(you will have to paste them again).\nThis cannot be undone."
    ),
    "继续（还要再确认一次）": "Continue (one more confirmation)",
    "再次确认": "Confirm again",
    "确认重置": "Confirm reset",
    "无需恢复": "Nothing to restore",
    "当前已经全是出厂设置，没有可恢复的内容。": (
        "Everything is already at factory defaults; nothing to restore."
    ),
    "用户在恢复出厂设置的警示框里选择了取消，未做任何改动。": (
        "The user cancelled the factory-settings warning dialog; nothing was changed."
    ),
    "用户在恢复出厂设置的二次确认里选择了取消，未做任何改动。": (
        "The user cancelled the second factory-settings confirmation; nothing was changed."
    ),
    "已恢复出厂设置（密钥已删除，需要重新粘贴；风格文件保留）。": (
        "Factory settings restored (keys were deleted and must be pasted again; style files were kept)."
    ),
    "部分未完成": "Partly finished",
    "确认极低画质": "Confirm very low quality",
    "仍然使用（继续）": "Use it anyway (continue)",
    "返回修改质量": "Go back and change quality",
    "用户在极低画质确认框里选择返回，本次导出已取消。": (
        "The user chose to go back in the very-low-quality dialog; this export was cancelled."
    ),
    "用户确认按极低画质（PS 刻度 %d）继续导出。": (
        "The user confirmed exporting at very low quality (PS scale %d)."
    ),
    "开始%s%s": "Start%s%s",
    "已切换到%s。": "Switched to %s.",

    # --- 主窗口：异常护栏与收尾 --------------------------------------------
    "发生内部错误（程序未退出）": "An internal error occurred (the app keeps running)",
    "界面回调发生未捕获异常（已拦截，程序继续运行）：\n%s": (
        "An unhandled exception occurred in a UI callback (intercepted; the app keeps running):\n%s"
    ),
    "界面回调里出现未预期的错误：\n\n{0}: {1}\n\n程序已拦截该错误并继续运行，你可以继续操作。\n"
    "完整调用栈已写入日志文件（界面上的「打开日志目录」按钮可直达）。": (
        "Unexpected error in a UI callback:\n\n{0}: {1}\n\nThe app intercepted it and keeps "
        "running, so you can continue.\nThe full stack trace is in the log file (use Open Log "
        "Folder to get there)."
    ),
    "自动重启失败：%s": "Automatic restart failed: %s",
    "退出清理出错（已忽略，不影响关闭）：%s": "Exit cleanup failed (ignored; closing is unaffected): %s",
    "退出清理：%s": "Exit cleanup: %s",
    "无法打开目录 %s：%s": "Cannot open the folder %s: %s",
    "已补齐 ICC 资源：%s": "Added missing ICC resources: %s",
    "色彩管理资源不齐全：\n%s": "Color-management resources are incomplete:\n%s",
    "色彩管理资源齐全：\n%s": "Color-management resources are complete:\n%s",
    "检查 ICC 资源失败（不影响运行）：%s": "Checking ICC resources failed (does not affect the run): %s",
    "exiftool 可用：%s": "exiftool available: %s",
    "exiftool 不可用。%s": "exiftool unavailable. %s",
    "发现 %d 个训练样本对。": "Found %d training sample pairs.",
    "连接「%s」与其它连接共用了同一把密钥（账户 %s）——%s": (
        "Connection “%s” shares the same key with another connection (account %s) — %s"
    ),
    "打开脚本目录": "Open Script Folder",
    "打开本次运行的脚本目录（manifest.json / export_batch.jsx / run_export.bat）。\n"
    "注意：关闭程序时该目录会被自动清理，需要手动补跑请先操作。": (
        "Open this run's script folder (manifest.json / export_batch.jsx / run_export.bat).\n"
        "Note: the folder is cleaned up when the app closes, so act before that if you need to "
        "re-run manually."
    ),

    # --- 控件层（widgets.py）-----------------------------------------------
    "留空则使用默认位置": "Leave empty to use the default location",
    "「关于」图标加载失败，回退到系统默认图标": (
        "Loading the About icon failed; falling back to the system default icon"
    ),

    # --- 连接对话框（connection_dialog.py）---------------------------------
    "新建连接": "New Connection",
    "连接 = 端点 + 密钥 + 一个你起的名字。": "A connection = endpoint + key + a name you choose.",
    "用官方端点？从「预设」里挑一家，自动填好地址。": (
        "Official endpoint? Pick a provider from Preset and the address is filled in."
    ),
    "用中转站 / 本地网关？预设留空，自己填 base_url 即可 —— 它们都是 OpenAI 兼容接口。": (
        "Gateway / local server? Leave the preset empty and type the base_url — they are all "
        "OpenAI-compatible."
    ),
    "（不选预设，自己填地址）": "(No preset — type the address myself)",
    "预设只是模板：选一家会把它的官方 base_url 与能力参数填进来。\n"
    "中转站 / 本地服务请留空，然后在下面手填地址。": (
        "A preset is only a template: picking one fills in its official base_url and capability "
        "flags.\nFor gateways / local servers leave it empty and type the address below."
    ),
    "例如：DeepSeek-主力 / 中转-A / 本地-Ollama": "e.g. DeepSeek-main / gateway-A / local-Ollama",
    "你自己起的名字，用来区分同一家的多把密钥。\n它同时决定密钥在凭据管理器里的账户名：conn:<这个名字>，\n"
    "所以「DeepSeek-主力」和「DeepSeek-备用」是两把互不覆盖的钥匙。": (
        "A name you choose, to tell several keys of the same vendor apart.\nIt also decides the "
        "account name in the credential manager: conn:<this name>,\nso “DeepSeek-main” and "
        "“DeepSeek-backup” are two keys that never overwrite each other."
    ),
    "API 根地址（不含 /chat/completions）。\n这一项是**事实来源**：请求实际就发到这里。\n"
    "官方端点可在 config/models.yaml 里抄；中转站看它自己的文档。": (
        "API base URL (without /chat/completions).\nThis is the source of truth: requests really "
        "go here.\nOfficial endpoints can be copied from config/models.yaml; for gateways check "
        "their own documentation."
    ),
    "sk-…（可留空，稍后在主界面填）": "sk-… (can be left empty and filled in on the main window)",
    "例如「海边暖调人像」": "e.g. “warm seaside portrait”",

    # --- 日志面板 / 工作线程 ------------------------------------------------
    "运行日志会显示在这里（只显示 INFO 及以上级别，DEBUG 详情请查看日志文件）": (
        "The run log appears here (INFO and above; see the log file for DEBUG details)"
    ),
    "复制全部": "Copy All",
    "工作线程异常：%s": "Worker thread exception: %s",
    "拉取模型列表失败：%s": "Fetching the model list failed: %s",

    # --- 图标（icon.py）-----------------------------------------------------
    "没有找到图标文件 %s（用 tools/make_icon.py 生成），本次不加窗口图标。": (
        "Icon file %s not found (generate it with tools/make_icon.py); no window icon this time."
    ),
    "图标文件存在但 Qt 打不开：%s": "The icon file exists but Qt cannot open it: %s",
    "加载图标失败（忽略）：%s": "Loading the icon failed (ignored): %s",
    "设置 AppUserModelID 失败（忽略）：%s": "Setting AppUserModelID failed (ignored): %s",

    # --- 第二批（宽模式覆盖到的"返回给界面显示的消息 / 表头 / 状态"）---------
    "\n\n不会动的东西：\n• styles/ 里训练出来的风格文件（那是你的成果，不属于设置）\n"
    "• 你的照片、XMP 旁侧文件、models.yaml 配置\n\n"
    "重置后需要重新粘贴 API 密钥、重新选一次模型与风格。此操作不能撤销。": (
        "\n\nWhat will NOT be touched:\n• trained style files in styles/ (your work, not settings)\n"
        "• your photos, XMP sidecars and models.yaml\n\n"
        "After the reset you must paste the API keys again and pick a model and style once more. "
        "This cannot be undone."
    ),
    "  · 你的照片、训练好的风格、模型与其它设置都不受影响。": (
        "  · Your photos, trained styles, models and other settings are unaffected."
    ),
    "  · 程序立刻不再使用它（本次会话的内存副本也会一起清掉）；": (
        "  · The app stops using it immediately (the in-session copy is cleared too);"
    ),
    "  · 系统密钥链（凭据管理器）里的条目会被移除，**无法撤销**"
    "（要恢复只能重新粘贴一次密钥）；": (
        "  · The entry in the system keychain (Credential Manager) is removed and cannot be "
        "undone (recovering it means pasting the key again);"
    ),
    " [永久跳过]": " [permanently skipped]",
    "Photoshop 可用": "Photoshop available",
    "RAW 与 XMP ({0});;RAW 照片 ({1});;XMP 设置 (*.xmp *.XMP);;所有文件 (*)": (
        "RAW and XMP ({0});;RAW photos ({1});;XMP settings (*.xmp *.XMP);;All files (*)"
    ),
    "RAW 照片 ({0});;所有文件 (*)": "RAW photos ({0});;All files (*)",
    "json_object + 本地强校验": "json_object + strict local validation",
    "{0} @ {1}（账户 {2}）": "{0} @ {1} (account {2})",
    "{0} · {1}\nmodel id: {2}\n请求形状: {3}\n{4}；输出格式: {5}\n"
    "输出上限: {6} token；上下文: {7} token": (
        "{0} · {1}\nmodel id: {2}\nRequest shape: {3}\n{4}; output format: {5}\n"
        "Output limit: {6} tokens; context: {7} tokens"
    ),
    "{0}({1} 次{2})": "{0} ({1} times{2})",
    "• 失败记录：{0} 条全部清除（含「永久跳过」档案，清掉后这些文件会重新进入待处理队列）": (
        "• Failure records: all {0} cleared (including “permanently skipped” entries; those "
        "files go back into the pending queue)"
    ),
    "• 已保存的 API 密钥：{0} 条会直接删除、无法恢复，之后要重新粘贴"
    "（环境变量里的密钥程序删不掉，会单独说明）": (
        "• Saved API keys: {0} will be deleted and cannot be recovered; you will have to paste "
        "them again (keys coming from environment variables cannot be deleted by the app; this is "
        "reported separately)"
    ),
    "• 界面偏好：": "• UI preferences:",
    "• 缩略图缓存：{0} 个文件删除（下次运行重新提取，更慢但结果不变）": (
        "• Thumbnail cache: {0} files deleted (the next run extracts them again; slower, same "
        "results)"
    ),
    "• 被移除的模型：{0} 个全部放回下拉": "• Removed models: all {0} put back into the dropdown",
    "• 连接列表：删除自定义连接文件，回到出厂的那几条": (
        "• Connection list: the custom connections file is deleted; back to the factory entries"
    ),
    "⚠ 下列密钥由**同名环境变量**提供，程序无权删除：": (
        "⚠ These keys come from environment variables of the same name; the app cannot delete "
        "them:"
    ),
    "⚠ 密钥链不可用（或写入失败），密钥只在**本次会话**有效（账户名 {0}，{1}），"
    "关闭程序后需重输。": (
        "⚠ The keychain is unavailable (or the write failed), so the key is valid for this "
        "session only (account {0}, {1}) and must be re-entered after closing the app."
    ),
    "⚠ 注意：检测到同名环境变量 {0} 也在提供密钥。环境变量是你在系统里设的，程序无权修改 —— "
    "删除之后程序仍会读到它。要彻底停用，请在系统环境变量里删掉那一条。": (
        "⚠ Note: an environment variable named {0} also provides this key. Environment variables "
        "are set by you and the app may not modify them — after deletion the app still reads it. "
        "To disable it for good, delete that entry in the system environment variables."
    ),
    "　⚠ 与连接{0}用的是同一把密钥（同一个账户的额度与限流）；"
    "要给另一家单独的密钥，请用「＋」新建一条连接再填。": (
        "  ⚠ Uses the same key as connection {0} (same account quota and rate limits); for a "
        "separate key, create another connection with “+” and paste it there."
    ),
    "　⚠ 需处理：{0}（详见日志）": "  ⚠ Action needed: {0} (see the log)",
    "上次选用的连接 / 模型": "Last used connection / model",
    "上次选用的风格": "Last used style",
    "删除后：": "After deleting:",
    "删除已保存的密钥": "Delete Saved API Keys",
    "删除（仅从下拉移除，可恢复）": "Remove (dropdown only, reversible)",
    "删除（会删掉风格文件）": "Delete (removes the style file)",
    "置顶": "Pin",
    "取消置顶": "Unpin",
    "已置顶": "Pinned",
    "已取消置顶": "Unpinned",
    "处理完成": "Run finished",
    "处理结束（有失败）": "Run finished (with failures)",
    "完成，但有 {0} 张失败": "Finished, but {0} images failed",
    "密钥已保存到系统密钥链（账户名 {0}，{1}）{2}。": (
        "Key saved to the system keychain (account {0}, {1}){2}."
    ),
    "密钥链": "Keychain",
    "密钥链可用": "Keychain available",
    "导出文件名 = 原主文件名 + 尾缀。\n留空 → 默认尾缀 -1（重复导出自动变成 -2、-3）；\n"
    "填 edit → 「文件名-edit.jpg」（再导出会得到 -edit_2.jpg，不会写成 -edit-edit）。\n"
    "这里填的内容会被记住，下次打开程序自动带出。": (
        "Export file name = original base name + suffix.\nEmpty → default suffix -1 (repeated "
        "exports become -2, -3);\n“edit” → “name-edit.jpg” (a further export gives -edit_2.jpg, "
        "never -edit-edit).\nWhat you type here is remembered and restored the next time you open "
        "the app."
    ),
    "导出文件名尾缀，例如 edit → 文件名-edit.jpg。留空则用默认尾缀 -1"
    "（重复导出自动变成 -2、-3）": (
        "Export file name suffix, e.g. edit → name-edit.jpg. Empty uses the default suffix -1 "
        "(repeated exports become -2, -3)."
    ),
    "导出格式 {0} 已不再提供（TIFF 无法通过 Photoshop 脚本导出），已回退到 {1}": (
        "Export format {0} is no longer available (TIFF cannot be exported through Photoshop "
        "scripting); falling back to {1}"
    ),
    "导出格式 {0} 已按 {1} 处理": "Export format {0} was treated as {1}",
    "尚未加载模型配置，无法删除密钥。": (
        "The model configuration is not loaded, so the key cannot be deleted."
    ),
    "工作线程未正常退出，跳过清理以免干扰仍在进行的导出": (
        "A worker thread did not exit cleanly; skipping cleanup so a running export is not "
        "disturbed"
    ),
    "工作线程未能在 15 秒内退出（可能正在等待网络响应），将强制关闭。": (
        "A worker thread did not exit within 15 seconds (it may be waiting for a network "
        "response); forcing shutdown."
    ),
    "已停止": "Stopped",
    "已停止（已完成的已保留）": "Stopped (finished results kept)",
    "已停止（风格未保存）": "Stopped (style not saved)",
    "已删除连接「{0}」在密钥链与本会话内存里的密钥（账户 {1}）；但**同名的环境变量 {2} "
    "仍在提供密钥**，程序会继续用它 —— 要彻底停用请在系统环境变量里删除该变量，然后重启程序。": (
        "Deleted the key of connection “{0}” from the keychain and this session's memory "
        "(account {1}); but an environment variable named {2} still provides it and the app keeps "
        "using it — delete that variable in the system environment and restart the app to disable "
        "it for good."
    ),
    "已删除连接「{0}」的密钥（账户 {1}）。要再次使用请重新粘贴密钥并保存。": (
        "Deleted the key of connection “{0}” (account {1}). Paste it again to use it."
    ),
    "已恢复出厂设置：连接回到出厂、密钥清除 {0} 条、失败记录清除 {1} 条、"
    "缩略图缓存清除 {2} 个文件、隐藏模型放回 {3} 个、界面偏好重置 {4} 项。": (
        "Factory settings restored: connections back to factory, {0} keys deleted, {1} failure "
        "records cleared, {2} thumbnail cache files deleted, {3} hidden models restored, {4} UI "
        "preferences reset."
    ),
    "已找到密钥 {0}（来源：{1}，账户名 {2}）。如需替换，请在输入框中填入新值后点「保存」。": (
        "Found key {0} (source: {1}, account {2}). To replace it, type the new value and click "
        "Save."
    ),
    "已把限速改为 Kimi {0}（并发 {1} / 每分钟 {2} 次），写入连接配置（下次启动仍然有效）": (
        "Kimi rate limit set to {0} (concurrency {1} / {2} per minute) and written to the "
        "connection config (still valid on the next start)"
    ),
    "已收到停止请求：**不会再发送任何新请求**（包括重试与拆单重发）；"
    "已在飞的那几条会等它返回或超时，已完成的处理结果会保留。": (
        "Stop requested: no new requests will be sent (no retries, no per-image resends); "
        "requests already in flight are awaited or time out, and finished results are kept."
    ),
    "已清除缩略图缓存（{0} 个文件）。": "Thumbnail cache cleared ({0} files).",
    "当前值：{0}": "Current value: {0}",
    "当前按 Kimi <b>Tier0</b> 限速（并发 {0} / 每分钟 {1} 次）。"
    "若你已充值升级（Tier1 起：并发 {2} / 每分钟 {3} 次），"
    "<a href=\"kimi-tier1\">点此改回 Tier1</a>。": (
        "Kimi is currently rate-limited as <b>Tier0</b> (concurrency {0} / {1} per minute). "
        "If you have topped up (Tier1 and above: concurrency {2} / {3} per minute), "
        "<a href=\"kimi-tier1\">click here to switch back to Tier1</a>."
    ),
    "当前输出质量为 Photoshop 刻度 {0} / {1}（约等于 libjpeg {2}），属于极低画质。\n\n"
    "压缩块状伪影在正常观看尺寸下肉眼可见，不适合交付或二次编辑。\n\n"
    "这个取值本身是合法的，Photoshop 会照此编码。请确认你确实要用它（例如生成极小的预览图）。": (
        "The output quality is Photoshop scale {0} / {1} (about libjpeg {2}), which counts as "
        "very low.\n\nCompression blocking is visible at normal viewing size and is unsuitable "
        "for delivery or further editing.\n\nThe value itself is legal and Photoshop will encode "
        "with it — please confirm you really want it (e.g. for tiny previews)."
    ),
    "无法打开目录 {0}：{1}": "Cannot open the folder {0}: {1}",
    "无法识别的导出格式 {0}，已回退到 {1}": (
        "Unrecognised export format {0}; falling back to {1}"
    ),
    "无法识别的质量值 {0}，已回退到默认 {1}": (
        "Unrecognised quality value {0}; falling back to the default {1}"
    ),
    "无（提示词 + 本地校验）": "none (prompt + local validation)",
    "未提供 API 密钥，已取消。": "No API key was given; cancelled.",
    "未知来源": "unknown source",
    "本批提示词；与所选风格叠加，冲突以它为准；留空则按风格/自动一致性校正": (
        "Prompt for this batch; stacks with the selected style and wins on conflict; empty "
        "follows the style / automatic consistency correction"
    ),
    "本次会话内存": "this session's memory",
    "来源：{0}": "Source: {0}",
    "模型配置": "Model configuration",
    "注意：Kimi 按「累计充值额」分档 —— 未充值的账户（Tier0）只允许 "
    "<b>并发 {0} / 每分钟 {1} 次</b>，而当前按 Tier1（并发 {2} / 每分钟 {3} 次）跑，会反复 429。"
    "请注意，若权益为 Tier0，<a href=\"kimi-tier0\">请点击此处切换为 Tier0</a>。": (
        "Note: Kimi tiers depend on cumulative top-up — an account that has not topped up "
        "(Tier0) allows only <b>concurrency {0} / {1} per minute</b>, while the current setting is "
        "Tier1 (concurrency {2} / {3} per minute), which keeps hitting 429. If your entitlement "
        "is Tier0, <a href=\"kimi-tier0\">click here to switch to Tier0</a>."
    ),
    "浏览…": "Browse…",
    "演练时不写任何 XMP（完全不改动用户文件）": (
        "Dry run writes no XMP at all (user files are left completely untouched)"
    ),
    "照片": "Photos",
    "状态": "Status",
    "环境变量": "environment variable",
    "环境变量 {0}": "Environment variable {0}",
    "环境自检完成": "Environment check finished",
    "用户在界面上选择了 {0}": "The user chose {0} in the UI",
    "界面主题（浅色 / 深色）": "UI theme (light / dark)",
    "离线调试（演练）与限流": "Offline debug (dry run) and rate limits",
    "离线调试：不调用 AI、不需要密钥，直接注入写死的假分析结果"
    "（用于没有可用密钥时验证整条链路）": (
        "Offline debug: no AI calls and no key needed; hard-coded fake results are injected (for "
        "verifying the whole pipeline when no key is available)"
    ),
    "系统密钥链": "system keychain",
    "系统密钥链（凭据管理器）": "system keychain (Credential Manager)",
    "缺失": "missing",
    "置顶的模型": "Pinned models",
    "置顶的风格": "Pinned styles",
    "脚本目录已不存在（退出时已自动清理）：{0}\n"
    "需要手动补跑导出的话，请重新跑一次，并在关闭程序之前完成。": (
        "The script folder no longer exists (it was cleaned up on exit): {0}\nIf you need to run "
        "the export manually, run the batch again and finish before closing the app."
    ),
    "被移除（隐藏）的模型": "Removed (hidden) models",
    "要删除连接「{0}」的密钥吗？": "Delete the key of connection “{0}”?",
    "视觉=不支持（走 caption 降级）": "vision=not supported (falls back to caption)",
    "视觉=支持": "vision=supported",
    "训练失败（风格未保存）": "Training failed (style not saved)",
    "训练完成": "Training finished",
    "训练已停止：**不会再发送请求，风格档案未保存**（磁盘上是原来的那份）。": (
        "Training stopped: no further requests, and the style profile was not saved (the file on "
        "disk is unchanged)."
    ),
    "训练未产出风格档案：**本次训练没有成功，磁盘上的风格文件保持原样**。"
    "请查看上方日志中的错误原文（超时类失败可以直接重试一次）。": (
        "Training produced no style profile: this run did not succeed and the style file on disk "
        "is unchanged. Read the error text in the log above (timeout failures can simply be "
        "retried)."
    ),
    "该模型不支持图片输入，将走 caption 降级通道（只看本地统计量，精度低于看图判断）": (
        "This model cannot take images, so the caption fallback is used (it only sees local "
        "statistics and is less accurate than looking at the photo)"
    ),
    "详细日志：{0}": "Detailed log: {0}",
    "请先在 config/models.yaml 中配置模型（当前未加载）。": (
        "Configure a model in config/models.yaml first (none is loaded)."
    ),
    "请填 base_url —— 请求实际就发到这里，它是这一页最重要的一项。": (
        "Enter the base_url — requests really go here; it is the most important field on this page."
    ),
    "请填连接名（用来区分同一家的多把密钥）。": (
        "Enter a connection name (to tell several keys of the same vendor apart)."
    ),
    "调色助手": "Grading Assistant",
    "调色参数": "Grading settings",
    "账户名：{0}": "Account: {0}",
    "质量值 {0} 超出 0–{1}，已夹到 {2}": "Quality {0} is outside 0–{1}; clamped to {2}",
    "质量参数收到的旧档位名「{0}」已转换为 Photoshop 刻度 {1}；建议改用 0–{2} 的整数": (
        "The legacy quality name “{0}” was converted to Photoshop scale {1}; prefer an integer "
        "from 0–{2}"
    ),
    "跳过嵌入式预览，直接用 rawpy 解码（慢，但更接近 ACR 中性观感）": (
        "Skip the embedded preview and decode with rawpy (slower, but closer to ACR's neutral "
        "rendering)"
    ),
    "输出文件名尾缀": "Export file name suffix",
    "输出目录": "Output",
    "输出目录；默认与源文件同目录": "Output folder; defaults to the source folder",
    "输出设置": "Output settings",
    "这会重置下列内容：\n": "This resets the following:\n",
    "连接「{0}」（账户 {1}）目前没有已保存的密钥，无需删除。": (
        "Connection “{0}” (account {1}) has no saved key; nothing to delete."
    ),
    "连接名": "Connection name",
    "连接名不能包含「::」（模型键用 :: 分隔连接与模型）。": (
        "The connection name cannot contain “::” (model keys use :: to separate connection and "
        "model)."
    ),
    "连接文件删除失败（{0}）": "Deleting the connections file failed ({0})",
    "退出清理：{0}": "Exit cleanup: {0}",
    "选择 RAW 照片（支持：{0}）": "Choose RAW photos (supported: {0})",
    "选择文件夹": "Choose Folder",
    "选择训练样本（RAW 与其同名的 XMP 旁侧文件）": (
        "Choose training samples (RAW files with their same-name XMP sidecars)"
    ),
    "选择输出目录": "Choose Output Folder",
    "隐藏": "Hide",
    "预设": "Preset",
    "风格档案已生成：样本 {0} 个，统计字段 {1} 个，风格规则 {2} 条，排除字段 {3} 个。": (
        "Style profile generated: {0} samples, statistics for {1} fields, {2} style rules, {3} "
        "fields excluded."
    ),
    "（styles/ 里训练出来的风格文件未改动）": "(trained style files in styles/ are unchanged)",
    "（全量：已完成的文件也会重新请求 API）": "(full run: finished files are requested again)",
    "（只跑失败：其余一律跳过）": "(failed only: everything else is skipped)",
    "（密钥链不可用，本次会话仅在内存中保存，重启需重输）": (
        "(keychain unavailable: kept in memory for this session only; re-enter after restart)"
    ),
    "（没有找到）": "(not found)",
    "（点击「保存」写入系统密钥链）": "(click Save to store it in the system keychain)",
    "（约等于 libjpeg {0}）": "(about libjpeg {0})",
    "（续跑：跳过已完成的文件，不重复请求 API）": (
        "(resume: skip finished files, no duplicate API requests)"
    ),
    "（要彻底停用请去系统里删掉这些环境变量）": (
        "(to disable them for good, delete those environment variables in the system)"
    ),
    "；上次学到的限流值已作废，立即按新档位跑。": (
        "; the learned rate limit was discarded and the new tier applies immediately."
    ),
    "只处理上次失败过且未达永久跳过阈值的文件": (
        "Process only files that failed before and have not hit the permanent-skip limit"
    ),

    # --- 主窗口：成对文案 / 多行提示（机械化包装产生的模板）----------------
    "关于 {0}": "About {0}",
    "已清除 {0} 条失败记录。": "Cleared {0} failure records.",
    "将删除 {0} 个缓存文件（约 {1} MB）。\n"
    "清除后下次运行需要重新提取并压缩缩略图（更慢，但结果不变）。是否继续？": (
        "This deletes {0} cache files (about {1} MB).\nThe next run must extract and compress "
        "thumbnails again (slower, same results). Continue?"
    ),
    "已补齐 ICC 资源：{0}": "Added missing ICC resources: {0}",
    "%s（用户操作：高级设置 → 恢复出厂设置）": (
        "%s (user action: Advanced Settings → Restore Factory Settings)"
    ),
    "界面语言已改为 %s（重启后生效）": "Interface language changed to %s (takes effect after restart)",
    "当前没有可用的连接，请先在「连接」里新建一条。": (
        "No connections available; create one under Connection first."
    ),
    "输入框为空，未做任何改动。": "The input was empty; nothing was changed.",
    "模型「%s」不支持 json_schema，将用 %s。": (
        "Model “%s” does not support json_schema; using %s instead."
    ),
    "模型「%s」：%s": "Model “%s”: %s",
    "给连接「{0}」填一个模型 id：\n（会原样传给 API；填错会得到 404 之类的报错）": (
        "Enter a model id for connection “{0}”:\n(passed to the API as-is; a typo usually comes "
        "back as a 404-style error)"
    ),
    "离线调试：不调用 AI、不需要连接与密钥，所以「连接 / 模型 / 密钥」已停用。\n"
    "本次只处理前 {0} 张，出片落到 _export_dryrun；XMP 照写，但内容全是假数据，别用于交付。": (
        "Offline debug: no AI calls, so no connection or key is needed — the Connection / Model / "
        "Key group is disabled.\nThis run processes only the first {0} files and writes output "
        "into _export_dryrun; XMP is still written but its values are fake — do not deliver it."
    ),
    "模仿哪种调色风格；不选则用内置的保守默认策略。\n{0} = 已置顶，排在列表最前面。\n"
    "在列表里右键任意一条，可以置顶/取消置顶或删除。": (
        "Copy which grading style; when nothing is selected the built-in conservative default "
        "policy is used.\n{0} = pinned, shown first in the list.\nRight-click any entry in the "
        "list to pin, unpin or delete it."
    ),
    "用大白话描述这批照片想调成什么样，例如：\n“海边日落，保留暖调，肤色别过饱和。”\n\n"
    "与风格规则冲突时以这里为准；留空则按风格走（都没选就做保守的统一校正）。": (
        "Describe in plain words how this batch should look, e.g.:\n“Seaside sunset, keep the "
        "warm tone, don't oversaturate skin.”\n\nThis wins over the style rules if they "
        "conflict; leave it empty to follow the style (with nothing selected, a conservative "
        "uniform correction is applied)."
    ),
    "训练和输出共用上方的「模型 / 密钥」设置，配一次即可（训练同样要调用 AI）。\n"
    "保存后可在输出模式里直接选用；结果存为 styles/<风格名>.json（想打开这个目录：文件 → 打开风格文件夹）。": (
        "Training and output share the Model / Key settings above; configure them once (training "
        "also calls the AI).\nAfter saving, pick it in output mode; the result is stored as "
        "styles/<style name>.json (to open that folder: File → Open Styles Folder)."
    ),
    "训练模式：添加若干「RAW + 它的 XMP」成对样本，程序会读你已有的调色设置，\n"
    "区分“你自己调的”和“相机/镜头默认的”，归纳成你的专属风格。": (
        "Training mode: add several “RAW + its XMP” sample pairs; the app reads the grading you "
        "already applied,\nseparates “what you changed” from “camera/lens defaults”, and "
        "summarises them into a style of your own."
    ),
    "连接 = 端点 + 密钥 + 一个你起的名字。\n用官方端点？从「预设」里挑一家，自动填好地址。\n"
    "用中转站 / 本地网关？预设留空，自己填 base_url 即可 —— 它们都是 OpenAI 兼容接口。": (
        "A connection = endpoint + key + a name you choose.\nOfficial endpoint? Pick a provider "
        "from Preset and the address is filled in.\nGateway / local server? Leave the preset "
        "empty and type the base_url — they are all OpenAI-compatible."
    ),
}
