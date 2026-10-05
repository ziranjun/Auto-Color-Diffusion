# -*- coding: utf-8 -*-
"""自动清理：退出时清临时文件 + 启动时清过期备份与缓存。

================================================================================
【本模块存在的唯一理由是"安全删除"。改代码前请先读完这段】
================================================================================

用户诉求：软件每次跑完都会留下一些临时文件，希望关闭程序时自动清掉，
         但**绝不能删到重要文件**。

因此这里只用**白名单 + 根锚定**两把锁，刻意不做"遍历目录找垃圾"：

    1. 白名单：只清理 `_junk_targets()` 里**显式列出**的位置。
       没有任何"扫一遍数据目录看哪个像垃圾"的逻辑 —— 那种写法迟早会误删。
    2. 根锚定：每个待删路径 `resolve()`（会跟随符号链接与目录联接）之后，
       必须**位于数据目录内部**，且不属于受保护目录。
       任何越界路径一律跳过并记进报告，不做"尽力而为"的删除。

    3. 拒绝链接：待删路径若是符号链接或目录联接，**一律不删**。
       我们自己建的都是普通目录；出现链接说明环境被人动过，
       而 `shutil.rmtree` 在联接上有可能把**目标目录**整个删掉。

两类清理入口（都走上面三把锁）：

    · `cleanup_session_junk()`   —— 退出时：缩略图缓存 / XMP 中转 /
                                    本次运行目录（可保留某一次以便手动补跑）；
    · `prune_old_xmp_backups()`  —— 启动时：过期（默认 7 天）的侧车备份。

启动那条由 `run_startup_cleanup()` 包一下，界面启动时调一次（用户 2026-09-26 裁决，
且明确只保留这一条 —— 不要按"打开次数"清缓存那套）。
它**刻意不碰** `runs/`：启动时无法判断"用户是不是还要手动补跑上一轮的
run_export.bat"（那条判断依赖本次会话的状态），而运行目录很小、退出时就会清掉。

明确**永不触碰**的东西：

    - 用户的照片、旁侧 .xmp、出片目录里的任何文件 ——
      本模块根本不会走到数据目录之外，连路径拼接都不会出现它们；
    - `<数据目录>/styles/`  用户训练出来的风格档案（删了要重新训练，不可再生）；
    - `<数据目录>/state/`   断点记录 records.json / failed.json
                             （删了会失去"续跑"能力与失败历史）；
    - `<数据目录>/config/`  models.yaml 的副本（用户配的模型与网关地址）。
      这三个即使在数据目录**内部**也一律保留，见 `_guard()` 的最后一段。

关于程序自身的运行日志（`logs/app-*.log`）：
    默认**不删**（`DELETE_APP_LOGS = False`）。
    它已经按 10MB × 5 份轮转，占用有硬上限；而它是出问题时**唯一**的排查线索
    —— 本项目一路都是靠它定位故障的。
    真要连它一起删，把 `DELETE_APP_LOGS` 改成 True，但请想清楚后果：
    删掉之后程序下次出问题，你将没有任何日志可查。

想临时跳过清理（例如要人工检查备份）：设环境变量 `ACB_NO_CLEANUP=1`。
退出清理与启动清理都会跳过，自检脚本因此可以零副作用地在真实数据目录旁边跑。
"""

from __future__ import annotations
from .i18n import T, TF

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from .logging_setup import get_logger
from .paths import (
    config_dir,
    data_root,
    logs_dir,
    runs_dir,
    state_dir,
    styles_dir,
    thumbs_dir,
    xmp_backups_dir,
)

log = get_logger("cleanup")

# 是否连程序自身的运行日志一起删。默认 False，理由见模块头部注释。
DELETE_APP_LOGS: Final[bool] = False

# 侧车备份保留多少天（用户 2026-09-26 裁决：7 天，**每次启动**检查一次）。
#
# 备份是“改写前把原件存一份”，存在价值随时间下降：真正要找回原件的那次
# 一定发生在发现问题的当天或第二天；一直堆着只会占地方，而且会让人
# 在翻目录时挑花眼。保留 7 天是个“足够找回、又不会无限增长”的折中。
#
# 注意：**只有这一条启动清理规则**。曾经还设计过“打开软件满 5 次清一次缓存”，
# 用户 2026-09-26 明确说不要，已移除 —— 缓存本来就在每次正常退出时清空
#（见 cleanup_session_junk），不要复活那套启动计数。
XMP_BACKUP_KEEP_DAYS: Final[int] = 7

# 侧车备份文件名里的时间戳（`<主名>.<YYYYmmdd_HHMMSS>.xmp`，见 xmp/writer.py）。
_BACKUP_STAMP_RE: Final[re.Pattern[str]] = re.compile(r"\.(\d{8})_(\d{6})\.xmp$", re.IGNORECASE)

# 设置后跳过全部清理（人工排查时用）。
DISABLE_ENV: Final[str] = "ACB_NO_CLEANUP"

# 程序自身日志的文件名模式（logging_setup 里的轮转命名）。
APP_LOG_GLOB: Final[str] = "app-*.log*"

# state/ 下唯一允许清理的子目录：写 DNG 内嵌 XMP 时的中转文件目录。
# 只清空**内容**，目录本身保留（exiftool 那条路径会按需重建）。
_TMP_XMP_DIRNAME: Final[str] = "_tmp_xmp"


@dataclass
class CleanupReport:
    """一次清理的结果。用于日志与自检，不参与任何决策。"""

    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0
    # 每个清理位置的命中数，形如 {"缩略图缓存": 18}
    by_target: dict[str, int] = field(default_factory=dict)
    # 被安全守卫拦下的路径及原因（正常运行时应当为空）
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.deleted_files or self.deleted_dirs)

    def describe(self) -> str:
        if not self.touched and not self.errors:
            return T("无临时文件需要清理")
        size = f"{self.freed_bytes / 1024:.0f} KB"
        parts = "、".join(f"{name} {count} 个" for name, count in self.by_target.items())
        text = f"已清理 {self.deleted_files + self.deleted_dirs} 项 / {size}"
        if parts:
            text += f"（{parts}）"
        if self.skipped:
            text += f"；跳过 {len(self.skipped)} 项"
        if self.errors:
            text += f"；{len(self.errors)} 项失败"
        return text


def _junk_targets() -> list[tuple[Path, str]]:
    """白名单（清空内容型）：只清空这些目录的**内容**，目录本身保留。

    每条都要能回答"删了会怎样"：
        - 缩略图缓存 → 下次重新提取，只是慢一点，零信息损失；
        - 写 XMP 的临时文件 → 正常结束后本应为空，残留说明中途异常退出。

    运行目录（`paths.runs_dir()` 下的 `<运行标识>/`）**不在**这张表里 ——
    它有"保留某一次"的额外规则（见 `purge_run_dirs`），单独处理。
    """
    return [
        (thumbs_dir(), T("缩略图缓存")),
        (state_dir() / _TMP_XMP_DIRNAME, T("XMP 中转临时文件")),
    ]


def _allowed_roots() -> list[Path]:
    """`_guard()` 允许删除的根位置。

    比 `_junk_targets()` 多一个 runs_dir：那里删的是整个运行目录，
    而不是"清空某个目录的内容"，所以不能混在同一张表里。
    """
    return [
        thumbs_dir(),
        state_dir() / _TMP_XMP_DIRNAME,
        runs_dir(),
        # 侧车备份：只删**过期**的那些（见 prune_old_xmp_backups），
        # 进白名单是为了让 _guard() 放行，而不是“整个目录随时可删”。
        xmp_backups_dir(),
    ]


def _is_link_like(path: Path) -> bool:
    """是否是符号链接 / 目录联接（junction）。

    我们自己建的都是普通目录。出现链接说明环境被人动过手脚，
    此时**一律不删** —— `shutil.rmtree` 在联接上有可能把目标目录整个删掉。
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True

    is_junction = getattr(path, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def _guard(path: Path) -> str | None:
    """删除前的安全检查。返回 None 表示允许，否则返回拒绝原因。

    这是误删的最后一道闸门。任何新增清理项都必须能通过它。
    """
    try:
        resolved = path.resolve()
        root = data_root().resolve()
    except OSError as exc:
        return f"路径无法解析（{exc}）"

    if resolved == root:
        return T("拒绝删除数据目录本身")
    if root not in resolved.parents:
        return f"不在数据目录内（数据目录={root}）"
    if _is_link_like(path):
        return T("是符号链接或目录联接，拒绝删除")

    # 白名单（同样 resolve 后比较，避免大小写/短路径差异）。
    #
    # 判定是"路径本身 **或位于**某个白名单目录之内"——只比相等是不够的：
    # 真正要删的是白名单目录里的**子文件**，只比相等会让清理静默地什么都不做。
    #
    # 为什么放在受保护目录判定**之前**：state/_tmp_xmp 位于受保护的 state/ 之内，
    # 但它本身就是白名单项。这不是漏洞——白名单是代码里写死的少数几项，
    # 而且 resolve() 会把 `..` 规范化掉，无法用 `_tmp_xmp/../records.json` 绕过
    # （那种写法解析后是 state/records.json，不是任何白名单目录的子项）。
    try:
        allowed = {target.resolve() for target in _allowed_roots()}
    except OSError:
        return T("白名单解析失败，保守起见拒绝删除")

    for allowed_path in allowed:
        if resolved == allowed_path or allowed_path in resolved.parents:
            return None

    # 受保护目录：即使在数据目录内部也一律保留。
    for protected, why in (
        (styles_dir(), T("风格档案")),
        (config_dir(), T("模型配置")),
        (state_dir(), T("断点与失败记录")),
    ):
        try:
            pr = protected.resolve()
        except OSError:
            continue
        if resolved == pr or pr in resolved.parents:
            return f"位于受保护的 {protected.name}/ 内（{why}），不在白名单"

    return T("不在清理白名单内")


def _tree_size(path: Path) -> int:
    """统计目录内文件总字节数（仅用于报告"释放了多少空间"）。"""
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not _is_link_like(child):
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _delete_one(path: Path, label: str, report: CleanupReport) -> None:
    """删掉单个条目（文件或目录）。所有失败都只记录，不抛出。"""
    reason = _guard(path)
    if reason is not None:
        report.skipped.append(f"{path.name}：{reason}")
        log.warning(T("退出清理：跳过 %s —— %s"), path, reason)
        return

    try:
        if path.is_dir():
            # _guard 已确认不是链接，rmtree 不会顺着链接跑到外面去。
            size = _tree_size(path)
            file_count = sum(1 for _ in path.rglob("*") if _.is_file())
            shutil.rmtree(path)
            report.deleted_dirs += 1
            report.deleted_files += file_count
            report.freed_bytes += size
            report.by_target[label] = report.by_target.get(label, 0) + file_count
        else:
            size = path.stat().st_size
            path.unlink()
            report.deleted_files += 1
            report.freed_bytes += size
            report.by_target[label] = report.by_target.get(label, 0) + 1
    except OSError as exc:
        # 常见原因：文件被别的进程占用（例如 Photoshop 还开着日志文件）。
        # 删不掉不是错误 —— 它是临时文件，下次启动/关闭还会再试。
        report.errors.append(f"{path.name}：{exc}")


def _clear_dir_contents(target: Path, label: str, report: CleanupReport) -> None:
    """清空一个白名单目录的**内容**，保留目录本身。

    保留目录是为了避免"清理完目录不见了"这种让人困惑的状态
    （而且启动时仍会 ensure_runtime_dirs，两种做法都安全）。
    """
    if not target.is_dir():
        return
    reason = _guard(target)
    if reason is not None:
        report.skipped.append(f"{target.name}/：{reason}")
        log.warning(T("退出清理：跳过目录 %s —— %s"), target, reason)
        return
    try:
        children = sorted(target.iterdir())
    except OSError as exc:
        report.errors.append(f"{target.name}/：{exc}")
        return
    for child in children:
        _delete_one(child, label, report)


def purge_run_dirs(keep: Path | None, report: CleanupReport) -> None:
    """删除历史运行目录（脚本三件套 + Photoshop 日志与结果）。

    `keep` 指定的那一个会被保留 —— 调用方在"本次导出没成功"时传它进来：
    那时用户可能还要双击 run_export.bat 手动补跑，把脚本删掉等于断了他的后路。

    这是本模块里唯一**删整个目录**的地方（其余都是清空内容），
    所以同样要过 `_guard()`。
    """
    root = runs_dir()
    if not root.is_dir():
        return

    try:
        keep_resolved = keep.resolve() if keep is not None else None
        children = sorted(root.iterdir())
    except OSError as exc:
        report.errors.append(f"runs/：{exc}")
        return

    for child in children:
        if not child.is_dir():
            continue
        if keep_resolved is not None:
            try:
                if child.resolve() == keep_resolved:
                    report.skipped.append(
                        f"{child.name}/：本次导出未完成，保留以便手动补跑"
                    )
                    continue
            except OSError:
                pass
        _delete_one(child, T("运行目录（脚本与日志）"), report)


def cleanup_session_junk(keep_run_dir: Path | None = None) -> CleanupReport:
    """清理本次会话产生的临时文件。**不抛异常**（关闭流程不能被它打断）。

    keep_run_dir：要保留下来的运行目录（本次导出未成功时传它，
    让用户还能手动补跑）。见 `purge_run_dirs`。

    顺序说明：先清缓存与临时文件，最后才是程序日志（见 `delete_app_logs`）——
    因为删日志需要关闭 logging handler，关掉之后就再也没地方写日志了。
    """
    report = CleanupReport()

    if os.environ.get(DISABLE_ENV):
        report.skipped.append(f"环境变量 {DISABLE_ENV} 已设置，跳过清理")
        log.info(T("退出清理：%s 已设置，跳过。"), DISABLE_ENV)
        return report

    root = data_root()
    if not root.is_dir():
        # 数据目录都不存在，没什么可清的。这一步也是安全兜底：
        # 后面所有路径都以它为前提。
        return report

    for target, label in _junk_targets():
        _clear_dir_contents(target, label, report)

    # 运行目录：导出跑完就没用了（脚本靠绝对路径出片），关掉程序时一并清掉。
    purge_run_dirs(keep_run_dir, report)

    return report


def _close_log_handlers() -> None:
    """关闭并摘掉所有 logging handler，让日志文件可以被删除。

    Windows 上**正在被打开的文件无法删除**：FileHandler 会一直持有句柄，
    不先关掉只会拿到 PermissionError，而不是"文件不存在"。
    这也是本函数只能在退出流程**最后**调用的原因。
    """
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭阶段不能再抛
            pass
        root_logger.removeHandler(handler)


def delete_app_logs(report: CleanupReport) -> None:
    """按需删除程序自身的运行日志（默认关闭，见 DELETE_APP_LOGS）。

    必须**最后**调用：它会关掉日志 handler，之后再 log 就没人接了，
    所以调用方要先把清理摘要写进日志。
    """
    if not DELETE_APP_LOGS:
        return
    logs = logs_dir()
    if not logs.is_dir():
        return

    files: list[Path] = []
    try:
        files = sorted(p for p in logs.glob(APP_LOG_GLOB) if p.is_file())
    except OSError:
        return

    _close_log_handlers()
    for path in files:
        _delete_one(path, T("程序运行日志"), report)


# ============================================================================
# 启动清理（用户 2026-09-26 裁决的两条规则）
# ============================================================================

def _backup_timestamp(path: Path) -> datetime | None:
    """从备份文件名里取出它的创建时刻；取不到则退回 mtime。

    【为什么优先看文件名】备份名就是 `<主名>.<YYYYmmdd_HHMMSS>.xmp`
    （见 xmp/writer.py）：名字里的时间是**写入那一刻**的事实。而 mtime 会变
    —— 用户复制/备份/同步/杀毒软件扫过之后，一个两年前的原件看上去会很"新"，
    于是永远删不掉。反过来，名字解析不出来（用户手改过名）时才用 mtime，
    而且宁可晚删（mtime 偏新就保留），不可误删。
    """
    match = _BACKUP_STAMP_RE.search(path.name)
    if match:
        try:
            return datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def prune_old_xmp_backups(days: int = XMP_BACKUP_KEEP_DAYS,
                          now: datetime | None = None) -> CleanupReport:
    """删掉过期的侧车（.xmp）备份。**不抛异常**。

    只动 `<数据目录>/backups/xmp/` 下的 **.xmp 文件**：
      · 非 .xmp（用户自己往里面放的东西）一律不碰；
      · 子目录不碰（`_delete_one` 只被喂文件）；
      · 目录本身、以及 backups/ 的父层都删不到（`_guard` 的白名单只到
        `backups/xmp/`，根锚定还会拦下"数据目录本身"）。
    """
    report = CleanupReport()
    folder = xmp_backups_dir()
    if not folder.is_dir():
        return report

    cutoff = (now or datetime.now()) - timedelta(days=max(0, int(days)))
    try:
        candidates = sorted(p for p in folder.glob("*.xmp") if p.is_file())
    except OSError as exc:
        report.errors.append(f"backups/xmp/：{exc}")
        return report

    for path in candidates:
        stamp = _backup_timestamp(path)
        if stamp is None:
            # 连 mtime 都读不到（权限/被占用）：保守起见留着，下次再说。
            report.skipped.append(f"{path.name}：读不到时间，保留")
            continue
        if stamp <= cutoff:
            _delete_one(path, T("过期的侧车备份"), report)
    return report


def run_startup_cleanup() -> CleanupReport:
    """程序启动时的自动清理，返回清理报告（供日志与自检）。

    规则只有一条（用户 2026-09-26 裁决）：**侧车备份保留 7 天**，每次启动检查一次。

    ⚠ 别再往里加"按打开次数清缓存"之类的规则 —— 用户明确说不要（见
    `XMP_BACKUP_KEEP_DAYS` 的注释）；缓存本来就在每次正常退出时清空。

    整体包在 try 里：清理是本程序的附加服务，绝不能拦住启动。
    `ACB_NO_CLEANUP=1` 时直接跳过（自检工具靠它做到零副作用）。
    """
    report = CleanupReport()
    if os.environ.get(DISABLE_ENV):
        log.info(T("启动清理：%s 已设置，跳过。"), DISABLE_ENV)
        return report
    try:
        report = prune_old_xmp_backups()
        if report.touched or report.errors or report.skipped:
            log.info(TF('启动清理（备份）：{0}', report.describe()))
    except Exception as exc:  # noqa: BLE001 - 启动流程不能被清理打断
        log.warning(T("启动清理（备份）出错（已忽略，不影响启动）：%s"), exc)
    return report
