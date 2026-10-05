# -*- coding: utf-8 -*-
"""输出模式主流程（硬约束第一节的 1–6 步）。

流程
----
    1. 扫描白名单 RAW（递归子目录，大小写不敏感）
    2. 提取嵌入式预览（exiftool → rawpy 兜底），命中缓存则跳过
    3. ICC 转换到 sRGB + 长边 1024 + q85 JPEG（缓存）
    4. AI 分析（并发，带预算与重试）
    5. 写 XMP（非 DNG 写侧车；DNG 写回文件内部）
    6. 生成 Photoshop .jsx + manifest，并按需调用 Photoshop 导出

并发与线程纪律（硬约束 #9）
---------------------------
本模块**完全不接触任何 UI 控件**。所有对外反馈都通过 JobCallbacks 里的
普通 Python 回调函数完成，由 UI 层负责把回调转成 pyqtSignal 发射。
这样即使本模块跑在 QThread 里，也不违反"严禁在子线程操作任何 UI 控件"。

网络请求只在并发 worker 线程里发起，主线程（QThread 的 run 体）只做编排，
不直接发请求——满足"严禁在主线程发起网络请求"。

停止语义（硬约束 #8）
--------------------
「停止」= 跳过剩余请求，已完成的保留。
实现：每个阶段开始前检查 should_stop()；AI 阶段用 future 取消尚未开始的任务；
每张完成即写盘（JobState.mark_done 原子落盘），因此停止后进度天然保留。
"""

from __future__ import annotations
from ..i18n import T, TF

import threading
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..ai.adapter import AdapterLike, ItemResult, PreviewItem
from ..ai import guardrails
from ..ai.prompts import build_output_user_text, render_style_block
from ..cache import ThumbCache, thumb_key
from ..constants import (
    AUTOPILOT_PROMPT,
    BUDGET_MULTIPLIER,
    COLLISION_MAX_INDEX,
    DEFAULT_COLOR_SPACE,
    DEFAULT_EXPORT_FORMAT,
    DEFAULT_PROMPT,
    DEFAULT_PS_JPEG_QUALITY,
    DEFAULT_STYLE_NAME,
    DEFAULT_WORKERS,
    DRY_RUN_LIMIT,
    EXPORT_DIR_NAME,
    EXPORT_DIR_NAME_DRYRUN,
    EXPORT_SUFFIX_COLLISION_SEPARATOR,
    EXPORT_SUFFIX_SEPARATOR,
    PROGRESS_FILE_STAGE_PERCENT,
    PROGRESS_POST_STEPS_MIN,
    SKIN_A_RED,
    SKIN_B_YELLOW,
    SKIN_RATIO_MIN,
    STYLE_EXECUTION_PROMPT,
    WARN_AUTO_MATCH,
    extension_for_format,
    is_dng,
    is_heif,
    resolve_export_format,
    resolve_export_suffix,
    uses_embedded_xmp,
)
from ..errors import PreviewExtractionError, StopRequestedError
from ..logging_setup import get_logger
from ..paths import interop_dir
from ..ps import jsx_render, photoshop
from ..raw import decode as raw_decode
from ..raw.camera_profile import guess_camera_profile
from ..raw.exiftool import ExiftoolRunner
from ..raw import icc
from ..raw import lens, orientation
from ..xmp import masks as masks_mod
from ..raw.preview import extract_thumbnail
from ..raw.stats import ImageStats, batch_medians, compute_stats
from ..xmp import dng as xmp_dng
from ..xmp import reader as xmp_reader
from ..xmp import writer as xmp_writer
from .job import JobState, ScanItem, build_scan_items, iter_raw_files
from . import mask_registry
from . import wb_baseline as wb_registry
from .style_profile import is_autopilot_style

log = get_logger("output_mode")

# 尾缀识别（硬约束 #14 的修订版）：把"用户自定义尾缀"从源主名末尾剥掉，
# 避免产出 "photo-edit-edit.jpg" 这种叠加后缀。
# 旧版用的是写死的 "_E" 正则；现在尾缀由用户决定，所以剥除逻辑也参数化。
#
# 刻意**不**去剥裸的 "-\d+"：那会把 "DSC-1234" 这类正常文件名毁成 "DSC"，
# 再导出成 "DSC-1.jpg"。默认尾缀 "-1" 与计数器因此不参与剥离，
# 冲突时的递增完全靠 build_export_plan 的冲突检测完成（见那里）。


@dataclass
class JobCallbacks:
    """向 UI 汇报的回调集合。

    默认全部为空实现，因此本模块在 CLI/无界面模式下也能直接用，
    不必到处写 if callbacks is not None。
    """

    log: Callable[[str, int], None] = lambda message, level: None
    progress: Callable[[int, int], None] = lambda done, total: None
    stage: Callable[[str], None] = lambda text: None
    item_status: Callable[[str, str], None] = lambda name, status: None
    should_stop: Callable[[], bool] = lambda: False

    def info(self, message: str) -> None:
        self.log(message, 20)  # logging.INFO

    def warn(self, message: str) -> None:
        self.log(message, 30)  # logging.WARNING

    def error(self, message: str) -> None:
        self.log(message, 40)  # logging.ERROR

    def check_stop(self) -> None:
        """若用户请求停止，抛异常立刻脱出（由上层捕获当作正常结束）。"""
        if self.should_stop():
            raise StopRequestedError(T("用户请求停止"))


@dataclass
class OutputOptions:
    """输出模式的全部可调参数（对应界面上的每一个控件）。"""

    sources: list[Path] = field(default_factory=list)
    prompt: str = ""
    style_name: str | None = None
    style_data: dict[str, Any] | None = None

    color_space: str = DEFAULT_COLOR_SPACE
    # 导出格式：JPG / PNG（见 constants.EXPORT_FORMATS；没有 TIFF，原因见那里）。
    export_format: str = DEFAULT_EXPORT_FORMAT
    # 用户自定义的文件名尾缀原文（如 "edit"）。留空 = 用默认尾缀 "-1"。
    # 存原文而不是解析结果：界面回显与 build_export_plan 共用同一份输入，
    # 解析只发生一处（resolve_export_suffix），不会出现两份规则。
    export_suffix: str = ""
    # 输出 JPG 质量：直接使用 Photoshop 的 0–12 刻度（0 最低、12 最高）。
    # 不再用 libjpeg 语义的档位名——原因见 constants 里 PS_JPEG_QUALITY_MIN 的注释。
    # 注意：只对 JPG 生效；PNG 是无损格式，这个值会被忽略。
    ps_quality: int = DEFAULT_PS_JPEG_QUALITY
    output_dir: Path | None = None          # 自定义输出目录（显式指定时优先）
    # 「输出到源目录同层」——**默认 True**：导出的照片与 RAW 放在一起，
    # 这是用户明确要求的默认行为（以前默认塞进 _export 子目录，找图很别扭）。
    # 离线调试（演练）另有规则，见 export_dir_for()。
    output_to_source_dir: bool = True

    recursive: bool = True
    # 断点续跑：True 时跳过已完成的文件（不重复请求 API）。
    # **默认 False**：真实反馈过"想重跑却被静默跳过"——用户要的是可控，
    # 而不是替他省钱。需要续跑时显式打开（界面「续跑」按钮 / --resume）。
    resume: bool = False
    only_failed: bool = False
    dry_run: bool = False
    dry_run_no_xmp: bool = False

    workers: int = DEFAULT_WORKERS
    max_requests: int | None = None
    use_cache: bool = True
    prefer_raw_decode: bool = False
    allow_raw_decode_fallback: bool = True
    dng_sidecar: bool = False               # DNG 是否也生成旁侧文件
    run_photoshop: bool = True


@dataclass
class OutputResult:
    """一次输出模式运行的汇总。"""

    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    # 被「永久跳过」的文件名（累计失败达上限）。**必须点名**：
    # 只给一个“跳过 N 张”的话，用户无法把它与“少处理了几张”对上号 ——
    # 真实反馈：“DNG 文件依然没有被处理”，而当时唯一的信息就是那个数字。
    permanent_skips: list[str] = field(default_factory=list)
    stopped: bool = False
    # 出片目录：**只放出图**（界面「打开输出目录」按钮指的就是这里）。
    export_dir: Path | None = None
    # 本次实际使用的导出格式（JPG / PNG）。
    export_format: str = DEFAULT_EXPORT_FORMAT
    # 本次运行的脚本产物目录，在软件数据目录下（见 jsx_render.script_dir_for）。
    # 里面是 manifest.json / export_batch.jsx / run_export.bat 与 PS 日志、结果。
    # 关闭程序时会连目录一起删掉（cleanup.purge_run_dirs），所以
    # 想手动双击 run_export.bat 补跑，必须在**关闭程序之前**做。
    script_dir: Path | None = None
    jsx_path: Path | None = None
    manifest_path: Path | None = None
    bat_path: Path | None = None
    ps_status: str = ""
    # 结构化的是否成功：None = 本次没有调用 Photoshop（用户关掉了开关），
    # False = 调用了但没全部成功（含部分失败），True = 全部成功。
    # **界面与命令行只用它做决策**（要不要保留脚本目录给用户手动补跑），
    # 绝不对 ps_status 做子串匹配 —— 那是踩过坑的写法：失败时的文案
    # "完成（成功 0/1，失败 1）"里含"成功"二字，会把判断骗反。
    ps_ok: bool | None = None
    # Photoshop 侧日志的实际落盘位置（就在 script_dir 里，见 jsx_render 的注释）。
    # 放在这里是为了让界面能告诉用户"日志到底在哪"——
    # 用户拿到的只有 JPG，找不到日志就等于没有线索。
    ps_log_path: Path | None = None
    usage: str = ""
    budget: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [
            f"完成 {self.done} 张，失败 {self.failed} 张，跳过 {self.skipped} 张（共 {self.total} 张）",
        ]
        if self.permanent_skips:
            names = "、".join(self.permanent_skips[:6])
            if len(self.permanent_skips) > 6:
                names += f" 等 {len(self.permanent_skips)} 个"
            lines.append(
                f"⚠ 其中 {len(self.permanent_skips)} 个是「此前失败达上限、永久跳过」的，"
                f"本次根本没处理：{names}。\n"
                "   要重试它们：点菜单「日志 → 清除失败记录」后重新运行。"
            )
        if self.stopped:
            lines.append(T("任务被手动停止：已完成的处理结果已保留，可随时点「续跑」继续。"))
        if self.export_dir:
            lines.append(f"输出目录：{self.export_dir}")
        if self.script_dir:
            lines.append(f"脚本目录（关闭程序后自动清理）：{self.script_dir}")
        if self.jsx_path:
            lines.append(f"Photoshop 脚本：{self.jsx_path}")
        if self.ps_status:
            lines.append(f"Photoshop：{self.ps_status}")
        if self.usage:
            lines.append(f"Token 用量：{self.usage}")
        if self.budget:
            lines.append(f"请求预算：{self.budget}")
        return "\n".join(lines)


# ============================================================================
# 导出路径规划（硬约束 #14）
# ============================================================================

def strip_export_suffix(stem: str, suffix: str | None = None) -> str:
    """剥掉主文件名末尾已有的**自定义**尾缀。

    避免 photo-edit.cr3 → photo-edit-edit.jpg 这种叠加（用户明确要求）。
    只剥用户自定义的尾缀 —— 那是用户自己的标记，剥掉是安全的；
    默认尾缀（"-1"）长得像普通编号，剥它会毁掉 "DSC-1234" 这类正常文件名
    （变成 "DSC"，再导出成 "DSC-1.jpg"），所以 suffix=None 时原样返回。
    """
    if suffix and len(stem) > len(suffix) and stem.lower().endswith(suffix.lower()):
        return stem[: -len(suffix)]
    return stem


def export_dir_for(raw_path: Path, opts: OutputOptions) -> Path:
    """决定某个 RAW 的**出片**目录。

    这里只决定出图落在哪；manifest/jsx/bat 这些脚本产物一律落在
    软件数据目录下的运行目录里（见 jsx_render.script_dir_for），
    **不会污染出片目录**。

    优先级：
        1. 用户显式指定的输出目录（界面「输出目录」框）——显式选择永远优先；
        2. 离线调试（演练）→ <源目录>/_export_dryrun/。演练产物与正式产物**绝不混放**：
           混在一起就分不清哪张是试出来的、哪张是真的，而且演练不调 Photoshop，
           留在源目录里只会让人误以为"已经导过了"；
        3. 默认（output_to_source_dir=True）→ RAW 所在目录本身；
        4. 取消勾选 → <源目录>/_export/。
    """
    if opts.output_dir is not None:
        return opts.output_dir
    if opts.dry_run:
        return raw_path.parent / EXPORT_DIR_NAME_DRYRUN
    if opts.output_to_source_dir:
        return raw_path.parent
    return raw_path.parent / EXPORT_DIR_NAME


def build_export_plan(items: list[ScanItem], opts: OutputOptions) -> dict[str, Path]:
    """为整批文件规划导出路径，并解决重名。

    返回 {源文件绝对路径: 输出路径}。**用路径而不是断点键做映射键**，
    这是一个必须遵守的约定：
        断点键不含目录（为满足"移动文件夹后键不变"），因此两个同名副本
        可能共用同一个键（内容相同的情况）。
        如果这里也用键做映射，第二个文件会覆盖第一个，导致**少出一张图**——
        这类问题在日志里完全看不出来（任务全部成功）。
    用路径做键则天然一一对应，每个源文件都有自己的输出名。

    命名规则（用户明确要求）：
        原主文件名 + 尾缀 + 扩展名。
        尾缀 = 用户在界面上填的那个（留空则用默认 "-1"）。
        重名时：
            自定义尾缀（-edit）→ 尾缀后追加 _2、_3：x-edit.jpg、x-edit_2.jpg
            默认尾缀（-1）    → 数字递增：          x-1.jpg、x-2.jpg
        （默认尾缀刻意不写成 x-1_2.jpg —— 那看起来像"1 的第 2 个变体"，
          而用户要的是"第 2 张导出图"。）
    扩展名由 opts.export_format 决定（JPG/PNG）。
    任何情况下都**不覆盖已存在的文件**。
    """
    plan: dict[str, Path] = {}
    used: set[str] = set()

    # 尾缀与格式都在这里解析一次（单一入口），界面只负责把用户原文传进来。
    suffix, is_default_suffix = resolve_export_suffix(opts.export_suffix)
    export_format, format_note = resolve_export_format(opts.export_format)
    if format_note:
        log.warning("%s", format_note)
    extension = extension_for_format(export_format)

    for item in items:
        target_dir = export_dir_for(item.path, opts)
        base = strip_export_suffix(
            item.path.stem, None if is_default_suffix else suffix
        )

        candidate = target_dir / f"{base}{suffix}{extension}"
        if str(candidate).lower() in used or candidate.exists():
            for index in range(2, COLLISION_MAX_INDEX + 1):
                name = (
                    f"{base}{EXPORT_SUFFIX_SEPARATOR}{index}{extension}"
                    if is_default_suffix
                    else f"{base}{suffix}{EXPORT_SUFFIX_COLLISION_SEPARATOR}{index}{extension}"
                )
                candidate = target_dir / name
                if str(candidate).lower() not in used and not candidate.exists():
                    break
            else:
                # 极端情况：同名过多。退化为追加时间戳，保证不覆盖任何文件。
                import time

                candidate = target_dir / f"{base}{suffix}_{int(time.time())}{extension}"
                log.warning(T("同名文件过多，已改用时间戳命名：%s"), candidate.name)

        used.add(str(candidate).lower())
        plan[str(item.path)] = candidate

    return plan


# ============================================================================
# 阶段 2–3：预览提取（含缓存与统计）
# ============================================================================

@dataclass
class PreviewOutcome:
    """一张图的预览结果。"""

    item: ScanItem
    jpeg: bytes | None = None
    source: str = ""
    stats: ImageStats | None = None
    # 这张图的 as-shot 白平衡基准（已含“不许漂移”的判定，见 wb_baseline 模块）
    # 与给模型看的那句话（注入逐图指标行）。
    wb_baseline: wb_registry.WbBaseline = field(
        default_factory=lambda: wb_registry.WbBaseline(None, None)
    )
    wb_text: str = ""
    from_cache: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _prepare_one(
    item: ScanItem,
    *,
    exiftool: ExiftoolRunner,
    cache: ThumbCache,
    opts: OutputOptions,
) -> PreviewOutcome:
    """处理单张图的预览：先查缓存，未命中则提取并写缓存。

    硬约束 #16：缩略图必须缓存；键与断点键同源；
    命中则跳过提取与压缩（这是批量重跑时最省时间的一环）。
    """
    outcome = PreviewOutcome(item=item)

    # --- 缓存命中 ---
    if cache.enabled:
        # 缓存键 = 文件标识键 + 缩略图配方（见 cache.thumb_key 的注释）。
        hit = cache.get(thumb_key(item.key))
        if hit is not None:
            jpeg, meta = hit
            outcome.jpeg = jpeg
            outcome.source = str(meta.get("source") or "cache")
            outcome.from_cache = True
            for warning in meta.get("warnings") or []:
                outcome.warnings.append(str(warning))

    # --- 未命中：提取 ---
    if outcome.jpeg is None:
        try:
            result = extract_thumbnail(
                item.path,
                exiftool,
                prefer_raw_decode=opts.prefer_raw_decode,
                allow_raw_decode_fallback=opts.allow_raw_decode_fallback,
            )
        except PreviewExtractionError as exc:
            outcome.error = str(exc)
            return outcome

        outcome.jpeg = result.jpeg_bytes
        outcome.source = result.source
        outcome.warnings.extend(result.warnings)
        if cache.enabled:
            cache.put(
                thumb_key(item.key),
                result.jpeg_bytes,
                {
                    "source": result.source,
                    "color_reason": result.color_reason,
                    "warnings": result.warnings,
                },
            )

    # --- 统计量（供批内一致性模式与 caption 通道使用）---
    try:
        outcome.stats = compute_stats(outcome.jpeg)
    except Exception as exc:
        # 统计失败不影响主流程（只是失去批内一致性目标）。
        log.debug(T("统计计算失败 %s：%s"), item.path.name, exc)
        outcome.warnings.append(f"画面统计计算失败：{exc}")

    # --- as-shot 白平衡（给模型的绝对基准）---
    # 【不要挪到下面去】模型必须知道"这张图的起点是多少 K"，否则它只能拿
    # 风格文本里的绝对区间当目标值（实测把 4600K 的片子写成 6100K）。
    # 成本：每张图多一次 exiftool 调用（~60–90 ms），相对一次 API 请求可以忽略。
    #
    # 基准走登记表（wb_baseline）：**不能**直接把文件里读到的值当基准 ——
    # 对 TIFF/HEIC 这类把参数写进自身格式的文件，我们上一轮写进去的值
    # 会被当成这一轮的起点，结果是“每跑一次更暖一点”（用户 2026-09-26 实测到）。
    outcome.wb_baseline = wb_registry.effective_as_shot(item.key, item.path, exiftool)
    outcome.wb_text = _wb_prompt_text(outcome.wb_baseline, outcome.stats)

    return outcome


# ============================================================================
# 阶段 5：写 XMP
# ============================================================================

@dataclass
class XmpOutcome:
    ok: bool
    target: Path | None = None
    mode: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    # 新建 XMP 时写进去的相机配置（None = 没能确定），用于批次级汇总提示。
    camera_profile: str | None = None
    camera_profile_note: str = ""
    created_new: bool = False
    # 交给 Photoshop 的那份文件（HEIF 走中转 TIFF，其余就是原文件）。
    acr_target: Path | None = None
    # 本次写进去的蒙版 CorrectionSyncID（没写蒙版就是空）。
    # 写成功后要交给 `mask_registry` 登记：下一轮靠它认哪些蒙版是**我们**写的，
    # 从而只替换我们的、不碰用户手工的那一批。
    mask_sync_ids: tuple[str, ...] = ()


def _intermediate_tiff_path(raw_path: Path) -> Path:
    """HEIF 的中转 TIFF 路径：`<数据目录>/state/_tmp_xmp/<主名>.<哈希>.tif`。

    放这里而不是出片目录（出片目录只该有 JPG）也不放用户照片目录（不能堆垃圾）：
    这个目录本来就在退出清理的白名单里（"XMP 中转临时文件"），程序一关就清空。
    名字带路径哈希：同名不同目录的两张照片不会互相覆盖，
    而同一次会话里反复处理同一张也不会有多份 200MB 的 TIFF 堆着。
    """
    import hashlib

    digest = hashlib.sha1(str(raw_path.resolve()).lower().encode("utf-8")).hexdigest()[:8]
    return interop_dir() / f"{raw_path.stem}.{digest}.tif"


def _resolve_camera_profile(
    raw_path: Path,
    exiftool: ExiftoolRunner,
    opts: OutputOptions,
) -> tuple[str | None, str]:
    """需要新建 XMP 时才去推导相机配置（已有文件一律保持原值）。

    为什么先判"已有设置里有没有相机配置"：读机内设置要走一次 exiftool
    （约 60–90 ms/张），而"文件里的那份"才是用户的真相，已有就不必去读。
    注意判断位置随格式不同（DNG/TIFF 在**文件内部**，其余在旁侧 .xmp）。
    """
    if is_dng(raw_path.name) and opts.dng_sidecar:
        sidecar = xmp_writer.embedded_sidecar_path_for(raw_path)
        text = ""
        if sidecar.is_file():
            try:
                text = sidecar.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
    elif uses_embedded_xmp(raw_path.name):
        # DNG / TIFF：设置写在文件内部（实测 ACR 读的就是它）。
        packet = b""
        if exiftool.available:
            try:
                packet = exiftool.read_xmp_packet(raw_path) or b""
            except Exception as exc:  # noqa: BLE001 - 读不到就当"没有"，不阻断
                log.debug(T("%s 读内嵌 XMP 失败：%s"), raw_path.name, exc)
        text = packet.decode("utf-8", "replace")
    else:
        sidecar = xmp_writer.sidecar_path_for(raw_path)
        text = ""
        if sidecar.is_file():
            try:
                text = sidecar.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""

    if 'crs:CameraProfile="' in text:
        # 已有相机配置 → 那才是用户的真相，没必要去读机内设置。
        return None, T("已有内嵌 XMP，相机配置保持原值")

    guess = guess_camera_profile(raw_path, exiftool)
    return guess.value, guess.reason


def _metrics_line(stats: ImageStats | None, wb_text: str = "") -> str | None:
    """把一张图的本地统计量压成一行指标（注入提示词，见步骤 3）。

    刻意比 caption 更短：caption 是给"看不到图"的模型的完整描述，
    而这行是给视觉模型的**量化锚点** —— 它已经能看到图，只需要
    "这张图现在的亮度/对比/饱和度/色温大概在什么位置"来做横向比较。

    ``wb_text`` 是这张图的 **as-shot 白平衡**（ACR 打开时的起点）。
    为什么要给模型：``crs:Temperature`` 是绝对开尔文，模型看不到 as-shot 时
    就只能拿风格文本里的绝对区间当目标值（实测写成 +1500K，人像肤色直接偏黄）。
    注意它和"估算色温"不是一回事：后者是程序从缩略图算的，前者是文件里的元数据。

    肤色那一截（2026-09-26 用户反馈「AI 画蒙版时根本不会调整蒙版的色温色调」）：
    局部色温/色调是修肤色最直接的手（全局白平衡动它会让背景一起变色），
    而模型对"这张图肤色到底偏不偏"只能靠肉眼估 —— 所以把实测值（占比 /
    肤色 a* / b*）与**该往哪个方向调**直接写给它，判定用 constants 里的 SKIN_* 阈值
    （它们同时也是程序侧的口径，避免"提示词说一套、程序判另一套"）。
    """
    if stats is None:
        return wb_text or None
    line = (
        f"亮度均值 {stats.luma_mean:.0f}/255（P5 {stats.luma_p05:.0f}、P95 {stats.luma_p95:.0f}）、"
        f"亮度标准差（对比度代理）{stats.luma_std:.0f}、平均饱和度 {stats.saturation_mean:.0f}/255、"
        f"估算色温 {stats.estimated_cct:.0f}K、死黑占比 {stats.shadow_clip_ratio * 100:.1f}%、"
        f"死白占比 {stats.highlight_clip_ratio * 100:.1f}%"
    )
    if stats.skin_ratio >= SKIN_RATIO_MIN:
        line += (
            f"、肤色占比 {stats.skin_ratio * 100:.0f}%（肤色 a* {stats.skin_a:+.0f}、"
            f"b* {stats.skin_b:+.0f}）"
        )
        if stats.skin_b >= SKIN_B_YELLOW:
            line += "→ 肤色偏黄：面部蒙版请用**负**的局部色温（变冷）往回收"
        elif stats.skin_a >= SKIN_A_RED:
            line += "→ 肤色偏红：面部蒙版请用局部色调往回收"
    if wb_text:
        line += f"、{wb_text}"
    return line


def _as_shot_wb_text(raw_path: Path, exiftool: ExiftoolRunner) -> str:
    """拼一句给模型看的 as-shot 白平衡描述（读不到时明说读不到）。

    "读不到"必须写出来：否则模型会把"没给"理解成"可以自己定"，
    而它给的就是一个绝对值。
    """
    kelvin, tint = guardrails.read_as_shot_wb(raw_path, exiftool)
    if kelvin is None:
        return T("机身 as-shot 白平衡：读不到（这种情况请不要给 Temperature/Tint）")
    if tint is None:
        return TF(
            '机身 as-shot 色温 {0}K（这是这张图的起点，Temperature 要相对它判断）',
            f"{kelvin:.0f}",
        )
    return TF(
        '机身 as-shot 色温 {0}K、Tint {1}（这是这张图的起点，Temperature/Tint 都要相对它判断）',
        f"{kelvin:.0f}",
        f"{tint:+.0f}",
    )


def _wb_prompt_text(baseline: wb_registry.WbBaseline, stats: ImageStats | None) -> str:
    """白平衡基准 + “方向不许叠加”的**逐图事实**（一起进指标行）。

    为什么要把判断结果直接写给模型：用户 2026-09-26 的原话是
    「本来就黄，方向让调黄，结果更黄」。模型看不到 as-shot，而"估算色温"
    它又不一定肯自己做减法，所以程序把结论直接给出：
    **这张图的观感已经比 as-shot 更暖 → 不要再给偏暖的偏移**。
    护栏会在写盘前做同样的事（两道，一道软一道硬）。
    """
    if baseline.kelvin is None:
        return T("机身 as-shot 白平衡：读不到（这种情况请不要给 Temperature/Tint）")

    if baseline.tint is None:
        text = TF(
            '机身 as-shot 色温 {0}K（这是这张图的起点，Temperature 要相对它判断）',
            f"{baseline.kelvin:.0f}",
        )
    else:
        text = TF(
            '机身 as-shot 色温 {0}K、Tint {1}（这是这张图的起点，Temperature/Tint 都要相对它判断）',
            f"{baseline.kelvin:.0f}",
            f"{baseline.tint:+.0f}",
        )
    if (
        stats is not None
        and stats.estimated_cct <= baseline.kelvin - guardrails.WB_WARM_CONFLICT_MARGIN_K
    ):
        text += TF(
            '；⚠ 这张图的观感已经比 as-shot 更暖（缩略图估算 {0}K）→ 不要再给偏暖的 Temperature，'
            '要动就只能往中性/偏冷走',
            f"{stats.estimated_cct:.0f}",
        )
    return text


def _panel_names_for(params: dict) -> list[str]:
    """这一张图用到了哪些面板（按字段分组去重，保持 fields.py 的登记顺序）。

    【可观测性 · 步骤 0】用户抱怨"风格没生效"时，日志里必须能直接看到
    "程序当时认为这张图动了哪几个面板"，而不是让他自己去翻 XMP。
    """
    from ..xmp import fields as F

    seen: dict[str, None] = {}
    for name in params:
        spec = F.get_field(name)
        if spec is None:
            continue
        seen.setdefault(F.GROUP_LABELS.get(spec.group, spec.group), None)
    return list(seen)


def _panel_usage(params_list: list[dict]) -> list[tuple[str, int, int]]:
    """统计每个 ACR 面板被多少张图用到（面板名, 用到的张数, 字段总数）。

    为什么要它：用户反馈"只调了亮和颜色"，而日志里只写"应用 6 个字段"，
    看不出是哪几个面板。没有这条统计，就只能在用户投诉后才去翻 XMP。
    """
    from ..xmp import fields as F

    usage: dict[str, list[int]] = {}
    for params in params_list:
        seen: set[str] = set()
        for name in params:
            spec = F.get_field(name)
            if spec is None:
                continue
            group = F.GROUP_LABELS.get(spec.group, spec.group)
            entry = usage.setdefault(group, [0, 0])
            entry[1] += 1
            seen.add(group)
        for group in seen:
            usage[group][0] += 1
    return sorted(
        ((group, counts[0], counts[1]) for group, counts in usage.items()),
        key=lambda row: (-row[1], row[0]),
    )


def _style_uses_masks(style: dict[str, Any] | None) -> bool:
    """这套风格值不值得给图加局部调整（准入门槛）。

    依据只能是**用户自己的训练素材**：训练时统计的"带蒙版样本占比"达到阈值，
    才算"他有这个习惯"。没达到就不写 —— 宁可少写，也不要把局部调整
    当成'看起来更专业'的装饰强加给每张图。

    自主决策风格（AI自主决策）没有训练素材，也就没有可依据的习惯；
    但因为用户明确选择了"不设约束"，这里放行（由用户在界面上自己承担）。
    """
    if not isinstance(style, dict):
        return False
    if is_autopilot_style(style):
        return True
    usage = style.get("mask_usage")
    if not isinstance(usage, dict):
        return False
    try:
        ratio = float(usage.get("mask_usage_ratio") or 0.0)
    except (TypeError, ValueError):
        return False
    return ratio >= masks_mod.STYLE_MASK_USAGE_MIN_RATIO


def _write_xmp_one(
    item: ScanItem,
    params: dict,
    curves: dict,
    *,
    exiftool: ExiftoolRunner,
    opts: OutputOptions,
    masks: list[dict] | None = None,
    replace_mask_sync_ids: frozenset[str] | None = None,
    known_mask_sync_ids: frozenset[str] | None = None,
) -> XmpOutcome:
    """按文件类型选择写出策略（硬约束 #1 的 DNG 例外）。"""
    try:
        profile_value, profile_note = _resolve_camera_profile(item.path, exiftool, opts)
        # 镜头开关：逐图读这张照片**自己的**基线（侧车优先、crd: 兵底），
        # 只补缺失的开关，已有值一律不动（见 acb/raw/lens.py 的说明）。
        # 配置文件本身（LensProfileName/Filename/Digest/IsEmbedded）交给 ACR，绝不写。
        lens_baseline = lens.baseline_for_raw(item.path, exiftool)
        program_fields = lens_baseline.values
        # 两种情况都是 debug：`note()` 里已经写清了"用的是侧车基线还是默认值"，
        # 日志正文一样不代表信息丢了（原来的 if/else 两个分支体完全相同，
        # 那才是真的把 resolved 的信息丢掉了 —— 已合并成一条）。
        log.debug("%s %s", item.path.name, lens_baseline.note())

        # 局部调整（步骤 5）：几何是显示帧坐标，必须**逐图**读方向再换算。
        # 缺方向时 write_sidecar/render_xmp 会直接报错，不会静默写错位置。
        orientation_label: str | None = None
        if masks:
            photo = orientation.read_orientation(item.path, exiftool)
            orientation_label = photo.label
            log.info(
                T("%s 局部调整：%d 个蒙版，照片方向 %s（显示时旋转 %d°）"),
                item.path.name,
                len(masks),
                photo.label,
                photo.rotation,
            )
        if is_heif(item.path.name):
            return _write_heif_via_tiff(
                item,
                params,
                curves,
                exiftool=exiftool,
                masks=masks,
                program_fields=program_fields,
                orientation_label=orientation_label,
                replace_mask_sync_ids=replace_mask_sync_ids,
                known_mask_sync_ids=known_mask_sync_ids,
            )

        if uses_embedded_xmp(item.path.name):
            if is_dng(item.path.name) and opts.dng_sidecar:
                # 用户坚持要旁侧文件：写 foo.dng.xmp，并附显式警告。
                # ⚠ 这里必须和其它两条分支一样把 masks / orientation 传进去，
                #   否则勾了「DNG 也生成旁侧文件」之后，AI 给出的局部调整会
                #   **既不写进去、也不报错**（静默少写一类参数，比报错难查得多）。
                target = xmp_writer.embedded_sidecar_path_for(item.path)
                created_new = not target.exists()
                data, applied, skipped, warnings, mask_ids = xmp_writer.render_xmp(
                    params,
                    curves,
                    None,
                    profile_value,
                    masks=masks,
                    orientation=orientation_label,
                    program_fields=program_fields,
                    replace_mask_sync_ids=replace_mask_sync_ids,
                    known_mask_sync_ids=known_mask_sync_ids,
                )
                target.write_bytes(data)
                warnings.extend(
                    xmp_writer.validate_dng_policy(item.path, force_sidecar_for_dng=True)
                )
                return XmpOutcome(
                    ok=True,
                    target=target,
                    mode="sidecar-for-dng",
                    warnings=warnings,
                    camera_profile=profile_value,
                    camera_profile_note=profile_note,
                    created_new=created_new,
                    acr_target=item.path,
                    mask_sync_ids=tuple(mask_ids),
                )

            result = xmp_dng.write_embedded(
                item.path,
                params,
                curves,
                exiftool=exiftool,
                allow_sidecar_fallback=False,
                camera_profile=profile_value,
                profile_note=profile_note,
                program_fields=program_fields,
                masks=masks,
                orientation=orientation_label,
                replace_mask_sync_ids=replace_mask_sync_ids,
                known_mask_sync_ids=known_mask_sync_ids,
            )
            return XmpOutcome(
                ok=True,
                target=result.target,
                mode=result.mode,
                warnings=result.warnings,
                camera_profile=result.camera_profile,
                camera_profile_note=result.camera_profile_note,
                created_new=result.created_new,
                acr_target=item.path,
                mask_sync_ids=tuple(result.mask_sync_ids),
            )

        result = xmp_writer.write_sidecar(
            item.path,
            params,
            curves,
            camera_profile=profile_value,
            profile_note=profile_note,
            program_fields=program_fields,
            masks=masks,
            orientation=orientation_label,
            replace_mask_sync_ids=replace_mask_sync_ids,
            known_mask_sync_ids=known_mask_sync_ids,
        )
        # 侧车模式也附上 DNG 策略说明（对 DNG 走侧车分支时已处理，这里不会触发）。
        return XmpOutcome(
            ok=True,
            target=result.target,
            mode=result.mode,
            warnings=result.warnings,
            camera_profile=result.camera_profile,
            camera_profile_note=result.camera_profile_note,
            created_new=result.created_new,
            acr_target=item.path,
            mask_sync_ids=tuple(result.mask_sync_ids),
        )

    except Exception as exc:
        return XmpOutcome(ok=False, error=str(exc))


def _write_heif_via_tiff(
    item: ScanItem,
    params: dict,
    curves: dict,
    *,
    exiftool: ExiftoolRunner,
    masks: list[dict] | None,
    program_fields: dict | None,
    orientation_label: str | None,
    replace_mask_sync_ids: frozenset[str] | None = None,
    known_mask_sync_ids: frozenset[str] | None = None,
) -> XmpOutcome:
    """HEIF 的出片通道：解码成 TIFF（8 位，原因见 decode.write_intermediate_tiff），把设置写进那份 TIFF。

    【为什么必须这么办 —— 2026-09-26 真机实测】
    Photoshop 对 HEIC 里的相机设置**完全不理会**：内嵌 XMP、`<名>.xmp`、
    `<名>.heic.xmp` 三种写法都试过，导出亮度一模一样（mean 64.5）；
    以智能对象打开也拿不到 ACR 通道；佳能的 `.hif` 更是直接打不开。
    而 TIFF 的 `crs:` 设置实测会被 ACR 应用（120.2 → 168.6）。
    所以 HEIF 走：程序解码 → TIFF（带设置）→ Photoshop 出片。

    ⚠ 代价必须说清楚（已写进 warnings 与 README）：**原 HEIF 不会被改动**，
    出片的像素来自本程序的解码（已按 BT.2408 的 203 nit 标定到与 Photoshop
    的 HDR→SDR 观感一致，见 acb/raw/decode.py 的实测数据）。
    """
    target = _intermediate_tiff_path(item.path)
    try:
        raw_decode.write_intermediate_tiff(item.path, target)
    except raw_decode.DirectDecodeError as exc:
        return XmpOutcome(ok=False, error=str(exc))

    # 局部调整的坐标帧按**中转 TIFF** 算：它的像素已经正过、方向标签是 1，
    # 而原 HIF 的 EXIF 是 8 —— 拿原文件算会把蒙版写到错位置。
    if masks:
        try:
            photo = orientation.read_orientation(target, exiftool)
        except Exception as exc:  # noqa: BLE001 - 拿不到方向就写不了蒙版，如实报错
            return XmpOutcome(ok=False, error=str(exc))
        orientation_label = photo.label
        log.info(
            T("%s 局部调整：%d 个蒙版，中转 TIFF 方向 %s（显示时旋转 %d°）"),
            item.path.name, len(masks), photo.label, photo.rotation,
        )

    try:
        result = xmp_dng.write_embedded(
            target,
            params,
            curves,
            exiftool=exiftool,
            allow_sidecar_fallback=False,
            # 渲染后的 TIFF 没有"相机配置文件"这个概念（ACR 的配置列表只对 RAW 有意义），
            # 写进去反而可能让 ACR 报错，所以不写、也不当作"配置缺失"去提醒。
            camera_profile=None,
            profile_note="",
            program_fields=program_fields,
            masks=masks,
            orientation=orientation_label,
            policy_warning=False,
            # 中转 TIFF 每轮都是从 HEIF 重新解码的（内容全新），
            # 但文件路径固定 → 登记表键是原 HEIF 的键。传下去无害且一致。
            replace_mask_sync_ids=replace_mask_sync_ids,
            known_mask_sync_ids=known_mask_sync_ids,
            # 中转件是我们刚解码出来的临时文件（在 state\_tmp_xmp\ 里，退出时清掉），
            # 给它留 _original 副本纯属浪费磁盘：8192×5464 的 8 位 TIFF 约 130 MB，
            # 一批两百张就是白占 26 GB。**原 HEIF 从始至终没被改动**，
            # 所以这里也没有"少了一层保护"的问题。
            keep_backup=False,
        )
    except Exception as exc:  # noqa: BLE001
        return XmpOutcome(ok=False, error=str(exc))

    warnings = list(result.warnings)
    warnings.append(
        TF('{0}：HEIF 的相机设置无法被 Photoshop/ACR 直接读取（实测），'
           '本次已解码为 TIFF 中转件并通过它出片；原文件未被改动。',
           item.path.name)
    )
    log.info(T("%s：HEIF → 中转 TIFF %s（已写入 %d 个字段）"),
             item.path.name, target.name, len(result.applied_fields))
    return XmpOutcome(
        ok=True,
        target=result.target,
        mode="heif-tiff",
        warnings=warnings,
        camera_profile=None,
        camera_profile_note=T("中转 TIFF 不写相机配置（ACR 的配置列表只对 RAW 有意义）"),
        created_new=False,
        acr_target=target,
        mask_sync_ids=tuple(result.mask_sync_ids),
    )


# ============================================================================
# 主流程
# ============================================================================

def resolve_effective_prompt(
    typed_prompt: str | None,
    style_data: dict[str, Any] | None,
) -> tuple[str, str]:
    """决定本次请求用哪一条提示词，返回 (提示词, 给用户看的说明)。

    【为什么必须抽成纯函数】用户 2026-09 的真实缺陷就出在这里：
      旧代码写的是 `prompt=opts.prompt or DEFAULT_PROMPT`，
      于是"选了风格但没填提示词"时，DEFAULT_PROMPT 里那句
      「不做 HSL 分区偏移、不做分离色调与颜色分级（保持 0）」
      与风格块里的 21 条规则正面矛盾 —— 结果只剩基本面板被改，
      用户评价是「只调整了亮和颜色」。
    抽出来后可以直接断言"有风格时绝不出现那句禁止"。
    """
    typed = (typed_prompt or "").strip()
    if typed:
        if style_data and not is_autopilot_style(style_data):
            # 两句提示词打架时谁说了算，**必须写进请求里**：
            # 风格块写着「风格规则（请逐条遵守）」，而它排在提示词**前面** ——
            # 不给显式优先级，模型会两边各听一半（表现为"只挑了不冲突的那几条做"）。
            # 「AI自主决策」不加这句：它的块里写着"没有需要遵守的个人风格偏好"，
            # 再加一句"以提示词为准"只会让人误以为机器字段禁令也可以被顶掉。
            typed += T("\n（本批提示词与上述风格规则冲突时，以本批提示词为准）")
            return typed, T("使用你填写的提示词（与所选风格叠加，冲突以提示词为准）")
        if style_data:
            return typed, T("使用你填写的提示词（风格为「AI自主决策」，不加偏好限制）")
        return typed, T("使用你填写的提示词（未选风格，按中性约束处理）")
    if is_autopilot_style(style_data):
        # 内置「AI自主决策」：没有偏好规则可遵守，用专门那一条，
        # 而不是 STYLE_EXECUTION_PROMPT（它假定存在一份"逐条执行"的风格规则）。
        return (
            AUTOPILOT_PROMPT,
            T("已选择内置「AI自主决策」：不套用任何个人风格，由模型按每张图自行判断"
            "（字段白名单、取值范围与机器字段禁令照旧生效）"),
        )
    if style_data:
        return (
            STYLE_EXECUTION_PROMPT,
            T("未填写提示词：按所选风格的规则执行"
            "（含 HSL / 分离色调 / 颜色分级等特征面板）"),
        )
    return (
        DEFAULT_PROMPT,
        f"未填写提示词，已启用内置默认提示词：{DEFAULT_PROMPT[:60]}…",
    )


def run_output_mode(
    opts: OutputOptions,
    *,
    adapter: AdapterLike,
    exiftool: ExiftoolRunner,
    job_state: JobState,
    callbacks: JobCallbacks,
    cancel_event: threading.Event | None = None,
) -> OutputResult:
    """执行输出模式。返回汇总结果。"""
    result = OutputResult()
    stopped = False

    # --- 阶段 1：扫描 -------------------------------------------------------
    callbacks.stage(T("扫描文件"))
    files = iter_raw_files(opts.sources, recursive=opts.recursive)
    items = build_scan_items(files)
    result.total = len(items)

    if not items:
        callbacks.warn(T("未找到任何白名单内的 RAW 文件。支持的格式：见界面提示或 README。"))
        return result

    callbacks.info(TF('扫描到 {0} 个 RAW 文件。', len(items)))

    # dry-run：只跑前 N 张（硬约束 #7）
    if opts.dry_run:
        items = items[:DRY_RUN_LIMIT]
        result.total = len(items)
        callbacks.warn(
            TF('【离线调试（演练）】只处理前 {0} 张，用于验证链路。注意：演练仍会写出真实 XMP（除非加 --dry-run-no-xmp），请确认已在副本上操作。', len(items))
        )

    # --- 断点筛选 -----------------------------------------------------------
    pending, skipped = job_state.select_pending(
        items, resume=opts.resume, only_failed=opts.only_failed
    )
    result.skipped = len(skipped)
    for item, reason in skipped:
        callbacks.info(TF('跳过 {0}：{1}', item.path.name, reason))
        callbacks.item_status(item.path.name, TF('跳过（{0}）', reason[:40]))

    # 「永久跳过」必须**点名 + 说清怎么恢复**，而不只是一个数字。
    #
    # 【为什么单独拎出来说 —— 真实反馈】用户「我又进行了一轮测试，发现 DNG 文件
    # 依然没有被处理」：那 3 个 DNG 在上一轮因 exiftool 写回失败被记成永久跳过
    # （键 = 大小|mtime|文件名，与目录无关），于是**任何模式都不会再碰它们**，
    # 而当时的输出只有一句“跳过 3 张（共 21 张）”——没有名字、没有原因、没有出路。
    # 这类“档案永远记着”的机制必须自带一条明确的恢复路径，否则用户
    # 只能把“少处理了几张”当成软件坏了。
    permanent = [(item, reason) for item, reason in skipped
                 if job_state.is_permanently_skipped(item.key)]
    if permanent:
        result.permanent_skips = [item.path.name for item, _ in permanent]
        names = "、".join(result.permanent_skips[:8])
        if len(result.permanent_skips) > 8:
            names += f" 等 {len(result.permanent_skips)} 个"
        notice = (
            f"⚠ 有 {len(permanent)} 个文件因「此前累计失败达上限」被略过，"
            f"本次及以后任何模式都不会再处理：{names}。\n"
            f"  上一次失败的原因（以第一个为例）：{permanent[0][1][:400]}\n"
            "  若该问题已解决（改了配置 / 升级了版本），点菜单「日志 → 清除失败记录」"
            "后重新运行，即可重试这几个文件。"
        )
        callbacks.warn(notice)
        # 再显式写一条文件日志：callbacks 走的是界面桥，而这条必须落盘 ——
        # 用户回看日志时，这是「为什么少了几张」的唯一证据。
        log.warning(notice)
        result.warnings.append(notice)

    if not pending:
        callbacks.info(T("没有需要处理的文件（全部已完成或已跳过）。"))
        result.stopped = False
        return result

    # --- 请求预算（硬约束 #18）---------------------------------------------
    limit = opts.max_requests if opts.max_requests else len(pending) * BUDGET_MULTIPLIER
    from ..ai.client import RequestBudget, is_account_level_error

    budget = RequestBudget(
        limit=limit,
        hint=f"= 待处理 {len(pending)} 张 × {BUDGET_MULTIPLIER}（含重试）",
    )
    adapter.client.budget = budget
    callbacks.info(
        TF('请求预算上限 {0} 次（= 待处理 {1} 张 × {2}，含重试）。', limit, len(pending), BUDGET_MULTIPLIER)
    )

    # --- 阶段 2–3：预览 -----------------------------------------------------
    callbacks.stage(T("提取预览并压缩"))
    cache = ThumbCache(enabled=opts.use_cache)
    outcomes: dict[str, PreviewOutcome] = {}
    # 进度总步数 = 文件处理步数 + 收尾预留步数。
    # 分析阶段的步数按**唯一键**计，因为内容相同的重复文件只会被分析一次
    # （见 _run_analysis 的去重逻辑），否则进度条会永远差几个百分点。
    #
    # 【收尾阶段必须留出可见区间】按比例留而不是留固定几步，
    # 原因见 constants.PROGRESS_FILE_STAGE_PERCENT 的注释。
    unique_keys = {item.key for item in pending}
    file_steps = len(pending) + len(unique_keys) + len(pending)
    total_steps = max(
        file_steps + PROGRESS_POST_STEPS_MIN,
        int(round(file_steps * 100.0 / PROGRESS_FILE_STAGE_PERCENT)),
    )
    # 收尾第一步（脚本生成完）推进一半，让"Photoshop 导出中"停在中间位置，
    # 而不是一上来就贴近 100%。
    post_step_chunk = max(1, (total_steps - file_steps) // 2)
    step_done = 0
    callbacks.progress(0, total_steps)

    workers = max(1, min(opts.workers, len(pending)))
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acb-preview") as pool:
            futures = {
                pool.submit(
                    _prepare_one, item, exiftool=exiftool, cache=cache, opts=opts
                ): item
                for item in pending
            }
            for future in as_completed(futures):
                callbacks.check_stop()
                item = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = PreviewOutcome(item=item, error=f"预览处理异常：{exc}")
                outcomes[item.key] = outcome
                step_done += 1
                callbacks.progress(step_done, total_steps)

                if outcome.error:
                    attempts = job_state.mark_failed(item, outcome.error)
                    callbacks.error(TF('{0} 预览失败（累计 {1} 次）：{2}', item.path.name, attempts, outcome.error))
                    callbacks.item_status(item.path.name, T("预览失败"))
                else:
                    tag = T("缓存命中") if outcome.from_cache else outcome.source
                    callbacks.item_status(item.path.name, TF('预览就绪（{0}）', tag))
                    for warning in outcome.warnings:
                        callbacks.warn(TF('{0}：{1}', item.path.name, warning))
    except StopRequestedError:
        stopped = True
        callbacks.warn(T("已停止：预览阶段中断，已完成的处理结果已保留。"))

    ready = [o for o in outcomes.values() if o.jpeg is not None and not o.error]

    # --- 批内统计（问题 e 的自动一致性校正模式）----------------------------
    stats_list = [o.stats for o in ready if o.stats is not None]
    medians = batch_medians(stats_list) if stats_list else None

    auto_match = not (opts.prompt or "").strip() and not opts.style_data
    if auto_match:
        callbacks.warn(T(WARN_AUTO_MATCH))
        if medians:
            callbacks.info(
                T("批内统计中位数：亮度 {luma:.1f}、对比度 {contrast:.1f}、"
                "估算色温 {cct:.0f}K、饱和度 {sat:.1f}").format(**medians)
            )
        else:
            callbacks.warn(T("无法计算批内统计（缺少可用预览），将只使用保守的默认约束。"))

    style_name = opts.style_name
    if auto_match:
        # 未选风格时回退到内置中性基准（问题 e 的第 1 条）。
        from .style_profile import ensure_seed_styles, load_style_profile

        ensure_seed_styles()
        fallback = load_style_profile(DEFAULT_STYLE_NAME)
        if fallback:
            style_name = DEFAULT_STYLE_NAME
            callbacks.info(TF('未选择风格，已使用内置「{0}」中性基准。', DEFAULT_STYLE_NAME))
    style_block = render_style_block(opts.style_data, style_name)

    # 提示词优先级（用户 2026-09 反馈"训练出来的风格根本没生效"的根因所在）：
    #   1) 用户填了提示词 → 用他的；风格规则同时生效（两者叠加）；
    #   2) 没填但**选了风格** → 用 STYLE_EXECUTION_PROMPT：
    #      要求按风格逐条执行，包括 HSL / 分离色调 / 颜色分级；
    #      **绝不能**再塞 DEFAULT_PROMPT（它写着"不做 HSL/颜色分级"，与风格互抵）；
    #   3) 两样都没有 → 才是 DEFAULT_PROMPT 的保守全局校正。
    # 判断逻辑本身在 resolve_effective_prompt 里（纯函数，自检直接断言它）。
    effective_prompt, prompt_note = resolve_effective_prompt(opts.prompt, opts.style_data)
    callbacks.info(prompt_note)

    # --- 阶段 4：AI 分析 ----------------------------------------------------
    if ready and not stopped:
        callbacks.stage(T("AI 分析"))
        analysis = _run_analysis(
            ready,
            adapter=adapter,
            style_block=style_block,
            prompt=effective_prompt,
            medians=medians,
            auto_match=auto_match,
            workers=opts.workers,
            job_state=job_state,
            callbacks=callbacks,
            step_done=step_done,
            total_steps=total_steps,
            cancel_event=cancel_event,
        )
        step_done = analysis["step_done"]
        results_map: dict[str, ItemResult] = analysis["results"]
        stats_by_key: dict[str, Any] = analysis.get("stats", {})
        stopped = stopped or analysis["stopped"]
    else:
        results_map = {}
        stats_by_key = {}

    # --- 阶段 5：写 XMP -----------------------------------------------------
    if results_map and not stopped:
        callbacks.stage(T("写出 XMP"))
    elif results_map and stopped:
        # 停止后**仍要写**已经分析完成的那几张：那些请求已经花过钱，
        # 写盘是本地动作；未分析的文件（results_map 里根本没有）本次不动。
        callbacks.warn(
            TF('已停止：仍会把**已经分析完成**的 {0} 张写出 XMP（这批请求已经花过钱，丢掉才是浪费）；未分析的文件本次不动。', len(results_map))
        )

    # 面板分布：让"只调了亮和颜色"这种事在日志里一眼可见，
    # 而不需要等用户喊"风格没生效"再去翻 XMP。
    # 有图"一条参数都没有"时必须出声：那既可能是"本来就调得很好的图"（合法答案），
    # 也可能是模型其实没看图片/没执行风格规则（静默的质量失败）。
    # 两者在界面上长得一样，所以只能由程序把这种可能性说出来。
    no_change = [r for r in results_map.values() if r.ok and not r.params and not r.curves]
    if no_change:
        callbacks.warn(
            TF('{0} 张图的响应里没有任何参数（模型表示「无需调整」）。如果这些图本来就该调整，请检查模型是否真的看到了图片、以及是否为某个不具备视觉能力的模型（模型下拉悬停可看能力；日志里每张图也各有一条警告）。', len(no_change))
        )

    written_params = [r.params for r in results_map.values() if r.ok and r.params]
    if written_params:
        panels = _panel_usage(written_params)
        callbacks.info(
            f"本次写入的面板分布（风格：{style_name or T('未指定')}；图数/字段数）："
            + "、".join(f"{group} {files}/{fields}" for group, files, fields in panels)
        )
        if opts.style_data:
            zone_groups = {"HSL", T("分离色调"), T("颜色分级"), T("相机校准")}
            if not any(group in zone_groups for group, _f, _c in panels):
                callbacks.warn(
                    T("所选风格要求的分区调整（HSL / 分离色调 / 颜色分级 / 相机校准）"
                    "一次都没用上——请检查模型是否真的执行了风格规则（这次结果可能仍不像你的风格）。")
                )

    # --- 照抄检测（程序侧护栏 · 步骤 3）--------------------------------------
    # 用户实测：同风格跑一批图，18 份侧车里的值全部等于训练统计的中位数/均值。
    # 提示词里已写了「禁止照抄」，但那是软约束；这里做一遍可观测的硬检查。
    copying: list[str] = []
    if opts.style_data:
        copying = guardrails.detect_copying(
            [r.params for r in results_map.values() if r.ok and r.params],
            opts.style_data,
        )
        if copying:
            callbacks.warn(
                f"照抄告警：{len(copying)} 个字段在多张图上取值完全相同、且正好等于训练统计的中位数/均值。"
                "这通常意味着模型把历史统计值当成了每张图的目标值（这正是要修的毛病）："
                + "；".join(copying[:3])
                + ("；…" if len(copying) > 3 else "")
            )
            for line in copying:
                log.warning(T("照抄告警：%s"), line)

    export_plan = build_export_plan(pending, opts)
    manifest_items: list[dict[str, Any]] = []
    new_profiles: list[str] = []
    missing_profile_notes: list[str] = []
    # 每张图被护栏改动的情况（文件名 → 说明），写进该图的「画像」日志
    guard_notes: dict[str, list[str]] = {}
    # 局部调整（步骤 5）的计数：真正写入多少张、被风格门槛拦下多少张、
    # 因文件里已有别人的蒙版而跳过多少张
    masks_written = 0
    mask_dropped_by_style = 0
    masks_skipped_existing = 0
    # “风格本来就会用蒙版、但模型这次一个都没给”的计数。
    # 为什么要单独统计：实测踩过 —— 系统提示词里一句旧话把 masks 一律禁掉了，
    # 结果一连几轮“一个蒙版也没有”，而日志里一行都不提，用户只能看到结果不对。
    style_expects_masks = _style_uses_masks(opts.style_data)
    mask_expected_but_missing = 0
    # 白平衡被护栏动过的张数（夹紧或整组跳过）。人像偏色那次反馈里，
    # 用户最需要知道的就是"这 5 张到底有没有被程序改过白平衡"。
    wb_adjusted = 0
    wb_skipped = 0
    # 写 XMP 失败的文件（名字与原因）。用它的两个理由：
    #   1) 批次末尾汇总一条日志——旧版只往界面状态栏丢一句，日志里**一行都没有**，
    #      用户回头"看日志"时根本看不到（真实反馈：DNG 一张都没写进去）。
    #   2) 失败要计入 result.failed，否则"全部完成"的汇总会把失败藏起来。
    xmp_failures: list[tuple[str, str]] = []

    for item in pending:
        item_result = results_map.get(item.key)
        if item_result is None:
            continue

        if not item_result.ok:
            # 账户级错误（额度/计费/密钥）不算进“永久跳过”的阈值：
            # 这类失败与这张图无关，用户充值/改完配置后一定要能重跑（见 job.mark_failed）。
            error_text = item_result.error or T("未知错误")
            attempts = job_state.mark_failed(
                item, error_text, counts_toward_skip=not is_account_level_error(error_text)
            )
            callbacks.error(TF('{0} 分析失败（累计 {1} 次）：{2}', item.path.name, attempts, error_text))
            callbacks.item_status(item.path.name, T("分析失败"))
            if is_account_level_error(error_text):
                callbacks.warn(
                    TF('{0} 的失败是**账户侧**的问题（额度/计费/密钥），不计入「失败 2 次即永久跳过」——等你充值或改完配置后，直接重跑或点「只跑失败」就能再处理它。', item.path.name)
                )
            continue

        # --- 写盘前的程序侧护栏（步骤 3）-----------------------------------
        # 1) 极端值必须有依据：超过该字段的"无规则上限"、风格规则里又没提到它，就夹回
        #    （上限会按训练统计的置信度自动收紧）；
        # 2) 分组预算：饱和度类的加权总量、镜头晕影 + 裁剪后晕影的合计暗角量。
        clamped, clamp_notes = guardrails.clamp_extremes(item_result.params, opts.style_data)
        budgeted, budget_notes = guardrails.apply_group_budgets(clamped)
        # 3) 白平衡必须相对**这张图自己的 as-shot**：crs:Temperature 是绝对开尔文，
        #    而模型看不到 as-shot（只看到缩略图估算色温 + 风格文本里的绝对区间）。
        #    实测 DSC03646.ARW 机内 4600K 被写成 6100K，人像肤色整片偏黄（见 guardrails 注释）。
        had_wb = "Temperature" in budgeted or "Tint" in budgeted
        wb_ref = wb_registry.effective_as_shot(item.key, item.path, exiftool)
        item_stats = stats_by_key.get(item.key)
        wb_fixed, wb_notes = guardrails.clamp_white_balance(
            budgeted,
            as_shot_kelvin=wb_ref.kelvin,
            as_shot_tint=wb_ref.tint,
            style=opts.style_data,
            preview_cct=item_stats.estimated_cct if item_stats else None,
        )
        if wb_notes:
            wb_adjusted += 1
            if had_wb and "Temperature" not in wb_fixed and "Tint" not in wb_fixed:
                wb_skipped += 1
        notes = clamp_notes + budget_notes + wb_notes
        if notes:
            item_result.params = wb_fixed
            guard_notes[item.path.name] = notes
            for note in notes:
                item_result.warnings.append(f"护栏：{note}")
                log.info(T("%s 护栏：%s"), item.path.name, note)

        # --- 局部调整的准入（步骤 5）-----------------------------------------
        # 只有风格训练素材里"带蒙版样本占比"达到阈值，才允许给图加局部调整。
        # 依据只能是用户自己的素材（见 _style_uses_masks 的说明）。
        mask_specs = list(item_result.masks or [])
        if mask_specs and not style_expects_masks:
            log.info(
                T("%s：风格不做局部调整（训练素材里带蒙版比例未达门槛），"
                "已丢弃模型提出的 %d 个蒙版"),
                item.path.name,
                len(mask_specs),
            )
            mask_specs = []
            mask_dropped_by_style += 1
        elif mask_specs:
            # 范围复核：用**这套风格自己**训练出来的两层范围
            #（软=实测习惯，硬=留余量后的裁决线；拿不到风格数据就用兜底表）。
            mask_specs, range_notes = masks_mod.clamp_local_to_ranges(
                mask_specs, masks_mod.local_range_layers(opts.style_data)
            )
            for note in range_notes:
                item_result.warnings.append(f"局部调整：{note}")
                log.info(T("%s 局部调整：%s"), item.path.name, note)

        # dry-run-no-xmp：只验证 AI 链路，不碰用户文件。
        if opts.dry_run and opts.dry_run_no_xmp:
            callbacks.info(TF('{0}：离线调试（--dry-run-no-xmp）已跳过 XMP 写出。', item.path.name))
            job_state.mark_done(item, item_result)
        else:
            # 【刻意不在这里再查停止旗标】
            # 停止的含义是“别再花钱发请求”；而这一张的分析**已经发过请求、钱已经花了**，
            # 写 XMP 是纯本地动作（毫秒级）。以前这里每张都查一次停止，
            # 用户在分析阶段点停止后，已经拿到的结果一张都不会落盘 ——
            # 既白花了钱，又与“已完成的处理结果会保留”（硬约束 #8）相矛盾。
            write_outcome = _write_xmp_one(
                item,
                item_result.params,
                item_result.curves,
                exiftool=exiftool,
                opts=opts,
                masks=mask_specs,
                # 只允许替换**我们上一轮自己写的**蒙版：用户手工画的那些
                # 没有 ID 记录，`masks.remove_corrections` 碰不到它们。
                # 两条路一起走：本文件的记录（快） + 全库 ID（TIFF/DNG 写完
                # 断点键就变了，按键查会查不到自己写过的蒙版 —— 见 mask_registry.all_ids）。
                replace_mask_sync_ids=mask_registry.ids_for(item.key),
                known_mask_sync_ids=mask_registry.all_ids(),
            )
            if not write_outcome.ok:
                attempts = job_state.mark_failed(item, write_outcome.error)
                reason = write_outcome.error or T("未知错误")
                # 同时进文件日志与界面：写不出来是**必须**能事后追查到的事。
                log.error(T("%s 写 XMP 失败（累计 %d 次）：%s"), item.path.name, attempts, reason)
                callbacks.error(TF('{0} 写 XMP 失败（累计 {1} 次）：{2}', item.path.name, attempts, reason))
                callbacks.item_status(item.path.name, T("写 XMP 失败"))
                # 标记到结果上：否则汇总里的"失败 N 个"只统计分析失败。
                item_result.error = reason
                xmp_failures.append((item.path.name, reason))
                continue
            for warning in write_outcome.warnings:
                callbacks.warn(TF('{0}：{1}', item.path.name, warning))
                result.warnings.append(f"{item.path.name}：{warning}")
            # 相机配置：写进去了就汇报；"新建但没确定"才是要提醒的情况。
            if write_outcome.camera_profile:
                new_profiles.append(write_outcome.camera_profile)
            elif write_outcome.created_new:
                missing_profile_notes.append(
                    f"{item.path.name}：{write_outcome.camera_profile_note or T('未能确定')}"
                )
            item_result.xmp_target = write_outcome.target
            item_result.xmp_mode = write_outcome.mode
            # 交给 Photoshop 的那份（HEIF 是中转 TIFF，其余就是原文件）。
            item_result.acr_target = write_outcome.acr_target or item.path
            job_state.mark_done(item, item_result)
            # 写盘**成功**之后才登记基准：写失败时文件没变，
            # 提前登记会让下一轮把我们没写进去的值误当成“文件里的现状”。
            wb_registry.record_written(item.key, wb_ref, item_result.params)
            # 同理登记蒙版 ID（没写蒙版就是空，不会覆盖已有记录）。
            mask_registry.record(item.key, write_outcome.mask_sync_ids)
            # 计数要按**真的写进去了**算（write_outcome.mask_sync_ids 非空）。
            # 以前看的是 mask_specs —— 于是"文件里已有你自己的蒙版、本次整组跳过"
            # 也会被报成"写入了局部调整"（实测 2026-09-29：摘要说 4 张，
            # 实际只有 1 张落盘），用户一看就对不上。
            if write_outcome.mask_sync_ids:
                masks_written += 1
            elif mask_specs:
                masks_skipped_existing += 1
            elif style_expects_masks:
                mask_expected_but_missing += 1
            # 【可观测性 · 步骤 0】每张图一条"画像"日志：风格 / 用到的面板 / 参数个数 / 曲线。
            # 目的是让"风格没生效"这类问题能直接从日志定位，而不用去翻 XMP。
            # 后续步骤会往这一行里继续加：维度强度（7 档）、场景指标、命中的规则、蒙版。
            log.info(
                T("[画像] %s | 风格=%s | 面板=%s | 参数=%d%s%s%s"),
                item.path.name,
                style_name or T("（未指定）"),
                "、".join(_panel_names_for(item_result.params)) or T("无"),
                len(item_result.params),
                f" | 曲线 {len(item_result.curves)} 条" if item_result.curves else "",
                f" | 局部调整 {len(mask_specs)} 个（{'、'.join(str(m.get('name')) for m in mask_specs)}）"
                if mask_specs else "",
                f" | 护栏：{'；'.join(guard_notes[item.path.name])}" if item.path.name in guard_notes else "",
            )
            callbacks.info(
                TF('{0}：已写出 {1} XMP（{2} 个参数）→ {3}', item.path.name, write_outcome.mode, len(item_result.params), write_outcome.target.name if write_outcome.target else '?')
            )
            callbacks.item_status(item.path.name, T("已完成"))

        step_done += 1
        callbacks.progress(step_done, total_steps)

        target = export_plan.get(str(item.path))
        if target is not None:
            manifest_items.append(
                {
                    # raw 字段的语义是"交给 Photoshop 打开的那个文件"：
                    # RAW/DNG/TIFF 就是它自己；HEIF 是我们的 TIFF 中转件
                    #（Photoshop 不读 HEIC 里的设置，见 _write_heif_via_tiff）。
                    "raw": str(item_result.acr_target or item.path),
                    "out": str(target),
                    "xmp": str(item_result.xmp_target or ""),
                }
            )

    # --- 白平衡护栏（以每张图的 as-shot 为基准）的汇总 ---------------------
    # 为什么汇总要单独说：路径上的每一次"白平衡被改"都是用户面子上能看到的偏色与否，
    # 不能只活在逐图日志里。
    if wb_adjusted:
        notice = TF(
            '白平衡：{0} 张图的 Temperature/Tint 被程序侧护栏动过（基准是每张图自己的 as-shot ＋ 这套风格训练出的偏移范围）',
            wb_adjusted,
        )
        if wb_skipped:
            notice += TF(
                '；其中 {0} 张读不到 as-shot 色温，已整组跳过白平衡（绝对色温猜错就是整张偏色，所以宁可不改）',
                wb_skipped,
            )
        notice += T('。逐张细节见「画像」日志里的「护栏」段。')
        log.info("%s", notice)
        callbacks.info(notice)

    # --- 局部调整（步骤 5）的汇总 -------------------------------------------
    if masks_written or mask_dropped_by_style or masks_skipped_existing:
        pieces: list[str] = []
        if masks_written:
            pieces.append(f"{masks_written} 张图写入了局部调整")
        if mask_dropped_by_style:
            pieces.append(
                f"{mask_dropped_by_style} 张图被风格门槛拦下"
                "（这套风格的训练素材里很少用蒙版，见 STYLE_MASK_USAGE_MIN_RATIO）"
            )
        if masks_skipped_existing:
            pieces.append(
                f"{masks_skipped_existing} 张因为文件里已经有**不是本程序写的**蒙版组而跳过"
                "（在 ACR 里删掉那一组再重跑，本程序就能接管）"
            )
        callbacks.info(T("局部调整：") + "；".join(pieces) + "。")
    elif style_expects_masks and mask_expected_but_missing:
        ratio = 0.0
        usage = (opts.style_data or {}).get("mask_usage")
        if isinstance(usage, dict):
            try:
                ratio = float(usage.get("mask_usage_ratio") or 0.0)
            except (TypeError, ValueError):
                ratio = 0.0
        notice = (
            f"局部调整：本风格 {ratio:.0%} 的训练照片带蒙版，但本批 {mask_expected_but_missing} 张"
            "都没有给出局部调整（模型这次没有返回 masks）。全局参数已照常写出，"
            "需要局部调整的话重跑这几张即可（模型每次给的蒙版不保证一样）。"
        )
        # 两处都发：callbacks 面向界面，logging 面向文件日志。
        # 只发 callbacks 的话，事后回看日志会发现"这轮一个蒙版都没有"这件事**没有任何记录**
        #（PERMANENT_SKIP 那次教训：管线消息必须落进文件日志）。
        log.warning("%s", notice)
        callbacks.warn(notice)

    # --- 写 XMP 失败的汇总（必须能被看见，不能只留在状态栏）-----------------
    if xmp_failures:
        embedded_failed = [name for name, _ in xmp_failures
                           if uses_embedded_xmp(name) or is_heif(name)]
        message = (
            f"有 {len(xmp_failures)} 个文件写 XMP 失败（例如：{xmp_failures[0][0]} — "
            f"{xmp_failures[0][1][:120]}）。这些文件本次**没有被调色**，"
            "也不会出现在 Photoshop 导出清单里。"
        )
        if embedded_failed:
            message += (
                f"其中 {len(embedded_failed)} 个是内嵌 XMP 格式（DNG/TIFF/HEIF）："
                "优先看日志里 exiftool 的 Error 行"
                "（若为 `[minor] Maker notes could not be parsed`，说明这个文件需要 -m 才能改写，"
                "属于已处理过的情况，请把该文件与日志一并反馈）。"
            )
        callbacks.warn(message)
        result.warnings.append(message)

    # --- 相机配置汇总（用户 2026-09 反馈：新建的 XMP 把相机配置弄丢了）---
    if new_profiles:
        callbacks.info(
            T("XMP 里的相机配置已按相机机内设置写好：")
            + "、".join(sorted(set(new_profiles)))
            + T("（ACR 打开后会直接使用它，不再是「Adobe 标准」）")
        )
    if missing_profile_notes:
        message = (
            f"有 {len(missing_profile_notes)} 个新建的 XMP 没能确定相机配置"
            f"（例如：{missing_profile_notes[0]}）——"
            "ACR 可能回落到「Adobe 标准」。如果你在 ACR 里用的是相机配置（如 Camera Portrait），"
            "请打开这些文件后在「配置文件」里选一次；之后本程序会保持你的选择。"
        )
        callbacks.warn(message)
        result.warnings.append(message)

    # --- 阶段 6：生成 Photoshop 脚本并按需执行 -----------------------------
    if manifest_items and not stopped:
        callbacks.stage(T("生成 Photoshop 脚本"))
        try:
            output_dir = _common_output_dir(manifest_items, opts)
            # 脚本产物放到软件数据目录下的运行目录里，不进出片目录：
            # 出片目录是交付目录，只该有 JPG（用户反馈过两次）。
            script_dir = jsx_render.script_dir_for(output_dir)
            plan = jsx_render.render_export_outputs(
                script_dir=script_dir,
                manifest_items=manifest_items,
                color_space=opts.color_space,
                quality_value=opts.ps_quality,
                export_format=opts.export_format,
                dry_run=opts.dry_run,
            )
            result.export_dir = output_dir
            result.export_format = plan.export_format
            result.script_dir = plan.script_dir
            result.jsx_path = plan.jsx_path
            result.manifest_path = plan.manifest_path
            result.bat_path = plan.bat_path
            result.ps_log_path = plan.ps_log_path
            callbacks.info(TF('已生成 Photoshop 脚本：{0}', plan.jsx_path))
            callbacks.info(TF('已生成导出清单：{0}（{1} 项）', plan.manifest_path, len(manifest_items)))
        except Exception as exc:
            callbacks.error(TF('生成 Photoshop 脚本失败：{0}', exc))
            plan = None
        # 无论成功与否都要推进进度：卡在 99% 比停在 100% 更让人困惑。
        step_done += post_step_chunk
        callbacks.progress(step_done, total_steps)

        if plan is not None and opts.run_photoshop:
            # Display P3：必须在**拉起 Photoshop 之前**确保 profile 已安装。
            # 为什么：Photoshop 的 `convertProfile()` 只认"已安装的 profile 名"，
            # 而且它只在**自己启动时**扫描色彩目录；本程序是自己 Dispatch PS 的，
            # 所以在这里装完，紧接着启动的 PS 就能扫到它 —— 用户不需要手动重启。
            # 不过屏幕上已经开着 Photoshop 的情况不罕见，所以下面还会再提示一次。
            p3_install_note = ""
            if icc.color_space_needs_display_p3(opts.color_space):
                _p3_path, p3_install_note = icc.install_display_p3_profile()
                log.info(T("Display P3：%s"), p3_install_note)
            callbacks.stage(T("调用 Photoshop 导出"))
            outcome = photoshop.run_export_script(
                plan.jsx_path,
                callbacks,
                result_path=plan.ps_result_path,
                log_path=plan.ps_log_path,
            )
            result.ps_status = outcome.status
            result.ps_ok = outcome.ok
            if outcome.ok:
                callbacks.info(outcome.status)
            else:
                callbacks.warn(outcome.status)
            # 色彩空间没转换成功：这时候**唯一**有用的建议是"重启 Photoshop 再导出"
            # （PS 只在启动时扫色彩目录）。只说"未安装？"会让人以为要自己去找 profile，
            # 而程序其实已经把自制的那份装好了。
            if outcome.color_space_failures and icc.color_space_needs_display_p3(opts.color_space):
                hint = TF(
                    '程序已把自制的 Display P3 等效 profile 装到 Adobe 的色彩目录（{0}）。'
                    'Photoshop 只在**启动时**扫描色彩目录：若它当时已经在运行，'
                    '请重启 Photoshop 后重新导出，色彩空间这次才会真正转换。',
                    p3_install_note or T("未安装成功，详见日志"),
                )
                log.warning("%s", hint)
                callbacks.warn(hint)
                result.warnings.append(hint)
        elif plan is not None:
            # 必须写进 ps_status，而不是只打一条日志：
            # 界面与命令行都靠它判断"是否需要用户手动补跑"，进而决定
            # 完成对话框里给不给「打开脚本目录」、退出时保不保留脚本目录。
            # 早期这里只 log 了一句、不写 ps_status，于是这条路径上
            # runs/<标识>/ 会被当成垃圾整目录删掉 —— 恰好把用户唯一的
            # 补救手段（run_export.bat）删了。
            result.ps_status = (
                T("已按要求跳过 Photoshop 调用"
                "（可手动运行 run_export.bat 完成导出）")
            )
            # 明确记成"没成功"：这时用户确实需要手动跑一次，
            # 所以脚本目录要留着、完成对话框要给指引。
            result.ps_ok = False
            callbacks.info(result.ps_status)
        # 注意：这里刻意**不**把进度补满。剩下的收尾步数由函数结尾的
        # progress(total_steps, total_steps) 补上，也就是说
        # 进度条满格 = "脚本执行 + 导出 + 汇总"真的全部结束了。

    # --- 汇总 ---------------------------------------------------------------
    stats = job_state.stats()
    result.done = min(len(results_map), sum(1 for r in results_map.values() if r.ok))
    result.failed = len([1 for r in results_map.values() if not r.ok])
    result.stopped = stopped
    result.usage = adapter.client.usage_summary()
    result.budget = f"{budget.describe()}（上限 {budget.limit}）"

    callbacks.stage(T("完成"))
    if stats["permanent_skip"]:
        callbacks.warn(
            TF('有 {0} 个文件累计失败达上限已永久跳过；修正原因后可用「清除失败记录」重置。', stats['permanent_skip'])
        )
    callbacks.progress(total_steps, total_steps)
    return result


def _common_output_dir(manifest_items: list[dict[str, Any]], opts: OutputOptions) -> Path:
    """挑一个"有代表性的"出片目录。

    两个用途：界面「打开输出目录」的默认目标；以及给运行标识做种子。
    多个源目录时取第一个——manifest 里每一项都带自己的绝对输出路径，
    所以这个选择**不影响任何一张图实际落在哪**。
    """
    for entry in manifest_items:
        parent = Path(entry["out"]).parent
        if parent.is_dir():
            return parent
    return Path(manifest_items[0]["out"]).parent


# ============================================================================
# AI 分析阶段（并发 + 停止 + 断点）
# ============================================================================

def _run_analysis(
    ready: list[PreviewOutcome],
    *,
    adapter: AdapterLike,
    style_block: str,
    prompt: str,
    medians: dict[str, float] | None,
    auto_match: bool,
    workers: int,
    job_state: JobState,
    callbacks: JobCallbacks,
    step_done: int,
    total_steps: int,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    """并发调用模型分析全部待处理图片。"""
    callbacks.info(TF('模型能力：{0}', adapter.describe()))

    sub = PreviewItem  # 局部别名，缩短下面构造语句

    preview_items: list[PreviewItem] = []
    outcomes_by_id: dict[str, PreviewOutcome] = {}
    for outcome in ready:
        # file_id 用断点键（永不含奇怪字符），而不是文件名——
        # 文件名可能含引号、换行、中文标点，放进 JSON/提示词容易出问题。
        file_id = outcome.item.key

        # 去重：内容完全相同的重复文件会共用同一个键（见 job.build_scan_items）。
        # 这里只把它们提交给模型一次，省一次请求；XMP 写出与导出仍会按
        # pending 列表逐文件进行，因此两个副本都会被正确产出。
        if file_id in outcomes_by_id:
            log.info(
                T("%s 与 %s 内容相同、共用分析结果，跳过重复的 API 请求。"),
                outcome.item.path.name,
                outcomes_by_id[file_id].item.path.name,
            )
            continue

        caption = outcome.stats.to_caption_text() if outcome.stats else None
        preview_items.append(
            sub(
                file_id=file_id,
                filename=outcome.item.path.name,
                path=outcome.item.path,
                preview_jpeg=outcome.jpeg or b"",
                preview_source=outcome.source,
                preview_low_confidence=outcome.source == "thumbnail",
                caption=caption if not adapter.supports_vision else None,
                # 逐图指标：视觉模型也发一份（量化锚点），目的是让模型按"这张图现在的样子"
                # 重新判断调整量，而不是套用风格统计的历史数值（步骤 3）。
                metrics=_metrics_line(outcome.stats, outcome.wb_text),
            )
        )
        outcomes_by_id[file_id] = outcome

    # 按配置的每请求图片数分组
    group_size = max(1, adapter.spec.images_per_request)
    groups = [preview_items[i : i + group_size] for i in range(0, len(preview_items), group_size)]

    callbacks.info(
        TF('开始分析 {0} 张图，分成 {1} 个请求组，并发 {2}。', len(preview_items), len(groups), workers)
    )

    results: dict[str, ItemResult] = {}
    stopped = False

    # 把「用户是否已点停止」交给适配器（内含客户端）。
    # 【为什么必须往下传 —— 用户真实反馈】他点暂停后，处理失败的几张**还在继续重试**
    # （他用 GLM 时发现的，GLM 是思考模型、单发就慢，叠加拆单重发后特别明显）。
    # 以前停止旗标只在下面的提交循环里看：那时所有组早已全部提交给线程池了，
    # 于是在飞的组会带着自己的重试、超时翻倍、降级重发一路跑完，队列里的组也会被逐个发出去。
    setter = getattr(adapter, "set_cancel_check", None)
    if callable(setter):
        setter(callbacks.should_stop)
    # 并发意图同样交给适配器：让端点闸门能在限流/超时时**自动降档**
    #（用户实测：并发 4 时始终会零星失败，降到 2/1 后消失）。
    concurrency_setter = getattr(adapter, "set_concurrency_hint", None)
    if callable(concurrency_setter):
        concurrency_setter(workers)

    # 【为什么不把 groups 一次全丢进线程池】改成"滑动窗口"：在飞最多 workers 组，
    # 每收完一组才决定要不要补下一组。这样停止之后**不可能**再有组发出去 ——
    # 队列里不存在"等着被发出的组"。以前一次全提交，停止只能拦住"提交"这个动作，
    # 拦不住已经在队列里的那些（用户看到的就是"点了暂停还在继续发请求"）。
    pending_groups: list[list[PreviewItem]] = list(groups)
    stop_announced = False

    def _announce_stop() -> None:
        """停止：喊一次话，并把"待提交队列"清空（它们没有被提交，因此不会再发）。"""
        nonlocal stopped, stop_announced
        stopped = True
        if stop_announced:
            return
        stop_announced = True
        leftover = sum(len(group) for group in pending_groups)
        callbacks.warn(
            T("已停止：**不会再发送任何新请求**（重试与拆单重发也一并停下）。"
            "已经发出的那几组仍会收回来 —— 它们的请求已经花过钱，"
            "已经分析完成的图会照常写出 XMP")
            + (f"；剩余 {leftover} 张本次不动（可用「续跑」继续）。" if leftover else "。")
        )
        pending_groups.clear()

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acb-ai")
    in_flight: dict[Any, list[PreviewItem]] = {}
    try:
        # 一开始就发现已停止（例如在预览阶段就按了停止）：一组都不发。
        if callbacks.should_stop() or (cancel_event is not None and cancel_event.is_set()):
            _announce_stop()
        while pending_groups and len(in_flight) < workers and not stopped:
            group = pending_groups.pop(0)
            # 记录尝试（断点/失败计数用）
            for item_preview in group:
                job_state.mark_attempt(outcomes_by_id[item_preview.file_id].item)
            in_flight[
                pool.submit(
                    adapter.analyze_group,
                    group,
                    style_block=style_block,
                    user_prompt=prompt,
                    batch_stats=medians,
                    auto_match=auto_match,
                )
            ] = group

        while in_flight:
            done, _pending = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                group = in_flight.pop(future)
                try:
                    group_results = future.result()
                except (StopRequestedError, CancelledError):
                    # 用户已停止（控制流信号，**不是**这组图失败）：不记失败、不重发。
                    _announce_stop()
                    continue
                except Exception as exc:
                    # analyze_group 内部已尽力兜底；能到这里的属于意外（如预算超限）。
                    group_results = [
                        ItemResult(file_id=p.file_id, error=f"分析异常：{exc}") for p in group
                    ]

                for item_result in group_results:
                    results[item_result.file_id] = item_result
                    step_done += 1
                    callbacks.progress(step_done, total_steps)

                    outcome = outcomes_by_id.get(item_result.file_id)
                    name = outcome.item.path.name if outcome else item_result.file_id
                    if item_result.ok:
                        callbacks.item_status(
                            name, TF('分析完成（{0} 个参数）', len(item_result.params))
                        )
                        if item_result.notes:
                            callbacks.info(TF('{0}：{1}', name, item_result.notes))
                        for warning in item_result.warnings:
                            callbacks.warn(TF('{0}：{1}', name, warning))
                    else:
                        callbacks.item_status(name, T("分析失败"))

                # 收完一组再决定要不要补下一组 —— 停止从这里开始生效。
                if callbacks.should_stop() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    _announce_stop()
                    continue
                while pending_groups and len(in_flight) < workers:
                    group = pending_groups.pop(0)
                    for item_preview in group:
                        job_state.mark_attempt(outcomes_by_id[item_preview.file_id].item)
                    in_flight[
                        pool.submit(
                            adapter.analyze_group,
                            group,
                            style_block=style_block,
                            user_prompt=prompt,
                            batch_stats=medians,
                            auto_match=auto_match,
                        )
                    ] = group
    finally:
        pool.shutdown(wait=True)

    return {"results": results, "stopped": stopped, "step_done": step_done,
            # 画面统计量（含肤色占比与暖偏）随结果一起回去：写盘前的“方向不许叠加”护栏
            # 需要它，而重算一次预览代价太大（它就在这份 outcome 里）。
            "stats": {key: outcome.stats for key, outcome in outcomes_by_id.items()
                      if outcome.stats is not None}}
