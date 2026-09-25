"""给已经写出的 XMP「补写处理版本（ProcessVersion）」—— 不重新请求 AI，只改这一个字段。

【为什么需要这个工具（用户 2026-09-24 的问题）】
他在 ACR 里打开程序写的文件，校准面板的「处理版本」显示「1 版」并问为什么不是 6 版。
根因：旧代码**刻意不写** crs:ProcessVersion（以为“留空 = ACR 用当前默认版本”），
而 ACR 在缺这个字段时按**最老的 Version 1 (2003)** 解释整包设置 ——
2003 引擎不认高光/阴影/白色/黑色/纹理/去薄雾/颜色分级，蒙版（需 PV4+）更不会生效。
也就是说：那批文件以前即使参数写对了，ACR 也没有按 2012 那套去渲染。

要看清“参数到底对不对”，不必重新花 API 调一遍 —— 把处理版本补上、再让 Photoshop
重新导出一次即可（导出走的是本地 XMP，不花钱）。这个工具就是做这件事的。

行为（刻意保守）：
  · **默认演练**：只打印哪些文件缺该字段，一个字节都不写；加 --apply 才真写；
  · 已有 ProcessVersion 的文件**一律不动**（那是你 ACR 的事实，不是我们能改的）；
  · 侧车覆盖前先备份到数据目录下的 backups/xmp（与管线里的侧车写盘同一条规矩）；
  · DNG 走「读出内嵌包 → 改 → 写回文件内部」，exiftool 会另留一份 *_original。

用法：
  python tools/patch_process_version.py <文件或目录> [...]           # 演练（默认）
  python tools/patch_process_version.py --apply <文件或目录> [...]   # 真的写入
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 管道默认是 GBK：中文输出会被打成乱码，用户看到的就是一片问号。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.constants import is_supported_raw  # noqa: E402
from acb.raw.exiftool import ExiftoolRunner  # noqa: E402
from acb.xmp import dng as dng_mod  # noqa: E402
from acb.xmp import reader as R  # noqa: E402
from acb.xmp import writer as W  # noqa: E402


def _try_apply_sidecar(target: Path, *, apply: bool) -> str:
    """侧车：缺处理版本就补上；返回一句人话（用于汇总）。"""
    try:
        raw_bytes = target.read_bytes()
        doc = R.XmpDocument(raw_bytes, source=str(target))
    except Exception as exc:  # noqa: BLE001 —— 单个文件的问题不该中断整批
        return f"跳过（无法解析）：{exc}"

    if _has_process_version(doc):
        return "已有处理版本，跳过"

    if not apply:
        return f"缺处理版本 → 将补写 {W.DEFAULT_PROCESS_VERSION}"

    W.ensure_process_version(doc)
    data = W.serialize(doc)
    backup = W._backup_before_overwrite(target, raw_bytes)  # noqa: SLF001 —— 与管线共用同一套备份规矩
    tmp = target.with_name(target.name + ".acbtmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    note = f"已补写 {W.DEFAULT_PROCESS_VERSION}"
    if backup is not None:
        note += f"（原文件已备份 → {backup}）"
    return note


def _has_process_version(doc: R.XmpDocument) -> bool:
    ns = W.NS.qname("crs", "ProcessVersion")
    return any(desc.get(ns) for desc in doc.descriptions)


def _try_apply_dng(target: Path, exiftool: ExiftoolRunner, *, apply: bool) -> str:
    """DNG：改内嵌 XMP 包（同样的“缺才补”规矩）。"""
    if not exiftool.available:
        return "跳过（DNG 需要 exiftool，当前不可用）"
    embedded = dng_mod.read_embedded(target, exiftool)
    if not embedded:
        return "跳过（没有内嵌 XMP）"
    try:
        doc = R.XmpDocument(embedded, source=f"{target.name}!<embedded-xmp>")
    except Exception as exc:  # noqa: BLE001
        return f"跳过（内嵌 XMP 无法解析）：{exc}"

    if _has_process_version(doc):
        return "已有处理版本，跳过"
    if not apply:
        return f"缺处理版本 → 将补写 {W.DEFAULT_PROCESS_VERSION}（写回文件内部）"

    W.ensure_process_version(doc)
    ok, note = exiftool.write_xmp_packet(target, W.serialize(doc))
    return f"已补写 {W.DEFAULT_PROCESS_VERSION}" if ok else f"写回失败：{note}"


def collect_targets(paths: list[Path]) -> list[Path]:
    """把用户给的文件/目录展开成“侧车 .xmp 或 RAW”清单（与原片同名的侧车优先）。"""
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(p for p in sorted(path.rglob("*")) if p.is_file())
        else:
            print(f"（路径不存在，已忽略）{path}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="给已有 XMP 补写 crs:ProcessVersion（默认演练，加 --apply 才写）",
    )
    parser.add_argument("paths", nargs="+", help="文件或目录（可多个）")
    parser.add_argument("--apply", action="store_true", help="真的写入（默认只演练）")
    args = parser.parse_args(argv)

    exiftool = ExiftoolRunner()
    targets = collect_targets([Path(p) for p in args.paths])
    xmps = [p for p in targets if p.suffix.lower() == ".xmp"]
    raws = [p for p in targets if is_supported_raw(p.name)]

    mode = "**写入模式**" if args.apply else "演练模式（一个字节都不写）"
    print(f"{mode}：找到 {len(xmps)} 个侧车、{len(raws)} 个 RAW。")
    if not args.apply:
        print("（想看实际结果就加 --apply；侧车会先备份到数据目录的 backups/xmp）")
    print()

    changed = 0
    failures = 0
    for path in xmps:
        note = _try_apply_sidecar(path, apply=args.apply)
        print(f"  [侧车] {path.name}：{note}")
        changed += 1 if note.startswith(("已补写", "将补写")) else 0
        failures += 1 if note.startswith("写回失败") else 0
    for path in raws:
        if not W.is_embedded_mode(path):
            sidecar = W.sidecar_path_for(path)
            if not sidecar.is_file():
                continue  # 没有侧车的 RAW：ACR 会用自己的默认，我们不凭空造文件
            print(f"  [侧车] {sidecar.name}（{path.name} 的设置文件）已在上面的侧车里")
            continue
        note = _try_apply_dng(path, exiftool, apply=args.apply)
        print(f"  [DNG ] {path.name}：{note}")
        changed += 1 if note.startswith(("已补写", "将补写")) else 0
        failures += 1 if note.startswith("写回失败") else 0

    verb = "已补写" if args.apply else "需要补写"
    print(f"\n共 {changed} 个文件{verb}处理版本。")
    if changed and not args.apply:
        print("确认后加 --apply 重跑一次即可；补完让 Photoshop 重新导出一次（不花 API 钱）。")
    # 退出码语义与其它门禁脚本一致（find_xmp_files / roundtrip / verify_xmp 都是 0/1/2）：
    # 有写回失败 → 1（自动化里能察觉）；一个目标都没找到 → 2（多半是路径给错了，
    # 以前这种情况会返回 0，看起来像"成功且无需补写"）。演练模式下"需要补写"不算失败。
    if failures:
        print(f"[失败] 有 {failures} 个文件写回失败，详见上面各行。")
        return 1
    if not xmps and not raws:
        print("[失败] 没有找到任何侧车或 RAW（检查一下路径）。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
