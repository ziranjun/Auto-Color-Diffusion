# -*- coding: utf-8 -*-
"""任务状态、断点续跑、失败记账（硬约束 #6 / #11 / #18）。

断点键的设计（含对硬约束 #11 的修正）
------------------------------------
硬约束 #11 原文："键 = 文件绝对路径 + 文件大小 + mtime 的哈希，
避免用户移动文件夹后续跑全部失效。"

这句话内部是**自相矛盾**的：只要键里含绝对路径，
用户把文件夹移动或改名后，所有键都会变化，续跑必然全部失效——
而这正是该条款想要避免的结果。

因此实现为（已在方案中向你确认）：
    键 = sha256(文件大小 | mtime_ns | 文件名)[:16]      ← 不含目录
    绝对路径作为记录字段保存，用于日志与审计，不参与哈希。
效果：
    - 移动/重命名文件夹后，键不变，续跑依然有效（达成原始意图）；
    - 同一目录下同名同大小同 mtime 的两个文件会被视为同一个（理论上可能，
      实际上 mtime_ns 精确到 100 纳秒，碰撞概率可忽略）；
    - 文件内容被修改（大小或 mtime 变化）→ 键变化 → 视为新文件重新处理，
      这是正确行为。

failed.json 与逃生舱
--------------------
硬约束 #18：failed.json 中同一文件累计失败 2 次即永久跳过，不再进入 --resume。
但"永久"必须有出口，否则用户修好外部原因（换密钥、装 exiftool）后无路可走。
因此额外提供：
    - CLI: --clear-failures
    - 界面:「清除失败记录」按钮
没有这个出口，failed.json 就成了只进不出的黑洞。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..cache import content_probe, file_stamp
from ..constants import MAX_ATTEMPTS_PER_FILE, is_supported_raw, is_dng
from ..logging_setup import get_logger
from ..paths import state_dir

log = get_logger("job")

RECORDS_FILENAME = "records.json"
FAILED_FILENAME = "failed.json"

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class ScanItem:
    """一个待处理的 RAW 文件。"""

    path: Path
    key: str
    size: int
    mtime_ns: int

    @property
    def is_dng(self) -> bool:
        return is_dng(self.path.name)


def iter_raw_files(sources: Iterable[Path], recursive: bool = True) -> list[Path]:
    """扫描白名单 RAW 文件。

    支持两种输入：
        - 目录：按 recursive 决定是否递归子目录（硬约束 #1 要求支持递归）
        - 具体文件：直接纳入（用户可能只选了单个文件）
    结果按路径排序，保证多次运行的顺序一致（便于复现与对比日志）。
    """
    found: list[Path] = []
    seen: set[str] = set()

    for source in sources:
        try:
            if source.is_file():
                if is_supported_raw(source.name):
                    resolved = str(source.resolve())
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append(source)
                else:
                    log.warning("跳过不支持的文件（不在白名单内）：%s", source.name)
                continue

            if source.is_dir():
                iterator = source.rglob("*") if recursive else source.glob("*")
                for candidate in iterator:
                    try:
                        if not candidate.is_file():
                            continue
                    except OSError:
                        continue
                    if not is_supported_raw(candidate.name):
                        continue
                    resolved = str(candidate.resolve())
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    found.append(candidate)
                continue

            log.warning("路径不存在或不可访问，已跳过：%s", source)
        except OSError as exc:
            log.warning("扫描 %s 时出错，已跳过：%s", source, exc)

    found.sort(key=lambda p: str(p).lower())
    return found


def build_scan_items(paths: Iterable[Path]) -> list[ScanItem]:
    """把文件路径转换为带断点键的扫描项。

    键碰撞的处理（这是自检工具抓到的一个真实 bug）
    ------------------------------------------------
    断点键 = sha256(大小 | mtime_ns | 文件名)，**不含目录**。
    这是为了满足"移动文件夹后键不变"（硬约束 #11 的修正）。

    但"不含目录"必然带来一个后果：两个同名、同大小、同 mtime 的文件
    （典型场景：用 robocopy /COPY:DAT 把同一张卡拷两份，时间戳被原样保留）
    会算出相同的键。

    最初的处理是"碰撞就碰撞"，结果是**只导出了其中一个文件**
    （因为导出计划是按键索引的字典，第二项覆盖了第一项）。
    这类 bug 非常隐蔽：日志显示任务全部成功，但少出一张图。

    现在的处理是分两步：
      1. 检测碰撞，对碰撞组内的文件读取**内容指纹**（头 64KB + 尾 64KB 的哈希）。
         只对碰撞组读，因此正常情况下零额外开销。
      2. 按内容指纹再分组：
         - 内容**不同** → 用内容指纹加后缀消歧。这个后缀与目录无关，
           因此依然满足"移动文件夹后键不变"。
         - 内容**完全相同** → 判定为真重复图片，**共用同一个键**，
           于是只请求一次 API（省一次费用），导出两份文件。
           这也是用户期望的行为：同一张图的两个副本应该被调成一样。
    """
    # 第一步：算出基础键
    stamps: list[tuple[Path, Any]] = []
    for path in paths:
        try:
            stamp = file_stamp(path)
        except OSError as exc:
            log.warning("无法读取文件属性，已跳过 %s：%s", path, exc)
            continue
        stamps.append((path, stamp))

    # 第二步：按基础键分组，找出碰撞
    grouped: dict[str, list[tuple[Path, Any]]] = {}
    for path, stamp in stamps:
        grouped.setdefault(stamp.key, []).append((path, stamp))

    items: list[ScanItem] = []

    for base_key, group in grouped.items():
        if len(group) == 1:
            path, stamp = group[0]
            items.append(
                ScanItem(path=path, key=base_key, size=stamp.size, mtime_ns=stamp.mtime_ns)
            )
            continue

        # --- 发生碰撞：计算每个文件的内容指纹 ---
        log.warning(
            "检测到 %d 个文件的基础键相同（同名、同大小、同修改时间）：%s",
            len(group),
            "、".join(p.name for p, _ in group),
        )
        probed: dict[str, list[tuple[Path, Any]]] = {}
        for path, stamp in group:
            probe = content_probe(path)
            probed.setdefault(probe, []).append((path, stamp))

        if len(probed) == 1:
            # 所有文件内容完全一致：真重复，共用键以复用分析结果。
            for path, stamp in group:
                log.info(
                    "  %s 与其它副本内容一致，将复用同一分析结果（节省一次 API 请求）",
                    path.name,
                )
                items.append(
                    ScanItem(path=path, key=base_key, size=stamp.size, mtime_ns=stamp.mtime_ns)
                )
            continue

        # 内容不同：用内容指纹消歧。该后缀与目录无关，因此移动文件夹后依然稳定。
        for probe, sub_group in probed.items():
            if len(sub_group) == 1:
                path, stamp = sub_group[0]
                new_key = f"{base_key}-{probe[:6]}"
            else:
                # 极罕见：同一碰撞组里既有"内容相同的重复"又有"内容不同的"。
                new_key = f"{base_key}-{probe[:6]}"
            for path, stamp in sub_group:
                log.info("  已用内容指纹消歧：%s → 键 %s", path.name, new_key)
                items.append(
                    ScanItem(path=path, key=new_key, size=stamp.size, mtime_ns=stamp.mtime_ns)
                )

    return items


@dataclass
class ItemRecord:
    """单个文件的处理记录（断点续跑的依据）。"""

    path: str
    size: int
    mtime_ns: int
    status: str = STATUS_PENDING
    attempts: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    curves: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    xmp_target: str = ""
    xmp_mode: str = ""
    warning_count: int = 0
    updated_at: float = 0.0
    error: str = ""


class JobState:
    """持久化的任务状态。

    两个文件：
        records.json —— 每个文件的处理结果（含已产出的参数，便于续跑时跳过 API）
        failed.json  —— 失败计数与永久跳过标记

    两者都按"断点键"索引，因此跨程序重启、跨批次运行都能正确续上。
    写入用"临时文件 + os.replace"原子替换，避免被强杀时状态文件损坏。
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = Path(directory) if directory else state_dir()
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._failed: dict[str, dict[str, Any]] = {}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("无法创建状态目录 %s：%s。本次运行将不保存断点。", self._dir, exc)
        self._load()

    # --- 路径 ---------------------------------------------------------------

    @property
    def records_path(self) -> Path:
        return self._dir / RECORDS_FILENAME

    @property
    def failed_path(self) -> Path:
        return self._dir / FAILED_FILENAME

    # --- 磁盘读写 -----------------------------------------------------------

    def _load(self) -> None:
        self._records = self._read_json(self.records_path)
        self._failed = self._read_json(self.failed_path)
        log.debug(
            "载入任务状态：%d 条记录，%d 条失败记录。",
            len(self._records),
            len(self._failed),
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("状态文件损坏，已忽略：%s（%s）", path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("写入状态文件失败 %s：%s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_records(self) -> None:
        self._write_json(self.records_path, self._records)

    def _save_failed(self) -> None:
        self._write_json(self.failed_path, self._failed)

    # --- 查询 ---------------------------------------------------------------

    def get_record(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._records.get(key)

    def is_done(self, key: str) -> bool:
        record = self.get_record(key)
        return bool(record and record.get("status") == STATUS_DONE)

    def failed_attempts(self, key: str) -> int:
        with self._lock:
            entry = self._failed.get(key) or {}
            return int(entry.get("attempts") or 0)

    def is_permanently_skipped(self, key: str) -> bool:
        """是否已永久跳过（硬约束 #18）。"""
        with self._lock:
            entry = self._failed.get(key) or {}
            return bool(entry.get("permanent_skip"))

    def explain_skip(self, key: str) -> str:
        """生成跳过原因说明，用于日志。"""
        attempts = self.failed_attempts(key)
        if self.is_permanently_skipped(key):
            entry = self._failed.get(key) or {}
            return (
                f"已累计失败 {attempts} 次（上限 {MAX_ATTEMPTS_PER_FILE}），永久跳过。"
                f"最后错误：{str(entry.get('last_error') or '')[:200]}。"
                "如已修正外部原因，可用 --clear-failures 或界面「清除失败记录」按钮重置。"
            )
        record = self.get_record(key)
        if record and record.get("status") == STATUS_DONE:
            return "上次已完成，跳过 API 请求（断点续跑）"
        return ""

    # --- 选择待处理项 -------------------------------------------------------

    def select_pending(
        self,
        items: list[ScanItem],
        *,
        resume: bool = True,
        only_failed: bool = False,
    ) -> tuple[list[ScanItem], list[tuple[ScanItem, str]]]:
        """挑出本次需要处理的项。

        返回 (待处理列表, 被跳过列表及原因)。

        规则（硬约束 #6 / #18）：
            - resume=True 时，status=done 的项直接跳过，不重复请求 API；
            - only_failed=True 时，只处理"失败过但未达到永久跳过阈值"的项；
            - 永久跳过的项在任何模式下都跳过（除非调用 clear_failures）。
        """
        pending: list[ScanItem] = []
        skipped: list[tuple[ScanItem, str]] = []

        for item in items:
            if self.is_permanently_skipped(item.key):
                skipped.append((item, self.explain_skip(item.key)))
                continue

            is_done = self.is_done(item.key)
            had_failure = self.failed_attempts(item.key) > 0

            if only_failed:
                # --only-failed：只关心"上次没成功的"，已完成的一律跳过。
                if not had_failure or is_done:
                    skipped.append((item, "不在失败清单中（--only-failed 模式）"))
                    continue
            elif resume and is_done:
                skipped.append((item, self.explain_skip(item.key)))
                continue

            pending.append(item)

        return pending, skipped

    # --- 更新 ---------------------------------------------------------------

    def mark_attempt(self, item: ScanItem) -> None:
        """记录一次尝试（用于统计重试次数）。"""
        with self._lock:
            record = self._records.setdefault(
                item.key,
                {
                    "path": str(item.path),
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                    "status": STATUS_PENDING,
                },
            )
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["updated_at"] = time.time()
            self._save_records()

    def mark_done(self, item: ScanItem, result: Any) -> None:
        """标记成功，并保存产出（便于续跑时无需重新请求 API）。

        每张完成即立刻落盘（硬约束 #6"已完成的不重复请求 API"，
        以及停止按钮"已完成的保留"）。
        """
        with self._lock:
            self._records[item.key] = {
                "path": str(item.path),
                "size": item.size,
                "mtime_ns": item.mtime_ns,
                "status": STATUS_DONE,
                "attempts": int((self._records.get(item.key) or {}).get("attempts") or 1),
                "params": getattr(result, "params", {}) or {},
                "curves": getattr(result, "curves", {}) or {},
                "notes": getattr(result, "notes", "") or "",
                "xmp_target": str(getattr(result, "xmp_target", "") or ""),
                "xmp_mode": str(getattr(result, "xmp_mode", "") or ""),
                "warning_count": len(getattr(result, "warnings", []) or []),
                "updated_at": time.time(),
                "error": "",
            }
            # 成功后清掉失败计数，避免"曾经失败过"影响后续 --only-failed 的判断。
            if item.key in self._failed:
                del self._failed[item.key]
                self._save_failed()
            self._save_records()

    def mark_failed(self, item: ScanItem, error: str, *, counts_toward_skip: bool = True) -> int:
        """标记失败并累加计数，返回累计失败次数。

        counts_toward_skip=False 用于**账户级错误**（额度/计费/密钥）：
        这类失败与这张图无关，不应该把它推向“永久跳过”（否则用户充完值以后
        这些图仍然不会处理，还得再点一次「清除失败记录」——一个很难发现的陷阱）。
        计数仍然照常累加，所以「只跑失败」能把它捞回来。
        """
        with self._lock:
            record = self._records.setdefault(
                item.key,
                {
                    "path": str(item.path),
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                },
            )
            record["status"] = STATUS_FAILED
            record["error"] = error[:2000]
            record["updated_at"] = time.time()

            entry = self._failed.setdefault(
                item.key,
                {
                    "path": str(item.path),
                    "first_failed_at": time.time(),
                },
            )
            # 注意：这里的 attempts 是**跨运行累计**的失败次数，
            # 与 record["attempts"]（本次运行的尝试次数）语义不同。
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["last_error"] = error[:2000]
            entry["last_failed_at"] = time.time()
            entry["permanent_skip"] = bool(
                counts_toward_skip and entry["attempts"] >= MAX_ATTEMPTS_PER_FILE
            )
            if not counts_toward_skip:
                # 让“为什么它还没被永久跳过”这件事在文件里能查到。
                entry["account_level"] = True

            self._save_records()
            self._save_failed()
            return int(entry["attempts"])

    def mark_failed_with_result(self, item: ScanItem, result: Any) -> None:
        """保存"已完成但可能部分失败"的结果（与 mark_done 结构相同）。"""
        self.mark_done(item, result)

    def clear_failures(self) -> int:
        """清空失败记录（逃生舱）。返回清除条数。"""
        with self._lock:
            count = len(self._failed)
            self._failed = {}
            self._save_failed()
            log.info("已清除失败记录 %d 条。", count)
            return count

    def reset_all(self) -> int:
        """清空全部状态（CLI 的 `--reset-state` 用）。"""
        with self._lock:
            count = len(self._records)
            self._records = {}
            self._failed = {}
            self._save_records()
            self._save_failed()
            log.info("已清空任务状态 %d 条。", count)
            return count

    def failure_summary(self) -> list[tuple[str, int, str, bool]]:
        """返回失败清单：(路径, 次数, 最后错误, 是否永久跳过)。"""
        with self._lock:
            rows: list[tuple[str, int, str, bool]] = []
            for entry in self._failed.values():
                rows.append(
                    (
                        str(entry.get("path") or ""),
                        int(entry.get("attempts") or 0),
                        str(entry.get("last_error") or ""),
                        bool(entry.get("permanent_skip")),
                    )
                )
            rows.sort(key=lambda r: (-r[1], r[0]))
            return rows

    def stats(self) -> dict[str, int]:
        """给界面用的统计。"""
        with self._lock:
            done = sum(1 for r in self._records.values() if r.get("status") == STATUS_DONE)
            failed = len(self._failed)
            permanent = sum(1 for e in self._failed.values() if e.get("permanent_skip"))
            return {"done": done, "failed": failed, "permanent_skip": permanent}
