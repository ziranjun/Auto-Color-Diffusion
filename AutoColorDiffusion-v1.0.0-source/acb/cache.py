# -*- coding: utf-8 -*-
"""缓存与断点键。

硬约束 #16：
    缩略图必须缓存：cache/thumbs/，键 = 源文件绝对路径 + 文件大小 + mtime 的哈希
    （与断点键同源），命中则跳过提取与压缩；训练模式同理。

硬约束 #11 与 Further Considerations #1（用户已裁决）：
    原始表述"键 = 文件绝对路径 + 文件大小 + mtime 的哈希"自相矛盾——
    只要键里含绝对路径，用户把整个文件夹移动/改名后所有键都会变化，
    续跑就会全部失效，与"避免用户移动文件夹后续跑全部失效"的目标直接冲突。
    因此实际实现为：键 = sha256(大小 | mtime_ns | 文件名)，**不含目录**；
    绝对路径仅作为记录字段存储，用于日志与审计，不参与哈希。

mtime 精度选择 st_mtime_ns 而非 st_mtime：
    Windows NTFS 的 mtime 是 100 纳秒精度，而 Python 的 st_mtime 是 float，
    在 2026 年这个量级下已经丢失低位（float 双精度约 15–16 位有效数字，
    而纳秒时间戳有 19 位），不同文件的 mtime 可能被 round 成同一个 float。
    用整数纳秒彻底避免这个碰撞。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import THUMB_LONG_EDGE, THUMB_MIME, THUMB_MIN_VALID_BYTES, THUMB_QUALITY
from .logging_setup import get_logger
from .paths import thumbs_dir

log = get_logger("cache")

# 键长度：sha256 十六进制取前 16 位。
# 依据：16 个十六进制字符 = 64 位空间。在"单批数千张、用户长期累积数万张"
#       的量级下，生日碰撞概率约 (5e4)^2 / 2^65 ≈ 1e-11，工程上可忽略；
#       同时比完整 64 位十六进制短，文件名更可读、路径长度压力更小。
KEY_LENGTH = 16


@dataclass(frozen=True)
class FileStamp:
    """文件的稳定标识：大小 + 纳秒级 mtime + 文件名。

    frozen=True 让它可哈希、可安全用于集合去重。
    """

    name: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        """计算缓存/断点键。"""
        raw = f"{self.size}|{self.mtime_ns}|{self.name}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return digest[:KEY_LENGTH]


def file_stamp(path: Path) -> FileStamp:
    """读取文件标识。文件不存在或无法 stat 时抛 OSError，由调用方处理。"""
    st = path.stat()
    return FileStamp(name=path.name, size=st.st_size, mtime_ns=st.st_mtime_ns)


def file_key(path: Path) -> str:
    """便捷函数：直接算键。"""
    return file_stamp(path).key


def thumb_key(stamp_key: str) -> str:
    """把"文件标识键"派生成"缩略图缓存键"：再掺一次**缩略图配方**。

    【为什么必须掺配方】FileStamp.key 只含 `大小|mtime|文件名`，它刻意与
    "怎么生成缩略图"无关 —— 因为同一个键还要当**断点键**用，而硬约束 #11
    要求"移动文件夹后键不变"、升级后也不能失效。

    但缓存里的**内容**是由配方决定的：长边、JPEG 质量、色彩处理方式
    任意一项变了，旧缓存就不再等价于新配方要的东西。
    不掺配方的后果很隐蔽：升级到改了 THUMB_LONG_EDGE / THUMB_QUALITY 的版本后，
    程序会把旧配方的缩略图当成新的直接喂给 AI —— 任务全部"成功"，
    但 AI 看到的东西和当前设置不符，看日志也查不出来。
    掺进去之后，配方一变键就变，旧缓存自然失效并重新提取。
    """
    recipe = f"{THUMB_LONG_EDGE}x{THUMB_QUALITY}|{THUMB_MIME}"
    raw = f"{stamp_key}|{recipe}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:KEY_LENGTH]


# 内容指纹的取样字节数：文件头 64KB + 文件尾 64KB。
# 依据：RAW 文件的头部含 EXIF（拍摄时间、机身序列号、缩略图），尾部含
#       MakerNotes/图像数据尾部，两者合起来足以区分任何两张不同的照片。
#       只读 128KB 而不是做全文件哈希，是因为一张 50MB 的 RAW 全哈希要
#       几十毫秒，几百张就是数十秒的额外等待；而 128KB 读取在毫秒级。
# 注意：这是**概率性**指纹，理论上存在碰撞，但它只在"文件大小与 mtime 完全
#       相同"这一已经极罕见的前提下才被使用，因此实际风险可忽略。
CONTENT_PROBE_BYTES = 64 * 1024


def content_probe(path: Path, probe_bytes: int = CONTENT_PROBE_BYTES) -> str:
    """计算内容指纹（头 + 尾 + 大小 的 sha256 前 16 位）。

    用途见 pipeline/job.py::build_scan_items —— 只在断点键发生碰撞时才调用，
    用于区分"同名同大小同 mtime 但内容不同"的两个文件。
    读取失败时返回空串，调用方会退化为按文件名分组的保守行为。
    """
    hasher = hashlib.sha256()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            hasher.update(str(size).encode("ascii"))
            hasher.update(handle.read(probe_bytes))
            if size > probe_bytes * 2:
                # 只在文件足够大时才读尾部，避免头尾取样重叠。
                handle.seek(-probe_bytes, os.SEEK_END)
                hasher.update(handle.read(probe_bytes))
    except OSError as exc:
        log.debug("读取内容指纹失败 %s：%s", path, exc)
        return ""
    return hasher.hexdigest()[:16]


class ThumbCache:
    """缩略图磁盘缓存。

    缓存文件命名 <key>.jpg。除了 JPEG 本体，同目录下写一个 <key>.json
    记录来源与色彩判定依据，这样缓存命中时仍能还原日志信息
    （否则日志会说不出"这张图当时是用哪种来源提取的"）。
    """

    def __init__(self, directory: Path | None = None, enabled: bool = True) -> None:
        self._dir = Path(directory) if directory else thumbs_dir()
        self._enabled = enabled
        if self._enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # 缓存目录不可写不应导致程序不可用，只降级为"不缓存"。
                log.warning("无法创建缩略图缓存目录 %s：%s。本次运行将不缓存。", self._dir, exc)
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def thumb_path(self, key: str) -> Path:
        return self._dir / f"{key}.jpg"

    def meta_path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> tuple[bytes, dict] | None:
        """读取缓存。命中返回 (JPEG 字节, 元数据字典)，未命中返回 None。"""
        if not self._enabled:
            return None
        path = self.thumb_path(key)
        try:
            if not path.is_file():
                return None
            if path.stat().st_size < THUMB_MIN_VALID_BYTES:
                # 小于下限说明是上次异常中断留下的残片，删掉重新生成。
                log.debug("缓存文件过小，视为损坏并删除：%s", path)
                path.unlink(missing_ok=True)
                return None
            data = path.read_bytes()
        except OSError as exc:
            log.debug("读取缩略图缓存失败 %s：%s", path, exc)
            return None

        meta: dict = {}
        try:
            meta_file = self.meta_path(key)
            if meta_file.is_file():
                import json

                meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return data, meta

    def put(self, key: str, jpeg_bytes: bytes, meta: dict) -> None:
        """写入缓存。

        写入策略：先写临时文件再原子 replace。
        理由：批量任务可能被用户中途强杀，若直接写目标文件，
        会留下半截 JPEG，下次命中时 decode 失败——那比"没缓存"更糟。
        """
        if not self._enabled:
            return
        import json

        tmp_thumb = self.thumb_path(key).with_suffix(".jpg.tmp")
        tmp_meta = self.meta_path(key).with_suffix(".json.tmp")
        try:
            tmp_thumb.write_bytes(jpeg_bytes)
            os.replace(tmp_thumb, self.thumb_path(key))
            tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_meta, self.meta_path(key))
        except OSError as exc:
            log.debug("写入缩略图缓存失败（key=%s）：%s", key, exc)
            for leftover in (tmp_thumb, tmp_meta):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> int:
        """清空缓存，返回删除的文件数（「日志 → 清除缩略图缓存」用）。"""
        if not self._dir.is_dir():
            return 0
        count = 0
        for item in self._dir.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    count += 1
            except OSError:
                continue
        return count

    def stats(self) -> tuple[int, int]:
        """返回 (文件数, 总字节数)，用于界面展示。"""
        if not self._dir.is_dir():
            return 0, 0
        count = 0
        total = 0
        for item in self._dir.iterdir():
            try:
                if item.is_file():
                    count += 1
                    total += item.stat().st_size
            except OSError:
                continue
        return count, total
