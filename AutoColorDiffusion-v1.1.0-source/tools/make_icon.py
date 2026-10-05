# -*- coding: utf-8 -*-
"""生成软件图标（圆角风格）—— 源图 → 圆角 PNG 组 + 多尺寸 .ico。

用法
----
    # 1) 用你自己的图（推荐：把图存成 assets/icon/source.png 后直接跑）
    python tools/make_icon.py                     # 默认读 assets/icon/source.png
    python tools/make_icon.py --source D:\\x.png   # 临时指定别的源图
    python tools/make_icon.py --art-ratio 1.0      # 内容顶到边（默认 0.84，四周留呼吸边）

    底色（白底/黑底/透明底）是**量出来的**，不需要告诉它；图案外围是大片纯色时
    默认就会把那些留白裁掉，再按 art_ratio 补齐成正方形。

    # 2) 没有源图也没关系：程序会按"圆角方块 + 三个半透明圆"复刻一版
    python tools/make_icon.py --procedural

产物（都会被打包与运行时读取）
------------------------------
    assets/icon/app-512.png   圆角 PNG（关于对话框/文档用）
    assets/icon/app-256.png   圆角 PNG（窗口图标运行时用）
    assets/app.ico            多尺寸 ICO（16/24/32/48/64/128/256，exe 图标用）

为什么要"生成"而不是直接塞一张图
--------------------------------
  · **圆角是我们统一加的**：源图可能带白边/直角，而 Windows 11 的图标语言是
    圆角方块；抓半径、加透明圆角、再逐级缩小，比让人手改七种尺寸可靠；
  · **小尺寸不能直接缩放**：16px 下 1 像素的柔光会糊成一团灰，必须逐尺寸重采样
    （本工具对 ≤32px 的尺寸**不加柔光**，保证小图标干净）；
  · exe 图标必须是多尺寸 ICO，任务栏/资源管理器/Alt-Tab 会各自挑不同尺寸。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageChops, ImageDraw, ImageFilter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = ROOT / "assets" / "icon"
DEFAULT_SOURCE = ICON_DIR / "source.png"
ICO_PATH = ROOT / "assets" / "app.ico"

# 主图分辨率：比用到的最大尺寸大一档，缩小才锐利（LANCZOS 降采样有抗锯齿效果）。
MASTER = 1024

# 圆角半径 = 边长的 22%（Windows 11 图标的观感；实测 40/48 像素在大图标上偏方）。
CORNER_RADIUS_RATIO = 0.22
# 实际生效的比例：可被 `--radius` 覆盖（源图自带的圆角半径不同时用它对齐）。
_radius_ratio = CORNER_RADIUS_RATIO

# 裁掉底色留白后，内容占边长的比例 —— 剩下的部分就是四周的“呼吸边”。
# 0.84 ≈ 留 8% 边距；设为 1.0 = 内容顶到边。
ART_RATIO = 0.84

# ICO 里要包含的尺寸：Windows 会按场合各取所需
# （16 列表/任务栏小图标、32 桌面、48 资源管理器、256 大图标视图）。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# 小尺寸不加柔光：光晕在 16px 下会变成一圈脏灰边。
GLOW_MIN_SIZE = 48

# --- 复刻参数（--procedural 用）---------------------------------------------
# 四个半透明圆：上左蓝、上右黄绿、下左青、下中红。坐标/半径是**相对边长**的
# 归一化值（照原图目测）；颜色取的是原图里"单圆不重叠处"的像素值。
PROCEDURAL_CIRCLES = (
    # (中心 x, 中心 y, 半径, RGB, 叠加强度)
    (0.355, 0.360, 0.243, (108, 152, 246), 0.86),   # 蓝
    (0.650, 0.370, 0.245, (214, 224, 104), 0.86),   # 黄绿
    (0.295, 0.615, 0.236, (110, 220, 205), 0.86),   # 青
    (0.615, 0.690, 0.236, (246, 108, 112), 0.86),   # 红
)
# 每个圆外面那圈柔光（原图里很显眼：整块图标像蒙着一层彩色薄雾）
GLOW_ALPHA = 92
GLOW_BLUR_RATIO = 0.030
GLOW_SWELL = 1.10          # 光晕比圆本身大一点，才形成"外圈发亮"


def rounded_mask(size: int, radius_ratio: float | None = None) -> Image.Image:
    """生成圆角方块的 alpha 蒙版（L 模式，255 = 不透明）。"""
    ratio = _radius_ratio if radius_ratio is None else radius_ratio
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(round(size * ratio)), fill=255
    )
    return mask


def _multiply_layer(base: Image.Image, layer: Image.Image, mask: Image.Image) -> Image.Image:
    """把一层半透明色块按"正片叠底"叠加到 base 上（重叠处变深、色调像照片里的滤色片）。"""
    blended = ImageChops.multiply(base, layer)
    return Image.composite(blended, base, mask)


def procedural_master(size: int = MASTER) -> Image.Image:
    """按原图的观感复刻一版：冷白底 + 彩色柔光 + 四个半透明圆（正片叠底）+ 圆角。

    混合方式对照原图定的：
      · 重叠处**变深但不发黑**（蓝∩黄 = 明亮的春绿）→ 正片叠底，但只用 ~86% 强度；
      · 每个圆外面有一圈同色柔光 → 圆本身之外的模糊副本按 alpha 叠上去；
      · 底色是极浅的冷白（左上偏蓝、右下偏粉）→ numpy 直接算一张渐变底。
    """
    import numpy as np

    # 1) 底色：白 + 两处极浅的彩色偏色（左上偏蓝、右下偏粉）
    yy, xx = np.mgrid[0:size, 0:size]
    fx, fy = xx / size, yy / size
    blue_glow = np.clip(1.0 - np.hypot(fx - 0.26, fy - 0.30) * 1.7, 0.0, 1.0)
    pink_glow = np.clip(1.0 - np.hypot(fx - 0.74, fy - 0.80) * 1.7, 0.0, 1.0)
    base = np.stack(
        [
            255.0 - 10.0 * pink_glow,                      # R：右下偏粉
            255.0 - 8.0 * (blue_glow + pink_glow) * 0.5,   # G
            255.0 - 12.0 * blue_glow,                      # B：左上偏蓝
        ],
        axis=-1,
    )
    image = Image.fromarray(base.clip(0, 255).astype("uint8"), "RGB")

    for cx, cy, radius, color, strength in PROCEDURAL_CIRCLES:
        # 2a) 柔光：把圆放大一点、模糊，用该圆自己的颜色按 alpha 叠上去
        glow_mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(glow_mask).ellipse(
            (
                (cx - radius * GLOW_SWELL) * size,
                (cy - radius * GLOW_SWELL) * size,
                (cx + radius * GLOW_SWELL) * size,
                (cy + radius * GLOW_SWELL) * size,
            ),
            fill=GLOW_ALPHA,
        )
        glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(size * GLOW_BLUR_RATIO))
        image = Image.composite(Image.new("RGB", (size, size), color), image, glow_mask)

        # 2b) 圆本身：正片叠底，但按 strength 收一点强度（否则重叠区会发黑）
        circle_mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(circle_mask).ellipse(
            (
                (cx - radius) * size,
                (cy - radius) * size,
                (cx + radius) * size,
                (cy + radius) * size,
            ),
            fill=255,
        )
        circle_mask = circle_mask.filter(ImageFilter.GaussianBlur(size * 0.003))
        if strength < 1.0:
            circle_mask = circle_mask.point(lambda v: int(v * strength))
        image = _multiply_layer(image, Image.new("RGB", (size, size), color), circle_mask)

    # 3) 整体柔光：糊一层用滤色提亮，得到原图那种"透亮"的观感
    glow = image.filter(ImageFilter.GaussianBlur(size * 0.015))
    image = ImageChops.screen(image, glow.point(lambda v: int(v * 0.30)))

    # 4) 圆角
    out = image.convert("RGBA")
    out.putalpha(rounded_mask(size))
    return out


def _border_pixel(image: Image.Image) -> tuple[int, int, int, int]:
    """取四角像素里出现最多的那个当"底色"（白底 / 黑底 / 透明底都能认）。"""
    w, h = image.size
    corners = [image.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    return max(set(corners), key=corners.count)


def _content_bbox(image: Image.Image, border: tuple[int, int, int, int],
                  threshold: int) -> tuple[int, int, int, int] | None:
    """返回"不是底色"的内容外接框（只比 RGB，透明底色同样会被当成底色）。"""
    ref = Image.new("RGB", image.size, border[:3])
    diff = ImageChops.difference(image.convert("RGB"), ref).convert("L")
    return diff.point(lambda v: 255 if v > threshold else 0).getbbox()


def from_source(path: Path, master: int = MASTER, *, trim: bool = True,
                trim_threshold: int = 12, art_ratio: float = ART_RATIO) -> Image.Image:
    """读用户给的源图：去掉四周底色留白 → 用底色补齐正方形 → 只缩不放 → 加圆角 alpha。

    为什么先裁留白：导出/截图出来的图常常带一圈白边或黑边，
    直接加圆角会得到"圆角里还套着一圈边"的观感（在相反明暗的桌面上尤其明显）。

    "底色"是**量出来的**（取四角像素里最常见的那个），不是写死的白色 ——
    黑底的设计稿同样能正确裁边，补齐正方形时用的也是它自己的底色，
    不会凭空出现白边或透明补丁。

    ⚠ **只缩不放**：源图比 master 小时保持原尺寸 —— 把小图放大到 1024 再缩回去
    等于白糊一层（实测 858×858 的源图上采样 19% 后，小尺寸的边缘明显发毛）。
    master 只是个上限。

    `trim=False` / `--trim-threshold` 是给"源图本身就是一张完整的圆角图标"用的：
    那种图不该再裁，否则会把设计里的浅色底板一起切掉（只剩几个圆，没了圆角方块）。
    `art_ratio` 只在裁剪生效时起作用：裁完的内容摆进正方形后四周留多少边。
    """
    image = Image.open(path).convert("RGBA")
    border = _border_pixel(image)

    # 1) 裁掉四周底色留白（整行/整列都是底色就切掉），再按 art_ratio 摆进正方形画布
    if trim:
        bbox = _content_bbox(image, border, trim_threshold)
        if bbox:
            image = image.crop(bbox)
        content = max(image.size)
        side = content if art_ratio >= 1.0 else int(round(content / max(0.5, art_ratio)))
    else:
        # 不裁时保持原行为：补透明，不改源图自带的底色
        border = (0, 0, 0, 0)
        side = max(image.size)

    side = max(side, image.width, image.height)
    square = Image.new("RGBA", (side, side), border)
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))

    # 2) 缩放（只缩不放）+ 圆角
    scale = min(1.0, master / side)
    target = max(64, int(round(side * scale)))
    scaled = square.resize((target, target), Image.Resampling.LANCZOS) if scale < 1.0 else square
    scaled.putalpha(rounded_mask(target))
    return scaled


def build_variants(master: Image.Image) -> dict[int, Image.Image]:
    """从主图生成各尺寸。小尺寸去掉柔光、提亮，并把细线"发胖"一像素。

    深底 + 细金线这类设计（月亮/圆环）在 32px 以下会被重采样削得几乎看不见，
    所以 ≤24px 用 3×3 的「取邻域最大值」把亮线膨胀一圈 —— 只提亮、不加灰雾，
    黑底仍然是黑底。
    """
    variants: dict[int, Image.Image] = {}
    for size in sorted(set(ICO_SIZES) | {256, 512}):
        small = master.resize((size, size), Image.Resampling.LANCZOS)
        if size < GLOW_MIN_SIZE:
            # 小图标：提高对比、削掉灰雾，否则 16px 看起来像"蒙了层灰"
            boost = 1.06 if size >= 32 else 1.22
            rgb = small.convert("RGB").point(lambda v: min(255, int(v * boost)))
            if size <= 24:
                rgb = rgb.filter(ImageFilter.MaxFilter(3))
            small = rgb.convert("RGBA")
            small.putalpha(rounded_mask(size))
        variants[size] = small
    return variants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成圆角风格的应用图标（PNG + ICO）")
    parser.add_argument("--source", type=Path, default=None,
                        help=f"源图（默认 {DEFAULT_SOURCE.relative_to(ROOT)}；不存在则用 --procedural）")
    parser.add_argument("--procedural", action="store_true",
                        help="不用源图，按内置参数复刻一版（三个半透明圆 + 圆角）")
    parser.add_argument("--no-trim", action="store_true",
                        help="源图已经是完整圆角图标时用：不再自动裁掉四周留白")
    parser.add_argument("--trim-threshold", type=int, default=12,
                        help="判定“纯色边框”的容差（0-255，默认 12；调小=只裁几乎纯白的边）")
    parser.add_argument("--radius", type=float, default=CORNER_RADIUS_RATIO,
                        help=f"圆角半径（占边长比例，默认 {CORNER_RADIUS_RATIO}）")
    parser.add_argument("--art-ratio", type=float, default=ART_RATIO,
                        help=f"裁掉留白后内容占边长的比例（默认 {ART_RATIO}；1.0 = 内容顶到边）")
    args = parser.parse_args(argv)

    global _radius_ratio
    _radius_ratio = args.radius

    source = args.source or DEFAULT_SOURCE
    if args.procedural or not source.is_file():
        reason = "--procedural 指定" if args.procedural else f"没找到源图 {source}"
        print(f"按内置参数复刻图标（{reason}）。")
        master = procedural_master()
    else:
        print(f"使用源图：{source}")
        master = from_source(source, trim=not args.no_trim,
                             trim_threshold=args.trim_threshold,
                             art_ratio=args.art_ratio)

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    variants = build_variants(master)
    for size in (512, 256):
        target = ICON_DIR / f"app-{size}.png"
        variants[size].save(target)
        print(f"  写出 {target.relative_to(ROOT)}（{size}×{size}）")

    # ICO：以最大的一张为源，让 Pillow 逐尺寸重采样。
    variants[256].save(ICO_PATH, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  写出 {ICO_PATH.relative_to(ROOT)}（含 {', '.join(str(s) for s in ICO_SIZES)}）")

    # 读回核对：ICO 里到底存了哪些尺寸（写不进去时必须是失败，而不是"看起来成功"）。
    with Image.open(ICO_PATH) as ico:
        got = sorted(ico.info.get("sizes", set()))
    ok = got == sorted((s, s) for s in ICO_SIZES)
    print(f"  回读 ICO 尺寸：{got}")
    print("完成。" if ok else "⚠ ICO 尺寸与预期不一致，请检查 Pillow 版本。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
