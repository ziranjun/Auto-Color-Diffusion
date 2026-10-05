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
from ..i18n import T, TF

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
# Photoshop 的 convertProfile 用的**描述名**（不是文件名）。
# 这两个字符串必须与 `constants.COLOR_SPACE_TO_PS_PROFILE` 里的值一致 ——
# 不一致就会出现“探测说已就绪、转换却报错”的诡异局面。
PROFILE_NAME_DISPLAY_P3 = "Display P3"
PROFILE_NAME_ADOBE_RGB = "Adobe RGB (1998)"
PROFILE_NAME_SRGB = "sRGB IEC61966-2.1"

# EXIF 标签号（来自 EXIF 2.32 规范）：
#   40961 = ColorSpace      1 = sRGB, 0xFFFF(65535) = Uncalibrated
#   40961 上"未校准"常见于 Adobe RGB / ProPhoto RGB 的相机输出，
#   此时需要看 40965 (InteropIndex)：'R03' = Adobe RGB (1998)。
EXIF_TAG_COLOR_SPACE = 40961
EXIF_TAG_INTEROP_INDEX = 40965
EXIF_COLOR_SPACE_SRGB = 1
# 274 = Orientation。`build_srgb_thumbnail` 用它读"预览字节自己带没带方向"：
# 带（6/8/3…）→ `exif_transpose` 已就地应用并移除该标签，不能再按文件级方向补转。
# ⚠ 2026-09-30 补：该常量曾**漏定义** → `getexif().get(EXIF_TAG_ORIENTATION)`
#   每次都 NameError 被 except 吞掉 → own_tag 恒为 None → 凡"预览自带方向标签"
#   的机型都会 exif_transpose + rotate_to_display **双转**（Pylance 未定义变量发现）。
EXIF_TAG_ORIENTATION = 274

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

# ---------------------------------------------------------------------------
# Display P3（输出侧）—— 同样由本程序按公开规范自算，不捆绑 Apple 的文件
# ---------------------------------------------------------------------------
# 背景（2026-09-26 实测，整条链的来龙去脉）：
#   Photoshop 的 `doc.convertProfile(name)` **只认已安装的 profile 名**（传 ICC 文件
#   完整路径也不行）；名字对不上时 PS 不报错，而是**照常导出**并写进当前文档的
#   profile（本机实测：源 sRGB TIFF → 产出被标成 Adobe RGB (1998)、像素没被转换）。
#   于是"界面选了 Display P3、产出却是别的色彩空间"是个**静默**失败。
#   而本机系统色彩目录里并没有 Display P3 —— 所以要么用户自己装，要么我们装。
# Display P3 的公开定义：DCI-P3 三原色 + **D65** 白点 + **sRGB 传输曲线**
#（注意它和 DCI-P3 不是一回事：DCI-P3 是 DCI 白点 + γ=2.6，那一条见 decode.py 的警告）。
DISPLAY_P3_COMPAT_FILENAME = "DisplayP3-compatible.icc"
DISPLAY_P3_PRIMARIES_XY: dict[str, tuple[float, float]] = {
    "r": (0.6800, 0.3200),
    "g": (0.2650, 0.6900),
    "b": (0.1500, 0.0600),
}
DISPLAY_P3_WHITE_XY: tuple[float, float] = (0.3127, 0.3290)   # D65

# 描述名**就是** "Display P3"（= `PROFILE_NAME_DISPLAY_P3`），这一点是刻意的：
# Photoshop 的 `convertProfile()` 按描述名匹配，写成别的名字等于装了也用不上。
# Adobe RGB 那边要求第三方换措辞（"compatible with …"）是 Adobe 的单方要求；
# Display P3 是公开规范，没有这条约束。文件来源写在 `cprt`（版权）标签里如实说明。



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


# sRGB 传输曲线的参数（IEC 61966-2-1 / 公开规范值）
SRGB_GAMMA = 2.4
SRGB_A = 1 / 1.055
SRGB_B = 0.055 / 1.055
SRGB_C = 1 / 12.92
SRGB_D = 0.04045


def _icc_para_srgb_tag() -> bytes:
    """parametricCurveType **type 4**，参数就是 sRGB 的分段曲线。

    ⚠ 方向不能写反：ICC 显示类 profile 的 TRC 是 **设备值 → 线性光**（即"解码"方向，
    也就是人们常说的 EOTF），不是 sRGB 的**编码**函数。写反了会得到"双重 gamma"的
    暗画面 —— 上一轮先在原型里踩过这个坑，靠**独立复算**（灰度必须原样穿过）才发现。
    type 4: Y = (aX+b)^g + e （X ≥ d），否则 Y = cX + f。
    sRGB 在 d=0.04045 处两段连续且斜率连续，e=f=0 —— 与 LittleCMS 内置 sRGB profile
    用的是同一条参数化曲线（这也是选 type 4 而不是自己塞查表的原因：精确且只有几十字节）。
    """
    import struct

    return (
        b"para" + b"\0" * 4
        + struct.pack(">HH", 4, 0)          # 类型 4 + 保留
        + _s15f16(SRGB_GAMMA) + _s15f16(SRGB_A) + _s15f16(SRGB_B)
        + _s15f16(SRGB_C) + _s15f16(SRGB_D) + _s15f16(0.0) + _s15f16(0.0)   # e / f
    )


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
        raise AssertionError(TF('ICC 头长度错误：{0}', len(header)))

    return header + struct.pack(">I", len(tags)) + table + blobs


def build_display_p3_compatible_profile() -> bytes:
    """按公开规范生成 Display P3 等效矩阵型 ICC profile（ICC v2，约 220 字节）。

    **这是本程序自己算出来的文件，不是 Apple 的那份**：色度参数取公开规范值
    （DCI-P3 三原色 + D65 白点 + sRGB 传输曲线），文件头的 CMM/creator 留空，
    来源写在 `cprt` 里；只有 `desc` 必须是 "Display P3" —— 因为 Photoshop 的
    `convertProfile()` 按描述名匹配（见本区块顶部的说明）。

    正确性怎么保证（不靠"看起来对"）：
      · 自检里有**独立复算**：P3 数值 → TRC 解码 → XYZ(D65) → Bradford 到 PCS(D50)
        → 反适应回 D65 → sRGB → sRGB 编码，与 LittleCMS 的结果逐像素比对（≤2 码值）；
      · 另加一条"灰度必须原样穿过"的断言（P3 与 sRGB 共用同一条传输曲线与白点，
        中性灰在两个空间里的数值**必须相等**）—— 它专门盯"TRC 写反导致双重 gamma"
        这类缺陷（原型阶段真的踩过）。
    """
    import struct

    matrix_d65 = _rgb_to_xyz_matrix(DISPLAY_P3_PRIMARIES_XY, DISPLAY_P3_WHITE_XY)
    adapt = _bradford(_xy_to_xyz(DISPLAY_P3_WHITE_XY), ICC_PCS_D50)
    import numpy as np

    m_d50 = adapt @ np.array(matrix_d65, dtype=float)

    tags: dict[bytes, bytes] = {
        b"desc": _icc_desc_tag(PROFILE_NAME_DISPLAY_P3),
        b"cprt": _icc_text_tag(
            "Generated by Auto Color Diffusion from the published Display P3 spec "
            "(DCI-P3 primaries, D65 white, sRGB transfer). Not an Apple file."
        ),
        b"wtpt": _icc_xyz_tag(*ICC_PCS_D50),
        b"rXYZ": _icc_xyz_tag(float(m_d50[0, 0]), float(m_d50[1, 0]), float(m_d50[2, 0])),
        b"gXYZ": _icc_xyz_tag(float(m_d50[0, 1]), float(m_d50[1, 1]), float(m_d50[2, 1])),
        b"bXYZ": _icc_xyz_tag(float(m_d50[0, 2]), float(m_d50[1, 2]), float(m_d50[2, 2])),
        b"rTRC": _icc_para_srgb_tag(),
        b"gTRC": _icc_para_srgb_tag(),
        b"bTRC": _icc_para_srgb_tag(),
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
    header += b"\0" * 4                                    # CMM：留空（不是 Apple 做的）
    header += struct.pack(">I", 0x02100000)                # ICC v2.1
    header += b"mntr" + b"RGB " + b"XYZ "                  # 显示类 / RGB / PCS=XYZ
    header += struct.pack(">HHHHHH", 2026, 9, 26, 0, 0, 0)  # 创建时间
    header += b"acsp" + b"\0" * 4                           # 签名 + 平台
    header += struct.pack(">I", 0) + b"\0" * 4 + b"\0" * 4  # flags / 厂商 / 型号
    header += struct.pack(">Q", 0)                          # attributes
    header += struct.pack(">I", 0)                          # 渲染意图：感知
    header += _icc_xyz_tag(*ICC_PCS_D50)[8:]                # PCS 光源（12 字节）
    header += struct.pack(">I", 0)                          # creator
    header += b"\0" * 16 + b"\0" * 28                      # profile ID + 保留
    if len(header) != header_size:
        raise AssertionError(TF('ICC 头长度错误：{0}', len(header)))

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
    # Adobe 把**用户自己放**的 profile 放在这里（本机实测存在、且写进去 "+
    # Photoshop 的"转换为配置文件"列表认——注意它在 Profiles\ 子目录下，
    # 只列顶层目录会搜不到，所以另外单独列一份带子目录的路径）。
    r"%LOCALAPPDATA%\Adobe\Color\Profiles",
    r"%APPDATA%\Adobe\Color\Profiles",
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
                log.debug(T("找到 %s：%s"), filename, candidate)
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
            log.debug(T("使用磁盘上的 sRGB profile：%s（%d 字节）"), path, len(data))
            return data
        except OSError as exc:
            log.warning(T("读取 sRGB profile 失败，改用 LittleCMS 内置定义：%s"), exc)
    log.debug(T("磁盘上没有 sRGB.icc，使用 LittleCMS 内置 sRGB 定义（等价）"))
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

    生成三个文件，都不涉及第三方授权素材：

    | 文件 | 怎么来的 |
    |---|---|
    | `sRGB.icc` | LittleCMS 的公开定义（`createProfile("sRGB")`）当场构造 |
    | `AdobeRGB1998-compatible.icc` | 按 Adobe 公开的色度参数（D65 / gamma 563/256 / 三原色 xy）自算的等效矩阵型 profile；命名遵循 Adobe 官方要求的 "compatible with Adobe RGB (1998)" 措辞 |
    | `DisplayP3-compatible.icc` | 按 Display P3 公开规范（DCI-P3 三原色 / D65 / sRGB 传输曲线）自算的等效 profile。它**必须**由 `install_display_p3_profile()` 装进 Adobe 的色彩目录才起作用（PS 不扫程序目录）；生成在这里只是为了随包分发、并让用户能自己看一眼这个文件 |

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
            DISPLAY_P3_COMPAT_FILENAME: build_display_p3_compatible_profile(),
        }
    except Exception as exc:
        # 这个方法在启动时被调用，**不允许**因为色彩管理出问题而拦住整个程序：
        # sRGB 侧永远有 LittleCMS 内置定义兜底。
        log.warning(T("生成 ICC profile 失败（程序仍可运行，sRGB 走内置定义）：%s"), exc)
        return created
    for directory in (icc_dir(), generated_icc_dir()):
        # 只在"这个目录里一个待生成文件都还不存在"或"它可写"时才尝试
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug(T("ICC 目录不可写，换下一个：%s（%s）"), directory, exc)
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
                log.info(T("已生成 ICC profile：%s（%d 字节）"), target, len(data))
            except OSError as exc:
                log.debug(T("写入 ICC 失败：%s（%s）"), target, exc)
        # 该目录能写就停在这里（不重复往下一处写）
        if wrote_any or os.access(directory, os.W_OK):
            break
    return created


def find_profile_by_description(description: str, *, dirs: tuple[Path, ...] | None = None) -> Path | None:
    """按 **ICC 描述名** 找一份已安装的 profile（找不到返回 None）。

    为什么要它：Photoshop 的 `doc.convertProfile(name, ...)` **只认已安装的
    profile 名**（实测：传 ICC 文件完整路径也不行）。名字对不上就抛错，
    而 PS 仍然会正常导出、把**当前文档的 profile** 写进文件 ——
    于是“界面选了 Display P3，产出实际是 sRGB/Adobe RGB”（实测本机产出被标成
    Adobe RGB (1998)）。

    ⚠ **本函数只能说明“系统/Adobe 色彩目录里有没有”，不能当成“PS 能不能命中”**：
    Photoshop 自己还带一套内置 profile（本机实测：系统目录里没有 Adobe RGB 的 ICC
    文件，而 PS 的工作空间却能设成 Adobe RGB (1998) 并写进文件）。
    所以“找不到”只能当**提醒**，不能当断言；真正的信号是每次导出后由
    `ps_result.json` 的 `colorSpaceFailures` 报回来的**实测结果**。

    ``dirs`` 用来把搜索面收窄到"Photoshop 真的会扫"的目录（见 `display_p3_available`）：
    默认 `icc_search_dirs()` 里还包含**本程序自己的** `assets/icc`（开发时就是仓库目录），
    在那里找到一份"跟我们装出去的那份长得一样"的文件**并不代表 PS 找得到它**。
    """
    wanted = (description or "").strip().lower()
    if not wanted:
        return None
    for directory in (dirs if dirs is not None else icc_search_dirs()):
        root = Path(directory)
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.icc")) + sorted(root.rglob("*.icm")):
            try:
                # ⚠ 描述名在 `.profile` 上（`ImageCmsProfile` 自己没这个属性）。
                # 曾经写成 `ImageCmsProfile.profile_description`，AttributeError 被
                # 下面的 except 吃掉 → **永远返回 None**（探测功能静默失效）。
                opened = ImageCms.getOpenProfile(str(candidate))
                desc = (opened.profile.profile_description or "").strip().lower()
            except Exception:  # noqa: BLE001 - 坏文件不应影响搜索
                continue
            if desc == wanted:
                return candidate
    return None


def display_p3_available() -> Path | None:
    """**Photoshop 真的可能看到** 的 Display P3 profile（找不到返回 None）。

    只扫系统/Adobe 色彩目录，**不扫本程序自己的工作目录** —— 理由见
    `find_profile_by_description` 的 dirs 说明：那是"能搜到却不代表 PS 能用"的
    同款骗局（本机就是这么开始"选中 Display P3 却静默产出别的色彩空间"的）。

    返回 None **不代表**导出一定失败（PS 还带内置 profile），只说明
    “按名字去系统目录里找没找到” —— 用于启动日志与自动安装的判断依据。
    """
    return find_profile_by_description(
        PROFILE_NAME_DISPLAY_P3, dirs=system_profile_dirs()
    )


def adobe_profile_install_dirs() -> tuple[Path, ...]:
    """本程序**可以写入**的 Adobe 用户级色彩目录（按优先级）。

    只列用户级目录，**刻意不包括** `%WINDIR%\\System32\\spool\\drivers\\color`：
    往那里写需要管理员权限，本程序不弹 UAC（宁可不装，也不要让用户面对提权对话框）。

    实测依据（本机）：把 profile 放进 `%LOCALAPPDATA%\\Adobe\\Color\\Profiles`
    之后，Photoshop 的「转换为配置文件」列表就能看到它。

    ⚠ 每次调用都重新读环境变量（**不能加 lru_cache**）：自检要能把
    `%APPDATA%` / `%LOCALAPPDATA%` 指到临时目录，验证"不碰用户真实目录"。
    """
    out: list[Path] = []
    for raw in (r"%APPDATA%\Adobe\Color\Profiles", r"%LOCALAPPDATA%\Adobe\Color\Profiles"):
        expanded = os.path.expandvars(raw)
        if "%" in expanded:          # 环境变量没展开成功（变量不存在）
            continue
        path = Path(expanded)
        if path not in out:
            out.append(path)
    return tuple(out)


@lru_cache(maxsize=1)
def display_p3_profile_bytes() -> bytes:
    """自制 Display P3 等效 profile 的字节（带缓存）。

    优先用磁盘上已有的那份（随包分发或运行时自愈生成的），都没有就当场构造 ——
    与 `srgb_profile_bytes()` 同构。三者内容一致（同一个生成函数），所以"文件在不在"
    只影响是否重算，不影响正确性。
    """
    path = find_icc_file(DISPLAY_P3_COMPAT_FILENAME)
    if path is not None:
        try:
            data = path.read_bytes()
            if data:
                return data
        except OSError as exc:
            log.debug(T("读取 %s 失败，改为当场构造：%s"), path, exc)
    return build_display_p3_compatible_profile()


def install_display_p3_profile() -> tuple[Path | None, str]:
    """把自制的 Display P3 等效 profile 装进 Adobe 的用户级色彩目录。

    返回 ``(装好之后的路径 或 None, 人类可读的说明)``。三种情况都不抛异常
    （启动/导出前调用，色彩管理绝不能拦住主流程）：

    · 系统里**已经有**任何一份描述名为 "Display P3" 的 profile → 什么都不做
      （用户自己装的、或 Adobe 装的都优先，我们绝不覆盖别人的东西）；
    · 目标目录里已有同名文件但内容不同 → **跳过**（那可能是用户自己放的文件）；
    · 写入失败（目录只读/权限）→ 返回 None 并说明原因，由调用方决定是否出声。

    为什么必须装到 Adobe 目录（而不是放我们自己的 `assets/icc`）：
    Photoshop 只扫自己的色彩目录，程序目录里的 profile 它**看不见**。
    """
    existing = display_p3_available()
    if existing is not None:
        return existing, TF('已存在名为「{0}」的 profile，无需安装（{1}）',
                            PROFILE_NAME_DISPLAY_P3, existing)

    data = display_p3_profile_bytes()
    last_error = T("没有可写的 Adobe 色彩目录")
    for directory in adobe_profile_install_dirs():
        target = directory / DISPLAY_P3_COMPAT_FILENAME
        try:
            if target.is_file():
                if target.read_bytes() == data:
                    return target, TF('自制 Display P3 等效 profile 已就位（{0}）', target)
                last_error = TF('{0} 已存在同名但内容不同的文件，已跳过（不覆盖用户文件）', target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            last_error = TF('写入 {0} 失败：{1}', target, exc)
            continue
        return target, TF(
            '已安装自制 Display P3 等效 profile：{0}（{1} 字节；Photoshop 下次启动即可按「{2}」使用）',
            target, len(data), PROFILE_NAME_DISPLAY_P3,
        )
    return None, last_error


def color_space_needs_display_p3(color_space: str) -> bool:
    """这个"输出色彩空间"选项是不是 Display P3。

    比对的是 `COLOR_SPACE_TO_PS_PROFILE` 里的**profile 名**（而不是在代码里
    重写一遍 "Display P3" 字面量）：界面选项名与 PS 的 profile 名是两张表，
    只有这张映射表知道它们怎么对应。
    """
    from ..constants import COLOR_SPACE_TO_PS_PROFILE

    return COLOR_SPACE_TO_PS_PROFILE.get(color_space) == PROFILE_NAME_DISPLAY_P3


def describe_optional_profiles() -> str:
    """描述 ICC 资源的就位情况（启动时写进日志）。

    这里必须区分两种"没有"：
      · **缺失** —— 它本该起作用却不在（输入侧会退到 rawpy，慢但颜色正确）→ 值得 WARNING；
      · **未安装** —— 输出侧的 Display P3：程序会在需要时**自己装**一份等效 profile
        （见 `install_display_p3_profile`），所以这只是告知，不是警告。
        两件事用**不同的词**，是为了让 `主界面` 那句"资源不齐全"的 WARNING
        仍然只在 Adobe RGB 真缺失时出现（不然每次启动都刷一条假警告，
        真正的警告就被淹没 —— "狼来了"比不报警更糟）。
    """
    lines: list[str] = []
    for filename, purpose in (
        (ADOBE_RGB_ICC_FILENAME, "Adobe 官方那份；缺失时用自带的 "
                                 f"{ADOBE_RGB_COMPAT_FILENAME} 等效 profile 兜底"),
        (ADOBE_RGB_COMPAT_FILENAME, T("程序按公开参数自算的等效 profile（输入侧兜底用）")),
    ):
        found = find_icc_file(filename)
        lines.append(
            f"  - {filename}：已就位（{found}）" if found is not None
            else f"  - {filename}：{T('缺失')}（{purpose}）"
        )

    # Display P3 这一行看的是"Photoshop 能不能按名字找到"（只扫 Adobe/系统目录）。
    installed = display_p3_available()
    if installed is not None:
        lines.append(f"  - {PROFILE_NAME_DISPLAY_P3}：{T('已安装')}（{installed}）")
    else:
        lines.append(
            f"  - {PROFILE_NAME_DISPLAY_P3}：{T('未安装')}"
            f"（{T('导出时若选它，程序会自动安装自制的等效 profile')}："
            f"{'、'.join(str(d) for d in adobe_profile_install_dirs())}）"
        )
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
            return ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes)), T("嵌入 ICC（img.info['icc_profile']）")
        except Exception as exc:
            # profile 损坏时不能直接丢弃：记录并继续往下走 EXIF 判定。
            log.warning(T("嵌入 ICC profile 解析失败，转用 EXIF 判定：%s"), exc)

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
                TF('预览图声明为 Adobe RGB(1998)（EXIF InteropIndex=R03），但找不到 {0}。\n已搜索：{1}\n这个 profile 受 Adobe 授权限制，本程序不能自带。\n两个选择：\n  1) 从 Photoshop 安装目录或系统色彩目录把它复制到 {2}（文件名保持 {3} 即可）；\n  2) 什么都不做 —— 程序会自动改用 rawpy 全解码，颜色是正确的，只是每张慢 1–5 秒。', ADOBE_RGB_ICC_FILENAME, searched, icc_dir(), ADOBE_RGB_ICC_FILENAME)
            )

    # 硬约束 #2 的默认假设：不带 profile 的图像按 sRGB 解释。
    return ImageCms.ImageCmsProfile(io.BytesIO(srgb_profile_bytes())), T("无 profile，按 sRGB 解释")


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
            raise ValueError(T("ImageCms.profileToProfile 返回了 None"))
        return converted, reason
    except Exception as exc:
        log.warning(T("ICC 转换失败（源判定：%s），已退化为不做色彩变换：%s"), reason, exc)
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


def rotate_to_display(img: "Image.Image", orientation: str | None) -> "Image.Image":
    """把**存储帧**的图像旋转成**显示帧**（按 EXIF 方向）。

    为什么这个函数必须存在（2026-09-26 用户反馈「AI 蒙版没画准」的根因）：
      · 相机嵌在 RAW 里的预览 JPEG **常常不带自己的方向标签**（实测 Canon CR3：
        `getexif()` 里根本没有 274，方向只写在**文件级** IFD 里），
        所以 `ImageOps.exif_transpose()` 对它**什么也不做** ——
        我们送给 AI 的是一张**躺倒的照片**（实测：`_G4A1619.CR3` 拿到 1024×683，
        而它显示的应该是 683×1024 竖构图）；
      · 而提示词告诉模型「坐标用显示帧（你看到的方向，天空在上方）」→
        模型看到的是躺倒的图，它给的坐标理所当然也是躺倒那一版的，
        我们又按显示帧再转一次 → **转了两遍**，蒙版就画到别处去了。
    旋转方向用 `rotation_degrees()`（唯一的真值来源）；PIL 的 `rotate(angle)`
    是逆时针为正，所以取负值。
    """
    from .orientation import normalize, rotation_degrees

    degrees = rotation_degrees(normalize(orientation))
    if not degrees:
        return img
    return img.rotate(-degrees, expand=True)


def build_srgb_thumbnail(
    image_bytes: bytes,
    long_edge: int = THUMB_LONG_EDGE,
    orientation: str | None = None,
) -> tuple[bytes, str]:
    """从图像字节（嵌入式预览）生成 sRGB 缩略图。

    完整链路（每一步都不能少）：
        解码 → 方向校正 → ICC 转换到 sRGB → 长边缩至 1024 → q85 JPEG + 嵌入 sRGB profile

    方向校正分两步，缺一不可：
      1. `ImageOps.exif_transpose`：应用**预览字节自带**的方向标签（部分机型会带）；
      2. `orientation` 参数（调用方传入的**文件级** EXIF 方向）：只在预览自己**没有**
         可用标签时才用它 —— Canon/Nikon 的嵌入式预览正是这种情况，
         只做第 1 步就会把竖拍照片躺倒着送给 AI（详见 `rotate_to_display`）。

    返回 (JPEG 字节, 色彩判定依据说明)。
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        # 预览自己带的方向标签（EXIF 274）。0 / 1 / None 都算"没有可用的标签"。
        own_tag: int | None = None
        try:
            own_tag = img.getexif().get(EXIF_TAG_ORIENTATION)
        except Exception:  # noqa: BLE001 - 读不到标签不该拦住缩略图
            own_tag = None
        # exif_transpose 会应用方向并移除该标签，避免后续编码再带上导致二次旋转。
        img = ImageOps.exif_transpose(img)
        if orientation and own_tag in (None, 0, 1):
            img = rotate_to_display(img, orientation)
        srgb_img, reason = to_srgb(img)
        resized = resize_long_edge(srgb_img, long_edge)
        return encode_jpeg_srgb(resized), reason


def build_srgb_thumbnail_from_array(
    rgb_array,
    long_edge: int = THUMB_LONG_EDGE,
    orientation: str | None = None,
) -> bytes:
    """从 numpy RGB 数组生成 sRGB 缩略图（rawpy 兜底路径用）。

    rawpy 的 postprocess 默认 output_color=SRGB（已应用相机矩阵与 sRGB gamma），
    因此这里**不再做 ICC 转换**，只标记为 sRGB 并嵌入 profile。
    `orientation` 与 `build_srgb_thumbnail` 同义（rawpy 产出的是**存储帧**像素，
    同样需要按文件级方向摆正）。⚠ **已经摆正过的数组**（例如 TIFF/HEIF 直接解码
    那条路，它自己已经应用过方向）必须**不传**这个参数，否则会转两遍。

    若将来支持其他 output_color，这里需要补对应的源 profile。
    """
    import numpy as np  # 局部导入：仅 rawpy 兜底路径需要，避免主流程强依赖

    if rgb_array.dtype != np.uint8:
        # rawpy 设 output_bps=8 时应已是 uint8；保守起见做一次裁剪+转换。
        rgb_array = np.clip(rgb_array, 0, 255).astype(np.uint8)
    img = Image.fromarray(rgb_array, mode="RGB")
    if orientation:
        img = rotate_to_display(img, orientation)
    resized = resize_long_edge(img, long_edge)
    return encode_jpeg_srgb(resized)
