# -*- coding: utf-8 -*-
"""训练模式（第二节的【训练模式】+ 硬约束 #15 / #19）。

流程
----
    1. 用户添加若干 (RAW + 其对应 XMP) 对
    2. 执行输出模式的第 2、3 步（提取预览 → ICC 转 sRGB → 长边 1024 → q85 缓存）
    3. 把 XMP 转回 JSON（reader.training_snapshot）
    4. 做三层差分，分离"用户实际调整"与"相机/镜头默认值"
    5. 把预览图 + 差分后的参数一并交给 AI，让其归纳偏好（temperature=0.2）
    6. 本地用 numpy 补齐统计量（mean/median/min/max），组装 style_profile.json
    7. 由用户命名后保存到 styles/，界面下拉即可选用

为什么统计量必须本地算而不让模型给：
    模型心算的均值/中位数不可复现、不可审计，而且很容易算错。
    把 AI 定位在"归纳语义"（风格倾向、禁区），把算术留给 numpy，
    是让 style_profile.json 可信的关键分工。

样本不足提示
------------
样本 < TRAIN_MIN_SAMPLES_WARN（10）时，界面与日志都要提示
"样本不足，结果可能不稳定"（硬约束 / 第二节明确要求）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ai.adapter import ModelAdapter, PreviewItem
from ..ai.client import RequestBudget
from ..cache import ThumbCache, thumb_key
from ..constants import DEFAULT_WORKERS, TRAIN_MIN_SAMPLES_WARN, TRAIN_REQUEST_BUDGET, is_dng
from ..errors import PreviewExtractionError, StopRequestedError
from ..logging_setup import get_logger
from ..raw.exiftool import ExiftoolRunner
from ..raw.preview import extract_thumbnail
from ..raw.stats import compute_stats
from ..xmp import reader as xmp_reader
from .job import iter_raw_files
from .output_mode import JobCallbacks
from .style_profile import (
    build_style_profile,
    compute_adjustments,
    save_style_profile,
)

log = get_logger("train_mode")


@dataclass
class TrainOptions:
    """训练模式的参数。"""

    recursive: bool = True
    use_cache: bool = True
    prefer_raw_decode: bool = False
    allow_raw_decode_fallback: bool = True
    workers: int = DEFAULT_WORKERS


@dataclass
class TrainPair:
    """一个训练样本对（RAW + 对应 XMP）。"""

    raw: Path
    xmp: Path | None      # DNG 时为 None（设置内嵌在文件里）

    @property
    def label(self) -> str:
        return self.raw.name


@dataclass
class TrainSample:
    """一个已解析的训练样本。"""

    pair: TrainPair
    snapshot: dict[str, Any]
    adjusted: dict[str, Any] = field(default_factory=dict)
    excluded: list[dict[str, str]] = field(default_factory=list)
    preview_jpeg: bytes | None = None
    caption: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TrainResult:
    """训练运行结果。"""

    profile: dict[str, Any] | None = None
    saved_path: Path | None = None
    sample_count: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)
    insufficient_samples: bool = False
    # 本次训练的 token 用量（来自客户端记账）。
    # 【为什么要在这里加】用户 2026-09-24 让“从日志统计每张照片的 token”，
    # 结果发现输出模式每轮都记了「Token 用量：输入 … 输出 …」，
    # 而**训练那一发从不记** —— 归纳请求要把全部样本的参数明细 + 缩略图一次发上去，
    # 是单次最贵的一发，却在日志里完全看不到花费。
    usage: str = ""
    # 用户点了停止（不是失败）：界面要区别显示 —— “训练失败”会引导他去查日志，
    # 而停止是他自己按的，只需要告诉他风格没保存。
    stopped: bool = False


# ============================================================================
# 样本配对发现
# ============================================================================

def sidecar_candidates(raw_path: Path) -> list[Path]:
    """列出某个 RAW 可能的旁侧 XMP 路径（兼容大小写与 .dng.xmp 形态）。

    为什么要考虑大小写：Windows 文件系统本身不区分大小写，
    但跨平台拷贝（如从 macOS 或网盘同步）后会出现 .XMP 的大写形态，
    在部分同步盘上它们甚至会被当成不同文件。
    另外 DNG 用户若曾按"强制旁侧文件"模式跑过，会有 foo.dng.xmp。
    """
    stem = raw_path.with_suffix("")
    candidates = [
        raw_path.with_suffix(".xmp"),
        raw_path.with_suffix(".XMP"),
        Path(str(stem) + ".dng.xmp") if is_dng(raw_path.name) else Path(str(stem) + ".xmp"),
    ]
    # 去重并保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def discover_pairs(sources: list[Path], *, recursive: bool = True) -> list[TrainPair]:
    """从用户选中的路径里发现 (RAW, XMP) 对。

    用户可能选中的东西：
        - 目录（递归找 RAW，并为每个 RAW 找同目录同名 XMP）
        - 具体的 RAW 文件
        - 具体的 XMP 文件（反查同目录同名 RAW）
    """
    pairs: list[TrainPair] = []
    seen_raws: set[str] = set()

    # --- 先处理显式的 RAW 与目录 --------------------------------------------------
    raw_sources = [p for p in sources if p.is_file() and p.suffix.lower() != ".xmp"]
    dir_sources = [p for p in sources if p.is_dir()]
    xmp_sources = [p for p in sources if p.is_file() and p.suffix.lower() == ".xmp"]

    raw_paths: list[Path] = []
    if raw_sources:
        raw_paths.extend(raw_sources)
    if dir_sources:
        raw_paths.extend(iter_raw_files(dir_sources, recursive=recursive))

    for raw in raw_paths:
        key = str(raw.resolve()).lower()
        if key in seen_raws:
            continue
        seen_raws.add(key)

        if is_dng(raw.name):
            # DNG 不需要旁侧文件（设置内嵌），但也允许有。
            existing = next((c for c in sidecar_candidates(raw) if c.is_file()), None)
            pairs.append(TrainPair(raw=raw, xmp=existing))
            continue

        existing = next((c for c in sidecar_candidates(raw) if c.is_file()), None)
        if existing is None:
            log.warning("跳过 %s：找不到同名 XMP，无法作为训练样本。", raw.name)
            continue
        pairs.append(TrainPair(raw=raw, xmp=existing))

    # --- 再处理用户显式选中的 XMP（反查 RAW）--------------------------------------
    for xmp in xmp_sources:
        name = xmp.name
        if name.lower().endswith(".dng.xmp"):
            raw = xmp.with_name(xmp.name[: -len(".xmp")])
        else:
            raw = xmp.with_suffix(".cr3")  # 占位，下面用通配替换
            raw = xmp.with_suffix("")
            # 用 glob 找同主名的任何白名单 RAW
            candidates = [
                p
                for p in raw.parent.glob(f"{raw.name}.*")
                if p.is_file() and p.suffix.lower() != ".xmp"
            ]
            raw = next((p for p in candidates if p.suffix.lower() in
                        (".cr3", ".cr2", ".nef", ".nrw", ".arw", ".srf", ".sr2",
                         ".raf", ".orf", ".rw2", ".pef", ".dng")), None)  # type: ignore[assignment]
        if raw is None or not Path(raw).is_file():
            log.warning("跳过 %s：找不到对应的 RAW 文件。", xmp.name)
            continue
        key = str(Path(raw).resolve()).lower()
        if key in seen_raws:
            continue
        seen_raws.add(key)
        pairs.append(TrainPair(raw=Path(raw), xmp=xmp))

    pairs.sort(key=lambda pair: str(pair.raw).lower())
    return pairs


# ============================================================================
# 样本解析
# ============================================================================

def _load_one_sample(
    pair: TrainPair,
    *,
    exiftool: ExiftoolRunner,
    cache: ThumbCache,
    opts: TrainOptions,
    adapter: ModelAdapter,
) -> TrainSample:
    """解析单个训练样本：预览 + XMP → 差分后的参数。"""
    sample = TrainSample(pair=pair, snapshot={})

    # --- XMP 解析（训练模式读的是"用户已有的 XMP"，不含 AI 参与）---
    try:
        if pair.xmp is not None:
            doc = xmp_reader.read_xmp_file(pair.xmp)
        else:
            # DNG：设置内嵌在文件内部，用 exiftool 读出来解析。
            packet = exiftool.read_xmp_packet(pair.raw) if exiftool.available else None
            if not packet:
                raise xmp_reader.XmpParseError(
                    "DNG 内嵌 XMP 读取失败（可能是没有 exiftool，或该 DNG 确实没有设置）"
                )
            doc = xmp_reader.read_xmp_bytes(packet, source=f"{pair.raw.name}!<embedded>")
        sample.snapshot = doc.training_snapshot()
    except Exception as exc:
        sample.error = f"解析 XMP 失败：{exc}"
        return sample

    # 未知字段只记 DEBUG，不阻断——用户可能用过我们没有登记的 ACR 新字段。
    unknown = sample.snapshot.get("unknown_fields") or []
    if unknown:
        sample.warnings.append(
            f"{pair.label} 含 {len(unknown)} 个未登记的 crs 字段（将忽略）：{unknown[:8]}"
        )

    # --- 差分：区分用户调整与相机/镜头默认 ---
    adjusted, excluded = compute_adjustments(sample.snapshot)
    sample.adjusted = adjusted
    sample.excluded = excluded

    # --- 预览（用于让模型看到画面，以及生成 caption）---
    if cache.enabled:
        # 与 output_mode 用同一套派生规则（文件标识键 + 缩略图配方），
        # 这样训练与输出共用的缓存目录不会互相污染。
        hit = cache.get(thumb_key(_key_for(pair.raw)))
        if hit is not None:
            sample.preview_jpeg = hit[0]

    if sample.preview_jpeg is None:
        try:
            result = extract_thumbnail(
                pair.raw,
                exiftool,
                prefer_raw_decode=opts.prefer_raw_decode,
                allow_raw_decode_fallback=opts.allow_raw_decode_fallback,
            )
        except PreviewExtractionError as exc:
            sample.warnings.append(f"预览提取失败（将以纯参数训练）：{exc}")
        else:
            sample.preview_jpeg = result.jpeg_bytes
            sample.warnings.extend(result.warnings)
            if cache.enabled:
                cache.put(
                    thumb_key(_key_for(pair.raw)),
                    result.jpeg_bytes,
                    {"source": result.source, "color_reason": result.color_reason,
                     "warnings": result.warnings},
                )

    # 非视觉模型走 caption 通道（硬约束 #19）。
    if sample.preview_jpeg and not adapter.supports_vision:
        try:
            stats = compute_stats(sample.preview_jpeg)
            sample.caption = stats.to_caption_text()
        except Exception as exc:
            sample.warnings.append(f"统计量计算失败：{exc}")

    # --- as-shot 色温（白平衡偏移统计的基准，实施步骤 1c）---
    # 为什么必须读它：crs:Temperature 是**绝对开尔文**（IMG_2509 写的是 5500），
    # 而这张照片的现场色温由相机估出来是另一个数（实测 as-shot = 4743K）。
    # 把不同照片的绝对值平均得不到任何"习惯"；用户真正稳定的习惯是
    # "相对 as-shot 偏暖多少 K"（5500-4743 = +757K）。
    # exiftool 的 ColorTempAsShot（CR3 的 CMT4 轨）就是 Lightroom 用作 as-shot 的那个值。
    if exiftool.available:
        try:
            tags = exiftool.read_tags(pair.raw, ["ColorTempAsShot", "ColorTemperature"])
        except Exception as exc:  # noqa: BLE001 —— 读不到就跳过白平衡统计，不阻断训练
            sample.warnings.append(f"读取 as-shot 色温失败：{exc}")
            tags = {}
        raw_cct = (tags.get("ColorTempAsShot") or "").strip()
        if raw_cct:
            try:
                sample.snapshot["as_shot_cct"] = float(raw_cct)
            except ValueError:
                sample.warnings.append(f"as-shot 色温无法解析：{raw_cct!r}")
        else:
            sample.warnings.append("这张照片没有 as-shot 色温标签，白平衡偏移统计会跳过它")

    return sample


def _key_for(path: Path) -> str:
    """算文件标识键（与断点键同源，硬约束 #16）。

    注意：这只是"文件标识"，还要经 cache.thumb_key() 掺入缩略图配方
    才能当缓存键用 —— 否则升级后改了缩略图参数会导致读到旧配方的缓存。
    """
    from ..cache import file_key

    return file_key(path)


# ============================================================================
# 主流程
# ============================================================================

def run_train_mode(
    pairs: list[TrainPair],
    opts: TrainOptions,
    *,
    adapter: ModelAdapter,
    exiftool: ExiftoolRunner,
    callbacks: JobCallbacks,
    style_name: str,
    cancel_event: threading.Event | None = None,
) -> TrainResult:
    """执行训练模式。"""
    result = TrainResult(sample_count=len(pairs))

    if not pairs:
        callbacks.error("没有可用的训练样本。请添加 RAW 文件及其同名的 XMP 旁侧文件。")
        return result

    # --- 样本数量提示（硬约束要求）-----------------------------------------
    if len(pairs) < TRAIN_MIN_SAMPLES_WARN:
        result.insufficient_samples = True
        message = (
            f"样本不足，结果可能不稳定：当前 {len(pairs)} 个样本，"
            f"建议至少 {TRAIN_MIN_SAMPLES_WARN} 个。"
            "样本越少，中位数与范围统计越容易被单张照片的偶然设置带偏。"
        )
        callbacks.warn(message)
        result.warnings.append(message)
    else:
        callbacks.info(f"训练样本 {len(pairs)} 个，满足建议下限（{TRAIN_MIN_SAMPLES_WARN}）。")

    # --- 解析全部样本 -------------------------------------------------------
    callbacks.stage("解析训练样本")
    cache = ThumbCache(enabled=opts.use_cache)
    samples: list[TrainSample] = []
    total = len(pairs)
    done = 0
    callbacks.progress(0, total)

    workers = max(1, min(opts.workers, total))
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acb-train") as pool:
            futures = {
                pool.submit(
                    _load_one_sample,
                    pair,
                    exiftool=exiftool,
                    cache=cache,
                    opts=opts,
                    adapter=adapter,
                ): pair
                for pair in pairs
            }
            for future in as_completed(futures):
                callbacks.check_stop()
                pair = futures[future]
                try:
                    sample = future.result()
                except Exception as exc:
                    sample = TrainSample(pair=pair, snapshot={}, error=f"处理异常：{exc}")
                samples.append(sample)
                done += 1
                callbacks.progress(done, total)

                if sample.error:
                    result.failed += 1
                    callbacks.error(f"{pair.label}：{sample.error}")
                    callbacks.item_status(pair.label, "解析失败")
                else:
                    callbacks.item_status(
                        pair.label, f"已解析（{len(sample.adjusted)} 个用户调整）"
                    )
                    for warning in sample.warnings:
                        callbacks.warn(f"{pair.label}：{warning}")
    except StopRequestedError:
        callbacks.warn("已停止：训练样本解析中断。")
        return result

    usable = [s for s in samples if not s.error]
    if not usable:
        callbacks.error("没有任何可用的训练样本（全部解析失败）。")
        return result

    # --- 统计"用户实际调整了哪些字段" --------------------------------------
    adjusted_field_count: dict[str, int] = {}
    for sample in usable:
        for name in sample.adjusted:
            adjusted_field_count[name] = adjusted_field_count.get(name, 0) + 1

    if adjusted_field_count:
        top = sorted(adjusted_field_count.items(), key=lambda kv: -kv[1])[:10]
        callbacks.info(
            "用户调整最频繁的字段（字段: 出现样本数）："
            + "、".join(f"{name}:{count}" for name, count in top)
        )
    else:
        callbacks.warn(
            "在差分后没有发现任何'用户实际调整'的字段。"
            "这说明这批样本可能都是相机默认设置（例如全部由相机直出、未在 ACR 里调过），"
            "训练结果将只有限的参考价值。"
        )

    # --- 交给模型归纳语义 ---------------------------------------------------
    callbacks.stage("AI 归纳风格偏好")
    preview_items = [
        PreviewItem(
            file_id=_key_for(sample.pair.raw),
            filename=sample.pair.raw.name,
            path=sample.pair.raw,
            preview_jpeg=sample.preview_jpeg or b"",
            preview_source="cache-or-extract",
            caption=sample.caption,
            training_params=sample.adjusted,
            training_curves=sample.snapshot.get("curves") or {},
            training_excluded=[
                f"{entry['field']}({entry['reason']})" for entry in sample.excluded[:80]
            ],
            training_mask_usage=_mask_usage_text(sample),
        )
        for sample in usable
    ]

    if cancel_event is not None and cancel_event.is_set():
        callbacks.warn("已停止：未提交训练请求。")
        return result

    # 预算：训练**只发一次**归纳请求（体量最大的一发：全部样本的参数明细 + 缩略图）。
    # 但“一次请求”不等于“只花一个预算位”：每次真正发出的 HTTP 尝试都要计费
    # （首发 + 读超时重试 + 解析修复重试），预算给 1 时一次超时就把整次训练废掉。
    # 实测：44 个样本的首发在 180s 读超时上挂掉，重试被预算拒绝，
    # 风格档案没有保存，而用户端只看到一个“请求超时”。
    adapter.client.budget = RequestBudget(
        limit=TRAIN_REQUEST_BUDGET,
        hint="训练只发一次归纳请求，含读超时重试与解析修复",
    )
    callbacks.info(
        f"训练请求预算 {TRAIN_REQUEST_BUDGET} 次（一次归纳，含超时重试与解析修复）。"
    )
    # 停止也要管到训练的这一发：以前点了停止，它会带着重试（含翻倍后的长超时）跟到底。
    setter = getattr(adapter, "set_cancel_check", None)
    if callable(setter):
        setter(cancel_event.is_set if cancel_event is not None else None)

    try:
        model_output = adapter.analyze_training(preview_items, style_name)
    except StopRequestedError:
        callbacks.warn(
            "已停止：训练未完成（不会再发请求，风格档案未保存）。"
        )
        result.stopped = True
        result.warnings.append("已停止：训练未完成，风格档案未保存。")
        return result
    except Exception as exc:
        callbacks.error(f"AI 归纳失败：{exc}")
        result.warnings.append(f"AI 归纳失败：{exc}")
        # 必须把后果说清楚：旧版只报“归纳失败”，磁盘上留着上一次的风格文件，
        # 用户很容易以为训练成功了，然后奇怪“为什么风格没变”。
        callbacks.warn(
            "本次训练**没有完成：风格档案没有被保存或更新**，磁盘上仍是原来的那份。"
            "排查建议：先看上面的报错原文；超时类失败可直接重试一次，"
            "样本很多（几十上百张）时可以先减到 20–30 张，或换一个更快的模型再训。"
        )
        result.warnings.append("训练未完成：风格档案未保存。")
        return result

    # --- 用量记账（必须可见：这是单次最贵的一发）---------------------------
    _usage_of = getattr(getattr(adapter, "client", None), "usage_summary", None)
    if callable(_usage_of):
        try:
            result.usage = str(_usage_of())
        except Exception:  # noqa: BLE001 —— 记账失败不该弄死训练
            result.usage = ""
    if result.usage:
        callbacks.info(f"训练用量：{result.usage}")

    # --- 组装并保存 ---------------------------------------------------------
    callbacks.stage("生成风格档案")
    # 场景标签（模型看图为每张样本打的名）写回各自 snapshot，供 scene_rules 分组统计。
    # 只认模型返回的、且能对上 file_id 的标签；对不上的一律留空（不猜）。
    labels = model_output.get("scene_labels")
    if isinstance(labels, dict):
        for sample in usable:
            label = labels.get(_key_for(sample.pair.raw))
            if isinstance(label, str) and label.strip():
                sample.snapshot["scene_label"] = label.strip()
        labeled = sum(1 for sample in usable if sample.snapshot.get("scene_label"))
        callbacks.info(f"场景标签：{labeled}/{len(usable)} 张由模型标出。")
        if labeled < len(usable):
            callbacks.warn(
                f"有 {len(usable) - labeled} 张没有拿到场景标签，"
                "这些照片不会参与 scene_rules（场景规则）的统计。"
            )
    else:
        callbacks.warn(
            "模型没有返回 scene_labels：本次不会生成场景规则（scene_rules）。"
            "场景规则是按你的训练素材统计出来的，没有标签就没有依据。"
        )

    snapshots = [sample.snapshot for sample in usable]
    profile = build_style_profile(
        name=style_name,
        snapshots=snapshots,
        model_output=model_output,
    )
    result.profile = profile
    result.sample_count = len(usable)

    callbacks.info(
        f"风格档案统计完成：{len(profile['param_ranges'])} 个字段有统计量，"
        f"{len(profile['text_rules'])} 条风格规则，"
        f"{len(profile['ignore_fields'])} 个字段被判定为相机/镜头默认值而排除。"
    )

    try:
        path = save_style_profile(profile)
    except Exception as exc:
        callbacks.error(f"保存风格档案失败：{exc}")
        result.warnings.append(f"保存失败：{exc}")
        return result

    result.saved_path = path
    callbacks.info(f"风格档案已保存：{path}")
    callbacks.info("现在可以在界面的「风格」下拉中选中它，用于后续批量调色。")

    if result.insufficient_samples:
        callbacks.warn("再次提醒：样本不足，结果可能不稳定。建议补充更多样本后重新训练。")

    return result


def _mask_usage_text(sample: TrainSample) -> str | None:
    """把单个样本的蒙版使用情况转成一句话（供训练提示词使用）。"""
    stats = sample.snapshot.get("masks")
    if stats is None or not getattr(stats, "used", False):
        return None
    parts = [
        f"蒙版 {getattr(stats, 'total_corrections', 0)} 个",
        f"手动画笔/渐变 {getattr(stats, 'manual_count', 0)} 个",
        f"AI 主体蒙版 {getattr(stats, 'subject_count', 0)} 个",
        f"AI 对象选择 {getattr(stats, 'object_count', 0)} 个",
    ]
    if getattr(stats, "total_retouch_areas", 0):
        parts.append(f"污点修复 {stats.total_retouch_areas} 处")
    names = getattr(stats, "names", [])[:6]
    if names:
        parts.append("名称示例：" + "、".join(names))
    return "；".join(parts)
