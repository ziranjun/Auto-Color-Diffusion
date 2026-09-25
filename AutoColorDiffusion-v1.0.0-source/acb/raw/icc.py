# -*- coding: utf-8 -*-
"""ICC 色彩管理与缩略图编码。

硬约束 #2（必须写明的因果链）：
    AI 看的是 sRGB 缩略图，XMP 参数作用于原始 RAW，二者不可混用。
    因此缩略图编码前必须做 ICC 转换到 sRGB 并嵌入 sRGB profile，
    禁止直接丢弃或覆盖 profile。

为什么这件事不能省：
    如果相机把预览图编码为 Adobe RGB(1998) 却剥掉 profile 交给 AI，
    那么同一块"中等红色"在 Adobe RGB 数值体系里比 sRGB 更"淡"，
    AI 会误判为欠饱和，从而输出 +Vibrance 的补偿参数；
    但该参数作用在 RAW 上会让真实出片**过饱和**。
    嵌入 sRGB profile 是为了让 AI 与后续一切看图工具看到同一组数值。

依赖 Pillow 的 ImageCms（内部封装 LittleCMS）。
"""

from __future__ import annotations

import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from ..constants import THUMB_LONG_EDGE, THUMB_QUALITY
from ..errors import IccProfileMissingError
from ..logging_setup import get_logger
from ..paths import data_root, icc_dir

log = get_logger("icc")

# 打包内预设 ICC 文件名（spec 的 datas 条目必须与之一致）
SRGB_ICC_FILENAME = "sRGB.icc"
ADOBE_RGB_ICC_FILENAME = "AdobeRGB1998.icc"
DISPLAY_P3_ICC_FILENAME = "DisplayP3.icc"

# EXIF 标签号（来自 EXIF 2.32 规范）：
#   40961 = ColorSpace      1 = sRGB, 0xFFFF(65535) = Uncalibrated
#   40961 上"未校准"常见于 Adobe RGB / ProPhoto RGB 的相机输出，
#   此时需要看 40965 (InteropIndex)：'R03' = Adobe RGB (1998)。
EXIF_TAG_COLOR_SPACE = 40961
EXIF_TAG_INTEROP_INDEX = 40965
EXIF_COLOR_SPACE_SRGB = 1

# 渲染意图：PERCEPTUAL（感知）而非 RELATIVE_COLORIMETRIC。
# 依据：缩略图是要"喂给 AI 判断观感"的图像，感知意图会做色域压缩映射，
#       保证在高饱和区域不出现裁切断层——这比绝对色度准确更贴合
#       "人眼看到什么"的判断目标。若用相对色度，超出 sRGB 的饱和色会被硬裁切，
#       AI 会把裁切当作"颜色断层/过曝"而误判。
RENDERING_INTENT = ImageCms.Intent.PERCEPTUAL


# 由本程序**自己按公开参数生成**的 Adobe RGB (1998) 等效 profile 的文件名。
#
# 【为什么不直接捆绑 Adobe 那份】Adobe 官方页面（adobe.com/digitalimag/adobergb.html）原话：
#   ·"With the appropriate legal agreements, it is also available for distribution
#     by third-party hardware and software vendors."
#     → 第三方分发**需要先与 Adobe 签协议**，不是默许；
#   ·"If vendors choose to create their own profile according to this specification,
#     and they want to indicate to their customers that this profile was written in
#     accordance with Adobe's specification, then an alternate phrasing is required,
#     such as 'compatible with Adobe RGB (1998)'."
#     → 官方**明确认可**第三方按规范自制 profile，并规定了该怎么命名。
# 所以我们走第二条路：用规范里公开的色度参数（D65、gamma 563/256、三原色 xy）
# 自己生成一个等效 profile，命名与描述都写 "compatible with Adobe RGB (1998)"。
# 若用户的机器上真有 Adobe 那份（Photoshop 装的），搜索顺序会**优先用官方的**。
ADOBE_RGB_COMPAT_FILENAME = "AdobeRGB1998-compatible.icc"

# Adobe RGB (1998) 规范里的公开色度参数（不是从 Adobe 的文件里读出来的）。
ADOBE_RGB_PRIMARIES_XY: dict[str, tuple[float, float]] = {
    "r": (0.6400, 0.3300),
    "g": (0.2100, 0.7100),
    "b": (0.1500, 0.0600),
}
ADOBE_RGB_WHITE_XY: tuple[float, float] = (0.3127, 0.3290)   # D65
ADOBE_RGB_GAMMA = 563 / 256                                  # = 2.19921875
ICC_PCS_D50 = (0.9642, 1.0, 0.8249)                          # ICC PCS 白点


def _xy_to_xyz(xy: tuple[float, float], luminance: float = 1.0) -> tuple[float, float, float]:
    x, y = xy
    return (x / y * luminance, luminance, (1.0 - x - y) / y * luminance)


def _rgb_to_xyz_matrix(
    primaries: dict[str, tuple[float, float]], white: tuple[float, float]
) -> list[list[float]]:
    """RGB→XYZ 矩阵（列 = 三原色）。纯 numpy，不依赖 LittleCMS。"""
    import numpy as np

    m = np.array([_xy_to_xyz(primaries[c]) for c in ("r", "g", "b")], dtype=float).T
    scale = np.linalg.solve(m, np.array(_xy_to_xyz(white), dtype=float))
    return (m * scale).tolist()


def _bradford(src_white, dst_white):
    """Bradford 色适应矩阵（ICC 的 PCS 是 D50，所以必须把 D65 适应过去）。"""
    import numpy as np

    m = np.array([[0.8951, 0.2664, -0.1614],
                  [-0.7502, 1.7135, 0.0367],
                  [0.0389, -0.0685, 1.0296]], dtype=float)
    src = m @ np.array(src_white, dtype=float)
    dst = m @ np.array(dst_white, dtype=float)
    return np.linalg.inv(m) @ np.diag(dst / src) @ m


def _s15f16(value: float) -> bytes:
    """ICC 的 s15Fixed16Number。"""
    import struct

    return struct.pack(">i", int(round(float(value) * 65536)))


def _icc_xyz_tag(x: float, y: float, z: float) -> bytes:
    import struct

    return b"XYZ " + b"\0" * 4 + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _icc_curve_tag(gamma: float) -> bytes:
    """curveType：count=1 + u8Fixed8 的 gamma（Adobe RGB 的 563/256 正好能精确表示）。"""
    import struct

    return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256)))


def _icc_desc_tag(ascii_text: str) -> bytes:
    """textDescriptionType（ICC v2）。末尾的 scriptcode 区按规范固定 67 字节。"""
    import struct

    payload = ascii_text.encode("ascii", "replace") + b"\0"
    body = b"desc" + b"\0" * 4 + struct.pack(">I", len(payload)) + payload
    body += struct.pack(">II", 0, 0)                     # unicode 语言码 + 长度 = 0
    body += struct.pack(">H", 0) + struct.pack(">B", 0)  # scriptcode 码 + 长度
    body += b"\0" * 67                                  # scriptcode 数据
    return body


def _icc_text_tag(text_value: str) -> bytes:
    import struct

    payload = text_value.encode("ascii", "replace") + b"\0"
    return b"text" + b"\0" * 4 + struct.pack(">I", len(payload)) + payload


def build_adobe_rgb_compatible_profile() -> bytes:
    """按公开参数生成 Adobe RGB (1998) 等效矩阵型 ICC profile（ICC v2，约 560 字节）。

    **这是本程序自己算出来的文件，不是 Adobe 的那份**（文件头的 CMM/creator 都是空的，
    desc 里写的是 "Compatible with Adobe RGB (1998)"）。

    正确性怎么保证（不靠"看起来对"）：
      · 色度参数取规范公开值：D65 白点、gamma 563/256、三原色 xy；
      · ICC 的 PCS 是 D50，所以用 **Bradford** 把 D65 适应到 D50，
        写进 rXYZ/gXYZ/bXYZ（wtpt 写 D50）；
      · 自检 `test_color_management` 用**独立实现的 numpy 矩阵运算**复算同一条转换，
        逐像素比对（要求最大通道差 ≤ 2）。
    """
    import struct

    matrix_d65 = _rgb_to_xyz_matrix(ADOBE_RGB_PRIMARIES_XY, ADOBE_RGB_WHITE_XY)
    adapt = _bradford(_xy_to_xyz(ADOBE_RGB_WHITE_XY), ICC_PCS_D50)
    import numpy as np

    m_d50 = adapt @ np.array(matrix_d65, dtype=float)

    tags: dict[bytes, bytes] = {
        b"desc": _icc_desc_tag("Compatible with Adobe RGB (1998)"),
        b"cprt": _icc_text_tag(
            "Generated by Auto Color Diffusion from the published Adobe RGB (1998) "
            "colorimetry. Not an Adobe file."
        ),
        b"wtpt": _icc_xyz_tag(*ICC_PCS_D50),
        b"rXYZ": _icc_xyz_tag(float(m_d50[0, 0]), float(m_d50[1, 0]), float(m_d50[2, 0])),
        b"gXYZ": _icc_xyz_tag(float(m_d50[0, 1]), float(m_d50[1, 1]), float(m_d50[2, 1])),
        b"bXYZ": _icc_xyz_tag(float(m_d50[0, 2]), float(m_d50[1, 2]), float(m_d50[2, 2])),
        b"rTRC": _icc_curve_tag(ADOBE_RGB_GAMMA),
        b"gTRC": _icc_curve_tag(ADOBE_RGB_GAMMA),
        b"bTRC": _icc_curve_tag(ADOBE_RGB_GAMMA),
    }

    header_size = 128
    table_size = 4 + len(tags) * 12
    offset = header_size + table_size
    table, blobs = b"", b""
    for sig in sorted(tags):
        data = tags[sig]
        table += sig + struct.pack(">II", offset, len(data))
        blobs += data
        offset += len(data)
        pad = (-offset) % 4
        if pad:
            blobs += b"\0" * pad
            offset += pad
    total = header_size + table_size + len(blobs)

    header = struct.pack(">I", total)
    header += b"\0" * 4                                    # CMM：留空（不是 Adobe 做的）
    header += struct.pack(">I", 0x02100000)                # ICC v2.1
    header += b"mntr" + b"RGB " + b"XYZ "                  # 显示类 / RGB / PCS=XYZ
    header += struct.pack(">HHHHHH", 2026, 9, 21, 0, 0, 0)  # 创建时间
    header += b"acsp" + b"\0" * 4                           # 签名 + 平台
    header += struct.pack(">I", 0) + b"\0" * 4 + b"\0" * 4  # flags / 厂商 / 型号
    header += struct.pack(">Q", 0)                          # attributes
    header += struct.pack(">I", 0)                          # 渲染意图：感知
    header += _icc_xyz_tag(*ICC_PCS_D50)[8:]                # PCS 光源（12 字节）
    header += struct.pack(">I", 0)                          # creator
    header += b"\0" * 16 + b"\0" * 28                      # profile ID + 保留
    if len(header) != header_size:
        raise AssertionError(f"ICC 头长度错误：{len(header)}")

    return header + struct.pack(">I", len(tags)) + table + blobs


# 系统里可能存有 Adobe RGB / Display P3 profile 的目录（按优先级）。
# 为什么要找系统那一份：**Adobe 官方那份只有 Adobe 有权这么命名与分发**，
# 用户机器上装了 Photoshop（本工具本来就要求装 PS）时那份就在硬盘上，
# 找到就用它（比我们自己生成的等效 profile 更"正统"）。
# 找不到也没关系：程序自带的 `AdobeRGB1998-compatible.icc` 会兜底（见上）。
SYSTEM_PROFILE_DIRS_WINDOWS: tuple[str, ...] = (
    # Windows 自带的色彩目录（sRGB 一定在这，Adobe RGB 视安装情况而定）
    r"%WINDIR%\System32\spool\drivers\color",
    r"%COMMONPROGRAMFILES%\Adobe\Color\Profiles",
    r"%PROGRAMFILES%\Adobe\Color\Profiles",
    r"%LOCALAPPDATA%\Adobe\Color",
    r"%APPDATA%\Adobe\Color",
)

# 同一个 profile 在不同来源里的常见文件名（Adobe 自己也换过好几次写法）
PROFILE_FILENAME_ALIASES: dict[str, tuple[str, ...]] = {
    # 顺序即优先级：**Adobe 官方那份排在前面**（只有它有权叫 Adobe RGB (1998)），
    # 我们自己按公开参数生成的等效 profile 垫在最后兜底。
    "AdobeRGB1998.icc": ("AdobeRGB1998.icc", "AdobeRGB1998.icm", "Adobe RGB (1998).icc",
                         "Adobe RGB (1998).icm", ADOBE_RGB_COMPAT_FILENAME),
    "DisplayP3.icc": ("DisplayP3.icc", "DisplayP3.icm", "Display P3.icc", "Display P3.icm"),
    "sRGB.icc": ("sRGB.icc", "sRGB Color Space Profile.icm"),
}


def generated_icc_dir() -> Path:
    """程序可以自己写 ICC 的目录（数据目录内，永远可写）。

    为什么要它：`icc_dir()` 在打包后指向 `_internal\\assets\\icc`，那是只读的，
    运行时生成的文件写不进去。数据目录一定可写，所以自生成的文件落在这里。
    """
    return data_root() / "assets" / "icc"


def icc_search_dirs() -> tuple[Path, ...]:
    """ICC 搜索目录，按优先级：程序自带 → 数据目录（自生成）→ 系统色彩目录。"""
    dirs: list[Path] = []
    for candidate in (icc_dir(), generated_icc_dir()):
        if candidate not in dirs:
            dirs.append(candidate)
    dirs.extend(d for d in system_profile_dirs() if d not in dirs)
    return tuple(dirs)


def system_profile_dirs() -> tuple[Path, ...]:
    """候选的系统 profile 目录（展开环境变量，只保留真实存在的）。"""
    out: list[Path] = []
    for raw in SYSTEM_PROFILE_DIRS_WINDOWS:
        expanded = os.path.expandvars(raw)
        if "%" in expanded:          # 环境变量没展开成功（变量不存在）
            continue
        path = Path(expanded)
        if path.is_dir() and path not in out:
            out.append(path)
    return tuple(out)


@lru_cache(maxsize=32)
def find_icc_file(filename: str) -> Path | None:
    """把某个 ICC 文件名解析成真实路径：先程序自带目录，再系统目录。

    返回 None 表示**确实找不到**——调用方据此决定是报错还是走别的路径，
    而不是拿一个错误的 profile 凑合（见 _source_profile 的 R03 分支）。

    循环顺序是**"文件名"在外、"目录"在内**，这点很重要：
    `PROFILE_FILENAME_ALIASES` 的顺序是优先级（官方名字在前、我们自己生成的等效
    文件垫后），必须让"哪一份"压过"放在哪个目录"。反过来的话，程序自带的
    `assets/icc/AdobeRGB1998-compatible.icc` 会遮蔽掉系统里那份**真正的**
    Adobe RGB(1998) —— 这个坑是 smoke_test 里"系统目录里的 profile 能被搜到"
    那条用例抓出来的（fake 文件放在模拟的系统目录里，却总也搜不到）。
    """
    names = PROFILE_FILENAME_ALIASES.get(filename, (filename,))
    for name in names:
        for directory in icc_search_dirs():
            candidate = directory / name
            if candidate.is_file():
                log.debug("找到 %s：%s", filename, candidate)
                return candidate
    return None


@lru_cache(maxsize=1)
def srgb_profile_bytes() -> bytes:
    """获取 sRGB ICC profile 的字节内容（带缓存）。

    sRGB 这一份**永远拿得到**：优先用 `assets/icc/sRGB.icc`（或系统里的
    sRGB Color Space Profile.icm），都没有就用 LittleCMS 的内置公开定义
    `createProfile("sRGB")` 当场构造。

    实测这两者**只在 ICC 头里的"创建时间"那 4 个字节上不同**（偏移 32..35 = 分/秒），
    其余 584 字节逐字节相同，同一批颜色的转换结果完全一致（8 个颜色逐一比对）。
    所以"有没有这个文件"不影响色彩管理的正确性 —— 它只是省一次构造。

    ⚠ 注意别把这句话套到 Adobe RGB 上：**那个不能凭空构造**，
    缺了就是缺了（见 _source_profile）。
    """
    path = find_icc_file(SRGB_ICC_FILENAME)
    if path is not None:
        try:
            data = path.read_bytes()
            log.debug("使用磁盘上的 sRGB profile：%s（%d 字节）", path, len(data))
            return data
        except OSError as exc:
            log.warning("读取 sRGB profile 失败，改用 LittleCMS 内置定义：%s", exc)
    log.debug("磁盘上没有 sRGB.icc，使用 LittleCMS 内置 sRGB 定义（等价）")
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def load_icc_file_bytes(filename: str) -> bytes | None:
    """读取 ICC 文件（先程序自带目录，再系统色彩目录）。找不到返回 None。

    原先只看 `assets/icc/` 一个目录，于是"机器上装了 Photoshop、系统里
    明明有 Adobe RGB profile"的机器也被判成缺失。改成搜索之后，
    缺文件的情况收窄到"系统里真的没有这一份"。
    """
    path = find_icc_file(filename)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def ensure_icc_assets() -> list[Path]:
    """补齐本程序**自己生成**的 ICC 文件；返回本次新生成的文件列表。

    生成两个文件，都不涉及第三方授权素材：

    | 文件 | 怎么来的 |
    |---|---|
    | `sRGB.icc` | LittleCMS 的公开定义（`createProfile("sRGB")`）当场构造 |
    | `AdobeRGB1998-compatible.icc` | 按 Adobe 公开的色度参数（D65 / gamma 563/256 / 三原色 xy）自算的等效矩阵型 profile；命名遵循 Adobe 官方要求的 "compatible with Adobe RGB (1998)" 措辞 |

    写进**第一个可写的搜索目录**：优先程序自带目录（开发时就是 `assets/icc`，
    方便随包分发），不可写（打包后的 `_internal`）时退回数据目录
    （`<数据目录>/assets/icc`）—— 两边都在 `find_icc_file` 的搜索路径里。

    只在文件不存在时写：**绝不覆盖 Adobe 官方那份**，也不覆盖用户自己放的。
    写入失败不抛异常（sRGB 侧有 LittleCMS 内置定义兜底，功能完整）。
    """
    created: list[Path] = []
    try:
        payloads = {
            SRGB_ICC_FILENAME: ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes(),
            ADOBE_RGB_COMPAT_FILENAME: build_adobe_rgb_compatible_profile(),
        }
    except Exception as exc:
        # 这个方法在启动时被调用，**不允许**因为色彩管理出问题而拦住整个程序：
        # sRGB 侧永远有 LittleCMS 内置定义兜底。
        log.warning("生成 ICC profile 失败（程序仍可运行，sRGB 走内置定义）：%s", exc)
        return created
    for directory in (icc_dir(), generated_icc_dir()):
        # 只在"这个目录里一个待生成文件都还不存在"或"它可写"时才尝试
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug("ICC 目录不可写，换下一个：%s（%s）", directory, exc)
            continue
        wrote_any = False
        for filename, data in payloads.items():
            target = directory / filename
            if target.is_file():
                continue
            try:
                target.write_bytes(data)
                created.append(target)
                wrote_any = True
                log.info("已生成 ICC profile：%s（%d 字节）", target, len(data))
            except OSError as exc:
                log.debug("写入 ICC 失败：%s（%s）", target, exc)
        # 该目录能写就停在这里（不重复往下一处写）
        if wrote_any or os.access(directory, os.W_OK):
            break
    return created


def describe_optional_profiles() -> str:
    """描述 ICC 资源的就位情况（启动时写进日志）。

    这里必须区分两种"没有"：
      · **缺失** —— 它本该起作用却不在（输入侧会退到 rawpy，慢但颜色正确）→ 值得 WARNING；
      · **不需要** —— 设计上就用不到它（Display P3 只在输出侧把名称交给 Photoshop）
        → 只是告知。若也写成"缺失"，每次启动都刷一条假警告，
        真正的警告就被淹没（"狼来了"效应，比不报警更糟）。
    """
    lines: list[str] = []
    for filename, purpose, unused in (
        (ADOBE_RGB_ICC_FILENAME, "Adobe 官方那份；缺失时用自带的 "
                                 f"{ADOBE_RGB_COMPAT_FILENAME} 等效 profile 兜底", False),
        (ADOBE_RGB_COMPAT_FILENAME, "程序按公开参数自算的等效 profile（输入侧兜底用）", False),
        (DISPLAY_P3_ICC_FILENAME,
         "设计上不需要（输出侧只把配置文件名称交给 Photoshop，用你本机那份）", True),
    ):
        found = find_icc_file(filename)
        if found is not None:
            lines.append(f"  - {filename}：已就位（{found}）")
        else:
            lines.append(f"  - {filename}：{'不需要' if unused else '缺失'}（{purpose}）")
    return "\n".join(lines)


# Interoperability IFD 的指针标签（EXIF 2.32：0xA005），以及它里面 InteropIndex 的标签号（1）。
EXIF_TAG_INTEROP_IFD = 0xA005
EXIF_TAG_INTEROP_INDEX_IN_IFD = 1


def read_interop_index(exif) -> str | None:
    """读 InteropIndex（`"R03"` = Adobe RGB (1998)）。取不到返回 None。

    ⚠ **必须读 Interop IFD，不能只读顶层 `exif.get(40965)`。**
    按 EXIF 标准，0xA005 是"指向 Interoperability IFD 的指针"，它住在 **Exif IFD**
    里；Pillow 的 `Exif.get()` 只查它当前加载的那一层。实测（手工构造两种布局的
    JPEG，都不带嵌入 ICC）：
      · 标准布局：0th IFD 里只有 0x8769(Exif IFD) 与 ColorSpace
        → `exif.get(40965)` 返回 **None**；
      · 非标准布局（把 0xA005 写在 0th IFD）
        → `exif.get(40965)` 返回 **一个 int 偏移量**（38），不是字符串。
    两种情况下 `isinstance(v, str)` 都是 False —— 也就是说，
    **这个判断以前从来没命中过**，所有声明 Adobe RGB 的预览都被悄悄当成 sRGB 用了。
    正确读法：`exif.get_ifd(0xA005)` 才能拿到 `{1: "R03"}`。
    """
    # 1) 标准位置：Interop IFD 里的 tag 1
    try:
        interop = exif.get_ifd(EXIF_TAG_INTEROP_IFD)
    except Exception:      # Pillow 在缺指针/结构异常时会抛 KeyError 等
        interop = None
    if isinstance(interop, dict):
        value = interop.get(EXIF_TAG_INTEROP_INDEX_IN_IFD)
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        if isinstance(value, str) and value.strip():
            return value.strip().upper().rstrip("\x00")

    # 2) 兼容把 InteropIndex 直接写在当前层的（少见，但有些工具会这么写）
    value = exif.get(EXIF_TAG_INTEROP_INDEX)
    if isinstance(value, str) and value.strip():
        return value.strip().upper().rstrip("\x00")
    return None


def _source_profile(img: Image.Image) -> tuple[ImageCms.ImageCmsProfile, str]:
    """判定源图的 ICC profile 与判定依据（用于日志留痕）。

    判定优先级（问题 c/b 的因果链落地）：
        1. 图片自带的 icc_profile 块 —— 最权威，直接用；
        2. EXIF ColorSpace == 1 → sRGB；
        3. EXIF InteropIndex == 'R03' → Adobe RGB (1998)；
        4. 以上都无 → 按 sRGB 解释（硬约束 #2 规定的默认假设）。
    返回 (profile, 依据说明)；依据说明会写进日志，
    便于用户事后核查"AI 到底看的是什么色彩空间"。
    """
    icc_bytes = img.info.get("icc_profile")
    if icc_bytes:
        try:
            return ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes)), "嵌入 ICC（img.info['icc_profile']）"
        except Exception as exc:
            # profile 损坏时不能直接丢弃：记录并继续往下走 EXIF 判定。
            log.warning("嵌入 ICC profile 解析失败，转用 EXIF 判定：%s", exc)

    try:
        exif = img.getexif()
    except Exception:
        exif = None

    if exif is not None:
        color_space = exif.get(EXIF_TAG_COLOR_SPACE)
        if color_space == EXIF_COLOR_SPACE_SRGB:
            return (
                ImageCms.ImageCmsProfile(io.BytesIO(srgb_profile_bytes())),
                "EXIF ColorSpace=sRGB(1)",
            )
        interop = read_interop_index(exif)
        if interop == "R03":
            adobe = load_icc_file_bytes(ADOBE_RGB_ICC_FILENAME)
            if adobe:
                return (
                    ImageCms.ImageCmsProfile(io.BytesIO(adobe)),
                    "EXIF InteropIndex=R03(Adobe RGB)",
                )
            # ⚠ 这里**刻意报错，而不是"按 sRGB 凑合"**。
            # 把它当 sRGB 用会让 AI 把"Adobe RGB 数值偏淡"误判成欠饱和，
            # 于是输出 +Vibrance，而参数作用在 RAW 上会让出片**过饱和** ——
            # 一个本可以避免的系统性偏色。上层收到这个异常会改用 rawpy 全解码
            # （它直接输出 sRGB，颜色是对的），代价只是每张慢 1–5 秒。
            searched = "、".join(
                [str(icc_dir())] + [str(p) for p in system_profile_dirs()]
            )
            raise IccProfileMissingError(
                "预览图声明为 Adobe RGB(1998)（EXIF InteropIndex=R03），"
                f"但找不到 {ADOBE_RGB_ICC_FILENAME}。\n"
                f"已搜索：{searched}\n"
                "这个 profile 受 Adobe 授权限制，本程序不能自带。\n"
                "两个选择：\n"
                "  1) 从 Photoshop 安装目录或系统色彩目录把它复制到"
                f" {icc_dir()}（文件名保持 {ADOBE_RGB_ICC_FILENAME} 即可）；\n"
                "  2) 什么都不做 —— 程序会自动改用 rawpy 全解码，"
                "颜色是正确的，只是每张慢 1–5 秒。"
            )

    # 硬约束 #2 的默认假设：不带 profile 的图像按 sRGB 解释。
    return ImageCms.ImageCmsProfile(io.BytesIO(srgb_profile_bytes())), "无 profile，按 sRGB 解释"


def to_srgb(img: Image.Image) -> tuple[Image.Image, str]:
    """把任意输入图转换到 sRGB 色彩空间。

    返回 (转换后的图, 判定依据说明)。转换失败时不抛异常：
    退化为"不做色彩变换"，但日志会明确 WARN——
    因为宁可用一个可能轻微偏色的缩略图，也不该让整张图提取失败。
    """
    src_profile, reason = _source_profile(img)

    # 统一到 RGB（P 调色板/L 灰度/LA/CMYK 都要先转，
    # 否则 ImageCms 在部分模式下会直接报错）。
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        # 灰度图转 RGB 后再做 ICC 是安全的：源 profile 对灰度图同样有效。
        img = img.convert("RGB")

    srgb_profile = ImageCms.ImageCmsProfile(io.BytesIO(srgb_profile_bytes()))

    try:
        converted = ImageCms.profileToProfile(
            img,
            src_profile,
            srgb_profile,
            renderingIntent=RENDERING_INTENT,
            outputMode="RGB",
        )
        # Pillow 的类型标注允许 profileToProfile 返回 None（失败时）。
        # 显式判空，避免把一个 None 当作图像往上传，
        # 那会在下游（缩放/编码）报出难以定位的 AttributeError。
        if converted is None:
            raise ValueError("ImageCms.profileToProfile 返回了 None")
        return converted, reason
    except Exception as exc:
        log.warning("ICC 转换失败（源判定：%s），已退化为不做色彩变换：%s", reason, exc)
        return img.convert("RGB"), f"{reason}；ICC 转换失败，未做变换"


def resize_long_edge(img: Image.Image, long_edge: int = THUMB_LONG_EDGE) -> Image.Image:
    """按长边等比缩放。

    只缩小、不放大：放大不会增加任何信息，只会让 AI 看到的细节
    变成插值出来的假细节（还会掩盖真实噪点，影响降噪相关判断）。
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    current_long = max(w, h)
    if current_long <= long_edge:
        return img
    scale = long_edge / float(current_long)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    # 重采样算法：LANCZOS。依据——它是 PIL 里唯一同时具备"抗锯齿 + 振铃可控"
    # 的高质量缩小算法；BILINEAR 会在缩小时丢失细节并产生摩尔纹，
    # NEAREST 会产生锯齿，二者都会干扰 AI 对噪点/细节的判断。
    return img.resize(new_size, Image.Resampling.LANCZOS)


def encode_jpeg_srgb(img: Image.Image, quality: int = THUMB_QUALITY) -> bytes:
    """把已转为 sRGB 的图编码为 JPEG，并嵌入 sRGB profile。

    关键点（硬约束 #2 禁止"丢弃或覆盖 profile"）：
        必须显式传 icc_profile=srgb_profile_bytes()，
        否则 Pillow 保存时会把原图的 ICC 一并丢掉，产出"无色彩空间声明"的
        JPEG —— 那正是硬约束禁止的行为。
    """
    if img.mode != "RGB":
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(
        buffer,
        format="JPEG",
        quality=quality,
        icc_profile=srgb_profile_bytes(),
        # optimize=False：优化编码在 1024px 图上收益极小（约省 2%），
        # 但会显著增加 CPU 时间，在批量场景下不划算。
        optimize=False,
        # subsampling 保持 Pillow 默认（q>=95 时为 4:4:4，否则 4:2:0），
        # 这是与主流相机 JPEG 一致的行为。
    )
    return buffer.getvalue()


def build_srgb_thumbnail(image_bytes: bytes, long_edge: int = THUMB_LONG_EDGE) -> tuple[bytes, str]:
    """从图像字节（嵌入式预览）生成 sRGB 缩略图。

    完整链路（每一步都不能少）：
        解码 → EXIF 方向校正 → ICC 转换到 sRGB → 长边缩至 1024 → q85 JPEG + 嵌入 sRGB profile

    EXIF 方向校正在缩放之前做，是因为竖拍照片的预览常带 Orientation=8（旋转 270°），
    若不先校正，AI 看到的是一张躺倒的照片，构图判断会失准。

    返回 (JPEG 字节, 色彩判定依据说明)。
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        # exif_transpose 会应用方向并移除该标签，避免后续编码再带上导致二次旋转。
        img = ImageOps.exif_transpose(img)
        srgb_img, reason = to_srgb(img)
        resized = resize_long_edge(srgb_img, long_edge)
        return encode_jpeg_srgb(resized), reason


def build_srgb_thumbnail_from_array(rgb_array, long_edge: int = THUMB_LONG_EDGE) -> bytes:
    """从 numpy RGB 数组生成 sRGB 缩略图（rawpy 兜底路径用）。

    rawpy 的 postprocess 默认 output_color=SRGB（已应用相机矩阵与 sRGB gamma），
    因此这里**不再做 ICC 转换**，只标记为 sRGB 并嵌入 profile。
    若将来支持其他 output_color，这里需要补对应的源 profile。
    """
    import numpy as np  # 局部导入：仅 rawpy 兜底路径需要，避免主流程强依赖

    if rgb_array.dtype != np.uint8:
        # rawpy 设 output_bps=8 时应已是 uint8；保守起见做一次裁剪+转换。
        rgb_array = np.clip(rgb_array, 0, 255).astype(np.uint8)
    img = Image.fromarray(rgb_array, mode="RGB")
    resized = resize_long_edge(img, long_edge)
    return encode_jpeg_srgb(resized)
