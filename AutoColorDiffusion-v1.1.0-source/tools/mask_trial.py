# -*- coding: utf-8 -*-
"""蒙版 A/B 对照试验：同一张照片两份拷贝，只差"有没有蒙版"。

为什么改成 A/B（上一版为何白做）
--------------------------------
上一版把拷贝重名成 IMG_2509.CR3 放在桌面，结果你在 ACR 里打开的其实是
z:\\ACR Coding\\test_data\\IMG_2509.CR3（同名），看到的当然是你自己那 6 个旧蒙版。
所以这一版：
  · 拷贝用**完全不同的文件名**（MASKTEST_ 开头）；
  · 换一张**侧车里本来就没有蒙版**的照片（_G4A1392），旧蒙版无处可来；
  · 同时出 A / B 两份，只差"有没有蒙版"，方便对照组：
        MASKTEST_A_global.CR3       —— 只写了两个很显眼的全局参数，**没有蒙版**
        MASKTEST_B_global_masks.CR3 —— 同样两个全局参数 **+ 三个测试蒙版**

两份的底子都是你自己的 _G4A1392.xmp（你的全局调色、相机配置全保留），
区别只有：A 无蒙版、B 有三个蒙版；全局参数两份**完全一样**。

判读方法（关键）
----------------
  A. 先看 A：画面应明显变亮（+2EV）且明显变艳（+70）
       → 变了 ＝ 侧车确实被 ACR 读了，问题只在蒙版；
       → 没变 ＝ 侧车根本没被读（那就是另一类问题，我去查路径/命名/缓存）。
  B. 再看 B（三个测试蒙版）：
       ① 测试-线性渐变  左半大幅压暗 −1.2EV（一大块明显变暗）
       ② 测试-径向渐变  圈内提亮 +1.0EV（羽化 88）
       ③ 测试-画笔      一条横贯画面的粗笔触，提亮 +1.5EV + 加饱和（应有亮带）
       → 蒙版面板里应正好只有这三个，名字带①②③。

用法
----
    .venv\\Scripts\\python.exe tools\\mask_trial.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print 中文会抛
# UnicodeEncodeError，把结果变成一段 traceback。统一把标准输出切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

from acb.paths import data_root  # noqa: E402
from acb.raw.exiftool import ExiftoolRunner  # noqa: E402
from acb.raw.orientation import read_orientation  # noqa: E402
from acb.xmp import fields as F  # noqa: E402
from acb.xmp import masks as masks_mod  # noqa: E402
from acb.xmp import namespaces as NS  # noqa: E402
from acb.xmp import writer as xmp_writer  # noqa: E402

DEFAULT_RAW = ROOT / "test_data" / "IMG_2509.CR3"
DEFAULT_BASE = ROOT / "test_data" / "IMG_2509.xmp"
# 默认放到**数据目录**里（原来是写死的桌面路径，换机器/换用户名就跑不了）。
# 用户可以 --out 覆盖。
DEFAULT_OUT = data_root() / "mask_trial"

# 全局参数：**不动**（A、B 两份的全局完全一样，只差蒙版），
# 这样你切来切去时唯一的变化就是蒙版本身。
TRIAL_PARAMS: dict[str, object] = {}

# 三个测试蒙版：名字带编号、效果给得足（**面板值**，就是你 ACR 面板上看到的那个数）。
BRUSH_DABS: list[tuple[float, float]] = [
    (round(0.12 + 0.76 * i / 39, 6), round(0.62 + 0.02 * ((i % 5) - 2) / 2, 6)) for i in range(40)
]

TRIAL_MASKS: list[dict] = [
    {
        "type": "linear",
        "name": "① 测试-线性渐变",
        "correction_name": "① 测试-线性渐变",
        "zero": (0.665335, 0.545150),
        "full": (0.204391, 0.524827),
        "local": {
            "LocalExposure2012": -1.20,
            "LocalHighlights2012": -40,
            "LocalSaturation": -20,
        },
    },
    {
        "type": "radial",
        "name": "② 测试-径向渐变",
        "correction_name": "② 测试-径向渐变",
        "rect": (0.038784, 0.118224, 0.934566, 0.443148),
        "angle": 0,
        "midpoint": 50,
        "roundness": 0,
        "feather": 88,
        "flipped": True,
        "local": {
            "LocalExposure2012": 1.00,
            "LocalTexture": 30,
        },
    },
    {
        "type": "brush",
        "name": "③ 测试-画笔",
        "correction_name": "③ 测试-画笔",
        "radius": 0.05,
        "flow": 0.85,
        "center_weight": 0.0,
        "dabs": BRUSH_DABS,
        "local": {
            "LocalExposure2012": 1.50,
            "LocalSaturation": 50,
        },
    },
]

# ④ 用"显示帧"坐标输入（人眼看到的方向：天空在上方），由程序自动换算成 ACR 的存储帧。
# 意图：压暗画面**顶部那条天空**（从顶部 2% 至 28% 之间渐变）。
# 它在 ACR 里应该落在**天空**上 —— 如果落到了左边/右边，说明坐标帧换算写反了。
DISPLAY_MASK: dict = {
    "type": "linear",
    "name": "④ 测试-天空顶高（显示帧换算）",
    "correction_name": "④ 测试-天空顶高（显示帧换算）",
    "zero": (0.5, 0.28),
    "full": (0.5, 0.02),
    "local": {
        "LocalExposure2012": -1.00,
        "LocalSaturation": -30,
    },
}


def _attr_names(element) -> set[str]:
    """取元素的属性名（去掉命名空间）。"""
    return {key.split("}")[-1] for key in element.attrib}


def _baseline_sample() -> Path | None:
    """找一份"带着你手工蒙版"的侧车当比对基准。

    优先 test_data/IMG_2509.xmp；若它的蒙版已被手动删除，就回退到我们写入前
    自动备份的那份（backups/xmp/IMG_2509.*.xmp，里面有你原始的 5 组局部调整）。
    """
    candidates = [ROOT / "test_data" / "IMG_2509.xmp"]
    backups = data_root() / "backups" / "xmp"
    if backups.is_dir():
        with_masks = [p for p in sorted(backups.glob("IMG_2509.*.xmp"), reverse=True)
                      if "<crs:MaskGroupBasedCorrections" in p.read_text(encoding="utf-8", errors="replace")]
        candidates.extend(with_masks)
    for path in candidates:
        if path.is_file() and "<crs:MaskGroupBasedCorrections" in path.read_text(encoding="utf-8", errors="replace"):
            return path
    return None


def _sample_correction_attrs(sample: Path) -> set[str] | None:
    """从用户样本里取一组"只有基础段"的局部调整属性集合，用于逐项比对。"""
    import xml.etree.ElementTree as ET

    if not sample.is_file():
        return None
    text = sample.read_text(encoding="utf-8", errors="replace")
    for open_tag, close_tag in ((text.find("<?xpacket"), text.rfind("?>")),):
        if open_tag >= 0 and close_tag > open_tag:
            text = text[:open_tag] + text[close_tag + 2 :]
    root = ET.fromstring(text.strip())
    for correction in root.iter(NS.DESCRIPTION_TAG):
        if correction.get(NS.qname("crs", "What")) != "Correction":
            continue
        names = _attr_names(correction)
        if not any(n.startswith("LocalColorGrade") for n in names):
            return names
    return None


def verify(sidecar: Path) -> bool:
    """打开刚写出的侧车，检查蒙版结构与字段，返回是否全部通过。"""
    from acb.xmp import namespaces as NS

    ok = True
    doc = xmp_writer.XmpDocument(sidecar.read_bytes(), source=str(sidecar))
    primary = xmp_writer._primary_description(doc)
    group = primary.find(NS.qname("crs", "MaskGroupBasedCorrections"))
    if group is None:
        print("  ✗ 侧车里没有找到 crs:MaskGroupBasedCorrections")
        return False

    baseline = _sample_correction_attrs(_baseline_sample() or Path("（无）"))
    corrections = group.find(NS.SEQ_TAG).findall(NS.LI_TAG)
    expects = ["Mask/Gradient", "Mask/CircularGradient", "Mask/Aggregate", "Mask/Gradient"]
    print(f"  · 局部调整组数 = {len(corrections)}（预期恰好 {len(expects)}：①②③④）")
    ok = ok and len(corrections) == len(expects)
    for index, correction in enumerate(corrections):
        desc = correction.find(NS.DESCRIPTION_TAG)
        masks = desc.find(NS.qname("crs", "CorrectionMasks")).find(NS.SEQ_TAG).findall(NS.LI_TAG)
        holder = masks[0] if masks[0].get(NS.qname("crs", "What")) else masks[0].find(NS.DESCRIPTION_TAG)
        what = holder.get(NS.qname("crs", "What"))
        names = _attr_names(desc)
        local_attrs = sorted(n for n in names if n.startswith("Local"))
        moved = [k for k in local_attrs if desc.get(NS.qname("crs", k)) not in ("0", "100")]
        good = what == expects[index] and len(masks) == 1
        ok = ok and good
        print(
            f"  · 第 {index + 1} 组：{'✓' if good else '✗'} 蒙版={what} "
            f"名称={holder.get(NS.qname('crs', 'MaskName'))!r} 属性 {len(names)} 个、"
            f"非缺省局部参数 {len(moved)} 个 {moved}"
        )
        if baseline is not None:
            extra = names - baseline
            missing = baseline - names
            same = not extra and not missing
            ok = ok and same
            print(
                f"      与样本逐项比对：{'✓ 属性集合完全一致' if same else '✗ 差异'}"
                + (f"｜多出 {sorted(extra)}" if extra else "")
                + (f"｜缺少 {sorted(missing)}" if missing else "")
            )
        if what == "Mask/Aggregate":
            paint = holder.find(NS.qname("crs", "Masks")).find(NS.SEQ_TAG).find(NS.LI_TAG).find(NS.DESCRIPTION_TAG)
            dabs = paint.find(NS.qname("crs", "Dabs")).find(NS.SEQ_TAG).findall(NS.LI_TAG)
            lines = [d.text for d in dabs]
            print(f"      画笔：落点数据行 {len(lines)} 条，首 4 条 = {lines[:4]}")
            ok = ok and all(str(x).startswith(("r ", "f ", "d ")) for x in lines)
            # 样本的写法是逐点成对：每点 1 个 r + 1 个 d，外加 1 个 f
            ok = ok and len(lines) == len(BRUSH_DABS) * 2 + 1

    for name in TRIAL_PARAMS:
        if F.get_field(name) is None:
            print(f"  ✗ 全局参数字段未登记：{name}")
            ok = False
    print(f"  · 全局参数 {len(TRIAL_PARAMS)} 个均已在字段表登记")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="蒙版 A/B 对照试验（A 只全局 / B 全局+蒙版）")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW, help="源 RAW（原件不会被改动）")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE, help="作为底子的侧车（你的设置，保留）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="试验文件夹")
    args = parser.parse_args()

    raw_src: Path = args.raw
    if not raw_src.is_file():
        print(f"找不到源 RAW：{raw_src}")
        return 2
    base_src: Path = args.base
    if not base_src.is_file():
        print(f"找不到作为底子的侧车：{base_src}")
        return 2

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    exiftool = ExiftoolRunner()

    jobs = (
        ("MASKTEST_A_noMasks", False, "你的全局设置原样，**没有蒙版**（对照用）"),
        ("MASKTEST_B_masks", True, "你的全局设置 + 四个测试蒙版 ①②③④"),
    )
    results: list[tuple[str, Path, bool]] = []
    ok = True
    for stem, want_masks, note in jobs:
        raw_copy = out_dir / f"{stem}.CR3"
        sidecar = out_dir / f"{stem}.xmp"
        if raw_copy.exists():
            raw_copy.unlink()
        if sidecar.exists():
            sidecar.unlink()
        shutil.copy2(raw_src, raw_copy)
        # 侧车用**你原来的**那份（里本来就没有蒙版），只改全局参数 + 可选蒙版。
        shutil.copy2(base_src, sidecar)
        print(f"\n=== {stem} ===\n  说明：{note}")

        masks = None
        if want_masks:
            photo = read_orientation(raw_copy, exiftool)
            print(f"  这张照片的 EXIF 方向 = {photo.label!r}（{photo.source}，显示时旋转 {photo.rotation}°）")
            # ①②③ 的坐标原本是从你侧车里抄的**存储帧**值，先转成显示帧；
            # 再交给 writer 自动换算回存储帧 —— 这样走的就是批量将要走的同一条路。
            display_specs = [masks_mod.stored_to_display_spec(s, photo.label) for s in TRIAL_MASKS]
            display_specs.append(DISPLAY_MASK)
            print("  往返自检（显示帧 → 存储帧，应与原值一致）：")
            for original, display_spec in zip(TRIAL_MASKS, display_specs):
                back = masks_mod.display_to_stored_spec(display_spec, photo.label)
                same = (back.get("zero") == original.get("zero")
                        and back.get("full") == original.get("full")
                        and back.get("rect") == original.get("rect"))
                print(f"    {original.get('name')}: 原 {original.get('zero') or original.get('rect')}"
                      f" → 回 {back.get('zero') or back.get('rect')} {'✓' if same else '✗'}")
                ok = ok and same
            print(f"  ④ 显示帧 {DISPLAY_MASK['zero']} → {DISPLAY_MASK['full']}"
                  f" 换算后 = {masks_mod.display_to_stored_spec(DISPLAY_MASK, photo.label)['zero']}"
                  f" → {masks_mod.display_to_stored_spec(DISPLAY_MASK, photo.label)['full']}")
            masks = display_specs

        result = xmp_writer.write_sidecar(
            raw_copy,
            TRIAL_PARAMS,
            None,
            camera_profile=None,
            profile_note="侧车已存在，保持原有相机配置",
            masks=masks,
            orientation=(photo.label if want_masks else None),
            masks_frame="display",
        )
        print(f"  已写 → {result.target}")
        print(f"  处理动作：{result.applied_fields}")
        for warning in result.warnings:
            print(f"  ⚠ {warning}")

        if want_masks:
            print("  自检：")
            ok = verify(result.target) and ok
        results.append((stem, raw_copy, want_masks))

    print("\n=== exiftool 校验 ===")
    if not getattr(exiftool, "available", False):
        # exiftool 不可用时 status.path 是 None，str(None) 会让 subprocess 直接抛
        # FileNotFoundError（而且是在两批文件都写完之后才崩）。
        print("  跳过：没有找到 exiftool。")
        return
    for stem, _raw, _masks in results:
        sidecar = out_dir / f"{stem}.xmp"
        proc = subprocess.run(
            [str(exiftool.status.path), "-validate", "-warning", "-a", str(sidecar)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        notes = [line.strip() for line in proc.stdout.splitlines()
                 if line.strip().startswith(("Warning", "Error"))]
        print(f"  [{stem}] {len(notes)} 条提示"
              + (f"，例如：{notes[0][:110]}" if notes else ""))

    print("\n=== 请你在 ACR 里打开这两张（同一个文件夹里，名字不一样）===")
    for stem, raw_copy, _masks in results:
        print(f"    {raw_copy}")
    print("你的原照片、原侧车都没有被改动。")
    print("判读：A 是你原来的样子；B 多出来的东西就是这四个蒙版：")
    print("  ① 线性渐变（你原来那个位置）：左半压暗 -1.2EV + 高光 -40（面板值）")
    print("  ② 径向渐变（你原来那个位置，羽化 88）：圈内 +1.00EV + 纹理 +30")
    print("  ③ 画笔：一条粗笔触（40 个落点 / 半径 0.05 / 流量 0.85），+1.50EV + 饱和 +50")
    print("  ④ 用显示帧坐标写的'压暗顶部天空'，程序自动换算成存储帧 => 应该落在天空上")
    print("重点：③ 上次在 ACR 里根本没出现（已按你样本的嵌套写法修）；")
    print("     ④ 用来验证坐标帧换算（若落在左侧而不是天空，就是换算方向错了）。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
