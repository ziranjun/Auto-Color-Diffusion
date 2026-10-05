# -*- coding: utf-8 -*-
"""英文文案表 · 日志与管线消息层（AI / 管线 / RAW / XMP / PS / 清理）。

键 = 代码里的中文原文（逐字符一致）。这些文案会进界面的日志面板与日志文件，
所以同样属于"给人看的"；AI 提示词与风格规则文本**不在这里**（见 acb/i18n.py 说明）。
"""

from __future__ import annotations

MSG: dict[str, str] = {
    # --- AI 适配器 ----------------------------------------------------------
    "经过 {0} 次尝试仍无法获得合法结果。最后错误：{1}": (
        "Still no valid result after {0} attempts. Last error: {1}"
    ),
    "已请求停止（{0}）：不再发送新的 API 请求。": (
        "Stop requested ({0}): no further API requests will be sent."
    ),
    "响应中缺少 items 数组。实际键：{0}": "The response has no items array. Actual keys: {0}",
    "响应中缺少这些 file_id 的结果：{0}（期望 {1} 项，实得 {2} 项）": (
        "The response is missing results for these file_ids: {0} (expected {1}, got {2})"
    ),
    "响应中包含未请求的 file_id：%s，已忽略。": (
        "The response contains an unrequested file_id: %s; ignored."
    ),
    "没有可用的训练样本": "No training samples available",
    "分析失败（%s）：%s": "Analysis failed (%s): %s",
    "训练结果缺少必需字段：{0}": "The training result is missing required fields: {0}",
    "text_rules 必须是非空数组": "text_rules must be a non-empty array",
    "第 %d 次响应解析/校验失败：%s": "Response parse/validation failed on attempt %d: %s",
    "多图请求失败，拆分为单图请求重试（共 %d 张）。": (
        "The multi-image request failed; retrying as single-image requests (%d images)."
    ),
    "{0}\n该模型不支持设置输出上限（max_tokens_mode=omit）：请求里没有上限字段，"
    "程序无法用「抬高出上限」这个手段救回来，重发一次也是同样的结果（本次已跳过重发）。\n"
    "请改用以下任一方式：\n  1) 减小单次输出量：保持 images_per_request=1，或分批处理；\n"
    "  2) 若这家其实支持设置上限，把该条目的 max_tokens_mode 改成 max_tokens / "
    "max_completion_tokens 再试；\n"
    "  3) 关掉推理/思考模式（extra_body，例如 {{reasoning_effort: none}}）—— "
    "推理 token 不再挤占输出预算，同样的问题往往就消失了。": (
        "{0}\nThis model does not support an output limit (max_tokens_mode=omit): the request "
        "carries no limit field, so raising the limit cannot help and a resend would fail the "
        "same way (the resend was skipped).\nTry one of these instead:\n"
        "  1) Reduce the output size: keep images_per_request=1, or process in batches;\n"
        "  2) If the provider does support a limit, change that entry's max_tokens_mode to "
        "max_tokens / max_completion_tokens and retry;\n"
        "  3) Turn off reasoning/thinking mode (extra_body, e.g. {{reasoning_effort: none}}) — "
        "reasoning tokens no longer eat the output budget and the problem usually disappears."
    ),
    "{0}\n已把 {1} 从 {2} 抬到 {3} 重试过（发送方式：{4}），仍失败。\n"
    "可尝试：1) 在模型条目里把 max_output_tokens 直接调大；2) 关掉思考模式"
    "（DeepSeek 可用 extra_body: {{reasoning_effort: none}}）；3) 调小 images_per_request "
    "以减少单次输出量；4) 把 supports_json_object / supports_json_schema 关掉以改变输出约束方式。": (
        "{0}\nAlready retried with {1} raised from {2} to {3} (sent as: {4}), and it still "
        "failed.\nTry: 1) raise max_output_tokens directly in the model entry; 2) turn off "
        "thinking mode (for DeepSeek: extra_body: {{reasoning_effort: none}}); 3) lower "
        "images_per_request to reduce the output size; 4) turn off supports_json_object / "
        "supports_json_schema to change how the output is constrained."
    ),
    "第 %d 次尝试失败（%s）：把 %s 从 %d 抬到 %d 后重试一次。": (
        "Attempt %d failed (%s): raising %s from %d to %d and retrying once."
    ),
    "第 %d 次尝试失败（%s），但输出上限已到天花板 %d，只能原样重试。": (
        "Attempt %d failed (%s), but the output limit is already at the ceiling %d; retrying "
        "unchanged."
    ),

    # --- AI 客户端 ----------------------------------------------------------
    "未实现的请求形状 api_style={0}（模型条目 {1}）。\n"
    "请检查 config/models.yaml 或 config/connections.yaml 里的 api_style：\n"
    "  目前只实现了 openai（OpenAI 兼容端点）。\n"
    "Anthropic 原生 /v1/messages 与 Gemini 原生 :generateContent 需要新增请求构造分支后才能使用 "
    "—— 换 model 名字是换不过去的。": (
        "Unimplemented request shape api_style={0} (model entry {1}).\n"
        "Check api_style in config/models.yaml or config/connections.yaml:\n"
        "  only openai (OpenAI-compatible endpoints) is implemented.\n"
        "Anthropic's native /v1/messages and Gemini's native :generateContent need their own "
        "request-building branches — renaming the model will not get you there."
    ),
    "已作废 %s 的限流记忆（%s），改按当前配置跑。": (
        "Discarded the learned rate limit for %s (%s); using the current configuration."
    ),
    "已记住：%s 的 %s 只能使用 temperature=%s。": (
        "Noted: %s only accepts temperature=%s for %s."
    ),
    "已记住：%s 不支持 response_format.%s。下次不再发送该字段（想重试请删 %s）。": (
        "Noted: %s does not support response_format.%s. It will not be sent again (delete %s "
        "to retry)."
    ),
    "缩略图 %d 字节超过单图上限 %d 字节，正在降质重编码。": (
        "The thumbnail (%d bytes) exceeds the per-image limit (%d bytes); re-encoding at lower "
        "quality."
    ),
    "模型返回内容为空": "The model returned empty content",
    "模型返回的顶层不是 JSON 对象（是 {0}）": (
        "The model's top-level value is not a JSON object (it is {0})"
    ),
    "模型返回的内容中找不到 JSON 对象。原始内容前 300 字：{0}": (
        "No JSON object found in the model output. First 300 characters: {0}"
    ),
    "模型返回的 JSON 顶层不是对象": "The model's JSON top level is not an object",
    "本轮端点并发上限 = %d（用户设 %d；服务端声明/记忆的基准=%s；遇到限流或超时会自动减半）。": (
        "Endpoint concurrency limit for this run = %d (configured %d; server-stated/learned "
        "baseline=%s; halved automatically after a rate limit or timeout)."
    ),
    "%s：按配置不发送输出上限字段（max_tokens_mode=omit），单次调用输出的 token 数完全由服务端决定 "
    "—— 大批量运行请注意账单。（想设上限就把该条目的 max_tokens_mode 改成 max_tokens / "
    "max_completion_tokens）": (
        "%s: per configuration no output-limit field is sent (max_tokens_mode=omit); the server "
        "decides how many tokens a single call produces — watch your bill on large batches. "
        "(To set a limit, change that entry's max_tokens_mode to max_tokens / "
        "max_completion_tokens.)"
    ),
    "无法从响应中提取文本内容。响应摘要：{0}": (
        "Could not extract text content from the response. Response summary: {0}"
    ),
    "写入端点限流记忆失败（不影响本次运行）：%s": (
        "Failed to write the endpoint rate-limit memory (does not affect this run): %s"
    ),
    "作废端点限流记忆失败（不影响本次运行）：%s": (
        "Failed to discard the endpoint rate-limit memory (does not affect this run): %s"
    ),
    "写入固定温度记忆失败（不影响本次运行）：%s": (
        "Failed to write the fixed-temperature memory (does not affect this run): %s"
    ),
    "写入端点能力记忆失败（不影响本次运行）：%s": (
        "Failed to write the endpoint capability memory (does not affect this run): %s"
    ),
    "模型返回的 JSON 无法解析：{0}。内容前 300 字：{1}": (
        "The model's JSON could not be parsed: {0}. First 300 characters: {1}"
    ),
    "%s：按配置不发送输出上限字段（max_tokens_mode=omit）；服务端将使用它的默认值 %s。": (
        "%s: per configuration no output-limit field is sent (max_tokens_mode=omit); the server "
        "will use its default of %s."
    ),
    "extra_body 里不能写这些键：{0}。\n"
    "它们由程序管理（model / messages / stream / response_format）：写错了不是在「微调」，"
    "而是在改程序的行为，出错时无法归因。\n"
    "想调输出格式请用 supports_json_schema / supports_json_object，想改模型请改「模型」下拉。": (
        "These keys cannot be put in extra_body: {0}.\nThey are managed by the app (model / "
        "messages / stream / response_format): writing them is not “fine-tuning”, it changes the "
        "app's behaviour and makes failures impossible to attribute.\n"
        "Use supports_json_schema / supports_json_object for the output format, and the Model "
        "dropdown to change the model."
    ),
    "服务端压力偏大（%s）：本次运行把该端点并发上限从 %d 降到 %d"
    "（仅为本次运行，不写记忆；下次启动仍按配置）。": (
        "The server is under pressure (%s): concurrency for this endpoint is lowered from %d to "
        "%d for this run only (not remembered; the next start uses the configuration)."
    ),
    "已请求停止（%s）：不再发送新的 API 请求。": (
        "Stop requested (%s): no further API requests will be sent."
    ),
    "响应中没有 choices 字段：{0}": "The response has no choices field: {0}",
    "模型输出因达到 max_output_tokens 被截断（finish_reason={0}）。": (
        "The model output was truncated at max_output_tokens (finish_reason={0})."
    ),
    "端点限流记忆已过期（超过 %d 天），本次不采用：%s。若服务端仍然限流，程序会重新从 429 原文里学。": (
        "The learned rate limit expired (older than %d days) and is not used: %s. If the server "
        "still rate-limits, the app will learn it again from the 429 body."
    ),
    "端点限量生效：%s 最多 %s 个请求同时在飞、每分钟最多 %s 次"
    "（配置 max_concurrency/max_rpm 与上次学到的值取更严的一个；想清掉记忆：删 %s）。": (
        "Endpoint limits in effect: %s allows at most %s requests in flight and %s per minute "
        "(the stricter of the configured max_concurrency/max_rpm and the learned values; delete "
        "%s to clear the memory)."
    ),
    "本批请求数已达上限{0}。\n已用 {1} 次。为避免失控耗费，任务已停止。\n"
    "常见原因：API 密钥失效、模型名写错、网络不通、或服务端持续超时，导致每次尝试都失败。\n"
    "排查建议：先看日志里最近的错误原文，修正后重跑（输出模式可用「只跑失败」）。": (
        "This batch hit the request limit of {0}.\n{1} requests were used. The task stopped to "
        "prevent runaway cost.\nCommon causes: an invalid API key, a wrong model name, no network, "
        "or the server timing out every attempt.\nWhat to do: read the latest error text in the "
        "log, fix it and run again (use Failed Only in output mode)."
    ),
    "即使降到 quality=45 仍超过上限，将按现状发送（可能被服务端拒收）。": (
        "Even at quality=45 it exceeds the limit; sending as is (the server may reject it)."
    ),
    "请求 %s（第 %d 次尝试，模型 %s，消息 %d 条，读超时 %gs）": (
        "Request %s (attempt %d, model %s, %d messages, read timeout %gs)"
    ),
    "将在 %.2f 秒后重试（第 %d/%d 次）。": "Retrying in %.2f seconds (attempt %d/%d).",
    "服务端回显模型：%s（本次请求的是 %s）": (
        "Server echoed model: %s (this request asked for %s)"
    ),
    "服务端未回显 model 字段，无法核对实际使用的模型（常见于自建网关）。": (
        "The server did not echo a model field, so the model actually used cannot be verified "
        "(common with self-hosted gateways)."
    ),
    "降质到 quality=%d 后为 %d 字节。": "After lowering to quality=%d it is %d bytes.",
    "关闭 HTTP 连接池失败（忽略）：%s": "Closing the HTTP connection pool failed (ignored): %s",
    "在端点闸门处等了 %.1f 秒才发出（服务端限并发/冷却中）。": (
        "Waited %.1f seconds at the endpoint gate before sending (server concurrency limit / "
        "cooldown)."
    ),
    "请求超时（第 %d 次，读超时 %gs）：%s": "Request timed out (attempt %d, read timeout %gs): %s",
    "请求异常（第 %d 次）：%s": "Request error (attempt %d): %s",
    "服务端返回可重试状态码 {0}：{1}": "Server returned retryable status {0}: {1}",
    "服务端返回 %d（第 %d 次），将退避重试。": (
        "Server returned %d (attempt %d); backing off and retrying."
    ),
    "⚠ 模型不匹配：请求的是 %s，服务端回显的是 %s。"
    "切换可能未生效（中转站改路由，或把该 id 降级到了默认模型）。Token 用量按服务端实际使用的模型计费。": (
        "⚠ Model mismatch: requested %s but the server echoed %s. The switch may not have taken "
        "effect (a gateway rerouted, or the id was downgraded to a default model). Token usage is "
        "billed for the model the server actually used."
    ),
    "按上次的记录，%s 不支持 response_format.%s，本次不再发送该字段（改用提示词 + 本地强校验）。"
    "想重新试探：删掉 %s。": (
        "Per the previous record, %s does not support response_format.%s, so it is not sent "
        "(the prompt plus strict local validation is used instead). To probe again, delete %s."
    ),
    "超时后把读超时从 %gs 提到 %gs 再试（思考模型或并发较高时常见）。": (
        "After a timeout, raising the read timeout from %gs to %gs and retrying (common with "
        "thinking models or high concurrency)."
    ),
    "服务端说的是**账户配额/额度**问题（不是调用频率）：%s": (
        "The server is reporting an account quota/credit problem (not request frequency): %s"
    ),
    "API 返回 429（账户配额/额度类，不是单纯的并发上限）：{0}\n"
    "服务端原文写的是配额/计费类（阿里云百炼官方错误码 429-Throttling.AllocationQuota / "
    "insufficient_quota：「每秒钟或每分钟消耗 Token 数（TPS/TPM）触发限流」或免费额度/账单额度用尽）。"
    "程序**不再重试**（退避 1 秒不会让配额恢复，只会白花一次请求）。请按顺序处理：\n"
    "  1) 等 1–5 分钟后点「只跑失败」重跑 —— TPM 按分钟窗口算，窗口滑过就恢复；\n"
    "  2) 若每次跑到几十张就复发：这是「每分钟 token 配额」被打满，把「高级设置 → 打开高级设置 → "
    "同时处理几张」降到 2 或 1（每张带图请求要上万 token，4 并发很容易打满），或减少单次处理的图片数；\n"
    "  3) 若连单独一张都失败：才是额度/账单问题 —— 去该服务商控制台确认「免费额度 / 计费与套餐 / "
    "预算上限」；有些账号开了「免费额度用完即停」，需要在控制台关掉才能继续按量计费调用。": (
        "API returned 429 (account quota/credit class, not a plain concurrency cap): {0}\n"
        "The server's own text is quota/billing related (Aliyun Bailian error codes "
        "429-Throttling.AllocationQuota / insufficient_quota: “tokens per second/minute (TPS/TPM) "
        "rate limit” or free/billing quota exhausted). The app does not retry (a 1-second backoff "
        "will not restore a quota; it just wastes a request). Handle it in this order:\n"
        "  1) Wait 1–5 minutes and click Failed Only — TPM uses a rolling minute window, so it "
        "recovers once the window slides;\n"
        "  2) If it recurs after a few dozen images, the per-minute token quota is saturated: "
        "lower “Advanced Settings → Open Advanced Settings → Parallel files” to 2 or 1 (an image "
        "request costs tens of thousands of tokens, so 4 parallel workers fill it easily), or "
        "process fewer images at a time;\n"
        "  3) If even a single image fails, it really is a quota/billing problem — check “free "
        "quota / plan and billing / budget limit” in the provider's console; some accounts have "
        "“stop when the free quota is used up” enabled and it must be turned off to keep paying "
        "per use."
    ),
    "该端点限制：同时在飞最多 %s 个、每分钟最多 %s 次（服务端原文：%s）。"
    "本次运行起已自动限量，以后启动也按这个值；想重新探测：删掉 %s。": (
        "Endpoint limit: at most %s in flight and %s per minute (server text: %s). The limit is "
        "applied automatically from this run on and on future starts; delete %s to probe again."
    ),
    "按服务端建议全端点冷却 %.0f 秒（所有工人一起等）。": (
        "Cooling the whole endpoint for %.0f seconds as the server suggested (all workers wait)."
    ),
    "响应不是合法 JSON：{0}": "The response is not valid JSON: {0}",
    "响应非 JSON（第 %d 次）：%s": "Response is not JSON (attempt %d): %s",
    "该端点拒绝了 response_format（%s）。已自动改成「不带输出格式约束、仅靠提示词 + 本地校验」重发一次；"
    "本次运行后续请求也不会再带它。建议在模型条目里关掉 supports_json_schema，并确认是否需要打开 "
    "supports_json_object（json_object 档）。": (
        "The endpoint rejected response_format (%s). It was resent without an output-format "
        "constraint (prompt + local validation only); later requests in this run will not carry "
        "it either. Consider turning off supports_json_schema for this model entry, and check "
        "whether supports_json_object (json_object tier) should be enabled."
    ),
    "该模型只接受 temperature=%s（服务端原文：%s）。已改用该值重发一次，并记住它；以后启动也用它": (
        "This model only accepts temperature=%s (server text: %s). Resent once with that value "
        "and remembered it; future starts use it too"
    ),
    "降级重发失败：%s": "Retry after downgrade failed: %s",
    "改用 temperature=%s 重发失败：%s": "Resend with temperature=%s failed: %s",

    # --- 离线调试 -----------------------------------------------------------
    "已启用离线调试：不会发出任何网络请求，所有分析结果都是写死的假数据。": (
        "Offline debug enabled: no network requests at all; every analysis result is hard-coded "
        "fake data."
    ),
    "离线调试：已为 %d 张图注入写死的假分析结果（未调用任何 API）。": (
        "Offline debug: injected hard-coded fake results for %d images (no API was called)."
    ),
    "离线调试：返回写死的假风格归纳「%s」（%d 个样本，未调用任何 API）。": (
        "Offline debug: returned the hard-coded fake style summary “%s” (%d samples, no API call)."
    ),

    # --- 模型列表拉取 -------------------------------------------------------
    "正在拉取模型列表：%s": "Fetching the model list: %s",
    "拉到 %d 个模型：%s": "Fetched %d models: %s",
    "服务商返回里没有模型列表（{0}）": "The provider response has no model list ({0})",
    "服务商返回的模型列表为空（{0}）": "The provider returned an empty model list ({0})",

    # --- 缓存 ---------------------------------------------------------------
    "读取内容指纹失败 %s：%s": "Reading the content fingerprint failed %s: %s",
    "缓存文件过小，视为损坏并删除：%s": "Cache file too small; treating it as corrupt and deleting: %s",
    "读取缩略图缓存失败 %s：%s": "Reading the thumbnail cache failed %s: %s",
    "写入缩略图缓存失败（key=%s）：%s": "Writing the thumbnail cache failed (key=%s): %s",
    "无法创建缩略图缓存目录 %s：%s。本次运行将不缓存。": (
        "Cannot create the thumbnail cache folder %s: %s. Nothing will be cached this run."
    ),

    # --- 退出清理 -----------------------------------------------------------
    "退出清理：跳过 %s —— %s": "Exit cleanup: skipping %s — %s",
    "退出清理：跳过目录 %s —— %s": "Exit cleanup: skipping folder %s — %s",
    "退出清理：%s 已设置，跳过。": "Exit cleanup: %s is set; skipping.",

    # --- 管线：任务与扫描 ---------------------------------------------------
    "检测到 %d 个文件的基础键相同（同名、同大小、同修改时间）：%s": (
        "Detected %d files sharing the same base key (same name, size and mtime): %s"
    ),
    "载入任务状态：%d 条记录，%d 条失败记录。": (
        "Loaded task state: %d records, %d failure records."
    ),
    "路径不存在或不可访问，已跳过：%s": "Path does not exist or is not accessible; skipped: %s",
    "已清除失败记录 %d 条。": "Cleared %d failure records.",
    "已清空任务状态 %d 条。": "Cleared %d task records.",
    "扫描 %s 时出错，已跳过：%s": "Error while scanning %s; skipped: %s",
    "无法读取文件属性，已跳过 %s：%s": "Cannot read file attributes; skipped %s: %s",
    "  %s 与其它副本内容一致，将复用同一分析结果（节省一次 API 请求）": (
        "  %s has identical content to another copy; reusing the same analysis (saves one API "
        "request)"
    ),
    "  已用内容指纹消歧：%s → 键 %s": "  Disambiguated by content fingerprint: %s → key %s",
    "无法创建状态目录 %s：%s。本次运行将不保存断点。": (
        "Cannot create the state folder %s: %s. Progress will not be saved this run."
    ),
    "状态文件损坏，已忽略：%s（%s）": "State file is corrupt and was ignored: %s (%s)",
    "写入状态文件失败 %s：%s": "Writing the state file failed %s: %s",
    "跳过不支持的文件（不在白名单内）：%s": "Skipping an unsupported file (not in the whitelist): %s",

    # --- 管线：输出模式 -----------------------------------------------------
    "扫描文件": "Scanning files",
    "扫描到 {0} 个 RAW 文件。": "Found {0} RAW files.",
    "请求预算上限 {0} 次（= 待处理 {1} 张 × {2}，含重试）。": (
        "Request budget: {0} (= {1} pending × {2}, including retries)."
    ),
    "提取预览并压缩": "Extracting and compressing previews",
    "完成": "Done",
    "模型能力：{0}": "Model capabilities: {0}",
    "开始分析 {0} 张图，分成 {1} 个请求组，并发 {2}。": (
        "Analysing {0} images in {1} request groups, concurrency {2}."
    ),
    "未找到任何白名单内的 RAW 文件。支持的格式：见界面提示或 README。": (
        "No whitelisted RAW files found. For supported formats see the UI hint or the README."
    ),
    "【离线调试（演练）】只处理前 {0} 张，用于验证链路。注意：演练仍会写出真实 XMP"
    "（除非加 --dry-run-no-xmp），请确认已在副本上操作。": (
        "[Offline debug] Only the first {0} files are processed, to verify the pipeline. Note: "
        "it still writes real XMP unless --dry-run-no-xmp is given, so make sure you are working "
        "on copies."
    ),
    "跳过 {0}：{1}": "Skipping {0}: {1}",
    "跳过（{0}）": "Skipped ({0})",
    "没有需要处理的文件（全部已完成或已跳过）。": (
        "Nothing to process (everything is finished or skipped)."
    ),
    "AI 分析": "AI analysis",
    "写出 XMP": "Writing XMP",
    "{0} 张图的响应里没有任何参数（模型表示「无需调整」）。"
    "如果这些图本来就该调整，请检查模型是否真的看到了图片、以及是否为某个不具备视觉能力的模型"
    "（模型下拉悬停可看能力；日志里每张图也各有一条警告）。": (
        "The response contained no parameters for {0} images (the model says no adjustment is "
        "needed). If those images should have been adjusted, check that the model actually "
        "received the images and that it is not a model without vision (hover the Model dropdown "
        "to see capabilities; each of those images also has a warning in the log)."
    ),
    "生成 Photoshop 脚本": "Generating the Photoshop script",
    "有 {0} 个文件累计失败达上限已永久跳过；修正原因后可用「清除失败记录」重置。": (
        "{0} files failed enough times to be skipped permanently; fix the cause and use Clear "
        "Failure Records to reset them."
    ),
    "用户请求停止": "Stop requested by the user",
    "统计计算失败 %s：%s": "Statistics failed %s: %s",
    "%s 局部调整：%d 个蒙版，照片方向 %s（显示时旋转 %d°）": (
        "%s local adjustments: %d masks, photo orientation %s (rotated %d° for display)"
    ),
    "已停止：预览阶段中断，已完成的处理结果已保留。": (
        "Stopped: interrupted during preview; finished results were kept."
    ),
    "无法计算批内统计（缺少可用预览），将只使用保守的默认约束。": (
        "Cannot compute batch statistics (no usable previews); only conservative defaults will "
        "be used."
    ),
    "未选择风格，已使用内置「{0}」中性基准。": (
        "No style selected; using the built-in neutral baseline “{0}”."
    ),
    "已停止：仍会把**已经分析完成**的 {0} 张写出 XMP（这批请求已经花过钱，丢掉才是浪费）；"
    "未分析的文件本次不动。": (
        "Stopped: XMP is still written for the {0} images that were already analysed (those "
        "requests were paid for; discarding them would be a waste). Files that were not analysed "
        "are left untouched."
    ),
    "{0} 分析失败（累计 {1} 次）：{2}": "{0} analysis failed ({1} times so far): {2}",
    "分析失败": "Analysis failed",
    "%s：风格不做局部调整（训练素材里带蒙版比例未达门槛），已丢弃模型提出的 %d 个蒙版": (
        "%s: the style does not use local adjustments (mask ratio in the training set is below "
        "the threshold); discarded the %d masks proposed by the model"
    ),
    "{0}：离线调试（--dry-run-no-xmp）已跳过 XMP 写出。": (
        "{0}: XMP writing skipped by offline debug (--dry-run-no-xmp)."
    ),
    "[画像] %s | 风格=%s | 面板=%s | 参数=%d%s%s%s": (
        "[profile] %s | style=%s | panels=%s | params=%d%s%s%s"
    ),
    "{0}：已写出 {1} XMP（{2} 个参数）→ {3}": "{0}: wrote {1} XMP ({2} parameters) → {3}",
    "已完成": "Finished",
    "已生成 Photoshop 脚本：{0}": "Photoshop script generated: {0}",
    "已生成导出清单：{0}（{1} 项）": "Export manifest generated: {0} ({1} items)",
    "调用 Photoshop 导出": "Calling Photoshop to export",
    "%s 与 %s 内容相同、共用分析结果，跳过重复的 API 请求。": (
        "%s and %s have identical content and share the analysis; skipping the duplicate API "
        "request."
    ),
    "同名文件过多，已改用时间戳命名：%s": (
        "Too many files with the same name; switched to timestamp naming: %s"
    ),
    "所选风格要求的分区调整（HSL / 分离色调 / 颜色分级 / 相机校准）一次都没用上"
    "——请检查模型是否真的执行了风格规则（这次结果可能仍不像你的风格）。": (
        "The selected style requires local panel adjustments (HSL / split toning / color grading "
        "/ camera calibration) but none were used — check whether the model really followed the "
        "style rules (this batch may still not look like your style)."
    ),
    "照抄告警：%s": "Copy warning: %s",
    "{0} 的失败是**账户侧**的问题（额度/计费/密钥），不计入「失败 2 次即永久跳过」"
    "——等你充值或改完配置后，直接重跑或点「只跑失败」就能再处理它。": (
        "{0} failed for an account-level reason (quota / billing / key); it does not count toward "
        "“permanently skipped after 2 failures” — after you top up or fix the configuration, just "
        "run again or click Failed Only to process it."
    ),
    "%s 护栏：%s": "%s guardrails: %s",
    "%s 写 XMP 失败（累计 %d 次）：%s": "%s failed to write XMP (%d times so far): %s",
    "{0} 写 XMP 失败（累计 {1} 次）：{2}": "{0} failed to write XMP ({1} times so far): {2}",
    "写 XMP 失败": "XMP write failed",
    "{0}：{1}": "{0}: {1}",
    "生成 Photoshop 脚本失败：{0}": "Generating the Photoshop script failed: {0}",
    "{0} 预览失败（累计 {1} 次）：{2}": "{0} preview failed ({1} times so far): {2}",
    "预览失败": "Preview failed",
    "预览就绪（{0}）": "Preview ready ({0})",
    "%s 局部调整：%s": "%s local adjustments: %s",
    "分析完成（{0} 个参数）": "Analysis done ({0} parameters)",

    # --- 管线：训练模式 -----------------------------------------------------
    "解析训练样本": "Parsing training samples",
    "AI 归纳风格偏好": "AI summarising the style preference",
    "训练请求预算 {0} 次（一次归纳，含超时重试与解析修复）。": (
        "Training request budget: {0} (one summarisation, including timeout retries and parse "
        "repairs)."
    ),
    "生成风格档案": "Generating the style profile",
    "风格档案统计完成：{0} 个字段有统计量，{1} 条风格规则，{2} 个字段被判定为相机/镜头默认值而排除。": (
        "Style profile computed: statistics for {0} fields, {1} style rules, {2} fields excluded "
        "as camera/lens defaults."
    ),
    "风格档案已保存：{0}": "Style profile saved: {0}",
    "现在可以在界面的「风格」下拉中选中它，用于后续批量调色。": (
        "You can now pick it in the Style dropdown for future batches."
    ),
    "没有可用的训练样本。请添加 RAW 文件及其同名的 XMP 旁侧文件。": (
        "No usable training samples. Add RAW files together with their same-name XMP sidecars."
    ),
    "训练样本 {0} 个，满足建议下限（{1}）。": (
        "Training samples: {0}, which meets the recommended minimum ({1})."
    ),
    "没有任何可用的训练样本（全部解析失败）。": (
        "No usable training samples (all of them failed to parse)."
    ),
    "在差分后没有发现任何'用户实际调整'的字段。"
    "这说明这批样本可能都是相机默认设置（例如全部由相机直出、未在 ACR 里调过），"
    "训练结果将只有限的参考价值。": (
        "After diffing, no field showed a genuine user adjustment. This suggests the samples are "
        "all at camera defaults (e.g. straight out of camera, never edited in ACR), so the "
        "trained result will be of limited value."
    ),
    "已停止：未提交训练请求。": "Stopped: the training request was not submitted.",
    "训练用量：{0}": "Training usage: {0}",
    "场景标签：{0}/{1} 张由模型标出。": "Scene labels: {0}/{1} images labelled by the model.",
    "模型没有返回 scene_labels：本次不会生成场景规则（scene_rules）。"
    "场景规则是按你的训练素材统计出来的，没有标签就没有依据。": (
        "The model returned no scene_labels, so no scene rules are generated. Scene rules are "
        "derived from your training material; without labels there is no evidence to base them on."
    ),
    "再次提醒：样本不足，结果可能不稳定。建议补充更多样本后重新训练。": (
        "Reminder: not enough samples, results may be unstable. Add more samples and train again."
    ),
    "跳过 %s：找不到同名 XMP，无法作为训练样本。": (
        "Skipping %s: no same-name XMP found, so it cannot be a training sample."
    ),
    "跳过 %s：找不到对应的 RAW 文件。": "Skipping %s: no matching RAW file found.",
    "已停止：训练样本解析中断。": "Stopped: parsing of training samples was interrupted.",
    "已停止：训练未完成（不会再发请求，风格档案未保存）。": (
        "Stopped: training did not finish (no further requests, the style profile was not saved)."
    ),
    "AI 归纳失败：{0}": "AI summarisation failed: {0}",
    "本次训练**没有完成：风格档案没有被保存或更新**，磁盘上仍是原来的那份。"
    "排查建议：先看上面的报错原文；超时类失败可直接重试一次，样本很多（几十上百张）时"
    "可以先减到 20–30 张，或换一个更快的模型再训。": (
        "This training run did not finish: the style profile was not saved or updated, and the "
        "file on disk is still the previous one. What to do: read the error above; timeout "
        "failures can simply be retried, and with many samples (dozens to hundreds) you can "
        "first cut down to 20–30, or train with a faster model."
    ),
    "有 {0} 张没有拿到场景标签，这些照片不会参与 scene_rules（场景规则）的统计。": (
        "{0} images got no scene label; they do not take part in the scene_rules statistics."
    ),
    "保存风格档案失败：{0}": "Saving the style profile failed: {0}",
    "DNG 内嵌 XMP 读取失败（可能是没有 exiftool，或该 DNG 确实没有设置）": (
        "Reading the embedded DNG XMP failed (maybe exiftool is missing, or the DNG really has "
        "no settings)"
    ),
    "解析失败": "Parse failed",
    "已解析（{0} 个用户调整）": "Parsed ({0} user adjustments)",

    # --- XMP：内嵌（DNG）---------------------------------------------------
    "DNG 内嵌 XMP 已更新：%s（应用 %d 个字段）；%s": (
        "Embedded DNG XMP updated: %s (%d fields applied); %s"
    ),
    "{0}：这是 DNG 文件，其 XMP 嵌入在文件内部，必须使用 exiftool 才能安全改写，但当前未检测到 "
    "exiftool。\n请安装 exiftool（见程序启动时的安装指引）后重试；\n"
    "或在界面上勾选「DNG 也生成旁侧 .xmp 文件」以知情接受该文件可能不生效。": (
        "{0}: this is a DNG file whose XMP is embedded inside it, so it can only be rewritten "
        "safely with exiftool — and exiftool was not found.\nInstall exiftool (see the guidance "
        "shown at startup) and retry;\nor tick “Also write a sidecar .xmp for DNG” in the UI to "
        "knowingly accept that this file may not take effect."
    ),
    "%s：DNG 内嵌 XMP 写回失败：%s": "%s: writing the embedded DNG XMP failed: %s",
    "%s：内嵌 XMP 写后核对失败，缺失 %d 个字段（%s）": (
        "%s: post-write verification of the embedded XMP failed; %d fields are missing (%s)"
    ),
    "{0} 的内嵌 XMP 写回后核对失败：{1} 个字段没进文件（例如 {2}）。\n"
    "参数写进去了但没生效，比直接报错更难查，所以这里按失败处理。\n"
    "原文件已由 exiftool 备份为 {3}_original，可直接改回原名恢复。": (
        "Post-write verification of {0}'s embedded XMP failed: {1} fields did not make it into "
        "the file (e.g. {2}).\nValues that are written but do not take effect are harder to "
        "diagnose than a plain error, so this counts as a failure.\nexiftool backed the original "
        "up as {3}_original; rename it back to restore."
    ),
    "%s：exiftool 不可用，降级写出旁侧文件 %s": (
        "%s: exiftool is unavailable; falling back to writing the sidecar %s"
    ),
    "%s：读出内嵌 XMP %d 字节，含 %d 段 rdf:Description": (
        "%s: read %d bytes of embedded XMP containing %d rdf:Description blocks"
    ),
    "{0} 的内嵌 XMP 无法解析（{1}）。\n"
    "为避免破坏其中的蒙版/曲线等结构，本次已跳过该文件。如需强制重写，请先用 exiftool 导出内嵌 XMP "
    "并人工确认。": (
        "The embedded XMP of {0} cannot be parsed ({1}).\nTo avoid damaging its masks/curves and "
        "other structures, this file was skipped. If you must force a rewrite, first export the "
        "embedded XMP with exiftool and check it by hand."
    ),

    # --- 风格档案 -----------------------------------------------------------
    "风格档案已保存：%s（%d 个样本）": "Style profile saved: %s (%d samples)",
    "已释放内置风格种子 %d 个到 %s": "Extracted %d built-in style seeds to %s",
    "风格文件已存在：{0}": "Style file already exists: {0}",
    "风格文件损坏，无法装载 %s：%s": "Style file is corrupt and cannot be loaded %s: %s",
    "风格「%s」在用户目录里还没有，先用打包内的副本（%s）。": (
        "Style “%s” is not in the user folder yet; using the bundled copy for now (%s)."
    ),
    "打包内的风格文件无法读取（%s）：%s": "The bundled style file cannot be read (%s): %s",

    # --- 偏好 ---------------------------------------------------------------
    "偏好文件无法解析，已按空配置处理（%s）：%s": (
        "The preferences file cannot be parsed; treating it as empty (%s): %s"
    ),
    "偏好保存失败（已忽略，不影响本次使用）：%s": (
        "Saving preferences failed (ignored; this session is unaffected): %s"
    ),
    "偏好里的导出格式 %r 已不再支持，已回退到 %s。": (
        "The export format %r in the preferences is no longer supported; falling back to %s."
    ),
    "偏好里的色彩空间 %r 已不在可选列表里，保持 %s。": (
        "The color space %r in the preferences is no longer selectable; keeping %s."
    ),

    # --- Photoshop 脚本渲染 -------------------------------------------------
    "已生成导出脚本产物：%s（%d 项，格式 %s，色彩空间 %s → %s，质量 %s）": (
        "Export script artifacts generated: %s (%d items, format %s, color space %s → %s, "
        "quality %s)"
    ),
    "未知色彩空间 %r，回退到 %s。": "Unknown color space %r; falling back to %s.",
    "质量参数：%s": "Quality parameter: %s",
    "找不到 jsx 模板：{0}\n开发模式下请确认项目内存在 assets/jsx/export_batch.jsx；"
    "打包模式下请确认 spec 的 datas 已包含它。": (
        "jsx template not found: {0}\nIn development, make sure assets/jsx/export_batch.jsx "
        "exists in the project; in a packaged build, make sure the spec's datas include it."
    ),
    "Photoshop 自动导出依赖 Windows COM（win32com.client），仅在 Windows 上可用。\n"
    "本项目不做 macOS/Linux 实现。": (
        "Automatic Photoshop export relies on Windows COM (win32com.client) and is available on "
        "Windows only.\nThis project does not implement macOS/Linux support."
    ),

    # --- RAW：相机配置 / exiftool / ICC / 镜头 / 方向 / 预览 -----------------
    "%s 读取机内风格失败：%s": "%s failed to read the in-camera style: %s",
    "exiftool 报错（返回码 {0}）：{1}": "exiftool reported an error (exit code {0}): {1}",
    "exiftool 不可用：%s": "exiftool unavailable: %s",
    "执行：%s": "Running: %s",
    "exiftool 警告：%s": "exiftool warning: %s",
    "exiftool 返回码 {0}：{1}": "exiftool exit code {0}: {1}",
    "exiftool 输出：%s": "exiftool output: %s",
    "%s 读取 %s 大小失败：%s": "%s failed to read the size of %s: %s",
    "%s 命中 %s：%d 字节": "%s matched %s: %d bytes",
    "读取内嵌 XMP 失败 %s：%s": "Reading the embedded XMP failed %s: %s",
    "检测到 exiftool %s（%s）": "Detected exiftool %s (%s)",
    "%s 的 %s 有 %.1f MB，超过 %.1f MB 上限（大概率是未压缩大位图），跳过": (
        "%s's %s is %.1f MB, above the %.1f MB limit (most likely an uncompressed large bitmap); "
        "skipping"
    ),
    "%s 提取 %s 失败：%s": "%s failed to extract %s: %s",
    "磁盘上没有 sRGB.icc，使用 LittleCMS 内置 sRGB 定义（等价）": (
        "No sRGB.icc on disk; using LittleCMS's built-in sRGB definition (equivalent)"
    ),
    "ICC 头长度错误：{0}": "Wrong ICC header length: {0}",
    "使用磁盘上的 sRGB profile：%s（%d 字节）": (
        "Using the sRGB profile on disk: %s (%d bytes)"
    ),
    "生成 ICC profile 失败（程序仍可运行，sRGB 走内置定义）：%s": (
        "Generating the ICC profile failed (the app still runs; sRGB uses the built-in "
        "definition): %s"
    ),
    "预览图声明为 Adobe RGB(1998)（EXIF InteropIndex=R03），但找不到 {0}。\n已搜索：{1}\n"
    "这个 profile 受 Adobe 授权限制，本程序不能自带。\n两个选择：\n"
    "  1) 从 Photoshop 安装目录或系统色彩目录把它复制到 {2}（文件名保持 {3} 即可）；\n"
    "  2) 什么都不做 —— 程序会自动改用 rawpy 全解码，颜色是正确的，只是每张慢 1–5 秒。": (
        "The preview declares Adobe RGB(1998) (EXIF InteropIndex=R03) but {0} was not found.\n"
        "Searched: {1}\nThat profile is under Adobe's licensing terms, so this app cannot bundle "
        "it.\nTwo options:\n  1) Copy it from your Photoshop installation or the system color "
        "folder to {2} (keeping the file name {3});\n  2) Do nothing — the app falls back to a "
        "full rawpy decode, which is color-correct but takes 1–5 seconds longer per image."
    ),
    "ImageCms.profileToProfile 返回了 None": "ImageCms.profileToProfile returned None",
    "ICC 转换失败（源判定：%s），已退化为不做色彩变换：%s": (
        "ICC conversion failed (source judged as: %s); degrading to no color transform: %s"
    ),
    "找到 %s：%s": "Found %s: %s",
    "读取 sRGB profile 失败，改用 LittleCMS 内置定义：%s": (
        "Reading the sRGB profile failed; using LittleCMS's built-in definition: %s"
    ),
    "ICC 目录不可写，换下一个：%s（%s）": "ICC folder is not writable; trying the next one: %s (%s)",
    "已生成 ICC profile：%s（%d 字节）": "Generated ICC profile: %s (%d bytes)",
    "嵌入 ICC profile 解析失败，转用 EXIF 判定：%s": (
        "Parsing the embedded ICC profile failed; falling back to the EXIF judgement: %s"
    ),
    "写入 ICC 失败：%s（%s）": "Writing ICC failed: %s (%s)",
    "%s 读取镜头基线失败：%s": "%s failed to read the lens baseline: %s",
    "%s 读取方向失败：%s": "%s failed to read the orientation: %s",
    "%s：使用 rawpy 兜底解码（较慢）。原因：%s": (
        "%s: falling back to a full rawpy decode (slower). Reason: %s"
    ),
    "exiftool 提取失败 %s：%s": "exiftool extraction failed %s: %s",
    "%s：exiftool 不可用，无法读嵌入式预览。": (
        "%s: exiftool is unavailable, so the embedded preview cannot be read."
    ),
    "{0}：所有预览提取路径均失败。\n尝试记录：{1}\n"
    "可能原因：该 RAW 为冷门变体格式（如部分 NRW/SRF/SR2）、文件损坏，或相机型号过新尚未被 libraw 支持。": (
        "{0}: every preview extraction path failed.\nAttempts: {1}\nPossible causes: an uncommon "
        "RAW variant (e.g. some NRW/SRF/SR2), a damaged file, or a camera model too new for "
        "libraw."
    ),
    "%s 的 %s 解码失败：%s": "%s failed to decode %s: %s",
    "%s：预览来源=%s（%d×%d → 长边 %d），色彩判定=%s": (
        "%s: preview source=%s (%d×%d → long edge %d), color judgement=%s"
    ),
    "%s：预览声明 Adobe RGB 但缺 ICC profile，改走 rawpy 全解码。": (
        "%s: the preview declares Adobe RGB but its ICC profile is missing; switching to a full "
        "rawpy decode."
    ),

    # --- 蒙版与 XMP 读写 ----------------------------------------------------
    "已写入 %d 个几何蒙版：%s": "Wrote %d geometric masks: %s",
    "局部参数 {0} 没有在 acb/xmp/fields.py 里登记（group=\"mask\"），无法校验范围。"
    "请先补登记，不要在 masks.py 里再手写一份。": (
        "The local parameter {0} is not registered in acb/xmp/fields.py (group=\"mask\"), so its "
        "range cannot be validated. Register it there first instead of writing another copy in "
        "masks.py."
    ),
    "已清除 %d 个蒙版组（按调用方要求）": "Cleared %d mask groups (as requested by the caller)",
    "%s 中没有 rdf:Description 段（空 XMP）。": "%s has no rdf:Description block (empty XMP).",
    "读取 XMP 文件失败 {0}：{1}": "Reading the XMP file failed {0}: {1}",
    "{0} 中未找到 <x:xmpmeta> 或 <rdf:RDF> 根元素，该文件可能不是 XMP 侧车文件。": (
        "{0} contains no <x:xmpmeta> or <rdf:RDF> root element; it may not be an XMP sidecar."
    ),
    "{0} 的 <rdf:RDF> 没有闭合标签。": "{0} has an unclosed <rdf:RDF> tag.",
    "{0} 的 <rdf:RDF> 起始标签未闭合。": "{0} has an unterminated <rdf:RDF> start tag.",
    "{0} XML 解析失败：{1}": "{0} XML parse failed: {1}",
    "{0} 中缺少 <rdf:RDF>。": "{0} is missing <rdf:RDF>.",
    "{0} 的 <x:xmpmeta> 没有闭合标签。": "{0} has an unclosed <x:xmpmeta> tag.",
    "%s 中字段 %s 出现重复定义（%r vs %r），保留首个非空值。": (
        "Field %s appears twice in %s (%r vs %r); keeping the first non-empty value."
    ),
    "曲线 %s 的点 %r 不是二元组，已跳过。": "Curve %s point %r is not a pair; skipped.",
    "曲线 %s 的点 %r 含非数值，已跳过。": "Curve %s point %r contains a non-number; skipped.",
    "原 XMP 无 rdf:Description，已就地创建。": (
        "The original XMP has no rdf:Description; one was created in place."
    ),
    "已写出侧车 XMP：%s（应用 %d 个字段，跳过 %d 个）": (
        "Sidecar XMP written: %s (%d fields applied, %d skipped)"
    ),
    "已把 HasSettings 置为 True。": "Set HasSettings to True.",
    "XMP 文档没有根元素，无法序列化。": "The XMP document has no root element; cannot serialize.",
    "已补写 crs:ProcessVersion=%s（原文件没有该字段）": (
        "Added crs:ProcessVersion=%s (the original file had no such field)"
    ),
    "已将 WhiteBalance 由 %r 置为 Custom，使 Temperature/Tint 生效。": (
        "Changed WhiteBalance from %r to Custom so Temperature/Tint take effect."
    ),
    "备份原有 XMP 失败（继续写）：%s": "Backing up the original XMP failed (continuing): %s",
    "%s 已有值（%s），程序字段不覆盖": (
        "%s already has a value (%s); program fields do not overwrite it"
    ),
    "%s：新建侧车，按机内设置写入相机配置「%s」（%s）": (
        "%s: new sidecar; writing camera profile “%s” from the in-camera setting (%s)"
    ),
    "%s：新建侧车，但无法确定相机配置：%s": (
        "%s: new sidecar, but the camera profile could not be determined: %s"
    ),
    "已备份原有 XMP：%s": "Backed up the original XMP: %s",
    "已写入曲线控制点，同时把 ToneCurveName2012 置为 Custom。": (
        "Curve control points written, and ToneCurveName2012 set to Custom."
    ),
    "%s 已有侧车且已有相机配置，保持原值（%s）": (
        "%s already has a sidecar with a camera profile; keeping the original value (%s)"
    ),
    "%s 解析失败，已改写为 %s": "%s failed to parse; rewritten as %s",
    "写蒙版必须提供照片方向（orientation）：ACR 的坐标是存储帧，批量里每张照片的横竖方向可能不同，"
    "猜一个方向会把蒙版画到错的位置。（若坐标本来就是存储帧，请显式传 masks_frame='stored'）": (
        "Writing masks requires the photo orientation: ACR's coordinates live in the stored "
        "frame, and orientation differs from photo to photo in a batch, so guessing one puts the "
        "masks in the wrong place. (If the coordinates really are in the stored frame, pass "
        "masks_frame='stored' explicitly.)"
    ),
    "%s：已有侧车（本程序建的）缺相机配置，已补上「%s」（%s）": (
        "%s: the existing sidecar (created by this app) lacked a camera profile; added “%s” (%s)"
    ),
    "工作线程异常：%s\n%s": "Worker thread exception: %s\n%s",
    "拉取模型列表失败：%s\n%s": "Fetching the model list failed: %s\n%s",
    "已清空任务状态 {0} 条。": "Cleared {0} task records.",
    "发现 {0} 个训练样本对。": "Found {0} training sample pairs.",
    "样本 {0} 个，统计字段 {1} 个，风格规则 {2} 条。": (
        "{0} samples, statistics for {1} fields, {2} style rules."
    ),
    "样本不足，结果可能不稳定。\n建议至少提供 {0} 个样本后重新训练。": (
        "Not enough samples; results may be unstable.\nProvide at least {0} samples and train "
        "again."
    ),
    "[离线调试（演练）] 只处理前 {0} 张{1}。": (
        "[offline debug] Only the first {0} files are processed{1}."
    ),
    "标准输出无法切到 UTF-8（忽略，中文可能乱码）：%s": (
        "Could not switch standard output to UTF-8 (ignored; non-ASCII text may be garbled): %s"
    ),
    "items 中的元素不是对象：{0}": "Element in items is not an object: {0}",
    "无法识别的照片方向：{0}": "Unrecognised photo orientation: {0}",
    "照片方向是镜像（{0}），本程序暂不处理："
    "镜像会同时翻转蒙版坐标，写错位置比什么都不写更糟。": (
        "The photo orientation is mirrored ({0}); this app does not handle that: mirroring flips "
        "the mask coordinates as well, and writing them in the wrong place is worse than writing "
        "nothing."
    ),
    "字段 {0} 的类型 {1} 不支持属性格式化": (
        "Field {0} has type {1}, which cannot be formatted as an attribute"
    ),
    "字段 {0} 的类型 {1} 不支持属性解析": (
        "Field {0} has type {1}, which cannot be parsed from an attribute"
    ),
    "未注册的命名空间前缀：{0}": "Unregistered namespace prefix: {0}",
    "masks_frame 只能是 'display' 或 'stored'，收到 {0}": (
        "masks_frame must be 'display' or 'stored'; got {0}"
    ),
    "%s：已有侧车没相机配置，但不满足补写条件（已有值=%s 能确定=%s 本程序建的=%s）": (
        "%s: the existing sidecar has no camera profile but does not qualify for patching "
        "(existing value=%s, determinable=%s, created by this app=%s)"
    ),

    # --- 第三批：说明 / 原因 / 状态值（会出现在日志面板与状态栏）-------------
    "\n⚠ 服务端提了「图片」：很可能是这个模型**不接受图片输入**。\n"
    "请按以下任一种处理：\n"
    "  1) 换成支持视觉的模型（例如同为 DeepSeek 的 deepseek-flash）；\n"
    "  2) 在 config/models.yaml 的该服务商下加 model_overrides，把它的 supports_vision 设为 "
    "false —— 这样程序会走 caption 降级通道，并在界面上用红字告诉你精度会下降，而不是发一个"
    "必然失败的请求。": (
        "\n⚠ The server mentioned “image”: this model most likely does not accept image "
        "input.\nDo one of the following:\n"
        "  1) Switch to a vision-capable model (e.g. deepseek-flash, also from DeepSeek);\n"
        "  2) Add model_overrides under this provider in config/models.yaml and set its "
        "supports_vision to false — the app then falls back to the caption channel and tells you "
        "in red that accuracy drops, instead of sending a request that is bound to fail."
    ),
    "\n提示：Photoshop 可能正在显示对话框（如授权/更新提示）或正忙。"
    "请手动打开 Photoshop，关掉所有对话框后重试；或直接使用输出目录下的 run_export.bat 手动执行。": (
        "\nTip: Photoshop may be showing a dialog (licence/update) or is busy. Open Photoshop "
        "manually, close every dialog and retry; or run run_export.bat from the output folder by "
        "hand."
    ),
    "\n该响应里有 reasoning_content，说明思考模式开着。": (
        "\nThe response carries reasoning_content, so thinking mode is on."
    ),
    "\n超时提示：该模型（思考模型尤甚）或当前并发下响应较慢。"
    "可在模型条目里把 request_timeout_s 调大（如 180），或把并发工人数调小。": (
        "\nTimeout tip: this model (thinking models especially) or the current concurrency is "
        "slow. Raise request_timeout_s for the model entry (e.g. 180), or lower the number of "
        "parallel workers."
    ),
    "\n（本批提示词与上述风格规则冲突时，以本批提示词为准）": (
        "\n(If the batch prompt conflicts with the style rules above, the batch prompt wins.)"
    ),
    "        winget install exiftool   或   choco install exiftool": (
        "        winget install exiftool   or   choco install exiftool"
    ),
    "        在「用户变量」里选中 Path → 编辑 → 新建 → 粘贴目录路径 → 确定。": (
        "        Under “User variables” select Path → Edit → New → paste the folder path → OK."
    ),
    "        程序启动时会自动优先使用它。": (
        "        the app prefers that copy automatically when it starts."
    ),
    "        解压后把 exiftool.exe 放在本程序 exe 的同级目录（或 _internal\\ 目录）即可，": (
        "        after unzipping, put exiftool.exe next to this app's exe (or in _internal\\) —"
    ),
    "        设置 → 系统 → 系统信息 → 高级系统设置 → 环境变量 →": (
        "        Settings → System → About → Advanced system settings → Environment Variables →"
    ),
    "        重开程序后生效。验证方法：新开 PowerShell 执行 exiftool -ver。": (
        "        restart the app to apply. To verify, run exiftool -ver in a new PowerShell."
    ),
    "  - DNG 内嵌 XMP 读写：无法完成（DNG 的 XMP 在文件内部，必须用 exiftool 改写）。": (
        "  - DNG embedded XMP read/write: impossible (a DNG keeps its XMP inside the file, which "
        "only exiftool can rewrite)."
    ),
    "  - 预览提取：降级为 rawpy 全解码，可正常完成，但每张约慢 1–5 秒；": (
        "  - Preview extraction: falls back to a full rawpy decode, which works but is 1–5 "
        "seconds slower per image;"
    ),
    "  方法 1（推荐，免安装）：从 https://exiftool.org 下载 Windows 可执行版压缩包，": (
        "  Option 1 (recommended, no install): download the Windows executable zip from "
        "https://exiftool.org,"
    ),
    "  方法 2（加入 PATH）：把解压出的 exiftool.exe 所在目录加入系统 PATH：": (
        "  Option 2 (add to PATH): add the folder containing exiftool.exe to the system PATH:"
    ),
    "  方法 3（包管理器）：用 winget 或 chocolatey 安装，": (
        "  Option 3 (package manager): install with winget or chocolatey,"
    ),
    "(空)": "(empty)",
    ", 永久跳过": ", permanently skipped",
    "429 限流": "429 rate limit",
    "<脚本目录>": "<script folder>",
    "OpenAI 兼容（POST {base_url}/chat/completions，model 在请求体里）": (
        "OpenAI-compatible (POST {base_url}/chat/completions, model in the request body)"
    ),
    "PATH 中未找到 exiftool，且程序目录下没有 exiftool.exe": (
        "exiftool was not found in PATH and there is no exiftool.exe next to the app"
    ),
    "RAW 里没有可识别的机内风格标签，已保持 ACR 默认": (
        "No recognisable in-camera style tag in the RAW; keeping the ACR default"
    ),
    "Windows 安装方法（任选其一）：": "How to install on Windows (pick one):",
    "XMP 中转临时文件": "XMP staging temp files",
    "XMP 里的相机配置已按相机机内设置写好：": (
        "The camera profile in the XMP was written from the in-camera setting:"
    ),
    "exiftool 不可用（未安装或未检测到）": "exiftool unavailable (not installed or not detected)",
    "exiftool 不可用，按未旋转处理": "exiftool unavailable; treating the photo as unrotated",
    "exiftool 不可用，无法改写 DNG 内嵌 XMP": (
        "exiftool unavailable; cannot rewrite the embedded DNG XMP"
    ),
    "exiftool 不可用，无法读取机内配置": (
        "exiftool unavailable; cannot read the in-camera configuration"
    ),
    "exiftool: 未找到 PreviewImage / JpgFromRaw / ThumbnailImage": (
        "exiftool: no PreviewImage / JpgFromRaw / ThumbnailImage found"
    ),
    "rawpy postprocess(output_color=sRGB 默认)": "rawpy postprocess (output_color=sRGB default)",
    "上次已完成，跳过 API 请求（断点续跑）": (
        "Already finished last time; skipping the API request (resume)"
    ),
    "不在失败清单中（--only-failed 模式）": "not in the failure list (--only-failed mode)",
    "不在清理白名单内": "not in the cleanup whitelist",
    "不支持（走 caption 降级）": "not supported (falls back to caption)",
    "不支持（走提示词+本地强校验降级）": (
        "not supported (falls back to prompt + strict local validation)"
    ),
    "不需要": "not needed",
    "任务被手动停止：已完成的处理结果已保留，可随时点「续跑」继续。": (
        "Task stopped manually: finished results were kept; click Resume at any time to continue."
    ),
    "使用你填写的提示词（与所选风格叠加，冲突以提示词为准）": (
        "Using your prompt (stacked with the selected style; the prompt wins on conflict)"
    ),
    "使用你填写的提示词（未选风格，按中性约束处理）": (
        "Using your prompt (no style selected; neutral constraints apply)"
    ),
    "使用你填写的提示词（风格为「AI自主决策」，不加偏好限制）": (
        "Using your prompt (style is “AI Autopilot”, no preference limits applied)"
    ),
    "修复重试前": "before the repair retry",
    "内嵌设置里没有镜头开关，按 ACR 默认": (
        "the embedded settings have no lens switches; using the ACR default"
    ),
    "分析前": "before analysis",
    "分离色调": "Split Toning",
    "原有 XMP 无法解析，未参与相机配置判断": (
        "the existing XMP cannot be parsed; it was not used to decide the camera profile"
    ),
    "参数字典中没有任何可用字段": "the parameter object contains no usable fields",
    "发送前": "before sending",
    "名称示例：": "Name examples:",
    "响应里没有 params（键缺失或为 null）": (
        "the response has no params (the key is missing or null)"
    ),
    "完成（结果未知，请查看 ps_log.txt）": (
        "Finished (result unknown; check ps_log.txt)"
    ),
    "密钥为空。": "The key is empty.",
    "局部调整：": "Local adjustments:",
    "嵌入 ICC（img.info['icc_profile']）": "Embedded ICC (img.info['icc_profile'])",
    "嵌入式预览不可用，已回退 rawpy 全解码（该操作较慢）": (
        "the embedded preview is unusable; fell back to a full rawpy decode (slower)"
    ),
    "已停止：**不会再发送任何新请求**（重试与拆单重发也一并停下）。"
    "已经发出的那几组仍会收回来 —— 它们的请求已经花过钱，已经分析完成的图会照常写出 XMP": (
        "Stopped: no new requests will be sent (retries and per-image resends stop too). "
        "Requests already sent are still collected — they were paid for, and images that were "
        "already analysed get their XMP written as usual."
    ),
    "已停止：训练未完成，风格档案未保存。": (
        "Stopped: training did not finish and the style profile was not saved."
    ),
    "已写回内嵌 XMP": "Embedded XMP written back",
    "已去掉包裹的引号": "removed surrounding quotes",
    "已去掉多余的「Bearer 」前缀": "removed the redundant “Bearer ” prefix",
    "已去掉首尾空白字符": "trimmed leading/trailing whitespace",
    "已尝试的查找位置：": "Locations already searched:",
    "已按要求跳过 Photoshop 调用（可手动运行 run_export.bat 完成导出）": (
        "Photoshop was skipped as asked (you can run run_export.bat to export manually)"
    ),
    "已有侧车且已有相机配置，保持原值": (
        "a sidecar already exists with a camera profile; keeping the original value"
    ),
    "已有侧车，相机配置保持原值": (
        "a sidecar already exists; keeping the original camera profile"
    ),
    "已有内嵌 XMP，相机配置保持原值": (
        "embedded XMP already exists; keeping the original camera profile"
    ),
    "已选择内置「AI自主决策」：不套用任何个人风格，由模型按每张图自行判断"
    "（字段白名单、取值范围与机器字段禁令照旧生效）": (
        "Built-in “AI Autopilot” selected: no personal style is applied; the model decides per "
        "image (the field whitelist, value ranges and machine-field bans still apply)"
    ),
    "影响范围：": "Impact:",
    "径向渐变": "Radial Gradient",
    "批内统计中位数：亮度 {luma:.1f}、对比度 {contrast:.1f}、估算色温 {cct:.0f}K、"
    "饱和度 {sat:.1f}": (
        "Batch medians: luma {luma:.1f}, contrast {contrast:.1f}, estimated color temperature "
        "{cct:.0f}K, saturation {sat:.1f}"
    ),
    "拆单重发前": "before the per-image resend",
    "拒绝删除数据目录本身": "refusing to delete the data folder itself",
    "支持": "supported",
    "断点与失败记录": "checkpoints and failure records",
    "无": "none",
    "无 profile，按 sRGB 解释": "no profile; interpreted as sRGB",
    "无临时文件需要清理": "no temp files to clean",
    "无侧车也无内嵌设置，按 ACR 默认（8/8 样本与 crd: 基线一致）": (
        "no sidecar and no embedded settings; using the ACR default (consistent with 8/8 samples "
        "and the crd: baseline)"
    ),
    "是符号链接或目录联接，拒绝删除": "it is a symlink or junction; refusing to delete",
    "服务端返回了空的 content。官方文档（DeepSeek《JSON Output》注意事项）提到使用 JSON Output "
    "时有概率如此；另一个常见原因是 max_tokens 不够（思考模式的推理 token 也算在其中）。": (
        "The server returned empty content. DeepSeek's own JSON Output notes mention this can "
        "happen with JSON output; another common cause is a max_tokens limit that is too low "
        "(reasoning tokens count towards it in thinking mode)."
    ),
    "未写明": "not stated",
    "未填写提示词：按所选风格的规则执行（含 HSL / 分离色调 / 颜色分级等特征面板）": (
        "No prompt given: the selected style's rules are followed (including HSL / Split Toning / "
        "Color Grading and other characteristic panels)"
    ),
    "未指定": "not specified",
    "未提供": "not provided",
    "未检测到 exiftool。程序已降级为 rawpy 全解码提取预览（速度较慢）。": (
        "exiftool was not detected. The app falls back to a full rawpy decode for previews "
        "(slower)."
    ),
    "未知": "unknown",
    "未知错误": "unknown error",
    "未知（看服务端原文）": "unknown (see the server's own text)",
    "未能确定": "cannot be determined",
    "未记录到具体原因": "no specific reason was recorded",
    "本次运行的脚本目录（软件数据目录下的 runs 子目录）": (
        "this run's script folder (the runs subfolder inside the app data folder)"
    ),
    "校验失败：": "Validation failed:",
    "校验通过（含警告）：": "Validation passed (with warnings):",
    "模型未提出任何调整（params 为空对象），按「无需调整」处理": (
        "the model proposed no adjustments (params is an empty object); treating it as “no change "
        "needed”"
    ),
    "用户调整最频繁的字段（字段: 出现样本数）：": (
        "Fields the user adjusted most often (field: number of samples):"
    ),
    "用户选择": "user choice",
    "画笔": "Brush",
    "画笔没有有效落点，已退化为「画一个中心点」": (
        "the brush has no valid dabs; degrading to “paint a single centre point”"
    ),
    "白名单解析失败，保守起见拒绝删除": (
        "whitelist resolution failed; refusing to delete to stay safe"
    ),
    "相机校准": "Camera Calibration",
    "离线调试数据未通过校验器，这属于代码缺陷而非用户输入问题：": (
        "The offline debug data failed the validator — that is a code defect, not a user input "
        "problem:"
    ),
    "离线调试：未发出任何网络请求，token 用量为 0": (
        "Offline debug: no network request was made, token usage is 0"
    ),
    "程序按公开参数自算的等效 profile（输入侧兜底用）": (
        "an equivalent profile computed by the app from public parameters (input-side fallback)"
    ),
    "程序运行日志": "application log",
    "线性渐变": "Linear Gradient",
    "线性渐变 FullY": "Linear Gradient FullY",
    "给出了 Temperature/Tint，将自动把 WhiteBalance 置为 Custom 使其生效": (
        "Temperature/Tint were given, so WhiteBalance is set to Custom to make them take effect"
    ),
    "缓存命中": "cache hit",
    "缩略图来源可信度低，细节判断不可靠": (
        "the thumbnail source has low confidence, so fine-detail judgements are unreliable"
    ),
    "缩略图缓存": "thumbnail cache",
    "蒙版:": "Masks:",
    "被调用者拒绝": "rejected by the callee",
    "训练只发一次归纳请求，含读超时重试与解析修复": (
        "training sends a single summarisation request, including read-timeout retries and parse "
        "repairs"
    ),
    "训练未完成：风格档案未保存。": (
        "Training did not finish: the style profile was not saved."
    ),
    "设计上不需要（输出侧只把配置文件名称交给 Photoshop，用你本机那份）": (
        "not needed by design (the output side only passes the profile name to Photoshop, which "
        "uses your own copy)"
    ),
    "该图仅有小尺寸缩略图可用，AI 对细节类参数的判断可靠性下降，"
    "建议人工复核锐化与降噪相关设置。": (
        "Only a small thumbnail is available for this image, so the AI's judgement of "
        "detail-related parameters is less reliable; check the sharpening and noise settings by "
        "hand."
    ),
    "该文件已有蒙版组（不是本程序写的），本次跳过蒙版写入（不覆盖你的手工蒙版）": (
        "This file already has mask groups that this app did not write; mask writing was "
        "skipped (your hand-made masks are not overwritten)."
    ),
    "该文件的预览声明为 Adobe RGB 但系统里找不到它的 ICC profile，"
    "已改用 rawpy 全解码以避免按 sRGB 误判颜色（每张慢 1–5 秒）。": (
        "This file's preview declares Adobe RGB but its ICC profile is missing, so a full rawpy "
        "decode is used to avoid judging the colors as sRGB by mistake (1–5 seconds slower per "
        "image)."
    ),
    "该照片": "this photo",
    "该照片的侧车": "this photo's sidecar",
    "读超时": "read timeout",
    "运行目录（脚本与日志）": "run folders (scripts and logs)",
    "这张照片没有 as-shot 色温标签，白平衡偏移统计会跳过它": (
        "This photo has no as-shot color temperature tag; the white-balance offset statistics "
        "skip it."
    ),
    "这张照片没有可用的镜头基线（侧车与 crd: 里都没有相关字段），镜头开关保持原样": (
        "This photo has no usable lens baseline (no related fields in the sidecar or crd:), so "
        "the lens switches are left as they are."
    ),
    "退避等待中": "backing off",
    "颜色分级": "Color Grading",
    "风格档案": "style profile",
    "验证": "verify",
    "高保真模式：已跳过嵌入式预览": "high-fidelity mode: the embedded preview was skipped",
    "（ACR 打开后会直接使用它，不再是「Adobe 标准」）": (
        "(ACR uses it directly when it opens the file, instead of “Adobe Standard”)"
    ),
    "（未指定）": "(not specified)",

    # --- 第四批：exiftool 探测不再把"超时"当成"没装"（2026-09-26 实测）-----
    "启动耗时：从进程启动到界面就绪 %.2f 秒": (
        "Startup time: %.2f s from process start to the window being ready"
    ),
    "exiftool（检测中…）": "exiftool (checking…)",
    "exiftool 未响应（启动探测超时），已转入后台重试。": (
        "exiftool did not respond (startup probe timed out); retrying in the background."
    ),
    "exiftool 后台探测异常：%s": "Background exiftool probe failed: %s",
    "开始处理前重试一次 exiftool 探测（上次是超时）": (
        "Retrying the exiftool probe once before processing (the last attempt timed out)"
    ),
    "正在重启：%s": "Restarting: %s",
    "正在重启…": "Restarting…",
    # --- 第五批：启动清理（侧车备份保留 7 天）------------------------------
    "过期的侧车备份": "expired sidecar backup",
    "启动清理（备份）：{0}": "Startup cleanup (backups): {0}",
    "启动清理（备份）出错（已忽略，不影响启动）：%s": (
        "Startup cleanup (backups) failed (ignored; startup is unaffected): %s"
    ),
    "启动清理：%s 已设置，跳过。": (
        "Startup cleanup: %s is set; skipping."
    ),
    "exiftool 搜索诊断：which(exiftool.exe)=%s、which(exiftool)=%s、"
    "PATH 长度=%d、含 exiftool 目录=%s、cwd=%s": (
        "exiftool search diagnostics: which(exiftool.exe)=%s, which(exiftool)=%s, "
        "PATH length=%d, contains exiftool folder=%s, cwd=%s"
    ),
    "exiftool 探测超时：argv=%s 耗时 %.1fs": (
        "exiftool probe timed out: argv=%s took %.1fs"
    ),
    "界面语言已改为 {0}，重新启动后生效。\n\n"
    "点「立即重启」后本程序会自动关闭并重新打开；通常几秒，\n"
    "若系统此刻较忙（或刚退出旧进程）可能要十几秒，请稍候。": (
        "The interface language was changed to {0}; it takes effect after a restart.\n\n"
        "After you click “Restart now”, this app closes and opens again automatically. "
        "That usually takes a few seconds — if the system is busy, or the old process just "
        "exited, it may take ten-odd seconds. Please wait."
    ),

    # --- 第六批：TIFF / HEIF 支持（V1.1.0）--------------------------------
    "HEIF 支持已就绪（pillow-heif %s，libheif %s）": (
        "HEIF support ready (pillow-heif %s, libheif %s)"
    ),
    "HEIF 支持不可用（%s）：%s": "HEIF support unavailable (%s): %s",
    "读取 HEIF（.heic/.heif/.hif）需要 {0}。\n"
    "安装：python -m pip install -r requirements.txt（或 pip install {0}）后重启本程序。\n"
    "在此之前，HEIF 文件会被跳过（其它格式不受影响）。": (
        "Reading HEIF (.heic/.heif/.hif) requires {0}.\n"
        "Install it with: python -m pip install -r requirements.txt (or pip install {0}), then "
        "restart this app.\n"
        "Until then, HEIF files are skipped (other formats are unaffected)."
    ),
    "HEIF/HDR：PQ → SDR（{0:g} nit 白点 + 平滑肩部，不裁剪）": (
        "HEIF/HDR: PQ → SDR ({0:g} nit white point + smooth shoulder, no clipping)"
    ),
    "HEIF/HDR：HLG → SDR（BT.2100 OOTF + 平滑肩部，不裁剪）": (
        "HEIF/HDR: HLG → SDR (BT.2100 OOTF + smooth shoulder, no clipping)"
    ),
    "HEIF：未知传输特性 {0}，按 sRGB 解释": (
        "HEIF: unknown transfer characteristic {0}; interpreting as sRGB"
    ),
    "该 HEIF 声明的传输特性 {0} 不认识，色彩按 sRGB 解释。": (
        "This HEIF declares transfer characteristic {0}, which is not recognised; colours are "
        "interpreted as sRGB."
    ),
    "该 HEIF 声明的原色 {0} 不是 BT.2020/BT.709，按 sRGB 解释。": (
        "This HEIF declares primaries {0}, which are neither BT.2020 nor BT.709; colours are "
        "interpreted as sRGB."
    ),
    "无 ICC/nclx，按 sRGB 解释": "no ICC/nclx; interpreted as sRGB",
    "{0}：解码失败（{1}）。\n该文件可能已损坏，或使用了本程序尚不支持的变体。": (
        "{0}: decoding failed ({1}).\nThe file may be damaged, or use a variant this app does not "
        "support yet."
    ),
    "{0}：无法写出中转 TIFF（{1}）。": "{0}: could not write the intermediate TIFF ({1}).",
    "%s：已解码为 sRGB TIFF（%d×%d，%s）": "%s: decoded to an sRGB TIFF (%d×%d, %s)",
    "{0} 的相机设置无法由 Photoshop/ACR 直接读取（实测），已改为「解码成 TIFF → 写入设置 → "
    "由 Photoshop 出片」；原文件不会被改动。": (
        "{0}'s camera settings cannot be read by Photoshop/ACR directly (measured), so the file "
        "is decoded to a TIFF, the settings are written into that TIFF, and Photoshop exports "
        "from it. The original file is not modified."
    ),
    "{0}：HEIF 不写旁侧 XMP。Photoshop/ACR 对 HEIC 里的相机设置"
    "（内嵌与两种旁侧命名）实测一律不读，所以 HEIF 走"
    "「解码成 TIFF → 写入设置 → 出片」这条通道"
    "（见 acb/raw/decode.py 与 constants.HEIF_EXTENSIONS）。": (
        "{0}: no sidecar XMP is written for HEIF. Photoshop/ACR was measured to ignore camera "
        "settings in HEIC entirely (embedded, plus both sidecar naming schemes), so HEIF goes "
        "through “decode to TIFF → write settings → export” (see acb/raw/decode.py and "
        "constants.HEIF_EXTENSIONS)."
    ),
    "{0}：HEIF 的相机设置无法被 Photoshop/ACR 直接读取（实测），本次已解码为 TIFF 中转件"
    "并通过它出片；原文件未被改动。": (
        "{0}: Photoshop/ACR cannot read camera settings from HEIF (measured), so this run decoded "
        "it to an intermediate TIFF and exported from that. The original file is unchanged."
    ),
    "中转 TIFF 不写相机配置（ACR 的配置列表只对 RAW 有意义）": (
        "no camera profile is written into the intermediate TIFF (ACR's profile list only applies "
        "to raw files)"
    ),
    "%s：HEIF → 中转 TIFF %s（已写入 %d 个字段）": (
        "%s: HEIF → intermediate TIFF %s (%d fields written)"
    ),
    "%s 局部调整：%d 个蒙版，中转 TIFF 方向 %s（显示时旋转 %d°）": (
        "%s local adjustments: %d masks, intermediate TIFF orientation %s (rotated %d° for "
        "display)"
    ),
    "%s 读内嵌 XMP 失败：%s": "%s: failed to read the embedded XMP: %s",

    # --- 第七批：TIFF/HEIF 高光与色彩空间（2026-09-26）------------------
    "%s：方向的取值是 %s（不在 1–8 内），按未旋转处理": (
        "%s: the orientation value is %s (outside 1–8); treating it as unrotated"
    ),
    "EXIF Orientation 无效取值（{0}），按未旋转处理": (
        "EXIF Orientation holds an invalid value ({0}); treating it as unrotated"
    ),
    "该 HEIF 声明的原色 {0} 不是 BT.2020/BT.709/P3，按 sRGB 解释。": (
        "This HEIF declares primaries {0}, which are neither BT.2020, BT.709 nor P3; "
        "colours are interpreted as sRGB."
    ),
    "该 HEIF 声明的是 DCI-P3（影院参考：DCI 白点 + γ=2.6），"
    "它与 Display P3 不是同一种 —— 原色相同，但白点与传输曲线都不同。"
    "本程序按声明解；若这个文件其实是 Display P3 却被标成 DCI-P3，"
    "画面会略微偏绿、中间调偏暗。": (
        "This HEIF declares DCI-P3 (the cinema reference: DCI white point + γ=2.6), which is "
        "NOT the same as Display P3 — same primaries, but different white point and transfer "
        "curve. This app follows the declaration; if the file is really Display P3 but was "
        "tagged DCI-P3, the image will look slightly green and midtones a bit darker."
    ),
    "；⚠ {0} 项未能转换为目标色彩空间（{1} 未安装？）": (
        "; ⚠ {0} item(s) could not be converted to the target color space ({1} not installed?)"
    ),

    # --- 第八批：白平衡以 as-shot 为基准（2026-09-26 人像肤色偏红黄）------
    "机身 as-shot 白平衡：读不到（这种情况请不要给 Temperature/Tint）": (
        "In-camera as-shot white balance: unavailable (in this case do NOT emit Temperature/Tint)"
    ),
    "机身 as-shot 色温 {0}K（这是这张图的起点，Temperature 要相对它判断）": (
        "In-camera as-shot color temperature {0}K (this is the starting point for this photo; "
        "judge Temperature relative to it)"
    ),
    "机身 as-shot 色温 {0}K、Tint {1}（这是这张图的起点，Temperature/Tint 都要相对它判断）": (
        "In-camera as-shot color temperature {0}K, Tint {1} (this is the starting point for this "
        "photo; judge both Temperature and Tint relative to it)"
    ),
    "白平衡：{0} 张图的 Temperature/Tint 被程序侧护栏动过"
    "（基准是每张图自己的 as-shot ＋ 这套风格训练出的偏移范围）": (
        "White balance: the guardrail changed Temperature/Tint on {0} photo(s) "
        "(the basis is each photo's own as-shot plus the offset range learned from this style)"
    ),
    "；其中 {0} 张读不到 as-shot 色温，已整组跳过白平衡（绝对色温猜错就是整张偏色，所以宁可不改）": (
        "; for {0} of them the as-shot color temperature was unreadable, so white balance was "
        "skipped entirely (a wrong absolute temperature tints the whole photo, so doing nothing "
        "is safer)"
    ),
    "。逐张细节见「画像」日志里的「护栏」段。": (
        ". See the “guardrail” part of each photo's profile log line for per-photo details."
    ),

    # --- 第九批：Display P3 自制等效 profile 的自动安装（2026-09-26）--------
    "没有可写的 Adobe 色彩目录": "No writable Adobe color directory",
    "已存在名为「{0}」的 profile，无需安装（{1}）": (
        "A profile named “{0}” is already installed; nothing to do ({1})"
    ),
    "自制 Display P3 等效 profile 已就位（{0}）": (
        "The self-built Display P3 equivalent profile is already in place ({0})"
    ),
    "{0} 已存在同名但内容不同的文件，已跳过（不覆盖用户文件）": (
        "{0} already holds a file with this name but different content; skipped "
        "(user files are never overwritten)"
    ),
    "写入 {0} 失败：{1}": "Failed to write {0}: {1}",
    "已安装自制 Display P3 等效 profile：{0}（{1} 字节；"
    "Photoshop 下次启动即可按「{2}」使用）": (
        "Installed the self-built Display P3 equivalent profile: {0} ({1} bytes; the next "
        "Photoshop launch can use it as “{2}”)"
    ),
    "已安装": "installed",
    "未安装": "not installed",
    "导出时若选它，程序会自动安装自制的等效 profile": (
        "if you pick it for export the app installs its own equivalent profile automatically"
    ),
    "读取 %s 失败，改为当场构造：%s": (
        "Failed to read %s; building the profile on the spot instead: %s"
    ),
    "未安装成功，详见日志": "install failed, see the log for details",
    "程序已把自制的 Display P3 等效 profile 装到 Adobe 的色彩目录（{0}）。"
    "Photoshop 只在**启动时**扫描色彩目录：若它当时已经在运行，"
    "请重启 Photoshop 后重新导出，色彩空间这次才会真正转换。": (
        "The app installed its self-built Display P3 equivalent profile into Adobe's color "
        "directory ({0}). Photoshop only scans color directories **at startup**: if it was "
        "already running, restart Photoshop and export again — only then will the color space "
        "really be converted."
    ),

    # --- 第十批：白平衡基准登记表 + “方向不许叠加”（2026-09-26）-------------
    "这份文件没有可信的 as-shot 基准（参数是本程序写的）": (
        "This file has no trustworthy as-shot baseline (the settings were written by this app)"
    ),
    "沿用首次记录的 as-shot（避免每次重跑都往同一方向多推一次）": (
        "Keeping the as-shot recorded on first sight (so repeated runs do not keep pushing the "
        "same direction)"
    ),
    "文件的白平衡已被其它工具/你本人改动，按当前值作为基准": (
        "The file's white balance was changed by another tool or by you; using the current value "
        "as the baseline"
    ),
    "读不到 as-shot（这份文件本身没有白平衡信息）": (
        "The as-shot is unreadable (this file carries no white-balance information)"
    ),
    "%s：文件里的白平衡是本程序上次写的（%s），基准仍按 %s 计算。": (
        "%s: the white balance in this file is what this app wrote last time (%s); the baseline "
        "is still computed from %s."
    ),
    "白平衡基准登记表损坏，已忽略：%s（%s）": (
        "The white-balance baseline registry is corrupt and was ignored: %s (%s)"
    ),
    "写入白平衡基准登记表失败 %s：%s": (
        "Failed to write the white-balance baseline registry %s: %s"
    ),
    "；⚠ 这张图的观感已经比 as-shot 更暖（缩略图估算 {0}K）→ "
    "不要再给偏暖的 Temperature，要动就只能往中性/偏冷走": (
        "; ⚠ this photo already looks warmer than its as-shot (thumbnail estimate {0}K) → do NOT "
        "push Temperature warmer; if anything, move it toward neutral or cooler"
    ),

    # --- 第十一批：蒙版重跑替换 + 登记表 + 预览摆正（2026-09-26）--------------
    "%s 读取方向失败，按未旋转处理：%s": (
        "Failed to read the orientation of %s; treating it as unrotated: %s"
    ),
    "%s：预览来源=%s（%d×%d → 长边 %d），色彩判定=%s%s": (
        "%s: preview source=%s (%d×%d → long edge %d), colour assessment=%s%s"
    ),
    "，已按方向摆正（{0}）": (
        ", rotated upright to match the display orientation ({0})"
    ),
    "，方向标签是 {0} 但预览已是正的，未旋转": (
        ", the orientation tag says {0} but the preview is already upright, so it was not rotated"
    ),
    "写入蒙版登记表失败 %s：%s": (
        "Failed to write the mask registry %s: %s"
    ),
    "蒙版登记表损坏，已忽略：%s（%s）": (
        "The mask registry is corrupt and was ignored: %s (%s)"
    ),
    "已移除本程序上一轮写入的 %d 个蒙版（保留你手工画的那些）": (
        "Removed the %d masks this app wrote in the previous round (your hand-made ones are kept)"
    ),
    "原有的蒙版组全部由本程序写入，已整组替换为本次的 %d 个": (
        "Every mask in the existing group was written by this app, so the whole group was "
        "replaced with this round's %d mask(s)"
    ),
    "原有的蒙版组里既有本程序写的也有你手工画的：已换成本程序的 %d 个，保留你的 %d 个": (
        "The existing mask group holds both this app's masks and yours: this app's %d mask(s) "
        "were replaced, your %d were kept untouched"
    ),
}
