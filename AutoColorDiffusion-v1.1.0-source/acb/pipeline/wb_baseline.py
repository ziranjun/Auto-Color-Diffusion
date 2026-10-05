# -*- coding: utf-8 -*-
"""as-shot 白平衡基准登记表 —— 防止"我们自己上一次写下的值"变成下一次的基准。

【为什么必须有这个东西】2026-09-26 用户实测：
    `44430003.TIF` 与 `44430024.TIF` 这两张 TIFF **原本没有任何 XMP**
    （日志原话：`44430024.TIF 没有内嵌 XMP，将新建一份写入文件内部`）。
    于是第一轮我们写进去的 `Temperature=5700`、第二轮再读这个文件时，
    它已经变成"这份文件的 as-shot"了 —— 而模型看到 as-shot=5700 后又写 5950，
    第三轮再读就是 5950 …… **每跑一次就顺着风格方向多推一点**，
    用户看到的就是"这一轮的输出比上一版更红了"。

同一件事对 RAW 不会发生：RAW 的 as-shot 来自机内 maker note（有侧车也不会被覆盖，
实测 `_G4A1619.CR3` 侧车里写着 +5500，读回来仍是机内 5200）。所以这个登记表
主要保护 TIFF / HEIC 这类**把参数写进文件自身**的格式。

【判据：怎么区分"我们的产物"与"用户自己改过"】
    登记表里记下 (键 → 当时的 as-shot 基准 + 我们最后写下去的值)。
    下一轮读文件时：
      · 文件里的值**恰好等于我们上次写下的值** → 那是我们自己的产物
        → 仍然沿用**第一次**记录的基准（不漂移）；
      · 文件里的值**不等于**我们写的 → 用户（或别的工具）改过它
        → 以文件现在的值为准（用户的选择永远优先，见 CameraProfile 那套同样的规矩）。

键用断点键（`ScanItem.key` = 文件大小 + mtime + 文件名），所以用户在两次运行之间
编辑过文件也不会串味：键变了就是另一条记录。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ai import guardrails
from ..i18n import T
from ..logging_setup import get_logger
from ..paths import state_dir

log = get_logger("wb_baseline")

BASELINE_FILENAME = "wb_baselines.json"

# 登记表最大条目数（超出就丢掉最旧的）。100 万像素级的批处理也很少超过它，
# 加这个上限只是为了不让文件无限长大（它毕竟是运行时状态，不是用户配置）。
MAX_ENTRIES = 5000

# 判"文件里的值就是我们写的那个"的容差（ACR 会按整数字段回整，1K 足够）。
SAME_VALUE_TOLERANCE = 1.0

_LOCK = threading.Lock()
# 一次运行内的记忆：避免同一张图在"注入提示词"和"写盘前护栏"两处各读一次 exiftool。
_MEMO: dict[str, "WbBaseline"] = {}


@dataclass(frozen=True)
class WbBaseline:
    """一张图的 as-shot 基准（可能为空 = 这份文件根本没有可信的基准）。"""

    kelvin: float | None
    tint: float | None
    # 说明：给日志与提示词用（例如"沿用首次记录的基准"）。
    note: str = ""

    @property
    def known(self) -> bool:
        return self.kelvin is not None


def _path() -> Path:
    return state_dir() / BASELINE_FILENAME


def _load() -> dict[str, dict[str, Any]]:
    path = _path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 状态文件坏了不该让程序起不来，也不该拦住这一批的处理：
        # 坏掉的最坏后果只是"这一次的基准退回到读文件"，下一轮会重新记上。
        log.warning(T("白平衡基准登记表损坏，已忽略：%s（%s）"), path, exc)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    if len(data) > MAX_ENTRIES:
        # 保留最新的 MAX_ENTRIES 条（dict 保序：JSON 里的顺序就是写入顺序）
        for key in list(data)[: len(data) - MAX_ENTRIES]:
            data.pop(key, None)
    path = _path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        # 目录可能还不存在（自检在临时 APPDATA 下跑时就是这样；
        # 真实启动会由 ensure_runtime_dirs 建好，但这里不能依赖它）。
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning(T("写入白平衡基准登记表失败 %s：%s"), path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def effective_as_shot(
    key: str,
    raw_path: Path,
    exiftool: guardrails.TagReader,
) -> WbBaseline:
    """解析这张图**真正该用的** as-shot 基准（含"不漂移"的判定）。

    结果在一次运行内按文件键记忆（同一张图会被问两次：注入指标 / 写盘前复核）。
    """
    with _LOCK:
        memo = _MEMO.get(key)
    if memo is not None:
        return memo

    current_k, current_t = guardrails.read_as_shot_wb(raw_path, exiftool)
    data = _load()
    entry = data.get(key)
    note = ""

    if entry:
        written_k = entry.get("written_k")
        if (
            written_k is not None
            and current_k is not None
            and abs(float(current_k) - float(written_k)) <= SAME_VALUE_TOLERANCE
        ):
            # 文件里现在的色温就是我们上次写下去的那个 → 这是**我们的产物**
            recorded = entry.get("as_shot_k")
            if recorded is None:
                # 第一次处理时它就没有可信基准（例如本来没有 XMP 的 TIFF）：
                # 那就保持"没有基准"，白平衡整组不动 —— 绝不拿我们自己写的值当起点。
                result = WbBaseline(None, None, T("这份文件没有可信的 as-shot 基准（参数是本程序写的）"))
            else:
                result = WbBaseline(
                    float(recorded),
                    float(entry["as_shot_t"]) if entry.get("as_shot_t") is not None else None,
                    T("沿用首次记录的 as-shot（避免每次重跑都往同一方向多推一次）"),
                )
            log.info(
                T("%s：文件里的白平衡是本程序上次写的（%s），基准仍按 %s 计算。"),
                raw_path.name,
                f"{current_k:.0f}K",
                f"{result.kelvin:.0f}K" if result.known else T("无"),
            )
            _remember(key, result)
            return result
        if written_k is not None and current_k is not None:
            note = T("文件的白平衡已被其它工具/你本人改动，按当前值作为基准")
    elif current_k is None:
        note = T("读不到 as-shot（这份文件本身没有白平衡信息）")

    result = WbBaseline(current_k, current_t, note)
    _remember(key, result)
    return result


def _remember(key: str, baseline: WbBaseline) -> None:
    with _LOCK:
        _MEMO[key] = baseline


def record_written(
    key: str,
    baseline: WbBaseline,
    params: dict[str, Any],
) -> None:
    """成功写出 XMP 之后登记：这次的基准 + 我们写下去的值。

    只在**写盘成功**之后调用（写失败时文件没变，登记了反而会让下一轮误判）。
    """
    written_k = params.get("Temperature")
    written_t = params.get("Tint")
    if written_k is None and written_t is None and baseline.kelvin is None and baseline.tint is None:
        # 既没写白平衡、也没基准可记：不必留条目。
        return
    with _LOCK:
        data = _load()
        entry = data.get(key) or {}
        # 「第一次见到的基准」先到先得 —— 这就是“不漂移”的全部秘密。
        # 注意：若此前记的是 None（当年没有可信基准），而这次读到了真值
        #（说明用户后来自己在 ACR 里设过白平衡），那就改成新值。
        if entry.get("as_shot_k") is None:
            entry["as_shot_k"] = baseline.kelvin
        if entry.get("as_shot_t") is None:
            entry["as_shot_t"] = baseline.tint
        # 只在**真的写了值**时才记“我们写了什么”（否则会把上一轮的值抹掉，
        # 导致下一轮把自己的产物误当成用户改动）。
        if written_k is not None:
            entry["written_k"] = written_k
        if written_t is not None:
            entry["written_t"] = written_t
        data[key] = entry
        _save(data)


def reset_memo() -> None:
    """清掉进程内记忆（自检用：模拟"下一次启动"）。"""
    with _LOCK:
        _MEMO.clear()
