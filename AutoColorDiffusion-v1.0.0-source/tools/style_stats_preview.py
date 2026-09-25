# -*- coding: utf-8 -*-
"""本地风格统计预览 —— 不调用 AI，直接看差分结果。

用途
----
这是一个"透明化"工具，回答用户最常问的质疑：
    「为什么我明明调过某个参数，风格档案里却没有它？」

它把 acb/pipeline/style_profile.py 的三层差分过程完整打印出来：
    第 1 层  crd: 差分      —— XMP 里 camera-raw-defaults 命名空间是相机出厂基线
    第 2 层  ACR 默认值差分 —— ACR_DEFAULT_VALUES 表
    第 3 层  批内不变项剔除 —— 整批取值完全一致的字段判为默认
每一项被排除的字段都会附带 reason 与 detail，可逐条核对。

顺带还会打印蒙版使用统计，验证"忽略几何、统计频率"这条规则的效果。

为什么值得单独做成工具
    硬约束 #15 要求区分「用户实际调整」与「相机/镜头默认值」，这是整个项目
    最容易出错、也最容易被用户质疑的一环。把中间过程暴露出来，
    用户可以自己判断统计是否可信，而不是只能相信一个黑盒 JSON。

用法
----
    python tools/style_stats_preview.py test_data
    python tools/style_stats_preview.py test_data --save-preview   # 顺带写出一份预览 JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print 中文或 ✓/✗ 会抛
# UnicodeEncodeError —— 那会把"未通过清单"整段变成 traceback，等于把最关键的信息
# 藏起来（本项目真的踩到过）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.paths import data_root  # noqa: E402
from acb.pipeline import style_profile as SP  # noqa: E402
from acb.xmp import fields as F  # noqa: E402
from acb.xmp import reader as R  # noqa: E402

# 每个字段最多展示的值个数（避免枚举型字段刷屏）
MAX_VALUES_SHOWN = 4


def load_snapshots(files: list[Path]) -> tuple[list[dict], list[str]]:
    """读取全部 XMP 的 training_snapshot。"""
    snapshots: list[dict] = []
    failures: list[str] = []
    for path in files:
        try:
            snapshots.append(R.read_xmp_file(path).training_snapshot())
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
    return snapshots, failures


def print_adjusted_report(per_sample: list[dict[str, object]]) -> None:
    """打印"判定为用户实际调整"的字段统计。"""
    counts: Counter = Counter()
    values: dict[str, list[float]] = defaultdict(list)

    for adjusted in per_sample:
        for name, value in adjusted.items():
            counts[name] += 1
            if isinstance(value, (int, float)):
                values[name].append(float(value))

    print("\n" + "=" * 100)
    print(f"判定为「用户实际调整」的字段（共 {len(counts)} 个）")
    print("=" * 100)
    if not counts:
        print("  （无）—— 说明这批样本看起来都是相机/ACR 默认设置。")
        return

    print(f"{'字段':<34}{'样本数':>7}  {'统计':<40}{'ACR 面板'}")
    print("-" * 100)
    for name, count in counts.most_common():
        spec = F.get_field(name)
        nums = values.get(name) or []
        if nums:
            stat = f"均值 {sum(nums) / len(nums):.2f}  范围 {min(nums):.2f} .. {max(nums):.2f}"
        else:
            stat = "（非数值）"
        print(f"{name:<34}{count:>7}  {stat:<40}{(spec.panel if spec else '?')}")


def print_excluded_report(
    exclusion_map: dict[str, dict[str, str]],
    exclusion_counts: dict[str, int],
    total_samples: int,
) -> None:
    """打印被排除的字段及理由。

    分两栏输出，这个区分很关键：
      A. 完全默认（在**全部**样本中都被排除）→ 进入 ignore_fields，会被写进提示词
         告诉 AI"不要据此推断"。
      B. 部分默认（只在部分样本中被排除）→ **不**进入 ignore_fields。
         否则等于让 AI 忽略用户真实调整过的偏好。
    """
    print("\n" + "=" * 100)
    print(f"字段排除情况（共评估 {len(exclusion_map)} 个字段，样本 {total_samples} 个）")
    print("=" * 100)

    reason_labels = {
        "crd_equals_crs": "与相机出厂基线一致（crd: 差分）",
        "acr_factory_default": "等于 ACR 出厂默认值",
        "batch_invariant": "批内取值完全一致",
        "lens_default": "镜头校正跟随机内默认",
        "wb_not_effective": "白平衡非 Custom，数值被 ACR 忽略",
        "machine_field": "机器/版本相关字段",
    }

    fully: list[str] = []
    partial: list[str] = []
    for field_name, count in exclusion_counts.items():
        (fully if count >= total_samples else partial).append(field_name)

    print(f"\n【A. 完全默认 → 写入 ignore_fields，会告知 AI 忽略】（{len(fully)} 个）")
    print("    这些字段在**每一个**样本里都被判定为非用户调整，因此可以安全地排除。")
    _print_grouped(fully, exclusion_map, exclusion_counts, total_samples, reason_labels)

    print(f"\n【B. 部分默认 → 不进 ignore_fields，避免误导 AI】（{len(partial)} 个）")
    print("    这些字段在某些样本里是默认值，但在另一些样本里确实被用户调过。")
    print(f"    把它们当作「默认值」会让 AI 忽略真实偏好，因此只在此展示、不参与提示词。")
    _print_grouped(partial, exclusion_map, exclusion_counts, total_samples, reason_labels)


def _print_grouped(
    field_names: list[str],
    exclusion_map: dict[str, dict[str, str]],
    exclusion_counts: dict[str, int],
    total_samples: int,
    reason_labels: dict[str, str],
) -> None:
    """按 reason 分组打印字段列表。"""
    grouped: dict[str, list[str]] = defaultdict(list)
    for field_name in field_names:
        grouped[exclusion_map.get(field_name, {}).get("reason", "?")].append(field_name)

    for reason, names in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        label = reason_labels.get(reason, reason)
        print(f"\n    -- {label}：{len(names)} 个 --")
        for field_name in sorted(names)[:18]:
            detail = exclusion_map.get(field_name, {}).get("detail", "")
            count = exclusion_counts.get(field_name, 0)
            print(f"       {field_name:<32}[{count}/{total_samples}] {detail}")
        if len(names) > 18:
            print(f"       … 其余 {len(names) - 18} 个字段略")


def print_mask_report(snapshots: list[dict]) -> None:
    """打印蒙版使用统计（验证"忽略几何、统计频率"的效果）。"""
    summary = SP.describe_mask_usage(snapshots)
    print("\n" + "=" * 100)
    print("蒙版与局部调整统计（几何已忽略，只统计使用模式）")
    print("=" * 100)
    print(f"  样本总数                    : {summary['samples_total']}")
    print(f"  使用过局部调整的样本        : {summary['samples_with_masks']}"
          f"（{summary['mask_usage_ratio'] * 100:.0f}%）")
    print(f"  局部调整（蒙版组）总数      : {summary['total_corrections']}")
    print(f"  平均每张                    : {summary['corrections_per_sample']}")
    print(f"  实际蒙版对象数              : {summary['total_masks']}")
    print(f"  手动画笔/渐变               : {summary['manual_count']}")
    print(f"  AI 主体蒙版（人物/皮肤/头发）: {summary['subject_count']}")
    print(f"  AI 对象选择                 : {summary['object_count']}")
    print(f"  污点修复区域                : {summary['total_retouch_areas']}")

    local_means = summary.get("local_field_means") or {}
    if local_means:
        print("\n  局部参数的均值（绝对值 ≥10 才算显著倾向）：")
        for name, value in sorted(local_means.items(), key=lambda kv: -abs(kv[1]))[:14]:
            flag = "  ← 显著" if abs(value) >= 10 else ""
            print(f"    {name:<30} {value:+.3f}{flag}")

    rules = SP.mask_usage_to_text_rules(summary)
    if rules:
        print("\n  由蒙版统计生成的 text_rules：")
        for rule in rules:
            print(f"    · {rule}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地风格统计预览：打印训练模式的差分结果，不调用 AI",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="XMP 文件或包含 XMP 的文件夹")
    parser.add_argument("--save-preview", action="store_true",
                        help="额外写出一份 style_preview.json（含统计与排除项，不含 AI 归纳的规则）")
    parser.add_argument("--out", default=None, help="预览 JSON 的路径（默认写到数据目录）")
    parser.add_argument("--style-name", default="preview", help="预览用的风格名")
    args = parser.parse_args(argv)

    files: list[Path] = []
    for path in args.paths:
        if path.is_file() and path.suffix.lower() == ".xmp":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.xmp")))
    files = sorted(set(files))

    if not files:
        print(f"[错误] 在 {args.paths} 下没有找到任何 .xmp 文件。")
        return 2

    print("=" * 100)
    print(f"本地风格统计预览 —— {len(files)} 个 XMP 样本")
    print("=" * 100)
    print("注意：本工具不调用任何 AI。它只运行 style_profile.py 的本地统计部分。")

    snapshots, failures = load_snapshots(files)
    if failures:
        print(f"\n[警告] {len(failures)} 个文件解析失败：")
        for item in failures[:10]:
            print(f"  - {item}")
    if not snapshots:
        print("[错误] 没有成功解析任何样本。")
        return 2

    # 逐样本差分
    per_sample_adjusted: list[dict[str, object]] = []
    exclusion_counts: Counter = Counter()
    exclusion_map: dict[str, dict[str, str]] = {}
    unknown_fields: Counter = Counter()

    for snapshot in snapshots:
        adjusted, excluded = SP.compute_adjustments(snapshot)
        per_sample_adjusted.append(adjusted)
        for entry in excluded:
            exclusion_counts[entry["field"]] += 1
            exclusion_map.setdefault(entry["field"], entry)
        for name in snapshot.get("unknown_fields") or []:
            unknown_fields[name] += 1

    # 批内不变项剔除（这些字段在全部样本中一致，故计数直接记为样本总数）
    per_sample_adjusted, invariant_exclusions = SP.apply_batch_invariance(per_sample_adjusted)
    for entry in invariant_exclusions:
        exclusion_counts[entry["field"]] = len(snapshots)
        exclusion_map.setdefault(entry["field"], entry)

    print_adjusted_report(per_sample_adjusted)
    print_excluded_report(exclusion_map, exclusion_counts, len(snapshots))
    print_mask_report(snapshots)

    ranges = SP.compute_param_ranges(per_sample_adjusted)
    print("\n" + "=" * 100)
    print(f"param_ranges 统计结果（{len(ranges)} 个字段）")
    print("=" * 100)
    print(f"{'字段':<34}{'count':>6}{'mean':>10}{'median':>10}{'min':>10}{'max':>10}{'std':>9}")
    print("-" * 100)
    for name, stat in sorted(ranges.items()):
        print(
            f"{name:<34}{stat['count']:>6}{stat['mean']:>10.2f}{stat['median']:>10.2f}"
            f"{stat['min']:>10.2f}{stat['max']:>10.2f}{stat['std']:>9.2f}"
        )

    if unknown_fields:
        print("\n[提示] 样本中出现过未登记的 crs 字段（它们会被忽略，且不会进入统计）：")
        for name, count in unknown_fields.most_common(20):
            print(f"  {name}（{count} 个文件）")
        print("  请运行 tools/dump_crs_fields.py 查看完整报告。")

    if args.save_preview:
        profile = SP.build_style_profile(
            name=args.style_name,
            snapshots=snapshots,
            model_output={"text_rules": [], "style_summary": "（本地预览，未经 AI 归纳）"},
        )
        # 默认写到**数据目录**而不是当前工作目录：在仓库根目录跑一次就会凭空多出
        # 一个 style_preview.json（还容易被误提交）。
        default_target = data_root() / "style_preview.json"
        target = Path(args.out).resolve() if args.out else default_target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出预览：{target}")

    print("\n完成。以上统计完全由本地计算，可复现、可审计。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
