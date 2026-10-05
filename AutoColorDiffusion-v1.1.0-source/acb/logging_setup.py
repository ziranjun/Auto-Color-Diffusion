# -*- coding: utf-8 -*-
"""日志系统。

硬约束 #13：
    日志按日期轮转（单文件上限 10MB，保留 5 份）；界面日志区只显示 INFO 及以上，
    DEBUG 仅落文件。

实现要点：
    Python 标准库没有"同时按日期和大小轮转"的 handler：
      - RotatingFileHandler 只管大小，跨天不会自动开新文件；
      - TimedRotatingFileHandler 只管时间，单文件可能无限膨胀。
    因此这里实现 DatedSizeRotatingFileHandler，两个维度都管。

日期体现在文件名（logs/app-2026-09-19.log），而不是靠 rename 轮转，
好处是跨天后旧文件天然"归位"，无需搬动已在写的大文件。
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from .constants import LOG_BACKUP_COUNT, LOG_MAX_BYTES

# 文件日志的格式：时间 + 级别 + 模块名 + 消息。
# 含模块名（%(name)s 取的是 logger 名，本项目统一用 acb.xxx）便于定位问题。
FILE_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 界面日志区的格式：更短，省掉模块名（界面空间有限），保留秒级时间。
UI_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
UI_DATE_FORMAT = "%H:%M:%S"


class DatedSizeRotatingFileHandler(logging.Handler):
    """按日期 + 单文件大小双维度轮转的文件 handler。

    文件命名：
        当日主文件   logs/app-YYYY-MM-DD.log
        当日轮转备份 logs/app-YYYY-MM-DD.1.log（.1 为最新，数字越大越旧）

    行为：
        1. 每次写入前检查日期。跨天则关闭旧文件、打开新的按日文件名。
           —— 日期维度的轮转。
        2. 写入后检查当前文件大小。超过 max_bytes 则做一次"编号搬迁"：
           已存在的 .N 依次改名为 .(N+1)（从大到小搬，避免覆盖），
           再把主文件改名为 .1，然后重开主文件。
           —— 大小维度的轮转，仅作用于当天文件。
        3. 删除编号大于 backup_count 的备份，把总量钉在 (backup_count+1) 个文件内。
    """

    def __init__(
        self,
        log_dir: Path,
        stem: str = "app",
        max_bytes: int = LOG_MAX_BYTES,
        backup_count: int = LOG_BACKUP_COUNT,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)
        self._stem = stem
        self._max_bytes = int(max_bytes)
        self._backup_count = int(backup_count)
        self._encoding = encoding

        self._current_date: date | None = None
        self._stream = None  # 类型为 TextIOWrapper，延迟到首次 emit 时打开

        self._log_dir.mkdir(parents=True, exist_ok=True)

    # --- 内部工具 -----------------------------------------------------------

    def _main_path(self, day: date) -> Path:
        return self._log_dir / f"{self._stem}-{day.isoformat()}.log"

    def _backup_path(self, day: date, index: int) -> Path:
        return self._log_dir / f"{self._stem}-{day.isoformat()}.{index}.log"

    def _open_for(self, day: date) -> None:
        """关闭当前流并打开指定日期的主文件（追加模式）。"""
        self._close_stream()
        self._current_date = day
        # delay 语义：在这里显式 open，是为了能在跨天时立刻切换文件名。
        self._stream = open(self._main_path(day), "a", encoding=self._encoding, errors="replace")

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except OSError:
                # 关闭失败（如磁盘被拔出）不应让日志调用本身抛异常，
                # 否则会把业务代码带崩。
                pass
            self._stream = None

    def _rotate_by_size(self, day: date) -> None:
        """把主文件搬到 .1，并把已有备份整体后移一位。"""
        self._close_stream()
        try:
            # 从最旧的位置开始搬，倒序移动避免覆盖：
            # 例如 backup_count=5 时，先把 .4 删掉（超出保留范围），
            # 再把 .3→.4、.2→.3、.1→.2，最后 main→.1。
            oldest = self._backup_path(day, self._backup_count)
            if oldest.exists():
                oldest.unlink()
            for index in range(self._backup_count - 1, 0, -1):
                src = self._backup_path(day, index)
                dst = self._backup_path(day, index + 1)
                if src.exists():
                    src.replace(dst)
            main = self._main_path(day)
            if main.exists():
                main.replace(self._backup_path(day, 1))
        except OSError:
            # 搬迁失败（文件被其他进程占用等）不阻断日志：直接重开主文件继续追加，
            # 最坏结果是单个文件略大于 10MB。
            pass
        self._open_for(day)

    # --- logging.Handler 接口 ----------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            today = date.today()
            if self._current_date != today or self._stream is None:
                self._open_for(today)

            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()

            # 大小检查放在写入之后：只统计"已经写进去的字节"，
            # 因此单文件峰值最坏只略微超过 max_bytes 一行。
            try:
                if self._stream.tell() >= self._max_bytes:
                    self._rotate_by_size(today)
            except (OSError, ValueError):
                pass
        except Exception:
            # 日志系统自身绝不能抛异常（会污染业务逻辑），统一走标准兜底。
            self.handleError(record)

    def close(self) -> None:
        self._close_stream()
        super().close()


class UiLogSignalHandler(logging.Handler):
    """把日志转发到界面的 handler。

    硬约束 #13：界面日志区只显示 INFO 及以上，DEBUG 仅落文件。
    这里在 emit 内部再做一次级别过滤（除了 logger 上的级别控制之外），
    确保即使将来有人调低 logger 级别，界面也不会被 DEBUG 刷屏。

    跨线程安全说明：本 handler 的 emit 会在工作线程中被调用，
    而 emit 里执行的是外部传入的 callback。回调方（UI 层）必须保证
    该 callback 只做"发 Qt 信号"这件事——Qt 的跨线程信号会自动排队到
    主线程，因此不会出现子线程直接操作控件的问题（硬约束 #9）。
    """

    def __init__(self, callback, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.INFO:
            return
        try:
            self._callback(self.format(record), record.levelno)
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path, ui_callback=None) -> None:
    """初始化根 logger。

    - 文件 handler：收 DEBUG 及以上，供事后排查（硬约束 #13）。
    - 界面 handler：收 INFO 及以上（若提供 callback）。
    - 控制台 handler：仅在非打包环境下挂载，方便开发期观察。
      （打包后是 GUI 程序，没有控制台，sys.stderr 可能是 None，
        写入会抛异常，因此必须判断 isatty/存在性。）
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # 先清空已有 handler，避免重复调用 setup_logging 时日志重复输出。
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    root.setLevel(logging.DEBUG)

    file_handler = DatedSizeRotatingFileHandler(log_dir)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, FILE_DATE_FORMAT))
    root.addHandler(file_handler)

    if ui_callback is not None:
        ui_handler = UiLogSignalHandler(ui_callback, level=logging.INFO)
        ui_handler.setFormatter(logging.Formatter(UI_LOG_FORMAT, UI_DATE_FORMAT))
        root.addHandler(ui_handler)

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(UI_LOG_FORMAT, UI_DATE_FORMAT))
        root.addHandler(console)

    # 第三方库的噪声降级：requests/urllib3 在 DEBUG 下会把密钥与完整
    # 请求体写进日志（requests 的 urllib3.connectionpool 会记录 URL，
    # 而某些中转站把 key 放在 query 里）——这是必须避免的泄密途径。
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """统一的 logger 获取入口，前缀固定为 acb.*，便于按模块过滤。"""
    return logging.getLogger(f"acb.{name}" if not name.startswith("acb.") else name)
