# -*- coding: utf-8 -*-
"""模型配置加载。

硬约束 #4：模型不得硬编码，由 config/models.yaml 管理
    base_url / key_env / 是否支持视觉 / 输出格式能力 / max_context，
    切换模型只改 active 一行。

输出格式有三档（为什么不能只有两档，见 ai/adapter.py::_response_format）：
    1. response_format=json_schema —— 服务端按 schema 约束输出（最强，但支持的端点少）；
    2. response_format=json_object —— 服务端只保证"是合法 JSON"（DeepSeek 等多家支持）；
    3. 不带 response_format —— 纯靠提示词 + 本地 pydantic 强校验。
    三档都在，是因为"不支持 json_schema"不等于"什么都不能用"：把 DeepSeek
    按第 3 档处理会白白丢掉它本来就支持的 json_object。

配置文件的两层结构：
    1. 打包内只读种子：<resource_root>/config/models.yaml（PyInstaller datas）
    2. 用户可写副本：  <data_root>/config/models.yaml
    首次运行时把种子复制到用户目录；之后只读用户目录。
    这样做的好处是用户新增/修改模型不必重新打包 exe，
    符合打包要求 #20（所有配置相对于 exe 所在目录或 %APPDATA% 解析）。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .constants import REQUEST_TIMEOUT_S
from .errors import ModelConfigError
from .logging_setup import get_logger
from .paths import config_dir, resource_root

log = get_logger("config")

# 打包内配置种子的相对路径（与 spec 的 datas 条目必须一致）
BUNDLED_CONFIG_RELPATH = Path("config") / "models.yaml"

# 已实现的"请求形状"（api_style）。
# 存在的理由：不同家的 HTTP 请求结构**不只是 model 名字不同**——
#   OpenAI 兼容：POST /chat/completions，{"model": …, "messages": […]}；
#   Anthropic 原生：POST /v1/messages，结构不同且 max_tokens 必填；
#   Gemini 原生：POST /v1beta/models/{model}:generateContent，model 在 URL 里而不是 body 里。
# 所以"切模型只改一个字符串"**只对本表里存在的形状成立**。
# 不在表里的形状一律**加载即报错**（见 ProviderSpec._known_api_style），
# 宁可当场起不来，也不要等运行时返回 400 或静默走成别的模型。
IMPLEMENTED_API_STYLES: dict[str, str] = {
    "openai": "OpenAI 兼容（POST {base_url}/chat/completions，model 在请求体里）",
}


def validate_api_style(v: str) -> str:
    """只接受**已实现**的请求形状，其余一律拒绝（加载期就拦住）。

    这是"宁可当场起不来，也不要运行时才炸"的那一处：配置里写一个没实现的
    形状（比如 anthropic），旧行为是照发一个 OpenAI 形状的请求过去，
    表现为 400，或者更糟 —— 网关成功返回但用了别的模型。
    """
    style = str(v).strip().lower()
    if style not in IMPLEMENTED_API_STYLES:
        known = "\n".join(f"  - {k}：{d}" for k, d in IMPLEMENTED_API_STYLES.items())
        raise ValueError(
            f"api_style「{style}」尚未实现，已在加载时拒绝。\n"
            f"当前实现的请求形状只有：\n{known}\n"
            "Anthropic 原生 /v1/messages、Gemini 原生 :generateContent 都还没实现 ——"
            "它们不是改一个 model 字符串就能用的，必须先加请求构造分支。"
        )
    return style


# 模型名的"代次"排序 —— 界面上"其余模型"的排列顺序。
#
# 【为什么不能用字母序（真实踩过，静默降级）】
#   '-' 的码点 0x2D 小于 '3' 的 0x33，所以 `qwen-vl-max` 会排在
#   `qwen3-vl-plus` 前面：切到「千问」时自动选中了**更旧的那一代**，
#   而配置里明明把 Qwen3-VL 写在最前面并注明了官方依据。
#   用户看到的是"什么都没发生"，实际用的模型比推荐的旧一代 —— 不报错，
#   所以只能靠排序规则本身正确，靠"配置顺序刚好写对"迟早会复发。
#
# 规则：
#   1. 先取模型名里第一个"代次数字"（1–2 位），代次**从新到旧**排前面
#      （选择题里新一代几乎总是你想用的那个）；
#   2. 同代次按名字典序。
# 为什么限制 1–2 位：`fun-asr-flash-2026-06-15` 里的 2026 是日期，
# 当成代次会把它顶到最前面（这个形态的 id 在百炼的模型列表里真实存在）。
# 正则用 `(?<!\d)(\d{1,2})(?!\d)`：`2026` 这种 4 位数字在**任何**起始位置
# 都匹配不上（两位会被后面的数字否决，一位也被否决），所以它拿不到代次。
_MODEL_GENERATION_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")

# 日期形态（`2026-06-15` / `2024-08`）要先摘掉再找代次。
# 【为什么必须单独做这一步 —— 第一版就踩了】
#   上面那条正则挡得住 `2026`，挡不住它**后面**的 `06`：
#   `fun-asr-flash-2026-06-15` 会被读出代次 6，于是这个语音模型排到了
#   所有第 6 代语言模型前面。日期里的"月"和"日"都是 1–2 位、也满足
#   "前后不是数字"，所以只能先把整段日期抠掉。
_DATE_LIKE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}(?:-\d{2})?(?!\d)")


def model_generation(model_id: str) -> int:
    """模型名里的代次数字；看不出来（或只有日期）时返回 0。"""
    name = _DATE_LIKE_RE.sub("-", model_id or "")
    match = _MODEL_GENERATION_RE.search(name)
    return int(match.group(1)) if match else 0


def model_sort_key(model_id: str) -> tuple[int, str]:
    """排序键：代次降序 + 名字升序。

    返回值直接给 `sorted(key=...)` 用：代次取负数即可实现"新的在前"。
    """
    return (-model_generation(model_id), (model_id or "").lower())


def sort_model_ids(model_ids: Iterable[str]) -> list[str]:
    """按 model_sort_key 排序。

    单独成函数是为了让"排序行为"可以被直接断言（见 smoke_test 的
    「模型代次排序」一项），而不是只能靠界面截图去猜。
    """
    return sorted(model_ids, key=model_sort_key)


class ModelSpec(BaseModel):
    """单个模型条目的强校验定义。

    使用 extra="forbid"：用户把字段名拼错时（例如写成 baseurl）会立刻报错，
    而不是静默使用默认值——拼错配置导致的调试成本远高于启动时报错。
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    base_url: str
    endpoint_path: str = "/chat/completions"
    model: str
    key_env: str

    # 服务商标识：同一服务商下的多个模型共用同一个 key_env，
    # 界面"保存密钥 → 识别服务商 → 切换到该服务商模型"靠它分组。
    # 默认空串：旧配置文件里的条目没有它也能加载（单独成组）。
    provider: str = Field(default="")

    # 请求形状。客户端按它**分路构造请求**，绝不按 model 名字拼字符串
    # （见 IMPLEMENTED_API_STYLES 的说明）。
    api_style: str = Field(default="openai")

    @field_validator("api_style")
    @classmethod
    def _known_api_style(cls, v: str) -> str:
        return validate_api_style(v)

    # 能力开关：决定走哪条通道（硬约束 #4 / #19）
    supports_vision: bool
    # 输出格式三档中的前两档（第三档是"两个都是 false"）。
    # supports_json_object 默认 False：它只在**文档明确支持**的服务商上打开，
    # 因为有些端点会把不认识的 response_format 直接拒成 400。
    supports_json_schema: bool
    supports_json_object: bool = False

    # 上下文窗口，用于超长批次自动分批时的安全边界
    max_context: int = Field(gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)

    # 输出上限用**哪个参数名**、甚至"发不发"。三选一：
    #   "max_tokens"            —— 旧名字（默认；大多数兼容端点都认）
    #   "max_completion_tokens" —— 新名字。OpenAI 的推理模型（o 系 / gpt-5 及以后）
    #                              把它列为"只认这个"：发 max_tokens 会直接 400；
    #                              Kimi 官方也已把 max_tokens 标记为 deprecated。
    #   "omit"                  —— 一个都不发。阿里云百炼《结构化输出》写明：
    #                              "开启结构化输出时，请勿设置 max_tokens"
    #                              （它会把 JSON 截断成非法 JSON）。
    # 为什么必须可配：这是**真实用户会撞到的 400**，而不是理论风险 ——
    # 用户在下拉里选到推理模型就报错，而报错原文完全看不出是参数名的问题。
    max_tokens_mode: Literal["max_tokens", "max_completion_tokens", "omit"] = "max_tokens"

    # 是否发送 temperature。
    # OpenAI 的推理模型把 temperature / top_p / presence_penalty 等列为
    # **Unsupported parameters**（发了就 400）。关掉后请求里不带这个字段，
    # 由服务端用自己的默认值。
    sends_temperature: bool = True

    # 图片消息里**要不要带 `image_url.detail`**（"high"/"low"）。
    # 【为什么必须可配 —— 这是"字段合法性"问题，不是风格问题】
    # `detail` 是 OpenAI 的标准字段，但**不是每家文档都写了**：
    #   · 百度千帆《视觉理解》里 image_url 只文档了 `url` 一个键；
    #   · 火山方舟《对话(Chat) API》的 messages 子表也没在可摘录的文档里给出 detail。
    # 有些网关对"文档没写的字段"是**严格拒收**的（400 invalid parameter），
    # 而报错原文通常只说"参数错误"，根本看不出是多了个 detail。
    # 所以默认 true（保持既有行为），文档没写的家显式设 false。
    sends_image_detail: bool = True

    # --- schema 方言（两家不认的关键字不一样，别让一家限制削弱另一家）---------
    # 机制见 ai/schema.py 的 SchemaDialect。
    # 在这家端点上**额外**禁用哪些 JSON Schema 关键字（与全局不支持集合取并集）。
    # 例：某些兼容层不认 `format` 或 `pattern`。
    forbidden_schema_keywords: list[str] = Field(default_factory=list)
    # 是否接受"可空联合"（`{"anyOf":[T,{"type":"null"}]}` 或 `"type":[T,"null"]`）。
    # Kimi 的 MFJS 规范明确不支持 anyOf —— 关掉后，上面的 schema 会在**生成时**
    # 就不产生联合类型（不是事后删），并要求 strict 退化为非严格（见 resolve_strict）。
    allow_anyof_in_schema: bool = True

    # json_schema 的严格模式开关。
    # 背景：OpenAI 的 strict=true 要求 schema 里所有 properties 都出现在 required 中。
    # 本项目有约 90 个可写字段，若开 strict，模型每张图都必须把 90 个字段全吐一遍
    # （未调整的写 null），单图输出约 1000+ token。对 100 张的批次就是十万级 token，
    # 成本与延迟都不可接受。
    # 因此默认 strict=False：schema 仍随请求下发用于引导格式与约束范围，
    # 但**真正的把关者是本地 pydantic 强校验**（硬约束 #5）。
    # 若你用的模型在非 strict 下格式不稳定，可以把这个开关打开换来确定性，
    # 代价是输出 token 数显著上升。
    json_schema_strict: bool = False

    # 单次请求携带的图片数量。
    # 默认 1：每张图独立成请求，失败隔离性好——某个文件的响应解析失败
    # 只影响它自己，不会连带同批的其他图。
    # 调大能摊薄系统提示词的开销（约 3000 token/请求），
    # 且让模型能在批内做统一性比较；代价是失败粒度变粗。
    # 程序在检测到"多图请求的响应校验失败"时会自动拆成单图请求重试。
    images_per_request: int = Field(default=1, ge=1, le=8)

    # 两种模式的温度（硬约束 #19：训练 0.2 低于输出模式）
    temperature_output: float = Field(default=0.6, ge=0.0, le=2.0)
    temperature_train: float = Field(default=0.2, ge=0.0, le=2.0)

    extra_headers: dict[str, str] = Field(default_factory=dict)

    # 模型特有的请求体字段（原样合进 body）。
    # 为什么需要它：不同家的调优旋钮不在 OpenAI 的公共字段里。
    # 真实例子（DeepSeek 官方文档）：`thinking` / `reasoning_effort` 控制思考模式，
    # 默认是**开**的（强度 high）——不提供这个通道，用户要关思考模式就只能改代码。
    # 例：extra_body: {"reasoning_effort": "none"}。
    # model / messages / stream / response_format 四个键是程序契约，写进来会被拒。
    extra_body: dict[str, Any] = Field(default_factory=dict)

    # 单图 base64 前的字节上限。默认 1.5MB：base64 后约 2MB，
    # 在多数服务端 5–10MB 请求体上限内留足余量，同时避免把超大预览图发出去烧钱。
    image_max_bytes: int = Field(default=1_500_000, gt=0)

    # 单请求超时。默认值**取自 constants.REQUEST_TIMEOUT_S**，不再写死 60——
    # 此前这里硬编码 60，而 client 用的是 `spec.request_timeout_s or ...`，
    # 导致 constants 里的 REQUEST_TIMEOUT_S 永远走不到，改它没有任何效果。
    # 想单独调优某个模型（例如视觉模型给 30 秒），在本条目里写 request_timeout_s 即可。
    request_timeout_s: int = Field(default=REQUEST_TIMEOUT_S, gt=0)

    # 服务端**账户级**限流的真值：最多几个请求同时在飞、每分钟最多几次。
    # None = 不限（按工人的并发跑）。见 ProviderSpec 上同名注释里的依据。
    # 客户端会拿它与"429 原文里学到的值"取**更严**的一个（只能收紧，不会放宽）。
    max_concurrency: int | None = Field(default=None, ge=1)
    max_rpm: int | None = Field(default=None, ge=1)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        """去掉 base_url 末尾斜杠，避免与 endpoint_path 拼出双斜杠。"""
        v = v.strip()
        if not v:
            raise ValueError("base_url 不能为空")
        return v.rstrip("/")

    @field_validator("endpoint_path")
    @classmethod
    def _ensure_leading_slash(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/"):
            v = "/" + v
        return v

    @property
    def full_url(self) -> str:
        """最终请求地址。"""
        return f"{self.base_url}{self.endpoint_path}"

    @property
    def display(self) -> str:
        """给日志/界面看的简短标识。"""
        return f"{self.label} [{self.model}]"


class ModelOverride(BaseModel):
    """单个模型对服务商默认能力的**逐字段**覆盖。

    全部字段可选：只写"这个模型不一样"的那几项，其余继承服务商模板。

    什么时候必须用它（实测过的两类）：
      1. 同一家不同模型能力不同 —— DeepSeek 官方页面：`deepseek-flash` 支持图像理解，
         `deepseek-v4-pro` **不支持**。不写这条，程序会对 V4 Pro 照发图片。
      2. 同一家不同模型上下文/输出上限不同。

    故意 use `extra="forbid"`：写错字段名（例如 `vision`）必须当场报错，
    而不是静默继承默认值 —— 后者会让"以为设了"和"实际没设"长得一模一样。
    """

    model_config = ConfigDict(extra="forbid")

    # 这条覆盖的**依据**（强烈建议写，不写会在日志里提醒一次）。
    # 为什么要它：overrides 很容易长成一个“跟上游文档脱节的本地数据库”——
    # 今天加 supports_vision，明天就会有 supports_thinking / rate_limit ……
    # 三个月后没人知道哪一条还有效。所以定一条规矩：
    #   **只放“上游文档说不清、或我们实测过”的差异，并写清依据**。
    #   例：“DeepSeek 官方《模型 & 价格》页面：图像理解 flash 支持 / v4-pro 不支持”
    #       “实测：传图过去回 400”。
    # 用 None 而不是空串：空串会被写进用户的 YAML（`reason: ''`），那是纯噪声。
    reason: str | None = None

    supports_vision: bool | None = None
    supports_json_schema: bool | None = None
    supports_json_object: bool | None = None
    max_context: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_tokens_mode: Literal["max_tokens", "max_completion_tokens", "omit"] | None = None
    sends_temperature: bool | None = None
    sends_image_detail: bool | None = None
    forbidden_schema_keywords: list[str] | None = None
    allow_anyof_in_schema: bool | None = None
    json_schema_strict: bool | None = None
    images_per_request: int | None = Field(default=None, ge=1, le=8)
    temperature_output: float | None = Field(default=None, ge=0.0, le=2.0)
    temperature_train: float | None = Field(default=None, ge=0.0, le=2.0)
    max_concurrency: int | None = Field(default=None, ge=1)
    max_rpm: int | None = Field(default=None, ge=1)
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    image_max_bytes: int | None = Field(default=None, gt=0)
    request_timeout_s: int | None = Field(default=None, gt=0)


class ProviderSpec(BaseModel):
    """服务商模板：定义"某个 API 的端点与能力"，**不含具体 model**。

    具体模型 id 不写死（用户要求"不要占位"）：保存密钥后，程序调用
    `GET {base_url}{models_endpoint}` 拉取该服务商**真实可调用的模型列表**，
    再按此模板为每个 id 实例化一个 ModelSpec。`default_models` 只在
    "还没拉取过 / 拉取失败"时兜底，里面必须放服务商真实存在的 id。
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    base_url: str
    endpoint_path: str = "/chat/completions"
    models_endpoint: str = "/models"
    key_env: str

    # 请求形状。默认 openai（本项目内置的 12 家全部走 OpenAI 兼容端点，
    # 包括 Gemini —— 它用的是官方 /v1beta/openai 兼容端点，model 仍在 body 里）。
    api_style: str = "openai"

    supports_vision: bool
    supports_json_schema: bool
    supports_json_object: bool = False
    max_context: int = Field(gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)

    # 输出上限用**哪个参数名**、甚至"发不发"。三选一：
    #   "max_tokens"            —— 旧名字（默认；大多数兼容端点都认）
    #   "max_completion_tokens" —— 新名字。OpenAI 的推理模型（o 系 / gpt-5 及以后）
    #                              把它列为"只认这个"：发 max_tokens 会直接 400；
    #                              Kimi 官方也已把 max_tokens 标记为 deprecated。
    #   "omit"                  —— 一个都不发。阿里云百炼《结构化输出》写明：
    #                              "开启结构化输出时，请勿设置 max_tokens"
    #                              （它会把 JSON 截断成非法 JSON）。
    # 为什么必须可配：这是**真实用户会撞到的 400**，而不是理论风险 ——
    # 用户在下拉里选到推理模型就报错，而报错原文完全看不出是参数名的问题。
    max_tokens_mode: Literal["max_tokens", "max_completion_tokens", "omit"] = "max_tokens"

    # 是否发送 temperature。
    # OpenAI 的推理模型把 temperature / top_p / presence_penalty 等列为
    # **Unsupported parameters**（发了就 400）。关掉后请求里不带这个字段，
    # 由服务端用自己的默认值。
    sends_temperature: bool = True

    # 图片消息里要不要带 image_url.detail（见 ModelSpec 上的同名注释）。
    sends_image_detail: bool = True

    # schema 方言（逐家一份）。见 ModelSpec 上的同名注释与 ai/schema.py。
    forbidden_schema_keywords: list[str] = Field(default_factory=list)
    allow_anyof_in_schema: bool = True

    json_schema_strict: bool = False
    images_per_request: int = Field(default=1, ge=1, le=8)

    # 按模型 id 覆盖能力字段。
    # 存在的理由：**同一个服务商下的不同模型能力可以不同**。
    # 真实例子（DeepSeek 官方「模型 & 价格」页面写得很明确）：
    #   deepseek-flash（V4.1-Flash）：图像理解 支持；
    #   deepseek-v4-pro（V4-Pro-0813）：图像理解 **不支持**。
    # 只有服务商级的 supports_vision 时，选 V4 Pro 会照发图片 → 服务端报错；
    # 或者（更糟）用户以为能用视觉、实际拿到的是编造的画面描述。
    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)
    temperature_output: float = Field(default=0.6, ge=0.0, le=2.0)
    temperature_train: float = Field(default=0.2, ge=0.0, le=2.0)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    image_max_bytes: int = Field(default=1_500_000, gt=0)
    request_timeout_s: int = Field(default=REQUEST_TIMEOUT_S, gt=0)

    # 服务端**账户级**限流的预设值（None = 不限，按工人的并发跑）。
    # 存在的理由（2026-09-24 用户实测 + Kimi 官方《充值与限速》）：
    #   Kimi 按累计充值额分档：Tier0（¥0）并发 1 / RPM 3；Tier1（¥50）并发 15 / RPM 100 ……
    #   分档说的是"这个账户能同时跑几个请求"，跟你的机器、workers 设置无关。
    # 所以预设里直接写明，**不必先白撞一次才知道**。
    # 档位到底几档 **程序查不到**（官方没有这个接口，实测见 constants.KIMI_TIER_LIMITS），
    # 于是 Kimi 出厂按 Tier1，未充值的用户在界面上点一下红字链接即写成 Tier0
    # （那一笔写进连接文件，见 set_endpoint_limits）；其余各家目前不预设。
    # 程序还会从 429 原文里读服务端写明的真值并**只能收紧**（见 ai/client.py 的端点闸门）。
    max_concurrency: int | None = Field(default=None, ge=1)
    max_rpm: int | None = Field(default=None, ge=1)

    # 拉取失败 / 尚未拉取时的兜底模型（真实 id，不是占位）。
    default_models: list[str] = Field(default_factory=list)

    @field_validator("api_style")
    @classmethod
    def _known_api_style(cls, v: str) -> str:
        return validate_api_style(v)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("base_url 不能为空")
        return v.rstrip("/")

    @field_validator("endpoint_path", "models_endpoint")
    @classmethod
    def _ensure_leading_slash(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/"):
            v = "/" + v
        return v

    def spec_for(self, provider_key: str, model_id: str) -> ModelSpec:
        """按此模板 + 一个真实 model id 实例化出 ModelSpec。

        最后叠加 `model_overrides[model_id]` 里显式写过的字段 ——
        "这个具体模型与服务商默认不同"的那些能力（见 model_overrides 的说明）。
        覆盖是**逐字段**的：没写的字段仍继承服务商默认值。

        注意叠加时要把 reason 从"能力字段"里排除：它只是说明，
        不能当成 ModelSpec 的字段传下去（ModelSpec 没有 reason，extra=forbid 会报错）。
        """
        override = self.model_overrides.get(model_id)
        base: dict[str, Any] = {
            "label": model_id,
            "base_url": self.base_url,
            "endpoint_path": self.endpoint_path,
            "model": model_id,
            "key_env": self.key_env,
            "provider": provider_key,
            "api_style": self.api_style,
            "supports_vision": self.supports_vision,
            "supports_json_schema": self.supports_json_schema,
            "supports_json_object": self.supports_json_object,
            "max_context": self.max_context,
            "max_output_tokens": self.max_output_tokens,
            "max_tokens_mode": self.max_tokens_mode,
            "sends_temperature": self.sends_temperature,
            "sends_image_detail": self.sends_image_detail,
            "forbidden_schema_keywords": self.forbidden_schema_keywords,
            "allow_anyof_in_schema": self.allow_anyof_in_schema,
            "json_schema_strict": self.json_schema_strict,
            "images_per_request": self.images_per_request,
            "temperature_output": self.temperature_output,
            "temperature_train": self.temperature_train,
            "extra_headers": self.extra_headers,
            "extra_body": self.extra_body,
            "image_max_bytes": self.image_max_bytes,
            "request_timeout_s": self.request_timeout_s,
            "max_concurrency": self.max_concurrency,
            "max_rpm": self.max_rpm,
        }
        if override is not None:
            # exclude_none："没写"的字段必须继续继承服务商默认值，
            # 不能把 None 当成"显式关掉"。
            # 去掉 reason：它是给人看的依据，不是 ModelSpec 的字段。
            base.update(override.model_dump(exclude_none=True, exclude={"reason"}))
        return ModelSpec(**base)

    @property
    def models_url(self) -> str:
        """拉取模型列表的完整地址。"""
        return f"{self.base_url}{self.models_endpoint}"


class ConnectionSpec(ProviderSpec):
    """一条**连接**：一个 base_url + 一把密钥 + 一个备注名。

    这是用户实际在用的那个东西。服务商预设（ProviderSpec）只是**模板**，
    用来把 base_url 与能力字段填好；真正决定请求发到哪里的是连接。

    为什么要有它（用户与外部评审共同确认的设计反转）：
      现实里大量用户用的不是官方端点 —— SiliconFlow、one-api/new-api 中转、
      本地 Ollama/vLLM、公司内网网关。它们都走 OpenAI 兼容格式、key 都是
      sk- 开头，而且**一把 key 能调十几家的模型**。旧设计把"用户输入的第一顺位"
      放在"服务商"上，于是中转站用户被迫选一家厂商 —— 一选就错，
      既决定了 base_url（错的），又决定了密钥存进哪个槽（也是错的）。
      正确的主从关系是：**base_url 是事实来源，厂商名只是从它推导出来的显示标签**。

    多条连接可以共用同一个 base_url（"DeepSeek-主力"/"DeepSeek-备用"），
    各自有各自的密钥槽位 —— 这是"单槽存储"问题的解法。
    """

    model_config = ConfigDict(extra="forbid")

    # 备注名。用户起的，同时也是 keyring 账户名的一部分（见 connection_key_env）。
    name: str = ""
    # 从哪个预设继承来的（仅用于界面显示"预设：DeepSeek"，不参与请求）。
    preset: str = ""

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("连接名不能为空")
        if "::" in name:
            # 模型条目的键是 "<连接id>::<模型id>"，连接名里再出现 ::
            # 会让日志与偏好文件里的键无法一眼读懂（虽然解析不依赖它）。
            raise ValueError("连接名不能包含「::」（模型键用 :: 分隔连接与模型）")
        if any(ch in name for ch in "\r\n\t"):
            raise ValueError("连接名不能包含换行或制表符")
        return name


def connection_key_env(name: str) -> str:
    """自建连接的密钥账户名。

    带 "conn:" 前缀是刻意的：
      1. 出厂连接的账户名沿用旧环境变量名（DEEPSEEK_API_KEY 等），
         这样你**已经存在凭据管理器里的密钥不需要迁移**；
      2. 你自己建的连接一律用 "conn:<备注名>"，不会和出厂槽位撞名，
         所以"同一家两个密钥"是两把独立的钥匙，互不覆盖。
    """
    return f"conn:{name}"

class ModelsConfig(BaseModel):
    """整份 models.yaml 的结构。

    yaml 里只有 `providers:`（服务商模板）。`models:` 不在文件里 ——
    它由 load_models_config() 在加载时用 discovered_models.json（保存密钥后
    从服务商 /models 拉到的真实 id）实例化出来；拉不到时回退到
    ProviderSpec.default_models。
    """

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderSpec]
    # 连接（= 端点 + 密钥 + 备注名），从 connections.yaml 读；
    # 该文件不存在时用"出厂连接"（把预设直接当连接用，见 default_connections）。
    # 运行时字段，不来自 models.yaml。
    connections: dict[str, ConnectionSpec] = Field(default_factory=dict, exclude=True)
    # 运行时由 discovered/default_models 生成的模型实例，不来自 yaml。
    models: dict[str, ModelSpec] = Field(default_factory=dict, exclude=True)

    @field_validator("providers")
    @classmethod
    def _non_empty(cls, v: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]:
        if not v:
            raise ValueError("providers 不能为空，至少需要一个服务商")
        return v

    @property
    def active(self) -> str:
        """兼容旧字段：返回第一个模型条目名（没有模型时返回空串）。"""
        return next(iter(self.models), "")

    def active_spec(self) -> ModelSpec:
        """取"默认生效"的模型条目（第一个）。

        没有模型时抛错并给出下一步指引 —— 这不该是崩溃点，而是
        "请先保存某个服务商的 API 密钥，程序会自动拉取可调用的模型"。
        """
        if not self.models:
            raise ModelConfigError(
                "还没有任何可用模型。\n"
                "请在界面里保存某个服务商的 API 密钥 —— 程序会自动调用该服务商的 "
                "/models 接口，把真实可调用的模型列表加载进「模型」下拉。\n"
                "若你的服务商不支持 /models，请点「模型」下拉右边的「＋」手动添加，\n"
                "或直接编辑 %APPDATA%\\AutoColorDiffusion\\config\\discovered_models.json。"
            )
        return next(iter(self.models.values()))

    def choices(self) -> list[tuple[str, str]]:
        """界面下拉用：(条目名, 显示名) 列表。"""
        return [(key, spec.label) for key, spec in sorted(self.models.items())]

    def provider_models(self, provider: str) -> list[tuple[str, ModelSpec]]:
        """某个服务商下的全部模型，保持实例化顺序。"""
        return [
            (key, spec)
            for key, spec in self.models.items()
            if spec.provider == provider
        ]

    def first_model_of_provider(self, provider: str) -> tuple[str, ModelSpec] | None:
        """某个连接下"该自动选中哪个模型"。

        规则：**优先送支持视觉的模型**，没有才退回顺序里的第一个。
        为什么：本工具的每一条链路都靠缩略图判断（硬约束 #2），选中一个
        不支持图片输入的模型会把整条链路默默降级成 caption 通道 ——
        用户看到的是"模型切换成功了"，实际精度已经掉了一截。
        真实例子（DeepSeek 官方「模型 & 价格」页面）：同目录下
        `deepseek-flash` 支持图像理解、`deepseek-v4-pro` **不支持**。
        """
        items = self.provider_models(provider)
        if not items:
            return None
        for key, spec in items:
            if spec.supports_vision:
                return key, spec
        return items[0]

    def provider_of(self, key: str) -> str:
        """某个模型条目所属的**连接 id**。条目不存在时返回空串。

        历史上这个字段叫 provider（那时"连接"和"厂商"是同一个东西）；
        现在它的真实含义是连接 id，方法名保留是为了不动偏好文件里
        已经写下的 "deepseek::deepseek-v4-pro" 这类键。
        """
        spec = self.models.get(key)
        return spec.provider if spec is not None else ""

    connection_of = provider_of

    def connection_name(self, key: str) -> str:
        """某个模型条目所属连接的显示名（找不到就用 id）。"""
        cid = self.provider_of(key)
        conn = self.connections.get(cid)
        if conn is None:
            return cid
        return conn.name or conn.label or cid


# 服务商 id → 给用户看的中文名（识别出服务商后在状态栏里显示用）。
PROVIDER_LABELS: dict[str, str] = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "qwen": "通义千问",
    "glm": "智谱 GLM",
    "gemini": "Google Gemini",
    "kimi": "Kimi",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "xai": "xAI (Grok)",
    "doubao": "豆包（火山方舟）",
    "qianfan": "百度千帆",
    "anthropic": "Anthropic",
}


def detect_provider_from_key(secret: str) -> str | None:
    """从密钥串**尽力**识别服务商，返回 provider id；识别不了返回 None。

    各家密钥的真实形态（能查证的都查证过，官方文档 / 用户实测为准）：
      - 通义千问：  sk-ws 开头（用户确认的实际形态）
      - 百度千帆：  bce-v3/ 开头（官方示例 Bearer bce-v3/ALTAK-…）
      - OpenRouter：sk-or-v1-
      - Groq：      gsk_
      - xAI：       xai-
      - Perplexity：pplx-
      - Google：    AIza…（Gemini / AI Studio）
      - Anthropic： sk-ant-
      - OpenAI 新版：sk-proj- / sk-svcacct-（旧版是 sk-，已无法区分）
      - 智谱 GLM：  <数字 id>.<secret>（含一个点，点前纯数字）
      - DeepSeek / Kimi / 大量中转站：sk- 开头 —— **彼此无法区分**，
        一律返回 None，交回给界面上的「服务商」选择。
      - 豆包（火山方舟）：**故意不认**。官方公告《API Key 明文格式变更》只说
        "2026-09-17 12:00（UTC+8）之后创建的 API Key 使用新的明文格式"，
        没有给出任何可写进代码的前缀字面量；猜一个前缀的代价是把别人的密钥
        misroute 到豆包账户，比"多按一次下拉"严重得多。

    只在形态**确定唯一**时才下结论；参数里的规则顺序也是从"最具体"到"最泛"。
    ⚠️ 绝不拿"像什么"凑规则：曾经把 `sk_`（下划线）当成千问的特征，是错的
    （用户实测纠正），已删除 —— 宁可认不出回退到手动选择，也不要猜错把密钥
    存进别人家的账户。
    """
    s = secret.strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("sk-ws"):
        return "qwen"
    if low.startswith("sk-or-v1-"):
        return "openrouter"
    if low.startswith("sk-proj-") or low.startswith("sk-svcacct-"):
        return "openai"
    if low.startswith("sk-ant-"):
        return "anthropic"
    if low.startswith("gsk_"):
        return "groq"
    if low.startswith("xai-"):
        return "xai"
    if low.startswith("pplx-"):
        return "perplexity"
    if low.startswith("aiza"):
        return "gemini"
    if low.startswith("bce-v3/"):
        return "qianfan"
    # 智谱 GLM：<数字 id>.<secret>
    if "." in s:
        head = s.split(".", 1)[0]
        if head.isdigit():
            return "glm"
    return None


def user_config_path() -> Path:
    """用户可写的 models.yaml 路径。"""
    return config_dir() / "models.yaml"


def bundled_config_path() -> Path:
    """打包内的只读种子路径。"""
    return resource_root() / BUNDLED_CONFIG_RELPATH


# 服务商拉取的模型清单缓存文件名（与 models.yaml 同目录，只放模型 id）。
DISCOVERED_FILENAME = "discovered_models.json"


def discovered_models_path() -> Path:
    """已拉取模型清单的路径。"""
    return config_dir() / DISCOVERED_FILENAME


def load_discovered_models() -> dict[str, list[str]]:
    """读取"保存密钥后从 /models 拉到的真实模型清单"。

    格式：{ 服务商 id: [模型 id, ...] }。文件缺失/损坏时返回空表，
    让调用方回退到 default_models —— 读缓存失败不应当让程序起不来。
    """
    path = discovered_models_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for provider, ids in raw.items():
        if isinstance(ids, list):
            result[str(provider)] = [str(i) for i in ids if i]
    return result


def save_discovered_models(provider: str, model_ids: list[str]) -> None:
    """把某个服务商拉到的模型清单写盘（合并进现有缓存，不覆盖其他服务商）。"""
    data = load_discovered_models()
    data[provider] = list(dict.fromkeys(model_ids))  # 去重、保序
    path = discovered_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# 连接文件的文件名与结构：只放 `connections:`，与 models.yaml 分开写。
# 分开的理由：models.yaml 是**程序提供的预设目录**（升级时会自动补服务商），
# connections.yaml 是**用户自己的数据**（程序永不改动，除了用户主动增删）。
CONNECTIONS_FILENAME = "connections.yaml"

# 连接文件开头的说明。用户会手改这个文件，所以必须写清楚它是什么。
CONNECTIONS_HEADER = """# ============================================================================
# 连接（连接 = 端点 + 密钥 + 备注名）
# ----------------------------------------------------------------------------
# 这个文件是**你自己的**。程序只在"新建连接 / 删除连接"时改写它，
# 以及在一处例外上自愈：**从预设继承的能力字段**（supports_vision /
# supports_json_schema / supports_json_object / max_context / max_output_tokens /
# model_overrides / default_models）会跟上预设的修正 ——
# 因为这些值是我们写下的，写错时（实例：DeepSeek 的 json_schema）
# 留在旧连接里会让每次请求都先吃一个 400。你手改过的值一律不会被覆盖。
#
# 每一项是一个连接：
#   name                备注名（你自己起，用来区分"主力/备用/中转"）
#   base_url            事实来源：请求实际发到哪里
#   key_env             密钥在系统凭据管理器里的账户名（自建的用 conn:<备注名>）
#   api_style           请求形状，目前只支持 openai（OpenAI 兼容端点）
#   preset              从哪个预设继承的能力参数（只影响显示，不参与请求）
#
# 想加第二个 DeepSeek 密钥？复制一份、改 name 与 key_env 即可 ——
# 不要两个连接共用同一个 key_env，那会互相覆盖。
#
# 能力字段（supports_vision / supports_json_schema / max_context）：
#   从预设建的连接会继承预设的值；自建的连接取**保守默认**
#   （视觉=支持、json_schema=不支持、上下文 128k），可以在这里手改。
#
# 官方端点的 base_url 可以直接参考 config/models.yaml 里的预设。
# ============================================================================
"""


def connections_path() -> Path:
    """用户连接文件的路径（与 models.yaml 同目录）。"""
    return config_dir() / CONNECTIONS_FILENAME


def default_connections(presets: dict[str, ProviderSpec]) -> dict[str, ConnectionSpec]:
    """出厂连接：把内置预设直接当成可用连接（id 沿用服务商 id）。

    为什么必须有这一步：全新安装要"开箱即用"。如果连接只能自己建，
    第一次启动时「模型」下拉是空的、命令行也拿不到 active_spec()，
    整个程序等于不能跑。出厂连接让行为与旧版完全一致。

    出厂连接的 key_env 沿用旧的环境变量名（DEEPSEEK_API_KEY 等），
    所以你已经存在凭据管理器里的密钥**不需要任何迁移**。
    需要"同一家两把密钥"时，自建连接（key_env = conn:<备注名>）即可。
    """
    out: dict[str, ConnectionSpec] = {}
    for pid, preset in presets.items():
        out[pid] = ConnectionSpec(
            **preset.model_dump(),
            name=PROVIDER_LABELS.get(pid, preset.label),
            preset=pid,
        )
    return out


def load_connections(presets: dict[str, ProviderSpec]) -> dict[str, ConnectionSpec]:
    """读取用户连接；文件不存在时返回出厂连接。

    读坏了**不静默吞掉**：抛 ModelConfigError 并指出是哪一条连接、哪个字段 ——
    默默把你的连接丢掉，比启动时报错糟糕得多（你会以为程序把你的配置吃了）。
    """
    path = connections_path()
    if not path.is_file():
        return default_connections(presets)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelConfigError(f"无法读取连接文件 {path}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ModelConfigError(f"连接文件 {path} 的 YAML 语法错误：\n{exc}") from exc
    if raw is None:
        return default_connections(presets)
    if not isinstance(raw, dict):
        raise ModelConfigError(f"连接文件 {path} 的顶层必须是映射（connections: ...）")
    items = raw.get("connections")
    if items is None:
        return default_connections(presets)
    if not isinstance(items, dict):
        raise ModelConfigError(f"连接文件 {path} 的 connections 必须是映射（名称: 配置）")

    out: dict[str, ConnectionSpec] = {}
    for cid, entry in items.items():
        key = str(cid)
        try:
            out[key] = ConnectionSpec.model_validate(entry or {})
        except ValidationError as exc:
            raise ModelConfigError(
                f"连接文件 {path} 里的连接「{key}」校验失败：\n{exc}\n"
                "对照文件顶部的注释检查字段；不认识的字段名会直接报错（这是刻意的）。"
            ) from exc

    refreshed, changes = _refresh_connection_facts(presets, out)
    if changes:
        # 连接是从预设建的，创建时把能力字段抄了一份 —— 预设被修正后，
        # 老连接不会自己跟着变。实测后果：DeepSeek 连接继续发 json_schema，
        # 每次请求先吃一个 400。只修正"确实从预设继承、且用户没改过"的字段，
        # 然后写回（下一步若是新建/删除连接也会再写一次，不冲突）。
        log.warning(
            "连接文件里的 %d 处能力字段已跟上预设的修正（你改过的不会被覆盖）：%s",
            len(changes),
            "；".join(changes),
        )
        try:
            save_connections(refreshed)
        except OSError as exc:
            log.warning("修正连接文件失败（不影响本次运行）：%s", exc)
    return refreshed


def _refresh_connection_facts(
    presets: dict[str, ProviderSpec], connections: dict[str, ConnectionSpec]
) -> tuple[dict[str, ConnectionSpec], list[str]]:
    """把"从预设继承的能力字段"跟上预设；返回 (新连接表, 变更说明)。

    不碰 name / base_url / key_env / preset / extra_headers / 自建连接的保守默认。
    判据与 _refresh_capability_facts 一致：只有"用户没做过选择"的字段才会被改。
    """
    changes: list[str] = []
    out: dict[str, ConnectionSpec] = {}
    for cid, conn in connections.items():
        preset = presets.get(conn.preset) if conn.preset else None
        if preset is None:
            out[cid] = conn
            continue
        patch: dict[str, Any] = {}

        # A. 已知的错误能力值（服务商级与连接级同一张表）
        for (pid, field), (wrong, right) in CAPABILITY_FIXES.items():
            if pid != conn.preset or not hasattr(conn, field):
                continue
            if getattr(conn, field) == wrong and getattr(preset, field, None) == right:
                patch[field] = right

        # B. 预设里新增/改动的字段：连接里还是**字段默认值** → 说明没改过 → 跟上
        #    ⚠ 必须用属性取（getattr）而不是 preset.model_dump()：
        #    model_dump() 会把嵌套的 model_overrides 变成 dict，
        #    而下游 spec_for() 期望的是 ModelOverride 对象 ——
        #    实测会直接报 'dict' object has no attribute 'model_dump'。
        #    深拷贝是为了避免预设与多条连接共享同一个可变对象。
        for field in type(preset).model_fields:
            if field in ("name", "preset") or field in patch:
                continue
            default = _provider_field_default(field)
            if default is _MISSING:
                continue
            preset_value = getattr(preset, field, None)
            if preset_value == default:
                continue        # 预设值本身就是默认值，跟不跟上没区别
            if getattr(conn, field, None) == default:
                patch[field] = copy.deepcopy(preset_value)

        if patch:
            out[cid] = conn.model_copy(update=patch)
            changes.extend(f"{cid}.{f}" for f in sorted(patch))
        else:
            out[cid] = conn
    return out, changes


def save_connections(connections: dict[str, ConnectionSpec]) -> Path:
    """把连接写盘（整份覆盖 —— 它只有这一份真值来源）。

    exclude_none：model_overrides 里的 ModelOverride 是"全部字段可选"的模型，
    不排除 None 就会在用户文件里写出一堆 `xxx: null`（合法但全是噪声，
    而且容易让人以为"程序设了什么"）。
    """
    body = yaml.safe_dump(
        {"connections": {
            cid: conn.model_dump(exclude_none=True) for cid, conn in connections.items()
        }},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    path = connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONNECTIONS_HEADER + "\n" + body, encoding="utf-8")
    log.info("连接已保存：%s（%d 条）", path, len(connections))
    return path


def upsert_connection(
    presets: dict[str, ProviderSpec],
    conn: ConnectionSpec,
    conn_id: str = "",
) -> str:
    """新增/替换一条连接并写盘，返回它的 id（默认取备注名）。

    注意：写盘时会**先把出厂连接落成文件**（如果文件还不存在）。
    否则第一次新建连接会生成一个只有这一条的 connections.yaml，
    界面上的其余各家会"凭空消失"——那是最容易被误判成"程序把我配置吃了"的行为。
    """
    connections = load_connections(presets)
    cid = (conn_id or conn.name or ctx_fallback_id(conn)).strip()
    if not cid:
        raise ModelConfigError("连接必须有名字（name）")
    connections[cid] = conn
    save_connections(connections)
    return cid


def ctx_fallback_id(conn: ConnectionSpec) -> str:
    """没能取到名字时的兜底 id（用域名，避免"必须手填唯一名"）。"""
    host = conn.base_url.split("//", 1)[-1].split("/", 1)[0]
    return host or "connection"


def delete_connection(presets: dict[str, ProviderSpec], conn_id: str) -> bool:
    """删除一条连接（返回是否真的删了）。密钥**不删**，见下方说明。"""
    connections = load_connections(presets)
    if conn_id not in connections:
        return False
    del connections[conn_id]
    save_connections(connections)
    # 刻意不删 keyring 里的密钥：删连接往往是"改错了重建"，
    # 顺手清掉密钥会让用户还得去控制台复制一遍。留着不影响任何东西，
    # 重建同名连接时还能直接复用。想彻底删，去 Windows 凭据管理器里删。
    log.info("已删除连接「%s」（密钥保留在凭据管理器里）", conn_id)
    return True


def set_endpoint_limits(
    presets: dict[str, ProviderSpec],
    conn_id: str,
    *,
    concurrency: int | None,
    rpm: int | None,
) -> Path:
    """把一条连接的**账户级**限速写进 connections.yaml，返回写入的文件路径。

    为什么写连接文件而不是 models.yaml：加载时模型是由**连接**实例化的
    （load_models_config 里 `conn.spec_for(...)`），models.yaml 只是模板；
    限速是"这个账户能用多少并发"，跟账户（=连接/密钥）绑在一起才对。
    文件还不存在时会先把出厂连接落盘 —— 这是既有约定，见 upsert_connection。

    传 None 表示"不限"（不写死上限）；界面上的档位切换永远传具体数字，
    因为"我选了 Tier0"这件事必须显式记下来，否则重启后界面就没法判断该显示
    "切换到 Tier0" 还是"改回 Tier1"。
    """
    connections = load_connections(presets)
    conn = connections.get(conn_id)
    if conn is None:
        raise ModelConfigError(f"找不到连接「{conn_id}」，无法写入限速")
    conn.max_concurrency = concurrency
    conn.max_rpm = rpm
    path = save_connections(connections)
    log.info(
        "连接「%s」的限速已更新：并发 %s / 每分钟 %s 次（写入 %s）",
        conn_id,
        concurrency if concurrency is not None else "不限",
        rpm if rpm is not None else "不限",
        path,
    )
    return path


def _migrate_config_format(path: Path) -> None:
    """把旧版配置升级为 `providers:` 格式（一次性）。

    新格式只有 `providers:` 键。任何**没有 providers** 的旧配置
    （v1 的占位目录 / v2 的带 provider 字段的 model 目录）都会整份替换成
    内置新种子 —— 本项目当前只有一个用户，且用户明确要求移除占位，所以
    不做"保留用户自定义条目"的复杂合并。
    已是新格式但残留 `models` / `active` 键的，清掉它们（否则 extra=forbid 报错）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return

    if "providers" not in data:
        src = bundled_config_path()
        if src.is_file():
            path.write_bytes(src.read_bytes())
            log.info("模型配置已从旧格式升级为服务商目录（providers）：%s", path)
        return

    changed = False
    for key in ("models", "active"):
        if key in data:
            del data[key]
            changed = True
    if changed:
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        log.info("已清理模型配置里残留的旧键（models/active）：%s", path)


def _seed_header_comment() -> str:
    """取内置种子文件开头的注释块（第一个"既非注释也非空行"之前的所有内容）。

    只在需要重写用户配置时用作文件头，这样重写后的文件说明和内置种子一致，
    不会再出现"旧说明 + 新说明"两段叠在一起（实测老用户文件里就是两段）。
    """
    try:
        lines = bundled_config_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    head: list[str] = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    while head and not head[-1].strip():
        head.pop()
    return "\n".join(head) + "\n" if head else ""


# 曾经发出去的**错误能力声明**及其修正值。
#
# 【为什么需要这张表】老用户的 models.yaml 永远不会被种子覆盖（见 seed_user_config），
# 所以"我们把某个能力写错了"这件事到不了已经装好的机器上。而写错的后果是真实的：
# DeepSeek 的 supports_json_schema 被写成 true，实际它**不支持** json_schema，
# 每次请求都会先吃一个 400，再靠客户端降级重发 —— 请求数翻倍，
# 在"请求数 = 文件数 × 2"的预算下会直接把后面的文件挤掉。
#
# 【只改"用户没动过"的值】命中的条件同时成立才动：
#   1. 服务商 id 与字段名都在表里；
#   2. 用户配置里的值**恰好等于我们当年写错的那个值**（说明是继承来的）；
#   3. 种子里的新值确实不同。
# 用户自己改过的值一律不碰 —— 那是他的选择，不是我们的错误。
#
# 实例：DeepSeek（依据 api-docs.deepseek.com 的《JSON Output》指南与
# 《模型 & 价格》页面，2026-09 核对）。
CAPABILITY_FIXES: dict[tuple[str, str], tuple[Any, Any]] = {
    ("deepseek", "supports_json_schema"): (True, False),
    ("deepseek", "max_context"): (128000, 1000000),
    ("deepseek", "max_output_tokens"): (4096, 16384),
    ("deepseek", "default_models"): (["deepseek-v4-pro", "deepseek-flash"],
                                      ["deepseek-flash", "deepseek-v4-pro"]),
    # 2026-09 第二轮官方文档核对（用户要求"换个 API 也要能直接用"）：
    #   · 阿里云百炼《结构化输出》：「多模态输入（图像、视频、音频等）不支持
    #     json_schema，会自动降级为 json_object，schema 约束不生效」——
    #     而本工具的**每一次请求都带图**，所以 qwen 写 json_schema 是无意义的，
    #     只在部分模型上有风险。
    #   · 智谱《结构化输出》只文档了 `{"type":"json_object"}`。
    #   · Kimi 的 json_schema 要求 schema 符合 MFJS 规范（不符合会报错或 warning），
    #     而我们下发的 schema 含 minimum/maximum 等关键字，并非 MFJS 子集。
    # 三家统一改成 json_object + 本地强校验（json_schema 仍可在自己的配置里手动打开）。
    ("qwen", "supports_json_schema"): (True, False),
    ("glm", "supports_json_schema"): (True, False),
    ("kimi", "supports_json_schema"): (True, False),
    # 2026-09 第三轮官方文档核对（用户要求"加入豆包，并顺便核查百度千帆"）：
    #   · 千帆《模型列表》里 ernie-4.5-turbo-vl 的"最大输出"是 [2，16384]，
    #     我们当年写的 4096 是**随手抄的 DeepSeek 值**，不是千帆的值；
    #   · 当年的兜底第二个模型 ernie-4.5-8k-preview 只在《视觉理解》页的
    #     "支持模型"文字里出现过，不是模型列表里的正经一行 → 换成表里确凿
    #     存在的 ernie-5.0（128k 上下文、最大输出 65536，且文本/视觉两张表都在）。
    # 两条都只在"用户的值等于我们当年那个值"时才改 —— 用户自己挑的模型不动。
    ("qianfan", "max_output_tokens"): (4096, 16384),
    ("qianfan", "default_models"): (["ernie-4.5-turbo-vl", "ernie-4.5-8k-preview"],
                                    ["ernie-4.5-turbo-vl", "ernie-5.0"]),
    # 2026-09-24（用户实际报错：调 GLM 时整批超时失败）：
    #   旧种子给 qwen/glm/kimi/qianfan 写的 request_timeout_s 是 60，新种子改成了 180；
    #   但 models.yaml 对老用户**永不覆盖**，而 _refresh_capability_facts
    #   只补“新增字段”、不会把已有值改成种子值 —— 所以 180 从未到达我们已经装好的机器。
    #   实测症状：glm-5.3-flash（思考模型）一次带图请求要 51 秒，4 并发时多发几张
    #   就稳定越过 60 秒 → 整批“请求超时”失败。
    # 同样修正输出上限（旧种子写小了，思考模型的推理 token 也算在里面）。
    # 判据不变：只在“用户的值恰好等于我们当年写的值”时改。
    ("qwen", "request_timeout_s"): (60, 180),
    ("glm", "request_timeout_s"): (60, 180),
    ("kimi", "request_timeout_s"): (60, 180),
    ("qianfan", "request_timeout_s"): (60, 180),
    # 同一轮的普适修正：**凡是会带图的连接**都统一到 180 秒。
    # 依据：我们的请求是"1–多张图 + 份量不小的结构化 JSON"，思考模型下实测
    # 单次可达 51 秒（GLM）；写 60 秒时第一批里就总有几张刚好越线。
    # 为什么不能只靠“超时后翻倍重试”：那会白耗一次预算（预算 = 文件数 × 2），
    # 每张都白花一次时后面的文件会被预算卡住 —— 第一枪就得打中。
    # 不看图的 perplexity 维持 60（它的请求只是短文本）。
    ("deepseek", "request_timeout_s"): (60, 180),
    ("openai", "request_timeout_s"): (60, 180),
    ("gemini", "request_timeout_s"): (60, 180),
    ("openrouter", "request_timeout_s"): (60, 180),
    ("groq", "request_timeout_s"): (60, 180),
    ("xai", "request_timeout_s"): (60, 180),
    ("qwen", "max_output_tokens"): (4096, 8192),
    ("glm", "max_output_tokens"): (8192, 16384),
    ("kimi", "max_output_tokens"): (8192, 16384),
}

# 出厂默认值的修正（**只作用于 models.yaml**，不作用于连接文件）。
#
# 与 CAPABILITY_FIXES 的区别：CAPABILITY_FIXES 会同时改"连接文件"里的值，
# 而连接文件里的并发/限速可能是**用户在界面上主动选的档位**（见
# constants.KIMI_TIER_LIMITS 与主窗口的档位切换），改它等于每次启动都把用户的
# 选择抹掉 —— 自相矛盾。所以这条只把**模板**（models.yaml）里的出厂默认改过来，
# 让"从没选过档位"的用户拿到新默认；选过的人由连接文件说了算。
#
# 2026-09-24（用户要求）：Kimi 出厂默认从 Tier0（并发 1 / RPM 3）改为
# Tier1（并发 15 / RPM 100）。理由：档位由**累计充值额**决定，而官方没有
# 查询档位的接口（实测余额接口只回余额、对话响应无 ratelimit 头），按 Tier0
# 跑会让充过钱的用户白等（实测 4 并发 → 18 张只成 1 张）。
# 未充值的用户在界面点一下红字提示即可切回 Tier0（写进连接文件，优先于这里）。
# 判据同上：只在用户的值恰好等于我们当年写的那个值时改。
_DEFAULT_FIXES: dict[tuple[str, str], tuple[Any, Any]] = {
    ("kimi", "max_concurrency"): (1, 15),
    ("kimi", "max_rpm"): (3, 100),
}


def _refresh_capability_facts(user_providers: dict[str, Any], seed_providers: dict[str, Any]) -> list[str]:
    """修正用户配置里"继承来的错误能力值"，并补上种子里新增的字段。

    返回给日志用的变更说明（没改动就返回空列表）。

    两类改动，都只影响"用户从未在这个字段上做过选择"的情况：
      A. CAPABILITY_FIXES 命中的已知错误值 → 改成正确值；
      B. 种子里**新增的字段**（例如 supports_json_object / model_overrides）
         在用户条目里根本不存在 → 直接补上。
    刻意不做的事：不把用户已有的值改成种子的值（那是覆盖用户选择）。
    """
    changes: list[str] = []
    for provider_id, entry in user_providers.items():
        if not isinstance(entry, dict):
            continue
        seed_entry = seed_providers.get(provider_id)
        if not isinstance(seed_entry, dict):
            continue

        for (pid, field), (wrong, right) in CAPABILITY_FIXES.items():
            if pid != provider_id or field not in entry:
                continue
            if entry[field] == wrong and seed_entry.get(field) == right:
                entry[field] = right
                changes.append(f"{provider_id}.{field}: {wrong!r} → {right!r}")

        # A'. 出厂默认的档位修正（只改 models.yaml，**刻意不放进 CAPABILITY_FIXES**）
        #     CAPABILITY_FIXES 同时作用于连接文件，而连接文件里的 1/3 可能是用户
        #     在界面上主动选的 Tier0 —— 放进去会让界面选择每次启动都被改回 Tier1。
        for (pid, field), (wrong, right) in _DEFAULT_FIXES.items():
            if pid != provider_id or field not in entry:
                continue
            if entry[field] == wrong and seed_entry.get(field) == right:
                entry[field] = right
                changes.append(f"{provider_id}.{field}: {wrong!r} → {right!r}（出厂默认改档）")

        for field, seed_value in seed_entry.items():
            if field in entry:
                continue
            default = _provider_field_default(field)
            if default is not _MISSING and seed_value == default:
                # 字段缺失时行为与种子一致（比如 endpoint_path 的默认值就是
                # 种子里写的那个）→ 不必回填，免得把用户文件搞得喧宾夺主。
                # ⚠ 不能用"字段名白名单"代替这个判断：supports_json_object 的
                #   默认值是 False，而种子写的是 True —— 漏回填会让老用户永远
                #   用不上 json_object 档（实测踩到）。
                continue
            entry[field] = seed_value
            changes.append(f"{provider_id}.{field}: 补上（种子里新增）")
    return changes


_MISSING = object()


def _provider_field_default(field: str) -> Any:
    """ProviderSpec 上该字段的默认值（不存在或取不到时返回 _MISSING）。"""
    info = ProviderSpec.model_fields.get(field)
    if info is None:
        return _MISSING
    try:
        return info.get_default(call_default_factory=True)
    except Exception:  # pragma: no cover - pydantic 内部异常不值得上报
        return _MISSING


def _upgrade_config_providers(path: Path) -> list[str]:
    """把内置种子里"用户配置还没有的服务商"补进用户配置，返回补进去的 key。

    【为什么必须有这一步 —— 实测踩过】
    seed_user_config() 只在目标文件**不存在**时复制，老用户的 models.yaml
    一旦生成就永远不会再更新；而 _migrate_config_format() 只处理"完全没有
    providers 键"的 v1/v2 文件。于是当内置目录从 2 家扩展到 12 家时，
    老用户界面的「服务商」下拉里仍然只有 deepseek + openai ——
    看起来像"程序只支持这两家"。这是升级路径的漏洞，不是 UI 的 bug。

    策略是**外科式增补**，不是整份替换：
      - 用户已有的服务商条目原样保留（用户改过的 base_url / 备注不会被冲掉）
      - 种子里有、用户没有的服务商追加进去，顺序与种子一致
      - 重写时用种子的注释头（见 _seed_header_comment）
      - 顺带修正已知的错误能力值 / 补齐新增字段（见 _refresh_capability_facts，
        只动"用户从没改过"的字段）

    刻意**不**支持"用户删除某服务商"：内置目录被视为程序能力的一部分，
    不是用户数据，缺哪个就补哪个。真要禁用某家，请清空它的 default_models
    或者不填它的密钥。
    """
    src = bundled_config_path()
    if not src.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        seed = yaml.safe_load(src.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict) or not isinstance(seed, dict):
        return []
    user_providers = data.get("providers")
    seed_providers = seed.get("providers")
    if not isinstance(user_providers, dict) or not isinstance(seed_providers, dict):
        return []

    missing = [key for key in seed_providers if key not in user_providers]
    fixes = _refresh_capability_facts(user_providers, seed_providers)
    if not missing and not fixes:
        return []

    # 种子的顺序优先（deepseek/openai 保持在前），用户独有的服务商追加在后。
    merged: dict[str, Any] = {}
    for key, value in seed_providers.items():
        if key in missing:
            merged[key] = value               # 全新条目：整份取种子
        else:
            merged[key] = user_providers.get(key, value)
    for key, value in user_providers.items():
        if key not in merged:
            merged[key] = value

    body = yaml.safe_dump(
        {"providers": merged},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    path.write_text(_seed_header_comment() + "\n" + body, encoding="utf-8")
    if missing:
        log.info(
            "模型配置已补充 %d 个缺失的服务商：%s（文件：%s）",
            len(missing), ", ".join(missing), path,
        )
    if fixes:
        log.warning(
            "模型配置已修正 %d 处「继承来的错误能力值 / 缺失字段」（你改过的值不会被覆盖）：%s",
            len(fixes),
            "；".join(fixes),
        )
    return missing


def seed_user_config() -> bool:
    """首次运行时把种子配置复制到用户目录。

    返回 True 表示本次确实复制了（用于日志提示）。
    只在目标不存在时复制，绝不覆盖用户已经改过的配置。
    """
    target = user_config_path()
    if target.exists():
        return False
    src = bundled_config_path()
    if not src.is_file():
        raise ModelConfigError(
            f"找不到内置模型配置：{src}\n"
            "开发模式下请确认项目根目录存在 config/models.yaml；\n"
            "打包模式下请确认 spec 的 datas 已包含 config/models.yaml。"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(src.read_bytes())
    log.info("已释放默认模型配置到 %s", target)
    return True


def _advise_missing_override_reasons(connections: dict[str, ConnectionSpec]) -> None:
    """model_overrides 没写 reason 时提醒一次（不强拦，但不允许它默默生长）。

    为什么用“提醒”而不是“拒绝加载”：
    加一个必填字段会直接打破已有配置的加载（而能力覆盖本身是合法的）；
    但完全不提醒，三个月后没人知道某条覆盖的依据是什么。
    提醒是这两者之间的最小代价：写进日志，不拦住任何人。
    """
    missing: list[str] = []
    for cid, conn in connections.items():
        for model_id, override in (conn.model_overrides or {}).items():
            if not (override.reason or "").strip():
                missing.append(f"{cid}.{model_id}")
    if missing:
        log.info(
            "有 %d 条 model_overrides 没写 reason（依据）：%s。"
            "建议写成“官方文档出处”或“实测得到的现象”—— 否则日后没人能判断它还该不该留。",
            len(missing),
            "、".join(missing),
        )


def load_models_config() -> ModelsConfig:
    """加载并校验模型配置。

    失败时抛 ModelConfigError，消息里带上文件路径与原始 yaml 错误，
    因为这些是用户在界面上唯一能看到的线索。
    """
    seed_user_config()
    path = user_config_path()
    _migrate_config_format(path)
    if not path.is_file():
        raise ModelConfigError(f"模型配置文件不存在：{path}")
    # 老用户配置补服务商：内置目录后来新增的服务商必须能到达已有安装，
    # 否则界面「服务商」下拉里永远只有最初那两家。
    _upgrade_config_providers(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelConfigError(f"无法读取模型配置 {path}：{exc}") from exc

    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ModelConfigError(f"模型配置 YAML 语法错误 {path}：\n{exc}") from exc

    if not isinstance(data, dict):
        raise ModelConfigError(f"模型配置 {path} 的顶层必须是映射（key: value），实际是 {type(data).__name__}")

    try:
        config = ModelsConfig.model_validate(data)
    except ValidationError as exc:
        raise ModelConfigError(
            f"模型配置 {path} 校验失败：\n{exc}\n"
            "请对照 config/models.yaml 顶部的注释检查必填字段是否齐全。"
        ) from exc

    # 连接 = 用户实际在用的东西（出厂连接 + 自建连接）。
    # 模型目录由**连接**实例化，不再由预设实例化：预设只是模板。
    config.connections = load_connections(config.providers)
    _advise_missing_override_reasons(config.connections)

    # 用「已拉取的真实模型清单」实例化出下拉条目；拉不到就用连接里的兜底模型。
    discovered = load_discovered_models()
    models: dict[str, ModelSpec] = {}
    for conn_id, conn in config.connections.items():
        ids = discovered.get(conn_id) or conn.default_models
        for model_id in ids:
            models[f"{conn_id}::{model_id}"] = conn.spec_for(conn_id, model_id)
    config.models = models
    if models:
        log.info(
            "连接目录已加载：%s（%d 个模型）。",
            ", ".join(config.connections),
            len(models),
        )
    else:
        log.warning(
            "当前没有任何可用模型：%d 条连接都没有模型 id（保存密钥后会自动拉取，"
            "或点「＋」手动添加）。",
            len(config.connections),
        )
    return config
