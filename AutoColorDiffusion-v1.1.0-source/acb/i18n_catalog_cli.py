# -*- coding: utf-8 -*-
"""英文文案表 · 配置 / 密钥链 / CLI 输出。

键 = 代码里的中文原文（逐字符一致）。CLI 与配置文件报错同样是"给人看的"：
用户在命令行里跑一次，或者界面弹出一个配置错误，看到的就是这些句子。
"""

from __future__ import annotations

CLI: dict[str, str] = {
    # --- 配置（config.py）---------------------------------------------------
    "连接已保存：%s（%d 条）": "Connection saved: %s (%d total)",
    "已删除连接「%s」（密钥保留在凭据管理器里）": (
        "Deleted connection “%s” (its key stays in the credential manager)"
    ),
    "连接「%s」的限速已更新：并发 %s / 每分钟 %s 次（写入 %s）": (
        "Rate limit for connection “%s” updated: concurrency %s / %s per minute (written to %s)"
    ),
    "已释放默认模型配置到 %s": "Extracted the default model config to %s",
    "api_style「{0}」尚未实现，已在加载时拒绝。\n当前实现的请求形状只有：\n{1}\n"
    "Anthropic 原生 /v1/messages、Gemini 原生 :generateContent 都还没实现 "
    "——它们不是改一个 model 字符串就能用的，必须先加请求构造分支。": (
        "api_style “{0}” is not implemented and was rejected at load time.\nThe only request "
        "shape implemented is:\n{1}\nAnthropic's native /v1/messages and Gemini's native "
        ":generateContent are not implemented — renaming a model will not make them work; they "
        "need their own request-building branches first."
    ),
    "连接文件 {0} 的顶层必须是映射（connections: ...）": (
        "The top level of the connections file {0} must be a mapping (connections: ...)"
    ),
    "连接文件 {0} 的 connections 必须是映射（名称: 配置）": (
        "The connections field in {0} must be a mapping (name: config)"
    ),
    "连接文件里的 %d 处能力字段已跟上预设的修正（你改过的不会被覆盖）：%s": (
        "%d capability fields in the connections file were brought in line with the presets "
        "(your own edits are never overwritten): %s"
    ),
    "连接必须有名字（name）": "A connection must have a name",
    "找不到连接「{0}」，无法写入限速": "Connection “{0}” not found; cannot write the rate limit",
    "已清理模型配置里残留的旧键（models/active）：%s": (
        "Removed leftover legacy keys from the model config (models/active): %s"
    ),
    "模型配置已补充 %d 个缺失的服务商：%s（文件：%s）": (
        "Added %d missing providers to the model config: %s (file: %s)"
    ),
    "模型配置已修正 %d 处「继承来的错误能力值 / 缺失字段」（你改过的值不会被覆盖）：%s": (
        "Fixed %d “inherited wrong capability values / missing fields” in the model config "
        "(your own edits are never overwritten): %s"
    ),
    "找不到内置模型配置：{0}\n开发模式下请确认项目根目录存在 config/models.yaml；\n"
    "打包模式下请确认 spec 的 datas 已包含 config/models.yaml。": (
        "Built-in model config not found: {0}\nIn development, make sure config/models.yaml "
        "exists in the project root;\nin a packaged build, make sure the spec's datas include "
        "config/models.yaml."
    ),
    "有 %d 条 model_overrides 没写 reason（依据）：%s。"
    "建议写成“官方文档出处”或“实测得到的现象”—— 否则日后没人能判断它还该不该留。": (
        "%d model_overrides entries have no reason: %s. Write the source (an official document) "
        "or the observed behaviour — otherwise nobody can tell later whether it should stay."
    ),
    "模型配置文件不存在：{0}": "Model config file does not exist: {0}",
    "模型配置 {0} 的顶层必须是映射（key: value），实际是 {1}": (
        "The top level of model config {0} must be a mapping (key: value); it is {1}"
    ),
    "连接目录已加载：%s（%d 个模型）。": "Connection catalog loaded: %s (%d models).",
    "当前没有任何可用模型：%d 条连接都没有模型 id（保存密钥后会自动拉取，或点「＋」手动添加）。": (
        "No models available: none of the %d connections has a model id (saving a key fetches "
        "them automatically, or click “+” to add one manually)."
    ),
    "base_url 不能为空": "base_url cannot be empty",
    "连接名不能为空": "The connection name cannot be empty",
    "连接名不能包含「::」（模型键用 :: 分隔连接与模型）": (
        "The connection name cannot contain “::” (model keys use :: to separate connection and "
        "model)"
    ),
    "连接名不能包含换行或制表符": "The connection name cannot contain newlines or tabs",
    "providers 不能为空，至少需要一个服务商": (
        "providers cannot be empty; at least one provider is required"
    ),
    "还没有任何可用模型。\n"
    "请在界面里保存某个服务商的 API 密钥 —— 程序会自动调用该服务商的 /models 接口，"
    "把真实可调用的模型列表加载进「模型」下拉。\n"
    "若你的服务商不支持 /models，请点「模型」下拉右边的「＋」手动添加，\n"
    "或直接编辑 %APPDATA%\\AutoColorDiffusion\\config\\discovered_models.json。": (
        "No models available yet.\nSave an API key for a provider in the UI — the app calls that "
        "provider's /models endpoint and loads the real model list into the Model dropdown.\n"
        "If your provider has no /models endpoint, click “+” next to the Model dropdown to add "
        "one manually,\nor edit %APPDATA%\\AutoColorDiffusion\\config\\discovered_models.json "
        "directly."
    ),
    "无法读取连接文件 {0}：{1}": "Cannot read the connections file {0}: {1}",
    "连接文件 {0} 的 YAML 语法错误：\n{1}": "YAML syntax error in the connections file {0}:\n{1}",
    "模型配置已从旧格式升级为服务商目录（providers）：%s": (
        "The model config was upgraded from the old format to the provider catalog (providers): %s"
    ),
    "无法读取模型配置 {0}：{1}": "Cannot read the model config {0}: {1}",
    "模型配置 YAML 语法错误 {0}：\n{1}": "YAML syntax error in the model config {0}:\n{1}",
    "模型配置 {0} 校验失败：\n{1}\n请对照 config/models.yaml 顶部的注释检查必填字段是否齐全。": (
        "Validation of the model config {0} failed:\n{1}\nCheck the required fields against the "
        "comments at the top of config/models.yaml."
    ),
    "连接文件 {0} 里的连接「{1}」校验失败：\n{2}\n"
    "对照文件顶部的注释检查字段；不认识的字段名会直接报错（这是刻意的）。": (
        "Validation of connection “{1}” in {0} failed:\n{2}\nCheck the fields against the "
        "comments at the top of the file; unknown field names are rejected on purpose."
    ),
    "修正连接文件失败（不影响本次运行）：%s": (
        "Fixing the connections file failed (does not affect this run): %s"
    ),

    # --- 密钥链（keyring_store.py）------------------------------------------
    "keyring 后端可用：%s": "keyring backend available: %s",
    "已从环境变量 %s 读取密钥：%s": "Read the key from the environment variable %s: %s",
    "account 不能为空": "account cannot be empty",
    "keyring 不可用，密钥 %s 仅保存在本次会话内存中。": (
        "keyring is unavailable; the key %s is kept in this session's memory only."
    ),
    "密钥已保存到系统密钥链：%s": "Key saved to the system keychain: %s",
    "已清除本次会话内存中的密钥（account=%s）。": (
        "Cleared the key from this session's memory (account=%s)."
    ),
    "未找到 API 密钥（account={0}）。\n请通过界面输入并保存，或把密钥写入同名环境变量。\n"
    "密钥绝不写入任何配置文件，仅存放于系统密钥链（{1}）。": (
        "No API key found (account={0}).\nEnter and save one in the UI, or put the key in an "
        "environment variable of the same name.\nKeys are never written to a config file; they "
        "live only in the system keychain ({1})."
    ),
    "keyring 不可用，将仅在内存中保存密钥。原因：%s": (
        "keyring is unavailable; the key will be kept in memory only. Reason: %s"
    ),
    "未找到可用的 keyring 后端": "No usable keyring backend found",
    "keyring 回退到了不可用后端 {0}": "keyring fell back to the unusable backend {0}",
    "keyring 后端不可用，将仅在内存中保存密钥。原因：%s": (
        "The keyring backend is unavailable; the key will be kept in memory only. Reason: %s"
    ),
    "写入 keyring 失败（account=%s）：%s。密钥仅保存在本次会话内存中，程序关闭后需重输。": (
        "Writing to keyring failed (account=%s): %s. The key is kept in this session's memory "
        "only and must be re-entered after the app closes."
    ),
    "已从系统密钥链删除密钥（account=%s）。": (
        "Deleted the key from the system keychain (account=%s)."
    ),
    "已从 keyring 读取密钥：%s": "Read the key from keyring: %s",
    "从 keyring 读取密钥失败（account=%s）：%s": (
        "Reading the key from keyring failed (account=%s): %s"
    ),
    "查询 keyring 条目失败（account=%s）：%s": (
        "Querying the keyring entry failed (account=%s): %s"
    ),
    "删除 keyring 条目失败（account=%s）：%s": (
        "Deleting the keyring entry failed (account=%s): %s"
    ),

    # --- CLI（app.py）-------------------------------------------------------
    "Auto Color Diffusion v{0} —— AI 批量调色辅助工具": (
        "Auto Color Diffusion v{0} — AI batch grading assistant"
    ),
    "支持的后缀：{0}": "Supported extensions: {0}",
    "前提条件（很重要）：Camera Raw 首选项必须勾选\n"
    "  「将图像设置存储在侧车 .xmp 文件中」，否则旁侧 XMP 不会被读取。": (
        "Prerequisite (important): in Camera Raw preferences you must tick\n"
        "  “Store image settings in sidecar .xmp files”, otherwise the sidecar XMP is ignored."
    ),
    "DNG 提示：DNG 的设置会被写回文件内部，操作前请务必备份原片。": (
        "Note for DNG: DNG settings are written back inside the file, so always back up the "
        "original first."
    ),
    "[{0}] Photoshop：{1}": "[{0}] Photoshop: {1}",
    "[{0}] 系统密钥链：{1}": "[{0}] System keychain: {1}",
    "[{0}] {1}": "[{0}] {1}",
    "[OK] exiftool {0}：{1}": "[OK] exiftool {0}: {1}",
    "[OK] 密钥 {0}（账户名 {1}）": "[OK] Key {0} (account {1})",
    "[OK] 模型：{0}": "[OK] Model: {0}",
    "[OK] 模型：{0}（离线调试模式，不会发出任何网络请求）": (
        "[OK] Model: {0} (offline debug; no network requests will be made)"
    ),
    "[全量模式] 已完成的文件也会重新请求 API；想跳过它们请加 --resume。"
    "（注意：输出文件若已存在，会按尾缀规则递增而不会覆盖）": (
        "[full mode] Finished files are requested again; add --resume to skip them. (Note: if an "
        "output file already exists, the suffix is incremented instead of overwritten.)"
    ),
    "[提示] {0}": "[info] {0}",
    "[提示] 样本不足 {0} 个，结果可能不稳定。": (
        "[info] Fewer than {0} samples; results may be unstable."
    ),
    "[警告] {0}": "[warning] {0}",
    "[警告] 找不到风格「{0}」，将按未选择风格处理。": (
        "[warning] Style “{0}” was not found; continuing as if no style were selected."
    ),
    "[错误] {0}": "[error] {0}",
    "[错误] 无法加载图形界面（PyQt6 未安装？）：{0}": (
        "[error] Cannot load the graphical interface (is PyQt6 installed?): {0}"
    ),
    "[错误] 未找到 API 密钥（账户名 {0}）。\n"
    "无界面模式下无法弹出输入框，请先设置环境变量或打开界面保存一次：\n"
    "    PowerShell:  $env:{1} = \"sk-...\"\n若只想验证链路而不调用 API，请改用 --mock-ai。": (
        "[error] No API key found (account {0}).\nIn headless mode no input dialog can be shown; "
        "set the environment variable first, or open the UI and save it once:\n"
        "    PowerShell:  $env:{1} = \"sk-...\"\nTo verify the pipeline without calling the API, "
        "use --mock-ai instead."
    ),
    "[错误] 未找到模型条目 {0}；可用：{1}": (
        "[error] Model entry {0} was not found; available: {1}"
    ),
    "[错误] 未指定任何源路径。请用 --files 或位置参数传入文件夹/文件。": (
        "[error] No source path given. Pass a folder or file with --files or as a positional "
        "argument."
    ),
    "[错误] 训练未产出风格档案。": "[error] Training produced no style profile.",
    "[错误] 训练模式必须用 --style-name 指定风格名。": (
        "[error] Training mode requires --style-name."
    ),
    "可以改用命令行模式：python app.py --no-gui --files <目录>": (
        "You can use the command line instead: python app.py --no-gui --files <folder>"
    ),
    "\n--- {0} ---": "\n--- {0} ---",
    "\n失败清单（{0} 条，其中永久跳过 {1} 条）：": (
        "\nFailure list ({0} entries, {1} of them skipped permanently):"
    ),
    "\n修正原因后可用 --only-failed 重跑，或 --clear-failures 重置失败记录。": (
        "\nAfter fixing the cause, re-run with --only-failed, or reset the records with "
        "--clear-failures."
    ),
    "\n已中断。已完成的处理结果已保留，可用 --resume 继续。": (
        "\nInterrupted. Finished results were kept; continue with --resume."
    ),
    "\n已清理本次运行的脚本目录（{0} 个文件）。": (
        "\nCleaned up this run's script folder ({0} files)."
    ),
    "\n收尾清理出错（已忽略，不影响结果）：{0}": (
        "\nCleanup at the end failed (ignored; results are unaffected): {0}"
    ),
    "\n需要手动完成导出：脚本目录保留在\n  {0}\n"
    "  双击其中的 run_export.bat 即可（关闭程序不影响）。": (
        "\nManual export needed: the script folder was kept at\n  {0}\n  double-click "
        "run_export.bat inside it (closing the app does not affect it)."
    ),
    "\n风格档案已保存：{0}": "\nStyle profile saved: {0}",
    "    {0}: {1}": "    {0}: {1}",
    "  - {0}（{1} 次）{2}\n      {3}": "  - {0} ({1} times){2}\n      {3}",
    "已写出 XMP": "XMP written",
    "Auto Color Diffusion —— AI 批量调色辅助工具。为一批 RAW 生成 ACR 调整参数并写出 "
    "XMP 旁侧文件，再生成 Photoshop 批量导出脚本。": (
        "Auto Color Diffusion — an AI batch grading assistant. It generates ACR settings and "
        "XMP sidecars for a batch of RAW files, then produces a Photoshop batch export script."
    ),
    "DNG 也生成旁侧 .xmp（默认写回文件内部；勾选此项可能被 ACR 忽略）": (
        "Also write a sidecar .xmp for DNG (default is writing inside the file; ACR may ignore "
        "the sidecar)"
    ),
    "RAW 文件或文件夹（文件夹会递归扫描）": (
        "RAW files or folders (folders are scanned recursively)"
    ),
    "[已废弃] 现在这就是默认行为，保留仅为兼容旧脚本": (
        "[deprecated] this is now the default; kept only for old scripts"
    ),
    "[警告] ": "[warning] ",
    "base_url 必须以 http:// 或 https:// 开头。": (
        "base_url must start with http:// or https://."
    ),
    "不使用缩略图缓存": "Do not use the thumbnail cache",
    "不打开界面，直接命令行执行": "Run from the command line without opening the UI",
    "不放在源目录同层，改为输出到 <源目录>/_export/ 子目录": (
        "Do not put output next to the source; write to <source>/_export/ instead"
    ),
    "不限": "unlimited",
    "保存位置：{0}": "Saved to: {0}",
    "全部完成（{0} 张）": "All done ({0} images)",
    "单批最大请求数（含重试）；默认 = 文件数 × {0}": (
        "Maximum requests per batch (including retries); default = files × {0}"
    ),
    "只处理前 {0} 张用于验证链路": "Process only the first {0} files to verify the pipeline",
    "只生成 jsx/manifest，不自动调用 Photoshop": (
        "Only generate jsx/manifest; do not call Photoshop"
    ),
    "同位置参数；写成 --files 形式可读性更好": (
        "Same as the positional argument; --files reads better"
    ),
    "导出格式：JPG / PNG（默认 JPG）。PNG 为无损格式，--quality 对它们无效": (
        "Export format: JPG / PNG (default JPG). PNG is lossless, so --quality has no effect"
    ),
    "并发数，默认 {0}": "Number of parallel workers (default {0})",
    "手动添加模型": "Add a model manually",
    "提示词 + 本地强校验": "prompt + strict local validation",
    "断点续跑": "Resume",
    "清空全部任务状态（已完成记录 + 失败记录）后退出": (
        "Clear all task state (finished records + failure records) and exit"
    ),
    "清除 failed.json 中的失败记录（含永久跳过标记）后退出": (
        "Clear the failure records in failed.json (including permanent-skip flags) and exit"
    ),
    "清除缩略图缓存后退出": "Clear the thumbnail cache and exit",
    "示例：\n"
    "  python app.py                                        # 打开图形界面\n"
    "  python app.py --no-gui --files D:\\\\photos --dry-run    # 无界面演练 3 张\n"
    "  python app.py --no-gui --files D:\\\\photos --resume     # 续跑未完成的\n"
    "  python app.py --no-gui --files D:\\\\photos --only-failed\n"
    "  python app.py --clear-failures                       # 清除失败记录\n"
    "  python app.py --no-gui --files D:\\\\train --train --style-name 海边暖调\n": (
        "Examples:\n"
        "  python app.py                                        # open the GUI\n"
        "  python app.py --no-gui --files D:\\\\photos --dry-run    # headless dry run of 3 files\n"
        "  python app.py --no-gui --files D:\\\\photos --resume     # resume the unfinished\n"
        "  python app.py --no-gui --files D:\\\\photos --only-failed\n"
        "  python app.py --clear-failures                       # clear failure records\n"
        "  python app.py --no-gui --files D:\\\\train --train --style-name seaside-warm\n"
    ),
    "覆盖 models.yaml 的 active 模型条目名": (
        "Overrides the active model entry name in models.yaml"
    ),
    "训练产出的风格名（必填）": "Style name produced by training (required)",
    "跳过已完成的文件，不重复请求 API（默认**关闭**，即默认全部重新处理）": (
        "Skip finished files and do not request the API again (off by default, i.e. everything "
        "is reprocessed)"
    ),
    "输出 JPG 质量，直接使用 Photoshop 的 {0}–{1} 刻度（默认 {2}，即 Photoshop 上限）。"
    "数值越大画质越好、文件越大；{3} 约等于 libjpeg {4}，真实的 libjpeg 100 在 Photoshop "
    "里无法表达。为兼容旧脚本，也接受档位名 ": (
        "Output JPG quality, using Photoshop's {0}–{1} scale directly (default {2}, i.e. "
        "Photoshop's maximum). Higher means better quality and larger files; {3} is about "
        "libjpeg {4}, and true libjpeg 100 cannot be expressed in Photoshop. For compatibility "
        "with old scripts the legacy tier names are also accepted "
    ),
    "输出格式（JPG / PNG）": "Export format (JPG / PNG)",
    "输出色彩空间，默认 sRGB": "Output color space, default sRGB",
    "进入训练模式：从 (RAW + XMP) 对归纳风格档案": (
        "Enter training mode: derive a style profile from (RAW + XMP) pairs"
    ),
    "选择包含 RAW 的文件夹（会递归扫描子目录）": (
        "Choose a folder containing RAW files (subfolders are scanned)"
    ),
    "需要 API 密钥": "API key required",
    "风格名（styles/ 目录下的 name 字段）": "Style name (the name field in styles/)",
    "（已废弃）同层现在是默认行为；保留仅为兼容旧命令行": (
        "(deprecated) same-level output is now the default; kept only for old command lines"
    ),
    "（无损，画质设置不参与）": "(lossless; the quality setting does not apply)",
    "已清除缩略图缓存：删除 {0} 个文件（原有 {1} 个，约 {2} MB）。": (
        "Cleared the thumbnail cache: deleted {0} files (there were {1}, about {2} MB)."
    ),
    "前提条件（很重要）：Camera Raw 首选项必须勾选": (
        "Prerequisite (important): in Camera Raw preferences you must tick"
    ),
    "  「将图像设置存储在侧车 .xmp 文件中」，否则旁侧 XMP 不会被读取。": (
        "  “Store image settings in sidecar .xmp files”, otherwise the sidecar XMP is ignored."
    ),
    "Auto Color Diffusion 仅支持 Windows 平台。": (
        "Auto Color Diffusion supports Windows only."
    ),
    "当前检测到平台：{0}": "Detected platform: {0}",
    "本项目不提供 macOS / Linux 实现：\n"
    "  - Photoshop 批量导出依赖 win32com.client（Windows COM）；\n"
    "  - 密钥管理依赖 Windows 凭据管理器（keyring.backends.Windows）；\n"
    "  - 打包目标为 Windows 单文件/单目录 exe。": (
        "This project provides no macOS / Linux implementation:\n"
        "  - batch export to Photoshop relies on win32com.client (Windows COM);\n"
        "  - key management relies on Windows Credential Manager "
        "(keyring.backends.Windows);\n"
        "  - the packaged target is a Windows one-file / one-folder exe."
    ),
}
