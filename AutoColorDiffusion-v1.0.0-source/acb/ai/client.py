# -*- coding: utf-8 -*-
"""HTTP 客户端与请求预算（硬约束 #7 / #18）。

超时策略：**以模型条目的 `request_timeout_s` 为准**（`config/models.yaml` 里
看图连接统一 180s；Perplexity 不看图、保持 60s）。
    connect / read 两种超时：
        connect=CONNECT_TIMEOUT_S（默认 5s）—— TCP+TLS 握手在正常网络下 <2s，
                    迟迟连不上说明网络/DNS 有问题，快速失败比陪着读超时更好；
        read=spec.request_timeout_s，没配才回退到 REQUEST_TIMEOUT_S（默认 5s，
                    那个 5s 只是"密钥占位时快速失败"的兜底，不是真实可用的值）。
    超时后重试会把读超时按 TIMEOUT_ESCALATION_FACTOR 放大（天花板
    TIMEOUT_ESCALATION_CEILING）—— 强制思考的模型（GLM-5.3 实测一次带图 51s）
    常见的情况就是第一枪没打完就超时。

退避策略：指数退避 + 抖动。
    base=1s、factor=2 → 1s / 2s / 4s；
    ±20% 抖动（硬约束 #7 要求指数退避，抖动是本项目额外加的防护）：
    并发时若所有失败请求同步重试，会形成脉冲反复撞限流窗口，
    抖动把时间打散，是消除"惊群"的最小代价手段。

成本防护（硬约束 #18）：单批最大请求数 = 文件数 × BUDGET_MULTIPLIER，含重试。
    超出即停止并告警。计数在这里统一进行，避免各处自己算导致统计不准。
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from ..config import ModelSpec
from ..constants import (
    BACKOFF_BASE_S,
    BACKOFF_FACTOR,
    BACKOFF_JITTER,
    CONNECT_TIMEOUT_S,
    MAX_PARSE_RETRIES,
    REQUEST_TIMEOUT_S,
    TIMEOUT_ESCALATION_CEILING,
    TIMEOUT_ESCALATION_FACTOR,
)
from ..errors import (
    BudgetExceededError,
    EmptyContentError,
    QuotaExceededError,
    SchemaValidationError,
    StopRequestedError,
    TruncatedOutputError,
)
from ..logging_setup import get_logger
from ..paths import state_dir

log = get_logger("ai.client")

# 需要重试的 HTTP 状态码。
# 429：多数情况是限流，可重试 —— **但正文里写"配额/计费"的那种不是**：
#      那属于账户侧问题（见 _QUOTA_PATTERN），重试只会白花一次请求与预算，
#      所以 429 分支里要**先分类再决定重不重试**。
# 5xx：服务端临时故障。
# 4xx 的其他码（401 密钥错、404 模型名错、400 请求体非法）重试没有意义，
# 只会白烧配额并掩盖真正的问题，因此直接失败并把错误抛给用户。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})

# 记录响应体摘要的最大长度。用于日志排查，但不能把整个响应写进日志
# （响应里可能含模型回显的敏感内容，且会撑爆日志文件）。
RESPONSE_SNIPPET_LIMIT = 2000

# “这个端点拒绍过某种 response_format”的**持久记忆**文件名（放在数据目录的 state/ 下）。
#
# 【为什么必须落盘 —— 自愈不能白做一次就忘】
# 只在进程内记住的话，下次启动又会把同样被拒的字段再发一遍：
# 一次白花的请求（还带着 base64 图片），而且日志里每次都重复一条警告。
# 这个文件是**运行时状态**，不是用户配置：程序写、人可以读、删掉即可重试。
# 存在 state/ 而不是 connections.yaml：后者是用户数据，
# 只在“新建/删除连接”与“预设能力修正”时被改，不应再混进运行时探测结果。
ENDPOINT_CAPS_FILENAME = "endpoint_capabilities.json"


def endpoint_caps_path() -> Path:
    """端点能力记忆文件路径（每台机器的数据目录下）。"""
    return state_dir() / ENDPOINT_CAPS_FILENAME


def load_rejected_formats() -> dict[str, list[str]]:
    """读出"哪些 base_url 拒绍过哪些 response_format 类型"。

    读坏了一律返回空字典：这个文件只是加速与省钱的**建议**，
    它出问题绝不能拦住整个任务（宁可多花一次请求重探）。
    """
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    if isinstance(data, dict):
        for base, entry in data.items():
            formats = entry.get("rejected_formats") if isinstance(entry, dict) else entry
            if isinstance(formats, list):
                out[str(base)] = [str(f) for f in formats]
    return out


def remember_endpoint_limit(
    base_url: str,
    *,
    concurrency: int | None = None,
    rpm: int | None = None,
    evidence: str = "",
) -> None:
    """记下"这个端点限制同时在飞 / 每分钟多少个请求"（写进同一份端点能力记忆）。

    为什么值得落盘：限流是**账户级属性**，不会因为重启就变。
    记下来，下次启动就直接按它跑 —— 不再先白撞几个 429 才学会。
    """
    if not base_url or (concurrency is None and rpm is None):
        return
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        entry = data.get(base_url)
        if not isinstance(entry, dict):
            entry = {}
        if concurrency is not None and concurrency >= 1:
            entry["max_concurrency"] = int(concurrency)
        if rpm is not None and rpm >= 1:
            entry["max_rpm"] = int(rpm)
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        entry["last_evidence"] = (evidence or "")[:300]
        data[base_url] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 磁盘问题不该影响主流程
        log.debug("写入端点限流记忆失败（不影响本次运行）：%s", exc)


def load_endpoint_limits() -> dict[str, dict[str, int]]:
    """读出"哪些端点限制了多少并发 / 每分钟多少次"（上次运行学到的）。读坏了一律当空。

    超过 ENDPOINT_MEMORY_TTL_S 的旧记录会被忽略：限流是**账户档位**的属性，
    而档位会随着充值升级而变（官方按累计充值额分档）。记太久就会把一个
    已经升级的账户永久限在过去那个档位上 —— "充了钱还是跑不动"是比
    "多白撞一个 429"严重得多的问题。丢弃的代价只是重新学一次。
    """
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, int]] = {}
    if isinstance(data, dict):
        for base, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if _memory_entry_expired(entry):
                log.info(
                    "端点限流记忆已过期（超过 %d 天），本次不采用：%s。"
                    "若服务端仍然限流，程序会重新从 429 原文里学。",
                    ENDPOINT_MEMORY_TTL_S // 86400,
                    base,
                )
                continue
            facts: dict[str, int] = {}
            for key in ("max_concurrency", "max_rpm"):
                try:
                    number = int(entry.get(key) or 0)
                except (TypeError, ValueError):
                    number = 0
                if number >= 1:
                    facts[key] = number
            if facts:
                out[str(base)] = facts
    return out


def _memory_entry_expired(entry: dict[str, Any]) -> bool:
    """这条记忆是不是老到不该再信（没有/读不懂时间戳 → 当作没过期，宁可保守）。"""
    raw = entry.get("last_seen")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return (datetime.now() - seen).total_seconds() > ENDPOINT_MEMORY_TTL_S


def forget_endpoint_limit(base_url: str, *, reason: str = "") -> bool:
    """忘掉这个端点的限流记忆（只清限流那一部分，温度等其它记忆保留）。

    【什么时候该调它 —— 用户在界面上明确选了档位】
    学到的值来自真实 429，但它是一个**观察**；用户的选择是一个**决定**。
    观察可能已经过时（他刚充值升级到 Tier1），若继续按学习值取严，
    用户在界面上选完 Tier1 后仍然被压到 1 个并发 —— 那是"点了没用"，
    比多撞一个 429 严重得多。所以：用户选完就作废旧记忆，重新按他的选择跑；
    如果他选错了（账户仍是 Tier0），下一个 429 会重新学回来（自愈）。

    返回是否真的清了东西（日志与界面提示要用）。
    """
    global _LEARNED_LIMITS
    if not base_url:
        return False
    cleared = False
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        entry = data.get(base_url) if isinstance(data, dict) else None
        if isinstance(entry, dict):
            for key in ("max_concurrency", "max_rpm", "last_evidence"):
                cleared = entry.pop(key, None) is not None or cleared
            # 只剩 model_temperature 之类的其它事实 → 条目保留，不要连坐删除。
            if not entry:
                data.pop(base_url, None)
            else:
                data[base_url] = entry
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 磁盘问题不该影响主流程
        log.debug("作废端点限流记忆失败（不影响本次运行）：%s", exc)
        return False

    # 进程内的两处缓存也要清：已经建好的闸门里还揣着刚才那个更严的值，
    # 不清就会"配置改了、界面也说了，实际仍被压着"。
    with _GATES_LOCK:
        _GATES.pop(base_url, None)
    _LEARNED_LIMITS = None
    if cleared:
        log.info("已作废 %s 的限流记忆（%s），改按当前配置跑。", base_url, reason or "用户选择")
    return cleared


def remember_fixed_temperature(
    base_url: str, model: str, value: float, evidence: str = ""
) -> None:
    """记下"这个模型只接受这个 temperature"（写进同一份端点能力记忆）。"""
    if not base_url or not model:
        return
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        entry = data.get(base_url)
        if not isinstance(entry, dict):
            entry = {}
        table = entry.get("model_temperature")
        if not isinstance(table, dict):
            table = {}
        table[str(model)] = float(value)
        entry["model_temperature"] = table
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        entry["last_evidence"] = (evidence or "")[:300]
        data[base_url] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已记住：%s 的 %s 只能使用 temperature=%s。", base_url, model, value)
    except OSError as exc:  # pragma: no cover - 磁盘问题不该影响主流程
        log.debug("写入固定温度记忆失败（不影响本次运行）：%s", exc)


def load_fixed_temperature(base_url: str, model: str) -> float | None:
    """读出"这个模型只接受的 temperature"（没记过就是 None）。"""
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(base_url) if isinstance(data, dict) else None
    table = entry.get("model_temperature") if isinstance(entry, dict) else None
    value = table.get(model) if isinstance(table, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def remember_rejected_format(base_url: str, fmt: str, evidence: str) -> None:
    """记下"这个端点拒绍了这种 response_format"（下次不再先踩一次）。

    同时记下时间与服务端原文：三个月后回头看这份文件时，
    “当时到底回了什么”比“当时判断了什么”有用得多。
    """
    if not base_url or not fmt:
        return
    path = endpoint_caps_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
        entry = data.get(base_url)
        if not isinstance(entry, dict):
            entry = {"rejected_formats": []}
        formats = entry.get("rejected_formats")
        if not isinstance(formats, list):
            formats = []
        if fmt not in formats:
            formats.append(fmt)
        entry["rejected_formats"] = formats
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        entry["last_evidence"] = (evidence or "")[:300]
        data[base_url] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            "已记住：%s 不支持 response_format.%s。下次不再发送该字段（想重试请删 %s）。",
            base_url,
            fmt,
            path,
        )
    except OSError as exc:  # pragma: no cover - 磁盘问题不该影响主流程
        log.debug("写入端点能力记忆失败（不影响本次运行）：%s", exc)


# ============================================================================
# 端点并发闸门（429 专用）
# ============================================================================
# 依据（2026-09-24 用户实测）：Kimi 的 429 原文写得很明白 ——
#   `Your account ... request reached max organization concurrency: 1, please try again after 1 seconds`
# 也就是"这个组织同时在飞的请求数上限就是 1"。而我们默认 4 个工人一起发，
# 必然 3 个被拒；靠"睡 1 秒再试一次"救不回来（重试的 4 个又同时撞上去，还是 3 个被拒）——
# 实测结果是 18 张里只有 1 张成功。
# 所以这里做两件事：
#   1. 学：从 429 原文里读出这个端点允许的并发数，落盘记住（下次启动直接按它跑，不白撞）；
#   2. 让：任何一次 429 都触发**全端点冷却**（优先用标准的 Retry-After 头），
#      冷却期间所有工人一起等 —— 免得"你退一秒我进一秒"地互相撞。
#
# ⚠ 只认服务端**原文里写明的数字**，不做任何推测；读不出来就只冷却、不设上限，日志如实说明。
# 实测（2026-09-24）Kimi 会给两种不同的 429 正文（同一个账号）：
#   · `request reached max organization concurrency: 1`（同时在飞只能 1 个）
#   · `request reached organization max RPM: 3`（每分钟只能 3 次请求）
# 两条都必须接住 —— 只管并发不管 RPM，一个 18 张的批次仍然会全部撞墙。
_CONCURRENCY_PATTERN = re.compile(r"concurrency\s*[:：]?\s*(\d+)", re.IGNORECASE)
_RPM_PATTERN = re.compile(r"\bRPM\s*[:：]?\s*(\d+)", re.IGNORECASE)
# 正文里的"please try again after N seconds"（Retry-After 头的兼容回退；没有头才用它）。
_RETRY_AFTER_BODY_PATTERN = re.compile(r"try again after\s*(\d+)\s*second", re.IGNORECASE)
# 冷却时长上限：服务端写一个很大的数时不能把整批卡死（宁可真撞一次 429）。
RATE_LIMIT_COOLDOWN_CAP_S = 30.0

# 429 里的“账户配额/额度”类错误（与“调用频率限流”完全不是一回事）。
#
# 【官方依据】阿里云百炼《错误码》（2026-09-22 版）把它们分在两个条目：
#   · `429-Throttling.RateQuota/LimitRequests/…` —— RPS/RPM 触发限流；
#     正文如 `You have exceeded your request limit.`；
#   · `429-Throttling.AllocationQuota/insufficient_quota` —— Token 配额（TPS/TPM）或
#     免费额度/账单额度用尽；正文就是
#     `Allocated quota exceeded, please increase your quota limit.` 与
#     实测碰到的 `You exceeded your current quota, please check your plan and billing details.`
# 另有 `429-BudgetLimitExceeded`（预算管理里的额度用完）、
# `429-Prepaid/PostpaidBillOverdue`（账单到期）、`400/403-Arrearage`（欠费）——
# 都是“账户侧要人去处理”的事，重试没有意义。
# 判据只用“服务端原文里明写了配额/计费字样”，不靠状态码猜：
# 这样 Kimi 那种 `request reached max organization concurrency: 1` 不会被误伤。
_QUOTA_PATTERN = re.compile(
    r"("
    r"exceeded your current quota"        # 百炼实测正文
    r"|allocated quota exceeded"          # 百炼官方文案
    r"|insufficient_quota"
    r"|allocationquota"
    r"|free allocated quota"
    r"|exceeded your current requests list"
    r"|budgetlimitexceeded|budget.{0,20}(exhaust|exceed|limit)|budget management"
    r"|bill is overdue"
    r"|arrearage|in good standing"
    r"|check your plan and billing"
    r"|quota.{0,12}(exhaust|exceed|insufficient)"
    r"|free tier.{0,20}(exhaust|used up)"
    r"|余额不足|额度不足|额度.{0,4}(用尽|耗尽|已满)|欠费"
    r")",
    re.IGNORECASE,
)


def _quota_exhausted(body: str) -> bool:
    """这个 429（或任意错误正文）是不是在说“账户配额/额度不够了”。

    故意**只认服务端原文里的字样**，不靠状态码、不猜：
    猜错的方向都很贵 —— 把额度问题当限流处理会白重试、白冷却，还会把
    端点限流记忆写小（用户充值后仍然被压着跑）。
    """
    return bool(_QUOTA_PATTERN.search(body or ""))


# 账户级的“认证/权限”错误（与文件无关，用户改完配置就能重跑）。
_AUTH_PATTERN = re.compile(
    r"(API 返回 401|API 返回 402|invalid[_ ]api[_-]?key|incorrect api key"
    r"|InvalidApiKey|invalid access token|no api key provided"
    r"|密钥无效|密钥错误|无效的密钥)",
    re.IGNORECASE,
)


def is_account_level_error(text: str) -> bool:
    """这个错误是“账户侧要人去处理”的（额度/计费/密钥），而不是“这张图有问题”。

    用途：**这种错误不该把文件记成永久跳过**。
    否则会出现一个很难发现的陷阱：用户账户额度用尽导致一批文件各失败 2 次 →
    这 2 次与文件本身无关，但它们已经把这几个文件推到“永久跳过”；
    用户充完值再跑，这些文件仍然不会处理（只能再点一次「清除失败记录」）。

    注意：刻意**不**把 400/403 一律归入此类 —— 百炼的 403 里既有“密钥/权限”
    （账户级），也有 `DataInspectionFailed`（这张图被绿网拦，**确实是这张图的问题**）。
    """
    body = text or ""
    return _quota_exhausted(body) or bool(_AUTH_PATTERN.search(body))


# 统计每分钟请求数的窗口长度（秒）。标准就是 60 秒。
RATE_WINDOW_S = 60.0
# 学到的限流值最多信多久（秒）。7 天：限流跟着**账户档位**走，而档位随充值升级而变
# （Kimi 就是按累计充值额分档）。记太久会把已升级的账户永久限在过去那档 ——
# "充了钱还是跑不动"比"多白撞一次 429"严重得多，所以宁可定期重新学。
ENDPOINT_MEMORY_TTL_S = 7 * 24 * 3600


def _rate_limit_facts(response: Any) -> tuple[int | None, int | None, float | None]:
    """从一次 429 响应里读出（并发上限, 每分钟请求数上限, 建议冷却秒数）。

    读不出来的项就是 None（宁可不管，也不猜）。
    """
    headers = getattr(response, "headers", None) or {}
    body = getattr(response, "text", "") or ""

    def _number(pattern: re.Pattern[str]) -> int | None:
        match = pattern.search(body)
        if not match:
            return None
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        return value if value >= 1 else None

    cooldown: float | None = None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is not None:
        try:
            cooldown = float(str(raw).strip())
        except ValueError:
            cooldown = None
    if cooldown is None:
        match = _RETRY_AFTER_BODY_PATTERN.search(body)
        if match:
            cooldown = float(match.group(1))
    if cooldown is not None:
        cooldown = min(max(0.0, cooldown), RATE_LIMIT_COOLDOWN_CAP_S)
    return _number(_CONCURRENCY_PATTERN), _number(_RPM_PATTERN), cooldown


# 有些模型把 temperature 钉死在一个值上（实测 Kimi k2.6：
# `invalid temperature: only 0.6 is allowed for this model`）。
# 这属于"服务端说了算"的一类事实 —— 和 response_format 被拒、限流并发数一样：
# 与其让用户去翻文档改配置，不如读服务端的原话、记住它、当场改用该值重发一次。
_TEMPERATURE_ERROR_PATTERN = re.compile(r"only\s+([0-9]+(?:\.[0-9]+)?)\s+is\s+allowed", re.IGNORECASE)


def fixed_temperature_from_error(body: str) -> float | None:
    """从 400 原文里读出"这个模型只接受哪个 temperature"；读不出来就 None。"""
    text = body or ""
    if "temperature" not in text.lower():
        return None
    match = _TEMPERATURE_ERROR_PATTERN.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class _EndpointGate:
    """按端点共享的"同时在飞请求数 + 每分钟请求数"闸门，外加 429 冷却窗口。

    它管的是**服务端限制**，不是用户配的 workers：
    用户设 8 个工人也不会撞上"组织并发 1 / 每分钟 3 次"的墙 —— 多出来的在闸门外排队。
    """

    def __init__(
        self,
        limit: int | None = None,
        rpm: int | None = None,
        window: float = RATE_WINDOW_S,
    ) -> None:
        self._cv = threading.Condition()
        self._limit = limit
        self._rpm = rpm
        self._window = window
        self._in_flight = 0
        self._starts: list[float] = []
        self._cooldown_until = 0.0
        # 重入深度：一次请求里可能再发一枪（response_format 降级 / temperature 自愈），
        # 那一枪**不能**再去抢一个并发额度 —— 否则 limit=1 时自己把自己锁死（实测踩到）。
        self._tls = threading.local()

    @property
    def limit(self) -> int | None:
        return self._limit

    @property
    def rpm(self) -> int | None:
        return self._rpm

    def tighten(self, limit: int | None = None, rpm: int | None = None) -> bool:
        """把上限收紧（只收不放）；返回是否有变化。"""
        changed = False
        with self._cv:
            if limit is not None and limit >= 1 and (self._limit is None or limit < self._limit):
                self._limit = limit
                changed = True
            if rpm is not None and rpm >= 1 and (self._rpm is None or rpm < self._rpm):
                self._rpm = rpm
                changed = True
            if changed:
                self._cv.notify_all()
        return changed

    def reset_limit(self, limit: int | None) -> None:
        """把并发上限直接设为某值（可升可降）——**只给「一次运行开始」时用**。

        为什么需要它：自适应降档（4→2→1）是「本轮」的结论，不应该带到下一轮
        （用户在一个程序会话里会连着跑好几轮）。每轮开始时用「配置/记忆的基准值
        与本次 worker 数取小」重置一次，自适应就只在轮内生效。
        """
        with self._cv:
            value = int(limit) if limit is not None and int(limit) >= 1 else None
            if value != self._limit:
                self._limit = value
            self._cv.notify_all()

    def cooldown(self, seconds: float) -> None:
        """让**所有**工人都等一段时间（429 之后的全局冷静期）。"""
        if seconds <= 0:
            return
        with self._cv:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)
            self._cv.notify_all()

    def _rpm_wait(self, now: float) -> float:
        """还要等多少秒才能让"本窗口内的请求数"不超上限（调用时已持锁）。"""
        if self._rpm is None:
            return 0.0
        self._starts = [t for t in self._starts if now - t < self._window]
        if len(self._starts) < self._rpm:
            return 0.0
        # 等到最早那次请求滑出窗口
        return max(0.0, self._window - (now - min(self._starts)))

    def acquire(self, timeout: float) -> float:
        """等到"有并发余量 + 不在冷却中 + 没超每分钟请求数上限"。返回等待秒数。

        同一线程重复调用（一次请求里的第二枪）**不再占并发额度**，
        但仍然按"又一次真实请求"计入每分钟计数 —— 它确实发出去了。
        超时就照发（宁可碰一次 429，也不能把整批卡死在这个闸门上）。
        """
        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        depth = getattr(self._tls, "depth", 0)
        with self._cv:
            while True:
                now = time.monotonic()
                has_room = depth > 0 or self._limit is None or self._in_flight < self._limit
                rpm_wait = self._rpm_wait(now)
                if has_room and rpm_wait <= 0.0 and now >= self._cooldown_until:
                    if depth == 0:
                        self._in_flight += 1
                    self._tls.depth = depth + 1
                    self._starts.append(now)
                    return now - started
                remaining = deadline - now
                if remaining <= 0:
                    if depth == 0:
                        self._in_flight += 1
                    self._tls.depth = depth + 1
                    self._starts.append(now)
                    return now - started
                waits = [remaining, max(rpm_wait, 0.05)]
                if now < self._cooldown_until:
                    waits.append(self._cooldown_until - now)
                else:
                    waits.append(0.25)
                self._cv.wait(max(0.05, min(waits)))

    def release(self) -> None:
        """还回额度（嵌套调用只在最外层真正归还）。"""
        depth = getattr(self._tls, "depth", 0)
        if depth <= 0:
            return
        self._tls.depth = depth - 1
        if depth > 1:
            return
        with self._cv:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cv.notify_all()


_GATES: dict[str, _EndpointGate] = {}
# ⚠ 必须是 **可重入锁**：`_gate_for()` 会先拿它、再在锁内调 `_baseline_limits()`，
#   而后者为了保证“读学到的限流值”与“用户在界面作废旧记忆”不打架，也要拿同一把锁。
#   用普通 Lock 会当场自锁（实测：自检卡在 test_model_echo_check，90 秒无输出）。
_GATES_LOCK = threading.RLock()
_LEARNED_LIMITS: dict[str, dict[str, int]] | None = None


def _baseline_limits(spec: Any) -> tuple[int | None, int | None]:
    """该端点的「基准」并发/RPM = 配置值 与 记忆值 取更严（都无则不限）。

    注意：这是**基准**，不是实时值 —— 运行中的自适应降档不会写进这里，
    所以下一轮开始时能回到基准（见 _EndpointGate.reset_limit）。
    """
    global _LEARNED_LIMITS
    # 与 forget_endpoint_limit 共用一把锁：那个函数把缓存置 None 是"用户刚在界面
    # 改了档位"的时刻，若这里正好在无锁地读，就会把刚刚作废的旧值又装回闸门
    # （表现为"点了 Tier1 仍旧只跑 1 个并发"）。
    # ⚠ 本函数会在 `_gate_for()` 持有 _GATES_LOCK 时被调用 —— 那把锁是可重入的。
    with _GATES_LOCK:
        if _LEARNED_LIMITS is None:
            _LEARNED_LIMITS = load_endpoint_limits()
        learned = dict(_LEARNED_LIMITS)
    facts = learned.get(spec.base_url) or {}

    def _tightest(*values: int | None) -> int | None:
        real = [int(v) for v in values if v is not None]
        return min(real) if real else None

    return (
        _tightest(getattr(spec, "max_concurrency", None), facts.get("max_concurrency")),
        _tightest(getattr(spec, "max_rpm", None), facts.get("max_rpm")),
    )


def _gate_for(spec: ModelSpec) -> _EndpointGate:
    """取该端点的闸门（首次访问时装入“配置值”与“上次学到的值”中**更严**的那个）。

    两个来源都有用：
      · 配置（config/models.yaml 的 max_concurrency / max_rpm）是**事先声明**的；
      · 记忆（endpoint_capabilities.json）是上次从 429 原文里学到的。
    两道只取更严的一项，永远**不会放宽**；想重新探测就删那个文件。
    """
    base_url = spec.base_url
    with _GATES_LOCK:
        gate = _GATES.get(base_url)
        if gate is None:
            gate = _EndpointGate(*_baseline_limits(spec))
            _GATES[base_url] = gate
            if gate.limit is not None or gate.rpm is not None:
                log.info(
                    "端点限量生效：%s 最多 %s 个请求同时在飞、每分钟最多 %s 次"
                    "（配置 max_concurrency/max_rpm 与上次学到的值取更严的一个；"
                    "想清掉记忆：删 %s）。",
                    base_url,
                    gate.limit if gate.limit is not None else "不限",
                    gate.rpm if gate.rpm is not None else "不限",
                    endpoint_caps_path(),
                )
        return gate


@dataclass
class RequestBudget:
    """请求预算计数器（硬约束 #18）。

    limit = 文件数 × 2（含重试）。每次真正发出 HTTP 请求前调用 spend()。
    重试也计入——这正是"含重试"的含义：如果重试不计入，
    那么预算上限就形同虚设（一个坏文件可以无限重试）。
    """

    limit: int
    used: int = 0
    # 预算来源说明（写进超限错误里，帮助定位是哪条通路给的预算）。
    # 以前错误文案里硬编码了「默认 = 文件数 × 2 含重试」：训练通路只发一次归纳请求，
    # 看到这句话会被带偏 —— 实测“一次读超时”就把预算用光，而报错却指向“密钥失效/模型名写错”。
    hint: str = ""
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # 使用线程锁，因为并发 worker 会同时调用 spend()。
        import threading

        self._lock = threading.Lock()

    def spend(self, count: int = 1) -> None:
        """占用预算；超限则抛 BudgetExceededError。"""
        with self._lock:
            if self.used + count > self.limit:
                detail = f"（上限 {self.limit} 次；{self.hint}）" if self.hint else f"（上限 {self.limit} 次）"
                raise BudgetExceededError(
                    f"本批请求数已达上限{detail}。\n"
                    f"已用 {self.used} 次。为避免失控耗费，任务已停止。\n"
                    "常见原因：API 密钥失效、模型名写错、网络不通、或服务端持续超时，"
                    "导致每次尝试都失败。\n"
                    "排查建议：先看日志里最近的错误原文，修正后重跑（输出模式可用「只跑失败」）。"
                )
            self.used += count

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def describe(self) -> str:
        return f"{self.used}/{self.limit}"


def encode_image_data_url(jpeg_bytes: bytes, limit_bytes: int) -> str:
    """把 JPEG 字节编码为 data URL。

    若超过 limit_bytes，会逐级降低质量重新编码。
    理由：单图过大时服务端会直接拒收（各家上限多为 5–10MB 请求体），
    而重试同一个超大图永远不会成功，属于"必然失败的重试"。
    主动降质至少能让任务跑通，且在 1024 长边下降低质量对参数判断影响很小。
    """
    data = jpeg_bytes
    if len(data) > limit_bytes:
        from PIL import Image

        log.warning(
            "缩略图 %d 字节超过单图上限 %d 字节，正在降质重编码。",
            len(data),
            limit_bytes,
        )
        with Image.open(io.BytesIO(jpeg_bytes)) as img:
            img = img.convert("RGB")
            icc = img.info.get("icc_profile")
            for quality in (75, 65, 55, 45):
                buffer = io.BytesIO()
                save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": False}
                if icc:
                    save_kwargs["icc_profile"] = icc
                img.save(buffer, **save_kwargs)
                data = buffer.getvalue()
                if len(data) <= limit_bytes:
                    log.info("降质到 quality=%d 后为 %d 字节。", quality, len(data))
                    break
            else:
                log.error("即使降到 quality=45 仍超过上限，将按现状发送（可能被服务端拒收）。")

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def extract_json_object(text: str) -> dict:
    """从模型返回的文本中提取 JSON 对象。

    需要处理的现实情况（各家模型与中转站的差异）：
      1. 直接就是 JSON；
      2. 被 ```json ... ``` 代码围栏包着；
      3. 前面有一句"好的，以下是结果："之类的话；
      4. 结尾多一个逗号或注释（极少，但见过）。

    策略：先剥围栏，再找第一个 '{' 到最后一个 '}' 的区间做 json.loads。
    比花哨的正则更可靠——因为模型输出里的字符串内容也可能含花括号。
    """
    if not text:
        raise SchemaValidationError("模型返回内容为空")

    cleaned = text.strip()

    # 剥代码围栏
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        raise SchemaValidationError(f"模型返回的顶层不是 JSON 对象（是 {type(parsed).__name__}）")
    except json.JSONDecodeError:
        pass

    # 回退：取第一个 { 到最后一个 } 之间的内容
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise SchemaValidationError(
            f"模型返回的内容中找不到 JSON 对象。原始内容前 300 字：{cleaned[:300]}"
        )
    fragment = cleaned[start : end + 1]
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(
            f"模型返回的 JSON 无法解析：{exc}。内容前 300 字：{fragment[:300]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise SchemaValidationError("模型返回的 JSON 顶层不是对象")
    return parsed


# ============================================================================
# 请求构造：**按 api_style 分路**，绝不按 model 名字拼字符串
# ============================================================================
#
# 为什么必须分路：把「模型」从 A 切到 B，在 OpenAI 兼容端点上只是改了请求体里
# 的一个字符串；但换一家**协议不同**的服务商时，URL、body 结构、必填字段都可能
# 不一样（Anthropic 原生要 POST /v1/messages 且 max_tokens 必填；
# Gemini 原生把 model 放在 URL 里而不是 body 里）。
# 所以"切模型只改一个字符串"这句话，**只对 IMPLEMENTED_API_STYLES 里列出的形状成立**。
# 这里是那条边界的落地处：不在表里的形状走不到这里（加载配置时就被拒了），
# 万一走到（内存里手拼的 spec），这里也立刻抛错，而不是发出一个形状错的请求。


@dataclass(frozen=True)
class PreparedRequest:
    """一次请求的完整形状：URL + 请求头 + 请求体。"""

    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


def auth_headers(spec: Any, api_key: str) -> dict[str, str]:
    """请求头（唯一来源：密钥只走 Authorization，绝不放进 URL）。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # 部分网关需要 Accept 才能返回 JSON；加上无害。
        "Accept": "application/json",
    }
    # 注意：extra_headers 来自用户配置，**不包含密钥**
    # （避免被日志/中间设备记录）。
    headers.update(getattr(spec, "extra_headers", None) or {})
    return headers


# 程序自己管理、不允许被 extra_body 覆盖的请求体字段。
# 它们构成"这条请求是什么"的契约：覆盖它们等于让配置去改程序的行为，
# 出错时完全无法归因（而这正是 extra_body 存在的意义：只放"家的旋钮"）。
_PROTECTED_PAYLOAD_KEYS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "response_format",
        # 下面两个也归程序管：它们由能力字段（max_tokens_mode / sends_temperature）
        # 决定，写进 extra_body 就等于绕过能力判断 —— 而能力判断正是为了
        # 避开"推理模型不认 max_tokens / 不支持 temperature"这类必然的 400。
        "max_tokens",
        "max_completion_tokens",
        "temperature",
    }
)


def _build_openai_style(
    spec: Any,
    api_key: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    response_format: dict[str, Any] | None,
    max_tokens_override: int | None = None,
) -> PreparedRequest:
    """OpenAI 兼容形状：POST {base_url}{endpoint_path}，model 在 body 里。

    两个"各家不一样"的地方走能力字段，而不是写死：
      · 输出上限的参数名（max_tokens / max_completion_tokens / 不发）——
        OpenAI 推理模型只认 max_completion_tokens；阿里云百炼在结构化输出下
        明确要求不要设 max_tokens；
      · temperature 发不发 —— OpenAI 推理模型把它列为 Unsupported parameters。
    """
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": messages,
    }

    # 输出上限：默认用配置里的上限；重试时可由调用方临时抬高
    # （见 adapter 的空 content / 截断处理）。
    limit = max_tokens_override or spec.max_output_tokens
    mode = getattr(spec, "max_tokens_mode", "max_tokens")
    if mode == "max_completion_tokens":
        payload["max_completion_tokens"] = limit
    elif mode == "max_tokens":
        payload["max_tokens"] = limit
    else:
        # "omit"：一个都不发，交给服务端用自己的默认上限。
        log.debug(
            "%s：按配置不发送输出上限字段（max_tokens_mode=omit）；"
            "服务端将使用它的默认值 %s。",
            spec.display,
            limit,
        )

    if getattr(spec, "sends_temperature", True):
        payload["temperature"] = temperature
    if response_format is not None:
        payload["response_format"] = response_format

    # extra_body：模型特有的旋钮（如 DeepSeek 的 thinking / reasoning_effort）原样合入。
    extra_body = getattr(spec, "extra_body", None) or {}
    if extra_body:
        spoiled = sorted(_PROTECTED_PAYLOAD_KEYS & set(extra_body))
        if spoiled:
            raise SchemaValidationError(
                f"extra_body 里不能写这些键：{spoiled}。\n"
                "它们由程序管理（model / messages / stream / response_format）："
                "写错了不是在「微调」，而是在改程序的行为，出错时无法归因。\n"
                "想调输出格式请用 supports_json_schema / supports_json_object，"
                "想改模型请改「模型」下拉。"
            )
        payload.update(extra_body)
    return PreparedRequest(url=spec.full_url, headers=auth_headers(spec, api_key), payload=payload)


def build_chat_request(
    spec: Any,
    api_key: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    response_format: dict[str, Any] | None = None,
    max_tokens_override: int | None = None,
) -> PreparedRequest:
    """按 `spec.api_style` 分路构造请求。未知形状立即失败（不猜、不降级）。"""
    style = str(getattr(spec, "api_style", "") or "openai").strip().lower()
    if style == "openai":
        return _build_openai_style(
            spec,
            api_key,
            messages,
            temperature=temperature,
            response_format=response_format,
            max_tokens_override=max_tokens_override,
        )
    raise SchemaValidationError(
        f"未实现的请求形状 api_style={style}（模型条目 {getattr(spec, 'model', '?')}）。\n"
        "请检查 config/models.yaml 或 config/connections.yaml 里的 api_style：\n"
        "  目前只实现了 openai（OpenAI 兼容端点）。\n"
        "Anthropic 原生 /v1/messages 与 Gemini 原生 :generateContent 需要新增"
        "请求构造分支后才能使用 —— 换 model 名字是换不过去的。"
    )



class ApiClient:
    """Chat Completions 客户端。

    每个线程使用独立的 Session（requests.Session 不是线程安全的），
    因此这里用 threading.local 保存 Session。
    """

    def __init__(self, spec: ModelSpec, api_key: str, budget: RequestBudget) -> None:
        self.spec = spec
        self.budget = budget
        self._api_key = api_key
        # 线程本地 Session 与用量锁**必须在构造时就建好**。
        # 曾经它们是在首次 _session() 调用里 lazy 创建的 —— 那是数据竞争：
        # 两个 worker 同时进来会各自看到 None、各自 new 一把锁，
        # 于是 token 累加失去互斥（计数会少算）。锁这种东西没有"懒加载"的余地。
        self._local = threading.local()
        self._usage_lock = threading.Lock()
        # 记录本批累计 token 用量，便于用户了解花费。
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        # 服务端在响应里**回显**的 model 名（按出现顺序去重）。
        # 存在的理由："切模型"在协议上只是改了请求体里的一个字符串，
        # 而"服务端真的用了哪个模型"只有响应里的 model 字段能证明。
        self.echo_models: list[str] = []
        # 是否出现过"回显 ≠ 请求"。一旦出现，本次运行的产物就不可信
        # （用户以为自己用的是 V4 Pro，实际可能是被降级/改路由的别的模型）。
        self.model_mismatch = False
        self._echo_warned = False
        self._echo_missing_logged = False
        # 该端点是否拒绝过 response_format（详见 chat() 里的一次性降级）。
        self.response_format_rejected = False
        self._downgrade_logged = False
        # "这个模型没有输出上限"这件事只提醒一次（omit 模式，见 _warn_if_no_output_limit）。
        self._omit_limit_warned = False
        # 端点能力记忆（上次运行时探测到的结果）与“已提示过”标记。
        self._rejected_formats = load_rejected_formats()
        self._cache_notice_logged = False
        # 有些模型把 temperature 钉死（如 Kimi k2.6 只接受 0.6）。
        # 记忆里的值优先于配置 —— 它是服务端自己说的，而配置是我们猜的。
        self._fixed_temperature = load_fixed_temperature(spec.base_url, spec.model)
        # 「用户是否已点停止」的检查器（由 pipeline 注入，见 set_cancel_check）。
        # 【为什么要一路传到这里 —— 用户真实反馈】他点暂停后，失败的几张**还在继续重试**
        # （他用 GLM 时发现的；GLM 是思考模型，单发就慢，重试叠上拆单重发会很明显）。
        # 根因：停止旗标以前只在提交循环里看，已经发出去的那一组会带着自己的
        # 重试/超时翻倍/降级重发全部跑完 —— 那就是“点了停止还在发请求”。
        self.cancel_check: Callable[[], bool] | None = None
        # 本次运行的并发意图（= 用户设的并发工人数，由 pipeline 注入）。
        # 闸门据此得到一个**可自动降档**的并发上限（见 set_concurrency_hint）。
        self._concurrency_hint: int | None = None

    def set_concurrency_hint(self, workers: int | None) -> None:
        """把「本次运行打算开多少并发」告诉端点闸门（使它成为可降档的上限）。

        为什么要经过闸门：worker 数只能限制线程池，**被服务端限流/拖慢之后收紧不了**；
        闸门是唯一能同时限制所有在飞请求的地方。设定后：
          · 上限 = min(服务端声明/记忆的并发, 用户设的 worker 数)；
          · 收到 429 限流或读超时，程序自己把上限减半（下限 1，见 _self_restrict）。
        依据（用户 2026-09-24 实测）：同一批图并发 4 时零星失败、并发 2/1 时消失 ——
        与其让他手动试，不如让程序在碰到压力时自己降一档。
        """
        if workers is None or int(workers) < 1:
            return
        self._concurrency_hint = int(workers)
        gate = _gate_for(self.spec)
        # 每轮开始时重置一次：基准（配置/记忆）与本次 worker 数取小。
        # 不重置的话，同一个程序会话里上一轮的自适应降档会一直留着 ——
        # 而我们的承诺是“降档只影响本次运行”。
        baseline, _rpm = _baseline_limits(self.spec)
        target = int(workers) if baseline is None else min(baseline, int(workers))
        gate.reset_limit(target)
        log.info(
            "本轮端点并发上限 = %d（用户设 %d；服务端声明/记忆的基准=%s；"
            "遇到限流或超时会自动减半）。",
            target,
            int(workers),
            baseline if baseline is not None else "无",
        )

    def _self_restrict(self, reason: str) -> None:
        """服务端压力偏大时，**本次运行**把该端点并发上限再降一档（不写记忆）。

        只降不升、下限 1；不写 endpoint_capabilities.json ——
        “这次慢了”不等于“这个端点永远只能跑 1 个”，下次启动仍按配置来。
        """
        gate = _gate_for(self.spec)
        current = gate.limit if gate.limit is not None else self._concurrency_hint
        if current is None or current <= 1:
            return
        target = max(1, current // 2)
        if gate.tighten(limit=target):
            log.warning(
                "服务端压力偏大（%s）：本次运行把该端点并发上限从 %d 降到 %d"
                "（仅为本次运行，不写记忆；下次启动仍按配置）。",
                reason,
                current,
                target,
            )

    def set_cancel_check(self, check: Callable[[], bool] | None) -> None:
        """注入「是否已请求停止」的检查器（None = 不检查）。"""
        self.cancel_check = check

    def _raise_if_cancelled(self, where: str) -> None:
        """已请求停止就别再发请求了：抛 StopRequestedError 立刻脱出重试循环。

        检查器本身出错不该弄死主流程（它只是个控制信号），所以只当“没停止”。
        """
        if self.cancel_check is None:
            return
        try:
            cancelled = bool(self.cancel_check())
        except Exception:  # noqa: BLE001
            return
        if cancelled:
            log.info("已请求停止（%s）：不再发送新的 API 请求。", where)
            raise StopRequestedError(f"已请求停止（{where}）：不再发送新的 API 请求。")

    def _sleep_cancellable(self, delay: float) -> None:
        """可被「停止」打断的退避等待（否则停止后还要白等一个退避周期）。"""
        deadline = time.monotonic() + max(0.0, float(delay))
        while True:
            self._raise_if_cancelled("退避等待中")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))
    # --- Session 管理 -------------------------------------------------------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            # Session 本身不带业务头：每次请求的形状（含 Authorization）都由
            # build_chat_request 决定并显式传入，不同 api_style 才不会互相污染。
            session = requests.Session()
            self._local.session = session
        return session

    def _headers(self) -> dict[str, str]:
        """请求头（转发到 auth_headers，保证只有一处构造逻辑）。"""
        return auth_headers(self.spec, self._api_key)

    def _warn_if_no_output_limit(self) -> None:
        """omit 模式下"这次请求没有输出上限"必须**显式说出来**。

        它本身是正确的请求形状（有些家不许设上限，例如阿里云百炼在结构化输出时），
        但代价是单次输出的 token 数完全由服务端决定 —— 用户看不到任何异常，
        只会在账单上看到。所以每次会话只提醒一次（不逐请求刷屏），
        把"可能很贵"这件事放在日志里，而不是让它成为事后才发现的事故。

        依赖的字段：`max_tokens_mode`（config/models.yaml）。
        """
        if self._omit_limit_warned:
            return
        if getattr(self.spec, "max_tokens_mode", "max_tokens") != "omit":
            return
        self._omit_limit_warned = True
        log.warning(
            "%s：按配置不发送输出上限字段（max_tokens_mode=omit），"
            "单次调用输出的 token 数完全由服务端决定 —— 大批量运行请注意账单。"
            "（想设上限就把该条目的 max_tokens_mode 改成 max_tokens / max_completion_tokens）",
            self.spec.display,
        )

    def close(self) -> None:
        """释放**当前线程**的 HTTP 连接池。

        为什么要显式提供：requests.Session 存在 `threading.local` 里，
        所以一个 client 在不同线程里是各自一把 Session。流水线的分析线程
        在 `ThreadPoolExecutor` 退出时结束，线程一死它那把 Session 就失去引用、
        连带连接被回收 —— 所以流水线**不需要**调它（也调不到：那是别的线程的）。
        它给的是"长期持有同一个 client"的调用方：CLI 主线程、探针脚本、
        以及将来可能出现的常驻服务，跑完一批后主动收尾。
        """
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception as exc:   # noqa: BLE001 - 关连接失败不该影响收尾
                log.debug("关闭 HTTP 连接池失败（忽略）：%s", exc)

    # --- 请求 ---------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        max_tokens_override: int | None = None,
    ) -> dict:
        """发起一次 Chat Completions 请求，返回响应 JSON。

        重试规则见模块头部注释。所有重试都会计入预算（硬约束 #18）。

        另外带一条**一次性自愈**：若服务端用 400 回绝了 response_format
        （真实例子：DeepSeek 不支持 json_schema 时会回
        `This response_format type is unavailable now`），
        则去掉这个字段重发一次，并记下 `response_format_rejected`。
        理由：配置里的能力声明是我们写的，但**真正说话的是服务端**；
        为了一个可以自动绕开的字段把整批任务弄挂，代价太大。
        """
        self._warn_if_no_output_limit()

        # 该端点已经拒过一次（本次进程）→ 后面所有请求都不再带它，不重复碰壁。
        if self.response_format_rejected:
            response_format = None
        # 该端点以前（包括上次启动）拒过**这种**类型 → 直接不发。
        elif response_format is not None:
            fmt = str(response_format.get("type") or "")
            if fmt and fmt in (self._rejected_formats.get(self.spec.base_url) or []):
                if not self._cache_notice_logged:
                    self._cache_notice_logged = True
                    log.warning(
                        "按上次的记录，%s 不支持 response_format.%s，本次不再发送该字段"
                        "（改用提示词 + 本地强校验）。想重新试探：删掉 %s。",
                        self.spec.base_url,
                        fmt,
                        endpoint_caps_path(),
                    )
                response_format = None

        prepared = build_chat_request(
            self.spec,
            self._api_key,
            messages,
            temperature=(
                self._fixed_temperature if self._fixed_temperature is not None else temperature
            ),
            response_format=response_format,
            max_tokens_override=max_tokens_override,
        )
        url = prepared.url
        last_error: Exception | None = None
        # 只允许**一次**降级重发，否则可能把"真错误"当成"格式问题"反复重试。
        downgraded = False
        # 同理，"temperature 被服务端钉死"也只自愈一次。
        temperature_fixed = False
        effective_temperature = (
            self._fixed_temperature if self._fixed_temperature is not None else temperature
        )
        # 读超时（可按重试放大，见 TIMEOUT_ESCALATION_* 的注释）。
        # ⚠ 这里不能写 `spec.request_timeout_s or REQUEST_TIMEOUT_S`：
        #   显式写 0 的配置会被 `or` 静默换成兜底的 5 秒，
        #   用户改完配置发现"没生效"却查不出原因（0 是合法取值，必须单独判）。
        configured_timeout = self.spec.request_timeout_s
        read_timeout = float(
            REQUEST_TIMEOUT_S if configured_timeout is None else configured_timeout
        )
        # 端点级闸门：服务端限制的"同时在线 + 每分钟请求数"（配置预设 + 限流时自动收紧）。
        gate = _gate_for(self.spec)

        for attempt in range(MAX_PARSE_RETRIES + 1):
            # 用户点停止后，连重试也不发（硬约束 #8 的“跳过剩余请求”包括重试）。
            self._raise_if_cancelled("发送前")
            # 预算检查放在真正发请求之前，保证"发出的请求数"从不超过上限。
            self.budget.spend(1)

            try:
                log.debug(
                    "请求 %s（第 %d 次尝试，模型 %s，消息 %d 条，读超时 %gs）",
                    url,
                    attempt + 1,
                    self.spec.model,
                    len(messages),
                    read_timeout,
                )
                waited = gate.acquire(timeout=max(30.0, read_timeout))
                if waited >= 1.0:
                    log.debug("在端点闸门处等了 %.1f 秒才发出（服务端限并发/冷却中）。", waited)
                response = self._session().post(
                    prepared.url,
                    json=prepared.payload,
                    headers=prepared.headers,
                    timeout=(CONNECT_TIMEOUT_S, read_timeout),
                )
            except requests.Timeout as exc:
                last_error = exc
                log.warning("请求超时（第 %d 次，读超时 %gs）：%s", attempt + 1, read_timeout, exc)
                # 超时往往意味着“我们的并发把服务端/网关拖慢了”：本次运行降一档并重试。
                # （实测：GLM 这类思考模型在并发 4 下超时频发，降到 2/1 后基本消失。）
                self._self_restrict("读超时")
                # 思考模型/大图会明显更慢：不给更长的预算，重试只是再等一次相同的超时。
                escalated = min(read_timeout * TIMEOUT_ESCALATION_FACTOR, TIMEOUT_ESCALATION_CEILING)
                if escalated > read_timeout and attempt < MAX_PARSE_RETRIES:
                    log.info(
                        "超时后把读超时从 %gs 提到 %gs 再试（思考模型或并发较高时常见）。",
                        read_timeout,
                        escalated,
                    )
                    read_timeout = escalated
            except requests.RequestException as exc:
                last_error = exc
                log.warning("请求异常（第 %d 次）：%s", attempt + 1, exc)
            else:
                if response.status_code in RETRYABLE_STATUS:
                    # 429 有两种完全不同的东西，必须先分开 ——
                    # 只看状态码会把"账户额度不够"当成"调用太频繁"，于是白重试、白冷却，
                    # 还可能把额度问题学成"并发 1"（用户充了钱也照样被压着跑）。
                    # 依据见 _QUOTA_PATTERN 上方：阿里云百炼官方《错误码》把
                    # 429-Throttling.RateQuota 与 429-Throttling.AllocationQuota 分两条，
                    # 后者（配额/额度/账单）**重试无效**。
                    if response.status_code == 429 and _quota_exhausted(response.text):
                        log.error(
                            "服务端说的是**账户配额/额度**问题（不是调用频率）：%s",
                            response.text[:200],
                        )
                        raise QuotaExceededError(
                            f"API 返回 429（账户配额/额度类，不是单纯的并发上限）："
                            f"{response.text[:600]}\n"
                            "服务端原文写的是配额/计费类（阿里云百炼官方错误码 "
                            "429-Throttling.AllocationQuota / insufficient_quota："
                            "「每秒钟或每分钟消耗 Token 数（TPS/TPM）触发限流」"
                            "或免费额度/账单额度用尽）。程序**不再重试**"
                            "（退避 1 秒不会让配额恢复，只会白花一次请求）。请按顺序处理：\n"
                            "  1) 等 1–5 分钟后点「只跑失败」重跑 —— TPM 按分钟窗口算，窗口滑过就恢复；\n"
                            "  2) 若每次跑到几十张就复发：这是「每分钟 token 配额」被打满，"
                            "把「高级设置 → 打开高级设置 → 同时处理几张」降到 2 或 1"
                            "（每张带图请求要上万 token，4 并发很容易打满），或减少单次处理的图片数；\n"
                            "  3) 若连单独一张都失败：才是额度/账单问题 —— 去该服务商控制台确认"
                            "「免费额度 / 计费与套餐 / 预算上限」；有些账号开了「免费额度用完即停」，"
                            "需要在控制台关掉才能继续按量计费调用。"
                        )
                    last_error = SchemaValidationError(
                        f"服务端返回可重试状态码 {response.status_code}：{response.text[:300]}"
                    )
                    if response.status_code == 429:
                        # 限流：①读服务端写明的并发/RPM 上限（收紧闸门）②全端点冷静期。
                        limit, rpm, cooldown = _rate_limit_facts(response)
                        if gate.tighten(limit, rpm):
                            remember_endpoint_limit(
                                self.spec.base_url,
                                concurrency=limit,
                                rpm=rpm,
                                evidence=response.text[:300],
                            )
                            log.warning(
                                "该端点限制：同时在飞最多 %s 个、每分钟最多 %s 次"
                                "（服务端原文：%s）。本次运行起已自动限量，以后启动也按这个值；"
                                "想重新探测：删掉 %s。",
                                limit if limit is not None else "未写明",
                                rpm if rpm is not None else "未写明",
                                response.text[:160],
                                endpoint_caps_path(),
                            )
                        if cooldown:
                            gate.cooldown(cooldown)
                            log.info("按服务端建议全端点冷却 %.0f 秒（所有工人一起等）。", cooldown)
                        # 服务端**没有**写明并发上限时（多数厂商都是这样），
                        # 本次运行主动降一档：4 → 2 → 1。实测并发越高，越容易在
                        # 同一分钟里连续撞限流；降档能明显减少“跑完有一两张红”。
                        if limit is None:
                            self._self_restrict("429 限流")
                    log.warning(
                        "服务端返回 %d（第 %d 次），将退避重试。",
                        response.status_code,
                        attempt + 1,
                    )
                elif response.status_code >= 400:
                    # 一次性自愈：response_format 被服务端拒了 —— 去掉它重发一次。
                    #
                    # 【触发条件（刻意收得很窄，外部评审也认可这个口径）】
                    #   1. 本次请求**确实带了** response_format（没带就无从降级）；
                    #   2. 本次进程还没降级过（只降一次，避免把真错误反复当格式问题重试）；
                    #   3. 响应体里**指名道姓提到** response_format。
                    # 为什么不做成"逐字段剥离重试"：那会让一次失败变成 N 次请求，
                    # 在"请求数 = 文件数 × 2"的预算下会把后面的文件挤掉。
                    # 为什么不要求错误码是 invalid_request_error：真实例子里
                    # DeepSeek 回的是 `This response_format type is unavailable now`，
                    # 里面**没有** invalid_request_error 字样 —— 按它筛会漏掉真情况。
                    # 这也覆盖"文档说支持、服务端悄悄弃用"这一类：判据是服务端的回话，
                    # 不是我们的配置。
                    if (
                        not downgraded
                        and response_format is not None
                        and "response_format" in response.text
                    ):
                        downgraded = True
                        self.response_format_rejected = True
                        # 落盘：自愈不能白做一次就忘（见 ENDPOINT_CAPS_FILENAME 的说明）。
                        rejected_fmt = ""
                        if response_format is not None:
                            rejected_fmt = str(response_format.get("type") or "")
                            remember_rejected_format(
                                self.spec.base_url, rejected_fmt, response.text[:300]
                            )
                        if not self._downgrade_logged:
                            self._downgrade_logged = True
                            log.warning(
                                "该端点拒绝了 response_format（%s）。已自动改成"
                                "「不带输出格式约束、仅靠提示词 + 本地校验」重发一次；"
                                "本次运行后续请求也不会再带它。"
                                "建议在模型条目里关掉 supports_json_schema，"
                                "并确认是否需要打开 supports_json_object（json_object 档）。",
                                response.text[:200],
                            )
                        prepared = build_chat_request(
                            self.spec,
                            self._api_key,
                            messages,
                            temperature=effective_temperature,
                            response_format=None,
                            max_tokens_override=max_tokens_override,
                        )
                        response_format = None
                        # 不消耗解析重试名额：原地重发（但仍计入预算，已在上方 spend）。
                        try:
                            gate.acquire(timeout=max(30.0, read_timeout))
                            response = self._session().post(
                                prepared.url,
                                json=prepared.payload,
                                headers=prepared.headers,
                                timeout=(CONNECT_TIMEOUT_S, read_timeout),
                            )
                        except requests.RequestException as exc:
                            last_error = exc
                            log.warning("降级重发失败：%s", exc)
                            continue
                        finally:
                            gate.release()

                    # 一次性自愈：服务端把 temperature 钉死在一个值上
                    #（实测 Kimi k2.6：`invalid temperature: only 0.6 is allowed for this model`）。
                    # 和 response_format 被拒同一套思路：读服务端的原话、记住它、当场改用该值重发。
                    # 不同点：这个值会写进端点能力记忆，以后启动直接用 ——
                    # 训练模式（0.2）与输出模式（0.6）都靠它才能跑通。
                    if not temperature_fixed and response.status_code == 400:
                        value = fixed_temperature_from_error(response.text)
                        if value is not None and value != effective_temperature:
                            temperature_fixed = True
                            self._fixed_temperature = value
                            remember_fixed_temperature(
                                self.spec.base_url, self.spec.model, value, response.text[:300]
                            )
                            log.warning(
                                "该模型只接受 temperature=%s（服务端原文：%s）。"
                                "已改用该值重发一次，并记住它；以后启动也用它",
                                value,
                                response.text[:160],
                            )
                            effective_temperature = value
                            prepared = build_chat_request(
                                self.spec,
                                self._api_key,
                                messages,
                                temperature=value,
                                response_format=None if downgraded else response_format,
                                max_tokens_override=max_tokens_override,
                            )
                            try:
                                gate.acquire(timeout=max(30.0, read_timeout))
                                response = self._session().post(
                                    prepared.url,
                                    json=prepared.payload,
                                    headers=prepared.headers,
                                    timeout=(CONNECT_TIMEOUT_S, read_timeout),
                                )
                            except requests.RequestException as exc:
                                last_error = exc
                                log.warning("改用 temperature=%s 重发失败：%s", value, exc)
                                continue
                            finally:
                                gate.release()

                    # 不可重试的客户端错误：立即抛出并带上服务端原文，
                    # 因为这类错误的原文（如"model not found"）就是用户需要看到的修复线索。
                    if response.status_code >= 400:
                        raise SchemaValidationError(
                            f"API 返回 {response.status_code}（不可重试）：{response.text[:600]}\n"
                            "常见原因：密钥无效、模型名错误、余额不足、请求体超限。"
                            + self._capability_hint(response.text)
                        )

                # 到位说明状态码 < 400（可能是刚降级成功的那一次）。
                # ⚠ 注意：这里不能用 else 挂在上面那条 if/elif 上 ——
                #   降级重发成功后请求落在同一个 attempt 里，必须**当场解析返回**，
                #   否则循环会再发一次完全相同的请求（实测多花一次请求额度）。
                if response.status_code < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        last_error = SchemaValidationError(
                            f"响应不是合法 JSON：{response.text[:300]}"
                        )
                        log.warning("响应非 JSON（第 %d 次）：%s", attempt + 1, exc)
                    else:
                        self._record_response(data)
                        return data

            finally:
                # 无论成功/超时/异常，都要把闸门还回去（否则并发额度会漏掉，整批变慢）。
                gate.release()

            if attempt < MAX_PARSE_RETRIES:
                delay = self._backoff_delay(attempt)
                log.info("将在 %.2f 秒后重试（第 %d/%d 次）。", delay, attempt + 1, MAX_PARSE_RETRIES)
                # 退避也要能被停止打断：否则点了停止还会再等一个周期、然后（若不检查）又发一次。
                self._sleep_cancellable(delay)

        hint = ""
        if isinstance(last_error, requests.Timeout):
            hint = (
                "\n超时提示：该模型（思考模型尤甚）或当前并发下响应较慢。"
                "可在模型条目里把 request_timeout_s 调大（如 180），或把并发工人数调小。"
            )
        elif isinstance(last_error, SchemaValidationError) and "429" in str(last_error):
            parts = []
            if gate.limit is not None:
                parts.append(f"同时在飞 {gate.limit} 个")
            if gate.rpm is not None:
                parts.append(f"每分钟 {gate.rpm} 次")
            limit_text = "、".join(parts) if parts else "未知（看服务端原文）"
            hint = (
                f"\n限流提示：该端点限制为 {limit_text}，本次运行已自动限量；"
                "若仍频繁 429，请在「高级设置」里把并发工人数调到限制值或更低，"
                "或换更高档位的套餐。"
            )
        raise SchemaValidationError(
            f"请求在 {MAX_PARSE_RETRIES + 1} 次尝试后仍失败。最后一次错误：{last_error}" + hint
        )

    @staticmethod
    def _capability_hint(body_text: str) -> str:
        """把"能力声明写错了"这类 400 变成可操作的指引。

        为什么要做这件事：真实发生的场景 ——
        DeepSeek 官方页面写明了 `deepseek-v4-pro` **不支持图像理解**，
        而配置里只有服务商级的 supports_vision=true 时，程序会把图发过去，
        服务端回一个含糊的 400。用户看到"请求体超限/模型名错误"的通用提示，
        根本猜不到是\"这个模型不能看图\"。
        这里不做自动降级（把图偷偷换成 caption 会改变结果可信度，必须用户知情），
        只把原因和下一步说清楚。
        """
        text = (body_text or "").lower()
        image_markers = ("image", "vision", "multimodal", "image_url", "图像", "图片")
        if any(m in text for m in image_markers):
            return (
                "\n⚠ 服务端提了「图片」：很可能是这个模型**不接受图片输入**。\n"
                "请按以下任一种处理：\n"
                "  1) 换成支持视觉的模型（例如同为 DeepSeek 的 deepseek-flash）；\n"
                "  2) 在 config/models.yaml 的该服务商下加 model_overrides，"
                "把它的 supports_vision 设为 false —— 这样程序会走 caption 降级通道，"
                "并在界面上用红字告诉你精度会下降，而不是发一个必然失败的请求。"
            )
        return ""

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """计算退避时长（指数 + 抖动）。"""
        base = BACKOFF_BASE_S * (BACKOFF_FACTOR ** attempt)
        # 抖动范围 ±BACKOFF_JITTER（默认 ±20%）
        jitter = base * BACKOFF_JITTER * (random.random() * 2.0 - 1.0)
        return max(0.1, base + jitter)

    def _record_response(self, data: dict) -> None:
        """记账：token 用量 + **服务端回显的模型名**。

        为什么必须核对回显：把「模型」从 V4.1 Flash 切到 V4 Pro，在协议上
        只是改了请求体里的一个字符串。服务端到底用了哪个模型，**只有响应里的
        `model` 字段能证明**。中转站（one-api / new-api / SiliconFlow 这类）
        静默改路由、或把不认识的 model id 降级到自己的默认模型时，
        请求会**成功返回**、结果看起来也正常 —— 这种失败只能靠回显发现。
        不核对回显，界面上的"切换模型"就是盲的。
        """
        self._record_usage(data)
        if not isinstance(data, dict):
            return
        echo = data.get("model")
        if isinstance(echo, str) and echo:
            if echo not in self.echo_models:
                self.echo_models.append(echo)
                log.info("服务端回显模型：%s（本次请求的是 %s）", echo, self.spec.model)
            if echo != self.spec.model:
                self.model_mismatch = True
                if not self._echo_warned:
                    # 只警告一次：100 张图会重复 100 次，刷屏反而让人看不见。
                    self._echo_warned = True
                    log.warning(
                        "⚠ 模型不匹配：请求的是 %s，服务端回显的是 %s。"
                        "切换可能未生效（中转站改路由，或把该 id 降级到了默认模型）。"
                        "Token 用量按服务端实际使用的模型计费。",
                        self.spec.model,
                        echo,
                    )
        elif not self._echo_missing_logged:
            self._echo_missing_logged = True
            # 不算错误：部分自建网关就是不回显 model。只是告诉用户"这条证据没有"。
            log.info("服务端未回显 model 字段，无法核对实际使用的模型（常见于自建网关）。")

    def _record_usage(self, data: dict) -> None:
        """累计 token 用量（成本可见性）。"""
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        with self._usage_lock:
            self.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.total_completion_tokens += int(usage.get("completion_tokens") or 0)

    def usage_summary(self) -> str:
        """返回本批 token 用量摘要（含服务端回显的模型，作为"切换真的生效了"的证据）。"""
        base = (
            f"输入 {self.total_prompt_tokens} token，输出 {self.total_completion_tokens} token"
        )
        if not self.echo_models:
            return base
        actual = "、".join(self.echo_models)
        if self.model_mismatch:
            return f"{base}　⚠ 服务端实际使用：{actual}（请求 {self.spec.model}，切换可能未生效）"
        return f"{base}　服务端确认模型：{actual}"

    # --- 内容提取 -----------------------------------------------------------

    @staticmethod
    def extract_message_text(data: dict) -> str:
        """从响应中取出助手消息文本。

        兼容三种形态：
          1. choices[0].message.content 是字符串（标准）；
          2. content 是数组（部分网关把内容拆成多个 part）；
          3. choices[0].text（补全风格的老接口）。
        """
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise SchemaValidationError(f"响应中没有 choices 字段：{json.dumps(data)[:400]}")

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # 输出被截断，必然导致 JSON 不完整。宁可立刻报错也不要让
            # 上层拿到半截 JSON 去猜。
            # 单独分类（TruncatedOutputError）：上层的唯一有效动作是
            # **抬高出上限重试**，而不是重发同样的请求。
            raise TruncatedOutputError(
                f"模型输出因达到 max_output_tokens 被截断（finish_reason={finish_reason}）。"
            )

        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                return content
            # 空 content 单独报：DeepSeek 官方《JSON Output》指南的注意事项里
            # 明写「使用 JSON Output 功能时，API 有概率会返回空的 content」，
            # 并建议调整 prompt。把这句话放进错误里，用户才不至于
            # 把自己的配置或密钥反复折腾一遍。
            # 另外：思考模式的推理 token 也算 max_tokens，推理吃光额度后
            # 也会表现为空 content —— 所以上层会抬高出上限重试一次。
            raise EmptyContentError(
                "服务端返回了空的 content。"
                "官方文档（DeepSeek《JSON Output》注意事项）提到使用 JSON Output 时"
                "有概率如此；另一个常见原因是 max_tokens 不够"
                "（思考模式的推理 token 也算在其中）。"
                + (
                    "\n该响应里有 reasoning_content，说明思考模式开着。"
                    if message.get("reasoning_content") else ""
                )
            )
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                    texts.append(part.get("text") or "")
                elif isinstance(part, str):
                    texts.append(part)
            if texts:
                return "".join(texts)
        if isinstance(choice.get("text"), str):
            return choice["text"]

        raise SchemaValidationError(
            f"无法从响应中提取文本内容。响应摘要：{json.dumps(data, ensure_ascii=False)[:600]}"
        )
