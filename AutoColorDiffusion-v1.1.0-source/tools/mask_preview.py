# -*- coding: utf-8 -*-
"""蒙版预览：不用 ACR，把侧车里的蒙版直接叠在照片上出 JPEG，先自己看一眼。

【为什么做它】用户 2026-09-23 的要求：「你可以自己测试完导出来看看」。
ACR 没有命令行接口，没法自动验收；所以这里用 rawpy 解出照片，按我们对 ACR
**蒙版语义**的理解把覆盖度算出来，导出"原图 vs 蒙版叠加+效果模拟"的对照图。

它是"位置/范围"的检查工具，**不是** ACR 的像素级复刻：
  · 羽化曲线、局部滑块的真实作用都与 ACR 有差异（这里只做近似）；
  · 几何（线性渐变的轴向、径向渐变的椭圆、画笔落点）是照侧车里写死的数值算的，
    所以"蒙版盖在哪里"是可信的。

用法：
    .venv\\Scripts\\python.exe tools\\mask_preview.py --sidecar 某侧车.xmp
    .venv\\Scripts\\python.exe tools\\mask_preview.py --sidecar a.xmp b.xmp --raw 某照片.CR3 --out D:\\预览
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

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

from acb.raw.exiftool import ExiftoolRunner  # noqa: E402
from acb.raw.orientation import read_orientation  # noqa: E402
from acb.xmp import namespaces as NS  # noqa: E402
from acb.xmp.masks import _LOCAL_SCALES  # noqa: E402

# 方向旋转表不再自己维护：一律走 acb.raw.orientation（那边有单测守着的换算规则），
# exiftool 的路径也交给 ExiftoolRunner 去发现（exe 同目录 _internal/ → PATH）。


# --------------------------------------------------------------------------- 解码

def decode_raw(raw_path: Path, half: bool = True) -> tuple[Image.Image, int]:
    """解码 RAW，返回 (存储帧图像, 为了让画面正立需要旋转的角度)。

    【关键】ACR 的蒙版坐标是写在 **存储帧（未按 EXIF 旋转的那个方向）** 里的 ——
    2026-09-23 由用户的目视描述反推确定：把我们写在 `_G4A1392`（存储帧 8192x5464，
    Rotate 270 CW）上的坐标套到 IMG_2509 的几何上，如果按**显示帧**解释，蒙版应该
    仍落在画面左侧竖带；用户看到的却是"下 1/3 处"——只有按**存储帧**解释、
    再跟着 EXIF 旋转，才会变成下 1/3。
    所以这里也一律在存储帧里算覆盖度，画完再旋转成正立方向仅供观看。

    返回的旋转角度是**给 PIL 用的**（逆时针为正），所以取 orientation 里
    顺时针度数的相反数。
    """
    import rawpy

    with rawpy.imread(str(raw_path)) as raw:
        rgb = raw.postprocess(half_size=half, use_camera_wb=True, output_bps=8, user_flip=0)
    image = Image.fromarray(rgb)
    photo = read_orientation(raw_path, ExiftoolRunner())
    return image, -photo.rotation


# --------------------------------------------------------------------------- 侧车解析

def _num(el, name, default=0.0) -> float:
    raw = el.get(NS.qname("crs", name))
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def read_masks(sidecar: Path) -> list[dict]:
    """从侧车里读回局部调整（蒙版类型 + 几何 + 局部参数，局部参数换算回面板值）。"""
    text = re.sub(r"<\?xpacket.*?\?>", "", sidecar.read_text(encoding="utf-8", errors="replace"),
                  flags=re.S).strip()
    root = ET.fromstring(text)
    group = root.find(f".//{NS.qname('crs', 'MaskGroupBasedCorrections')}")
    if group is None:
        return []

    masks: list[dict] = []
    for li in group.find(NS.SEQ_TAG).findall(NS.LI_TAG):
        desc = li.find(NS.DESCRIPTION_TAG)
        local_panel: dict[str, float] = {}
        for key, raw in desc.attrib.items():
            name = key.split("}")[-1]
            if not name.startswith("Local"):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            scale = _LOCAL_SCALES.get(name, 0.01)
            local_panel[name] = value / scale if scale else value
        holder_li = desc.find(NS.qname("crs", "CorrectionMasks")).find(NS.SEQ_TAG).find(NS.LI_TAG)
        holder = holder_li if holder_li.get(NS.qname("crs", "What")) else holder_li.find(NS.DESCRIPTION_TAG)
        kind = holder.get(NS.qname("crs", "What"))
        entry: dict = {
            "name": holder.get(NS.qname("crs", "MaskName")) or desc.get(NS.qname("crs", "CorrectionName")),
            "what": kind,
            "local": {k: v for k, v in local_panel.items() if abs(v) > 1e-6},
        }
        if kind == "Mask/Gradient":
            entry["zero"] = (_num(holder, "ZeroX"), _num(holder, "ZeroY"))
            entry["full"] = (_num(holder, "FullX"), _num(holder, "FullY"))
        elif kind == "Mask/CircularGradient":
            entry["rect"] = (_num(holder, "Top"), _num(holder, "Left"),
                             _num(holder, "Bottom"), _num(holder, "Right"))
            entry["angle"] = _num(holder, "Angle")
            entry["midpoint"] = _num(holder, "Midpoint", 50.0)
            entry["feather"] = _num(holder, "Feather", 50.0)
        elif kind == "Mask/Aggregate":
            paint = holder.find(NS.qname("crs", "Masks")).find(NS.SEQ_TAG).find(NS.LI_TAG).find(
                NS.DESCRIPTION_TAG)
            entry["radius"] = _num(paint, "Radius", 0.1)
            entry["flow"] = _num(paint, "Flow", 0.6)
            dabs: list[tuple[float, float]] = []
            for line in paint.find(NS.qname("crs", "Dabs")).find(NS.SEQ_TAG).findall(NS.LI_TAG):
                parts = (line.text or "").split()
                if len(parts) == 3 and parts[0] == "d":
                    dabs.append((float(parts[1]), float(parts[2])))
            entry["dabs"] = dabs
        else:
            entry["unsupported"] = True
        masks.append(entry)
    return masks


# --------------------------------------------------------------------------- 覆盖度

def _grid(shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    height, width = shape
    yy, xx = np.mgrid[0:height, 0:width]
    return xx / max(1, width - 1), yy / max(1, height - 1)


def _smoothstep(edge0, edge1, x):
    t = np.clip((x - edge0) / np.maximum(1e-6, (edge1 - edge0)), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def coverage(mask: dict, shape: tuple[int, int]) -> np.ndarray:
    """按我们对 ACR 语义的理解算覆盖度（0..1）。"""
    x, y = _grid(shape)
    what = mask.get("what")
    if what == "Mask/Gradient":
        (zx, zy), (fx, fy) = mask["zero"], mask["full"]
        dx, dy = fx - zx, fy - zy
        denom = max(1e-9, dx * dx + dy * dy)
        t = ((x - zx) * dx + (y - zy) * dy) / denom
        return _smoothstep(0.0, 1.0, t)
    if what == "Mask/CircularGradient":
        top, left, bottom, right = mask["rect"]
        cx, cy = (left + right) / 2, (top + bottom) / 2
        rx, ry = max(1e-6, (right - left) / 2), max(1e-6, (bottom - top) / 2)
        angle = np.deg2rad(mask.get("angle", 0.0))
        ux, uy = (x - cx) * np.cos(angle) + (y - cy) * np.sin(angle), -(x - cx) * np.sin(angle) + (y - cy) * np.cos(angle)
        rho = np.sqrt((ux / rx) ** 2 + (uy / ry) ** 2)
        inner = float(np.clip(1.0 - mask.get("feather", 50.0) / 100.0, 0.0, 0.98))
        return 1.0 - _smoothstep(inner, 1.0, rho)
    if what == "Mask/Aggregate":
        radius = max(1e-4, mask.get("radius", 0.1))
        best = np.zeros(shape, dtype=np.float32)
        for dx, dy in mask.get("dabs", []):
            dist = np.sqrt((x - dx) ** 2 + (y - dy) ** 2)
            best = np.maximum(best, np.clip(1.0 - dist / radius, 0.0, 1.0))
        return best * float(np.clip(mask.get("flow", 0.6), 0.0, 1.0))
    return np.zeros(shape, dtype=np.float32)


# --------------------------------------------------------------------------- 渲染

def _to_linear(arr: np.ndarray) -> np.ndarray:
    return np.power(np.clip(arr, 0, 255) / 255.0, 2.2)


def _to_srgb(lin: np.ndarray) -> np.ndarray:
    return np.clip(np.power(np.clip(lin, 0.0, None), 1 / 2.2) * 255.0, 0, 255)


def apply_effect(base: np.ndarray, cov: np.ndarray, local: dict) -> np.ndarray:
    """把局部参数（面板值）近似作用到画面上。"""
    lin = _to_linear(base.astype(np.float32))
    out = lin.copy()
    ev = float(local.get("LocalExposure2012", 0.0))
    if abs(ev) > 1e-6:
        out *= np.power(2.0, ev * cov)[..., None]
    sat = float(local.get("LocalSaturation", 0.0)) / 100.0
    if abs(sat) > 1e-6:
        luma = out.mean(axis=2, keepdims=True)
        factor = 1.0 + sat * cov
        out = luma + (out - luma) * factor[..., None]
    return _to_srgb(out)


def overlay(image: Image.Image, cov: np.ndarray) -> Image.Image:
    """红色半透明覆盖度图（越红表示蒙版越强）。"""
    base = np.asarray(image).astype(np.float32)
    red = np.array([255.0, 30.0, 30.0], dtype=np.float32)
    alpha = (cov * 0.55)[..., None]
    return Image.fromarray(((base * (1 - alpha) + red * alpha)).astype(np.uint8))


def render(raw_path: Path, sidecar: Path, out_dir: Path, half: bool = True) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    image, rotation = decode_raw(raw_path, half=half)
    print(f"  照片 {raw_path.name} 存储帧尺寸 = {image.size[0]}x{image.size[1]}（观看时旋转 {rotation}°）")
    base = np.asarray(image).astype(np.float32)
    shape = (base.shape[0], base.shape[1])
    masks = read_masks(sidecar)
    print(f"  侧车 {sidecar.name} 里读到 {len(masks)} 个蒙版：{[m['name'] for m in masks]}")

    written: list[Path] = []
    for index, mask in enumerate(masks, 1):
        cov = coverage(mask, shape)
        effected = apply_effect(base, cov, mask.get("local", {}))
        # 左：原图 | 右：模拟效果；再附一张"红 = 蒙版覆盖"的定位图。
        side_by_side = np.concatenate([base.astype(np.uint8), effected.astype(np.uint8)], axis=1)
        overlay_pair = np.concatenate([np.asarray(overlay(image, cov)), base.astype(np.uint8)], axis=1)
        safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(mask["name"]))
        if rotation:
            side_by_side = np.asarray(Image.fromarray(side_by_side).rotate(rotation, expand=True))
            overlay_pair = np.asarray(Image.fromarray(overlay_pair).rotate(rotation, expand=True))
        path = out_dir / f"{index:02d}_{safe}_对照.jpg"
        Image.fromarray(side_by_side).save(path, quality=88)
        path2 = out_dir / f"{index:02d}_{safe}_蒙版位置.jpg"
        Image.fromarray(overlay_pair).save(path2, quality=88)
        written.extend([path, path2])
        # 额外把覆盖度算在**显示帧**上，两个解释都出一张，好对照哪种才是 ACR 看到的样子
        display_cov = coverage(mask, (shape[1], shape[0]))
        if rotation:
            display_cov = np.asarray(Image.fromarray((display_cov * 255).astype(np.uint8)).rotate(rotation, expand=True)) / 255.0
        if display_cov.shape == base.shape[:2]:
            path3 = out_dir / f"{index:02d}_{safe}_若按显示帧.jpg"
            Image.fromarray(np.asarray(overlay(image, display_cov))).save(path3, quality=88)
            written.append(path3)
        print(f"    {mask['name']}: 覆盖面积 = {float(cov.mean()) * 100:.1f}%  最大 = {float(cov.max()):.2f}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="把侧车里的蒙版叠在照片上出 JPEG")
    parser.add_argument("--sidecar", type=Path, nargs="+", required=True)
    parser.add_argument("--raw", type=Path, default=ROOT / "test_data" / "IMG_2509.CR3")
    parser.add_argument("--out", type=Path, default=None, help="默认与侧车同目录下的 预览/")
    args = parser.parse_args()

    for sidecar in args.sidecar:
        out_dir = args.out or sidecar.parent / "预览"
        print(f"\n=== {sidecar.name} ===")
        for path in render(args.raw, sidecar, out_dir):
            print(f"    已出图：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
