# -*- coding: utf-8 -*-
"""用 exiftool 校验 XMP/RAW 的元数据一致性。

用途
----
程序写出 XMP 后，用 exiftool 的 -validate 读回检查：
    - XMP 包结构是否合法（能不能被 exiftool 解析）；
    - 有没有标签级警告（例如"Wrong format for XMP-crs:Exposure2012"）；
    - 写回 DNG 内嵌 XMP 后，TIFF 结构与 XMP 是否仍然自洽。

这一步能抓住一类很隐蔽的问题：**我们写出的 XMP 在 Python 侧能读回来，
但 exiftool / ACR 认不出**（例如值格式不符合 XMP 类型定义）。
Python 的 ElementTree 只校验 XML 合法性，不校验 XMP 的类型约束，所以必须靠 exiftool 兜这一层。

用法
----
    python tools/verify_xmp.py test_data/IMG_4820.xmp
    python tools/verify_xmp.py test_data                 # 目录下所有 .xmp 与 RAW
    python tools/verify_xmp.py --dump test_data/IMG_4820.xmp   # 顺带打印关键 crs 字段
"""

from __future__ import annotations

import argparse
import sys
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

from acb.constants import RAW_EXTENSIONS  # noqa: E402
from acb.raw.exiftool import ExiftoolRunner  # noqa: E402
from acb.xmp import reader as R  # noqa: E402

# --dump 时展示的关键字段（覆盖各面板的代表性字段，便于人工核对是否落值）。
DUMP_FIELDS = [
    "WhiteBalance",
    "Temperature",
    "Tint",
    "Exposure2012",
    "Contrast2012",
    "Highlights2012",
    "Shadows2012",
    "Whites2012",
    "Blacks2012",
    "Vibrance",
    "Saturation",
    "Clarity2012",
    "ToneCurveName2012",
    "HueAdjustmentOrange",
    "LuminanceAdjustmentOrange",
    "SplitToningShadowHue",
    "ColorGradeMidtoneSat",
    "LensProfileEnable",
    "PerspectiveUpright",
    "CameraProfile",
    "ProcessVersion",
]


def collect_targets(paths: list[Path]) -> list[Path]:
    """收集要校验的文件（.xmp 与白名单 RAW）。"""
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix == ".xmp" or suffix in RAW_EXTENSIONS:
                found.append(path)
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if not item.is_file():
                    continue
                suffix = item.suffix.lower()
                if suffix == ".xmp" or suffix in RAW_EXTENSIONS:
                    found.append(item)
    return sorted(set(found))


def dump_fields(path: Path) -> None:
    """打印关键 crs 字段的落值情况（人工核对用）。"""
    print(f"\n  --- 关键字段落值（{path.name}）---")

    # .xmp 直接解析；RAW/DNG 走 exiftool 读内嵌 XMP。
    try:
        if path.suffix.lower() == ".xmp":
            doc = R.read_xmp_file(path)
        else:
            exiftool = ExiftoolRunner()
            if not exiftool.available:
                print("    (需要 exiftool 才能读取 RAW/DNG 的内嵌 XMP)")
                return
            packet = exiftool.read_xmp_packet(path)
            if not packet:
                print("    (该文件没有内嵌 XMP)")
                return
            doc = R.XmpDocument(packet, source=f"{path.name}!<embedded>")
    except Exception as exc:
        print(f"    (解析失败：{exc})")
        return

    raw = doc.crs_raw()
    typed = doc.typed_params()
    for name in DUMP_FIELDS:
        if name not in raw:
            continue
        value = typed.get(name, raw[name])
        print(f"    {name:<32} = {value!r}   (原始字符串 {raw[name]!r})")

    unknown = doc.unknown_crs_fields()
    if unknown:
        print(f"    [注意] 存在 {len(unknown)} 个未登记字段：{unknown[:10]}")

    curves = doc.curves()
    if curves:
        print(f"    曲线：{ {k: len(v) for k, v in curves.items()} }")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 exiftool 校验 XMP 元数据一致性")
    parser.add_argument("paths", nargs="+", type=Path, help="XMP 文件、RAW 文件或目录")
    parser.add_argument("--dump", action="store_true", help="顺带打印关键 crs 字段的落值")
    args = parser.parse_args(argv)

    exiftool = ExiftoolRunner()
    if not exiftool.available:
        print("[无法继续] " + exiftool.status.install_guide())
        return 2

    print(f"使用 exiftool {exiftool.status.version}（{exiftool.status.path}）")

    targets = collect_targets(args.paths)
    if not targets:
        print(f"[错误] 在 {args.paths} 下没有找到任何 .xmp 或白名单 RAW 文件。")
        return 2

    total_problems = 0
    problem_files: list[tuple[Path, list[str]]] = []

    for path in targets:
        problems = exiftool.validate_file(path)
        if problems:
            total_problems += len(problems)
            problem_files.append((path, problems))
            print(f"[问题] {path.name}：{len(problems)} 条")
            for line in problems[:10]:
                print(f"      {line}")
        else:
            print(f"[通过] {path.name}")

        if args.dump:
            dump_fields(path)

    print("\n" + "=" * 80)
    if problem_files:
        print(f"[总体] {len(problem_files)}/{len(targets)} 个文件存在问题，共 {total_problems} 条。")
        print(
            "\n排查建议：\n"
            "  - 若提示 XMP 格式错误：检查 acb/xmp/fields.py 中该字段的 kind 是否与 ACR 期望一致\n"
            "    （布尔必须写 True/False，整数不能带小数点，曲线点必须是 0..255 整数）；\n"
            "  - 若提示 TIFF/IFD 结构错误（多出现在 DNG）：说明写回内嵌 XMP 时结构被破坏，\n"
            "    请用同目录下的 <原文件名>_original 备份恢复（exiftool 写回时自动生成）；\n"
            "  - 若只是 Warning 且涉及我们不写入的标签：通常可忽略，但建议记录到 README。"
        )
        return 1

    print(f"[总体] {len(targets)} 个文件全部通过校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
