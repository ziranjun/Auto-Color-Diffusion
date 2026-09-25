# -*- coding: utf-8 -*-
"""ModelAdapter —— 屏蔽模型能力差异的适配层（硬约束 #4 / #19）。

两条通道
--------
A. supports_json_schema = true
   使用 response_format = {"type": "json_schema", ...} 让服务端保证输出格式。
   这是首选通道，格式失败率最低。

B. supports_json_schema = false（降级通道）
   不使用 response_format，改为：
     - 把 schema 的结构要求以自然语言写进 system 提示词；
     - 要求"只输出 JSON，不要任何解释文字"；
     - 本地做**强校验**（pydantic + 字段白名单），失败则把错误原文回灌给模型
       让它自行修正，最多再试 2 次（硬约束 #5）。
   把错误回灌（self-repair）比单纯重试同一个提示词有效得多：
   模型看到"字段 crs:Exposure2012 取值 500 越界"就能改对，
   而重复发同样的请求往往得到同样的错误。

视觉能力
--------
supports_vision = false 时（硬约束 #19）：
    不发送图片，改用本地 numpy 统计量生成的 caption 文本（见 raw/stats.py），
    并且**不混用**：既然看不到图，就不该让它输出 HSL 分区、蒙版等
    需要画面语义的参数——这是通过专门的 caption system 提示词来约束的。
    此通道精度明显低于视觉模型，仅作可用性兜底，日志会明确提示。

训练模式
--------
temperature = 0.2（硬约束 #19），低于输出模式，保证偏好统计稳定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..config import ModelSpec
from ..constants import MAX_OUTPUT_TOKENS_CEILING, MAX_PARSE_RETRIES, TRAIN_TEMPERATURE
from ..xmp import masks as masks_mod
from ..errors import (
    EmptyContentError,
    QuotaExceededError,
    SchemaValidationError,
    StopRequestedError,
    TruncatedOutputError,
)
from ..logging_setup import get_logger
from ..xmp import validator as V
from . import prompts as P
from . import schema as S
from .client import ApiClient, RequestBudget, encode_image_data_url, extract_json_object

log = get_logger("ai.adapter")


def _strict_keyword_subset(schema: dict, dialect: Any = None) -> dict:
    """把 schema 剥成**该端点**支持的子集（真正的实现在 ai/schema.py）。

    单独包一层的理由：「哪些关键字能用」属于 schema 模块的知识，
    adapter 只负责「什么时候需要剥」—— 两件事分开才好改。

    方言逐家一份（见 ai/schema.py::SchemaDialect）：全局一套会让
    "某一家不认的关键字"连带削弱其他家的约束能力。
    """
    from .schema import DEFAULT_SCHEMA_DIALECT, strict_keyword_subset

    return strict_keyword_subset(
        schema, dialect if dialect is not None else DEFAULT_SCHEMA_DIALECT
    )


@dataclass
class PreviewItem:
    """一张待分析的图片及其上下文。"""

    file_id: str
    filename: str
    path: Path
    preview_jpeg: bytes
    preview_source: str = ""
    preview_low_confidence: bool = False
    caption: str | None = None
    # 训练模式专用
    training_params: dict[str, Any] | None = None
    training_curves: dict[str, Any] | None = None
    training_excluded: list[str] | None = None
    training_mask_usage: str | None = None
    # 本图的本地实测指标（亮度/对比/饱和度/估算色温…）。
    # 给**视觉模型**也发一份：它的作用是让模型有"这张图现在长什么样"的量化锚点，
    # 从而按这张图重新判断调整量，而不是套用风格统计里的历史数值（步骤 3）。
    metrics: str | None = None


@dataclass
class ItemResult:
    """单张图的分析结果。"""

    file_id: str
    params: dict[str, Any] = field(default_factory=dict)
    curves: dict[str, list] = field(default_factory=dict)
    # 局部调整意图（步骤 5）。几何是**显示帧**坐标，写入前由程序按每张照片
    # 自己的 EXIF 方向换算成 ACR 的存储帧（见 acb/xmp/masks.py）。
    masks: list[dict] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    # --- 写出结果（由 pipeline/output_mode 在 XMP 写成功后回填）--------------
    # 必须在这里正式声明，而不是让调用方动态挂属性。
    # 原因：job.JobState.mark_done 用 `getattr(result, "xmp_target", "")` 读取，
    # 如果字段名拼错或没赋上，会**静默退化为空字符串**——
    # 表现是"XMP 明明写好了，但 manifest 里的 xmp 路径是空的"，
    # 这类问题在日志里完全看不出来。声明成 dataclass 字段后，
    # 拼错字段名会立刻在静态检查与 __init__ 处暴露。
    xmp_target: Path | None = None
    xmp_mode: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


class AdapterLike(Protocol):
    """adapter 的最小接口契约（ModelAdapter 与 OfflineAdapter 都满足）。

    为什么用 Protocol 而不是抽一个基类：`ModelAdapter.__init__` 会立刻构造
    真实的 ApiClient，离线替身无法复用它的构造逻辑；用结构化类型就能让两者
    各自独立实现，又都能被类型检查器接受。

    这个契约同时也是"模型调用只收敛在 adapter 这一个边界"的书面凭据：
    只要满足下面这几个成员，就能把整条 pipeline 换成别的东西（离线替身、
    缓存回放、未来的本地模型），而不需要动 pipeline 一行。
    """

    spec: Any
    client: Any

    @property
    def supports_vision(self) -> bool: ...

    @property
    def supports_json_schema(self) -> bool: ...

    def describe(self) -> str: ...

    def analyze_group(
        self,
        items: list[PreviewItem],
        *,
        style_block: str,
        user_prompt: str,
        batch_stats: dict[str, float] | None = None,
        auto_match: bool = False,
        temperature: float | None = None,
    ) -> list[ItemResult]: ...

    def analyze_training(
        self, samples: list[PreviewItem], style_name: str
    ) -> dict[str, Any]: ...


class ModelAdapter:
    """模型能力的适配层。线程安全性说明：

    - ApiClient 内部的 requests.Session 按线程隔离（threading.local）；
    - ModelAdapter 自身不持有可变状态（除了只读的 spec 与 client 引用），
      因此可以被并发调用 analyze_group / analyze_training。
    """

    def __init__(self, spec: ModelSpec, api_key: str, budget: RequestBudget) -> None:
        self.spec = spec
        self.client = ApiClient(spec, api_key, budget)
        # "strict 被退化"这条警告整批只打一次（逐图打会把日志淹没）。
        self._strict_degraded_logged = False

    # --- 停止支持 -----------------------------------------------------------

    def set_cancel_check(self, check: "Callable[[], bool] | None") -> None:
        """注入「用户是否已点停止」的检查器（由 pipeline 注入）。

        为什么需要：用户点暂停后，**失败的几张还在继续重试**（他用 GLM 时发现的）。
        重试发生在三个地方：客户端内部的重试、本类的“把错误回灌重发”、
        以及多图失败后的**拆单重发** —— 只有第一处以前能被间接拦住（也不彻底），
        后两处完全不知道用户在停止。现在三处都走同一个检查器。
        """
        self.cancel_check = check
        setter = getattr(self.client, "set_cancel_check", None)
        if callable(setter):
            setter(check)

    def set_concurrency_hint(self, workers: int | None) -> None:
        """把本次运行的并发工人数告诉客户端（用于遇到限流/超时时自动降档）。"""
        setter = getattr(self.client, "set_concurrency_hint", None)
        if callable(setter):
            setter(workers)

    def _raise_if_cancelled(self, where: str) -> None:
        """已请求停止就抛 StopRequestedError（控制流信号，由 pipeline 当正常结束处理）。"""
        check = getattr(self, "cancel_check", None)
        if check is None:
            return
        try:
            cancelled = bool(check())
        except Exception:  # noqa: BLE001 —— 检查器出错不该弄死主流程
            return
        if cancelled:
            raise StopRequestedError(f"已请求停止（{where}）：不再发送新的 API 请求。")

    # --- 能力描述 -----------------------------------------------------------

    @property
    def supports_vision(self) -> bool:
        return self.spec.supports_vision

    @property
    def supports_json_schema(self) -> bool:
        return self.spec.supports_json_schema

    def describe(self) -> str:
        """给日志用的能力摘要。

        必须带上**实际请求地址**：用户把 base_url 指向中转站/内网网关时，
        "我到底在调谁"只有这一行能回答（只写"DeepSeek"会让人以为在调官方端点）。
        """
        return (
            f"{self.spec.display} @ {self.spec.full_url}；"
            f"视觉={'支持' if self.supports_vision else '不支持（走 caption 降级）'}；"
            f"json_schema={'支持' if self.supports_json_schema else '不支持（走提示词+本地强校验降级）'}；"
            # 输出上限也要写进来：它直接决定“单次能不能装下结果”与花费，
            # 出了空 content / 截断时，第一眼要看的就是这个数。
            # omit 模式必须**如实**说"不发这个字段"，不能拿配置里的数字假装，
            # 否则排查截断问题时会对着一个根本不存在的上限找原因。
            f"输出上限={self._limit_text()}"
        )

    def _limit_text(self) -> str:
        """输出上限的说明文字（三种发送方式分别不同）。"""
        mode = getattr(self.spec, "max_tokens_mode", "max_tokens")
        if mode == "omit":
            return f"不发该字段，由服务端决定（配置参考值 {self.spec.max_output_tokens}）"
        return f"{self.spec.max_output_tokens} token（{mode}）"

    def _limit_field(self) -> str | None:
        """当前上限字段的**真实名字**；omit 时返回 None。

        存在的理由：出问题时要能一眼看出程序到底在翻哪个字段。
        "抬高了上限却仍然截断"有一种最阴的成因 —— 翻的是服务端根本不看的
        那个字段（例如该发 max_completion_tokens 却写 max_tokens），
        请求照样 200，输出却比原来截得更早。所以这个值必须可读、可断言。
        """
        mode = getattr(self.spec, "max_tokens_mode", "max_tokens")
        if mode == "omit":
            return None
        return "max_completion_tokens" if mode == "max_completion_tokens" else "max_tokens"

    def _schema_dialect(self) -> Any:
        """这个模型条目的 schema 方言（逐家一份，见 ai/schema.py）。"""
        from .schema import dialect_for_spec

        return dialect_for_spec(self.spec)

    def _warn_if_strict_degraded(self) -> None:
        """strict 因方言限制被退化时**必须说一声**（不静默降级）。

        静默降级的后果：用户按文档打开 json_schema_strict，行为却和没开一样，
        他只会觉得"这个开关是坏的"，而真正的原因是端点不认任何联合类型。
        """
        from .schema import resolve_strict

        if not self.spec.json_schema_strict:
            return
        _effective, reason = resolve_strict(True, self._schema_dialect())
        if reason and not self._strict_degraded_logged:
            self._strict_degraded_logged = True
            log.warning("%s：%s", self.spec.display, reason)

    # --- response_format 构造 ----------------------------------------------

    def _response_format(self, schema: dict, name: str) -> dict | None:
        """按能力选择输出格式约束；三档，见 ai/schema.py 与 config.py 头部说明。

        为什么不能只有"支持 json_schema / 不支持"两档：
        DeepSeek 官方文档《JSON Output》明确写的是 `response_format={'type':'json_object'}`
        —— 它支持 JSON Output，但**不支持** json_schema。
        按旧的两档逻辑（supports_json_schema=false）它会被当成"什么都不支持"，
        白白丢掉服务端本来就有的 JSON 约束；更糟的是把 json_schema 硬发过去，
        实测会得到 400：`This response_format type is unavailable now`。
        """
        if self.supports_json_schema:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    # 先剥掉**这家**不支持的关键字（全局的 minimum/maxItems/…，
                    # 加上该端点的方言禁用项，如 Kimi MFJS 的联合类型）。
                    # 不剥的话：strict=true 时服务端会直接拒（schema 不在支持子集内），
                    # 而那是用户一开 strict 就会撞上的必然失败。
                    "schema": _strict_keyword_subset(schema, self._schema_dialect()),
                    # strict 的取舍见 ai/schema.py 模块头部说明。
                    "strict": bool(self.spec.json_schema_strict),
                },
            }
        if getattr(self.spec, "supports_json_object", False):
            # 只保证"是合法 JSON"，不保证字段结构 —— 结构仍由提示词
            # （_schema_as_text）+ 本地 pydantic 强校验负责。
            return {"type": "json_object"}
        return None

    def _schema_as_text(self, schema: dict) -> str:
        """把 JSON Schema 转成给模型看的文字要求（降级通道用）。

        ⚠ 首句必须带小写的 "json" 字样：DeepSeek 的 JSON Output 文档明确要求
        "用户传入的 system 或 user prompt 中必须含有 json 字样"，否则 json_object
        模式会被它拒掉。这也顺带对那些靠关键字识别的小网关友好。
        """
        import json

        return (
            "【输出格式要求】\n"
            "你必须**只输出一个 json 对象**（JSON 对象字面量），"
            "不要输出任何解释文字、不要用 Markdown 代码围栏。\n"
            "请严格遵循以下 JSON Schema：\n"
            "```json\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)[:12000]
            + "\n```\n"
            "特别强调：params 的键必须带 crs: 前缀，且只能使用上面列出的字段名；"
            "只返回需要改动的字段。"
        )

    # --- 消息组装 -----------------------------------------------------------

    def _build_messages(
        self,
        system_prompt: str,
        user_text: str,
        items: list[PreviewItem],
        schema: dict,
        caption_system_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        """组装 messages。

        视觉模型：文本 + 每张图的 base64 data URL（先标注 file_id，再紧跟图片）。
        非视觉模型：仅文本（caption 已在 user_text 里），并替换/补充提示词，
        明确告知"你看不到图"，防止模型编造画面内容（幻觉）。

        参数 caption_system_prompt 的语义：
            传入非 None —— 表示这是输出模式，直接用这份"看不到图"的专用提示词；
            传入 None    —— 表示这是训练模式等其他场景，只在原提示词后追加一条声明，
                           不整体替换（否则会丢掉训练模式特有的差分说明）。
        """
        system = system_prompt
        if not self.supports_json_schema:
            # 降级通道：必须把格式要求写进提示词。
            system = system + "\n\n" + self._schema_as_text(schema)

        if not self.supports_vision:
            if caption_system_prompt is not None:
                system = caption_system_prompt
                if not self.supports_json_schema:
                    system = system + "\n\n" + self._schema_as_text(schema)
            else:
                system = system + (
                    "\n\n【你无法看到图片】\n"
                    "你没有看图能力，程序给出的只是本地统计量，"
                    "请勿编造任何画面内容（例如「画面中有两个人」）。"
                )

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]

        if self.supports_vision:
            for item in items:
                # 顺序很重要：先放 file_id 标注，再放该图。
                # 反过来的话"下面这张图"这类指代会让模型错配 file_id 与图片。
                content.append(
                    {
                        "type": "text",
                        "text": f'"file_id" = "{item.file_id}" 的图片如下：',
                    }
                )
                data_url = encode_image_data_url(item.preview_jpeg, self.spec.image_max_bytes)
                image_url: dict[str, Any] = {"url": data_url}
                # detail 是 OpenAI 的标准字段，但**不是每家文档都写了**（见 config.py
                # sends_image_detail 的注释）：文档没写的家少发一个字段，
                # 免得被严格网关按"unknown parameter"拒掉，而报错看不出是它的问题。
                if getattr(self.spec, "sends_image_detail", True):
                    image_url["detail"] = "high"
                content.append({"type": "image_url", "image_url": image_url})

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    # --- 解析响应 -----------------------------------------------------------

    @staticmethod
    def _parse_items_object(obj: dict, expected: list[PreviewItem]) -> list[tuple[PreviewItem, ItemResult]]:
        """把响应 JSON 解析为 (item, result) 列表。

        严格性说明：
          - items 必须是数组；
          - 每个 file_id 必须属于本次请求的集合（防止模型编造文件名）；
          - 请求中的每个 file_id 都必须有返回项（缺失视为错误，触发重试）；
          - params 必须通过 xmp.validator 的强校验。
        """
        raw_items = obj.get("items")
        if not isinstance(raw_items, list):
            raise SchemaValidationError(f"响应中缺少 items 数组。实际键：{sorted(obj)}")

        by_id: dict[str, dict] = {}
        for entry in raw_items:
            if not isinstance(entry, dict):
                raise SchemaValidationError(f"items 中的元素不是对象：{entry!r}")
            file_id = str(entry.get("file_id") or "").strip()
            if file_id:
                by_id[file_id] = entry

        expected_ids = [item.file_id for item in expected]
        missing = [fid for fid in expected_ids if fid not in by_id]
        if missing:
            raise SchemaValidationError(
                f"响应中缺少这些 file_id 的结果：{missing}（期望 {len(expected_ids)} 项，实得 {len(by_id)} 项）"
            )

        unknown = [fid for fid in by_id if fid not in expected_ids]
        if unknown:
            # 多余项只警告不失败：模型偶尔会带上上次的残留，
            # 我们丢弃即可，没必要为它重试整个请求（浪费预算）。
            log.warning("响应中包含未请求的 file_id：%s，已忽略。", unknown)

        results: list[tuple[PreviewItem, ItemResult]] = []
        validation_errors: list[str] = []

        for item in expected:
            entry = by_id[item.file_id]
            raw_params = entry.get("params")
            raw_curves = entry.get("curves")

            # 合并 params 与 curves 后统一校验（validator 会分离两类字段）。
            merged: dict[str, Any] = {}
            if isinstance(raw_params, dict):
                # 注意：这里的空字典**不会被当成失败** —— 空对象是提示词允许的
                # 「本图无需调整」，而"键缺失/为 null"（下面的分支）仍然是错误。
                # 两者的区别见 validator.validate_params 里的三态说明。
                merged.update(raw_params)
            elif raw_params is None:
                validation_errors.append(f"{item.file_id}: 响应里没有 params（键缺失或为 null）")
            else:
                validation_errors.append(
                    f"{item.file_id}: params 不是对象（实际 {type(raw_params).__name__}）"
                )

            result = ItemResult(file_id=item.file_id, notes=str(entry.get("notes") or ""))

            verdict = V.validate_params(merged)
            result.params = verdict.params
            result.warnings.extend(verdict.warnings)
            result.curves = verdict.curves

            # 局部调整意图（步骤 5）：几何用显示帧坐标，写入前由程序按每张照片
            # 自己的 EXIF 方向换算；局部参数只允许 xmp/masks.AI_LOCAL_RANGES 里那一小扇门。
            raw_masks = entry.get("masks")
            parsed_masks, mask_warnings = masks_mod.parse_specs(raw_masks)
            result.masks = parsed_masks
            result.warnings.extend(mask_warnings)

            # curves 单独校验（它的键可能不在 params 里）。
            if isinstance(raw_curves, dict) and raw_curves:
                curve_verdict = V.validate_params(raw_curves)
                result.warnings.extend(curve_verdict.warnings)
                for name, points in curve_verdict.curves.items():
                    result.curves[name] = points

            if not verdict.ok:
                validation_errors.append(f"{item.file_id}: {verdict.summary()}")

            results.append((item, result))

        if validation_errors:
            raise SchemaValidationError("；".join(validation_errors[:6]))

        return results

    # --- 核心调用（含解析重试与错误回灌） ----------------------------------

    def _call_with_repair(
        self,
        system_prompt: str,
        user_text: str,
        items: list[PreviewItem],
        schema: dict,
        *,
        temperature: float,
        schema_name: str,
        parser: Callable[[dict], Any],
        caption_system_prompt: str | None = None,
    ) -> Any:
        """调用模型并解析，失败时把错误回灌让模型自修复。

        重试次数为 MAX_PARSE_RETRIES（当前 1，即最多 2 次尝试）。
        每次尝试都是一次真实请求，因而都会计入预算（硬约束 #18）。
        """
        response_format = self._response_format(schema, schema_name)
        messages = self._build_messages(
            system_prompt, user_text, items, schema, caption_system_prompt
        )

        last_error: Exception | None = None
        # 本次实际使用的输出上限。默认就是配置里的值；
        # 遇到“空 content / 被截断”时会抬高（见下），因为那两种症状
        # 很可能是推理 token 把这个字段吃完了。
        #
        # ⚠ 抬高的**值**是模式无关的（字段名由 client 按 max_tokens_mode 决定），
        #   所以"翻倍"这件事本身天然跟着模式走。真正需要单独处理的是 omit：
        #   那种模式下请求里根本没有上限字段，"翻倍"是**一个动作都没有**，
        #   重发必然一模一样地再失败一次。见下面的守卫。
        max_tokens = self.spec.max_output_tokens
        limit_field = self._limit_field()

        for attempt in range(MAX_PARSE_RETRIES + 1):
            # 每一轮都是真实请求：用户已点停止就不发（否则“停止后仍在重试”）。
            self._raise_if_cancelled("修复重试前")
            data = self.client.chat(
                messages,
                temperature=temperature,
                response_format=response_format,
                max_tokens_override=max_tokens,
            )

            try:
                text = self.client.extract_message_text(data)
            except (EmptyContentError, TruncatedOutputError) as exc:
                # 这两类症状的共同点：**重发同样的请求基本会再失败一次**。
                # 所以不能像“格式不对”那样把错误回灌给模型重试，
                # 而是把输出上限翻倍（思考模式的推理 token 也算在里面）。
                # 重试名额只有 1 次，所以就在这一次上直接翻，而不是先原样重试一遍。
                last_error = exc
                if limit_field is None:
                    # omit：请求里**根本没有**输出上限字段，所以"翻倍"是空动作。
                    # 不拦的话会出现最恶劣的一种失败：错误文案写着"已把上限抬到 X
                    # 重试过"，而实际什么都没改 —— 用户按这句提示去改 max_output_tokens，
                    # 改多少次都不会有任何变化（因为这个字段压根不发）。
                    # 所以直接给出真正能执行的动因，并且**不浪费**第二次请求（也省一次预算）。
                    raise SchemaValidationError(
                        f"{exc}\n"
                        "该模型不支持设置输出上限（max_tokens_mode=omit）："
                        "请求里没有上限字段，程序无法用「抬高出上限」这个手段救回来，"
                        "重发一次也是同样的结果（本次已跳过重发）。\n"
                        "请改用以下任一方式：\n"
                        "  1) 减小单次输出量：保持 images_per_request=1，或分批处理；\n"
                        "  2) 若这家其实支持设置上限，把该条目的 max_tokens_mode 改成 "
                        "max_tokens / max_completion_tokens 再试；\n"
                        "  3) 关掉推理/思考模式（extra_body，例如 {reasoning_effort: none}）"
                        "—— 推理 token 不再挤占输出预算，同样的问题往往就消失了。"
                    ) from exc
                if attempt >= MAX_PARSE_RETRIES:
                    raise SchemaValidationError(
                        f"{exc}\n"
                        f"已把 {limit_field} 从 {self.spec.max_output_tokens} 抬到 {max_tokens} "
                        f"重试过（发送方式：{self._limit_text()}），仍失败。\n"
                        "可尝试：1) 在模型条目里把 max_output_tokens 直接调大；"
                        "2) 关掉思考模式（DeepSeek 可用 extra_body: {reasoning_effort: none}）；"
                        "3) 调小 images_per_request 以减少单次输出量；"
                        "4) 把 supports_json_object / supports_json_schema 关掉以改变输出约束方式。"
                    ) from exc
                raised = min(max(self.spec.max_output_tokens * 2, max_tokens * 2),
                             MAX_OUTPUT_TOKENS_CEILING)
                if raised > max_tokens:
                    log.warning(
                        "第 %d 次尝试失败（%s）：把 %s 从 %d 抬到 %d 后重试一次。",
                        attempt + 1,
                        type(exc).__name__,
                        limit_field,
                        max_tokens,
                        raised,
                    )
                    max_tokens = raised
                else:
                    log.warning(
                        "第 %d 次尝试失败（%s），但输出上限已到天花板 %d，只能原样重试。",
                        attempt + 1,
                        type(exc).__name__,
                        MAX_OUTPUT_TOKENS_CEILING,
                    )
                continue

            try:
                obj = extract_json_object(text)
                return parser(obj)
            except SchemaValidationError as exc:
                last_error = exc
                log.warning(
                    "第 %d 次响应解析/校验失败：%s",
                    attempt + 1,
                    str(exc)[:500],
                )
                if attempt >= MAX_PARSE_RETRIES:
                    break
                # 错误回灌：把上一轮的输出与错误原因一起发回，要求修正。
                # 截断到 4000 字符，避免把超长输出再塞回去撑爆上下文。
                messages = messages + [
                    {"role": "assistant", "content": text[:4000]},
                    {
                        "role": "user",
                        "content": (
                            "你上一次的输出未通过本地校验，错误如下：\n"
                            f"{str(exc)[:1000]}\n\n"
                            "请修正后重新输出完整的 JSON（只输出 JSON，不要解释）。"
                            "注意：字段名必须带 crs: 前缀、取值必须在规定范围内、"
                            "不要包含任何机器相关字段。"
                        ),
                    },
                ]

        raise SchemaValidationError(
            f"经过 {MAX_PARSE_RETRIES + 1} 次尝试仍无法获得合法结果。最后错误：{last_error}"
        )

    # --- 输出模式：分析一组图片 --------------------------------------------

    def analyze_group(
        self,
        items: list[PreviewItem],
        *,
        style_block: str,
        user_prompt: str,
        batch_stats: dict[str, float] | None = None,
        auto_match: bool = False,
        temperature: float | None = None,
    ) -> list[ItemResult]:
        """分析一组图片（默认 1 张），返回每张的结果。

        **不抛异常**：任何失败都转为 ItemResult.error，
        保证单张失败不会中断整批任务（硬约束 #5）。
        """
        if not items:
            return []

        # 用户已点停止：连这一组也不发。
        self._raise_if_cancelled("分析前")

        temp = self.spec.temperature_output if temperature is None else temperature
        system_prompt = P.build_output_system_prompt()
        user_text = P.build_output_user_text(
            style_block=style_block,
            user_prompt=user_prompt,
            items=[
                {
                    "file_id": item.file_id,
                    "filename": item.filename,
                    "preview_source": item.preview_source,
                    "preview_note": "缩略图来源可信度低，细节判断不可靠" if item.preview_low_confidence else "",
                    "caption": item.caption,
                    "metrics": item.metrics,
                }
                for item in items
            ],
            batch_stats=batch_stats,
            auto_match=auto_match,
        )

        file_ids = [item.file_id for item in items]
        self._warn_if_strict_degraded()
        schema = S.build_response_schema(
            file_ids, strict=self.spec.json_schema_strict, dialect=self._schema_dialect()
        )

        try:
            results = self._call_with_repair(
                system_prompt,
                user_text,
                items,
                schema,
                temperature=temp,
                schema_name="acr_batch_params",
                parser=lambda obj: self._parse_items_object(obj, items),
                caption_system_prompt=P.build_caption_system_prompt(),
            )
        except Exception as exc:
            message = str(exc)
            # 停止是**控制流信号**，不是这张图的失败：原样上抛，
            # 让 pipeline 当作“已停止”处理（既不能拆单重发，也不能记入失败次数）。
            if isinstance(exc, StopRequestedError):
                raise
            log.error("分析失败（%s）：%s", ", ".join(file_ids), message[:600])
            # 账户配额/额度类错误（QuotaExceededError）**不能拆单重发**：
            # 配额跟请求大小无关，拆成 N 个单图请求只会把一次失败变成 N 次请求，
            # 结果一样是全失败 —— 白花预算与时间。但**必须给每张图都产生一条结果**，
            # 否则同组里没被上报的那几张会静默消失（"少处理了几张、日志里查不到"
            # 正是刚修完的那类问题）。
            if isinstance(exc, QuotaExceededError):
                return [ItemResult(file_id=item.file_id, error=message) for item in items]
            # 多图请求失败时拆成单图重试：把"整组失败"降级为"个别失败"，
            # 避免一张图的问题连累同组的其他图。
            if len(items) > 1:
                log.info("多图请求失败，拆分为单图请求重试（共 %d 张）。", len(items))
                individual: list[ItemResult] = []
                for single in items:
                    # 拆单重发前再查一次：一张失败后用户往往就按停止了，
                    # 那就不该把剩下的 N-1 张又各发一遍（每张还会再重试）。
                    self._raise_if_cancelled("拆单重发前")
                    individual.extend(
                        self.analyze_group(
                            [single],
                            style_block=style_block,
                            user_prompt=user_prompt,
                            batch_stats=batch_stats,
                            auto_match=auto_match,
                            temperature=temp,
                        )
                    )
                return individual

            return [ItemResult(file_id=items[0].file_id, error=message)]

        out: list[ItemResult] = []
        for item, result in results:
            if item.preview_low_confidence:
                result.warnings.append(
                    "该图仅有小尺寸缩略图可用，AI 对细节类参数的判断可靠性下降，"
                    "建议人工复核锐化与降噪相关设置。"
                )
            out.append(result)
        return out

    # --- 训练模式 -----------------------------------------------------------

    def analyze_training(self, samples: list[PreviewItem], style_name: str) -> dict[str, Any]:
        """归纳用户偏好，返回训练结果字典。

        硬约束 #19：训练请求 temperature=0.2（低于输出模式），保证偏好统计稳定。
        """
        if not samples:
            raise SchemaValidationError("没有可用的训练样本")

        system_prompt = P.build_training_system_prompt()
        user_text = P.build_training_user_text(
            samples=[
                {
                    "file_id": sample.file_id,
                    "filename": sample.filename,
                    "params": sample.training_params or {},
                    "curves": sample.training_curves or {},
                    "excluded_fields": sample.training_excluded or [],
                    "mask_usage": sample.training_mask_usage,
                    "caption": sample.caption,
                }
                for sample in samples
            ],
            style_name=style_name,
        )

        self._warn_if_strict_degraded()
        schema = S.build_training_schema(strict=self.spec.json_schema_strict)

        def parser(obj: dict) -> dict:
            required = [
                "style_summary",
                "text_rules",
                "saturation_tendency",
                "contrast_tendency",
                "shadow_color_cast",
                "hsl_habits",
                "lens_correction_preference",
                "excluded_reasoning",
            ]
            missing = [key for key in required if key not in obj]
            if missing:
                raise SchemaValidationError(f"训练结果缺少必需字段：{missing}")
            if not isinstance(obj.get("text_rules"), list) or not obj["text_rules"]:
                raise SchemaValidationError("text_rules 必须是非空数组")
            return obj

        # 训练模式请求体较大（含全部样本的参数明细），
        # 因此这里不做"多图拆单"的降级——训练是一次性归纳，拆开反而丢失全局视角。
        return self._call_with_repair(
            system_prompt,
            user_text,
            samples if self.supports_vision else [],
            schema,
            temperature=TRAIN_TEMPERATURE,
            schema_name="style_profile",
            parser=parser,
        )
