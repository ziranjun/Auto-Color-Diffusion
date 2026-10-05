# -*- coding: utf-8 -*-
"""扫描真实 XMP，反推 crs: 字段清单 —— "漏字段门禁"。

用途（对应问题 c 的"如何验证字段名真实存在"）
--------------------------------------------
本脚本把指定目录下所有 XMP 的 crs: 字段**穷举**出来，输出：
    字段名 | 出现文件数 | 出现次数 | 观察到的值域/取值集合 | 是否已在 fields.py 登记

核心机制是**覆盖率门禁**：
    只要样本里出现了任何"未在 fields.py 登记"的 crs 字段，脚本就以
    非零退出码结束并打印这些字段。
    这样可以防止一个很隐蔽的事故：相机/ACR 版本升级后引入了新字段，
    而我们的字段表没跟上——那时 AI 会看不到（或写错）这些字段，
    表现是"程序一切正常，但某些调整就是不生效"。

用法
----
    python tools/dump_crs_fields.py test_data
    python tools/dump_crs_fields.py test_data --no-gate    # 只报告，不因未登记字段失败
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# 允许以 `python tools/xxx.py` 直接运行（把项目根加入 sys.path）
# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print 中文或 ✓/✗ 会抛
# UnicodeEncodeError —— 那会把"未通过清单"整段变成 traceback，等于把最关键的信息
# 藏起来（本项目真的踩到过）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.xmp import fields as F  # noqa: E402
from acb.xmp import reader as R  # noqa: E402


@dataclass
class Observation:
    """一个 crs 字段的观察结果。"""

    name: str
    file_count: int = 0
    total_count: int = 0
    values: set[str] = field(default_factory=set)
    numeric_values: list[float] = field(default_factory=list)
    registered: bool = False
    ai_writable: bool = False

    def value_summary(self) -> str:
        """值域摘要：数值型给范围，枚举型给取值集合（最多 6 个）。"""
        if self.numeric_values:
            lo = min(self.numeric_values)
            hi = max(self.numeric_values)
            if lo == hi:
                return f"{lo:g}（恒定）"
            return f"{lo:g} .. {hi:g}"
        shown = sorted(self.values)[:6]
        suffix = "…" if len(self.values) > 6 else ""
        return " / ".join(shown) + suffix


def find_xmp_files(paths: list[Path]) -> list[Path]:
    """递归找出所有 .xmp 文件。"""
    found: list[Path] = []
    for path in paths:
        if path.is_file() and path.suffix.lower() == ".xmp":
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.rglob("*.xmp")))
    return sorted(set(found))


def scan(files: list[Path]) -> tuple[dict[str, Observation], list[str]]:
    """扫描全部 XMP，返回 (字段观察表, 解析失败列表)。"""
    observations: dict[str, Observation] = {}
    failures: list[str] = []

    for path in files:
        try:
            doc = R.read_xmp_file(path)
            raw = doc.crs_raw()
        except Exception as exc:
            failures.append(f"{path}: {exc}")
            continue

        for name, text in raw.items():
            obs = observations.get(name)
            if obs is None:
                spec = F.get_field(name)
                obs = Observation(
                    name=name,
                    # 必须走 F.get_field 这个统一接口，而不是直接查 F.FIELDS：
                    # fields.py 里有一类"名字数量不固定"的动态字段
                    # （Table_xxx、UprightTransform_N），它们由前缀规则识别，
                    # 不在静态字典中。直接查字典会把它们误报为未登记。
                    registered=spec is not None or name in F.CURVE_FIELDS,
                    ai_writable=bool(spec and spec.ai_writable),
                )
                observations[name] = obs
            obs.file_count += 1
            obs.total_count += 1
            obs.values.add(text)
            try:
                obs.numeric_values.append(float(text))
            except ValueError:
                pass

    return observations, failures


def print_report(observations: dict[str, Observation], file_count: int) -> None:
    """打印人类可读的报告。"""
    registered = sorted((o for o in observations.values() if o.registered), key=lambda o: o.name)
    unknown = sorted((o for o in observations.values() if not o.registered), key=lambda o: o.name)

    print("=" * 110)
    print(f"扫描 {file_count} 个 XMP，共发现 {len(observations)} 个不同的 crs: 字段")
    print(f"  已登记：{len(registered)} 个    未登记：{len(unknown)} 个")
    print("=" * 110)

    header = f"{'字段名':<34}{'文件数':>7}{'出现次数':>9}  {'值域/取值':<40}{'AI可写'}"
    print(header)
    print("-" * 110)

    for obs in registered:
        spec = F.get_field(obs.name)
        panel = spec.panel if spec else "(曲线元素)"
        print(
            f"{obs.name:<34}{obs.file_count:>7}{obs.total_count:>9}  "
            f"{obs.value_summary():<40}{'是' if obs.ai_writable else '否'}   {panel}"
        )

    if unknown:
        print("\n" + "!" * 110)
        print("以下字段出现在真实样本中，但未在 acb/xmp/fields.py 登记：")
        print("!" * 110)
        for obs in unknown:
            print(f"  {obs.name:<34}文件数 {obs.file_count:>4}  值域 {obs.value_summary()}")
        print(
            "\n处理建议：\n"
            "  1. 如果是 AI 应当能调整的审美参数 -> 在 fields.py 中登记（含类型/值域/面板）；\n"
            "  2. 如果是机器/版本相关字段 -> 标记 ai_writable=False，只保留不生成；\n"
            "  3. 如果只是我们不认识的第三方扩展 -> 确认无需处理后，可只记录在 README 的附录里。\n"
            "为什么必须处理：ACR 对不认识的 crs 字段是**静默忽略**的，"
            "未登记字段会让 AI 的相关调整悄悄失效，且不留任何报错。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描真实 XMP 并校验 crs: 字段白名单覆盖率（漏字段门禁）",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="XMP 文件或包含 XMP 的文件夹")
    parser.add_argument("--no-gate", action="store_true",
                        help="即使存在未登记字段也返回 0（只报告不拦截）")
    args = parser.parse_args(argv)

    files = find_xmp_files(args.paths)
    if not files:
        print(f"[错误] 在 {args.paths} 下没有找到任何 .xmp 文件。")
        return 2

    print(f"找到 {len(files)} 个 XMP 文件。")
    observations, failures = scan(files)

    if failures:
        print(f"\n[警告] 有 {len(failures)} 个文件解析失败：")
        for item in failures[:10]:
            print(f"  - {item}")

    print_report(observations, len(files))

    unknown = [o for o in observations.values() if not o.registered]
    if unknown and not args.no_gate:
        print(f"\n[门禁失败] 存在 {len(unknown)} 个未登记字段，请按上面的建议处理。")
        return 1

    print("\n[门禁通过] 样本中的所有 crs: 字段均已登记。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
