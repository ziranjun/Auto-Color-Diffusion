# -*- coding: utf-8 -*-
"""TIFF / HEIF 的直接解码（V1.1.0 起）。

为什么单独一个模块（而不是塞进 preview.py）
------------------------------------------
RAW 走的是"**嵌入式预览** → rawpy 兜底"那条路，因为 RAW 里的 JPEG 预览就是相机
认可的观感。而 TIFF / HEIF **文件本身就是位图**：没有"嵌入式预览"这一说，
直接解码拿到的就是 ACR/Photoshop 看到的那份像素。两者的失败原因、色彩处理、
性能特征完全不同，混在一个函数里会让两边都难改（旧的 extract_thumbnail 已经
够长了）。

【HEIF 的 HDR 必须转换 —— 2026-09-26 真机标定（含高光不裁剪）】
佳能 HIF（以及多数手机 HEIC）是 **HDR PQ**：nclx 写着 BT.2020 原色 + PQ 传输
（transfer_characteristics=16）。pillow-heif 解码出来的是 **PQ 码值本身**
（实测：8 位输出 = 10 位原始码值 / 65535 × 255，没有任何传输函数变换；
`Image.open` / `open_heif(convert_hdr_to_8bit=True)` / 16 位原始三者逐像素一致）。
直接当 sRGB 用会得到一张“死黑”的图（中位数 67/255，而 Photoshop 看到的是 52）。

用户 2026-09-26 的反馈：“预览原 HEIF 时高光处细节是保留的，处理过后高光处细节完全爆掉”。
根因就是下面这一步的**硬裁剪**，已用实测修正：

    实测 2G4A1596.HIF：峰值 754 nit，p95=564 nit，**42.6% 的像素 > 203 nit**
    · 旧行为（203 nit 当白后 clip）→ 36.0% 的像素被压平成 255（p95=p99.9=255）
    · Photoshop 渲染同一张 .heic（参照）→ **裁剪 0.0%**，max 253，p95 247
    · 现在（203 nit 白点 + 平滑肩部）→ **裁剪 0.0%**，max 249，p95 246
                                    mean 111.2 / p50 26（PS：110.1 / 28）
    2G4A1620.HIF：旧裁剪 6.6% → 现在 0.0%；mean 68.5 / p50 55（PS：64.5 / 52）

映射形式（参数由逐 nit 分桶的 Photoshop 锚点联合最小二乘拟合）：
    y = u / (1 + u^q)^(1/q)，  u = x * 1.25，  x = 亮度 / 203 nit，  q = 1.6
逐锚点残差 ≤ 3.8 码值（11–12 个锚点、两张样片，加权 RMS 2.09 码值）。
**不要退回“203 nit 直接当白”** —— 那等于把 203 nit 以上的细节全部扔掉。

另外：Photoshop/ACR 对 **非 RAW**（TIFF）默认是近乎直通的（实测：
未处理的 44430003.TIF 我们解码 mean=120.2、PS 出片 mean=120.17），
所以这份中间 TIFF 的像素几乎就是最终观感 —— 标定必须在这里做对。

【为什么 HEIF 还要"转成 TIFF"出片】
见 acb/constants.py::HEIF_EXTENSIONS 的注释：Photoshop 完全不理会 HEIC 里的
相机设置（内嵌/两种旁侧命名都试过），而 TIFF 那条通道实测有效。
"""

from __future__ import annotations
from ..i18n import T, TF

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from ..logging_setup import get_logger
from . import icc

log = get_logger("decode")

# HEIF 解码需要 pillow-heif 插件（libheif 的 Python 绑定）。
# 缺它时给出**能照做**的安装指引，而不是一句 ImportError。
HEIF_PLUGIN_PACKAGE = "pillow-heif"

# BT.2408 的 HDR 漫反射白参考值（nit）。见模块注释里的标定数据。
HDR_DIFFUSE_WHITE_NITS = 203.0

# ---- HDR→SDR 的“平滑肩部”（2026-09-26 用 Photoshop 实测标定，勿凭感觉改）----
# 旧实现把 203 nit 直接当 sRGB 白（线性 1.0），超过的部分 **clip**。
# 对真正的高光内容是灾难：实测 2G4A1596.HIF 峰值 754 nit、**42.6% 的像素超过 203 nit**，
# 于是 36% 的像素被压平成 255（天空整片死白、云层细节全丢）；
# 而 Photoshop 自己渲染同一张 .heic 时 **一个像素都没有裁剪**（最大 253）。
# 现在改用一条**平滑肩部**把线性光压进 [0,1)：
#     y = u / (1 + u^q)^(1/q)，  u = x * KNEE，  x = 亮度 / 203 nit
# 两个参数由“逐 nit 分桶取 Photoshop 渲染值中位数”的锚点做联合最小二乘得到
# （两张样片、11–12 个锚点，加权 RMS 2.09 码值、最大残差 3.82 码值）。
HDR_ROLLOFF_KNEE = 1.25      # 软膝位置（白点单位的倍数）
HDR_ROLLOFF_POWER = 1.6      # 肩部陡度（越大越“软”）

# HLG 的 OOTF（BT.2100）：显示亮度 = 峰值 nit × 场景线性^1.2。
# 1000 nit 是 BT.2100 对 HLG 的标称峰值亮度 —— 这个值**未用样片实测过**
# （手头只有 PQ 样片），所以它按标准取值而不是拟合值，遇到 HLG 样片要重新标定。
HLG_NOMINAL_PEAK_NITS = 1000.0
HLG_SYSTEM_GAMMA = 1.2

# nclx 里的枚举值（ISO/IEC 23001-8）。
# ⚠ **DCI-P3 与 Display P3 不是同一个东西**（用户 2026-09-26 特意提醒）：
#   原色相同，但白点与传递曲线都不同 ——
#     · Display P3（12，SMPTE EG 432-1）：D65 白点 + **sRGB 传输曲线**（分段），消费级；
#     · DCI-P3（11，SMPTE RP 431-2）：**DCI 白点**（x=0.314, y=0.351，偏绿）+ **γ=2.6**，影院参考。
#   把两者当一个东西处理（或者看到 P3 就套 γ=2.6）就会得到偏绿＋中位偏暗的图。
#   两条路都不猜：有 ICC 时按 profile 里的 TRC 解（手机 HEIC 实际走这条，
#   实测小米那张的 profile 是 `para` 参数化曲线 = sRGB 传输，不是 `curv` 的 γ），
#   只有 nclx 时按下面这张表解。
_PRIMARIES_BT709 = 1
_PRIMARIES_BT2020 = 9
_PRIMARIES_DCI_P3 = 11
_PRIMARIES_DISPLAY_P3 = 12
_TRANSFER_BT709 = 1
_TRANSFER_SRGB = 13
_TRANSFER_PQ = 16
_TRANSFER_ST428_1 = 17        # SMPTE ST 428-1：DCI 的纯 γ=2.6 幂律
_TRANSFER_HLG = 18

# 原色/白点表（xy 色度）。与 `raw/icc.py` 用同一套几何参数。
_D65_WHITE = (0.3127, 0.3290)
_DCI_WHITE = (0.314, 0.351)                       # SMPTE RP 431-2
_BT709_XY = {"r": (0.640, 0.330), "g": (0.300, 0.600), "b": (0.150, 0.060)}
_BT2020_XY = {"r": (0.708, 0.292), "g": (0.170, 0.797), "b": (0.131, 0.046)}
_DCI_P3_XY = {"r": (0.680, 0.320), "g": (0.265, 0.690), "b": (0.150, 0.060)}
_PRIMARIES_TABLE: dict[int, tuple[dict[str, tuple[float, float]], tuple[float, float]]] = {
    _PRIMARIES_BT709: (_BT709_XY, _D65_WHITE),
    _PRIMARIES_BT2020: (_BT2020_XY, _D65_WHITE),
    _PRIMARIES_DCI_P3: (_DCI_P3_XY, _DCI_WHITE),
    _PRIMARIES_DISPLAY_P3: (_DCI_P3_XY, _D65_WHITE),
}

_heif_registered: bool | None = None


class DirectDecodeError(Exception):
    """TIFF / HEIF 直接解码失败。"""


# ============================================================================
# pillow-heif 的可用性
# ============================================================================

def ensure_heif_support() -> bool:
    """注册 pillow-heif（幂等）。缺包时返回 False，**不抛异常**。

    调用方（extract_thumbnail）据此把"没装插件"变成一条清晰的提示，
    而不是让 ImportError 在批量任务里把整批打断（硬约束 #10）。
    """
    global _heif_registered
    if _heif_registered is not None:
        return _heif_registered
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        log.info(T("HEIF 支持已就绪（pillow-heif %s，libheif %s）"),
                 pillow_heif.__version__, pillow_heif.libheif_version())
        _heif_registered = True
    except Exception as exc:  # noqa: BLE001 - 缺依赖不该让程序起不来
        log.warning(T("HEIF 支持不可用（%s）：%s"), HEIF_PLUGIN_PACKAGE, exc)
        _heif_registered = False
    return _heif_registered


def heif_install_guide() -> str:
    """HEIF 插件缺失时的指引（写进文件级警告与日志）。"""
    return TF(
        "读取 HEIF（.heic/.heif/.hif）需要 {0}。\n"
        "安装：python -m pip install -r requirements.txt（或 pip install {0}）后重启本程序。\n"
        "在此之前，HEIF 文件会被跳过（其它格式不受影响）。",
        HEIF_PLUGIN_PACKAGE,
    )


# ============================================================================
# 色彩转换：nclx（BT.2020/HDR）→ sRGB
# ============================================================================

def _pq_eotf(code):
    """PQ（SMPTE ST 2084）EOTF：码值 → 绝对亮度（0..1 对应 0..10000 nit）。"""
    import numpy as np

    m1, m2 = 0.1593017578125, 78.84375
    c1, c2, c3 = 0.8359375, 18.8515625, 18.6875
    vp = np.maximum(code, 0.0) ** (1.0 / m2)
    num = np.maximum(vp - c1, 0.0)
    den = c2 - c3 * vp
    return np.clip((num / np.maximum(den, 1e-12)) ** (1.0 / m1), 0.0, 1.0)


def _hlg_eotf(code):
    """HLG（BT.2100）逆 OETF：码值 → 场景线性（0..1）。"""
    import numpy as np

    a, b, c = 0.17883277, 0.28466892, 0.55991073
    v = np.maximum(code, 0.0)
    low = (v * v) / 3.0
    high = (np.exp((v - c) / a) + b) / 12.0
    return np.clip(np.where(v <= 0.5, low, high), 0.0, 1.0)


def _hlg_scene_to_display(scene):
    """HLG 的**场景线性** → “白点单位”（1.0 = 203 nit 漫反射白）。

    BT.2100 的 HLG 需要两步：逆 OETF 得到**场景**线性，再过 OOTF 得到显示亮度
    （peak × 场景线性^γ）。这一步单独写，是因为 HLG 与 PQ 的差别就在它：
    PQ 已经是绝对亮度，HLG 不是。
    """
    import numpy as np

    nits = HLG_NOMINAL_PEAK_NITS * np.power(np.maximum(scene, 0.0), HLG_SYSTEM_GAMMA)
    return nits / HDR_DIFFUSE_WHITE_NITS


def _hdr_rolloff(white_units):
    """把“以 203 nit 为 1.0”的线性光压进 0..1（平滑肩部，**永不裁剪**）。

    【为什么必须有它】高光细节就活在这一步：真硬裁剪（旧行为）会把 203 nit 以上的
    一切都压成 255，而实际素材里那片区域常常占三分之一以上的像素。

    【参数怎么来的】逐 nit 分桶，取 Photoshop 自己渲染同一张 .heic 后对应像素的
    中位数亮度，得到 11–12 个 (nit → 显示线性) 锚点；再在这一族曲线上做联合
    最小二乘（两张样片）。拟合结果：加权 RMS 2.09 码值、最大残差 3.82 码值。
    """
    import numpy as np

    u = np.maximum(white_units, 0.0) * HDR_ROLLOFF_KNEE
    return u / np.power(1.0 + np.power(u, HDR_ROLLOFF_POWER), 1.0 / HDR_ROLLOFF_POWER)


def _inverse_oetf(code, transfer: int):
    """按传输特性把码值变成线性光（0..1；PQ 为 0..10000 nit 归一）。"""
    import numpy as np

    if transfer == _TRANSFER_PQ:
        return _pq_eotf(code)
    if transfer == _TRANSFER_HLG:
        return _hlg_eotf(code)
    if transfer == _TRANSFER_ST428_1:
        # SMPTE ST 428-1（DCI）：纯幂律 γ=2.6。
        # 这就是“DCI-P3 的 γ 与 Display P3 的 sRGB 传输不一样”那个差异的落点：
        # 同一张 50% 灰，在 γ2.6 下线性值 0.218，在 sRGB 下是 0.214——但中间调差得更多
        # （编码值 0.5 对应线性 0.218 vs 0.214；而编码值 0.25 → 0.0204 vs 0.0508）。
        return np.maximum(code, 0.0) ** 2.6
    if transfer == _TRANSFER_BT709:
        # BT.1886 的纯幂律近似（HEIF 里极少见，保守处理）
        return np.maximum(code, 0.0) ** 2.4
    # sRGB（13）或未知：按 sRGB 逆 OETF
    return np.where(code <= 0.04045, code / 12.92, ((code + 0.055) / 1.055) ** 2.4)


def _srgb_oetf(linear):
    """线性光 → sRGB 码值。"""
    import numpy as np

    v = np.clip(linear, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * (v ** (1.0 / 2.4)) - 0.055)


def _primaries_matrix_matrix(src_primaries: int, dst: str = "srgb"):
    """原色转换矩阵（线性光域）。BT.2020 / P3 → sRGB 用项目自己的色彩数学算。

    为什么自己算而不是抄一份常数矩阵：`icc._rgb_to_xyz_matrix` 已经在本项目里
    用了很久（生成 sRGB/Adobe RGB 等效 profile），算出来的矩阵与公认的常数矩阵一致，
    少一份需要维护的“魔法数字”。**白点也必须跟着原色走**：
    DCI-P3 的白点是 DCI 白（偏绿），Display P3 是 D65 —— 拿同一个白点去算两者，
    就等于把两种 P3 当成一种（见上面枚举值的注释）。
    """
    import numpy as np

    src = _PRIMARIES_TABLE.get(src_primaries)
    if src is None:
        src = (_BT709_XY, _D65_WHITE)
    src_xy, src_white = src
    m_src = np.array(icc._rgb_to_xyz_matrix(src_xy, src_white), dtype=float)
    m_dst = np.array(icc._rgb_to_xyz_matrix(_BT709_XY, _D65_WHITE), dtype=float)
    return np.linalg.inv(m_dst) @ m_src


@dataclass
class DecodedImage:
    """直接解码的结果。"""

    array: object                     # numpy uint8 RGB
    width: int
    height: int
    reason: str                       # 色彩判定依据（写进日志/提示词来源说明）
    warnings: list[str] = field(default_factory=list)


def _nclx_summary(nclx: dict) -> str:
    return (f"原色={nclx.get('color_primaries')} "
            f"传输={nclx.get('transfer_characteristics')}")


def convert_to_srgb8(img: Image.Image, nclx: dict | None) -> tuple[object, str, list[str]]:
    """把解码得到的图转成 8 位 sRGB 数组（HEIF 的 HDR/宽色域在这里落地）。"""
    import numpy as np

    warnings: list[str] = []
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0

    if not nclx:
        # 没有 nclx：TIFF/HEIC 都可能是"本来就写着 sRGB"。
        # 有 ICC 块时走 ICC 转换（更权威），否则按 sRGB 解释。
        icc_bytes = img.info.get("icc_profile")
        if icc_bytes:
            converted, reason = icc.to_srgb(img)
            return (np.asarray(converted.convert("RGB"), dtype=np.uint8),
                    reason, warnings)
        return ((arr * 255.0 + 0.5).astype(np.uint8),
                T("无 ICC/nclx，按 sRGB 解释"), warnings)

    primaries = int(nclx.get("color_primaries") or 0)
    transfer = int(nclx.get("transfer_characteristics") or 0)
    linear = _inverse_oetf(arr, transfer)

    # 这个图是 HDR 吗（需要那一记“平滑肩部”）？
    hdr = transfer in (_TRANSFER_PQ, _TRANSFER_HLG)

    if transfer == _TRANSFER_PQ:
        # PQ 给的是**绝对亮度**（0..1 = 0..10000 nit），先换成“203 nit = 1.0”的白点单位。
        linear = linear / (HDR_DIFFUSE_WHITE_NITS / 10000.0)
        reason = TF("HEIF/HDR：PQ → SDR（{0:g} nit 白点 + 平滑肩部，不裁剪）",
                    HDR_DIFFUSE_WHITE_NITS)
    elif transfer == _TRANSFER_HLG:
        # HLG 给的是**场景线性**，必须再过 OOTF 才是显示亮度（见 _hlg_scene_to_display）。
        linear = _hlg_scene_to_display(linear)
        reason = "HEIF/HDR：HLG → SDR（BT.2100 OOTF + 平滑肩部，不裁剪）"
    elif transfer == _TRANSFER_ST428_1:
        # DCI 的 γ=2.6（影院参考）。它**不是** Display P3 的 sRGB 传输曲线。
        reason = "HEIF：按 DCI（SMPTE ST 428-1，γ=2.6）解释"
    elif transfer in (_TRANSFER_BT709, _TRANSFER_SRGB):
        reason = "HEIF：按 SDR（BT.709/sRGB）解释"
    else:
        reason = TF("HEIF：未知传输特性 {0}，按 sRGB 解释", transfer)
        warnings.append(TF("该 HEIF 声明的传输特性 {0} 不认识，色彩按 sRGB 解释。", transfer))

    if primaries == _PRIMARIES_DCI_P3:
        # ⚠ 这一条必须说出来：DCI-P3 与 Display P3 原色相同、白点与传输都不同。
        # 标成 DCI-P3 的文件很可能是被标错的 Display P3，但**我们不猜** ——
        # 按标准解，并把差异如实写进警告（用户能自己判断是哪一种）。
        reason += "，DCI-P3 → sRGB（DCI 白点）"
        warnings.append(TF(
            '该 HEIF 声明的是 DCI-P3（影院参考：DCI 白点 + γ=2.6），'
            '它与 Display P3 不是同一种 —— 原色相同，但白点与传输曲线都不同。'
            '本程序按声明解；若这个文件其实是 Display P3 却被标成 DCI-P3，'
            '画面会略微偏绿、中间调偏暗。'
        ))
    elif primaries == _PRIMARIES_DISPLAY_P3:
        reason += "，Display P3 → sRGB"
    elif primaries in _PRIMARIES_TABLE:            # BT.709 / BT.2020
        pass
    elif primaries not in (0,):
        warnings.append(TF("该 HEIF 声明的原色 {0} 不是 BT.2020/BT.709/P3，按 sRGB 解释。",
                           primaries))

    if primaries in _PRIMARIES_TABLE and primaries != _PRIMARIES_BT709:
        matrix = _primaries_matrix_matrix(primaries)
        linear = linear @ np.asarray(matrix, dtype=np.float32).T
        if primaries == _PRIMARIES_BT2020:
            reason += "，BT.2020 → sRGB"

    # ⚠ 肩部必须压在**原色矩阵之后**：它是显示域的非线性压缩，
    #    放在矩阵之前会按通道各压各的，BT.2020 里尤其会把饱和度带跑偏。
    if hdr:
        linear = _hdr_rolloff(linear)

    srgb = _srgb_oetf(linear)
    return ((srgb * 255.0 + 0.5).astype(np.uint8), reason, warnings)


# ============================================================================
# 解码入口
# ============================================================================

def decode_srgb(path: Path, *, orient: bool = True) -> DecodedImage:
    """解码 TIFF / HEIF 为 8 位 sRGB 数组。

    orient=True 时应用 EXIF 方向（HEIF 的方向由 pillow-heif 在处理时应用，
    这里再兜一次是幂等的：方向标签已被它写成 1）。**送给 AI 的预览与
    送给 ACR 的 TIFF 必须是同一个方向**，否则局部调整的坐标会错位。
    """
    from ..constants import is_heif

    if is_heif(path.name) and not ensure_heif_support():
        raise DirectDecodeError(f"{path.name}：{heif_install_guide()}")

    try:
        with Image.open(path) as img:
            nclx = img.info.get("nclx_profile")
            img.load()
            if orient:
                img = ImageOps.exif_transpose(img)
            array, reason, warnings = convert_to_srgb8(img, nclx)
    except DirectDecodeError:
        raise
    except Exception as exc:
        raise DirectDecodeError(
            TF('{0}：解码失败（{1}）。\n该文件可能已损坏，或使用了本程序尚不支持的变体。',
               path.name, exc)
        ) from exc

    if nclx and "HDR" in reason:
        log.info(T("%s：%s（nclx %s）"), path.name, reason, _nclx_summary(nclx))
    else:
        log.debug(T("%s：%s"), path.name, reason)
    return DecodedImage(array=array, width=array.shape[1], height=array.shape[0],
                        reason=reason, warnings=warnings)


def write_intermediate_tiff(path: Path, target: Path) -> tuple[Path, str, list[str]]:
    """把 HEIF 解码成 8 位 sRGB TIFF，作为「ACR 能读的设置载体」。

    【为什么必须这么绕】见 constants.HEIF_EXTENSIONS 的注释：Photoshop/ACR
    对 HEIC 里的相机设置完全视而不见，而 TIFF 的 crs: 设置实测有效。
    于是 HEIF 的出片链路是：我们解码 → TIFF（内嵌 crs: 设置）→ Photoshop 导出。

    【为什么是 8 位而不是 16 位】实测过两条路，结论都是 8 位：
      · Pillow **写不了** 16 位 RGB TIFF：`Image.fromarray(uint16 三通道)` 直接
        报 "Cannot handle this data type"，而指定 `mode="RGB"` 会静默降成 8 位
        （实测写出的文件 bits=(8,8,8)）；
      · Photoshop 自己打开 HEIC 也只有 **8 位**（实测 doc.bitsPerChannel=8）——
        也就是说，Photoshop 那条路拿到的精度并不比我们这份高。
    代价要如实说：ACR 在 8 位数据上做很大幅度的调整（比如阴影 +80）会比在
    16 位上更容易出现断层。这是 HEIC 本身的导入精度决定的，不是本程序省掉了什么。

    目标目录由调用方给（数据目录下的中转目录，退出时自动清理）。
    ICC 一律写成 sRGB：我们的转换结果就是 sRGB，**必须声明**，
    否则 ACR 会按"未知"处理。
    """
    decoded = decode_srgb(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(decoded.array, mode="RGB")
    try:
        # 显式写方向标签（EXIF Orientation=274 值 1）：
        #   · 像素已经正过了，声明"未旋转"才与之一致；
        #   · 读方向的那段代码（raw/orientation.py）对"读不到标签"是**不猜**的，
        #     不写就会让带局部调整的 HEIF 在这一步报错。
        img.save(target, format="TIFF", compression="tiff_deflate",
                 icc_profile=icc.srgb_profile_bytes(), tiffinfo={274: 1})
    except Exception as exc:
        raise DirectDecodeError(
            TF('{0}：无法写出中转 TIFF（{1}）。', path.name, exc)
        ) from exc
    log.info(T("%s：已解码为 sRGB TIFF（%d×%d，%s）"),
             path.name, decoded.width, decoded.height, target.name)
    warnings = list(decoded.warnings)
    warnings.append(
        TF('{0} 的相机设置无法由 Photoshop/ACR 直接读取（实测），'
           '已改为「解码成 TIFF → 写入设置 → 由 Photoshop 出片」；'
           '原文件不会被改动。', path.name)
    )
    return target, decoded.reason, warnings
