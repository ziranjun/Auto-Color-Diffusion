# -*- coding: utf-8 -*-
"""读-改-写往返回归测试 —— 保护用户的蒙版数据。

为什么这是本项目最重要的一项自测
--------------------------------
真实 ACR 产物的 XMP 极重：
    IMG_7648.xmp  4152+ 行、11 个蒙版、含污点修复区域；
    IMG_4820.xmp  2073+ 行、含 AI 对象选择蒙版与多边形坐标。
这些内容承载用户数小时的手工修图成果。

本工具的核心承诺是"只改调色参数，其余一个字节都不碰"。
一旦某个 XML 处理细节出错（换错命名空间、丢掉嵌套 Seq、克隆节点时丢了属性），
用户的蒙版就会静默消失——而这是无法从"任务成功"的日志里看出来的。

因此本脚本做一件很具体的事：
    1. 读入一份真实 XMP，记录结构指纹（蒙版数、曲线点数、非 crs 属性集合等）；
    2. 施加一组测试参数（属性型 + 曲线型 + 白平衡联动）；
    3. 序列化后再读回来，重新计算指纹；
    4. 断言"不该变的都没变，该变的都变了"。

用法
----
    python tools/roundtrip_test.py test_data/IMG_7648.xmp
    python tools/roundtrip_test.py test_data            # 测试目录下所有 XMP
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print "✓/✗" 会抛
# UnicodeEncodeError —— 那会把"未通过清单"变成一段 traceback，等于把最关键的信息
# 藏起来（本文件真的踩到过这个坑）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.xmp import fields as F  # noqa: E402
from acb.xmp import namespaces as NS  # noqa: E402
from acb.xmp import reader as R  # noqa: E402
from acb.xmp import writer as W  # noqa: E402


@dataclass
class Fingerprint:
    """一份 XMP 的结构指纹。"""

    description_count: int = 0
    rdf_about: str | None = None
    # 非 crs 命名空间下的全部属性（这些是"绝不允许变化"的部分）
    non_crs_attributes: set[str] = field(default_factory=set)
    # crs 属性名集合
    crs_attribute_names: set[str] = field(default_factory=set)
    # 蒙版组里的 Correction 数量
    mask_correction_count: int = 0
    # 全部蒙版几何 li 的数量（深度嵌套，最容易被误改）
    mask_geometry_li_count: int = 0
    # 污点修复区域数量
    retouch_area_count: int = 0
    # 各曲线的点数
    curve_point_counts: dict[str, int] = field(default_factory=dict)
    # 元素型子节点的标签集合（用于确认没有丢节点）
    element_child_tags: set[str] = field(default_factory=set)

    def describe(self) -> str:
        return (
            f"Description 段={self.description_count}, rdf:about={self.rdf_about!r}, "
            f"非 crs 属性={len(self.non_crs_attributes)}, crs 属性={len(self.crs_attribute_names)}, "
            f"蒙版 Correction={self.mask_correction_count}, 蒙版几何 li={self.mask_geometry_li_count}, "
            f"污点修复={self.retouch_area_count}, 曲线={self.curve_point_counts}, "
            f"元素子节点={len(self.child_tags_sorted())}"
        )

    def child_tags_sorted(self) -> list[str]:
        return sorted(self.element_child_tags)


def fingerprint(doc: R.XmpDocument) -> Fingerprint:
    """计算结构指纹。"""
    fp = Fingerprint()
    fp.description_count = len(doc.descriptions)
    fp.rdf_about = doc.rdf_about

    for desc in doc.descriptions:
        for key in desc.attrib:
            if not key.startswith("{"):
                continue
            uri, _, local = key[1:].partition("}")
            if uri == NS.CRS_NS:
                fp.crs_attribute_names.add(local)
            elif uri == NS.RDF_NS:
                continue
            else:
                # 非 crs 属性（xmp:/tiff:/exif:/dc:/aux:/photoshop:/xmpMM:/crd: …）
                prefix = NS.URI_TO_PREFIX.get(uri, uri)
                fp.non_crs_attributes.add(f"{prefix}:{local}")
        for child in desc:
            prefix, local = NS.split_qname(child.tag)
            fp.element_child_tags.add(f"{prefix}:{local}")

    # 蒙版统计（含深层几何）
    stats = doc.mask_stats()
    fp.mask_correction_count = stats.total_corrections
    fp.retouch_area_count = stats.total_retouch_areas

    for desc in doc.descriptions:
        mg = desc.find(NS.qname("crs", "MaskGroupBasedCorrections"))
        if mg is not None:
            # 统计所有嵌套 li（多层 Seq 里的点坐标都算），这是最容易被误删的部分。
            fp.mask_geometry_li_count += sum(1 for _ in mg.iter(NS.LI_TAG))

    for name, points in doc.curves().items():
        fp.curve_point_counts[name] = len(points)

    return fp


# 测试用的参数集：覆盖属性型、浮点带符号、白平衡联动、曲线联动四种情况。
TEST_PARAMS: dict[str, object] = {
    "Exposure2012": 0.35,      # 浮点，验证 "+0.35" 格式
    "Contrast2012": 12,        # 正整数，验证 "+12" 格式
    "Highlights2012": -26,     # 负整数，验证无 + 号
    "Shadows2012": 24,
    "Vibrance": 18,            # 饱和度倾向
    "Saturation": 0,           # 恰好等于默认，不应被写坏
    "Clarity2012": 8,
    "HueAdjustmentOrange": -6,  # HSL 分区
    "ToneCurveName2012": "Linear",  # 会被曲线联动改成 Custom
    "ShadowTint": 3,
}

TEST_CURVES: dict[str, list] = {
    # 0..1 归一化域，writet 应映射到 0..255
    "ToneCurvePV2012": [[0.0, 0.0], [0.25, 0.22], [0.75, 0.80], [1.0, 1.0]],
}


def run_case(path: Path, verbose: bool = True) -> list[str]:
    """对单个 XMP 做往返测试，返回失败信息列表（空表示通过）。"""
    failures: list[str] = []

    print(f"\n{'=' * 100}")
    print(f"测试文件：{path}")
    print("=" * 100)

    # --- 原始指纹 ---
    original_raw = path.read_bytes()
    original = R.XmpDocument(original_raw, source=str(path))
    fp_before = fingerprint(original)
    print(f"改前指纹：{fp_before.describe()}")

    # --- 施加参数 ---
    data, applied, skipped, warnings = W.render_xmp(
        TEST_PARAMS, TEST_CURVES, original
    )
    print(f"已应用字段 {len(applied)} 个：{sorted(applied)}")
    if skipped:
        print(f"已跳过字段 {len(skipped)} 个：{sorted(skipped)}")
    for warning in warnings:
        print(f"  [警告] {warning}")

    # --- 重新解析 ---
    after = R.XmpDocument(data, source=f"{path}!<roundtrip>")
    fp_after = fingerprint(after)
    print(f"改后指纹：{fp_after.describe()}")

    # --- 断言 1：不该变的东西一个都不能变 ---
    if fp_before.description_count != fp_after.description_count:
        failures.append(
            f"rdf:Description 段数变化：{fp_before.description_count} -> {fp_after.description_count}"
        )

    if fp_before.rdf_about != fp_after.rdf_about:
        failures.append(f"rdf:about 变化：{fp_before.rdf_about!r} -> {fp_after.rdf_about!r}")

    lost_non_crs = fp_before.non_crs_attributes - fp_after.non_crs_attributes
    if lost_non_crs:
        failures.append(f"非 crs 属性丢失（{len(lost_non_crs)} 个）：{sorted(lost_non_crs)[:12]}")

    if fp_before.mask_correction_count != fp_after.mask_correction_count:
        failures.append(
            f"蒙版 Correction 数量变化：{fp_before.mask_correction_count} -> "
            f"{fp_after.mask_correction_count}（用户的局部调整被破坏！）"
        )

    if fp_before.mask_geometry_li_count != fp_after.mask_geometry_li_count:
        failures.append(
            f"蒙版几何节点数量变化：{fp_before.mask_geometry_li_count} -> "
            f"{fp_after.mask_geometry_li_count}（多边形坐标/手势数据被破坏！）"
        )

    if fp_before.retouch_area_count != fp_after.retouch_area_count:
        failures.append(
            f"污点修复区域数量变化：{fp_before.retouch_area_count} -> {fp_after.retouch_area_count}"
        )

    lost_tags = fp_before.element_child_tags - fp_after.element_child_tags
    if lost_tags:
        failures.append(f"元素型子节点丢失：{sorted(lost_tags)}")

    # 未在测试参数中涉及的曲线，其点数必须保持不变。
    for name, count in fp_before.curve_point_counts.items():
        if name in TEST_CURVES:
            continue
        if fp_after.curve_point_counts.get(name) != count:
            failures.append(
                f"未修改的曲线 {name} 点数变化：{count} -> {fp_after.curve_point_counts.get(name)}"
            )

    # --- 断言 2：该变的东西必须变了 ---
    after_params = after.typed_params()

    if abs(float(after_params.get("Exposure2012", 0)) - 0.35) > 1e-6:
        failures.append(f"Exposure2012 未正确写入：{after_params.get('Exposure2012')!r}")

    if int(after_params.get("Contrast2012", 0)) != 12:
        failures.append(f"Contrast2012 未正确写入：{after_params.get('Contrast2012')!r}")

    if int(after_params.get("Highlights2012", 0)) != -26:
        failures.append(f"Highlights2012 未正确写入：{after_params.get('Highlights2012')!r}")

    # 白平衡联动：写了 Temperature/Tint 就必须是 Custom。
    # 本测试用例没写 Temperature，因此这里只检查"没被误改成 Custom"。
    if "Temperature" not in TEST_PARAMS and "Tint" not in TEST_PARAMS:
        if after.crs_raw().get("WhiteBalance") == "Custom" and \
                original.crs_raw().get("WhiteBalance") != "Custom":
            failures.append("未提供 Temperature/Tint，但 WhiteBalance 被误改为 Custom")

    # 曲线联动：写了曲线点，ToneCurveName2012 必须是 Custom。
    if TEST_CURVES and after.crs_raw().get("ToneCurveName2012") != "Custom":
        failures.append(
            f"写入曲线后 ToneCurveName2012 未置为 Custom（实际 "
            f"{after.crs_raw().get('ToneCurveName2012')!r}）——ACR 会忽略我们写的曲线点"
        )

    new_points = after.curves().get("ToneCurvePV2012")
    expected = [(0, 0), (64, 56), (191, 204), (255, 255)]
    if new_points != expected:
        failures.append(f"ToneCurvePV2012 曲线点不正确：{new_points}（期望 {expected}）")

    # 原始文件必须没被改动（我们全程在内存里操作）。
    if path.read_bytes() != original_raw:
        failures.append("原始文件被意外修改（往返测试应当不影响源文件）")

    # --- 输出 ---
    if failures:
        print("\n[失败] 发现下列问题：")
        for item in failures:
            print(f"  ✗ {item}")
    else:
        print("\n[通过] 结构指纹完全保持，目标字段已正确写入。")

    return failures


def test_wb_linkage() -> list[str]:
    """专项测试：白平衡联动。

    实测 6/8 样本的 WhiteBalance 是 "As Shot"，而 ACR 在这种状态下
    **忽略** Temperature/Tint。因此这一条联动是"参数写了却不生效"的头号陷阱，
    值得单独测。
    """
    failures: list[str] = []
    print(f"\n{'=' * 100}")
    print("专项测试：白平衡联动（As Shot → Custom）")
    print("=" * 100)

    doc = R.XmpDocument(
        (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            f'    xmlns:crs="{NS.CRS_NS}"\n'
            '   crs:WhiteBalance="As Shot"\n'
            '   crs:Temperature="5500"\n'
            '   crs:Tint="+5"/>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
        ).encode("utf-8"),
        source="<wb-linkage>",
    )

    data, applied, _skipped, _warnings = W.render_xmp({"Temperature": 6200}, None, doc)
    after = R.XmpDocument(data, source="<wb-linkage-after>")
    raw = after.crs_raw()

    print(f"改后：WhiteBalance={raw.get('WhiteBalance')!r}, Temperature={raw.get('Temperature')!r}")
    if raw.get("WhiteBalance") != "Custom":
        failures.append("修改 Temperature 后 WhiteBalance 仍不是 Custom —— ACR 会忽略该数值")
    # 注意：比较的是**数值**而不是字符串。ACR 的书写风格是正整数带显式 + 号
    # （样本实测 Tint="+16"、Vibrance="+28"），因此这里写出的原样是 "+6200"。
    # 早期版本的断言写了 != "6200"，把这个正确行为误判为失败。
    try:
        written_temp = float(raw.get("Temperature", "0"))
    except ValueError:
        written_temp = -1.0
    if abs(written_temp - 6200.0) > 1e-6:
        failures.append(f"Temperature 未正确写入：原始字符串 {raw.get('Temperature')!r}（期望数值 6200）")
    if "WhiteBalance" not in applied:
        failures.append("applied 列表中没有记录 WhiteBalance 的联动修改")

    if failures:
        for item in failures:
            print(f"  ✗ {item}")
    else:
        print("  [通过] 白平衡联动正确。")
    return failures


def test_machine_field_rejection() -> list[str]:
    """专项测试：机器相关字段必须被拒收（AI 不允许产出）。"""
    failures: list[str] = []
    print(f"\n{'=' * 100}")
    print("专项测试：机器相关字段拒收与未知字段拒收")
    print("=" * 100)

    from acb.xmp.validator import validate_params

    verdict = validate_params({
        "crs:Exposure2012": 0.2,
        "crs:ProcessVersion": "15.4",     # 机器字段，应被拒
        "crs:CameraProfile": "Adobe Color",  # 机器字段，应被拒
        "crs:NotARealField": 5,            # 未登记，应被拒
    })
    print(f"校验结果：{verdict.summary()}")
    if verdict.ok:
        failures.append("非法字段未被拦截（校验竟然通过了）")
    if len(verdict.errors) < 3:
        failures.append(f"期望至少 3 条错误，实际 {len(verdict.errors)} 条：{verdict.errors}")

    verdict2 = validate_params({"crs:Exposure2012": 0.2, "crs:Contrast2012": 15})
    print(f"合法输入校验：{verdict2.summary()}")
    if not verdict2.ok:
        failures.append(f"合法输入被误拒：{verdict2.errors}")

    if failures:
        for item in failures:
            print(f"  ✗ {item}")
    else:
        print("  [通过] 非法字段被拒、合法字段通过。")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="XMP 读-改-写往返回归测试（保护用户蒙版数据）",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="XMP 文件或包含 XMP 的文件夹")
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

    all_failures: list[str] = []
    for path in files:
        try:
            failures = run_case(path)
        except Exception as exc:
            import traceback

            failures = [f"测试执行异常：{exc}\n{traceback.format_exc()}"]
        all_failures.extend(f"{path.name}: {item}" for item in failures)

    # 两个不依赖具体文件的专项测试
    try:
        all_failures.extend(f"白平衡联动: {item}" for item in test_wb_linkage())
    except Exception as exc:
        all_failures.append(f"白平衡联动测试异常：{exc}")

    try:
        all_failures.extend(f"字段拒收: {item}" for item in test_machine_field_rejection())
    except Exception as exc:
        all_failures.append(f"字段拒收测试异常：{exc}")

    print(f"\n{'=' * 100}")
    if all_failures:
        print(f"[总体失败] 共 {len(all_failures)} 项问题：")
        for item in all_failures:
            print(f"  ✗ {item}")
        return 1
    print(f"[总体通过] {len(files)} 个文件 + 2 项专项测试全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
