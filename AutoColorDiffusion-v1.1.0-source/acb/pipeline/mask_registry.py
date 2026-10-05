# -*- coding: utf-8 -*-
"""蒙版登记表：记住**我们自己写进文件的那些蒙版**，好在下一轮精确替换它们。

【为什么必须有】两条都是用户定的红线，只有这份登记表能同时满足：
  1. **不许覆盖他手工画的蒙版**（2026-09-23 用户裁决）；
  2. 我们自己写进去的蒙版要能**被下一轮换掉** —— 否则修好的坐标永远进不去。
     真实场景（2026-09-26）：AI 给的坐标因为"缩略图没摆正"而错位，修好之后重跑，
     结果 `apply_masks` 看到文件里已经有蒙版组 → 整组跳过 → 用户看到的还是旧的错蒙版。

区分"他的"与"我们的"只有一个可靠依据：`crs:CorrectionSyncID` ——
我们写入时是随机生成的，写完之后把这一组 ID 记在这里；下一轮按 ID 精确删掉这几条，
其余（他没有 ID 记录的）原样保留。

键用断点键（`ScanItem.key` = 大小 + mtime + 文件名），与 `wb_baseline` 同一套约定：
文件被改动过就是另一条记录，不会张冠李戴。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..i18n import T
from ..logging_setup import get_logger
from ..paths import state_dir

log = get_logger("mask_registry")

REGISTRY_FILENAME = "mask_sync_ids.json"
MAX_ENTRIES = 5000

_LOCK = threading.Lock()


def _path() -> Path:
    return state_dir() / REGISTRY_FILENAME


def _load() -> dict[str, list[str]]:
    path = _path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 坏掉的最坏后果只是"这一轮把我们的蒙版当成别人的"→ 不写蒙版（宁可不改）。
        log.warning(T("蒙版登记表损坏，已忽略：%s（%s）"), path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): [str(v) for v in value]
        for key, value in data.items()
        if isinstance(value, list)
    }


def _save(data: dict[str, list[str]]) -> None:
    if len(data) > MAX_ENTRIES:
        for key in list(data)[: len(data) - MAX_ENTRIES]:
            data.pop(key, None)
    path = _path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning(T("写入蒙版登记表失败 %s：%s"), path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def ids_for(key: str) -> frozenset[str]:
    """取"本程序写进这份文件的蒙版 ID"（没有就返回空集）。"""
    with _LOCK:
        return frozenset(_load().get(key) or ())


def all_ids() -> frozenset[str]:
    """全库记录过的蒙版 ID（不分文件）。

    【为什么需要它 —— 2026-09-30 实测踩到的真 bug】
    断点键 = sha256(大小 | mtime_ns | 文件名)。而 TIFF / DNG / HEIF 是
    **写进文件内部**的：我们一写，文件的大小与时间就变了 → 键跟着变 →
    下一轮按键查登记表**查不到** → 我们自己的蒙版会被当成"别人的"整组跳过，
    永远不再更新（日志里还会显示"不覆盖你的手工蒙版"，看起来是我们守规矩，
    实际上是蒙版再也不生效）。

    所以认领要**两条路一起走**：这个文件的记录（快、准） +
    全库 ID（不受键漂移影响）。ID 是随机 32 位十六进制、只可能来自我们自己的
    写入，所以"蒙版条目的 ID 在库里"就等于"这条是我们写的"，
    用户手工画的蒙版不可能命中。
    """
    with _LOCK:
        data = _load()
    out: set[str] = set()
    for ids in data.values():
        out.update(ids)
    return frozenset(out)


def record(key: str, sync_ids: tuple[str, ...] | list[str]) -> None:
    """写盘成功后登记本次写入的蒙版 ID。

    只在**写盘成功**之后调用：写失败时文件没变，登记了反而会让下一轮
    去删一个并不存在的（或属于别人的）条目。
    """
    ids = [str(v) for v in sync_ids if v]
    if not ids:
        return
    with _LOCK:
        data = _load()
        # 直接覆盖：本次写出后，文件里属于我们的蒙版就是这一组
        #（上一轮的已在写入时被精确替换掉）。
        data[key] = ids
        _save(data)


def forget(key: str) -> None:
    """忘掉某份文件的记录（自检用）。"""
    with _LOCK:
        data = _load()
        if key in data:
            data.pop(key)
            _save(data)
