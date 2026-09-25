# -*- coding: utf-8 -*-
"""Auto Color Diffusion —— 程序入口。

两种运行方式
------------
    1. 图形界面（默认）：python app.py
    2. 命令行（无界面）：python app.py --no-gui --files <目录或文件> [选项]

硬约束 #6 / #7 要求的 CLI 入口全部在此：
    --resume          断点续跑（跳过已完成的，不重复请求 API）
    --only-failed     只跑失败过的文件
    --clear-failures  清除失败记录（永久跳过的逃生舱）
    --dry-run         只跑 3 张验证链路（界面里叫「离线调试」）
    --max-requests N  覆盖请求数上限（默认 = 文件数 × 预算倍数 2）
    --workers N       并发数（默认 2）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from acb import __version__
from acb.constants import (
    BUDGET_MULTIPLIER,
    COLOR_SPACE_CHOICES,
    DEFAULT_EXPORT_FORMAT,
    DEFAULT_PS_JPEG_QUALITY,
    DEFAULT_WORKERS,
    DRY_RUN_LIMIT,
    EXPORT_FORMATS,
    LEGACY_QUALITY_ALIASES,
    LIBJPEG_EQUIVALENT,
    PS_JPEG_QUALITY_MAX,
    PS_JPEG_QUALITY_MIN,
    RAW_EXTENSIONS,
    TRAIN_MIN_SAMPLES_WARN,
    describe_low_quality_warning,
    is_very_low_quality,
    resolve_ps_quality,
)
from acb.errors import AcbError
from acb.logging_setup import get_logger, setup_logging
from acb.paths import ensure_runtime_dirs, logs_dir, require_windows


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    保持"每个选项都有中文说明 + 默认值来源"的风格，
    因为 CLI 是本工具在无人值守场景下的唯一入口。
    """
    parser = argparse.ArgumentParser(
        prog="autocolordiffusion",
        description=(
            "Auto Color Diffusion —— AI 批量调色辅助工具。"
            "为一批 RAW 生成 ACR 调整参数并写出 XMP 旁侧文件，再生成 Photoshop 批量导出脚本。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python app.py                                        # 打开图形界面\n"
            "  python app.py --no-gui --files D:\\\\photos --dry-run    # 无界面演练 3 张\n"
            "  python app.py --no-gui --files D:\\\\photos --resume     # 续跑未完成的\n"
            "  python app.py --no-gui --files D:\\\\photos --only-failed\n"
            "  python app.py --clear-failures                       # 清除失败记录\n"
            "  python app.py --no-gui --files D:\\\\train --train --style-name 海边暖调\n"
        ),
    )

    parser.add_argument("sources", nargs="*", type=Path,
                        help="RAW 文件或文件夹（文件夹会递归扫描）")
    parser.add_argument("--files", nargs="*", type=Path, default=None,
                        help="同位置参数；写成 --files 形式可读性更好")

    parser.add_argument("--no-gui", action="store_true", help="不打开界面，直接命令行执行")

    # --- 断点与失败处理（硬约束 #6）---
    resume_group = parser.add_argument_group("断点续跑")
    resume_group.add_argument("--resume", action="store_true",
                              help="跳过已完成的文件，不重复请求 API"
                                   "（默认**关闭**，即默认全部重新处理）")
    resume_group.add_argument("--no-resume", action="store_true",
                              help="[已废弃] 现在这就是默认行为，保留仅为兼容旧脚本")
    resume_group.add_argument("--only-failed", action="store_true",
                              help="只处理上次失败过且未达永久跳过阈值的文件")
    resume_group.add_argument("--clear-failures", action="store_true",
                              help="清除 failed.json 中的失败记录（含永久跳过标记）后退出")
    resume_group.add_argument("--reset-state", action="store_true",
                              help="清空全部任务状态（已完成记录 + 失败记录）后退出")

    # --- 离线调试（演练）与限流（硬约束 #7 / #18）---
    control = parser.add_argument_group("离线调试（演练）与限流")
    control.add_argument("--dry-run", action="store_true",
                         help=f"只处理前 {DRY_RUN_LIMIT} 张用于验证链路")
    control.add_argument("--dry-run-no-xmp", action="store_true",
                         help="演练时不写任何 XMP（完全不改动用户文件）")
    control.add_argument("--mock-ai", action="store_true",
                         help="离线调试：不调用 AI、不需要密钥，直接注入写死的"
                              "假分析结果（用于没有可用密钥时验证整条链路）")
    control.add_argument("--max-requests", type=int, default=None, metavar="N",
                         help=f"单批最大请求数（含重试）；默认 = 文件数 × {BUDGET_MULTIPLIER}")
    control.add_argument("--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
                         help=f"并发数，默认 {DEFAULT_WORKERS}")
    control.add_argument("--no-cache", action="store_true", help="不使用缩略图缓存")
    control.add_argument("--clear-cache", action="store_true", help="清除缩略图缓存后退出")

    # --- 调色参数 ---
    color = parser.add_argument_group("调色参数")
    color.add_argument("--prompt", default="", help="本批提示词；与所选风格叠加，冲突以它为准；留空则按风格/自动一致性校正")
    color.add_argument("--style", default=None, help="风格名（styles/ 目录下的 name 字段）")
    color.add_argument("--model", default=None, help="覆盖 models.yaml 的 active 模型条目名")

    # --- 输出 ---
    output = parser.add_argument_group("输出设置")
    output.add_argument("--color-space", default="sRGB", choices=list(COLOR_SPACE_CHOICES),
                        help="输出色彩空间，默认 sRGB")
    # 质量说明文案从常量表派生，不写死具体数字——否则改了刻度表而忘了改这里，
    # --help 就在骗人。
    quality_help = (
        f"输出 JPG 质量，直接使用 Photoshop 的 {PS_JPEG_QUALITY_MIN}–{PS_JPEG_QUALITY_MAX} 刻度"
        f"（默认 {DEFAULT_PS_JPEG_QUALITY}，即 Photoshop 上限）。"
        f"数值越大画质越好、文件越大；{PS_JPEG_QUALITY_MAX} 约等于 "
        f"libjpeg {LIBJPEG_EQUIVALENT[PS_JPEG_QUALITY_MAX]}，"
        "真实的 libjpeg 100 在 Photoshop 里无法表达。"
        "为兼容旧脚本，也接受档位名 " + "/".join(LEGACY_QUALITY_ALIASES) + "。"
    )
    output.add_argument("--quality", default=str(DEFAULT_PS_JPEG_QUALITY),
                        help=quality_help)
    output.add_argument("--output-dir", type=Path, default=None,
                        help="输出目录；默认与源文件同目录")
    output.add_argument("--format", default=DEFAULT_EXPORT_FORMAT,
                        choices=list(EXPORT_FORMATS),
                        help="导出格式：JPG / PNG（默认 JPG）。"
                             "PNG 为无损格式，--quality 对它们无效")
    output.add_argument("--suffix", default="",
                        help="导出文件名尾缀，例如 edit → 文件名-edit.jpg。"
                             "留空则用默认尾缀 -1（重复导出自动变成 -2、-3）")
    output.add_argument("--export-subdir", action="store_true",
                        help="不放在源目录同层，改为输出到 <源目录>/_export/ 子目录")
    output.add_argument("--same-level", action="store_true",
                        help="（已废弃）同层现在是默认行为；保留仅为兼容旧命令行")
    output.add_argument("--dng-sidecar", action="store_true",
                        help="DNG 也生成旁侧 .xmp（默认写回文件内部；勾选此项可能被 ACR 忽略）")
    output.add_argument("--no-photoshop", action="store_true",
                        help="只生成 jsx/manifest，不自动调用 Photoshop")
    output.add_argument("--high-fidelity", action="store_true",
                        help="跳过嵌入式预览，直接用 rawpy 解码（慢，但更接近 ACR 中性观感）")

    # --- 训练模式 ---
    train = parser.add_argument_group("训练模式")
    train.add_argument("--train", action="store_true",
                       help="进入训练模式：从 (RAW + XMP) 对归纳风格档案")
    train.add_argument("--style-name", default="", help="训练产出的风格名（必填）")

    parser.add_argument("--version", action="version", version=f"Auto Color Diffusion {__version__}")
    return parser


def collect_sources(args: argparse.Namespace) -> list[Path]:
    """合并位置参数与 --files。"""
    sources: list[Path] = []
    for item in list(args.sources or []) + list(args.files or []):
        if item is not None:
            sources.append(Path(item))
    return sources


def print_startup_banner() -> None:
    """打印启动信息与关键前提条件。"""
    print("=" * 78)
    print(f"Auto Color Diffusion v{__version__} —— AI 批量调色辅助工具")
    print("=" * 78)
    print(f"支持的后缀：{' '.join(ext.upper() for ext in RAW_EXTENSIONS)}")
    print("前提条件（很重要）：Camera Raw 首选项必须勾选")
    print("  「将图像设置存储在侧车 .xmp 文件中」，否则旁侧 XMP 不会被读取。")
    print("DNG 提示：DNG 的设置会被写回文件内部，操作前请务必备份原片。")
    print("=" * 78)


def run_cli(args: argparse.Namespace) -> int:
    """无界面执行路径。"""
    require_windows()
    ensure_runtime_dirs()
    setup_logging(logs_dir())

    log = get_logger("cli")
    # Windows 控制台默认可能是 GBK；中文日志与路径会乱码，
    # 因此显式把标准输出切到 UTF-8（errors=replace 保证永不因编码崩溃）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception as exc:   # noqa: BLE001 - 控制台不支持 reconfigure 时继续跑
        log.debug("标准输出无法切到 UTF-8（忽略，中文可能乱码）：%s", exc)

    print_startup_banner()

    # --- ICC 资源：补齐程序自己生成的 sRGB / Adobe RGB 等效 profile ---
    # 无界面路径同样需要（Adobe RGB 的输入预览要在颜色上正确必须先有这个文件）。
    # ensure_icc_assets 内部不抛异常，失败最多是"少一次自愈"。
    from acb.raw.icc import ensure_icc_assets

    for icc_file in ensure_icc_assets():
        print(f"已补齐 ICC 资源：{icc_file}")

    from acb.pipeline.job import JobState

    job_state = JobState()

    # --- 只做清理的短路分支 ---
    if args.clear_failures:
        count = job_state.clear_failures()
        print(f"已清除 {count} 条失败记录。")
        return 0

    if args.reset_state:
        count = job_state.reset_all()
        print(f"已清空任务状态 {count} 条。")
        return 0

    if args.clear_cache:
        from acb.cache import ThumbCache

        cache = ThumbCache()
        count, total = cache.stats()
        removed = cache.clear()
        print(f"已清除缩略图缓存：删除 {removed} 个文件（原有 {count} 个，约 {total / 1024 / 1024:.1f} MB）。")
        return 0

    # --- 环境自检 ---
    from acb.config import load_models_config
    from acb.keyring_store import KeyStore, mask
    from acb.ps import photoshop
    from acb.raw.exiftool import ExiftoolRunner

    exiftool = ExiftoolRunner()
    if exiftool.available:
        print(f"[OK] exiftool {exiftool.status.version}：{exiftool.status.path}")
    else:
        print("[警告] " + exiftool.status.install_guide())

    ps_ok, ps_message = photoshop.probe()
    print(f"[{'OK' if ps_ok else '警告'}] Photoshop：{ps_message}")

    key_store = KeyStore()
    print(f"[{'OK' if key_store.keyring_available else '警告'}] 系统密钥链："
          f"{'可用' if key_store.keyring_available else (key_store.keyring_error or '不可用')}")

    # --- 模型与密钥 ---
    try:
        config = load_models_config()
    except AcbError as exc:
        print(f"[错误] {exc}")
        return 2

    model_key = args.model or config.active
    spec = config.models.get(model_key)
    if spec is None:
        print(f"[错误] 未找到模型条目 {model_key}；可用：{', '.join(sorted(config.models))}")
        return 2

    # 离线调试：不要求密钥，也不构造 HTTP 客户端。
    # 必须在取密钥**之前**分支——否则没有可用密钥时会在下面直接 return 2，
    # 把"想在不联网的情况下把链路跑通"这件事堵死（而这正是它的用途）。
    if args.mock_ai:
        api_key = ""
    else:
        api_key = key_store.get(spec.key_env)

    if not args.mock_ai and not api_key:
        print(
            f"[错误] 未找到 API 密钥（账户名 {spec.key_env}）。\n"
            "无界面模式下无法弹出输入框，请先设置环境变量或打开界面保存一次：\n"
            f'    PowerShell:  $env:{spec.key_env} = "sk-..."\n'
            "若只想验证链路而不调用 API，请改用 --mock-ai。"
        )
        return 2

    from acb.ai.adapter import ModelAdapter
    from acb.ai.client import RequestBudget
    from acb.pipeline.output_mode import JobCallbacks

    if args.mock_ai:
        print(f"[OK] 模型：{spec.display}（离线调试模式，不会发出任何网络请求）")
        # 离线调试：不联网、不需要密钥，直接注入写死的假分析结果。
        from acb.ai.offline import build_offline_adapter

        adapter = build_offline_adapter(spec)
    else:
        print(f"[OK] 密钥 {mask(api_key)}（账户名 {spec.key_env}）")
        print(f"[OK] 模型：{spec.display}")
        # 预算先给占位值；run_output_mode 会用真实文件数重建。
        adapter = ModelAdapter(spec, api_key, RequestBudget(limit=10 ** 6))

    callbacks = JobCallbacks(
        log=lambda message, level: print(f"[{level}] {message}"),
        progress=lambda done, total: None,
        stage=lambda text: print(f"\n--- {text} ---"),
        item_status=lambda name, status: print(f"    {name}: {status}"),
        should_stop=lambda: False,
    )

    sources = collect_sources(args)
    if not sources:
        print("[错误] 未指定任何源路径。请用 --files 或位置参数传入文件夹/文件。")
        return 2

    # --- 训练模式 ---
    if args.train:
        style_name = (args.style_name or "").strip()
        if not style_name:
            print("[错误] 训练模式必须用 --style-name 指定风格名。")
            return 2

        from acb.pipeline.train_mode import TrainOptions, discover_pairs, run_train_mode

        pairs = discover_pairs(sources, recursive=True)
        print(f"发现 {len(pairs)} 个训练样本对。")
        result = run_train_mode(
            pairs,
            TrainOptions(use_cache=not args.no_cache, workers=args.workers),
            adapter=adapter,
            exiftool=exiftool,
            callbacks=callbacks,
            style_name=style_name,
        )
        if result.profile is None:
            print("[错误] 训练未产出风格档案。")
            return 1
        print(f"\n风格档案已保存：{result.saved_path}")
        print(f"样本 {result.sample_count} 个，统计字段 {len(result.profile.get('param_ranges') or {})} 个，"
              f"风格规则 {len(result.profile.get('text_rules') or [])} 条。")
        if result.insufficient_samples:
            print(f"[提示] 样本不足 {TRAIN_MIN_SAMPLES_WARN} 个，结果可能不稳定。")
        return 0

    # --- 输出模式 ---
    from acb.pipeline.output_mode import OutputOptions, run_output_mode
    from acb.pipeline.style_profile import ensure_seed_styles, load_style_profile

    # 先把内置风格种子释放到数据目录（幂等；与界面启动时的做法一致）。
    # 不先做这一步，`--style AI自主决策` 这种内置风格会因为"文件还没被释放"
    # 而按"找不到风格"处理 —— 实测踩到，用户会以为这个内置选项不存在。
    ensure_seed_styles()

    style_name = args.style or None
    style_data = load_style_profile(style_name) if style_name else None
    if style_name and not style_data:
        print(f"[警告] 找不到风格「{style_name}」，将按未选择风格处理。")

    # 把命令行传入的质量值解析为 Photoshop 刻度。越界值或旧档位名会返回
    # 提示文字，这里显式打印出来——静默接受会让用户以为设置生效了。
    ps_quality, quality_note = resolve_ps_quality(args.quality)
    if quality_note:
        print(f"[提示] {quality_note}")
    if is_very_low_quality(ps_quality):
        # 无界面场景不能弹模态框（无人值守会挂死等输入），所以只警告不拦截。
        # 拦截责任在界面那一侧；命令行用户是自己写参数的人，警告即可。
        print(f"[警告] {describe_low_quality_warning(ps_quality)}")

    opts = OutputOptions(
        sources=sources,
        prompt=args.prompt,
        style_name=style_name,
        style_data=style_data,
        color_space=args.color_space,
        export_format=args.format,
        export_suffix=args.suffix,
        ps_quality=ps_quality,
        output_dir=args.output_dir,
        # --same-level 现在是默认行为，所以它等于"不额外做什么"；
        # 真正的开关是反向的 --export-subdir。
        output_to_source_dir=not args.export_subdir,
        recursive=True,
        # 默认全量重跑：用户要的是"能重复处理"，而不是被静默跳过。
        resume=args.resume,
        only_failed=args.only_failed,
        dry_run=args.dry_run,
        dry_run_no_xmp=args.dry_run_no_xmp,
        workers=max(1, args.workers),
        max_requests=args.max_requests,
        use_cache=not args.no_cache,
        prefer_raw_decode=args.high_fidelity,
        dng_sidecar=args.dng_sidecar,
        run_photoshop=not args.no_photoshop,
    )

    if not args.resume and not args.only_failed and not args.train:
        print("[全量模式] 已完成的文件也会重新请求 API；"
              "想跳过它们请加 --resume。（注意：输出文件若已存在，"
              "会按尾缀规则递增而不会覆盖）")

    if args.dry_run:
        print(f"[离线调试（演练）] 只处理前 {DRY_RUN_LIMIT} 张"
              f"{'，且不写 XMP' if args.dry_run_no_xmp else '（会写出真实 XMP）'}。")

    result = run_output_mode(
        opts,
        adapter=adapter,
        exiftool=exiftool,
        job_state=job_state,
        callbacks=callbacks,
    )

    print("\n" + "=" * 78)
    print(result.summary_text())
    print("=" * 78)

    stats = job_state.stats()
    if stats["failed"]:
        print(f"\n失败清单（{stats['failed']} 条，其中永久跳过 {stats['permanent_skip']} 条）：")
        for path, count, error, permanent in job_state.failure_summary()[:20]:
            flag = " [永久跳过]" if permanent else ""
            print(f"  - {Path(path).name}（{count} 次）{flag}\n      {error[:200]}")
        print("\n修正原因后可用 --only-failed 重跑，或 --clear-failures 重置失败记录。")

    _cleanup_run_artifacts(result)

    return 0 if result.failed == 0 else 1


def _cleanup_run_artifacts(result) -> None:
    """命令行跑完清掉本次的运行目录（与界面退出清理同一套判定）。

    只清运行目录，**不动缩略图缓存**：命令行常被脚本反复调用，
    每次都把缓存清掉会让下次重跑白等一遍预览提取。

    判定条件必须与界面一致（见 MainWindow._manual_export_needed）：
    需要手动补跑 → 保留，否则删掉。命令行并不需要清理别的东西，
    所以这里直接调 purge_run_dirs 而不是 cleanup_session_junk。
    """
    if result.script_dir is None:
        return

    from acb import cleanup

    # 只看结构化字段，不做文案子串匹配（见 OutputResult.ps_ok 的注释）。
    manual_needed = bool(result.ps_ok is False and not result.stopped)
    try:
        report = cleanup.CleanupReport()
        cleanup.purge_run_dirs(
            result.script_dir if manual_needed else None, report
        )
        if manual_needed:
            # 保留时一定要说清楚在哪、以及下次记得删 —— 否则它会一直堆下去。
            print(
                f"\n需要手动完成导出：脚本目录保留在\n  {result.script_dir}\n"
                "  双击其中的 run_export.bat 即可（关闭程序不影响）。"
            )
        elif report.touched:
            print(f"\n已清理本次运行的脚本目录（{report.deleted_files} 个文件）。")
    except Exception as exc:  # noqa: BLE001 - 收尾阶段不能抛
        print(f"\n收尾清理出错（已忽略，不影响结果）：{exc}")


def main(argv: list[str] | None = None) -> int:
    """入口函数。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    # 无界面模式，或带了任何"纯命令行"动作参数时，直接走 CLI。
    cli_only = (
        args.no_gui
        or args.clear_failures
        or args.reset_state
        or args.clear_cache
    )
    if cli_only:
        try:
            return run_cli(args)
        except AcbError as exc:
            print(f"[错误] {exc}")
            return 2
        except KeyboardInterrupt:
            print("\n已中断。已完成的处理结果已保留，可用 --resume 继续。")
            return 130

    # 否则启动图形界面。
    try:
        from acb.ui.main_window import run_app
    except ImportError as exc:
        print(f"[错误] 无法加载图形界面（PyQt6 未安装？）：{exc}")
        print("可以改用命令行模式：python app.py --no-gui --files <目录>")
        return 2
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
