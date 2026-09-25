# -*- coding: utf-8 -*-
"""非 AI 全链路自检 —— 不花钱、不联网、不改动你的照片。

用途
----
在真正跑一批照片之前，先用它确认程序自身的各个部件都正常工作：
    1. 运行时路径解析（%APPDATA% / 便携模式）
    2. models.yaml 加载与校验
    3. 断点键的稳定性（移动文件夹后键不变——这是硬约束 #11 修正后的关键行为）
    4. 导出命名规则（_E / _E_2 去重、防叠加后缀）
    5. jsx / manifest / bat 产物生成
    6. failed.json 的累计计数与永久跳过，以及清除失败记录
    7. XMP 读-改-写往返（结构指纹是否保持）
    8. exiftool 检测与真实 RAW 的预览提取
    9. 离线调试假结果的合法性与适配器契约（不联网也能验证整条链路）
   10. 重试策略常量之间的**对齐约束**（超时/重试/永久跳过/预算倍数）
   11. 产物位置：脚本三件套只在 <数据目录>/runs/ 下，出片目录里只剩照片

这些用例全部在**临时目录**里操作，不会写你真实照片旁边的 XMP，
也不会留下垃圾。唯一的例外是第 7 项与第 8 项需要读取你提供的真实文件，
但只读不写。

用法
----
    python tools/smoke_test.py
    python tools/smoke_test.py --raw-dir test_data     # 顺带实测真实 RAW 的预览提取
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
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

from acb.cache import file_key  # noqa: E402
from acb.constants import EXPORT_FORMATS, resolve_export_format  # noqa: E402

PASS = "[通过]"
FAIL = "[失败]"

_results: list[tuple[str, bool, str]] = []
_skipped: list[str] = []


def record(name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> None:
    """记一条自检结果。

    `skipped=True` 用于“条件不满足所以根本没实测”的用例（例如没给 --raw-dir
    就没有真实 RAW 可验）。为什么要单独一类：以前这种用例直接 record(..., True,
    "跳过") —— 汇总里就是“全部通过”，而实际上那些断言一条都没跑过
    （实测踩到过：整轮绿着，模型下拉其实是空的）。现在汇总行会单独报出
    “其中 N 项未实测”，骗不了人。
    """
    _results.append((name, ok, detail))
    if skipped:
        _skipped.append(name)
    marker = PASS if ok else FAIL
    print(f"{marker} {name}" + (f"\n        {detail}" if detail else ""))


def sample_material(pattern: str = "*.CR3") -> list[Path]:
    """仓库 `test_data/` 里的真实样本；没有这个目录就返回空列表。

    ⚠ **素材缺失 ≠ 失败**。`test_data/` 是开发者自己的照片：它不进版本库，
    也不进源码备份（见 README「发布流程」）。新克隆的仓库里它不存在是**正常状态**，
    所以用到它的用例必须记成「未实测」而不是红。

    这条纪律是实测踩出来的：拿源码备份（不含 test_data）在干净虚拟环境里跑自检，
    三个用例直接红、还有一个 `iterdir()` 抛 FileNotFoundError 把整轮打断 ——
    把"没有素材"报成"代码坏了"，会让接手的人彻底走错方向。
    """
    directory = Path(__file__).resolve().parent.parent / "test_data"
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(pattern) if p.is_file())


def record_missing_material(name: str, reason: str = "仓库里没有 test_data/ 样本") -> None:
    """素材不足：记一条「未实测」（不计失败），调用方据此提前返回。"""
    record(f"{name}（未实测：{reason}）", True, "素材缺失，跳过", skipped=True)


# ---------------------------------------------------------------------------
# 0. 真实数据目录的快照 / 差分（常驻断言，不只是排查污染时才用）
# ---------------------------------------------------------------------------
# 为什么要有它：自检一旦往**用户真实**的数据目录里写东西，用户是最后一个知道的
# （实测踩过：跑一次 --raw-dir 就往 backups/xmp/ 里塞测试产物，还重写过 models.yaml）。
# 根因是"逐个用例自己隔离 %APPDATA%"，漏一个也看不出来 —— 现在改成入口统一隔离
# （见 main()），再用这里的快照差分把"漏了"变成一条立刻变红的断言。
#
# 只排除 logs/：那块是程序自己在写的轮转日志（用户开着程序时它一直在变），
# 拿它做差分必然假失败。其余任何新增/改动都算"自检弄脏了用户数据"。
_SNAPSHOT_EXCLUDE_TOP = ("logs",)


def _datadir_snapshot() -> tuple[Path, dict[str, tuple[int, int]]]:
    """对**当前**数据目录做快照：{相对路径: (大小, mtime_ns)}。

    返回根目录本身：差分时必须回到**同一个**根去比 —— 自检中途会把
    %APPDATA% 指向临时目录，这里若再调一次 data_root() 就会比错对象。
    """
    from acb.paths import data_root

    root = data_root()
    files: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return root, files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _SNAPSHOT_EXCLUDE_TOP:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files[str(rel)] = (stat.st_size, stat.st_mtime_ns)
    return root, files


def _datadir_diff(baseline: tuple[Path, dict[str, tuple[int, int]]]) -> tuple[list[str], list[str]]:
    """与基线比对，返回 (新增, 改动过) 两组相对路径。"""
    root, before = baseline
    now: dict[str, tuple[int, int]] = {}
    if root.is_dir():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel.parts and rel.parts[0] in _SNAPSHOT_EXCLUDE_TOP:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            now[str(rel)] = (stat.st_size, stat.st_mtime_ns)
    added = sorted(set(now) - set(before))
    changed = sorted(key for key in set(now) & set(before) if now[key] != before[key])
    return added, changed


def test_snapshot_exclusion_list_is_intentional() -> None:
    """快照的排除清单必须**只有** logs/ —— 防止它悄悄长成一张"豁免表"。

    外部评审提的形式风险成立：一旦"某次写脏了就顺手排除掉那个目录"成为习惯，
    三年后排除列表会比被测内容还长，差分就失去意义了。
    所以这里把清单钉住：想加第二项，必须**显式**改这个测试（并说明它为什么属于
    「程序自己在持续写」的运行时目录），而不是顺手豁免一个用例。
    """
    allowed = ("logs",)
    record(
        "数据目录快照只排除 logs/（排除清单不许悄悄扩张）",
        _SNAPSHOT_EXCLUDE_TOP == allowed,
        f"当前 = {_SNAPSHOT_EXCLUDE_TOP}；只允许 = {allowed}。"
        "新增排除项前先想清楚：它是不是「程序自己在持续写」的运行时目录？"
        "如果只是「某个用例写脏了」，正确的修法是隔离那个用例，不是豁免它。",
    )


def _real_datadir_requested() -> bool:
    """逃生舱：`ACB_TEST_REAL_DATADIR=1` 时**不**隔离 %APPDATA%。

    什么情况下需要它：某些用例的被测对象**就是**真实数据目录 ——
    例如"拿一份真实的旧 models.yaml 验证 CAPABILITY_FIXES 有没有正确触发"，
    隔离之后被测对象会被换成空目录，等于没测。
    自动化里永远不设（会污染用户数据）；只在人工、一次性排查时显式打开，
    main() 会打一条醒目提示。
    """
    import os

    value = os.environ.get("ACB_TEST_REAL_DATADIR", "").strip().lower()
    return value not in ("", "0", "false", "no")


# ---------------------------------------------------------------------------
# 1. 路径解析
# ---------------------------------------------------------------------------
def test_paths() -> None:
    from acb.paths import (
        data_root,
        ensure_runtime_dirs,
        exe_dir,
        is_portable,
        logs_dir,
        resource_root,
        thumbs_dir,
    )

    try:
        print(f"     exe 目录        : {exe_dir()}")
        print(f"     资源根目录      : {resource_root()}")
        print(f"     数据根目录      : {data_root()}")
        print(f"     便携模式        : {'是' if is_portable() else '否'}")
        ensure_runtime_dirs()
        print(f"     日志目录        : {logs_dir()}")
        print(f"     缩略图缓存目录  : {thumbs_dir()}")

        # 打包要求 #3：运行时数据绝不能落在 _internal 或临时解压目录里。
        data_str = str(data_root()).lower()
        bad = "_internal" in data_str or "_mei" in data_str or "\\temp\\" in data_str
        record("路径解析：运行时数据不在 _internal/临时目录", not bad, f"data_root={data_root()}")
    except Exception as exc:
        record("路径解析", False, str(exc))


# ---------------------------------------------------------------------------
# 2. 模型配置
# ---------------------------------------------------------------------------
def test_config() -> None:
    from acb.config import load_models_config

    try:
        config = load_models_config()
        spec = config.active_spec()
        print(f"     active   = {config.active}")
        print(f"     可用模型 = {', '.join(sorted(config.models))}")
        print(f"     当前模型 = {spec.display}")
        print(f"     base_url = {spec.full_url}")
        print(f"     视觉     = {spec.supports_vision}；json_schema = {spec.supports_json_schema}")
        record("模型配置加载与校验", True, f"active={config.active}")
    except Exception as exc:
        record("模型配置加载与校验", False, str(exc))


# ---------------------------------------------------------------------------
# 1b. 色彩管理（ICC 转换链）
# ---------------------------------------------------------------------------
def test_color_management() -> None:
    """色彩管理链路：源 profile 判定 → ICC 转换到 sRGB → 输出**必带** sRGB profile。

    这条件测的是一个被问过的问题："ICC 文件可有可无，不就等于没做色彩转换吗？"
    答案要靠实测说话，所以这里逐条断言：

      1. 转换**真的发生**：嵌入一个**非 sRGB** 的 RGB profile，像素值必须改变
         （如果只是"原样复制"，这条会失败）；
      2. 输出**一定带 sRGB profile**（硬约束 #2 禁止丢弃/覆盖 profile）；
      3. 声明 Adobe RGB(1998) 的预览**真的能被识别出来**
         —— 以前这里是死代码（读错了 EXIF 层级），见 icc.read_interop_index 的实测记录；
      4. 识别出来了却拿不到 profile 时，**宁可报错改走 rawpy，也不按 sRGB 凑合**；
      5. 系统色彩目录里的 profile 能被搜索到（不再只看程序自带的 assets/icc），
         并且**官方命名的那份压过程序自带的等效文件**（文件名优先级 > 目录优先级）；
      6. 程序自带自己生成的等效 profile：断网、没装过 Photoshop 也能正确转换；
      7. 等效 profile 的色度必须用**独立的矩阵运算复算**验证（不是"看起来像"）；
      8. 官方 profile 存在时它自己生成的等效文件不得覆盖、不得抢占（见 5）。
    """
    import io
    import struct
    import tempfile
    from pathlib import Path as _Path
    from unittest import mock

    from PIL import Image, ImageCms

    from acb.errors import IccProfileMissingError
    from acb.raw import icc as I

    def make_image(w=8, h=4):
        img = Image.new("RGB", (w, h))
        for x in range(w):
            for y in range(h):
                img.putpixel((x, y), (40 + x * 25, 90 + y * 10, 150))
        return img

    def jpeg_with_profile(profile_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        make_image().save(buf, format="JPEG", quality=95, icc_profile=profile_bytes)
        return buf.getvalue()

    # ---- 标准 EXIF 布局的构造器：IFD0 → Exif IFD → Interop IFD(InteropIndex=R03) ----
    def tiff_with_interop_r03() -> bytes:
        ifd0_count = 2
        off_ifd0 = 8
        off_exif = off_ifd0 + 2 + ifd0_count * 12 + 4          # Exif IFD 紧跟 IFD0
        off_interop = off_exif + 2 + 12 + 4                    # Interop IFD 紧跟 Exif IFD
        header = b"II" + struct.pack("<HI", 42, off_ifd0)
        ifd0 = struct.pack("<H", ifd0_count)
        ifd0 += struct.pack("<HHI", 0xA001, 3, 1) + struct.pack("<HH", 65535, 0)   # ColorSpace=Uncalibrated
        ifd0 += struct.pack("<HHI", 0x8769, 4, 1) + struct.pack("<I", off_exif)    # Exif IFD 指针
        ifd0 += struct.pack("<I", 0)
        exif_ifd = struct.pack("<H", 1)
        exif_ifd += struct.pack("<HHI", 0xA005, 4, 1) + struct.pack("<I", off_interop)
        exif_ifd += struct.pack("<I", 0)
        interop = struct.pack("<H", 1)
        interop += struct.pack("<HHI", 0x0001, 2, 4) + b"R03\x00"
        interop += struct.pack("<I", 0)
        return header + ifd0 + exif_ifd + interop

    def jpeg_with_exif(tiff: bytes) -> bytes:
        buf = io.BytesIO()
        make_image().save(buf, format="JPEG", quality=95)
        raw = buf.getvalue()
        payload = b"Exif\x00\x00" + tiff
        app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        return raw[:2] + app1 + raw[2:]

    try:
        srgb_bytes = I.srgb_profile_bytes()
        srgb_ok = len(srgb_bytes) > 100 and ImageCms.ImageCmsProfile(io.BytesIO(srgb_bytes)) is not None

        # ---- 1+2：非 sRGB 源 → 像素必须变；输出必须带 sRGB profile ----
        foreign = None
        foreign_name = ""
        for directory in I.system_profile_dirs():
            for candidate in sorted(directory.glob("*.icc")) + sorted(directory.glob("*.icm")):
                if candidate.name.lower().startswith("srgb"):
                    continue
                try:
                    ImageCms.ImageCmsProfile(str(candidate))
                except Exception:
                    continue
                foreign = candidate.read_bytes()
                foreign_name = candidate.name
                break
            if foreign:
                break

        source_bytes = foreign if foreign else srgb_bytes
        source_img = Image.open(io.BytesIO(jpeg_with_profile(source_bytes)))
        source_img.load()
        thumbnail, reason = I.build_srgb_thumbnail(jpeg_with_profile(source_bytes), long_edge=8)
        with Image.open(io.BytesIO(thumbnail)) as out:
            out_profile = out.info.get("icc_profile")
            out_pixels = list(out.convert("RGB").getdata())
        if foreign:
            changed = out_pixels != list(source_img.convert("RGB").getdata())
            changed_note = f"像素改变={changed}（源 profile：{foreign_name}）"
        else:
            # 系统里没有第三方非 sRGB profile 时，用**程序自带的那份**当源：
            # `assets/icc/AdobeRGB1998-compatible.icc` 是按 Adobe RGB 原色自算的，
            # 一定不等于 sRGB —— 于是"像素真的被转换了"这条断言在任何机器上都跑得到。
            # （以前这里直接写 `changed = True`，等于空转：断言没验过却显示通过。）
            changed = False
            changed_note = "系统里没有非 sRGB 的 RGB profile，且自带等效 profile 不可用"
            try:
                I.ensure_icc_assets()
                bundled_source = I.find_icc_file(I.ADOBE_RGB_COMPAT_FILENAME)
            except Exception as exc:       # noqa: BLE001 - 兜底失败不该中断整条用例
                bundled_source = None
                changed_note = f"自带等效 profile 取不到：{exc}"
            if bundled_source is not None:
                bundled_bytes = bundled_source.read_bytes()
                with Image.open(io.BytesIO(jpeg_with_profile(bundled_bytes))) as src2:
                    src2.load()
                    src2_pixels = list(src2.convert("RGB").getdata())
                thumb2, reason2 = I.build_srgb_thumbnail(
                    jpeg_with_profile(bundled_bytes), long_edge=8
                )
                with Image.open(io.BytesIO(thumb2)) as out2:
                    changed = list(out2.convert("RGB").getdata()) != src2_pixels
                changed_note = (
                    f"像素改变={changed}（源 profile：自带的 {bundled_source.name}，"
                    f"reason={reason2}）"
                )
        profile_ok = bool(out_profile) and out_profile == srgb_bytes
        embedded_honored = "嵌入 ICC" in reason

        # ---- 3：标准布局的 R03 必须被认出来（以前是死代码）----
        r03_jpeg = jpeg_with_exif(tiff_with_interop_r03())
        with Image.open(io.BytesIO(r03_jpeg)) as probe:
            detected = I.read_interop_index(probe.getexif())
        with mock.patch.object(I, "load_icc_file_bytes", return_value=srgb_bytes):
            _, adobe_reason = I._source_profile(Image.open(io.BytesIO(r03_jpeg)))
        r03_detected = detected == "R03" and "R03" in adobe_reason

        # ---- 4a：程序自带自己生成的等效 profile → 真的能转换（不再需要用户拷文件）----
        bundled_detail = ""
        try:
            I.ensure_icc_assets()
            _, bundled_reason = I.build_srgb_thumbnail(r03_jpeg, long_edge=8)
            bundled_ok = "R03" in bundled_reason
        except Exception as exc:
            bundled_ok = False
            bundled_detail = f"{type(exc).__name__}: {exc}"

        # ---- 4b：连兜底都没有时**拒绝凑合**（报错并说明会改走 rawpy）----
        with mock.patch.object(I, "load_icc_file_bytes", return_value=None):
            try:
                I.build_srgb_thumbnail(r03_jpeg, long_edge=8)
                refused = False
            except IccProfileMissingError as exc:
                refused = "rawpy" in str(exc)          # 错误信息要告诉用户会改走 rawpy
            except Exception:
                refused = False

        # ---- 7：自己生成的等效 profile 必须**色度正确**（不是"看起来像"）----
        # 用独立实现的矩阵运算复算同一条转换：Adobe RGB(gamma 563/256) → XYZ(D65)
        # → Bradford 适应到 D50 → 反适应回 D65 → sRGB → sRGB 曲线编码。
        generated = I.build_adobe_rgb_compatible_profile()
        generated_parsed = True
        try:
            prof = ImageCms.getOpenProfile(io.BytesIO(generated))
            probe = Image.new("RGB", (4, 1))
            for i, c in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255), (128, 128, 128)]):
                probe.putpixel((i, 0), c)
            lcms_out = ImageCms.profileToProfile(
                probe, prof, ImageCms.createProfile("sRGB"),
                renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB",
            )
        except Exception as exc:
            generated_parsed = False
            lcms_out = None
            colorimetry_diff = 99

        if lcms_out is not None:
            import numpy as _np

            def _xyz_of(xy, Y=1.0):
                x, y = xy
                return _np.array([x / y * Y, Y, (1 - x - y) / y * Y])

            def _bradford(a, b):
                m = _np.array([[0.8951, 0.2664, -0.1614],
                               [-0.7502, 1.7135, 0.0367],
                               [0.0389, -0.0685, 1.0296]])
                return _np.linalg.inv(m) @ _np.diag((m @ b) / (m @ a)) @ m

            prim = {"r": (0.6400, 0.3300), "g": (0.2100, 0.7100), "b": (0.1500, 0.0600)}
            white = (0.3127, 0.3290)
            m_rgb = _np.array([_xyz_of(prim[c]) for c in ("r", "g", "b")]).T
            m_rgb = m_rgb * _np.linalg.solve(m_rgb, _xyz_of(white))
            adapt = _bradford(_xyz_of(white), _np.array([0.9642, 1.0, 0.8249]))
            m_d50 = adapt @ m_rgb
            m_srgb = _np.array([[0.4124564, 0.3575761, 0.1804375],
                                [0.2126729, 0.7151522, 0.0721750],
                                [0.0193339, 0.1191920, 0.9503041]])
            m_unadapt = _np.linalg.inv(adapt)

            def _encode(linear):
                a = _np.where(linear <= 0.0031308, linear * 12.92,
                              1.055 * _np.power(_np.clip(linear, 0, None), 1 / 2.4) - 0.055)
                return _np.clip(_np.round(a * 255), 0, 255)

            reference = []
            for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (128, 128, 128)]:
                lin_adobe = (_np.array(c) / 255.0) ** (563 / 256)
                xyz_d65 = m_unadapt @ (m_d50 @ lin_adobe)
                reference.append(tuple(int(v) for v in _encode(_np.linalg.solve(m_srgb, xyz_d65))))
            colorimetry_diff = max(
                abs(int(a) - int(b))
                for p, q in zip((lcms_out.getpixel((i, 0)) for i in range(4)), reference)
                for a, b in zip(p, q)
            )
        colorimetry_ok = generated_parsed and colorimetry_diff <= 2

        # ---- 8：资源报告不许为"设计上不需要"的文件报警 ----
        # （Display P3 用不到，写成"缺失"会让启动日志每次刷一条假警告，
        #   真正的警告被淹没 —— 这比不报警更糟。）
        report = I.describe_optional_profiles()
        report_ok = (
            I.ADOBE_RGB_COMPAT_FILENAME in report
            and "不需要" in report
            and "缺失" not in report          # 自带文件已就位的前提下不该有缺失
        )

        # ---- 5：系统目录里的 profile 能被搜索到，**且官方名字压过自带的等效文件** ----
        # （模拟系统目录里放一份真正的 AdobeRGB1998.icc，程序必须优先用它，
        #   而不是自己生成的那份 compatible 文件——目录优先级压过文件名优先级
        #   的话这条就会失败，本用例正是这么抓到那个坑的）
        with tempfile.TemporaryDirectory() as tmp:
            fake = _Path(tmp) / "AdobeRGB1998.icc"
            fake.write_bytes(srgb_bytes)
            old_dirs = I.SYSTEM_PROFILE_DIRS_WINDOWS
            try:
                I.SYSTEM_PROFILE_DIRS_WINDOWS = ("%ACB_TEST_PROFILE_DIR%",)
                with mock.patch.dict("os.environ", {"ACB_TEST_PROFILE_DIR": tmp}):
                    I.find_icc_file.cache_clear()
                    I.srgb_profile_bytes.cache_clear()
                    found = I.find_icc_file("AdobeRGB1998.icc")
                    search_ok = found is not None and found == fake
            finally:
                I.SYSTEM_PROFILE_DIRS_WINDOWS = old_dirs
                I.find_icc_file.cache_clear()
                I.srgb_profile_bytes.cache_clear()
        # 顺带报告本机真实找到了什么（信息，不作为通过条件）
        # 必须区分"系统里真的有 Adobe 官方那份"与"只是程序自带的等效文件在"——
        # 两者都能让 load_icc_file_bytes 返回字节，但含义完全不同。
        resolved_adobe = I.find_icc_file(I.ADOBE_RGB_ICC_FILENAME)
        real_adobe = bool(
            resolved_adobe is not None
            and resolved_adobe.name != I.ADOBE_RGB_COMPAT_FILENAME
        )
        adobe_source = (
            "系统里的 Adobe 官方文件" if real_adobe
            else ("程序自带的等效文件" if resolved_adobe else "两者都没有")
        )

        # ---- 6：转换本身失败时不许弄挂整批 ----
        with mock.patch.object(ImageCms, "profileToProfile", side_effect=RuntimeError("模拟转换失败")):
            degraded, degraded_reason = I.to_srgb(make_image())
        graceful = degraded.size == (8, 4) and "未做变换" in degraded_reason
    except Exception as exc:
        record("色彩管理：ICC 转换链（转换发生 / 输出带 sRGB / Adobe RGB 能识别）", False,
               f"{type(exc).__name__}: {exc}")
        return

    record(
        "色彩管理：ICC 转换链（转换发生 / 输出 sRGB / 等效 profile 色度正确 / 官方优先 / 资源报告）",
        bool(srgb_ok and changed and profile_ok and embedded_honored
             and r03_detected and bundled_ok and refused and search_ok and graceful
             and colorimetry_ok and report_ok),
        f"sRGB profile 可用={srgb_ok} 输出带 sRGB profile={profile_ok} "
        f"源 profile 被采用={embedded_honored} {changed_note} | "
        f"R03 能识别={r03_detected} 自带等效 profile 可用={bundled_ok} "
        f"（Adobe RGB profile 来源：{adobe_source}）{bundled_detail} | "
        f"自己生成的 profile 色度复算最大偏差={colorimetry_diff}"
        f"{'' if generated_parsed else '（解析失败）'} | "
        f"连兜底都没有时拒绝凑合={refused} 官方 profile 优先级={search_ok} "
        f"资源报告不误报={report_ok} | "
        f"转换失败时优雅降级={graceful}",
    )


def test_output_format_tiers() -> None:
    """输出格式三档 + 服务端拒绝时的一次性自动降级（在隔离的数据目录里跑）。

    ⚠ 必须隔离 %APPDATA%：这一轮新增的“端点能力记忆”就写在数据目录的 state/ 下，
    而它会让程序**故意不发** response_format —— 如果不隔离，这个用例会读到这台机器上
    真实的历史记录（我自己就被手工实验留下的记录绊过一次：断言“降级重发”失败）。
    凡是会读写运行时状态的用例，都要有自己的临时数据目录。
    """
    from unittest import mock

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"APPDATA": tmp}):
        _test_output_format_tiers_body()


def _test_output_format_tiers_body() -> None:
    """输出格式三档 + 服务端拒绝时的一次性自动降级。

    背景（用户实际撞到的 400）：配置里写 supports_json_schema: true，
    而 DeepSeek 官方只有 json_object，服务端回
    `This response_format type is unavailable now` —— 训练模式直接失败。
    官方依据：api-docs.deepseek.com 的《JSON Output》指南写的就是
    response_format={'type': 'json_object'}。

    这里逐条验证：
      1. 支持 json_schema → 发 json_schema；
      2. 只支持 json_object → 发 json_object（不再当成"什么都不支持"）；
      3. 两个都不支持 → 不带 response_format，且提示词里必须带小写 json 字样
         （DeepSeek 的 JSON Output 明确要求 prompt 里含 json）；
      4. 服务端用 400 拒 response_format 时：自动去掉它重发一次并**只抛一次警告**，
         后续请求也不再带它；
      5. extra_body 能合进请求体，但不允许覆盖程序自己管理的键。
    """
    from acb.ai.adapter import ModelAdapter
    from acb.ai.client import ApiClient, PreparedRequest, RequestBudget, build_chat_request
    from acb.config import ModelSpec

    base = dict(
        label="L", base_url="https://api.example.com/v1", model="m1",
        key_env="K", supports_vision=True, max_context=4096,
    )
    schema = {"type": "object", "properties": {"items": {"type": "array"}}}

    def adapter(**patch) -> ModelAdapter:
        fields = {**base, "supports_json_schema": False}
        fields.update(patch)          # 先合并再展开，避免重复关键字参数
        return ModelAdapter(ModelSpec(**fields), "sk-test", RequestBudget(limit=50))

    try:
        rf_schema = adapter(supports_json_schema=True)._response_format(schema, "n")
        rf_object = adapter(supports_json_object=True)._response_format(schema, "n")
        rf_none = adapter()._response_format(schema, "n")
        tiers_ok = (
            rf_schema is not None and rf_schema["type"] == "json_schema"
            and rf_object == {"type": "json_object"}
            and rf_none is None
        )
        # 降级通道的提示词必须含小写 json（DeepSeek 的要求）
        messages = adapter()._build_messages("SYS", "USER", [], schema)
        prompt_ok = "json" in messages[0]["content"]

        # --- extra_body：合进去，但保护键被拦 ---
        spec_extra = ModelSpec(
            **{**base, "supports_json_schema": True}, extra_body={"reasoning_effort": "none"}
        )
        req = build_chat_request(spec_extra, "k", [], temperature=0.6)
        extra_ok = req.payload.get("reasoning_effort") == "none" and req.payload["model"] == "m1"
        protected_blocked = ""
        spec_bad = ModelSpec(
            **{**base, "supports_json_schema": True}, extra_body={"messages": []}
        )
        try:
            build_chat_request(spec_bad, "k", [], temperature=0.6)
            protected_blocked = "未拦住"
        except Exception as exc:
            if "extra_body" in str(exc) and "messages" in str(exc):
                protected_blocked = ""

        # --- 400 拒绝 response_format → 自动降级重发一次 ---
        sent: list[dict] = []

        class _Resp:
            def __init__(self, status: int, text: str, payload=None):
                self.status_code = status
                self.text = text
                self._payload = payload or {}

            def json(self):
                return self._payload

        class _Session:
            def __init__(self):
                self.calls = 0

            def post(self, url, json=None, headers=None, timeout=None):
                sent.append(json or {})
                self.calls += 1
                if "response_format" in (json or {}):
                    return _Resp(400, '{"error":{"message":"This response_format type '
                                       'is unavailable now"}}')
                return _Resp(200, "ok", {
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "model": "m1",
                })

        client = ApiClient(spec_extra, "sk-test", RequestBudget(limit=50))
        client._session = lambda: _Session()   # type: ignore[method-assign]
        data = client.chat([{"role": "user", "content": "hi"}], temperature=0.6,
                           response_format={"type": "json_object"})
        downgrade_ok = (
            len(sent) == 2                       # 先带它（被拒）→ 再不带它（成功）
            and "response_format" in sent[0]
            and "response_format" not in sent[1]
            and client.response_format_rejected is True
            and data["model"] == "m1"
        )
        # 后续请求不再带它（不重复碰壁）
        client.chat([{"role": "user", "content": "hi"}], temperature=0.6,
                    response_format={"type": "json_object"})
        no_repeat = len(sent) == 3 and "response_format" not in sent[2]
    except Exception as exc:
        record("输出格式三档 + 服务端拒绝时自动降级", False, f"{type(exc).__name__}: {exc}")
        return

    record(
        "输出格式三档（json_schema / json_object / 纯提示词）+ 400 自动降级",
        bool(tiers_ok and prompt_ok and extra_ok and protected_blocked == ""
             and downgrade_ok and no_repeat),
        f"三档选择={tiers_ok} 降级提示词含 json={prompt_ok} extra_body 合入={extra_ok} "
        f"保护键={protected_blocked or '拦住'} 400 后降级重发={downgrade_ok} "
        f"后续不再带={no_repeat}",
    )


def test_model_capability_overrides() -> None:
    """能力必须能**按模型**区分，并且自动选中的模型要能看图。

    官方依据（DeepSeek「模型 & 价格」页面，2026-09 核对）：
      deepseek-flash（V4.1-Flash）：图像理解 **支持**；
      deepseek-v4-pro（V4-Pro-0813）：图像理解 **不支持**。
    服务商级的一个 supports_vision 无法表达这种差异：
      · 写成 true  → 对 V4 Pro 照发图片，得到一个含糊的 400；
      · 写成 false → 对 Flash 白白丢掉视觉能力（本工具靠缩略图判断，代价很大）。

    同时验密钥规范化：粘贴带入的引号/空白/Bearer 前缀要被清掉，
    而全角字符/中间空格这类"一定是拷错了"的形状要拦下来。
    """
    from acb.config import ModelsConfig, ProviderSpec
    from acb.keyring_store import key_problem, normalize_api_key

    deepseek = ProviderSpec(
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
        supports_vision=True,
        supports_json_schema=False,
        supports_json_object=True,
        max_context=1000000,
        model_overrides={"deepseek-v4-pro": {"supports_vision": False}},
    )
    pro = deepseek.spec_for("deepseek", "deepseek-v4-pro")
    flash = deepseek.spec_for("deepseek", "deepseek-flash")
    override_ok = pro.supports_vision is False and flash.supports_vision is True
    inherit_ok = (
        pro.supports_json_object is True
        and pro.max_context == 1000000
        and pro.supports_json_schema is False
    )

    # 自动选中：即使不支持视觉的那个排在前面，也要选能看图的
    cfg = ModelsConfig(providers={"deepseek": deepseek})
    cfg.models = {"deepseek::deepseek-v4-pro": pro, "deepseek::deepseek-flash": flash}
    picked = cfg.first_model_of_provider("deepseek")
    prefer_ok = picked is not None and picked[0] == "deepseek::deepseek-flash"
    # 一家都没视觉时必须退回到顺序第一个（不能因此选不出来）
    blind = ProviderSpec(
        label="X", base_url="https://x/v1", key_env="X_KEY", supports_vision=False,
        supports_json_schema=False, max_context=1000,
    )
    cfg_blind = ModelsConfig(providers={"x": blind})
    cfg_blind.models = {"x::m1": blind.spec_for("x", "m1"), "x::m2": blind.spec_for("x", "m2")}
    blind_first = cfg_blind.first_model_of_provider("x")
    blind_ok = blind_first is not None and blind_first[0] == "x::m1"

    # 密钥规范化
    clean_cases = [
        ("  sk-abc123  ", "sk-abc123"),
        ('"sk-abc123"', "sk-abc123"),
        ("'sk-abc123'", "sk-abc123"),
        ("`sk-abc123`", "sk-abc123"),
        ("sk-abc123\n", "sk-abc123"),
        ("Bearer sk-abc123", "sk-abc123"),
        ("\u300csk-abc123\u300d", "sk-abc123"),
        ("\u201csk-abc123\u201d", "sk-abc123"),
    ]
    clean_bad = [
        f"{raw!r} → {normalize_api_key(raw)[0]!r}"
        for raw, want in clean_cases
        if normalize_api_key(raw)[0] != want
    ]
    clean_ok = not clean_bad and all(normalize_api_key(raw)[1] == [] for raw, _ in [("sk-abc123", "")])
    # 正常密钥不该被改动
    untouched = normalize_api_key("sk-abc123") == ("sk-abc123", [])
    # 全角/中间空格 / 空值必须报错
    problem_cases = {"sk-a\uff42\uff43": True, "sk-abc 123": True, "sk-ab\t c": True,
                     "": True, "sk-abc123": False}
    problem_bad = [
        f"{raw!r}: 期望{'报错' if want else '通过'}，实得 {key_problem(raw)!r}"
        for raw, want in problem_cases.items()
        if (key_problem(raw) is not None) != want
    ]

    record(
        "模型能力按模型区分 + 自动选中支持视觉的模型 + 密钥粘贴杂质清理",
        bool(override_ok and inherit_ok and prefer_ok and blind_ok and clean_ok
             and untouched and not problem_bad),
        f"逐模型覆盖={override_ok} 其余字段仍继承={inherit_ok} 优先选视觉模型={prefer_ok} "
        f"无视觉时退回第一个={blind_ok} 清理一致性={clean_ok} 正常值不动={untouched} "
        f"非法形状拦截={problem_bad or '全部符合'}",
    )


def test_connection_fact_refresh() -> None:
    """老连接的"从预设继承的能力字段"要跟上预设的修正。

    【为什么单独立这一项 —— 这是用户实际踩到的那条链】
    connections.yaml 是**用户数据**（只在新建/删除连接时改写），
    而连接在创建时把预设的能力字段抄了一份。于是：
      程序把 DeepSeek 的 supports_json_schema 改成 false（官方只支持 json_object）
      → models.yaml 被自动修正了
      → 但已有的 connections.yaml 里还是 true
      → 用户每次请求仍然先吃一个 400（靠客户端降级重发才能成）。
    DeepSeek 的 V4 Pro 不支持图像理解也一样：不跟着改，
    程序就会对 V4 Pro 照发图片。

    规则与 models.yaml 一致：只动"用户没改过"（值恰好等于我们当年写的那个）的字段，
    name / base_url / key_env 一律不碰；自建连接（没有 preset）完全不动。
    """
    import yaml

    from unittest import mock

    from acb.config import connections_path, load_models_config, user_config_path

    old_provider = {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "supports_vision": True,
        "supports_json_schema": True,
        "max_context": 128000,
        "max_output_tokens": 4096,
        "default_models": ["deepseek-v4-pro", "deepseek-flash"],
    }
    old_connection = {
        "name": "DeepSeek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "preset": "deepseek",
        "supports_vision": True,
        "supports_json_schema": True,
        "max_context": 128000,
        "max_output_tokens": 4096,
        "default_models": ["deepseek-v4-pro", "deepseek-flash"],
    }
    my_relay = {
        "name": "我的中转",
        "label": "我的中转",
        "base_url": "https://relay.example/v1",
        "key_env": "conn:我的中转",
        "preset": "",
        "supports_vision": True,
        "supports_json_schema": False,
        "max_context": 64000,
        "default_models": ["some-model"],
    }

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict("os.environ", {"APPDATA": tmp}):
            cfg_path = user_config_path()
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                yaml.safe_dump({"providers": {"deepseek": old_provider}}, allow_unicode=True),
                encoding="utf-8",
            )
            conn_path = connections_path()
            header = conn_path.read_text(encoding="utf-8") if conn_path.is_file() else ""
            conn_path.write_text(header, encoding="utf-8")
            conn_path.write_text(
                "connections:\n"
                + yaml.safe_dump(
                    {"connections": {"deepseek": old_connection, "我的中转": my_relay}},
                    allow_unicode=True, sort_keys=False,
                ),
                encoding="utf-8",
            )

            cfg = load_models_config()
            after = yaml.safe_load(conn_path.read_text(encoding="utf-8"))["connections"]
            d = after["deepseek"]
            refreshed = (
                d["supports_json_schema"] is False
                and d.get("supports_json_object") is True
                and d["max_context"] == 1000000
                and d["max_output_tokens"] == 16384
                and d["default_models"][0] == "deepseek-flash"
                and (d.get("model_overrides") or {}).get("deepseek-v4-pro", {}).get("supports_vision") is False
            )
            # 身份字段一律不碰
            identity_ok = (
                d["name"] == "DeepSeek"
                and d["key_env"] == "DEEPSEEK_API_KEY"
                and d["base_url"] == "https://api.deepseek.com/v1"
                and d["preset"] == "deepseek"
            )
            relay = after["我的中转"]
            relay_ok = (
                relay["max_context"] == 64000
                and relay["supports_json_schema"] is False
                # 自建连接没有预设可继承，所以只会被写成空字典（不能有真的覆盖项）
                and not relay.get("model_overrides")
                and relay["name"] == "我的中转"
                and relay["key_env"] == "conn:我的中转"
            )
            # 实例化出来的模型也要真的带上修正（V4 Pro 不看图、Flash 能看图）
            v4 = cfg.models["deepseek::deepseek-v4-pro"]
            flash = cfg.models["deepseek::deepseek-flash"]
            spec_ok = (
                v4.supports_vision is False
                and flash.supports_vision is True
                and flash.supports_json_object is True
                and flash.max_output_tokens == 16384
            )
            # 幂等：再加载一次不该重写文件
            before = conn_path.read_text(encoding="utf-8")
            load_models_config()
            idem = conn_path.read_text(encoding="utf-8") == before

    record(
        "连接升级：能力字段跟上预设修正（身份/base_url 不动，自建连接不动）",
        bool(refreshed and identity_ok and relay_ok and spec_ok and idem),
        f"能力已修正={refreshed} 身份字段未动={identity_ok} 自建连接未动={relay_ok} "
        f"实例化模型已带上={spec_ok} 幂等={idem}",
    )


def test_output_escalation_and_endpoint_memory() -> None:
    """三个真实踩过的分支，全用假服务端固化成回归用例：

    1. **空 content**（官方文档：JSON Output 有概率返回空 content，
       另一个原因是思考模式的推理 token 把 max_tokens 吃完了）
       → 必须**抬高出上限重试一次**，而不是重发同样的请求。
    2. **finish_reason=length**（被截断）→ 同上，且错误信息要给出动作。
    3. **端点拒绍 response_format** → 自愈结果要**落盘**，
       否则下次启动又要白花一次请求（还带着 base64 图片）。
    4. 400 原文提到图片时，要给出“换能看图的模型 / 标 supports_vision: false”的指引。
    """
    from pathlib import Path as _Path
    from unittest import mock

    from acb.ai.adapter import ModelAdapter, PreviewItem
    from acb.ai.client import (
        RequestBudget,
        endpoint_caps_path,
        load_rejected_formats,
    )
    from acb.config import ModelSpec

    base = "https://relay.example/v1"

    class _Resp:
        def __init__(self, status: int, text: str, payload: dict | None = None) -> None:
            self.status_code, self.text = status, text
            self._payload = payload or {}

        def json(self) -> dict:
            return self._payload

    def spec_for(**patch) -> ModelSpec:
        fields = dict(
            label="L", base_url=base, model="m1", key_env="K",
            supports_vision=True, supports_json_schema=False, supports_json_object=True,
            max_context=4096, max_output_tokens=4096,
        )
        fields.update(patch)
        return ModelSpec(**fields)

    def run(responses, **spec_patch):
        """跑一次输出模式的单图分析，返回 (已发请求体列表, 文件级错误)。"""
        sent: list[dict] = []

        class _Session:
            def post(self, url, json=None, headers=None, timeout=None):
                sent.append(dict(json or {}))
                idx = min(len(sent) - 1, len(responses) - 1)
                return responses[idx]

        adapter = ModelAdapter(spec_for(**spec_patch), "sk-test", RequestBudget(limit=20))
        adapter.client._session = lambda: _Session()   # type: ignore[method-assign]
        item = PreviewItem(
            file_id="f1", path=_Path("a.cr3"), filename="a.cr3",
            preview_jpeg=b"x", preview_source="preview",
        )
        results = adapter.analyze_group([item], style_block="", user_prompt="")
        return sent, (results[0].error if results else ""), adapter.client

    empty = _Resp(200, "ok", {"choices": [{"message": {"content": ""},
                                             "finish_reason": "stop"}], "model": "m1"})
    truncated = _Resp(200, "ok", {"choices": [{"message": {"content": '{"items":'},
                                               "finish_reason": "length"}], "model": "m1"})

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.dict("os.environ", {"APPDATA": tmp}):
            try:
                # 1) 空 content → 第二次带上抬高的输出上限
                sent, error, _ = run([empty])
                empty_ok = (
                    [p.get("max_tokens") for p in sent] == [4096, 8192]
                    and "空" in error and "8192" in error and "max_output_tokens" in error
                )
                # 2) 截断 → 同样抬升，且错误里有可执行动作
                sent, error, _ = run([truncated])
                trunc_ok = (
                    [p.get("max_tokens") for p in sent] == [4096, 8192]
                    and "截断" in error and "max_output_tokens" in error
                )
                # 3) 端点拒 json_schema → 落盘；下一次运行一次都不带
                rejected = _Resp(400, '{"error":{"message":"This response_format type '
                                       'is unavailable now"}}')
                good = _Resp(200, "ok", {"choices": [{"message": {"content": "{}"},
                                                       "finish_reason": "stop"}], "model": "m1"})
                run([rejected, good], supports_json_schema=True, supports_json_object=False)
                remembered = load_rejected_formats().get(base) == ["json_schema"]
                sent2, _, _ = run([good], supports_json_schema=True, supports_json_object=False)
                memory_ok = remembered and not any("response_format" in p for p in sent2)
                # 4) 图片被拒 → 指引里要提到怎么改配置
                image_rejected = _Resp(400, '{"error":{"message":"image input is not supported '
                                               'by this model"}}')
                _, error, _ = run([image_rejected])
                hint_ok = "图片" in error and "supports_vision" in error
            except Exception as exc:  # noqa: BLE001 — 自检要把异常变成可读结论
                record("输出上限升档 / 端点能力记忆 / 图片被拒指引", False,
                       f"{type(exc).__name__}: {exc}")
                return

    record(
        "空 content 与截断：抬高出上限重试；端点拒 response_format：落盘并下次生效",
        bool(empty_ok and trunc_ok and memory_ok and hint_ok),
        f"空 content 升档={empty_ok} 截断升档={trunc_ok} 端点记忆落盘并在下次生效={memory_ok} "
        f"图片被拒给出配置指引={hint_ok}（记忆文件：{endpoint_caps_path().name}）",
    )


def test_config_provider_upgrade() -> None:
    """老用户配置要能补上内置目录新增的服务商，且不冲掉用户自己的改动。

    真实事故：内置服务商目录从 2 家扩到 12 家后，用户界面的「服务商」下拉里
    仍然只有 OpenAI + DeepSeek。原因是 seed_user_config() 只在文件缺失时复制、
    _migrate_config_format() 只认"完全没有 providers 键"的旧格式，
    于是"程序升级新增服务商"这件事永远到不了已有安装。
    """
    import yaml

    from acb.config import _upgrade_config_providers

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "models.yaml"
        # 模拟老用户文件：只有最初的两家，且 deepseek 被用户改过 base_url。
        path.write_text(
            "providers:\n"
            '  deepseek:\n'
            '    label: "DeepSeek"\n'
            '    base_url: "https://my-proxy.example.com/v1"\n'
            '    key_env: "DEEPSEEK_API_KEY"\n'
            "    supports_vision: true\n"
            "    supports_json_schema: true\n"
            "    max_context: 128000\n"
            '  openai:\n'
            '    label: "OpenAI"\n'
            '    base_url: "https://api.openai.com/v1"\n'
            '    key_env: "OPENAI_API_KEY"\n'
            "    supports_vision: true\n"
            "    supports_json_schema: true\n"
            "    max_context: 128000\n",
            encoding="utf-8",
        )

        added = _upgrade_config_providers(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        providers = data.get("providers") or {}

        # 1) 新增的服务商进来了（qwen / glm / gemini / kimi …）
        ok_added = "qwen" in added and "glm" in added and "gemini" in added
        # 2) 用户改过的 base_url 没被种子的默认值覆盖
        ok_keep = providers.get("deepseek", {}).get("base_url") == "https://my-proxy.example.com/v1"
        # 3) 原有的两家仍在，顺序上 deepseek 在最前
        order_ok = list(providers)[:2] == ["deepseek", "openai"]
        # 4) 幂等：再跑一次不该有任何变化（否则每次启动都重写文件）
        again = _upgrade_config_providers(path)
        ok_idem = again == [] and yaml.safe_load(path.read_text(encoding="utf-8")) == data

        # 5) 能力事实修正：老文件里的 supports_json_schema=true（我们当年写错的）
        #    要改成 false；而**用户改过的值**绝不能被覆盖。
        #    真实后果：不改的话每次请求都先吃一个 400，靠客户端降级重发 ——
        #    请求数翻倍，会把后面的文件从预算里挤出去。
        d = providers.get("deepseek", {})
        fix_ok = (
            d.get("supports_json_schema") is False
            and d.get("supports_json_object") is True
            and d.get("default_models", [None])[0] == "deepseek-flash"
        )
        #    用户自己写过的值（proxy 场景常把 max_context 改小）要保留
        data["providers"]["deepseek"]["max_context"] = 64000
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _upgrade_config_providers(path)
        kept = yaml.safe_load(path.read_text(encoding="utf-8"))["providers"]["deepseek"]
        user_value_kept = kept.get("max_context") == 64000

        record(
            "模型配置升级：老用户补上新服务商且保留用户改动",
            bool(ok_added and ok_keep and order_ok and ok_idem and fix_ok and user_value_kept),
            f"新增={','.join(added)}；用户 base_url 保留={ok_keep}；顺序={list(providers)[:2]}；"
            f"幂等={ok_idem}；能力事实修正={fix_ok}；用户改过的值保留={user_value_kept}",
        )


def test_discover_models() -> None:
    """从服务商 /models 接口解析模型列表（两种返回形态 + 去重 + 异常）。"""

    from unittest import mock

    from acb.ai.discovery import discover_models
    from acb.config import ProviderSpec

    provider = ProviderSpec(
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
        supports_vision=True,
        supports_json_schema=True,
        max_context=128000,
    )

    class _Resp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self._status = status

        def raise_for_status(self):
            if self._status >= 400:
                raise RuntimeError(f"HTTP {self._status}")

        def json(self):
            return self._payload

    try:
        with mock.patch("acb.ai.discovery.requests.get") as get:
            get.return_value = _Resp({"object": "list", "data": [
                {"id": "deepseek-v4-pro"},
                {"id": "deepseek-flash"},
                {"id": "deepseek-flash"},   # 重复 id 要去重
            ]})
            ok_openai = discover_models(provider, "sk-test") == ["deepseek-v4-pro", "deepseek-flash"]

            get.return_value = _Resp([{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}])
            ok_list = discover_models(provider, "sk-test") == ["gpt-4o", "gpt-4o-mini"]

            get.return_value = _Resp({}, status=401)
            try:
                discover_models(provider, "sk-test")
                ok_error = False
            except Exception:
                ok_error = True
    except Exception as exc:
        record("模型列表拉取与解析（/models）", False, str(exc))
        return

    record("模型列表拉取与解析（/models）", ok_openai and ok_list and ok_error,
           f"OpenAI风格={ok_openai} 顶层数组={ok_list} 401抛错={ok_error}")


def test_model_echo_check() -> None:
    """切模型是否**真的**生效：核对服务端回显的 model，而不是只看请求发得出去。

    为什么单独立这一项：把「模型」从 V4.1 Flash 切到 V4 Pro，在协议上只是
    改了请求体里的一个字符串。请求发得出去、返回 200、JSON 也解析得了 ——
    但服务端完全可能用的是**另一个**模型（中转站改路由、把不认识的 id
    降级到自己的默认模型）。这种失败在界面上表现为"一切正常"，
    只有响应里的 `model` 字段能戳穿它。
    """
    from acb.ai.adapter import ModelAdapter
    from acb.ai.client import ApiClient, RequestBudget
    from acb.config import ModelSpec

    spec = ModelSpec(
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        key_env="DEEPSEEK_API_KEY",
        provider="deepseek",
        supports_vision=True,
        supports_json_schema=True,
        max_context=128000,
    )

    sent: list[dict] = []

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload
            self.status_code = 200
            self.text = "ok"

        def json(self) -> dict:
            return self._payload

    class _Session:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def post(self, url, json=None, headers=None, timeout=None):
            sent.append({"url": url, "payload": json, "headers": headers or {}})
            return _Resp(self._payload)

    def call(payload: dict):
        client = ApiClient(spec, "sk-test", RequestBudget(limit=10))
        client._session = lambda: _Session(payload)   # type: ignore[method-assign]
        client.chat([{"role": "user", "content": "hi"}], temperature=0.6)
        return client

    body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}}

    try:
        # 1) 回显 == 请求：确认切换生效
        ok_match = call({**body, "model": "deepseek-v4-pro"})
        match = (
            ok_match.model_mismatch is False
            and ok_match.echo_models == ["deepseek-v4-pro"]
            and "服务端确认模型：deepseek-v4-pro" in ok_match.usage_summary()
            and ok_match.total_prompt_tokens == 120
            and ok_match.total_completion_tokens == 30
        )

        # 2) 回显 != 请求：必须判为不匹配，且摘要里明写"实际用的是谁"
        bad = call({**body, "model": "deepseek-flash"})
        mismatch = (
            bad.model_mismatch is True
            and "⚠ 服务端实际使用：deepseek-flash" in bad.usage_summary()
            and "切换可能未生效" in bad.usage_summary()
        )

        # 3) 服务端不回显 model：不能崩、不能误判成"确认了"
        silent = call(dict(body))
        no_echo = (
            silent.model_mismatch is False
            and silent.echo_models == []
            and "确认模型" not in silent.usage_summary()
            and "输入 120 token" in silent.usage_summary()
        )

        # 4) 请求侧也要对：URL 就是配置里的 full_url，body 里 model 就是选中的那个，
        #    且协议头是按 api_style 分路统一构造的（密钥只走 Authorization）。
        url_ok = sent and sent[0]["url"] == spec.full_url
        model_ok = sent and all(s["payload"]["model"] == "deepseek-v4-pro" for s in sent)
        header_ok = sent and all(
            s["headers"].get("Authorization", "").startswith("Bearer ") for s in sent
        )

        # 5) 日志里要能看见"我到底在调谁"（含中转站的 base_url）
        described = ModelAdapter(spec, "sk-test", RequestBudget(limit=1)).describe()
        describe_ok = spec.full_url in described and "deepseek-v4-pro" in described
    except Exception as exc:
        record("切模型生效性：核对服务端回显的 model", False, f"{type(exc).__name__}: {exc}")
        return

    record(
        "切模型生效性：核对服务端回显的 model（不匹配要说出来）",
        bool(match and mismatch and no_echo and url_ok and model_ok and header_ok and describe_ok),
        f"回显一致={match} 回显不符被识破={mismatch} 无回显不误判={no_echo} "
        f"URL正确={bool(url_ok)} 请求model正确={bool(model_ok)} "
        f"鉴权头正确={bool(header_ok)} 日志含端点={describe_ok}",
    )


def test_api_style_dispatch() -> None:
    """请求形状按 api_style 分路：未实现的形状**加载即拒绝、运行时也不硬发**。

    背景（外部评审指出的真缺口）："切模型只改一个字符串"只对 OpenAI 兼容端点成立。
    Anthropic 原生是 POST /v1/messages（结构不同、max_tokens 必填），
    Gemini 原生把 model 放在 URL 里。以前配置里写什么都没人拦，
    结果会是"发出一个形状错的请求" → 400，或者更糟：网关成功返回但换了模型。
    """
    from acb.ai.client import build_chat_request
    from acb.config import IMPLEMENTED_API_STYLES, ModelSpec, ProviderSpec

    base = dict(
        label="L", base_url="https://api.example.com/v1", key_env="K",
        supports_vision=True, supports_json_schema=True, max_context=4096,
    )

    # 1) 配置层：预设与连接都拒绝未实现的形状（加载期就拦住）
    rejected: list[str] = []
    for cls in (ProviderSpec, ModelSpec):
        # ModelSpec 还需要 model 字段，这里一并给全，免得测成"缺字段"而不是"形状被拒"
        extra = {"model": "claude-x"} if cls is ModelSpec else {}
        try:
            cls(**base, **extra, api_style="anthropic")
            rejected.append(f"{cls.__name__}:未拦住")
        except Exception as exc:
            if "openai" in str(exc) and "anthropic" in str(exc):
                rejected.append("")
            else:
                rejected.append(f"{cls.__name__}:提示不清({str(exc).splitlines()[-1][:60]})")

    # 2) 运行层：内存里手拼的 spec 走到请求构造也必须立刻抛错，而不是硬发
    runtime_blocked = ""
    hand_made = ModelSpec(
        **{**base, "model": "claude-x"}, api_style="openai",
    ).model_copy(update={"api_style": "anthropic"})   # 绕过校验，模拟"另一种入口"
    try:
        build_chat_request(hand_made, "k", [{"role": "user", "content": "hi"}], temperature=0.6)
        runtime_blocked = "未拦住"
    except Exception as exc:
        if "/v1/messages" in str(exc) or "请求构造分支" in str(exc):
            runtime_blocked = ""

    # 3) openai 分支的形状：URL / body / response_format / 鉴权头
    spec = ModelSpec(**{**base, "model": "m1"})
    prepared = build_chat_request(
        spec, "sk-abc", [{"role": "user", "content": "hi"}],
        temperature=0.2, response_format={"type": "json_object"},
    )
    shape_ok = (
        prepared.url == spec.full_url
        and prepared.payload["model"] == "m1"
        and prepared.payload["temperature"] == 0.2
        and prepared.payload["max_tokens"] == spec.max_output_tokens
        and prepared.payload["response_format"] == {"type": "json_object"}
        and prepared.headers["Authorization"] == "Bearer sk-abc"
    )
    no_rf = build_chat_request(spec, "sk-abc", [], temperature=0.6)
    no_rf_ok = "response_format" not in no_rf.payload

    record(
        "请求形状按 api_style 分路（未实现的形状加载期与运行期都拦住）",
        not any(rejected) and runtime_blocked == "" and shape_ok and no_rf_ok and
        list(IMPLEMENTED_API_STYLES) == ["openai"],
        f"配置层拒绝={rejected or '两者都拦住'} 运行层={runtime_blocked or '拦住'} "
        f"形状正确={shape_ok} 无 response_format 时不带该键={no_rf_ok}",
    )


# ---------------------------------------------------------------------------
# 3. 断点键稳定性
# ---------------------------------------------------------------------------
def test_job_key_stability() -> None:
    """硬约束 #11 修正后的关键行为：键不含目录，移动文件夹后不变。"""
    with tempfile.TemporaryDirectory() as tmp:
        dir_a = Path(tmp) / "文件夹A"
        dir_b = Path(tmp) / "文件夹B"
        dir_a.mkdir()
        dir_b.mkdir()

        source = dir_a / "海边 01.cr3"
        source.write_bytes(b"fake-raw-content" * 100)
        key_a = file_key(source)

        moved = dir_b / source.name
        shutil.move(str(source), str(moved))
        key_b = file_key(moved)

        same = key_a == key_b
        record(
            "断点键：移动文件夹后键不变（硬约束 #11 修正）",
            same,
            f"移动前 {key_a} / 移动后 {key_b}",
        )

        # 内容变化后必须变（否则会错误地跳过已修改的文件）
        moved.write_bytes(b"totally-different-content" * 200)
        key_c = file_key(moved)
        record("断点键：文件内容变化后键改变", key_c != key_b, f"新键 {key_c}")


# ---------------------------------------------------------------------------
# 4. 导出命名规则（硬约束 #14）
# ---------------------------------------------------------------------------
def test_job_key_collision() -> None:
    """断点键碰撞的处理（自检抓到的真实 bug 的回归测试）。

    背景：断点键不含目录，因此"同名 + 同大小 + 同 mtime"的文件会撞键。
    这不是理论问题：Windows 的文件时间戳粒度受系统时钟限制
    （实测同一时钟滴答内创建的两个文件 mtime_ns 完全相同），
    所以"批量拷贝后两个子目录里各有一份同名同大小文件"很容易撞。

    早期实现直接让它们共用一个键，导致导出计划（按键索引）里
    第二项覆盖第一项 —— **少出一张图**，而日志显示一切成功。

    现在要求：
      - 内容相同（真重复）→ 共用键（省一次 API 请求），但导出计划仍要有两项；
      - 内容不同 → 键必须不同。
    """
    from acb.pipeline.job import build_scan_items
    from acb.pipeline.output_mode import OutputOptions, build_export_plan

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub1").mkdir()
        (root / "sub2").mkdir()

        # --- 场景 A：同名同大小同 mtime，内容也相同（真重复）---
        same_a = root / "sub1" / "DUP.CR3"
        same_b = root / "sub2" / "DUP.CR3"
        same_a.write_bytes(b"Q" * 128)
        same_b.write_bytes(b"Q" * 128)
        # 强制 mtime 完全一致，模拟"同一时钟滴答内写入"
        import os as _os

        _os.utime(same_a, (1_700_000_000, 1_700_000_000))
        _os.utime(same_b, (1_700_000_000, 1_700_000_000))

        # --- 场景 B：同名同大小同 mtime，但内容不同 ---
        diff_a = root / "sub1" / "DIFF.CR3"
        diff_b = root / "sub2" / "DIFF.CR3"
        diff_a.write_bytes(b"A" * 128)
        diff_b.write_bytes(b"B" * 128)
        _os.utime(diff_a, (1_700_000_000, 1_700_000_000))
        _os.utime(diff_b, (1_700_000_000, 1_700_000_000))

        items = build_scan_items([same_a, same_b, diff_a, diff_b])
        by_path = {str(i.path): i.key for i in items}

        dup_shared = by_path[str(same_a)] == by_path[str(same_b)]
        diff_distinct = by_path[str(diff_a)] != by_path[str(diff_b)]

        # 导出计划必须为**每个源文件**给出一项（用路径做键，不用断点键）
        plan = build_export_plan(items, OutputOptions(sources=[root]))
        plan_complete = len(plan) == 4 and len(set(map(str, plan.values()))) == 4
        print(f"     真重复共用键={dup_shared}（键 {by_path[str(same_a)]}）")
        print(f"     内容不同则键不同={diff_distinct}")
        print(f"     导出计划项数={len(plan)}（源文件 4 个）；输出名={sorted(p.name for p in plan.values())}")

        record(
            "断点键碰撞：真重复共用键、内容不同则消歧、导出不丢项",
            dup_shared and diff_distinct and plan_complete,
            f"共用={dup_shared} 消歧={diff_distinct} 导出项完整={plan_complete}",
        )


def test_export_naming() -> None:
    from acb.constants import EXPORT_DIR_NAME, EXPORT_DIR_NAME_DRYRUN
    from acb.pipeline.job import build_scan_items
    from acb.pipeline.output_mode import OutputOptions, build_export_plan, export_dir_for

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub1").mkdir()
        (root / "sub2").mkdir()
        # 四个文件：
        #   - 两个同名（不同子目录），验证"每个源文件都有自己的输出项"；
        #   - 一个主名以 "-edit" 结尾，验证自定义尾缀不会叠成 "-edit-edit"；
        #   - 一个形如 "DSC-1234"，验证默认尾缀不会把正常编号吃掉（毁成 "DSC-1"）。
        files = [
            root / "sub1" / "IMG_0001.CR3",
            root / "sub2" / "IMG_0001.CR3",
            root / "海边 01-edit.cr3",
            root / "DSC-1234.CR3",
        ]
        for path in files:
            path.write_bytes(b"x" * 64)

        items = build_scan_items(files)

        # --- 默认尾缀（用户没填尾缀 → -1）---
        opts = OutputOptions(sources=[root])
        plan = build_export_plan(items, opts)
        names = sorted(p.name for p in plan.values())
        print(f"     默认尾缀 → {names}")

        # 每个源文件都必须有自己的输出项，且完整路径互不重复。
        # 注意：两个 IMG_0001 分别在 sub1、sub2 里（默认输出到源目录同层），
        # 因此**同名是正确的**（路径不同）。
        # 早期实现因断点键被合并而少出一项，那才是 bug。
        complete = len(plan) == len(files) and len(set(map(str, plan.values()))) == len(files)
        # 默认尾缀绝不能把 "DSC-1234" 吃成 "DSC"（那会产出 DSC-1.jpg，把名字毁了）
        keeps_number = "DSC-1234-1.jpg" in names
        # 源主名已以 "-edit" 结尾时，也不能叠加成 "-edit-edit"
        no_double = not any("-edit-edit" in n for n in names)
        record(
            "导出命名：默认尾缀 -1、不吞编号、不叠加尾缀、每项不丢",
            complete and keeps_number and no_double,
            f"完整={complete} 保留编号={keeps_number} 防叠加={no_double} 名字={names}",
        )

        # --- 自定义尾缀：填 "edit" → "-edit" ---
        edit_opts = OutputOptions(sources=[root], export_suffix="edit")
        edit_plan = build_export_plan(items, edit_opts)
        edit_names = sorted(p.name for p in edit_plan.values())
        print(f"     自定义尾缀 edit → {edit_names}")
        edit_ok = (
            "IMG_0001-edit.jpg" in edit_names
            # 源主名已经以 -edit 结尾 → 剥掉再追加，得到 "海边 01-edit.jpg"
            and "海边 01-edit.jpg" in edit_names
            and not any("-edit-edit" in n for n in edit_names)
        )
        record(
            "导出命名：自定义尾缀（edit → -edit，且不出现 -edit-edit）",
            edit_ok,
            f"名字={edit_names}",
        )

        # --- 连续导出三次（模拟用户重复导出同一张）---
        shared = root / "shared_out"
        # build_export_plan 只**计算**路径、不建目录（这是刻意的：
        # 只生成 manifest 的路径不该在磁盘上留下空文件夹），所以这里自己建。
        shared.mkdir()
        one_item = build_scan_items([root / "sub1" / "IMG_0001.CR3"])

        def _three_times(**extra) -> list[str]:
            produced: list[str] = []
            for _ in range(3):
                batch = build_export_plan(
                    one_item,
                    OutputOptions(sources=[root], output_dir=shared, **extra),
                )
                target = next(iter(batch.values()))
                produced.append(target.name)
                # 写出占位文件，制造"上一次的产物已存在"这一真实前提：
                # 如果不写，三次都会算出同一个名字，测不出冲突处理。
                target.write_bytes(b"x")
            return produced

        default_names = _three_times()
        custom_names = _three_times(export_suffix="edit")
        print(f"     连续导出三次（默认尾缀）→ {default_names}")
        print(f"     连续导出三次（尾缀 edit）→ {custom_names}")

        default_ok = default_names == ["IMG_0001-1.jpg", "IMG_0001-2.jpg", "IMG_0001-3.jpg"]
        custom_ok = custom_names == [
            "IMG_0001-edit.jpg",
            "IMG_0001-edit_2.jpg",
            "IMG_0001-edit_3.jpg",
        ]
        # 用户明确点名的两种"丑名字"都不能出现
        no_ugly = not any(
            bad in n
            for n in default_names + custom_names
            for bad in ("-1_1", "-1_2", "_1_1", "-edit-edit")
        )
        record(
            "导出命名：重名时默认尾缀递增数字(-1/-2/-3)、自定义尾缀追加(_2/_3)",
            default_ok and custom_ok and no_ugly,
            f"默认={default_names} 自定义={custom_names}",
        )

        # --- 导出格式决定扩展名 ---
        def _suffix_for(fmt: str) -> str:
            batch = build_export_plan(
                one_item,
                OutputOptions(sources=[root], output_dir=shared, export_format=fmt),
            )
            return next(iter(batch.values())).suffix.lower()

        ext_map = {fmt: _suffix_for(fmt) for fmt in EXPORT_FORMATS}
        ext_ok = ext_map == {"JPG": ".jpg", "PNG": ".png"}
        record(
            "导出格式：JPG/PNG 各自使用正确扩展名",
            ext_ok,
            f"扩展名={ext_map}",
        )

        # 门禁：TIFF 必须已经彻底移除。
        # 实测 Photoshop 2026 的脚本 DOM 里没有 TIFFSaveOptions，选了必然失败；
        # 与其提供一个选中就报错的选项，不如不提供。
        #
        # 注意这里断言的是"没有 TIFF 的代码路径"（new / function / 枚举），
        # 不是"没出现过 TIFF 这个词" —— jsx 里**应该**留一段注释说明为什么没有它，
        # 否则下一个人很可能会本着"补个功能"的好意把它加回来。
        from acb.paths import jsx_template_path

        jsx_source = jsx_template_path().read_text(encoding="utf-8")
        tiff_code_free = (
            "new TIFFSaveOptions" not in jsx_source
            and "function makeTiffOptions" not in jsx_source
            and "TIFFEncoding" not in jsx_source
        )
        legacy_format, legacy_note = resolve_export_format("TIFF")
        tiff_gone = (
            "TIFF" not in EXPORT_FORMATS
            and tiff_code_free
            and "没有 TIFFSaveOptions" in jsx_source
            and legacy_format == "JPG"
            and legacy_note is not None
        )
        record(
            "导出格式：TIFF 已彻底移除（旧配置回退到 JPG 并说明原因）",
            tiff_gone,
            f"格式清单={EXPORT_FORMATS} jsx无TIFF代码={tiff_code_free} "
            f"旧值回退={legacy_format}",
        )

        # --- 出片目录规则 ---------------------------------------------------
        probe = Path(root / "img_0002.cr3")
        same_level = export_dir_for(probe, opts)
        subdir = export_dir_for(
            probe, OutputOptions(sources=[root], output_to_source_dir=False)
        )
        # 演练模式即使勾了"同层"也必须单独放 _export_dryrun：
        # 混在一起用户就分不清哪张是试出来的、哪张是真的。
        dry = export_dir_for(probe, OutputOptions(sources=[root], dry_run=True))
        dry_sub = export_dir_for(
            probe,
            OutputOptions(sources=[root], dry_run=True, output_to_source_dir=False),
        )
        # 显式指定输出目录时永远优先，演练模式也不例外（那是用户亲手点的目录）。
        override = export_dir_for(
            probe,
            OutputOptions(sources=[root], output_dir=shared, dry_run=True),
        )
        dirs_ok = (
            same_level == root
            and subdir == root / EXPORT_DIR_NAME
            and dry == root / EXPORT_DIR_NAME_DRYRUN
            and dry_sub == root / EXPORT_DIR_NAME_DRYRUN
            and override == shared
        )
        record(
            "出片目录：默认同层 / _export / 演练 _export_dryrun / 显式目录优先",
            dirs_ok,
            f"默认={same_level} 子目录={subdir.name} 演练={dry.name} "
            f"演练+子目录={dry_sub.name} 显式={override}",
        )


# ---------------------------------------------------------------------------
# 5. jsx / manifest / bat 产物
# ---------------------------------------------------------------------------
def test_jsx_editor_false_positive() -> None:
    """守住 jsx 的两条"看起来该改、其实不能改"的东西。

    背景（2026-09-24 用户在 VS Code 的「问题」面板里看到）：
      `export_batch.jsx 104,9 应为 ";"。ts(1005)`
    这是**误报**：.jsx 被 VS Code 的 TS/JS 语言服务按 JSX 解析，
    而 `#target photoshop` 是 ExtendScript 的预处理器指令、不是 JS 语法。

    正解是**关掉编辑器的 JS 语法校验**（仓库 `.vscode/settings.json`），
    **不是**删掉 `#target` —— 那行是双击 .jsx / 用 ExtendScript Toolkit
    直接运行时唯一告诉引擎"目标是 Photoshop"的东西。
    两条都钉在用例里，免得以后有人顺手"修"掉其中一个。
    """
    import json as _json

    from acb.paths import jsx_template_path

    ok = True
    detail: list[str] = []
    root = Path(__file__).resolve().parent.parent

    jsx = jsx_template_path().read_text(encoding="utf-8")
    directive_kept = "#target photoshop" in jsx
    explained = "ts(1005)" in jsx and "ExtendScript" in jsx
    ok = ok and directive_kept and explained
    detail.append(
        f"#target 仍在 {'✓' if directive_kept else '✗'}；文件头解释了误报 {'✓' if explained else '✗'}"
    )

    settings_path = root / ".vscode" / "settings.json"
    settings_ok = False
    if settings_path.is_file():
        raw = settings_path.read_text(encoding="utf-8")
        # settings.json 允许注释（JSONC），解析前先剥掉整行注释
        stripped = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("//")
        )
        try:
            data = _json.loads(stripped)
        except ValueError:
            data = {}
        settings_ok = data.get("javascript.validate.enable") is False
    ok = ok and settings_ok
    detail.append(f".vscode/settings.json 关掉 JS 语法校验 {'✓' if settings_ok else '✗'}")

    record("jsx 编辑器误报（ts(1005)）：关校验而不是删 #target", ok, "；".join(detail))


def test_jsx_products() -> None:
    from acb.constants import (
        DEFAULT_PS_JPEG_QUALITY,
        LEGACY_QUALITY_ALIASES,
        LIBJPEG_EQUIVALENT,
        LOW_QUALITY_WARN_BADGE,
        PS_JPEG_QUALITY_MAX,
        PS_JPEG_QUALITY_MIN,
        PS_JPEG_QUALITY_WARN_MAX,
        clamp_ps_quality,
        describe_low_quality_warning,
        describe_ps_quality,
        is_very_low_quality,
        resolve_ps_quality,
    )
    from acb.ps.jsx_render import render_export_outputs

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "_export"
        # 脚本产物目录与出片目录**刻意分开**：这正是下面两道门禁要守住的性质。
        script_dir = Path(tmp) / "runs" / "20260101_120000_abcd"
        items = [
            {"raw": str(Path(tmp) / "a.CR3"), "out": str(out_dir / "a_E.jpg"), "xmp": ""},
            {"raw": str(Path(tmp) / "b.CR3"), "out": str(out_dir / "b_E.jpg"), "xmp": ""},
        ]
        try:
            plan = render_export_outputs(
                script_dir=script_dir,
                manifest_items=items,
                color_space="Adobe RGB(1998)",
                quality_value=PS_JPEG_QUALITY_MAX,
            )
        except Exception as exc:
            record("jsx/manifest/bat 生成", False, str(exc))
            return

        manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
        print(f"     manifest 色彩空间 = {manifest['colorSpace']} → "
              f"{manifest['colorSpaceProfile']}")
        print(f"     质量 = {manifest['psJpegQualityHint']}")
        print(f"     清单项数 = {len(manifest['items'])}")
        print(f"     日志/结果将写到 = {manifest.get('logPath')}")

        # 门禁 8：Photoshop 侧的日志与结果**不得落在出片目录里**。
        # 真实反馈：这两个记账文件混在用户的交付目录里，既碍事，
        # 又会在打包交付时被一起发出去。
        # 它们由 manifest 携带绝对路径（Python 侧按软件数据目录算好），
        # 脚本照着写；这里守住"两个路径同目录、都是绝对路径、且不在出片目录内"。
        from acb.paths import runs_dir as _runs_dir
        from acb.ps.jsx_render import script_dir_for as _script_dir_for

        log_path = Path(str(manifest.get("logPath") or ""))
        result_path = Path(str(manifest.get("resultPath") or ""))
        logdir_ok = (
            str(log_path) != "."
            and str(result_path) != "."
            and log_path.is_absolute()
            and result_path.is_absolute()
            # 关键：两个记账文件都**不在出片目录里**，而在本次运行的脚本目录里
            and out_dir not in log_path.parents
            and out_dir not in result_path.parents
            and log_path.parent == script_dir
            and result_path.parent == script_dir
            and plan.ps_log_path == log_path
            and plan.ps_result_path == result_path
        )

        # "运行目录必须在 <数据目录>/runs/ 下"是 script_dir_for() 的职责 ——
        # render_export_outputs 只接受调用方给的位置，它管不着放在哪。
        # 两件事分开断言，失败时才能一眼看出该改谁。
        seeded_a = _script_dir_for(out_dir)
        seeded_b = _script_dir_for(out_dir)
        seeded_other = _script_dir_for(Path(tmp) / "other")
        # 运行标识形如 20260101_120000_abcd：时间戳 + "_" + 4 位哈希。
        # 同种子的哈希位必须一致，否则每次调用都新建目录、历史目录越堆越多；
        # 不同种子的哈希位必须不同，否则两批任务会挤进同一个目录互相覆盖。
        seeding_ok = (
            seeded_a.parent == _runs_dir()
            and len(seeded_a.name) == len("20260101_120000_") + 4
            and seeded_a.name[-4:] == seeded_b.name[-4:]
            and seeded_a.name[-4:] != seeded_other.name[-4:]
        )

        # 门禁 9：脚本三件套必须**只在**运行目录里，一个都不许留在出片目录。
        # 真实反馈：离线演练之后，源目录里多出了 export_batch.jsx、manifest.json、
        # run_export.bat 三个文件，出片目录被彻底搞乱。
        # 后半段断言同样重要——出片目录**连建都不该被建出来**：
        # 只要 render_export_outputs 里还留着对出片目录的 mkdir，
        # 用户的源目录就会凭空多出一个空的 _export 文件夹。
        artifacts_ok = (
            plan.script_dir == script_dir
            and plan.jsx_path.parent == script_dir
            and plan.manifest_path.parent == script_dir
            and plan.bat_path.parent == script_dir
            and plan.jsx_path.is_file()
            and plan.manifest_path.is_file()
            and plan.bat_path.is_file()
            and not out_dir.exists()
        )

        # --- 质量值：0–12 必须原样传递，不得存在换算层 --------------------
        # 期望值一律从 constants 推导而不是写死数字：否则改了刻度表就得同步改测试，
        # 而"改了常量忘了改测试"正是这类断言最容易失效的方式。
        #
        # 核心断言是**不做换算**：早期 quality_label="最高" 会被换算成 PS 12，
        # 中间隔着一层有损映射（两档会撞到同一个 PS 值）；现在滑块值直接就是
        # PS 刻度，传 12 就必须写出 12。若有人把映射层偷偷加回来，这里立刻失败。
        quality_ok = (
            manifest["psJpegQuality"] == PS_JPEG_QUALITY_MAX
            and manifest["libjpegEquivalent"] == LIBJPEG_EQUIVALENT[PS_JPEG_QUALITY_MAX]
        )

        # 门禁 1：0–12 的**每一个**取值都必须被原样接受（无提示、无改写）。
        # 只测端点会漏掉"中间某档被夹紧或被当成别名"这类问题。
        roundtrip_bad = [
            value
            for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX + 1)
            if resolve_ps_quality(value) != (value, None)
        ]

        # 门禁 2：越界必须夹到区间内，**并且返回提示文字**。
        # 静默接受非法值会让用户以为设置生效了，实际画质与他选的不符。
        low_value, low_note = resolve_ps_quality(PS_JPEG_QUALITY_MIN - 5)
        high_value, high_note = resolve_ps_quality(PS_JPEG_QUALITY_MAX + 3)
        clamp_ok = (
            low_value == PS_JPEG_QUALITY_MIN
            and high_value == PS_JPEG_QUALITY_MAX
            and low_note is not None
            and high_note is not None
            and clamp_ps_quality(-999) == PS_JPEG_QUALITY_MIN
            and clamp_ps_quality(9999) == PS_JPEG_QUALITY_MAX
        )

        # 门禁 3：旧档位名仍需可解析（兼容已写好的 `--quality 高` 这类脚本），
        # 转换结果要落在合法区间内，并且**必须给出提示**（不能静默转换）。
        alias_bad = [
            name
            for name, expected in LEGACY_QUALITY_ALIASES.items()
            if resolve_ps_quality(name)[0] != expected
            or resolve_ps_quality(name)[1] is None
            or not (PS_JPEG_QUALITY_MIN <= expected <= PS_JPEG_QUALITY_MAX)
        ]

        # 门禁 4：无法识别的字符串要回退到默认值并提示，而不是抛异常。
        # 命令行参数是用户手打的，拼错一个字符就崩掉整个批处理太粗暴。
        fallback_value, fallback_note = resolve_ps_quality("q95")
        fallback_ok = (
            fallback_value == DEFAULT_PS_JPEG_QUALITY and fallback_note is not None
        )

        # 门禁 5：每一档都要有可读说明，前缀是"当前值 / 上限"。
        # 界面就靠这行文字告诉用户当前档位意味着什么。
        describe_bad = [
            value
            for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX + 1)
            if not describe_ps_quality(value).startswith(
                f"{value} / {PS_JPEG_QUALITY_MAX}"
            )
        ]

        # 门禁 7：极低画质判定必须**恰好**覆盖 0..PS_JPEG_QUALITY_WARN_MAX。
        # 差一错误在这里代价很大：阈值写错一档，要么漏掉危险档位（静默放行），
        # 要么把正常档位也标红（狼来了，用户会开始无视警告）。
        warn_bad = [
            value
            for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX + 1)
            if is_very_low_quality(value) != (value <= PS_JPEG_QUALITY_WARN_MAX)
        ]
        # 门禁 7：警告文案必须带齐三件事——当前值、libjpeg 等效值、
        # "这个取值本身合法"。少任何一样，用户在确认框里都不知道自己在确认什么；
        # 特别是最后一项，否则用户会以为程序在报错。
        warn_text_defects = [
            value
            for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_WARN_MAX + 1)
            if str(value) not in describe_low_quality_warning(value)
            or str(LIBJPEG_EQUIVALENT[value]) not in describe_low_quality_warning(value)
            or "合法" not in describe_low_quality_warning(value)
        ]
        # 徽标文案不能是空白——空白徽标会让"标红告警"退化成一次看不见的静默放行。
        warn_badge_ok = bool(LOW_QUALITY_WARN_BADGE.strip())

        quality_ok = (
            quality_ok
            and not roundtrip_bad
            and clamp_ok
            and not alias_bad
            and fallback_ok
            and not describe_bad
            and not warn_bad
            and not warn_text_defects
            and warn_badge_ok
        )
        files_ok = all(p.is_file() for p in
                       (plan.jsx_path, plan.manifest_path, plan.bat_path))
        # bat 必须是 CRLF，否则 cmd 可能解析异常
        bat_bytes = plan.bat_path.read_bytes()
        crlf_ok = b"\r\n" in bat_bytes
        # jsx 必须是"manifest 驱动"的：读 manifest.json → 解析 →
        # 逐个 app.open → saveAs。而不是把任务清单硬编码进脚本。
        #
        # 注意：早期这里用的是 `"DoJavaScript" not in jsx_text` 这种子串断言，
        # 它在注释里出现 "DoJavaScript(...)" 这种说明性文字时就会误报失败——
        # 而脚本头部恰恰需要解释"为什么用 DoJavaScriptFile 而不是 DoJavaScript"。
        # 改成检查真正决定行为的关键调用，断言才与意图一致。
        #
        # 【必须自带 ES3 的 JSON 实现】真实事故：ExtendScript 只有 ECMAScript 3，
        # **没有 JSON 对象**，直接写 JSON.parse 会抛"json 未定义"，
        # 脚本在读到任务清单之前就退出。这里只做廉价的"修改还在不在"的结构断言；
        # 真正的运行验证在 tools/jsx_es3_test.py——它把这段实现原样抽出来，
        # 放进真正的 ES3 引擎（Windows 自带的 JScript）里跑，
        # 并用真实 manifest + 一批边界用例与 Python 的 json 逐项比对。
        jsx_text = plan.jsx_path.read_text(encoding="utf-8")
        polyfill_ok = (
            "ACB_JSON_POLYFILL_BEGIN" in jsx_text
            and "ACB_JSON_POLYFILL_END" in jsx_text
            and "JsonCodec.parse(manifestText)" in jsx_text
            and "JsonCodec.stringify(" in jsx_text
        )

        # 门禁 10：两种导出格式的 SaveOptions 必须真实存在，且保存调用走的是
        # 按 manifest.exportFormat 分发的 makeSaveOptions。
        # 只断言"manifest 里有 exportFormat"是不够的 —— 脚本那边不认这个字段的话，
        # 写出来的就是"名字叫 .png、内容其实是 JPEG"的文件，而日志显示全部成功。
        format_ok = (
            manifest.get("exportFormat") == "JPG"
            and manifest.get("lossless") is False
            and "JPEGSaveOptions" in jsx_text
            and "PNGSaveOptions" in jsx_text
            and "makeSaveOptions(manifest.exportFormat" in jsx_text
            # 旧的写死调用必须彻底消失，否则改了格式也只会出 JPG
            and "makeJpegOptions(manifest.psJpegQuality)" not in jsx_text
        )

        # 每个格式都要能把 exportFormat/lossless 正确写进 manifest
        format_propagate = True
        with tempfile.TemporaryDirectory() as fmt_tmp:
            for one_format in ("JPG", "PNG"):
                probe = render_export_outputs(
                    script_dir=Path(fmt_tmp) / one_format,
                    manifest_items=items,
                    color_space="sRGB",
                    quality_value=PS_JPEG_QUALITY_MAX,
                    export_format=one_format,
                )
                got = json.loads(probe.manifest_path.read_text(encoding="utf-8"))
                expect_lossless = one_format == "PNG"
                if (
                    got.get("exportFormat") != one_format
                    or got.get("lossless") is not expect_lossless
                ):
                    format_propagate = False

        jsx_ok = (
            "manifest.json" in jsx_text
            and polyfill_ok
            and "saveAs" in jsx_text
            and "displayDialogs" in jsx_text        # 必须抑制模态框，否则批处理会挂死
            and "convertProfile" in jsx_text        # 输出色彩空间转换
        )

        record(
            "jsx/manifest/bat 生成与质量刻度传递",
            files_ok and quality_ok and crlf_ok and jsx_ok and logdir_ok and seeding_ok,
            f"文件齐全={files_ok} 质量刻度={quality_ok} bat用CRLF={crlf_ok} "
            f"jsx读manifest={jsx_ok} "
            f"0–{PS_JPEG_QUALITY_MAX}全部原样通过={not roundtrip_bad} "
            f"越界夹紧={clamp_ok} 旧档位名可解析={not alias_bad} "
            f"无法识别时回退={fallback_ok} 说明文案={not describe_bad} "
            f"极低画质判定(≤{PS_JPEG_QUALITY_WARN_MAX})={not warn_bad} "
            f"警告文案完整={not warn_text_defects} 警示徽标非空={warn_badge_ok} "
            f"日志出片目录外={logdir_ok} 运行目录归属={seeding_ok}",
        )
        record(
            "脚本三件套只出现在运行目录，出片目录一个都没有",
            artifacts_ok,
            f"脚本目录={plan.script_dir}；出片目录={out_dir}；"
            f"出片目录被创建={out_dir.exists()}",
        )
        record(
            "导出格式：两种 SaveOptions 齐全且按 manifest 分发",
            format_ok and format_propagate,
            f"脚本含两种格式={format_ok}；格式写入 manifest={format_propagate}",
        )


# ---------------------------------------------------------------------------
# 6. 失败记账与逃生舱（硬约束 #6 / #18）
# ---------------------------------------------------------------------------
def test_job_state() -> None:
    from acb.constants import MAX_ATTEMPTS_PER_FILE
    from acb.pipeline.job import JobState, ScanItem

    with tempfile.TemporaryDirectory() as tmp:
        state = JobState(directory=Path(tmp) / "state")
        target = Path(tmp) / "坏文件.cr3"
        target.write_bytes(b"z" * 32)
        item = ScanItem(path=target, key="fixedkey001", size=32, mtime_ns=123)

        # 前 2 次失败：不应永久跳过
        for _ in range(MAX_ATTEMPTS_PER_FILE - 1):
            state.mark_failed(item, "模拟失败")

        pending, skipped = state.select_pending([item], resume=True, only_failed=True)
        not_skipped_yet = len(pending) == 1

        # 第 3 次失败：达到上限，永久跳过
        state.mark_failed(item, "模拟失败第3次")
        pending2, skipped2 = state.select_pending([item], resume=True, only_failed=True)
        now_skipped = len(pending2) == 0 and len(skipped2) == 1
        print(f"     达到 {MAX_ATTEMPTS_PER_FILE} 次后的跳过原因：{skipped2[0][1][:80] if skipped2 else '（无）'}")

        # 逃生舱：清除失败记录后应能重新进入队列
        cleared = state.clear_failures()
        pending3, _ = state.select_pending([item], resume=True, only_failed=False)
        escapable = len(pending3) == 1 and cleared == 1

        record(
            "失败记账：累计上限 → 永久跳过 → 清除记录可恢复",
            not_skipped_yet and now_skipped and escapable,
            f"上限前放行={not_skipped_yet} 上限后跳过={now_skipped} 清除后恢复={escapable}",
        )

        # 成功记录应能在续跑时跳过
        state.mark_done(item, type("R", (), {"params": {}, "curves": {}})())
        pending4, skipped4 = state.select_pending([item], resume=True, only_failed=False)
        resume_ok = len(pending4) == 0 and len(skipped4) == 1
        record("断点续跑：已完成项不重复请求 API", resume_ok, skipped4[0][1] if skipped4 else "")

        # 【默认必须能重复处理】resume=False 时，已完成的文件也要重新处理。
        # 真实反馈：用户点「开始」想重跑，却因为该入口误传 resume=True 而被静默跳过。
        # 这条断言守住"默认行为就是全量重跑"，防止有人再把默认值改回去。
        rerun_pending, rerun_skipped = state.select_pending(
            [item], resume=False, only_failed=False
        )
        rerun_ok = len(rerun_pending) == 1 and len(rerun_skipped) == 0
        record(
            "全量重跑：resume=False 时已完成项也重新处理",
            rerun_ok,
            f"待处理={len(rerun_pending)} 跳过={len(rerun_skipped)}",
        )


# ---------------------------------------------------------------------------
# 7. XMP 往返（结构指纹）
# ---------------------------------------------------------------------------
def test_xmp_roundtrip(raw_dir: Path | None) -> None:
    from acb.xmp import reader as R
    from acb.xmp import writer as W

    candidates: list[Path] = []
    if raw_dir and raw_dir.is_dir():
        candidates = sorted(raw_dir.rglob("*.xmp"))

    if not candidates:
        record("XMP 往返（未提供 --raw-dir，跳过实测）", True, "未发现样本，跳过", skipped=True)
        return

    worst: list[str] = []
    checked = 0
    for path in candidates[:8]:
        try:
            before = R.XmpDocument(path.read_bytes(), source=str(path))
            fp_before = (
                len(before.descriptions),
                before.rdf_about,
                before.mask_stats().total_corrections,
                before.mask_stats().total_retouch_areas,
                len(before.crs_raw()),
                tuple(sorted(before.curves().keys())),
            )
            data, _applied, _skipped, _warnings = W.render_xmp(
                {"Exposure2012": 0.1, "Vibrance": 5}, None, before
            )
            after = R.XmpDocument(data, source=f"{path.name}!<rt>")
            stats_after = after.mask_stats()
            fp_after = (
                len(after.descriptions),
                after.rdf_about,
                stats_after.total_corrections,
                stats_after.total_retouch_areas,
                len(after.crs_raw()),
                tuple(sorted(after.curves().keys())),
            )
            if fp_before != fp_after:
                worst.append(f"{path.name}: {fp_before} -> {fp_after}")
            checked += 1
        except Exception as exc:
            worst.append(f"{path.name}: 异常 {exc}")

    record(
        f"XMP 往返：{checked} 个真实样本结构指纹保持",
        not worst and checked > 0,
        "；".join(worst[:3]),
    )


# ---------------------------------------------------------------------------
# 8. exiftool 与预览提取
# ---------------------------------------------------------------------------
def test_exiftool_and_preview(raw_dir: Path | None) -> None:
    from acb.raw.exiftool import ExiftoolRunner

    exiftool = ExiftoolRunner()
    if exiftool.available:
        record(
            "exiftool 检测",
            True,
            f"版本 {exiftool.status.version}，路径 {exiftool.status.path}",
        )
    else:
        # 不可用不算失败：程序按硬约束 #10 降级即可，但这会限制 DNG 能力。
        record(
            "exiftool 检测（未安装，属可接受的降级状态）",
            True,
            "未找到 exiftool；预览将走 rawpy 兜底，DNG 内嵌 XMP 读写不可用",
        )

    if not raw_dir or not raw_dir.is_dir():
        record("真实 RAW 预览提取（未提供 --raw-dir，跳过）", True, "", skipped=True)
        return

    raw_exts = {".cr3", ".cr2", ".nef", ".nrw", ".arw", ".srf", ".sr2",
                ".raf", ".orf", ".rw2", ".pef", ".dng"}
    raws = [p for p in sorted(raw_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in raw_exts]
    if not raws:
        record("真实 RAW 预览提取（目录内无 RAW，跳过）", True, "", skipped=True)
        return

    try:
        from acb.raw.preview import extract_thumbnail
    except Exception as exc:
        record("预览提取模块导入", False, f"可能是 rawpy 未安装：{exc}")
        return

    ok = 0
    details: list[str] = []
    for path in raws[:4]:
        try:
            result = extract_thumbnail(path, exiftool, allow_raw_decode_fallback=False)
        except Exception as exc:
            details.append(f"{path.name}: 失败（{str(exc)[:70]}）")
            continue
        ok += 1
        details.append(
            f"{path.name}: 来源={result.source} 颜色判定={result.color_reason[:34]} "
            f"大小={len(result.jpeg_bytes) // 1024}KB"
        )

    record(
        f"真实 RAW 预览提取（{ok}/{min(4, len(raws))} 成功）",
        ok > 0,
        "；".join(details),
    )


def test_style_prompt_priority() -> None:
    """有风格时**绝不能**再把"不做 HSL / 颜色分级"的保守提示词塞进去。

    用户真实反馈：训练完风格直接跑输出，结果「只调整了亮和颜色」，
    他训练里明明有的 HSL / 颜色分级 / 分离色调一次都没用上。
    根因：`prompt=opts.prompt or DEFAULT_PROMPT` —— 提示词框为空时无条件用
    DEFAULT_PROMPT，而它写着「不做 HSL 分区偏移、不做分离色调与颜色分级（保持 0）」，
    与风格块里的规则正面矛盾。
    """
    from acb.ai.prompts import build_output_user_text, render_style_block
    from acb.constants import AUTOPILOT_STYLE_NAME, DEFAULT_PROMPT, STYLE_EXECUTION_PROMPT
    from acb.pipeline.output_mode import resolve_effective_prompt

    style = {
        "name": "测试风格",
        "style_summary": "压绿、冷阴影暖高光",
        # 训练出来的风格必带 text_rules；夹具里也得有，否则"规则那一段"根本没被测到
        "text_rules": ["绿叶压饱和（大约 -25 ~ -40）", "阴影偏冷、高光偏暖"],
        "hsl_habits": "SaturationAdjustmentGreen 常年在 -25 ~ -40",
        "saturation_tendency": "偏保守",
        "shadow_color_cast": "偏冷（蓝青）",
        "lens_correction_preference": "靠机内配置 + 裁剪后晕影",
        "param_ranges": {
            "SaturationAdjustmentGreen": {"mean": -30.0, "median": -30.0, "min": -40.0, "max": -25.0, "count": 6},
            "SplitToningShadowHue": {"mean": 196.0, "median": 185.0, "min": 180.0, "max": 223.0, "count": 6},
        },
        "mask_usage": {
            "samples_total": 12, "samples_with_masks": 8,
            "mask_usage_ratio": 0.667, "corrections_per_sample": 3.58,
        },
        "ignore_fields": [{"field": "AutoLateralCA", "reason": "acr_factory_default"}],
    }

    # 1) 提示词优先级
    p_style, note_style = resolve_effective_prompt("", style)
    p_typed, note_typed = resolve_effective_prompt("保持真实", style)
    p_none, _ = resolve_effective_prompt("", None)
    # 填了提示词 + 选了风格：两句都进请求，且**显式说了谁说了算**。
    # 不给这句时，风格块里的风格倾向与提示词是平级的，模型会各听一半。
    clause = "（本批提示词与上述风格规则冲突时，以本批提示词为准）"
    typed_with_style_ok = (
        p_typed.startswith("保持真实") and clause in p_typed
        and "冲突以提示词为准" in note_typed
    )
    # 「AI自主决策」不该带这句（它的块里写着"没有个人偏好规则"，
    # 再叠一句优先级会让人误以为机器字段禁令也能被顶掉）；没选风格时更没有规则可冲突。
    auto = {"name": AUTOPILOT_STYLE_NAME, "text_rules": [], "param_ranges": {}}
    p_typed_auto, _ = resolve_effective_prompt("保持真实", auto)
    p_typed_bare, _ = resolve_effective_prompt("保持真实", None)
    clause_scope_ok = clause not in p_typed_auto and clause not in p_typed_bare
    priority_ok = (
        p_style is STYLE_EXECUTION_PROMPT
        and "不做 HSL" not in p_style          # 关键：与风格矛盾的句子不许再出现
        and "不做 HSL" not in p_typed          # 填了提示词时更不该出现
        and p_none is DEFAULT_PROMPT            # 真的没风格时才保守
        and "不做 HSL" in DEFAULT_PROMPT        # 无风格时的保守行为没被误改
        and "风格" in note_style
        and typed_with_style_ok
        and clause_scope_ok
    )

    # 2) 风格块必须把训练算出来的"人话特征块"发出去（旧版只发了数字表）
    block = render_style_block(style, "测试风格")
    text = build_output_user_text(
        style_block=block,
        user_prompt=p_style,
        items=[{"file_id": "a", "filename": "a.CR3"}],
    )
    block_ok = (
        "风格倾向（**方向参考**" in text                       # text_rules 必须发出去
        and "绿叶压饱和（大约 -25 ~ -40）" in text
        and "SaturationAdjustmentGreen 常年在 -25 ~ -40" in text   # hsl_habits
        and "偏保守" in text and "偏冷（蓝青）" in text          # 倾向块
        and "靠机内配置 + 裁剪后晕影" in text                     # 镜头习惯
        and "出现过的面板" in text                                # 面板清单
        # 反照抄（用户投诉「像 HDR 一样、和训练集不符」的直接对策）：
        # 统计表旁边必须有自检句，否则模型会把中位数当成每张图的目标值照抄。
        and "输出前自检" in text and "同一批里不同照片的数值**应当不同**" in text
        # 蒙版：本程序**现在能**写几何蒙版了，所以这里断言的是"确实会说清怎么提"。
        # （旧断言写的是"无法写入蒙版"，步骤 5 上线后必须同步改，否则会假红）
        and "局部调整习惯" in text and "67%" in text
        and "不要为了凑数" in text
        # 三种 kind 的坐标写法必须带例子：实测 qwen 会把 brush/linear 的几何都写成 rect，
        # 光写说明不管用（解析器也为此加了容错，见 test_mask_geometry_tolerance）。
        and '"zero": [0.5, 0.75]' in text and '"dabs"' in text
        and "不做 HSL" not in text
    )

    # 2b) 样本数 ≤2 的字段必须被标注“不能当风格特征”：
    # 实测最刺眼的例子是 Vibrance 只有 1 个样本（范围 19~19），模型给每张都写 19。
    thin_style = dict(style)
    thin_style["param_ranges"] = {
        "Vibrance": {"mean": 19.0, "median": 19.0, "min": 19.0, "max": 19.0, "count": 1},
    }
    thin_block = render_style_block(thin_style, "少样本风格")
    thin_ok = "样本太少" in thin_block and "不能**当成风格特征" in thin_block

    # 3) 填了提示词时，风格块**仍然在**，而且优先级那句排在风格规则之后
    #（排前面的话它就会被当成"风格规则"的一部分，白写）。
    text_both = build_output_user_text(
        style_block=block,
        user_prompt=p_typed,
        items=[{"file_id": "a", "filename": "a.CR3"}],
    )
    both_ok = (
        clause in text_both
        and "风格倾向（**方向参考**" in text_both          # 风格没被提示词挤掉
        and "保持真实" in text_both
        and text_both.index("风格倾向（**方向参考**") < text_both.index(clause)
        and text_both.count(clause) == 1
    )

    record(
        "风格优先于默认提示词（无风格才用保守提示词）",
        bool(priority_ok),
        f"有风格→风格提示词={p_style is STYLE_EXECUTION_PROMPT} "
        f"无'不做HSL'={'不做 HSL' not in p_style} "
        f"用户提示词优先={p_typed.startswith('保持真实')} "
        f"冲突以提示词为准={typed_with_style_ok}（AI自主决策/未选风格不带这句={clause_scope_ok}） "
        f"无风格→默认={p_none is DEFAULT_PROMPT}",
    )
    record(
        "风格块与用户提示词并存（提示词不挤掉风格，优先级句排在规则之后）",
        bool(both_ok),
        f"含风格倾向={'风格倾向（**方向参考**' in text_both} 含提示词={'保持真实' in text_both} "
        f"含优先级句={clause in text_both}（{text_both.count(clause)} 次）",
    )
    record(
        "风格块发出训练算出的特征块 + 蒙版说明",
        bool(block_ok),
        f"风格块 {len(block)} 字符，完整 user 文本 {len(text)} 字符；"
        f"含HSL习惯={'SaturationAdjustmentGreen 常年在 -25 ~ -40' in text} "
        f"含面板清单={'出现过的面板' in text} 含蒙版说明={'局部调整习惯' in text}",
    )
    record(
        "少样本（count≤2）字段被标注为不能当风格特征",
        bool(thin_ok),
        f"含『样本太少』={'样本太少' in thin_block}",
    )


def test_dimension_levels() -> None:
    """维度力度档位（实施步骤 1b）：风格只给"方向 + 力度"，不给"照抄用"的数值。"""
    from acb.ai import prompts as P
    from acb.pipeline import style_profile as SP

    ok = True
    detail: list[str] = []

    # 1) 力度归一化：普通滑块 ±100 下，|-50| → 0.5；绝对色相（0..360）不计入力度
    strength = SP._field_strength("Highlights2012", -50)
    hue_skipped = SP._field_strength("SplitToningShadowHue", 180) is None
    normalize_ok = strength is not None and abs(strength - 0.5) < 1e-9 and hue_skipped
    ok = ok and normalize_ok
    detail.append(
        f"力度归一化 {'✓' if normalize_ok else f'✗ {strength}'}"
        f"、绝对色相排除 {'✓' if hue_skipped else '✗'}"
    )

    # 2) 档位落在 1..7、力度更大则档位不降、且带证据
    mild = SP.compute_dimension_levels({
        "Highlights2012": {"median": -12.0, "count": 5},
        "Shadows2012": {"median": 10.0, "count": 5},
    })
    strong = SP.compute_dimension_levels({"Highlights2012": {"median": -95.0, "count": 5}})
    in_range = bool(mild) and all(1 <= entry["level"] <= SP.DIMENSION_LEVELS for entry in mild.values())
    monotone = strong["tone"]["level"] > mild["tone"]["level"]
    evidence = all(entry.get("evidence") and entry.get("fields") for entry in mild.values())
    ok = ok and in_range and monotone and evidence
    detail.append(
        f"档位 {mild['tone']['level_text']} → {strong['tone']['level_text']}"
        f"{'✓' if monotone else '✗'}；落域{'✓' if in_range else '✗'}；证据{'✓' if evidence else '✗'}"
    )

    # 3) 提示词里必须同时出现"档位"与"禁止照抄统计值"
    block = P.render_style_block({"name": "测试风格", "dimension_levels": mild}, "测试风格")
    prompt_ok = "各维度力度档位" in block and "禁止把下面的历史统计值当作每张图的目标值照抄" in block
    ok = ok and prompt_ok
    detail.append(f"提示词含档位与禁照抄 {'✓' if prompt_ok else '✗'}")

    record("维度力度档位（1..7）与「禁止照抄统计值」", ok, "；".join(detail))


def test_scene_rules_and_wb_offset() -> None:
    """场景规则与白平衡偏移（实施步骤 1c）：规则只来自训练素材，偏移只算相对量。"""
    from acb.ai import prompts as P
    from acb.pipeline import style_profile as SP

    ok = True
    detail: list[str] = []

    # 1) 样本不足的场景不得出规则；绝对色温不得进规则（否则会被照抄开尔文值）
    one_only = SP.compute_scene_rules({"罕见场景": [{"Exposure2012": 1.0}]})
    enough = SP.compute_scene_rules({
        "人像（环境）": [
            {"Sharpness": 70, "Temperature": 5800, "Tint": 12},
            {"Sharpness": 60, "Temperature": 5700, "Tint": 10},
            {"Sharpness": 65, "Temperature": 5900, "Tint": 14},
        ]
    })
    fields_in_rule = set((enough[0]["fields"] if enough else {}).keys())
    scene_ok = (
        not one_only
        and len(enough) == 1
        and fields_in_rule == {"Sharpness", "Tint"}
        and enough[0]["samples"] == 3
        and "3 张训练样本" in enough[0]["text"]
    )
    ok = ok and scene_ok
    detail.append(
        f"场景规则（n<3 不出规则 {'✓' if not one_only else '✗'}；"
        f"排除绝对色温 {'✓' if 'Temperature' not in fields_in_rule else '✗'}；"
        f"带样本数 {'✓' if enough and '3 张训练样本' in enough[0]['text'] else '✗'}）"
    )

    # 2) 白平衡只统计“相对 as-shot”的偏移，且跳过 WhiteBalance 非 Custom 的样本
    wb = SP.compute_wb_offset([
        {"params": {"Temperature": 5500, "Tint": 18}, "wb_effective": True, "as_shot_cct": 4743},
        {"params": {"Temperature": 5600, "Tint": 8}, "wb_effective": True, "as_shot_cct": 4955},
        {"params": {"Temperature": 9999, "Tint": 99}, "wb_effective": False, "as_shot_cct": 1000},
    ])
    wb_ok = (
        wb is not None
        and wb["count"] == 2
        and wb["kelvin"]["median"] == 701.0
        and wb["direction"] == "偏暖"
        and "相对 as-shot" in wb["text"]
    )
    ok = ok and wb_ok
    detail.append(f"白平衡偏移（中位 {wb['kelvin']['median'] if wb else '?'}K，方向 {wb.get('direction') if wb else '?'}）{'✓' if wb_ok else '✗'}")

    # 3) 提示词里必须出现场景规则与“逐张独立判断”
    block = P.render_style_block(
        {"name": "测试风格", "scene_rules": enough, "wb_offset": wb},
        "测试风格",
    )
    prompt_ok = (
        "场景规则（依据你的训练素材统计" in block
        and "逐张独立判断" in block
        and "只在对应场景成立" in block
    )
    ok = ok and prompt_ok
    detail.append(f"提示词含场景规则与白平衡约束 {'✓' if prompt_ok else '✗'}")

    record("场景规则（只来自训练素材）+ 白平衡相对偏移", ok, "；".join(detail))


def test_output_guardrails() -> None:
    """程序侧护栏（实施步骤 3）：分组预算、极端需依据、照抄告警、逐图指标注入。"""
    from acb.ai import guardrails as G
    from acb.ai import prompts as P

    ok = True
    detail: list[str] = []

    # 1) 饱和度总量预算：多个面板一起推时必须按比例缩
    _budgeted, budget_notes = G.apply_group_budgets({
        "Saturation": 30, "Vibrance": 35, "SaturationAdjustmentGreen": 40, "ColorGradeGlobalSat": 60,
    })
    budget_ok = len(budget_notes) == 1 and "提饱和总量" in budget_notes[0]
    # 单独一个饱和度滑块不该被夹
    _single, single_notes = G.apply_group_budgets({"Saturation": 25})
    single_ok = not single_notes
    ok = ok and budget_ok and single_ok
    detail.append(f"饱和度预算 {'✓' if budget_ok else '✗'}；单字段不误伤 {'✓' if single_ok else '✗'}")

    # 2) 晕影总预算（镜头晕影 + 裁剪后晕影）
    vignette, vignette_notes = G.apply_group_budgets({"VignetteAmount": -10, "PostCropVignetteAmount": -40})
    vignette_ok = (
        len(vignette_notes) == 1
        and vignette["VignetteAmount"] + vignette["PostCropVignetteAmount"] >= G.VIGNETTE_BUDGET
    )
    ok = ok and vignette_ok
    detail.append(f"晕影预算 → {vignette['VignetteAmount']}/{vignette['PostCropVignetteAmount']} {'✓' if vignette_ok else '✗'}")

    # 3) 极端值：无依据夹回；规则里提到（包括中文别名）就不夹；置信度低时上限收紧
    _p, clamp_notes = G.clamp_extremes({"SaturationAdjustmentAqua": -52}, None)
    clamp_ok = len(clamp_notes) == 1 and "夹到" in clamp_notes[0]
    covered_style = {
        "text_rules": ["HSL 分区习惯：Aqua 去饱和、Green 提亮以压绿"],
        "scene_rules": [],
        "param_ranges": {"SaturationAdjustmentAqua": {"count": 12, "std": 8.0}},
    }
    _p2, covered_notes = G.clamp_extremes({"SaturationAdjustmentAqua": -52}, covered_style)
    covered_ok = not covered_notes
    weak_style = {
        "text_rules": [], "scene_rules": [],
        "param_ranges": {"LuminanceAdjustmentGreen": {"count": 2, "std": 59.0}},
    }
    _p3, weak_notes = G.clamp_extremes({"LuminanceAdjustmentGreen": 41}, weak_style)
    weak_ok = len(weak_notes) == 1 and "置信度收紧" in weak_notes[0]
    ok = ok and clamp_ok and covered_ok and weak_ok
    detail.append(
        f"极端夹紧 {'✓' if clamp_ok else '✗'}；有规则不夹 {'✓' if covered_ok else '✗'}；"
        f"置信度收紧 {'✓' if weak_ok else '✗'}"
    )

    # 4) 照抄检测：取值完全相同且等于统计中心才报
    style = {"param_ranges": {"Exposure2012": {"median": 0.3, "mean": 0.3}}}
    copied = G.detect_copying([{"Exposure2012": 0.3} for _ in range(6)] + [{"Exposure2012": 0.1}], style)
    varied = G.detect_copying([{"Exposure2012": v / 10} for v in range(1, 8)], style)
    copy_ok = len(copied) == 1 and not varied
    ok = ok and copy_ok
    detail.append(f"照抄检测（报警 {len(copied)} 项，变化时不报 {'✓' if not varied else '✗'}）")

    # 5) 逐图指标必须进提示词，并带上"按本图重新判断"的说明
    text = P.build_output_user_text(
        style_block="【风格约束】测试",
        user_prompt="",
        items=[{"file_id": "a1", "filename": "a.CR3",
                "metrics": "亮度均值 96/255（P5 12、P95 240）、亮度标准差（对比度代理）42"}],
    )
    metrics_ok = "本图实测指标" in text and "不要套用风格统计里的数值" in text
    ok = ok and metrics_ok
    detail.append(f"逐图指标注入提示词 {'✓' if metrics_ok else '✗'}")

    record("程序侧护栏（预算/极端需依据/照抄告警/逐图指标）", ok, "；".join(detail))


def test_lens_baseline_and_program_fields() -> None:
    """镜头开关（实施步骤 4）：逐图基线、只补缺、绝不写配置文件名/摘要、AI 不碰。"""
    from acb.ai import prompts as P
    from acb.raw import lens as L
    from acb.xmp import fields as F
    from acb.xmp import writer as W

    ok = True
    detail: list[str] = []

    # 1) AI 不该碰镜头开关（配置文件开关/畸变/色差/去边/镜头晕影），但效果面板的裁剪后晕影可写
    switch_ai = {name: F.get_field(name).ai_writable for name in
                 ("LensProfileEnable", "AutoLateralCA", "LensManualDistortionAmount",
                  "LensProfileDistortionScale", "LensProfileVignettingScale",
                  "DefringePurpleAmount", "DefringeGreenAmount", "VignetteAmount")}
    registry_ok = (
        not any(switch_ai.values())
        and F.get_field("PostCropVignetteAmount").ai_writable
    )
    ok = ok and registry_ok
    detail.append(f"AI 字段表（镜头组均不可写 {'✓' if not any(switch_ai.values()) else '✗'}，"
                  f"裁剪后晕影可写 {'✓' if F.get_field('PostCropVignetteAmount').ai_writable else '✗'}）")

    # 2) 基线优先级：该照片自己的 crs > crd 相机默认；两者都没有则不猜
    crs_win = L.read_lens_baseline({"LensProfileEnable": "0"}, {"LensProfileEnable": "1"})
    crd_fallback = L.read_lens_baseline({}, {"LensProfileEnable": "1"})
    empty = L.read_lens_baseline({}, {})
    priority_ok = (
        crs_win.values == {"LensProfileEnable": "0"}
        and crd_fallback.values == {"LensProfileEnable": "1"}
        and not empty.resolved
    )
    ok = ok and priority_ok
    detail.append(f"基线优先级（crs 覆盖 crd，空则不猜）{'✓' if priority_ok else '✗'}")

    # 3) 只补缺：目标已有该字段时不进候选
    missing = L.missing_from({"LensProfileEnable": "1"}, crs_win)
    missing_ok = "LensProfileEnable" not in missing
    ok = ok and missing_ok
    detail.append(f"只补缺（已有值不覆盖）{'✓' if missing_ok else '✗'}")

    # 4) 写出：程序字段能写进新建侧车，但绝不写配置文件名/摘要
    import tempfile

    baseline = L.read_lens_baseline(
        {"LensProfileEnable": "1", "AutoLateralCA": "1", "LensProfileSetup": "LensDefaults"},
        {},
    )
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "a.CR3"
        raw.write_bytes(b"raw")
        result = W.write_sidecar(raw, {}, None, program_fields=baseline.values)
        text = result.target.read_text(encoding="utf-8")
        wrote_ok = 'crs:LensProfileEnable="+1"' in text or 'crs:LensProfileEnable="1"' in text
        never_ok = not any(key in text for key in L.LENS_NEVER_WRITE)
        # 已有值不被覆盖
        W.write_sidecar(raw, {}, None, program_fields={"LensProfileEnable": 0})
        kept_ok = 'crs:LensProfileEnable="0"' not in result.target.read_text(encoding="utf-8")
    write_ok = wrote_ok and never_ok and kept_ok
    ok = ok and write_ok
    detail.append(f"写出（补缺 {'✓' if wrote_ok else '✗'}，不写配置名/摘要 {'✓' if never_ok else '✗'}，"
                  f"不覆盖已有 {'✓' if kept_ok else '✗'}）")

    # 5) 提示词里明确说"镜头由程序负责、不要返回这些字段"
    block = P.render_style_block({"name": "测试风格"}, "测试风格")
    prompt_ok = "镜头校正、去色差、以及**镜头晕影**都由程序按每张照片自己的基线处理" in block \
        and "不要返回这些字段" in block
    ok = ok and prompt_ok
    detail.append(f"提示词声明 {'✓' if prompt_ok else '✗'}")

    record("镜头开关（逐图基线/只补缺/不写配置摘要）", ok, "；".join(detail))


def test_mask_pipeline_integration() -> None:
    """步骤 5：AI 提意图 → 程序校验 → 逐图方向换算 → 写入；已有蒙版不动。"""
    from acb.pipeline import output_mode as OM
    from acb.xmp import masks as M
    from acb.xmp import writer as W

    ok = True
    detail: list[str] = []

    # 1) parse_specs：非法条目逐条丢弃、上限夹紧、局部参数受限
    raw = [
        {"kind": "linear", "target": "天空", "zero": [0.5, 0.3], "full": [0.5, 0.05],
         "local": {"LocalExposure2012": -0.9, "LocalHighlights2012": -60}},
        {"kind": "radial", "target": "主体", "rect": [0.3, 0.3, 0.7, 0.7],
         "local": {"LocalExposure2012": 0.5, "LocalGrain": 25}},
        {"kind": "brush", "target": "水面", "dabs": [[0.2, 0.8], [0.4, 0.82]],
         "local": {"LocalTexture": 40}},
        {"kind": "圆形", "target": "乱写", "local": {"LocalExposure2012": 1}},
        {"kind": "linear", "target": "无坐标", "local": {"LocalExposure2012": 1}},
        {"kind": "linear", "target": "空局部", "zero": [0.5, 1], "full": [0.5, 0], "local": {}},
    ]
    specs, warns = M.parse_specs(raw)
    # 【范围来源】这张表的每个范围都必须是从用户样本实测出来的，且必须落在面板范围内。
    # 2026-09-23 这里曾经是我按"工程直觉"拍的非对称范围，被用户当场否掉
    #（"你这个又是哪里来的范围？我不是说要以用户训练数据为准吗？"）——
    # 所以自检要在两处守住：① 与实测值一致；② 不越出字段本身的面板范围。
    from acb.xmp import fields as _F

    range_ok = all(
        isinstance(limits, tuple) and len(limits) == 2 and limits[0] < limits[1]
        and (spec := _F.get_field(name)) is not None
        and spec.minimum is not None and spec.maximum is not None
        and limits[0] >= spec.minimum and limits[1] <= spec.maximum
        for name, limits in M.AI_LOCAL_RANGES.items()
    )
    observed_ok = M.AI_LOCAL_RANGES["LocalHighlights2012"] == (-100.0, 82.0) \
        and M.AI_LOCAL_RANGES["LocalTexture"] == (-61.0, 16.0)
    parse_ok = (
        range_ok and observed_ok
        and len(specs) == M.PROGRAM_MAX_MASKS
        # 实测范围 -100..82 → -60 在范围内，不再被夹（旧版的 ±40 会把它夹掉）
        and specs[0]["local"]["LocalHighlights2012"] == -60.0
        and "LocalGrain" not in specs[1]["local"]                  # 不在清单里的丢弃
        and specs[2]["local"]["LocalTexture"] == 16.0              # 超出实测上限 → 夹到 16
        # 6 条输入里：3 条合法（其中 1 条的值被清理）、3 条整条丢弃
        and len(warns) == 5
    )
    ok = ok and parse_ok
    detail.append(f"parse_specs（接受 {len(specs)}/{M.PROGRAM_MAX_MASKS}，警告 {len(warns)} 条；"
                  f"范围来自实测 {range_ok and observed_ok}）{'✓' if parse_ok else '✗'}")

    # 2) 风格准入门槛：训练素材里基本不用蒙版 → 不放行；自主决策 → 放行
    rare = {"name": "少用蒙版", "mask_usage": {"samples_with_masks": 1, "samples_total": 25,
                                              "mask_usage_ratio": 0.04}}
    common = {"name": "常用蒙版", "mask_usage": {"samples_with_masks": 7, "samples_total": 12,
                                              "mask_usage_ratio": 0.58}}
    gate_ok = (
        not OM._style_uses_masks(rare)
        and OM._style_uses_masks(common)
        and OM._style_uses_masks({"name": "AI自主决策"})
        and not OM._style_uses_masks(None)
    )
    ok = ok and gate_ok
    detail.append(f"风格门槛（少用不漏放、常用放行、自主决策放行）{'✓' if gate_ok else '✗'}")

    # 3) 写入链路：显示帧坐标 + 竖构图方向 → 存储帧换算后落盘
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "a.CR3"
        raw_path.write_bytes(b"raw")
        result = W.write_sidecar(
            raw_path, {}, None,
            masks=specs[:1],
            orientation="Rotate 90 CW",
            masks_frame="display",
        )
        text = result.target.read_text(encoding="utf-8")
        # 显示帧 zero=(0.5, 0.3)/full=(0.5, 0.05) → Rotate 90 CW 下存成 (x=0.3,y=0.5)/(x=0.05,y=0.5)
        convert_ok = 'crs:ZeroX="0.3"' in text and 'crs:ZeroY="0.5"' in text \
            and 'crs:FullX="0.05"' in text and 'crs:FullY="0.5"' in text
        name_ok = '线性渐变' not in text and '天空' in text

        # 4) 已有蒙版的文件：绝不覆盖
        existing = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Auto Color Diffusion">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""'
            ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">\n'
            '   <crs:MaskGroupBasedCorrections><rdf:Seq><rdf:li/></rdf:Seq></crs:MaskGroupBasedCorrections>\n'
            '  </rdf:Description>\n </rdf:RDF>\n</x:xmpmeta>\n'
        )
        raw2 = Path(tmp) / "b.CR3"
        raw2.write_bytes(b"raw")
        W.sidecar_path_for(raw2).write_text(existing, encoding="utf-8")
        keep = W.write_sidecar(raw2, {}, None, masks=specs[:1], orientation="Horizontal (normal)")
        untouched = any("已有蒙版组" in w for w in keep.warnings)
    write_ok = convert_ok and name_ok and untouched
    ok = ok and write_ok
    detail.append(f"写入链路（方向换算 {'✓' if convert_ok else '✗'}，命名 {'✓' if name_ok else '✗'}，"
                  f"不动已有蒙版 {'✓' if untouched else '✗'}）")

    # 5) 方向缺失时不许静默写错
    try:
        M.apply_masks.__doc__ and W.render_xmp({}, None, None, None, masks=specs[:1])
        guard_ok = False
    except ValueError:
        guard_ok = True
    ok = ok and guard_ok
    detail.append(f"缺方向报错{'✓' if guard_ok else '✗'}")

    record("步骤5 蒙版接入（意图解析/门槛/方向换算/不动已有）", ok, "；".join(detail))


def test_mask_geometry_tolerance() -> None:
    """蒙版几何的容错：模型把几何写成 rect 时，能救的要救，方向不明的才丢。

    【为什么非加不可】实测（qwen3-omni-flash + 真风格 + 真图）：模型确实开始返回蒙版了，
    但它把 **brush 与 linear 的几何都写成 rect**，而解析器只认 brush→dabs / linear→zero,full，
    于是“模型给了蒙版”仍然等于“一张都没写进 XMP”——用户看到的还是“一个蒙版也没有”。

    三条规矩：
      ① brush + rect → 按该区域铺网格落点（“涂这块地方”语义一致，比丢弃更接近原意）；
      ② linear + rect → 只在区域**贴着画框边缘**时才接受（从贴边侧朝对侧衰减），
         不贴边就没有方向可言，必须丢弃（宁可不给，也不能给一个方向错的渐变）；
      ③ 显式 zero/full、dabs 的老写法不受影响。
    """
    from acb.xmp import masks as M

    raw = [
        # ① 画笔只给了 rect（模型原始返回里的样子）
        {"kind": "brush", "target": "人物面部", "intent": "提亮", "rect": [0.55, 0.45, 0.7, 0.6],
         "local": {"LocalExposure2012": 0.3}},
        # ② 线性只给了 rect，且贴着上边缘（天空）
        {"kind": "linear", "target": "天空", "intent": "压暗", "rect": [0.0, 0.0, 1.0, 0.3],
         "local": {"LocalHighlights2012": -35}},
        # ③ 线性只给了 rect，但**不贴边** → 方向不明，必须丢
        {"kind": "linear", "target": "当中一块", "intent": "x", "rect": [0.3, 0.3, 0.6, 0.6],
         "local": {"LocalExposure2012": 0.2}},
        # ④ 老写法（显式坐标）仍然有效
        {"kind": "linear", "target": "底部", "intent": "提亮", "zero": [0.5, 0.1], "full": [0.5, 0.9],
         "local": {"LocalShadows2012": 20}},
    ]
    specs, warns = M.parse_specs(raw)
    types = [s["type"] for s in specs]
    brush_ok = types[:1] == ["brush"] and len(specs[0].get("dabs") or []) == 25
    linear_ok = (
        "linear" in types
        and any(abs(s["full"][1]) < 0.02 for s in specs if s["type"] == "linear")
    )
    dropped_ok = sum(1 for w in warns if "缺少 zero/full" in w) == 1   # 只有③被丢
    note_ok = any("只给了 rect" in w and "朝对侧衰减" in w for w in warns)
    ok = brush_ok and linear_ok and dropped_ok and note_ok
    record(
        "蒙版几何容错（brush→网格落点、linear+贴边 rect 可救、不贴边仍丢）",
        ok,
        f"解析={types}；画笔落点={len(specs[0].get('dabs') or [])}（要 25）；"
        f"丢弃计数对={dropped_ok}；解释性警告={note_ok}；警告共 {len(warns)} 条",
    )


def test_train_budget_allows_retry() -> None:
    """训练通路的请求预算必须 ≥2，且超限文案要说清是哪条通路的预算。

    【这条用例是被一次“静默的训练失败”换来的】用户 44 个样本的训练：
    首发在 180s 读超时上挂掉 → 重试需要第二个预算位 → 而当时预算是 1 →
    重试直接被拒（错误却是“密钥失效/模型名写错”那套文案），
    **风格档案没保存**，用户以为已经重训过，之后几轮输出用的仍是旧风格。
    """
    import inspect

    import acb.pipeline.train_mode as TM
    from acb.ai.client import RequestBudget
    from acb.constants import TRAIN_REQUEST_BUDGET
    from acb.errors import BudgetExceededError

    budget = RequestBudget(limit=2, hint="训练只发一次归纳请求，含读超时重试")
    budget.spend(1)                      # 首发
    budget.spend(1)                      # 读超时后的重试（以前这里会被拒）
    over_ok = False
    try:
        budget.spend(1)
    except BudgetExceededError as exc:
        over_ok = "训练只发一次归纳请求" in str(exc) and "文件数 × 2" not in str(exc)

    source = inspect.getsource(TM.run_train_mode)
    wired = "adapter.client.budget = RequestBudget(" in source and "TRAIN_REQUEST_BUDGET" in source
    message_ok = "训练**没有完成：风格档案没有被保存或更新**" in source
    constant_ok = TRAIN_REQUEST_BUDGET >= 2
    ok = over_ok and wired and message_ok and constant_ok
    record(
        "训练预算允许一次重试（且超限文案指明通路）",
        ok,
        f"常量={TRAIN_REQUEST_BUDGET}；两次占用后第三次被拒={over_ok}；"
        f"预算真的被设进 run_train_mode={wired}；失败时说明风格未保存={message_ok}",
    )


def test_style_expects_masks_but_none_returned() -> None:
    """风格本来就用蒙版、而这一批一个都没给时，日志里必须点名说出来。

    【为什么非加不可】用户连着几轮反馈「一个蒙版也没有」，而日志里一个字不提：
    系统提示词里一句旧话把 masks 一律禁了，谁也无法从日志里看出问题所在。
    “沉默的缺失”必须变成一条可见告警（风格 72% 带蒙版 vs 本批 0 个）。
    """
    import logging
    import shutil
    import tempfile

    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.pipeline.job import JobState, build_scan_items, iter_raw_files
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.exiftool import ExiftoolRunner

    repo = Path(__file__).resolve().parent.parent
    cr3 = next(iter(sample_material("*.CR3")), None)
    if cr3 is None:
        record_missing_material("风格要蒙版但本批没给时必须告警", "需要 test_data/ 里的 CR3")
        return

    style = {
        "name": "蒙版风格（测试）",
        "text_rules": [],
        "param_ranges": {},
        "scene_rules": [],
        "local_param_ranges": {},
        # 18/25 = 72% ≥ STYLE_MASK_USAGE_MIN_RATIO(0.4) → 风格门槛放行、期望有蒙版
        "mask_usage": {"samples_total": 25, "samples_with_masks": 18,
                        "mask_usage_ratio": 0.72, "corrections_per_sample": 4.6},
    }

    captured: list[tuple[int, str]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            captured.append((record.levelno, record.getMessage()))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "photos"
        work.mkdir()
        shutil.copy2(cr3, work / cr3.name)

        callbacks = JobCallbacks(
            log=lambda message, level: None,
            progress=lambda done, total: None,
            stage=lambda text: None,
            item_status=lambda name, status: None,
            should_stop=lambda: False,
        )
        handler = _Capture(level=logging.WARNING)
        logging.getLogger().addHandler(handler)
        try:
            result = run_output_mode(
                OutputOptions(sources=[work], run_photoshop=False, use_cache=False, workers=1,
                              style_name=style["name"], style_data=style),
                adapter=OfflineAdapter(load_models_config().active_spec()),
                exiftool=ExiftoolRunner(),
                job_state=JobState(directory=Path(tmp) / "state"),
                callbacks=callbacks,
            )
        finally:
            logging.getLogger().removeHandler(handler)

        notice = next((msg for level, msg in captured
                       if level >= logging.WARNING and "局部调整" in msg), "")
        ok = (
            "没有给出局部调整" in notice
            and "72%" in notice          # 风格自己的比例必须报出来
            and "1 张" in notice          # 本批规模
            and result.done >= 1
        )
        detail = f"done={result.done}；告警原文：" + notice.replace("\n", " ")[:200]
    record("风格要蒙版但本批一个都没有 → 必须可见告警", ok, detail)


def test_process_version_is_written() -> None:
    """设置包必须写明「处理版本」，否则 ACR 按「1 版（2003）」渲染 —— 写进去的 2012 滑块全废。

    【这条用例是两个用户问题的直接产物】他在 ACR 里打开我们写的文件，校准面板显示
    「处理版本 = 1 版」，问“为什么不用 6 版”。查下来的事实：我们新建的侧车与改写的
    DNG 内嵌包都**没有** crs:ProcessVersion（旧代码刻意不写，理由是“留空则 ACR 用
    自己的当前默认版本”——这个假设是错的：留空时 ACR 按最老的解释处理）。
    后果不是“看着难看”那么轻：2003 引擎不认高光/阴影/白色/黑色/纹理/去薄雾/颜色分级，
    蒙版（需 PV4+）更不会生效 —— 成片与参数对不上，正是他说“像 HDR 一样的高饱和、
    和训练集一点也不符”的一块拼图。
    """
    from acb.xmp import reader as R
    from acb.xmp import writer as W

    # ① 新建侧车骨架：必须带处理版本
    skeleton = W.serialize(W.build_new_document("Camera Landscape")).decode("utf-8")
    new_ok = f'crs:ProcessVersion="{W.DEFAULT_PROCESS_VERSION}"' in skeleton

    # ② 已有值一律保留（那是用户 ACR 的事实，绝不被我们的默认值覆盖）
    existing = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""'
        ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
        ' crs:ProcessVersion="11.0" crs:Version="15.3" crs:HasSettings="True"/>\n'
        ' </rdf:RDF>\n</x:xmpmeta>\n'
    )
    kept_data, _, _, _ = W.render_xmp(
        {"Exposure2012": 0.5}, None, R.XmpDocument(existing.encode("utf-8"), source="<test>")
    )
    kept_text = kept_data.decode("utf-8")
    kept_ok = 'crs:ProcessVersion="11.0"' in kept_text and 'crs:ProcessVersion="15.4"' not in kept_text

    # ③ 没有该字段的包（实测 DJI DNG 内嵌包只有 crs:Version="7.0"）要补上 + 留下可见告警
    dji = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""'
        ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
        ' crs:Version="7.0" crs:HasSettings="True"/>\n'
        ' </rdf:RDF>\n</x:xmpmeta>\n'
    )
    filled_data, _, _, filled_warnings = W.render_xmp(
        {"Exposure2012": 0.5}, None, R.XmpDocument(dji.encode("utf-8"), source="<test>")
    )
    filled_text = filled_data.decode("utf-8")
    filled_ok = f'crs:ProcessVersion="{W.DEFAULT_PROCESS_VERSION}"' in filled_text
    told = any("处理版本" in w and "1 版" in w for w in filled_warnings)

    ok = new_ok and kept_ok and filled_ok and told
    record(
        "处理版本：新建写入 / 已有保留 / 缺字段补写（ACR 不再退化成 1 版）",
        ok,
        f"骨架含 {W.DEFAULT_PROCESS_VERSION}={new_ok}；已有 11.0 保留={kept_ok}；"
        f"缺字段补写={filled_ok}；有可见告警={told}",
    )


def test_stop_cancels_retries() -> None:
    """点停止后**不能再发请求**：重试不发、退避可打断、多图失败也不拆单重发。

    【用户真实反馈（GLM 时期）】他点了暂停，处理失败的那几张**还在继续重试**。
    根因有两处：① 客户端重试/退避与适配器的“错误回灌重发”“拆单重发”都不知道停止旗标
    （旗标以前只在提交循环里看）；② 多图失败后**拆成单图逐张重发**，一张失败会放大成
    好几发请求 —— 停止后这些请求照发，用户看到的就是“还在继续”。
    """
    import io
    from contextlib import redirect_stdout

    from acb.ai.adapter import ModelAdapter, PreviewItem
    from acb.ai.client import ApiClient, RequestBudget
    from acb.config import load_models_config
    from acb.errors import StopRequestedError

    spec = load_models_config().active_spec()
    posts: list[str] = []

    class _Resp:
        def __init__(self) -> None:
            self.status_code = 500
            self.text = "boom"
            self.headers: dict = {}

        def json(self) -> dict:
            return {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            posts.append(str(url))
            return _Resp()

    client = ApiClient(spec, "sk-test", RequestBudget(limit=10))
    client._session = lambda: _Session()  # type: ignore[method-assign]
    # 第一次发出请求之后 = 用户点了停止
    client.set_cancel_check(lambda: len(posts) >= 1)
    raised = False
    with redirect_stdout(io.StringIO()):
        try:
            client.chat([{"role": "user", "content": "hi"}], temperature=0.2)
        except StopRequestedError:
            raised = True
    client_ok = raised and len(posts) == 1

    # 适配器：StopRequestedError 必须原样上抛，**不能**被当成“这组图失败”进而拆单重发
    adapter = ModelAdapter(spec, "sk-test", RequestBudget(limit=10))
    split_calls: list[int] = []

    def _boom(system_prompt, user_text, items, schema, **kwargs):  # noqa: ANN001, ANN003 - 打桩
        # 签名必须与 _call_with_repair 对齐：以前这里写窄了 → TypeError 被当成“这组失败”
        # → 又走了拆单重发，用例反而看不出真实行为（踩过一次）。
        split_calls.append(len(items))
        raise StopRequestedError("已请求停止")

    adapter._call_with_repair = _boom  # type: ignore[method-assign]
    items = [
        PreviewItem(file_id=f"f{i}", filename=f"f{i}.CR3", path=Path(f"f{i}.CR3"),
                    preview_jpeg=b"")
        for i in range(3)
    ]
    adapter_raised = False
    with redirect_stdout(io.StringIO()):
        try:
            adapter.analyze_group(items, style_block="", user_prompt="")
        except StopRequestedError:
            adapter_raised = True
    adapter_ok = adapter_raised and split_calls == [3]     # 只发了一次（整组），没有拆成 3 次

    ok = client_ok and adapter_ok
    record(
        "停止后不再发送请求（重试/退避/拆单重发全部停下）",
        ok,
        f"客户端：发出 {len(posts)} 次后停下={client_ok}；"
        f"适配器：整组抛停止、未拆单={adapter_ok}（收到的组大小={split_calls}）",
    )


def test_stop_cancels_pending_groups() -> None:
    """停止后队列里**还没开始**的组不再发出，而且不能被记成失败。"""
    import shutil
    import tempfile
    import threading

    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.pipeline.job import JobState, build_scan_items, iter_raw_files
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.exiftool import ExiftoolRunner

    repo = Path(__file__).resolve().parent.parent
    raws = sample_material("*.CR3")[:2]
    if len(raws) < 2:
        record_missing_material("停止后不再发出未开始的组",
                                "需要 test_data/ 里至少 2 个 CR3")
        return

    cancel = threading.Event()
    started: list[list[str]] = []

    class _StoppingAdapter(OfflineAdapter):
        """第一组一开跑就“点停止”。"""

        def analyze_group(self, items, **kwargs):  # noqa: ANN001, ANN003
            started.append([item.filename for item in items])
            cancel.set()
            return super().analyze_group(items, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "photos"
        work.mkdir()
        for src in raws:
            shutil.copy2(src, work / src.name)

        callbacks = JobCallbacks(
            log=lambda message, level: None,
            progress=lambda done, total: None,
            stage=lambda text: None,
            item_status=lambda name, status: None,
            should_stop=lambda: cancel.is_set(),
        )
        result = run_output_mode(
            OutputOptions(sources=[work], run_photoshop=False, use_cache=False, workers=1),
            adapter=_StoppingAdapter(load_models_config().active_spec()),
            exiftool=ExiftoolRunner(),
            job_state=JobState(directory=Path(tmp) / "state"),
            callbacks=callbacks,
            cancel_event=cancel,
        )
        # 已经分析完成的那张**必须落盘**：那些请求已经花过钱，停止不该把结果丢掉。
        written = (work / (raws[0].stem + ".xmp")).is_file()
        ok = (
            result.stopped
            and len(started) == 1
            and result.failed == 0
            and result.done >= 1
            and written
        )
        detail = (
            f"stopped={result.stopped}；实际发起的组数={len(started)}（应为 1，队列里的第二个不再发）；"
            f"完成 {result.done} / 失败 {result.failed}（失败必须为 0）；"
            f"已分析的那张写出 XMP={written}"
        )
    record("停止后不再发出未开始的组、不记为失败、已分析的结果仍落盘", ok, detail)


def test_adaptive_concurrency_shrink() -> None:
    """遇到限流/超时要自动降档（4 → 2 → 1），且**只影响本次运行**。

    依据（用户 2026-09-24 原话）：「即使 …… deepseek-flash …… 用并发数 4 处理的时候
    仍然会有几张报错（3/24，2/18），更别说限制更严的 Kimi 和 Qwen，还有速度极慢的 GLM」。
    查证后的两部分：
      · 「2/18」那 2 张其实是 DNG 写回核对的误报（蒙版明明写进去了），已单独修；
      · 但并发 4 下 qwen 的 TPM 限流（18:08 那轮 15 张 429）与 GLM 的读超时确实更频繁。
    所以除了把默认并发降到 2，再让程序在收到 429/超时时自己降一档 ——
    而不是让用户手动来回试（他试了一整天）。
    """
    import io
    from contextlib import redirect_stdout

    import requests as _rq

    import acb.ai.client as C
    from acb.config import load_models_config

    cfg = load_models_config()
    preset = cfg.providers["glm"]
    spec = preset.spec_for("glm", preset.default_models[0])
    C.forget_endpoint_limit(spec.base_url, reason="自检开始：清掉记忆与闸门")

    class _Resp:
        def __init__(self, code: int, text: str) -> None:
            self.status_code, self.text = code, text
            self.headers: dict = {}

        def json(self) -> dict:
            return {"model": spec.model, "choices": [{"message": {"content": "{}"}}]}

    # ① 429（服务端**没写明**并发上限）→ 本次运行上限减半：4 → 2
    replies = [_Resp(429, '{"error":{"message":"Rate limit reached"}}'), _Resp(200, "{}")]

    class _S1:
        def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            return replies.pop(0) if replies else _Resp(200, "{}")

    client = C.ApiClient(spec, "k", C.RequestBudget(10))
    client._session = lambda: _S1()  # type: ignore[method-assign]
    client.set_concurrency_hint(4)
    before = C._gate_for(spec).limit
    with redirect_stdout(io.StringIO()):
        client.chat([{"role": "user", "content": "hi"}], temperature=0.2)
    after_429 = C._gate_for(spec).limit

    # ② 读超时 → 再减半（2 → 1，到下限就不再降）
    class _S2:
        def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            raise _rq.Timeout("slow")

    client2 = C.ApiClient(spec, "k", C.RequestBudget(10))
    client2._session = lambda: _S2()  # type: ignore[method-assign]
    with redirect_stdout(io.StringIO()):
        try:
            client2.chat([{"role": "user", "content": "hi"}], temperature=0.2)
        except Exception:  # noqa: BLE001 —— 这里只要“失败路径真的走过”
            pass
    after_timeout = C._gate_for(spec).limit

    # ③ 下一轮运行开始时必须**重置回基准**：降档只在轮内生效
    # （用户在一个程序会话里会连着跑好几轮；上一轮降的档带到下一轮就变成“越跑越慢”）。
    fresh = C.ApiClient(spec, "k", C.RequestBudget(10))
    fresh.set_concurrency_hint(4)
    after_reset = C._gate_for(spec).limit

    shrink_ok = (
        before == 4 and after_429 == 2 and after_timeout == 1 and after_reset == 4
    )

    # ④ 降档**不写记忆**：endpoint_capabilities.json 里不该出现这次学到的并发值
    learned = (C.load_endpoint_limits().get(spec.base_url) or {}).get("max_concurrency")
    memory_ok = learned is None or learned >= 2

    C.forget_endpoint_limit(spec.base_url, reason="自检结束：清掉测试闸门")
    ok = shrink_ok and memory_ok
    record(
        "限流/超时自动降档（4→2→1，轮内生效、不写记忆）",
        ok,
        f"初始 {before} → 429 后 {after_429} → 超时后 {after_timeout} → 下轮重置 {after_reset}；"
        f"记忆里的并发值={learned}（必须没写进去）",
    )


def test_icon_assets() -> None:
    """应用图标：素材必须在、格式对、打包清单带着它。

    图标是"没接上也不会报错"的典型（界面上只是没有图标），所以三件事都钉住：
      ① 圆角 PNG 与多尺寸 ICO 真的存在，且 PNG 的四角是透明的（= 圆角确实是做出来的，
         而不是把一张方图改个名）；
      ② ICO 里至少有 4 个尺寸且含 16 与 256（任务栏、资源管理器、大图标视图各取所需）；
      ③ `acb.spec` 真的会把 PNG 打进包、并把 ICO 作为 exe 图标 ——
         否则开发时能看到图标、打包后变成默认图标（典型的"只在成品里出现"的缺陷）。
    """
    from acb.paths import app_icon_ico_path, app_icon_png_path

    png = app_icon_png_path()
    ico = app_icon_ico_path()
    detail: list[str] = []
    ok = True

    if not png.is_file() or not ico.is_file():
        record(
            "应用图标：圆角 PNG + 多尺寸 ICO 存在（tools/make_icon.py 生成）",
            False,
            f"缺少 {png if not png.is_file() else ico}；跑一次 python tools/make_icon.py",
        )
        return

    from PIL import Image

    with Image.open(png) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        corners = [
            rgba.getpixel((0, 0)), rgba.getpixel((width - 1, 0)),
            rgba.getpixel((0, height - 1)), rgba.getpixel((width - 1, height - 1)),
        ]
        center_alpha = rgba.getpixel((width // 2, height // 2))[3]
    rounded = all(c[3] == 0 for c in corners) and center_alpha == 255
    ok = ok and rounded
    detail.append(f"{png.name} {width}×{height} 四角透明+中心不透明={rounded}")

    with Image.open(ico) as icon_file:
        sizes = sorted(icon_file.info.get("sizes", set()))
    multi = len(sizes) >= 4 and (16, 16) in sizes and (256, 256) in sizes
    ok = ok and multi
    detail.append(f"ICO 尺寸={sizes} 多尺寸且含 16/256={multi}")

    spec_text = (Path(__file__).resolve().parent.parent / "acb.spec").read_text(
        encoding="utf-8", errors="replace"
    )
    spec_ok = (
        'assets" / "icon"' in spec_text
        and '"app.ico"' in spec_text
        and 'assets" / "app.ico"' in spec_text
    )
    ok = ok and spec_ok
    detail.append(f"acb.spec 打包含图标 PNG + exe 图标={spec_ok}")

    record("应用图标：圆角 PNG + 多尺寸 ICO + 打包清单一致", ok, "；".join(detail))


def test_icon_from_source_framing() -> None:
    """图标源图处理：底色是**量出来的**（黑底也能裁）、裁完按比例留边、只缩不放。

    这条是补一个真踩过的坑：原来的裁边代码把"白"写死 —— 白底设计稿没问题，
    但黑底设计稿的留白根本裁不掉（黑 vs 白的差异处处都是 255，外接框=整张图），
    于是整张画布（实测 2567×2198、图案只占中间 712×736）被缩进图标，
    图案在 256px 上只剩一小点。所以现在两种底色都要验，且必须**量出来**。
    """
    from PIL import Image, ImageChops, ImageDraw
    from tools.make_icon import ART_RATIO, MASTER, from_source

    detail: list[str] = []
    ok = True

    def content_box(image: Image.Image) -> tuple[int, int, int, int]:
        """相对"补齐用的底色"取内容外接框（底色从圆角矩形内部取，避开圆角）。"""
        pad = image.getpixel((image.size[0] // 2, 2))[:3]
        ref = Image.new("RGB", image.size, pad)
        diff = ImageChops.difference(image.convert("RGB"), ref).convert("L")
        return diff.point(lambda v: 255 if v > 12 else 0).getbbox()

    with tempfile.TemporaryDirectory(prefix="acb_icon_src_") as tmp:
        tmp_dir = Path(tmp)

        def make_source(name: str, bg: tuple[int, int, int], canvas: tuple[int, int],
                        content: int, offset: tuple[int, int]) -> Path:
            """底色 canvas + 居中的 content×content 金块（非方形画布、留白不对称）。"""
            image = Image.new("RGB", canvas, bg)
            x, y = offset
            ImageDraw.Draw(image).ellipse((x, y, x + content - 1, y + content - 1),
                                          fill=(200, 160, 40))
            path = tmp_dir / name
            image.save(path)
            return path

        geometry: dict[str, tuple[int, tuple[int, int]]] = {}
        for name, bg in (("white.png", (255, 255, 255)), ("black.png", (0, 0, 0))):
            source = make_source(name, bg, canvas=(800, 600), content=200, offset=(300, 200))
            master = from_source(source)
            box = content_box(master)
            geometry[name] = (master.size[0], box)
            corners = [master.getpixel(p)[3] for p in
                       ((0, 0), (master.size[0] - 1, 0), (0, master.size[1] - 1),
                        (master.size[0] - 1, master.size[1] - 1))]
            centered = abs(box[0] - (master.size[0] - 1 - box[2])) <= 1 \
                and abs(box[1] - (master.size[1] - 1 - box[3])) <= 1
            gap = master.size[0] - (box[2] - box[0] + 1)
            detail.append(
                f"{name}：边长 {master.size[0]}（内容 {box[2] - box[0] + 1}，留白合计 {gap}）"
                f" 居中={centered} 四角透明={corners == [0, 0, 0, 0]}"
            )
            ok = ok and centered and corners == [0, 0, 0, 0]
            ok = ok and abs(box[2] - box[0] + 1 - 200) <= 2                 # 内容没被裁没被放大
            ok = ok and abs(master.size[0] - round(200 / ART_RATIO)) <= 2   # 边长按 art_ratio 给足

        same = geometry["white.png"] == geometry["black.png"]
        ok = ok and same
        detail.append(f"黑白两种底色算出的几何完全一致={same}（底色是量出来的，不是写死白色）")

        # 内容顶到边：art_ratio=1.0 时不该再留额外的边
        flush = from_source(tmp_dir / "black.png", art_ratio=1.0)
        flush_ok = abs(flush.size[0] - 200) <= 2
        ok = ok and flush_ok
        detail.append(f"art_ratio=1.0 时边长={flush.size[0]}（应为 200，内容顶到边）={flush_ok}")

        # 只缩不放：内容比 master 大时压到 master；比 master 小时保持原尺寸
        big = make_source("big.png", (0, 0, 0), canvas=(3000, 3000), content=1500,
                          offset=(700, 700))
        big_master = from_source(big)
        small_master = from_source(tmp_dir / "white.png")
        cap_ok = big_master.size[0] == MASTER
        ok = ok and cap_ok
        detail.append(
            f"大图压到上限 {big_master.size[0]}（={MASTER}）={cap_ok}；"
            f"小图保持原尺寸 {small_master.size[0]}（<{MASTER}，没被放大）="
            f"{small_master.size[0] < MASTER}"
        )
        ok = ok and small_master.size[0] < MASTER

    record("图标源图处理：黑底照样裁边、按 art_ratio 留边、只缩不放", ok, "；".join(detail))


def test_packaging_inventory_is_seed_only() -> None:
    """打包清单里的 styles 只允许是**两个内置种子**，不许整目录打包。

    为什么这是发布红线：
      · `ensure_seed_styles()` 会把打包 styles/ 里的**每个** json 释放到用户目录；
      · `is_seed_style()` 还会把它们当成"不可删的内置风格"。
    所以"把 styles 目录整个打进去"= 把开发者自己训练/测试的风格发给所有用户，
    而且用户还删不掉 —— 属于"只在成品里出现、事后很难补救"的缺陷。
    """
    import re

    from acb.constants import AUTOPILOT_STYLE_NAME, DEFAULT_STYLE_NAME

    root = Path(__file__).resolve().parent.parent
    spec_text = (root / "acb.spec").read_text(encoding="utf-8", errors="replace")

    match = re.search(r"SEED_STYLE_FILES\s*=\s*\(([^)]*)\)", spec_text)
    listed = tuple(re.findall(r'"([^"]+)"', match.group(1))) if match else ()
    expected = (f"{DEFAULT_STYLE_NAME}.json", f"{AUTOPILOT_STYLE_NAME}.json")
    same = listed == expected
    whole_dir = '(str(PROJECT_ROOT / "styles"), "styles")' in spec_text
    on_disk = all((root / "styles" / name).is_file() for name in listed) and bool(listed)
    # exiftool 必须是**整份**：只拷 exiftool.exe 会得到“跑不起来的壳”。
    # DNG 内嵌 XMP 读写全靠它，所以打包清单里必须有 exiftool_files。
    et_ok = "exiftool_files" in spec_text and "exiftool.exe" in spec_text
    # LICENSE 必须随包（许可条款第三条 a 款硬要求）
    license_ok = '"LICENSE"' in spec_text
    ok = same and not whole_dir and on_disk and et_ok and license_ok
    detail = (
        f"spec 里列出的种子={listed}；与 constants 一致={same}；"
        f"整目录打包 styles/={'是（必须改掉！）' if whole_dir else '否'}；"
        f"种子文件都在仓库里={on_disk}；spec 里声明捆绑整份 exiftool={et_ok}；"
        f"spec 里带上 LICENSE={license_ok}"
    )
    record("打包清单：styles 只含两个内置种子（不整目录打包）", ok, detail)


def test_release_tool_derives_version() -> None:
    """发布工具必须**从 acb.__version__ 派生版本号**，并且把守门检查留在流程里。

    两个"只有发布时才暴露"的事故：
      · 手写版本号 → 压缩包名、exe 属性、"关于"对话框三处对不上；
      · 顺手跳过守门 → 把开发者的照片/风格打进包（styles 曾经就是整目录打包的）。

    检查范围只钉**产物名**里的版本号（`AutoColorDiffusion-v<数字>…`）：
    文件里出现 "Version 1.0.0" 这种*别的*版本号（例如随包 LICENSE 的版本）是正常的，
    不能一刀切成"文件里不许出现 vX.Y.Z"—— 那会把正常文案也判成失败（真踩过：
    给构建信息加了一句"许可条款 v1.0.0"，结果这条用例红了）。
    """
    import re

    source = (Path(__file__).resolve().parent / "make_release.py").read_text(
        encoding="utf-8", errors="replace"
    )
    derived = "__version__" in source
    gated = "check_package.py" in source
    name_derived = re.search(r"AutoColorDiffusion-v\{__version__\}-win64\.zip", source) is not None
    hardcoded = re.findall(r"AutoColorDiffusion-v\d+\.\d+\.\d+", source)
    ok = derived and gated and name_derived and not hardcoded
    detail = (
        f"版本号取自 acb.__version__={derived}；产物名由它拼出={name_derived}；"
        f"流程里带守门检查={gated}；写死的产物名版本号={hardcoded or '无'}"
    )
    record("发布工具：版本号从 acb.__version__ 派生 + 守门检查在流程里", ok, detail)


def test_exiftool_invocation_prefers_perl() -> None:
    """打包内的 exiftool 必须走 **perl + exiftool.pl**，不能直接用那个启动器 exe。

    这是发布打包时（2026-09-25）实测踩到的：官方 Windows 包 = 启动器 exe + exiftool_files\\。
    整份放进 `_internal\\` 后，启动器报

        Can't locate strict.pm in @INC (@INC contains:) at ...exiftool_files\\exiftool.pl line 10

    而同一目录下用 `exiftool_files\\perl.exe exiftool_files\\exiftool.pl` 一切正常
    （源目录 / 带空格路径 / Z: 盘 / 打包后的 _internal 四种位置全 rc=0）。
    这条用例把"选哪条路"钉住，免得以后又退回那个跑不起来的启动器。
    """
    from acb.paths import exiftool_argv_prefix

    detail: list[str] = []
    ok = True
    with tempfile.TemporaryDirectory(prefix="acb_et_argv_") as tmp:
        root = Path(tmp)
        exe = root / "exiftool.exe"
        exe.write_bytes(b"fake launcher")

        # 1) 只有 exe（用户自己把单文件版放进来）：直接执行它
        solo = exiftool_argv_prefix(exe)
        solo_ok = solo == [str(exe)]
        detail.append(f"只有 exe 时={[Path(a).name for a in solo]}={solo_ok}")

        # 2) 完整布局：走 perl，并且显式带上 -I <lib>
        files = root / "exiftool_files"
        (files / "lib").mkdir(parents=True)
        (files / "perl.exe").write_bytes(b"fake perl")
        (files / "exiftool.pl").write_bytes(b"# fake script")
        full = exiftool_argv_prefix(exe)
        full_ok = (full[0] == str(files / "perl.exe") and "-I" in full
                   and full[full.index("-I") + 1] == str(files / "lib")
                   and full[-1] == str(files / "exiftool.pl"))
        detail.append(f"完整布局时={[Path(a).name for a in full]} 带 -I lib={full_ok}")

        # 3) 有 exiftool_files\\ 但缺 perl（残缺拷贝）：退回直接执行，不抛异常
        (files / "perl.exe").unlink()
        partial = exiftool_argv_prefix(exe)
        partial_ok = partial == [str(exe)]
        detail.append(f"残缺布局（缺 perl.exe）时退回直接执行={partial_ok}")

        ok = solo_ok and full_ok and partial_ok

    record("exiftool 调用方式：打包内优先 perl + exiftool.pl（启动器在产物里跑不起来）",
           ok, "；".join(detail))


def test_token_stats_parsing() -> None:
    """`tools/token_stats.py` 的解析必须认全真实日志里的四种行。

    【为什么要测它】这个工具是用户唯一能自己查"花了多少 token"的入口，而它刚被
    审查出四个会**静默给出错数字**的缺陷（都不是崩溃，是安静地少算/算错）：
      ① 训练轮的用量行前缀是「训练用量：」而不是「Token 用量：」→ 训练那一发永远统计不到；
      ② 模型名行是 `模型能力：备注名 [模型id] @ …`，用 \\S+ 抓会拿到备注名，
         备注名带空格时整条匹配不上（显示成 ?，多家的数字还会被混到一个键下）；
      ③ `训练样本 N 个，满足建议下限` 只在样本够时才打 → 样本不足那轮分母为 0，
         看着像"没花钱"；
      ④ 失败行来自两个不同 logger（acb.output_mode / acb.ui.worker），
         加模块名前缀条件会让"分析败"永远是 0；同一失败还会被记两行，必须按文件名去重。
    做法：造一份**假的**日志目录（格式照抄真实日志），跑真脚本、读它的输出。
    """
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "app-2026-01-01.log").write_text(
            "2026-01-01 10:00:00 [INFO   ] acb.ui.main: 开始输出模式（全量：已完成的文件也会重新请求 API）\n"
            "2026-01-01 10:00:02 [INFO   ] acb.ui.worker: 模型能力：通义千问 (Qwen) [qwen3-vl-flash] "
            "@ https://example.invalid/v1；视觉=支持\n"
            "2026-01-01 10:00:03 [INFO   ] acb.ai.client: 请求 http://x（第 1 次尝试）\n"
            "2026-01-01 10:00:04 [ERROR  ] acb.output_mode: a.CR3 写 XMP 失败（累计 2 次）：磁盘满\n"
            "2026-01-01 10:00:04 [ERROR  ] acb.ui.worker: a.CR3 写 XMP 失败（累计 2 次）：磁盘满\n"
            "2026-01-01 10:00:05 [ERROR  ] acb.ui.worker: b.CR3 分析失败（累计 1 次）：429\n"
            "2026-01-01 10:00:06 [INFO   ] acb.ui.main: 已清除 0 条失败记录\n"
            "2026-01-01 10:00:07 [INFO   ] acb.ui.main: 完成 3 张，失败 0 张，跳过 1 张（共 4 张）\n"
            "输出目录：D:\\out\n"
            "Token 用量：输入 30000 token，输出 3000 token　服务端确认模型：qwen3-vl-flash\n"
            "2026-01-01 11:00:00 [INFO   ] acb.ui.main: 开始训练模式\n"
            "2026-01-01 11:00:01 [INFO   ] acb.ui.worker: 样本不足：当前 4 个样本，建议至少 10 个\n"
            "训练用量：输入 8000 token，输出 900 token　服务端确认模型：qwen3-vl-flash\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(repo / "tools" / "token_stats.py"), "--log-dir", str(log_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=repo,
        )
        out = proc.stdout
        # 输出轮：分母 = 完成 3 + 写回失败去重后的 1（同一次失败被两个 logger 各写一行）
        output_ok = (
            "qwen3-vl-flash" in out          # ② 抓到的是方括号里的模型 id，不是备注名
            and "TEXT_MARKER_OUT" not in out  # 占位，永远为真
            and " 4 " in out
            and "10000" not in out            # 30000/3 不是 10,000 → 说明分母没被算成 3
            and "7500" in out                 # 30000 / 4 张
        )
        # 训练轮：④ 认「训练用量：」前缀；③ 认"当前 4 个样本"
        train_ok = "8000" in out and "2000" in out and "训练" in out
        failures_ok = "分析败" in out and "1" in out
        ok = proc.returncode == 0 and output_ok and train_ok and failures_ok
        record(
            "token 统计工具：认训练用量前缀 / 模型 id / 样本不足 / 失败列去重",
            ok,
            f"rc={proc.returncode} 输出轮分母=4（30000/4=7500）={output_ok} "
            f"训练轮统计到={train_ok} 失败列有值={failures_ok}",
        )


def test_train_mode_offline_end_to_end() -> None:
    """训练模式端到端（离线）：配对 → 解析 → 归纳 → 保存风格档案 + 用量记账。

    【为什么要补这条】用户 2026-09-24 要「从日志统计每张照片的 token」，
    结果发现：输出模式每轮都记了「Token 用量：输入 … 输出 …」，
    而**训练那一发从来不记** —— 它要把全部样本的参数明细 + 缩略图一次性发上去，
    是单次最贵的一发，却在日志里完全看不到花费。
    另外训练链路此前**一条用例都没有**，改坏了要等用户跑训练才暴露
    （实测踩过：预算给 1，一次读超时就把整次训练废掉，风格文件没保存）。
    """
    import io
    import os
    import shutil
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.pipeline.output_mode import JobCallbacks
    from acb.pipeline.train_mode import TrainOptions, discover_pairs, run_train_mode
    from acb.raw.exiftool import ExiftoolRunner

    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
        os.environ, {"APPDATA": str(Path(tmp) / "appdata")}
    ):
        # 拷到临时目录：训练只读样本，但风格文件会写进（被隔离的）数据目录
        work = Path(tmp) / "pairs"
        work.mkdir()
        samples = sample_material("*")
        if len(samples) < 3:
            record_missing_material("训练模式端到端（离线）",
                                    "需要 test_data/ 里的成对样本（RAW + XMP）")
            return
        for item in samples:
            if item.suffix.lower() in {".cr3", ".cr2", ".xmp", ".dng", ".jpg"}:
                shutil.copy2(item, work / item.name)

        pairs = discover_pairs([work], recursive=True)
        if len(pairs) < 3:
            record("训练模式端到端（离线）", False, f"样本对不足：{len(pairs)}")
            return

        msgs: list[tuple[int, str]] = []
        callbacks = JobCallbacks(
            log=lambda message, level: msgs.append((level, message)),
            progress=lambda done, total: None,
            stage=lambda text: None,
            item_status=lambda name, status: None,
            should_stop=lambda: False,
        )
        with redirect_stdout(io.StringIO()):
            result = run_train_mode(
                pairs,
                TrainOptions(use_cache=False, workers=2),
                adapter=OfflineAdapter(load_models_config().active_spec()),
                exiftool=ExiftoolRunner(),
                callbacks=callbacks,
                style_name="_自检-训练",
            )

        saved = bool(result.saved_path) and result.saved_path.is_file()
        profile_ok = bool(result.profile) and result.profile.get("sample_count") == result.sample_count
        usage_ok = bool(result.usage)
        logged = any("训练用量：" in message for _level, message in msgs)
        ok = saved and profile_ok and usage_ok and logged and result.failed == 0
        record(
            "训练模式端到端（离线）：配对/解析/归纳/保存 + 用量记账",
            ok,
            f"配对 {len(pairs)} → 样本 {result.sample_count}；失败 {result.failed}；"
            f"风格已保存={saved}；档案一致={profile_ok}；用量进日志={logged}（{result.usage[:50]}）",
        )


def test_local_range_two_layers() -> None:
    """局部参数范围 = 软范围（实测习惯）+ 硬范围（留余量）；局部颜色分级已收掉。

    用户 2026-09-23 的两条裁决：
      ① “留余量（软范围+硬范围双层）”—— 样本少时实测范围偏窄，不能当硬边界用；
      ② “局部颜色分级收掉”—— 它既像审美方向又是绝对色相/饱和度，容易被照抄。
    """
    from acb.ai import prompts as P
    from acb.ai import schema as S
    from acb.xmp import fields as F
    from acb.xmp import masks as M

    ok = True
    detail: list[str] = []

    style = {
        "name": "两层范围",
        "mask_usage": {"samples_with_masks": 8, "samples_total": 10, "mask_usage_ratio": 0.8},
        "local_param_ranges": {
            "LocalExposure2012": {"min": -0.5, "max": 0.25, "count": 4, "std": 0.2},
            "LocalHighlights2012": {"min": -100.0, "max": -100.0, "count": 2, "std": 0.0},
            "LocalTint": {"min": -6.0, "max": 7.0, "count": 2, "std": 3.5},
            "LocalColorGradeShadowHue": {"min": 200.0, "max": 230.0, "count": 5, "std": 5.0},
        },
    }
    layers = M.local_range_layers(style)

    # 1) 硬范围不窄于软范围，且不越出字段自己的面板范围
    bounds_ok = all(
        layer.hard_min <= layer.soft_min and layer.hard_max >= layer.soft_max
        and (spec := F.get_field(name)) is not None
        and spec.minimum is not None and spec.maximum is not None
        and layer.hard_min >= float(spec.minimum) and layer.hard_max <= float(spec.maximum)
        for name, layer in layers.items()
    )
    # 2) 余量按样本数走：同样跨度，2 张样本比 20 张样本宽
    few = M.expand_range("LocalTint", -6.0, 7.0, 2)
    many = M.expand_range("LocalTint", -6.0, 7.0, 20)
    margin_ok = few.hard_min < many.hard_min and few.hard_max > many.hard_max
    # 3) “所有样本同值”是合法信息：该字段必须保留，且硬范围被最小余量撑开
    highlights = layers.get("LocalHighlights2012")
    degenerate_ok = (
        highlights is not None
        and highlights.soft_min == highlights.soft_max == -100.0
        and highlights.hard_max > highlights.soft_max
    )
    ok = ok and bounds_ok and margin_ok and degenerate_ok
    detail.append(
        f"两层构造（硬⊇软且不越面板 {'✓' if bounds_ok else '✗'}；"
        f"样本少余量更宽 {'✓' if margin_ok else '✗'}；"
        f"同值字段不丢 {'✓' if degenerate_ok else '✗'}）"
    )

    # 4) 裁决行为：软内静默 / 略超软放行且记录 / 超硬夹回
    specs = [{"kind": "radial", "name": "主体", "rect": [0.3, 0.3, 0.7, 0.7],
              "local": {"LocalExposure2012": 0.25, "LocalTint": 9.0, "LocalHighlights2012": -130.0}}]
    kept, notes = M.clamp_local_to_ranges(specs, layers)
    local = kept[0]["local"] if kept else {}
    behavior_ok = (
        local.get("LocalExposure2012") == 0.25                       # 软上界内 → 原样
        and local.get("LocalTint") == 9.0                            # 略超软、在硬内 → 放行
        and local.get("LocalHighlights2012") == highlights.hard_min  # 超硬 → 夹回
        and sum("已放行" in n for n in notes) == 1
        and sum("已夹到" in n for n in notes) == 1
        and not any("LocalExposure2012" in n for n in notes)          # 软内的字段不该有噪音
    )
    ok = ok and behavior_ok
    detail.append(
        f"裁决（软内静默/略超放行/超硬夹回）{'✓' if behavior_ok else '✗'}"
        f"，放行 {sum('已放行' in n for n in notes)} 条、夹回 {sum('已夹到' in n for n in notes)} 条"
    )

    # 5) 局部颜色分级：清单、提示词、schema、风格文件四条路一起收掉
    item_local = S.build_masks_schema()["items"]["properties"]["local"]["properties"]
    grade_ok = (
        not any("ColorGrade" in name for name in M.AI_LOCAL_RANGES)
        and "颜色分级" not in M.ai_local_params_text()
        and not any("ColorGrade" in name for name in item_local)
        and "LocalColorGradeShadowHue" not in layers
    )
    parsed, warns = M.parse_specs([
        {"kind": "radial", "name": "主体", "rect": [0.3, 0.3, 0.7, 0.7],
         "local": {"LocalColorGradeShadowHue": 210, "LocalTint": 3}},
    ])
    grade_ok = grade_ok and len(parsed) == 1 and "LocalColorGradeShadowHue" not in parsed[0]["local"] \
        and any("LocalColorGradeShadowHue" in w for w in warns)
    ok = ok and grade_ok
    detail.append(f"局部颜色分级四条路一起收掉（清单/提示词/schema/风格文件+适配器丢弃）"
                  f"{'✓' if grade_ok else '✗'}")

    # 6) 提示词给的是**这套风格自己的软范围**，不是模块兜底表
    block = P.render_style_block(style, "两层范围")
    prompt_ok = "-0.5..0.25" in block and "尽量落在习惯范围内" in block
    ok = ok and prompt_ok
    detail.append(f"提示词用风格自己的软范围{'✓' if prompt_ok else '✗'}")

    record("局部范围两层（软+硬余量）+ 局部颜色分级收掉", ok, "；".join(detail))


def test_timeout_escalation_and_config_upgrade() -> None:
    """超时重试要放大读超时；旧配置里那个 60 秒必须能被自动修正。

    用户 2026-09-24 报错：调 GLM 时整批「请求超时」失败。实测证据：
      · 端点本身很快（/models 0.15s、纯文本 0.9s）；
      · glm-5.3-flash 是**强制思考**（官方《思考模式》：5.3/5.3-FLASH 不支持关闭思考），
        一次带图请求实测 51 秒才返回；
      · 而他的 models.yaml 里 request_timeout_s 还是旧种子写的 60 —— 新种子早已改成 180，
        但 models.yaml 对老用户永不覆盖，CAPABILITY_FIXES 里当时也没有这条 → 修正到不了他手上。
    所以这里守两件事：① 超时后的重试把读超时翻倍；② 升级表能把 60 改成 180。
    """
    import requests as _rq

    from acb import config as C
    from acb.ai.client import ApiClient, RequestBudget
    from acb.constants import TIMEOUT_ESCALATION_CEILING, TIMEOUT_ESCALATION_FACTOR
    from acb.config import load_models_config

    ok = True
    detail: list[str] = []

    # 1) 重试时的读超时必须放大（打桩 session：第一次超时，第二次成功）
    cfg = load_models_config()
    preset = cfg.providers["glm"]
    spec = preset.spec_for("glm", preset.default_models[0])
    seen: list[tuple] = []

    class _Resp:
        status_code = 200
        text = "{}"
        headers = {"content-type": "application/json"}

        def json(self) -> dict:
            return {"model": spec.model, "choices": [{"message": {"content": "{}"}}]}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            seen.append(timeout)
            if len(seen) == 1:
                raise _rq.exceptions.ReadTimeout("打桩：第一次超时")
            return _Resp()

    client = ApiClient(spec, "test-key", RequestBudget(10))
    client._session = lambda: _Session()  # type: ignore[method-assign]
    data = client.chat([{"role": "user", "content": "hi"}], temperature=0.2)
    base = float(spec.request_timeout_s)
    expect2 = min(base * TIMEOUT_ESCALATION_FACTOR, TIMEOUT_ESCALATION_CEILING)
    escalate_ok = (
        len(seen) == 2
        and seen[0][1] == base
        and seen[1][1] == expect2
        and expect2 > base
        and bool(data.get("choices"))
    )
    ok = ok and escalate_ok
    detail.append(f"超时重试放大（{base:g}s → {expect2:g}s）{'✓' if escalate_ok else '✗'}")

    # 2) 种子里的**看图**连接超时必须够大（否则思考模型必然超时）
    vision_providers = [name for name, p in cfg.providers.items() if p.supports_vision]
    slow_ok = all(float(cfg.providers[name].request_timeout_s) >= 120 for name in vision_providers)
    # 而且每一家都要有升级路径：老用户的 60 得能被自动改成 180（doubao 是新家，没有旧值）
    legacy = [name for name in vision_providers if name != "doubao"]
    upgrade_path_ok = all((name, "request_timeout_s") in C.CAPABILITY_FIXES for name in legacy)
    ok = ok and slow_ok and upgrade_path_ok
    detail.append(
        f"看图连接超时 ≥120s（{len(vision_providers)} 家）{'✓' if slow_ok else '✗'}；"
        f"全部有升级路径 {'✓' if upgrade_path_ok else '✗'}"
    )

    # 3) 升级表要能把"我们当年写的小值"改成新值，且不碰用户自己改过的值
    seed_entry = {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "request_timeout_s": 180,
        "max_output_tokens": 16384,
        "supports_vision": True,
    }
    legacy = {"glm": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                      "request_timeout_s": 60, "max_output_tokens": 8192, "supports_vision": True}}
    changes = C._refresh_capability_facts(legacy, {"glm": seed_entry})
    upgrade_ok = (
        legacy["glm"]["request_timeout_s"] == 180
        and legacy["glm"]["max_output_tokens"] == 16384
        and len(changes) >= 2
    )
    custom = {"glm": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                      "request_timeout_s": 90, "max_output_tokens": 8192, "supports_vision": True}}
    C._refresh_capability_facts(custom, {"glm": seed_entry})
    keep_ok = custom["glm"]["request_timeout_s"] == 90      # 90 不是我们写的值 → 不动
    ok = ok and upgrade_ok and keep_ok
    detail.append(
        f"升级表修正旧值 {'✓' if upgrade_ok else '✗'}；"
        f"用户自改值不动 {'✓' if keep_ok else '✗'}"
    )

    # 4) 放大倍数本身要有意义（不是 1 倍这种空动作）
    factor_ok = TIMEOUT_ESCALATION_FACTOR > 1 and TIMEOUT_ESCALATION_CEILING >= 180
    ok = ok and factor_ok
    detail.append(f"放大倍数与天花板合理 {'✓' if factor_ok else '✗'}")

    record("超时放大重试 + 旧配置超时自动修正（GLM 报错复盘）", ok, "；".join(detail))


def test_fixed_temperature_selfheal() -> None:
    """有些模型把 temperature 钉死：读服务端原话、记住、当场改用该值重发。

    实测 Kimi k2.6 只接受 0.6（`invalid temperature: only 0.6 is allowed for this model`）：
    输出模式发 0.6 能过，**训练模式发 0.2 每个文件都 400**。
    """
    import io as _io
    import contextlib as _ctx

    from acb.ai import client as C
    from acb.ai.client import ApiClient, RequestBudget
    from acb.config import load_models_config

    ok = True
    detail: list[str] = []

    real = ('{"error":{"message":"invalid temperature: only 0.6 is allowed for this model",'
            '"type":"invalid_request_error"}}')
    parse_ok = (
        C.fixed_temperature_from_error(real) == 0.6
        and C.fixed_temperature_from_error('{"error":{"message":"model not found"}}') is None
    )
    ok = ok and parse_ok
    detail.append(f"400 原文解析出固定温度 {'✓' if parse_ok else '✗'}")

    spec = load_models_config().providers["kimi"].spec_for("kimi", "kimi-k2.6")
    seen: list[float | None] = []

    class _Resp:
        def __init__(self, code: int, text: str) -> None:
            self.status_code, self.text = code, text
            self.headers: dict = {}

        def json(self) -> dict:
            return {"model": spec.model, "choices": [{"message": {"content": "{}"}}]}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
            seen.append(json.get("temperature"))
            if len(seen) == 1:
                return _Resp(400, real)
            return _Resp(200, "{}")

    client = ApiClient(spec, "k", RequestBudget(10))
    client._session = lambda: _Session()  # type: ignore[method-assign]
    with _ctx.redirect_stdout(_io.StringIO()):
        data = client.chat([{"role": "user", "content": "hi"}], temperature=0.2)
    heal_ok = (
        seen == [0.2, 0.6]
        and bool(data.get("choices"))
        and C.load_fixed_temperature(spec.base_url, spec.model) == 0.6
        and ApiClient(spec, "k", RequestBudget(1))._fixed_temperature == 0.6
    )
    ok = ok and heal_ok
    detail.append(
        f"自愈（发出 {seen} → 第二次改用 0.6 并成功、已记入记忆）{'✓' if heal_ok else '✗'}"
    )

    # 种子里直接写好也不能忘（新用户不用白撞一次；依据是服务端原文）
    override = (load_models_config().providers["kimi"].model_overrides or {}).get("kimi-k2.6")
    seed_ok = override is not None and override.temperature_train == 0.6 and override.temperature_output == 0.6
    ok = ok and seed_ok
    detail.append(f"种子里已写明只接受 0.6 {'✓' if seed_ok else '✗'}")

    record("固定 temperature 自愈（Kimi k2.6）", ok, "；".join(detail))


def test_rate_limit_gate() -> None:
    """429：读出服务端写明的并发上限、全局冷却、并且**真的**把并发压下去。

    用户 2026-09-24 报错：调 Kimi（kimi-k2.6）18 张只成功 1 张。日志里的 429 原文写着
    `request reached max organization concurrency: 1` —— 也就是"这个账户同时只能有
    1 个请求在飞"，而默认 4 个工人一起发，必然 3 个被拒、重试时又一起撞上去。
    """
    import threading
    import time as _time

    from acb.ai import client as C

    ok = True
    detail: list[str] = []

    class _Resp:
        def __init__(self, code: int, text: str, headers: dict | None = None) -> None:
            self.status_code, self.text = code, text
            self.headers = headers or {}

    kimi_body = (
        '{"error":{"message":"Your account org-cbf<ak-xyz> request reached max organization '
        'concurrency: 1, please try again after 1 seconds","type":"rate_limit_reached_error"}}'
    )
    kimi_rpm_body = (
        '{"error":{"message":"Your account org-cbf<ak-xyz> request reached organization '
        'max RPM: 3, please try again after 1 seconds","type":"rate_limit_reached_error"}}'
    )
    limit, rpm_from_body, cooldown = C._rate_limit_facts(_Resp(429, kimi_body, {"Retry-After": "1"}))
    _rpm_case_limit, rpm_limit, rpm_cooldown = C._rate_limit_facts(_Resp(429, kimi_rpm_body))
    generic_limit, generic_rpm, generic_cooldown = C._rate_limit_facts(
        _Resp(429, "Organization Rate limit exceeded, please try again after 1 seconds")
    )
    other_limit, other_rpm, other_cooldown = C._rate_limit_facts(_Resp(503, "upstream boom"))
    capped, _capped_rpm, _capped_cd = C._rate_limit_facts(
        _Resp(429, "concurrency: 3, please try again after 999 seconds")
    )
    parse_ok = (
        (limit, rpm_from_body, cooldown) == (1, None, 1.0)
        and (rpm_limit, rpm_cooldown) == (3, 1.0)                  # RPM 限制也要读出来
        and generic_limit is None and generic_rpm is None and generic_cooldown == 1.0
        and other_limit is None and other_rpm is None and other_cooldown is None
        and capped == 3                                            # 冷却时长有天花板
    )
    ok = ok and parse_ok
    detail.append(f"429 原文解析（并发 1 / RPM 3 / 无数字不猜 / 非429 不触发）{'✓' if parse_ok else '✗'}")

    # 闸门必须**真的**把同时在飞压到上限内（这是"只成功一张"的直接解法）
    gate = C._EndpointGate()
    gate.tighten(1)
    inflight = 0
    peak = 0
    lock = threading.Lock()

    def _worker() -> None:
        nonlocal inflight, peak
        gate.acquire(timeout=5.0)
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        _time.sleep(0.05)
        with lock:
            inflight -= 1
        gate.release()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    serialize_ok = peak == 1
    # 只收紧不放宽（先学到 1 个并发，后来说 4 个也不能放宽；RPM 同理），
    # 且同一个端点拿到同一个闸门实例
    from acb.config import load_models_config as _cfg_for_probe

    probe_spec = _cfg_for_probe().providers["kimi"].spec_for("kimi", "kimi-k2.6").model_copy(
        update={"base_url": "https://x.example/v1"}
    )
    widen_ok = (
        not gate.tighten(4, None)
        and gate.limit == 1
        and gate.tighten(None, 9) and not gate.tighten(None, 99)
        and gate.rpm == 9
        and C._gate_for(probe_spec) is C._gate_for(probe_spec)
    )
    ok = ok and serialize_ok and widen_ok
    detail.append(
        f"并发压到 1（实测峰值 {peak}）{'✓' if serialize_ok else '✗'}；"
        f"只收不放+按端点共享 {'✓' if widen_ok else '✗'}"
    )

    # RPM 闸门：2 次/窗口 内，第三次必须等满一个窗口（用短窗口测，不然要等 60 秒）
    rpm_gate = C._EndpointGate(rpm=2, window=0.3)
    t0 = _time.monotonic()
    for _ in range(3):
        rpm_gate.acquire(timeout=5.0)
        rpm_gate.release()
    elapsed = _time.monotonic() - t0
    rpm_ok = 0.25 <= elapsed <= 3.0
    ok = ok and rpm_ok
    detail.append(f"每分钟请求数闸门（2 次/窗口，第 3 次等 {elapsed:.2f}s）{'✓' if rpm_ok else '✗'}")

    # 学到的事实要落盘（下次启动直接按它跑，不再先白撞几个 429）
    base = "https://rate-limit-probe.example/v1"
    C.remember_endpoint_limit(base, concurrency=1, rpm=3, evidence=kimi_body[:120])
    facts = C.load_endpoint_limits().get(base) or {}
    persisted_ok = facts.get("max_concurrency") == 1 and facts.get("max_rpm") == 3
    ok = ok and persisted_ok
    detail.append(f"并发/RPM 上限落盘并在下次启动生效 {'✓' if persisted_ok else '✗'}")

    # 配置里**事先写明**的账户级限流（Kimi：官方《充值与限速》的档位值）。
    # 2026-09-24 起出厂默认是 **Tier1**（并发 15 / RPM 100）—— 理由与来源见
    # constants.KIMI_TIER_LIMITS 与 config._DEFAULT_FIXES 的注释。
    # 用户要求：调 Kimi 时要按账户档位限流；换回别的模型要能"改回来"（互不牵连）。
    from acb.config import load_models_config as _load_cfg

    from acb.constants import KIMI_TIER_LIMITS as _TIERS

    cfg = _load_cfg()
    kimi_spec = cfg.providers["kimi"].spec_for("kimi", "kimi-k2.6")
    other_spec = cfg.providers["deepseek"].spec_for("deepseek", "deepseek-flash")
    C._GATES.clear()          # 测试专用：清掉进程内闸门，从"干净启动"的状态看
    C._LEARNED_LIMITS = None
    kimi_gate = C._gate_for(kimi_spec)
    other_gate = C._gate_for(other_spec)
    preset_ok = (
        (cfg.providers["kimi"].max_concurrency, cfg.providers["kimi"].max_rpm) == _TIERS["tier1"]
        and all(
            p.max_concurrency is None and p.max_rpm is None
            for name, p in cfg.providers.items() if name != "kimi"
        )
        # 不用先撞 429 才学会
        and (kimi_gate.limit, kimi_gate.rpm) == _TIERS["tier1"]
        and other_gate.limit is None and other_gate.rpm is None   # 换别的连接就"改回来"
    )
    ok = ok and preset_ok
    detail.append(
        f"预设限流（Kimi {kimi_gate.limit}/{kimi_gate.rpm}，其他连接不受影响）"
        f"{'✓' if preset_ok else '✗'}"
    )

    # 配置值与学到的值取**更严**的一个（永远不放宽）
    C.remember_endpoint_limit("https://preset-probe.example/v1", concurrency=5, rpm=9)
    C._GATES.clear()
    C._LEARNED_LIMITS = None
    loose_spec = kimi_spec.model_copy(
        update={"base_url": "https://preset-probe.example/v1", "max_concurrency": 2, "max_rpm": None}
    )
    mixed_gate = C._gate_for(loose_spec)
    mixed_ok = mixed_gate.limit == 2 and mixed_gate.rpm == 9   # 并发取配置的 2，RPM 取学到的 9
    ok = ok and mixed_ok
    detail.append(f"配置与记忆取更严的一个（并发 {mixed_gate.limit} / RPM {mixed_gate.rpm}）{'✓' if mixed_ok else '✗'}")

    record("限流治理（429 并发+RPM 上限 + 全局冷却）", ok, "；".join(detail))


def test_kimi_tier_switch() -> None:
    """Kimi 账户档位：出厂 Tier1、老配置自动改档、界面那一键写盘、重启仍生效。

    【为什么需要这条】官方按**累计充值额**分档限速（Tier0 并发 1 / RPM 3；
    Tier1 并发 15 / RPM 100），但**没有任何接口能查档位** —— 实测
    /v1/users/me/balance 只回三个余额字段，对话响应里也没有 ratelimit 头。
    所以只能：出厂默认按 Tier1（否则充过钱的用户白等：实测 Tier0 下 4 并发出
    18 张只成 1 张），再在界面上给未充值的用户一个一键切 Tier0 的入口。
    这条用例钉住三件事：
      ① 种子 = Tier1；
      ② 老机器上写着 1/3 的文件会被自动改档到 15/100（否则默认永远到不了已装机）；
      ③ 界面写的是**连接文件**，且不会被任何自动机制改回去（用户的选择要保得住）。

    写路径在**子进程 + 再隔离一层 %APPDATA%** 里跑：它会改 models.yaml /
    新建 connections.yaml，而本套自检后面的用例还要用当前那份配置。
    """
    import os
    import subprocess
    import tempfile

    from acb import config as C
    from acb.constants import KIMI_TIER_LIMITS

    t0, t1 = KIMI_TIER_LIMITS["tier0"], KIMI_TIER_LIMITS["tier1"]
    ok = True
    detail: list[str] = []

    # ① 出厂种子（只读当前隔离配置，不动它）
    cfg = C.load_models_config()
    kimi = cfg.providers["kimi"]
    seed_ok = (kimi.max_concurrency, kimi.max_rpm) == t1
    ok = ok and seed_ok
    detail.append(
        f"出厂默认 = Tier1（并发 {kimi.max_concurrency} / 每分钟 {kimi.max_rpm}）"
        f"{'✓' if seed_ok else '✗'}"
    )

    # ②③ 写路径：子进程里从零开始走一遍完整生命周期
    #    ⚠ 子进程的 stdout 是管道，Windows 上默认按 GBK 编码 → 打印「✓」会抛
    #      UnicodeEncodeError 把整条用例判失败（实测踩到）。所以先切 UTF-8。
    script = """
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import yaml
from acb import config as C
from acb.ai import client as CL
from acb.config import ProviderSpec, ModelConfigError
from acb.constants import KIMI_TIER_LIMITS as T
t0, t1 = T["tier0"], T["tier1"]


def eff():
    spec = C.load_models_config().models["kimi::kimi-k2.6"]
    return spec.max_concurrency, spec.max_rpm


assert eff() == t1, ("全新安装应为 Tier1", eff())

models = C.user_config_path()
data = yaml.safe_load(models.read_text(encoding="utf-8"))
data["providers"]["kimi"]["max_concurrency"] = 1
data["providers"]["kimi"]["max_rpm"] = 3
models.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
assert eff() == t1, ("老配置(1/3)应被自动改档", eff())
after = yaml.safe_load(models.read_text(encoding="utf-8"))["providers"]["kimi"]
assert (after["max_concurrency"], after["max_rpm"]) == t1, after
print("改档 models.yaml:", after["max_concurrency"], after["max_rpm"])

presets = {"kimi": ProviderSpec.model_validate(
    yaml.safe_load(models.read_text(encoding="utf-8"))["providers"]["kimi"])}
path = C.set_endpoint_limits(presets, "kimi", concurrency=t0[0], rpm=t0[1])
assert path.is_file(), path
assert eff() == t0, ("界面那一键应写成 Tier0", eff())
assert eff() == t0, "再加载一次仍是 Tier0（没有任何自动机制改回去）"

C.set_endpoint_limits(presets, "kimi", concurrency=t1[0], rpm=t1[1])
assert eff() == t1, ("改回 Tier1", eff())

# 学习值必须能被"用户的选择"作废：否则记忆与配置取严，用户选完 Tier1 仍被压到 1
base = "https://api.moonshot.cn/v1"
CL.remember_endpoint_limit(base, concurrency=1, rpm=3, evidence="request reached max organization concurrency: 1")
CL.remember_fixed_temperature(base, "kimi-k2.6", 0.6, evidence="only 0.6 is allowed")
CL._GATES.clear(); CL._LEARNED_LIMITS = None
spec = C.load_models_config().models["kimi::kimi-k2.6"]
gate = CL._gate_for(spec)
assert (gate.limit, gate.rpm) == (1, 3), ("学习值应生效", gate.limit, gate.rpm)
assert CL.forget_endpoint_limit(base, reason="界面选择（测试）") is True
gate2 = CL._gate_for(spec)
assert (gate2.limit, gate2.rpm) == t1, ("作废后应按配置跑", gate2.limit, gate2.rpm)
after = yaml.safe_load(CL.endpoint_caps_path().read_text(encoding="utf-8"))[base]
assert after.get("model_temperature", {}).get("kimi-k2.6") == 0.6, ("温度记忆不能连坐删掉", after)
assert not CL.load_endpoint_limits().get(base), "限流值应已清掉"
print("作废学习值 → 闸门立即回到配置档位:", gate2.limit, gate2.rpm)

# 过期记忆不再采用（档位会随充值升级，记太久会把已升级的账户永久限在过去那档）
from datetime import datetime as _dt, timedelta as _td
CL.remember_endpoint_limit(base, concurrency=1, rpm=3)
caps = yaml.safe_load(CL.endpoint_caps_path().read_text(encoding="utf-8"))
caps[base]["last_seen"] = (_dt.now() - _td(days=8)).isoformat(timespec="seconds")
CL.endpoint_caps_path().write_text(
    __import__("json").dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
assert not CL.load_endpoint_limits().get(base), "超过 7 天的记录应被忽略"
CL.remember_endpoint_limit(base, concurrency=2, rpm=5)
assert CL.load_endpoint_limits().get(base) == {"max_concurrency": 2, "max_rpm": 5}, "新鲜的仍要采用"
print("过期丢弃 / 新鲜保留 ✓")

try:
    C.set_endpoint_limits(presets, "没有这条连接", concurrency=1, rpm=3)
except ModelConfigError:
    pass
else:
    raise AssertionError("未知连接应明确报错，而不是静默无操作")

print("子进程路径全部通过")
"""
    with tempfile.TemporaryDirectory(prefix="acb_tier_") as tmp:
        env = {**os.environ, "APPDATA": tmp}
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    write_ok = proc.returncode == 0
    ok = ok and write_ok
    detail.append(
        f"老配置自动改档 + 一键切 Tier0/回 Tier1 + 作废旧学习值 + 过期记忆不再采用 "
        f"{'✓' if write_ok else '✗'}"
        + ("" if write_ok else f"\n        {proc.stdout[-600:]}{proc.stderr[-600:]}")
    )

    record("Kimi 档位（出厂 Tier1 / 旧文件自动改档 / 一键切换可持久）", ok, "；".join(detail))


def test_permanent_skip_is_reported() -> None:
    """被「永久跳过」的文件必须**点名 + 说明原因 + 给出恢复路径**，且必须进日志系统。

    【这条用例是被一次真实投诉换来的】用户：「我又进行了一轮测试，发现 DNG 文件
    依然没有被处理」。查下来的事实：那 3 个 DNG 在更早一轮里因为 exiftool 写回失败
    被记成永久跳过（键 = 大小|mtime|文件名，**与目录无关**，所以换个文件夹也认得出），
    于是后来**任何模式都不会再碰它们**；而当时的输出只有一句
    「跳过 3 张（共 21 张）」——没有名字、没有原因、没有出路。

    另一个同样真实的缺口：管线消息以前只走 UI 的日志面板（sig_log），
    **文件日志里一个字都没有**，所以用户回看日志时连"跳过"都找不到。

    这条用例钉住三件事：
      ① `OutputResult.permanent_skips` / `summary_text()` 里有文件名与恢复路径；
      ② 一条 WARNING 说明被跳过的是谁、上次为什么失败、怎么重试；
      ③ 这条 WARNING 走的是 logging（⇒ 会落进文件日志），不是只发界面信号。
    """
    import logging
    import shutil
    import tempfile

    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.pipeline.job import (
        MAX_ATTEMPTS_PER_FILE,
        JobState,
        build_scan_items,
        iter_raw_files,
    )
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.exiftool import ExiftoolRunner

    repo = Path(__file__).resolve().parent.parent
    cr3 = next(iter(sample_material("*.CR3")), None)
    dng = next(iter(sample_material("*.DNG")), None)
    if cr3 is None or dng is None:
        record_missing_material("永久跳过必须点名并给出恢复路径",
                                "需要 test_data/ 里同时有 CR3 与 DNG")
        return

    captured: list[tuple[int, str]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            captured.append((record.levelno, record.getMessage()))

    ok = True
    detail: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "photos"
        work.mkdir()
        for src in (cr3, dng):
            shutil.copy2(src, work / src.name)
            sidecar = src.with_suffix(".xmp")
            if sidecar.is_file():
                shutil.copy2(sidecar, work / sidecar.name)

        state = JobState(directory=Path(tmp) / "state")
        items = build_scan_items(iter_raw_files([work]))
        skip_item = next(i for i in items if i.path.name == cr3.name)
        # 造出"历史失败达上限"的状态（不联网、不碰真实照片）
        for _ in range(MAX_ATTEMPTS_PER_FILE):
            state.mark_attempt(skip_item)
            state.mark_failed(skip_item, "打桩：写回内嵌 XMP 失败（Maker notes could not be parsed）")
        seeded = state.is_permanently_skipped(skip_item.key)

        seen_items: list[tuple[str, str]] = []
        callbacks = JobCallbacks(
            log=lambda message, level: None,
            progress=lambda done, total: None,
            stage=lambda text: None,
            item_status=lambda name, status: seen_items.append((name, status)),
            should_stop=lambda: False,
        )
        handler = _Capture(level=logging.WARNING)
        logging.getLogger().addHandler(handler)
        try:
            result = run_output_mode(
                OutputOptions(sources=[work], run_photoshop=False, use_cache=False, workers=1),
                adapter=OfflineAdapter(load_models_config().active_spec()),
                exiftool=ExiftoolRunner(),
                job_state=state,
                callbacks=callbacks,
            )
        finally:
            logging.getLogger().removeHandler(handler)

        summary = result.summary_text()
        named = result.permanent_skips == [cr3.name] and cr3.name in summary
        remedy = "清除失败记录" in summary
        table_named = any(name == cr3.name and "跳过" in status for name, status in seen_items)
        notice = next((msg for level, msg in captured
                       if level >= logging.WARNING and "永久跳过" in msg), "")
        notice_ok = (
            cr3.name in notice                      # 点名
            and "Maker notes" in notice             # 上次失败原因
            and "清除失败记录" in notice             # 恢复路径
        )
        ok = ok and seeded and named and remedy and table_named and notice_ok
        detail.append(
            f"账本已置永久跳过={seeded}；结果点名+恢复路径={named and remedy}；"
            f"表格状态列={table_named}；WARNING 走 logging（⇒ 落盘）={notice_ok}"
        )
        if notice_ok:
            detail.append("原文：" + notice.replace("\n", " ")[:220])

    record("永久跳过必须点名、给原因与恢复路径（并进日志系统）", ok, "；".join(detail))


def test_quota_429_is_not_rate_limit() -> None:
    """429 必须分成两类：账户配额/额度（不重试）vs 调用频率限流（照旧重试）。

    【为什么这条非加不可 —— 用户真实反馈】用户：「我又跑了一轮，有 7 张失败」。
    日志里 7 张全是 429，正文是阿里云百炼的
    `You exceeded your current quota, please check your plan and billing details.`
    —— 这是**账户配额/额度**类错误，不是并发/频率限流。而当时程序：
      · 先退避 1 秒重试一次（配额不会因为等 1 秒恢复 → 白花 1 次请求与预算）；
      · 提示写成「限流提示：…把并发工人数调低或换更高档位的套餐」（方向错了）。

    阿里云百炼官方《错误码》（2026-09-22 版）把它们分在两个条目：
      · `429-Throttling.RateQuota/LimitRequests…` —— RPS/RPM 频控 → 降频后重试；
      · `429-Throttling.AllocationQuota/insufficient_quota` —— Token 配额（TPS/TPM）
        或免费额度/账单额度用尽 → **重试无效**，要等窗口滑过或去控制台处理额度。
    这条用例钉住：识别靠**服务端原文**（不靠状态码猜）、配额类只发一次请求、
    不写端点限流记忆、并且绝不误伤 Kimi 那种 `max organization concurrency: 1`。
    """
    import logging
    import tempfile

    from acb.ai.client import (
        _GATES,
        ApiClient,
        _quota_exhausted,
        is_account_level_error,
        load_endpoint_limits,
        RequestBudget,
    )
    from acb.config import load_models_config
    from acb.errors import QuotaExceededError

    quota = {
        "百炼实测原文（用户这次碰到的）":
            '{"error":{"message":"You exceeded your current quota, please check your plan '
            'and billing details. For details, see: https://help.aliyun.com/zh/model-studio/"}}',
        "百炼官方文案":
            '{"error":{"message":"Allocated quota exceeded, please increase your quota limit."}}',
        "免费额度用尽":
            '{"error":{"message":"Free allocated quota exceeded."}}',
        "免费额度用完即停":
            '{"error":{"code":"AllocationQuota.FreeTierOnly","message":"The free tier of the '
            'model has been exhausted."}}',
        "预算管理上限":
            '{"error":{"message":"The budget configured in Budget Management has been '
            'exhausted."}}',
        "账户欠费":
            '{"error":{"code":"Arrearage","message":"Access denied, please make sure your '
            'account is in good standing."}}',
    }
    rate = {
        "Kimi 并发":
            '{"error":{"message":"Your account org-x request reached max organization '
            'concurrency: 1, please try again after 1 seconds"}}',
        "Kimi RPM":
            '{"error":{"message":"Your account org-x request reached organization max RPM: 3, '
            'please try again after 1 seconds"}}',
        "通用限流文案":
            '{"error":{"message":"Organization Rate limit exceeded, please try again after 1 '
            'seconds"}}',
        "上游 500": "upstream boom",
        "超时文案": "Read timed out. (read timeout=180)",
    }
    classified_ok = all(_quota_exhausted(body) for body in quota.values()) and not any(
        _quota_exhausted(body) for body in rate.values()
    )

    ok = classified_ok
    detail: list[str] = [
        f"分类：配额类 {len(quota)} 条全中、限流/超时/其它 {len(rate)} 条全不中"
        f"={'✓' if classified_ok else '✗'}"
    ]

    class _Resp:
        def __init__(self, body: str) -> None:
            self.status_code, self.text, self.headers = 429, body, {}

        def json(self):  # noqa: D102 - 打桩
            return {}

    class _Session:
        def __init__(self, body: str) -> None:
            self.body, self.calls = body, 0

        def post(self, *a, **k):  # noqa: D102 - 打桩
            self.calls += 1
            return _Resp(self.body)

    with tempfile.TemporaryDirectory() as tmp:
        # ① 配额类：只发一次、不写限流记忆、提示里给对的方向
        spec = load_models_config().providers["qwen"].spec_for("qwen", "qwen3-vl-plus").model_copy(
            update={"base_url": "https://quota-smoke.example/v1"}
        )
        client = ApiClient(spec, "sk-test", RequestBudget(limit=4))
        session = _Session(list(quota.values())[0])
        client._session = lambda *a, **k: session
        _GATES.clear()
        message = ""
        try:
            client.chat([{"role": "user", "content": "x"}], temperature=0.6)
        except QuotaExceededError as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - 打桩环境，要把真实异常类型报出来
            message = f"（抛出的不是 QuotaExceededError 而是 {type(exc).__name__}: {exc}）"
        quota_ok = (
            session.calls == 1                       # 不再重试
            and client.budget.used == 1              # 预算也只花 1
            and not load_endpoint_limits().get(spec.base_url)   # 不把额度问题学成"并发 1"
            # 提示必须同时给出三条出路（等窗口 / TPM 靠降并发 / 真额度问题去控制台），
            # 只给“降并发”会把额度问题引到错方向。
            and "配额" in message and "只跑失败" in message
            and "TPM" in message and "控制台" in message
        )
        ok = ok and quota_ok
        detail.append(
            f"配额类 429：请求 {session.calls} 次（期望 1）、预算 {client.budget.used}、"
            f"限流记忆未写入={'✓' if not load_endpoint_limits().get(spec.base_url) else '✗'}、"
            f"提示方向正确={'✓' if quota_ok else '✗'}"
        )

        # ② 对照组：Kimi 那种并发限流仍然重试并学习（不能被新分类误伤）
        spec_k = load_models_config().providers["kimi"].spec_for("kimi", "kimi-k2.6").model_copy(
            update={"base_url": "https://rate-smoke.example/v1"}
        )
        client_k = ApiClient(spec_k, "sk-test", RequestBudget(limit=4))
        session_k = _Session(rate["Kimi 并发"])
        client_k._session = lambda *a, **k: session_k
        _GATES.clear()
        learned: dict = {}
        try:
            client_k.chat([{"role": "user", "content": "x"}], temperature=0.6)
        except Exception:  # noqa: BLE001 - 这里必然会失败，只看副作用
            learned = load_endpoint_limits().get(spec_k.base_url) or {}
        rate_ok = session_k.calls == 2 and learned.get("max_concurrency") == 1
        ok = ok and rate_ok
        detail.append(
            f"对照（频率限流）：请求 {session_k.calls} 次（期望 2）、学到的并发 "
            f"{learned.get('max_concurrency')}（期望 1）{'✓' if rate_ok else '✗'}"
        )

        # ③ 适配器层：配额类错误**不拆单重发**，但每张图都要有一条结果
        #    （少一条结果 = 那张图静默消失，正是刚修完的那类问题）
        from acb.ai.adapter import ModelAdapter, PreviewItem

        adapter = ModelAdapter(spec, "sk-test", RequestBudget(limit=10))
        calls = {"n": 0}

        def _boom(*a, **k):  # noqa: ANN002, ANN003 - 打桩
            calls["n"] += 1
            raise QuotaExceededError("API 返回 429（账户配额/额度，不是并发/频率限制）")

        adapter.client.chat = _boom
        items = [
            PreviewItem(path=Path(f"{i}.CR3"), preview_jpeg=b"\xff\xd8", file_id=f"f{i}",
                        filename=f"{i}.CR3", preview_source="preview")
            for i in range(3)
        ]
        results = adapter.analyze_group(items, style_block="", user_prompt="")
        adapter_ok = calls["n"] == 1 and len(results) == 3 and all(r.error for r in results)
        ok = ok and adapter_ok
        detail.append(
            f"适配器层：整组 3 张 → 请求 {calls['n']} 次（期望 1，不拆单）、"
            f"结果 {len(results)} 条（期望 3，不静默丢）{'✓' if adapter_ok else '✗'}"
        )

        # ④ 账户级错误**不得**把文件推向“永久跳过”：
        #    否则用户充值后这些图仍然不处理，还得再点一次「清除失败记录」（很难发现的陷阱）。
        from acb.pipeline.job import MAX_ATTEMPTS_PER_FILE, JobState, ScanItem

        state = JobState(directory=Path(tmp) / "state-quota")
        account_item = ScanItem(path=Path("acct.CR3"), key="acct", size=1, mtime_ns=0)
        for _ in range(MAX_ATTEMPTS_PER_FILE + 2):
            state.mark_failed(
                account_item,
                "API 返回 429（账户配额/额度，不是并发/频率限制）：You exceeded your current quota…",
                counts_toward_skip=not is_account_level_error("You exceeded your current quota"),
            )
        pending_account, _ = state.select_pending([account_item], resume=True, only_failed=True)
        account_ok = (
            not state.is_permanently_skipped(account_item.key)   # 没被永久跳过
            and [i.key for i in pending_account] == ["acct"]      # 「只跑失败」还能捞回来
        )
        # 对照组：与文件本身有关的失败，照旧会永久跳过（不能把逃生舱改没了）
        normal_item = ScanItem(path=Path("bad.CR3"), key="bad", size=1, mtime_ns=0)
        for _ in range(MAX_ATTEMPTS_PER_FILE):
            state.mark_failed(normal_item, "打桩：写盘失败（与账户无关）")
        normal_ok = state.is_permanently_skipped(normal_item.key)
        ok = ok and account_ok and normal_ok
        detail.append(
            f"账户级失败不永久跳过={account_ok}（「只跑失败」仍可捞回）；"
            f"普通失败照旧永久跳过={normal_ok}"
        )

    record("429 分类：账户配额/额度（不重试）vs 频率限流（照旧重试）", ok, "；".join(detail))


def test_regression_no_copy_and_guards() -> None:
    """步骤 6 回归：同风格跑不同图不得同值；极端必须命中规则才放行。

    对应用户最核心的那条反馈：同一风格跑一批图，写出的侧车里同名字段值
    全部等于训练统计的中位数/均值 —— 把"统计值"当成了"每张图的目标值"。
    提示词里写了"禁止照抄"，但那是软约束，所以这里用可执行的方式钉住。
    """
    from acb.ai import guardrails as G
    from acb.ai.offline import MOCK_PARAMS
    from acb.pipeline import style_profile as SP

    ok = True
    detail: list[str] = []

    style = {"param_ranges": {"Exposure2012": {"median": 0.3, "mean": 0.3},
                              "Saturation": {"median": -8, "mean": -8}},
             "text_rules": [], "scene_rules": [], "local_param_ranges": {}}

    identical = [{"Exposure2012": 0.3, "Saturation": -8} for _ in range(6)]
    copied = G.detect_copying(identical, style)
    copy_flagged = len(copied) == 2

    varied = [{"Exposure2012": 0.1 * i, "Saturation": -8 + i} for i in range(1, 7)]
    varied_clean = not G.detect_copying(varied, style)

    # 离线调试的假数据整批一模一样 —— 也必须能识别出来
    offline_batch = [dict(MOCK_PARAMS), dict(MOCK_PARAMS)]
    offline_style = SP.build_style_profile(name="离线", snapshots=[], model_output={})
    exposure = float(MOCK_PARAMS.get("Exposure2012", 0))
    offline_style["param_ranges"] = {"Exposure2012": {"median": exposure, "mean": exposure}}
    offline_copied = G.detect_copying(offline_batch, offline_style)

    ok = ok and copy_flagged and varied_clean
    detail.append(
        f"照抄检测（一模一样必须报={'✓' if copy_flagged else '✗'}，"
        f"按图变化不误报={'✓' if varied_clean else '✗'}，"
        f"离线假数据可识别={'✓' if offline_copied else '—'}）"
    )

    # 极端值：有规则（文字规则或场景规则）才放行
    extreme = {"Highlights2012": -70, "Saturation": 55}
    _no, clamped = G.clamp_extremes(extreme, {"text_rules": [], "scene_rules": []})
    with_rule, _k = G.clamp_extremes(
        extreme, {"text_rules": ["高光一律压到 -80 以下以保天空"], "scene_rules": []}
    )
    _sr, scene_kept = G.clamp_extremes(
        extreme,
        {"text_rules": [],
         "scene_rules": [{"scene": "风景（日出日落）", "text": "【风景（日出日落）】习惯：高光 中位 -80"}],
         "param_ranges": {"Highlights2012": {"count": 12, "std": 10.0}}},
    )
    # 注意：scene_kept 是「说明文字」列表，成员判断要用子串（不是 in 列表）
    scene_ok = (
        not any("Highlights2012" in text for text in scene_kept)   # 场景规则提到「高光」→ 放行
        and any("Saturation" in text for text in scene_kept)       # 没提到的字段照样夹
    )
    guard_ok = (
        len(clamped) == 2
        and with_rule["Highlights2012"] == -70
        and scene_ok
    )
    ok = ok and guard_ok
    detail.append(
        f"极端需规则（无规则夹 {len(clamped)} 条，文字规则放行="
        f"{'✓' if with_rule['Highlights2012'] == -70 else '✗'}，"
        f"场景规则放行={'✓' if scene_ok else '✗'}）"
    )

    _p, quiet = G.apply_group_budgets({"Saturation": 10, "Vibrance": 12})
    ok = ok and not quiet
    detail.append(f"预算不误伤 {'✓' if not quiet else '✗'}")

    record("回归：同风格不同图不得同值 + 极端需规则", ok, "；".join(detail))


def test_mask_structure_roundtrip() -> None:
    """步骤 6 回归：写出的蒙版 XMP 必须能被自己的解析器读回；门禁口径 100%。"""
    import tempfile
    import xml.etree.ElementTree as ET

    from acb.xmp import masks as M
    from acb.xmp import reader as R
    from acb.xmp import writer as W

    ok = True
    detail: list[str] = []

    specs, _warn = M.parse_specs([
        {"kind": "linear", "target": "天空", "zero": [0.5, 0.30], "full": [0.5, 0.05],
         "local": {"LocalExposure2012": -0.9, "LocalHighlights2012": -40}},
        {"kind": "radial", "target": "主体", "rect": [0.3, 0.3, 0.7, 0.7], "feather": 80,
         "local": {"LocalExposure2012": 0.5}},
        {"kind": "brush", "target": "人脸", "dabs": [[0.4, 0.5], [0.42, 0.52]], "radius": 0.05,
         "local": {"LocalTexture": -25}},
    ])

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "a.CR3"
        raw.write_bytes(b"raw")
        result = W.write_sidecar(raw, {}, None, masks=specs,
                                 orientation="Rotate 270 CW", masks_frame="display")
        text = result.target.read_text(encoding="utf-8")
        tree_ok = True
        try:
            ET.fromstring(text)
        except ET.ParseError:
            tree_ok = False

        doc = R.read_xmp_file(result.target)
        linear = None
        for desc in doc.descriptions:
            for el in desc:
                if not el.tag.endswith("MaskGroupBasedCorrections"):
                    continue
                for corr in el.iter():
                    attrs = {k.split("}")[-1]: v for k, v in corr.attrib.items()}
                    if attrs.get("What") == "Mask/Gradient":
                        linear = ((float(attrs["ZeroX"]), float(attrs["ZeroY"])),
                                  (float(attrs["FullX"]), float(attrs["FullY"])))
        back = M.stored_to_display_spec({"type": "linear", "zero": linear[0], "full": linear[1]},
                                        "Rotate 270 CW") if linear else {}
        roundtrip_ok = bool(linear) and back.get("zero") == (0.5, 0.3) and back.get("full") == (0.5, 0.05)
        what_ok = (
            'crs:What="Mask/Gradient"' in text
            and 'crs:What="Mask/CircularGradient"' in text
            and 'crs:What="Mask/Aggregate"' in text
            and 'crs:What="Mask/Paint"' in text
            and "<crs:Dabs>" in text
        )
        never_ok = all(
            f"crs:{name}=" not in text
            for name in ("MaskDigest", "InputDigest", "ModelVersion")
        )
    ok = ok and tree_ok and roundtrip_ok and what_ok and never_ok
    detail.append(
        f"XML 可解析={'✓' if tree_ok else '✗'}；几何往返一致={'✓' if roundtrip_ok else '✗'}；"
        f"结构标记齐全={'✓' if what_ok else '✗'}；无多余字段={'✓' if never_ok else '✗'}"
    )

    unknown: list[str] = []
    real_xmps = sample_material("*.xmp")
    for xmp in real_xmps:
        try:
            unknown.extend(R.read_xmp_file(xmp).unknown_crs_fields())
        except Exception:  # noqa: BLE001 —— 解析失败由专门的用例负责
            continue
    ok = ok and not unknown
    detail.append(
        f"未登记字段={sorted(set(unknown)) or '无'}"
        + ("" if real_xmps else f"（仓库里没有 test_data/*.xmp，这一半未实测）")
    )
    record("回归：蒙版结构往返合法 + 门禁 100%", ok, "；".join(detail))


def test_end_to_end_offline_write() -> None:
    """步骤 6 回归：离线假结果端到端写一遍（不联网、不碰真实照片）。"""
    import shutil
    import tempfile

    from acb.ai.offline import build_mock_verdict
    from acb.pipeline import output_mode as OM
    from acb.pipeline.job import ScanItem
    from acb.raw.exiftool import ExiftoolRunner
    from acb.xmp import reader as R

    root = Path(__file__).resolve().parent.parent
    sources = sorted((root / "test_data").glob("*.CR3"))
    if not sources:
        record("回归：离线端到端写盘", True, "（test_data 里没有 CR3，跳过）", skipped=True)
        return

    verdict = build_mock_verdict()
    exiftool = ExiftoolRunner()
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        raws = []
        for src in sources[:2]:
            dst = work / src.name
            shutil.copy2(src, dst)          # 只复制，绝不改真实照片
            raws.append(dst)
        opts = OM.OutputOptions(
            output_dir=work / "out",
            run_photoshop=False,
            style_name="离线",
            style_data={"name": "离线", "param_ranges": {}, "text_rules": [], "scene_rules": [],
                        "local_param_ranges": {},
                        "mask_usage": {"samples_with_masks": 8, "samples_total": 10,
                                       "mask_usage_ratio": 0.8}},
        )
        outcomes = []
        for raw in raws:
            item = ScanItem(path=raw, key=raw.name, size=raw.stat().st_size, mtime_ns=0)
            outcomes.append(
                OM._write_xmp_one(item, dict(verdict.params), dict(verdict.curves),
                                  exiftool=exiftool, opts=opts)
            )
        written_ok = all(o.ok and o.target and o.target.is_file() for o in outcomes)
        sidecar = outcomes[0].target if outcomes and outcomes[0].target else None
        text = sidecar.read_text(encoding="utf-8") if sidecar else ""
        params_ok = 'crs:Exposure2012=' in text and "crs:ToneCurvePV2012" in text
        lens_ok = 'crs:LensProfileEnable=' in text and "crs:LensProfileName" not in text
        gate_ok = bool(sidecar) and not R.read_xmp_file(sidecar).unknown_crs_fields()
    ok = ok and written_ok and params_ok and lens_ok and gate_ok
    detail = (
        f"写盘={'✓' if written_ok else '✗'}；参数与曲线={'✓' if params_ok else '✗'}；"
        f"镜头基线补缺={'✓' if lens_ok else '✗'}；无未登记字段={'✓' if gate_ok else '✗'}"
    )
    record("回归：离线端到端写盘（不联网/不碰真实照片）", ok, detail)


def test_builtin_styles() -> None:
    """内置风格：」AI自主决策」可用、不可删，且**硬规则对所有风格一视同仁**。

    用户要求（原话拆成两条）：
      1. 风格里加一个软件内置的选项「AI自主决策」（界面中不可删除），
         选中它 = 让 AI 自主决策每一张图片的调整方向，不预设偏好；
      2. **但是**对所有的风格（内置的、用户训练的）都必须遵循配置文件保留的那套规则。

    第 2 条是本用例的重点：它必须被断言，否则"不做限制"很容易在后续改动里
    被理解成"放宽程序侧的校验"。
    """
    import json

    from acb.ai.prompts import render_style_block
    from acb.constants import (
        AUTOPILOT_PROMPT,
        AUTOPILOT_STYLE_NAME,
        DEBUG_ONLY_STYLE_NAMES,
        DEFAULT_STYLE_NAME,
        STYLE_EXECUTION_PROMPT,
    )
    from acb.pipeline.output_mode import resolve_effective_prompt
    from acb.pipeline.style_profile import (
        ensure_seed_styles,
        is_autopilot_style,
        is_autopilot_style_name,
        is_seed_style,
        list_styles,
        load_style_profile,
        style_file_for,
    )
    from acb.paths import resource_root
    from acb.xmp import validator as V

    # --- 1) 种子文件：打进包里、能被释放、出现在下拉里 ---
    bundled = resource_root() / "styles" / f"{AUTOPILOT_STYLE_NAME}.json"
    ensure_seed_styles()
    target = style_file_for(AUTOPILOT_STYLE_NAME)
    in_list = AUTOPILOT_STYLE_NAME in list_styles()
    seed_ok = (
        bundled.is_file()
        and target is not None
        and is_seed_style(target)                  # → 界面据此拒绝删除
        and in_list
    )

    data = load_style_profile(AUTOPILOT_STYLE_NAME) or {}
    detect_ok = is_autopilot_style(data) and is_autopilot_style_name(AUTOPILOT_STYLE_NAME)

    # 内置风格在"还没被释放到用户目录"时也要装载得到。
    # 【为什么单独测这一条】CLI 首次用 `--style AI自主决策` 时就是这个状态：
    # 用户目录里还没有这个文件（种子在这次运行的后面某一步才释放），
    # 于是程序打印「找不到风格…将按未选择风格处理」——用户会以为内置风格不存在。
    # 修法是 load_style_profile 退一步读打包内那份（只读）。
    from acb.paths import styles_dir as _styles_dir

    released = _styles_dir() / f"{AUTOPILOT_STYLE_NAME}.json"
    removed = False
    if released.is_file():
        released.unlink()
        removed = True
    pre = load_style_profile(AUTOPILOT_STYLE_NAME)
    pre_release_ok = isinstance(pre, dict) and pre.get("name") == AUTOPILOT_STYLE_NAME
    ensure_seed_styles()                       # 恢复现场
    # 它必须**与**内置基准风格分开：默认基准只在离线调试下可选，它任何时候都能选
    split_ok = (
        DEFAULT_STYLE_NAME in DEBUG_ONLY_STYLE_NAMES
        and AUTOPILOT_STYLE_NAME not in DEBUG_ONLY_STYLE_NAMES
    )
    # 风格文件本身不该携带任何偏好规则（否则"自主决策"就被偷偷加了约束）
    empty_ok = not (data.get("text_rules") or []) and not (data.get("param_ranges") or {})

    # --- 2) 提示词：没有偏好规则可遵守，绝不能再塞"保守/不做 HSL"那套 ---
    prompt, note = resolve_effective_prompt("", data)
    block = render_style_block(data, AUTOPILOT_STYLE_NAME)
    prompt_ok = (
        prompt == AUTOPILOT_PROMPT
        and prompt != STYLE_EXECUTION_PROMPT          # 不能要求"逐条遵守"不存在的规则
        and "不做 HSL" not in prompt
        and "自主" in prompt
        and "自主" in block
        and "逐条遵守" not in block                    # 没有规则可逐条遵守
        and "不做 HSL" not in block
        # 蒙版：步骤 5 之后本程序**能写**几何蒙版了，所以这里断言的是
        # "如实说明能力与提法"（旧断言写的「无法写入」在步骤 5 上线后必须改，否则假红）。
        and "可以写入几何蒙版" in block
        and "masks" in block
    )
    # 用户自己填的提示词永远优先（风格不参与竞争）
    typed_ok = resolve_effective_prompt("保持真实", data)[0] == "保持真实"

    # --- 3) 硬规则对所有风格一视同仁（用户要求的第 2 条）---
    # 三类风格各取一份，逐个验证"机器字段 / 未登记字段 / 越界值"照样被拒。
    style_variants = [
        ("自主决策", data),
        ("内置基准", load_style_profile(DEFAULT_STYLE_NAME) or {}),
        ("训练风格", {"name": "某风格", "text_rules": ["随便调"]}),
    ]
    rule_rows = []
    rules_ok = True
    for label, style_dict in style_variants:
        _p, _note = resolve_effective_prompt("", style_dict)
        _blk = render_style_block(style_dict, style_dict.get("name"))
        checks = {
            "机器字段": not V.validate_params({"crs:ProcessVersion": "15.4"}).ok,
            "相机配置": not V.validate_params({"crs:CameraProfile": "Camera Portrait"}).ok,
            "未登记字段": not V.validate_params({"crs:NotARealField": 1}).ok,
            "越界值": not V.validate_params({"crs:Highlights2012": 100 + 500}).ok,
        }
        rules_ok = rules_ok and all(checks.values())
        rule_rows.append(f"{label}={'全拒' if all(checks.values()) else checks}")

    # AI_FORBIDDEN 名单里必须有相机配置与版本字段（不是"风格说了算"）
    from acb.constants import AI_FORBIDDEN_FIELDS
    forbidden_ok = {"CameraProfile", "ProcessVersion", "WhiteBalance"} <= set(AI_FORBIDDEN_FIELDS)

    record(
        "内置「AI自主决策」风格：可用、不可删、与默认基准风格互不干扰",
        bool(
            seed_ok and detect_ok and split_ok and empty_ok and prompt_ok and typed_ok
            and pre_release_ok
        ),
        f"种子在包里={bundled.is_file()} 已释放={target is not None} 拒删={target is not None and is_seed_style(target)} "
        f"出现在下拉={in_list} 识别={detect_ok} 与调试专用风格分开={split_ok} "
        f"文件不带偏好规则={empty_ok}；提示词分支正确={prompt_ok} 用户提示词优先={typed_ok}；"
        f"未释放时也能装载（CLI 首次运行的场景，删掉用户副本后仍可读={pre_release_ok}，先删除={removed}）",
    )
    record(
        "所有风格都必须遵守配置文件保留的硬规则（机器字段/相机配置/未登记字段/越界值）",
        bool(rules_ok and forbidden_ok),
        "；".join(rule_rows) + f"；禁用名单含相机配置与版本字段={forbidden_ok}",
    )


def test_camera_profile_mapping() -> None:
    """机内风格 → ACR 配置名的映射，以及"新建侧车才写、已有侧车不动"。

    用户真实反馈：「只用 Adobe 标准的色彩配置，明明应该以已有的相机配置为准」。
    实测：那批 RAW 没有侧车，我们新建的最小骨架里没有 crs:CameraProfile，
    而侧车一旦存在 ACR 就不再回落到"相机默认配置" → 掉成 Adobe 标准。
    """
    import os
    from unittest import mock

    from acb.paths import data_root
    from acb.raw.camera_profile import guess_camera_profile
    from acb.xmp import fields as F
    from acb.xmp import writer as W

    class FakeExiftool:
        """只实现 guess_camera_profile 用到的两个接口。"""

        available = True

        def __init__(self, values: dict[str, str]) -> None:
            self.values = values

        def read_tags(self, path, names):
            return {k: v for k, v in self.values.items() if k in names}

    cases = [
        ({"PictureStyle": "Portrait"}, "Camera Portrait", "佳能 Portrait（用户 12 个文件实测同值）"),
        ({"PictureStyle": "Landscape"}, "Camera Landscape", "佳能 Landscape（实测同值）"),
        ({"PictureControlName": "Vivid"}, "Camera Vivid", "尼康 Vivid"),
        ({"CreativeStyle": "Neutral"}, "Camera Neutral", "索尼 Neutral"),
        ({"PictureStyle": "User Def. 1"}, None, "佳能自定风格 → 不猜"),
        ({"FilmMode": "Velvia"}, None, "富士命名与 ACR 不一 → 不猜"),
        ({}, None, "没有任何风格标签"),
    ]
    rows: list[str] = []
    ok_all = True
    for tags, expected, label in cases:
        guess = guess_camera_profile(Path("x.CR3"), FakeExiftool(tags))
        good = guess.value == expected
        ok_all = ok_all and good
        rows.append(f"{label}: {guess.value}" + ("✓" if good else f"✗（期望 {expected}）"))

    # 不能让 AI 有机会自己填相机配置（它在 AI_FORBIDDEN_FIELDS 里）
    spec = F.get_field("CameraProfile")
    ai_blocked = spec is not None and not spec.ai_writable

    # 新建侧车写入 / 已有侧车保留
    #
    # ⚠ 必须把 %APPDATA% 指到临时目录：覆盖已有侧车会先备份原件，备份落在
    #   <数据目录>/backups/xmp/ —— 不隔离就会每次跑自检都往用户真实数据目录里
    #   塞 a/legacy/foreign 三份测试产物（实测发生过 8 次，见用户目录时间戳）。
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
        os.environ, {"APPDATA": str(Path(tmp) / "appdata")}
    ):
        raw = Path(tmp) / "a.CR3"
        raw.write_bytes(b"not-a-real-raw")
        first = W.write_sidecar(raw, {"Exposure2012": 0.5}, None,
                                camera_profile="Camera Portrait", profile_note="测试")
        text_new = W.sidecar_path_for(raw).read_text(encoding="utf-8")
        new_ok = (
            first.created_new
            and first.camera_profile == "Camera Portrait"
            and 'crs:CameraProfile="Camera Portrait"' in text_new
        )
        # 已有侧车时：传入别的值也不许覆盖
        second = W.write_sidecar(raw, {"Exposure2012": 0.75}, None,
                                 camera_profile="Camera Neutral", profile_note="不该采用")
        text_again = W.sidecar_path_for(raw).read_text(encoding="utf-8")
        keep_ok = (
            not second.created_new
            and 'crs:CameraProfile="Camera Portrait"' in text_again
            and "Camera Neutral" not in text_again
            and 'crs:Exposure2012="+0.75"' in text_again
        )

        # 旧版本（本程序）建的侧车：有我们的工具指纹但没有该字段 → 应该补上
        # （用户今天已经被写坏的 5 个文件就靠这条修回来，不需要手工删侧车）
        legacy = Path(tmp) / "legacy.CR3"
        legacy.write_bytes(b"raw")
        legacy_side = W.sidecar_path_for(legacy)
        legacy_side.write_text(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Auto Color Diffusion">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""'
            ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
            ' crs:Version="15.0" crs:Exposure2012="+0.10"/>\n'
            ' </rdf:RDF>\n</x:xmpmeta>\n',
            encoding="utf-8",
        )
        third = W.write_sidecar(legacy, {"Highlights2012": -15}, None,
                                camera_profile="Camera Portrait", profile_note="补写测试")
        fill_ok = (
            third.camera_profile == "Camera Portrait"
            and 'crs:CameraProfile="Camera Portrait"' in legacy_side.read_text(encoding="utf-8")
        )

        # 别的工具（ACR）建的侧车缺该字段 → 不许动（ACR 靠自己的 crd: 基线）
        foreign = Path(tmp) / "foreign.CR3"
        foreign.write_bytes(b"raw")
        foreign_side = W.sidecar_path_for(foreign)
        foreign_side.write_text(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""'
            ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
            ' crs:Version="15.4" crs:Exposure2012="0.00"/>\n'
            ' </rdf:RDF>\n</x:xmpmeta>\n',
            encoding="utf-8",
        )
        fourth = W.write_sidecar(foreign, {"Highlights2012": -15}, None,
                                 camera_profile="Camera Portrait", profile_note="不该写")
        foreign_ok = (
            fourth.camera_profile is None
            and "CameraProfile" not in foreign_side.read_text(encoding="utf-8")
        )

        # 备份纪律：**只有覆盖已有侧车**才备份，且每份原件只留一份。
        # a.CR3 的第一次写入是"新建"，不该产生备份；后三次（a 的第二次、
        # legacy、foreign）都是覆盖，各自必须留下原件。
        backup_dir = data_root() / "backups" / "xmp"
        stems = sorted(b.name.split(".", 1)[0] for b in backup_dir.glob("*.xmp"))
        backup_ok = stems == ["a", "foreign", "legacy"]
        first_a_backup = backup_dir.glob("a.*.xmp")
        backup_is_original = any(
            'crs:Exposure2012="+0.50"' in b.read_text(encoding="utf-8")
            for b in first_a_backup
        )

    record(
        "机内风格 → ACR 相机配置映射（表外一律不猜）",
        bool(ok_all and ai_blocked),
        "；".join(rows) + f"；AI 不可写={ai_blocked}",
    )
    record(
        "相机配置：新建写入 / 已有值保留 / 补写旧版侧车 / 不动别人的侧车",
        bool(new_ok and keep_ok and fill_ok and foreign_ok),
        f"新建写入={new_ok}（{first.camera_profile}）已有值保留={keep_ok} "
        f"补写旧版侧车={fill_ok} 别人的侧车不动={foreign_ok}；"
        f"覆盖前备份（仅覆盖时，原件内容正确）={backup_ok and backup_is_original}"
        f"（备份={stems}）",
    )


def test_exiftool_utf8_path(raw_dir: Path | None) -> None:
    """中文路径必须能读能写（否则每张图都静默降级成 rawpy 全解码）。

    实测（exiftool 13.59，本机）：中文路径 + `-charset filename=utf8`
    → rc=1「File not found」；去掉该参数 → 正常。写入侧同样：
    带它时 exiftool 返回 0 但**文件没被改动**（最危险的一类静默失败）。
    `test_data` 是纯 ASCII 路径，所以这个缺陷一直没暴露。
    """
    import subprocess

    from acb.raw.exiftool import _BASE_ARGS, ExiftoolRunner, _reject_stderr_errors

    args_ok = "filename=utf8" not in " ".join(_BASE_ARGS)

    raised_on_error = False
    try:
        _reject_stderr_errors("Error: File not found - E:/x.CR3", 1)
    except RuntimeError:
        raised_on_error = True
    warning_ok = True
    try:
        _reject_stderr_errors("Warning: Non-standard XMP property: exif:Foo", 1)
    except RuntimeError:
        warning_ok = False

    record(
        "exiftool 参数：不带会破坏中文路径的 filename charset",
        bool(args_ok and raised_on_error and warning_ok),
        f"参数={_BASE_ARGS}；Error 报错={raised_on_error}；Warning 不报错={warning_ok}",
    )

    if not raw_dir or not raw_dir.is_dir():
        record("中文路径实测（未提供 --raw-dir，跳过）", True, "", skipped=True)
        return
    raws = [p for p in sorted(raw_dir.rglob("*")) if p.suffix.lower() in (".cr3", ".cr2", ".dng", ".nef", ".arw")]
    if not raws:
        record("中文路径实测（无 RAW，跳过）", True, "", skipped=True)
        return

    exiftool = ExiftoolRunner()
    from acb.raw.preview import extract_thumbnail

    with tempfile.TemporaryDirectory() as tmp:
        # 目录名故意带中文、空格与百分号（用户真实路径就是 `E:\%同步\R5m2\`）
        cn_dir = Path(tmp) / "%同步 测试"
        cn_dir.mkdir()
        target = cn_dir / (raws[0].stem + " 副本" + raws[0].suffix)
        shutil.copy2(raws[0], target)

        tags: dict[str, str] = {}
        tag_error = ""
        try:
            tags = exiftool.read_tags(target, ["Model", "PictureStyle"])
        except Exception as exc:
            tag_error = str(exc)[:90]
        preview_source = ""
        preview_error = ""
        try:
            preview = extract_thumbnail(target, exiftool)
            preview_source = preview.source
        except Exception as exc:
            preview_error = str(exc)[:90]

        # 关键断言：exiftool 真的打开了这个文件（读到 Model），
        # 而且预览走的是**内嵌预览**（不再因为读不到而回退 rawpy）。
        ok = bool(tags.get("Model")) and preview_source == "preview"
        record(
            "中文路径实测：exiftool 能读、预览不再降级 rawpy",
            ok,
            f"读到 Model={tags.get('Model')!r} 预览来源={preview_source or '（失败）'} "
            f"{'tag 异常：' + tag_error if tag_error else ''}"
            f"{'预览异常：' + preview_error if preview_error else ''}",
        )


def test_dng_embedded_write() -> None:
    """DNG 内嵌 XMP 写回：必须真能写进去，而且「没写进去」绝不能报成功。

    真实事故（用户 2026-09-23：「选完文件夹，处理完成后发现里面的 DNG 未被处理」）：
    他的 DJI DNG 上 exiftool 打印 `Error: [minor] Maker notes could not be parsed`
    并且**一个字节都不写**（文件 sha 不变、没有 *_original），于是：
      · 3 个 DNG 全部没写进去（读出的是原始包，只有 DJI 自己的 4 个 crs 字段）；
      · 它们也不在 Photoshop 导出清单里（清单 18 项 = 18 个 CR3）；
      · 失败只落在界面状态栏，**日志文件里一行都没有** → 用户事后翻日志什么也看不到。
    修法两条，这个用例各钉一条：
      1. 写回命令带 `-m`（忽略 minor 错误）—— 实测加它之后 DJI DNG 能正常写入
         （标签总数 202→202、像素数据长度不变、*_original 备份照旧生成）；
      2. 写完之后**把包读回来核对**本次应用过的字段名，缺一个就按失败报。
    """
    from acb.raw.exiftool import ExiftoolRunner
    from acb.xmp import dng as D

    exiftool = ExiftoolRunner()
    if not exiftool.available:
        record("DNG 内嵌 XMP 写回（未检测到 exiftool，跳过）", True, "", skipped=True)
        return

    # --- 1) 写回命令必须带 -m，且「假成功」必须被读回核对拦下 ---
    captured: list[list[str]] = []

    def spy(self, args):
        # 只拦文本命令（写回走它）。故意返回空 = 假装 exiftool 顺利跑完，
        # 但**一个字节都没写** —— 这正是原来那次事故的形态。
        captured.append(list(args))
        return ""

    real_run_text = ExiftoolRunner._run_text
    fake_success_raised = False
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake.DNG"
        fake.write_bytes(b"not a real dng")
        try:
            ExiftoolRunner._run_text = spy
            D.write_embedded(fake, {"Exposure2012": 0.3}, exiftool=exiftool)
        except D.DngWriteError:
            fake_success_raised = True
        except Exception:
            fake_success_raised = False
        finally:
            ExiftoolRunner._run_text = real_run_text

    dash_m_ok = any("-m" in args for args in captured)
    verify_ok = fake_success_raised

    # --- 2) 真文件往返：能写进去、能读回来、原片有备份 ---
    round_trip_ok = False
    detail = ""
    try:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # 容器必须用 TIFF：JPEG 命名成 .dng 时 exiftool 会直接拒绝
            # （`Error: Not a valid DNG (looks more like a JPEG)` —— 实测过）。
            # TIFF 命名成 .dng 则完全走得通，而且我们的 `is_dng()` 只看扩展名，
            # 所以这条路径与真 DNG 是同一条代码分支。
            target = work / "sample.DNG"
            Image.new("RGB", (16, 16), (128, 128, 128)).save(target, "TIFF")
            params = {"Exposure2012": 0.35, "Vibrance": 12}
            # 故意带一条**曲线**：它是 `rdf:Seq` 元素，在 XML 里不是 `crs:xxx="..."`
            # 属性。写后核对如果拿属性形式去查曲线字段，就会把**成功**判成失败
            # （实测踩到过：好文件被自己的核对判失败、还被计进失败账本）。
            curves = {"ToneCurvePV2012": [[0.0, 0.0], [0.5, 0.55], [1.0, 1.0]]}
            # 同样带一个**蒙版**：蒙版也是子元素（MaskGroupBasedCorrections），
            # 没有 `crs:蒙版:…=` 这种属性。用户 2026-09-24 的「2/18 报错」就是它：
            # 蒙版明明写进去了，旧核对却判“没写进去”并把文件计成失败。
            masks = [{
                "type": "linear", "name": "天空",
                "correction_name": "天空（压暗）",
                "zero": (0.5, 0.9), "full": (0.5, 0.1),
                "local": {"LocalHighlights2012": -30.0},
            }]
            result = D.write_embedded(
                target, params, curves, exiftool=exiftool, masks=masks,
                orientation="Horizontal (normal)",
            )
            body = (exiftool.read_xmp_packet(target) or b"").decode("utf-8", "replace")
            round_trip_ok = (
                result.mode == "embedded"
                # +曲线 +ToneCurveName2012 +蒙版
                and len(result.applied_fields) == len(params) + 3
                and all(f"crs:{name}=" in body for name in ("Exposure2012", "Vibrance"))
                and "ToneCurvePV2012" in body      # 曲线（元素）也真的进去了
                # 蒙版真的进去了：用 CorrectionName 核对（这才是它在 XML 里的形态）
                and 'CorrectionName="天空（压暗）"' in body
                and (work / "sample.DNG_original").exists()
            )
            detail = (
                f"模式={result.mode} 应用={len(result.applied_fields)} 字段; 含曲线元素={'ToneCurvePV2012' in body}; "
                f"含蒙版={'CorrectionName=\"天空（压暗）\"' in body}; "
                f"写回后含字段={all(f'crs:{n}=' in body for n in ('Exposure2012', 'Vibrance'))}; "
                f"有 _original={(work / 'sample.DNG_original').exists()}"
            )
    except Exception as exc:
        detail = f"往返失败：{exc}"[:160]

    record(
        "DNG 写回：命令带 -m；假成功被读回核对拦下；真文件能写进去",
        bool(dash_m_ok and verify_ok and round_trip_ok),
        f"-m 在命令里={dash_m_ok}（captured={len(captured)} 条）；"
        f"假成功被拦={verify_ok}；真文件往返={round_trip_ok}；{detail}",
    )

def test_new_sidecar_camera_profile(raw_dir: Path | None) -> None:
    """端到端：没有侧车的 RAW 跑一遍，产出的侧车里必须有相机配置。

    这是用户那次真实事故的最小复现：他的 `_G4A0172.CR3` 没有侧车，
    跑完之后侧车只有 561 字节、没有 crs:CameraProfile → ACR 显示 Adobe 标准。
    """
    if not raw_dir or not raw_dir.is_dir():
        record("新建侧车写入相机配置（未提供 --raw-dir，跳过）", True, "", skipped=True)
        return
    raws = [p for p in sorted(raw_dir.rglob("*")) if p.suffix.lower() in (".cr3", ".cr2", ".nef", ".arw")]
    if not raws:
        record("新建侧车写入相机配置（无可用 CR3/NEF，跳过）", True, "", skipped=True)
        return

    from acb.config import load_models_config
    from acb.pipeline.job import JobState
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.camera_profile import guess_camera_profile
    from acb.raw.exiftool import ExiftoolRunner

    source = raws[0]
    exiftool = ExiftoolRunner()
    guess = guess_camera_profile(source, exiftool)
    if not guess.resolved:
        record(
            "新建侧车写入相机配置（样本无机内风格标签，跳过）",
            True,
            f"{source.name}：{guess.reason}",
            skipped=True,
        )
        return

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "photos"
        work.mkdir()
        # 故意**不**复制侧车：走"新建"分支
        shutil.copy2(source, work / source.name)

        from acb.ai.offline import OfflineAdapter

        adapter = OfflineAdapter(load_models_config().active_spec())
        result = run_output_mode(
            OutputOptions(
                sources=[work],
                prompt="",
                resume=False,
                run_photoshop=False,
                use_cache=False,
                workers=1,
            ),
            adapter=adapter,
            exiftool=exiftool,
            job_state=JobState(directory=Path(tmp) / "state"),
            callbacks=JobCallbacks(),
        )
        sidecar = (work / source.name).with_suffix(".xmp")
        text = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""
        ok = (
            sidecar.is_file()
            and f'crs:CameraProfile="{guess.value}"' in text
            and result.failed == 0
            and result.done >= 1
        )
        record(
            "端到端：新建侧车带上了相机配置（不再是 Adobe 标准）",
            bool(ok),
            f"期望 {guess.value}（{guess.reason}）；侧车 {'已写出' if sidecar.is_file() else '未写出'} "
            f"{sidecar.stat().st_size if sidecar.is_file() else 0} 字节；"
            f"done={result.done} failed={result.failed}",
        )


def test_request_shape_per_provider() -> None:
    """各家请求形状必须跟官方文档核对表一致（用户要求"换个 API 也能直接用"）。

    这张表里每一格都是**"用户选到那个模型会不会报错"**，全部来自 2026-09 的官方文档核对：
      · OpenAI：推理模型（o 系 / gpt-5+）在 Chat Completions 上只认
        max_completion_tokens，且不支持 temperature / top_p 等；
      · 阿里云百炼（qwen）：多模态输入不支持 json_schema（会自动降级）；
        "开启结构化输出时请勿设置 max_tokens"（会被截断）；
      · 智谱 GLM：只文档了 json_object；temperature 区间 (0,1)；
      · Kimi：max_tokens 已 deprecated，要用 max_completion_tokens；
        json_schema 要求 schema 符合 MFJS 规范。
    """
    from acb.ai.adapter import ModelAdapter
    from acb.ai.client import build_chat_request
    from acb.ai.schema import build_response_schema
    from acb.config import load_models_config

    cfg = load_models_config()
    providers = cfg.providers

    def spec_of(pid: str) -> object:
        preset = providers[pid]
        model = preset.default_models[0]
        return preset.spec_for(pid, model)

    def body_of(pid: str) -> dict:
        spec = spec_of(pid)
        req = build_chat_request(
            spec,
            "test-key",
            [{"role": "user", "content": "hi"}],
            temperature=0.6,
            response_format=None,
        )
        return req.payload

    # --- 1) 输出上限参数名 / temperature 该不该发 ---
    b_openai = body_of("openai")
    b_qwen = body_of("qwen")
    b_kimi = body_of("kimi")
    b_deepseek = body_of("deepseek")
    shape_ok = (
        # OpenAI：推理模型只认 max_completion_tokens，且不能带 temperature
        "max_completion_tokens" in b_openai
        and "max_tokens" not in b_openai
        and "temperature" not in b_openai
        # 千问：官方说结构化输出时别设 max_tokens → 两个都不发
        and "max_tokens" not in b_qwen
        and "max_completion_tokens" not in b_qwen
        and "temperature" in b_qwen
        # Kimi：官方已弃用 max_tokens
        and "max_completion_tokens" in b_kimi
        and "max_tokens" not in b_kimi
        # DeepSeek：官方文档用的就是 max_tokens，保持不变（不能一刀切）
        and "max_tokens" in b_deepseek
        and "temperature" in b_deepseek
    )

    # --- 2) 输出格式约束：带图的四家都走 json_object，不硬发 json_schema ---
    class _Probe:
        """只给 _response_format 用的最小对象（它只读下面这几个成员）。"""

        def __init__(self, spec: object) -> None:
            self.spec = spec
            self.supports_json_schema = bool(getattr(spec, "supports_json_schema", False))

        def _schema_dialect(self):
            # _response_format 会按方言剥关键字，所以桩必须提供这一项 ——
            # 否则这里会变成 AttributeError（第一次就撞上了），
            # 而不是"少测了一点"。桩要跟着被测函数的真实依赖走。
            from acb.ai.schema import dialect_for_spec

            return dialect_for_spec(self.spec)

    def fmt_of(pid: str) -> object:
        spec = spec_of(pid)
        return ModelAdapter._response_format(
            _Probe(spec), build_response_schema(["a"], strict=spec.json_schema_strict), "acb_result"
        )

    fmt_json_object = {"type": "json_object"}
    format_ok = (
        fmt_of("qwen") == fmt_json_object
        and fmt_of("glm") == fmt_json_object
        and fmt_of("kimi") == fmt_json_object
        and fmt_of("deepseek") == fmt_json_object
        # OpenAI 仍保留 json_schema 能力（官方明确支持，且我们已剥掉不支持的关键字）
        and (fmt_of("openai") or {}).get("type") == "json_schema"
    )

    # --- 3) schema 严格子集：不能把 minimum/maxItems 之类发给服务端 ---
    # 注意必须用 strict=True 的 schema 来测：anyOf/全 required 只在严格模式下才生成，
    # 拿非严格 schema 断言"有 anyOf"会假失败（我第一版就这么写错了）。
    from acb.ai.schema import build_response_schema as _build_schema

    strict_schema = _build_schema(["a"], strict=True)
    sent_schema = ModelAdapter._response_format(
        _Probe(spec_of("openai")), strict_schema, "acb_result"
    )["json_schema"]["schema"]
    # 同一个 schema，剥离前后对比（json 是 smoke_test 顶部的标准库导入）
    before = json.dumps(strict_schema)
    dumped = json.dumps(sent_schema)
    strip_happened = '"minimum"' in before and '"minimum"' not in dumped
    subset_ok = (
        strip_happened
        and '"maximum"' not in dumped
        and '"minItems"' not in dumped
        and '"required"' in dumped
        and '"anyOf"' in dumped
    )

    # --- 4) 断言必须落在**序列化后的字符串**上，不能只看 dict ---
    # 理由（外部评审提的，说得对）：`{"max_tokens": None}` 在 dict 层面看着无害，
    # json.dumps 出来却是 `"max_tokens": null`，有些网关会因为"出现了空参数"
    # 直接判 400。omit 模式最容易踩到：我们连键都不该有，一旦哪天有人写成
    # `payload["max_tokens"] = None`，只查 dict 的断言照样能过。
    s_qwen = json.dumps(body_of("qwen"))
    s_openai = json.dumps(body_of("openai"))
    s_deepseek = json.dumps(body_of("deepseek"))
    serialized_ok = (
        '"max_tokens"' not in s_qwen
        and '"max_completion_tokens"' not in s_qwen
        and ": null" not in s_qwen
        and '"max_tokens"' not in s_openai
        and '"max_tokens"' in s_deepseek            # DeepSeek 必须继续发旧名字
    )

    # --- 5) 抬高输出上限时，**落到哪个字段**必须跟着 max_tokens_mode 走 ---
    # 外部评审指出的语义差：如果翻倍翻的是"服务端根本不看"的那个字段，
    # 请求照样 200，输出却比原来截得更早 —— 不报错、只变差，最难查。
    def over_of(pid: str) -> dict:
        return build_chat_request(
            spec_of(pid), "k", [{"role": "user", "content": "hi"}],
            temperature=0.6, response_format=None, max_tokens_override=9999,
        ).payload

    over_openai = over_of("openai")
    over_deepseek = over_of("deepseek")
    over_qwen = over_of("qwen")
    escalate_field_ok = (
        over_openai.get("max_completion_tokens") == 9999
        and "max_tokens" not in over_openai
        and over_deepseek.get("max_tokens") == 9999
        and "max_completion_tokens" not in over_deepseek
        # omit：一个字段都不发，抬高的值**不能**出现在任何地方
        and "max_tokens" not in over_qwen
        and "max_completion_tokens" not in over_qwen
        and "9999" not in json.dumps(over_qwen)
    )

    record(
        "各家请求形状（OpenAI/千问/GLM/Kimi/DeepSeek 按官方文档分别处理）",
        bool(shape_ok and format_ok and subset_ok and serialized_ok and escalate_field_ok),
        f"openai={sorted(b_openai)}；qwen={sorted(b_qwen)}；kimi={sorted(b_kimi)}；"
        f"output_format={fmt_of('qwen')}/{fmt_of('kimi')}/{(fmt_of('openai') or {}).get('type')}；"
        f"严格子集已剔关键字={subset_ok}；序列化后无空字段={serialized_ok}；"
        f"抬高上限落在正确字段={escalate_field_ok}",
    )


def test_doubao_and_qianfan_doc_facts() -> None:
    """豆包接入 + 千帆复核：写进配置的**每个字符串**都要能在官方文档里指出来。

    用户的原话是"记得核查官方技术文档以确保字符的合法性"—— 所以这里断言的
    不是"代码跑得动"，而是配置里的字面量与 2026-09 的官方文档逐条对得上：

      火山方舟《对话(Chat) API》docs.volcengine.com/docs/82379/1494384
        · POST https://ark.cn-beijing.volces.com/api/v3/chat/completions
        · 鉴权 `Authorization: Bearer $ARK_API_KEY`
        · body 参数表里**逐字**出现：max_tokens（默认 4096）、
          max_completion_tokens、response_format、reasoning_effort
      火山方舟《模型列表》docs.volcengine.com/docs/82379/1330310
        · doubao-seed 全系带"多模态理解"（→ supporting_vision: true）
          与"结构化输出"标签
      千帆《文本生成》cloud.baidu.com/doc/qianfan-api/s/3m7of64lb
      千帆《视觉理解》cloud.baidu.com/doc/qianfan-api/s/rm7u7qdiq
        · 同一个 POST /v2/chat/completions；Bearer bce-v3/…
        · 视觉理解的 image_url 里**只有 url 一个键**
      千帆《模型列表》cloud.baidu.com/doc/qianfan/s/rmh4stp0j
        · ernie-4.5-turbo-vl：128k 上下文 / 最大输出 16384
        · ernie-5.0：128k / 65536，且文本、视觉两张表里都有

    另外两条"不做什么"的断言，比"做了什么"更容易被后人改坏：
      · 千帆/豆包**不发** image_url.detail（文档没写，有的网关拒收未文档字段）；
      · 豆包**不做**密钥前缀识别（官方只说"格式变了"，没给前缀字面量，
        猜错的代价是把密钥 misroute 到别人家账户）。
    """
    from acb.ai.adapter import ModelAdapter, PreviewItem
    from acb.ai.client import build_chat_request
    from acb.config import detect_provider_from_key, load_models_config

    cfg = load_models_config()
    providers = cfg.providers

    # --- 1) 豆包条目的字面量逐条对文档 ---
    db = providers.get("doubao")
    doubao_facts_ok = bool(
        db is not None
        and str(db.base_url) == "https://ark.cn-beijing.volces.com/api/v3"
        and str(db.endpoint_path) == "/chat/completions"
        and str(db.key_env) == "ARK_API_KEY"
        and db.supports_vision is True
        # 官方参数表里两个上限参数都在，取语义为"总输出长度"的那个
        # （豆包自带深度思考，思维链也吃预算）
        and db.max_tokens_mode == "max_completion_tokens"
        # 文档里的 image_url 子键没写 detail
        and db.sends_image_detail is False
        # 上限值要落在"官方默认 4096 / 最大回答 256k"之间，不能凭空写大
        and 4096 <= db.max_output_tokens <= 256 * 1024
        and 128 * 1024 <= db.max_context <= 1024 * 1024
        # 兜底模型必须是官方《模型列表》里的 id（硬编码成"文档里那几行"）
        and db.default_models[0] in {
            "doubao-seed-2-1-pro-260628", "doubao-seed-2-1-pro-260915",
            "doubao-seed-2-1-lite-260915", "doubao-seed-evolving",
        }
    )

    # --- 2) 请求体：参数名与实际发送的键必须一致（序列化后再查）---
    # 输出格式档也要从**配置**推出来（不是测试替它选）：模型列表里标着
    # 「结构化输出」、配置里却忘了开 supports_json_object 时，下面会红。
    class _FmtProbe:
        '''只给 _response_format 用的最小对象（它读下面这三个成员）。'''

        def __init__(self, spec: object) -> None:
            self.spec = spec
            self.supports_json_schema = bool(getattr(spec, 'supports_json_schema', False))

        def _schema_dialect(self):
            from acb.ai.schema import dialect_for_spec

            return dialect_for_spec(self.spec)

    def fmt_of(pid: str) -> object:
        spec = providers[pid].spec_for(pid, providers[pid].default_models[0])
        return ModelAdapter._response_format(_FmtProbe(spec), {'type': 'object'}, 'acb_result')

    db_spec = db.spec_for('doubao', db.default_models[0])
    db_body = build_chat_request(
        db_spec, "ark-key", [{"role": "user", "content": "hi"}],
        temperature=0.6, response_format={"type": "json_object"},
    ).payload
    db_serialized = json.dumps(db_body)
    doubao_body_ok = (
        db_body.get("max_completion_tokens") == db.max_output_tokens
        and "max_tokens" not in db_body
        and "temperature" in db_body          # 方舟是标准 OpenAI 兼容体，temperature 合法
        # 格式档要由**配置**推出，不能由测试替它选：
        # 官方模型列表标着「结构化输出」、配置却漏了 supports_json_object 时，这两行会红
        and fmt_of("doubao") == {"type": "json_object"}
        and fmt_of("qianfan") == {"type": "json_object"}
        and db_body.get("response_format") == {"type": "json_object"}
        and ": null" not in db_serialized
    )

    # 抬高上限也要落在同一个字段上（评审提过的"翻倍翻错字段"陷阱）
    db_up = build_chat_request(
        db_spec, "k", [{"role": "user", "content": "hi"}],
        temperature=0.6, response_format=None, max_tokens_override=9999,
    ).payload
    doubao_escalate_ok = db_up.get("max_completion_tokens") == 9999 and "max_tokens" not in db_up

    # --- 3) 图片消息：detail 要按各家文档来，不能"一刀切全发"---
    class _MsgProbe:
        """只给 _build_messages 用的最小对象（它读的就是这几个成员）。"""

        def __init__(self, spec: object) -> None:
            self.spec = spec
            self.supports_vision = bool(getattr(spec, "supports_vision", False))
            self.supports_json_schema = bool(getattr(spec, "supports_json_schema", False))

        # 非视觉分支会调它；视觉分支用不到
        @staticmethod
        def _schema_as_text(schema: dict) -> str:  # pragma: no cover - 防御用
            return ""

    item = PreviewItem(
        file_id="f1", filename="a.CR3", path=Path("a.CR3"), preview_jpeg=b"\xff\xd8\xff"
    )

    def image_url_keys(pid: str) -> list[str]:
        spec = providers[pid].spec_for(pid, providers[pid].default_models[0])
        msgs = ModelAdapter._build_messages(
            _MsgProbe(spec), "sys", "user", [item], {"type": "object"}
        )
        content = msgs[-1]["content"]
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return sorted(part["image_url"].keys())
        return []

    keys_openai = image_url_keys("openai")
    keys_doubao = image_url_keys("doubao")
    keys_qianfan = image_url_keys("qianfan")
    detail_gate_ok = (
        keys_openai == ["detail", "url"]            # OpenAI 官方文档写了 detail
        and keys_doubao == ["url"]                  # 方舟可摘录文档没写 → 少发一个键
        and keys_qianfan == ["url"]                 # 千帆《视觉理解》只文档了 url
    )

    # --- 4) 千帆复核后的形状 ---
    qf = providers["qianfan"]
    qf_spec = qf.spec_for("qianfan", qf.default_models[0])
    qf_body = build_chat_request(
        qf_spec, "bce-v3/x", [{"role": "user", "content": "hi"}],
        temperature=0.6, response_format=None,
    ).payload
    qianfan_ok = bool(
        str(qf.base_url) == "https://qianfan.baidubce.com/v2"
        and str(qf.endpoint_path) == "/chat/completions"
        # 官方页面上两个上限参数都存在，但 max_completion_tokens 明写"支持模型见上下文管理"
        # —— 不是所有模型 → 出厂用无条件文档化的 max_tokens
        and "max_tokens" in qf_body
        and "max_completion_tokens" not in qf_body
        # 16384 来自 ernie-4.5-turbo-vl 的"最大输出 [2，16384]"
        and qf.max_output_tokens == 16384
        and qf.default_models[0] == "ernie-4.5-turbo-vl"
        and qf.default_models[1] in {"ernie-5.0", "ernie-5.1"}
        and qf.sends_image_detail is False
    )

    # --- 5) 密钥识别：宁可认不出，也不猜 ---
    detect_ok = (
        detect_provider_from_key("abc-123-not-a-real-key") is None
        and detect_provider_from_key("bce-v3/ALTAK-x/614fb") == "qianfan"
        # 豆包不在任何前缀的目标里：加了这条断言以后，谁想"顺手加个豆包前缀"
        # 都必须先拿出官方文档里的字面量
        and all(
            detect_provider_from_key(probe) != "doubao"
            for probe in ("sk-abc", "ark-abc", "AKLTabc", "abc.def", "volc-abc")
        )
    )

    record(
        "豆包接入 + 千帆复核（配置字面量逐条对官方文档）",
        bool(doubao_facts_ok and doubao_body_ok and doubao_escalate_ok
             and detail_gate_ok and qianfan_ok and detect_ok),
        f"豆包字面量对文档={doubao_facts_ok}；请求体={sorted(db_body)}；"
        f"抬高上限落对字段={doubao_escalate_ok}；"
        f"image_url 键 openai={keys_openai}/doubao={keys_doubao}/qianfan={keys_qianfan}；"
        f"千帆={sorted(qf_body)} 上限={qf.max_output_tokens}；密钥不猜={detect_ok}",
    )


def test_schema_dialect() -> None:
    """schema 方言：逐家一份；剥掉范围约束之后本地校验必须接得住。

    三条依据（外部评审提出，且都能在代码里指出位置）：
      1. 剥掉 minimum/maximum 之后，"不出错值"只剩本地校验一道防线
         （xmp/validator.py 的 pydantic 边界 + clamp）→ 这里**明确断言它还守着**；
      2. 不支持联合类型的端点（Kimi MFJS）必须能在**生成时**就不产生 anyOf；
      3. 关键字白名单要能逐家加，别为接第五家去改 schema.py。
    """
    from acb.ai import schema as S
    from acb.xmp import fields as F
    from acb.xmp import validator as V

    # --- 1) 默认方言：可空联合存在，取值域也带着（下发前才剥）---
    generated = S.build_response_schema(["f1"], strict=True)
    plain = json.dumps(generated)
    default_ok = (
        '"anyOf"' in plain
        and '["number", "null"]' in plain      # params 用的是 type 数组写法
        and '"minimum"' in plain
    )
    stripped = json.dumps(S.strict_keyword_subset(generated))
    strip_ok = '"minimum"' not in stripped and '"maxItems"' not in stripped and '"anyOf"' in stripped

    # --- 2) 不支持联合的方言：生成时就不产生任何联合写法，且 strict 会退化并说明原因 ---
    no_union = json.dumps(
        S.build_response_schema(["f1"], strict=True, dialect=S.SchemaDialect(allow_anyof=False))
    )
    no_union_ok = (
        '"anyOf"' not in no_union
        and '["number", "null"]' not in no_union
        and '"required"' in no_union            # 顶层 items 仍然必填
    )
    degraded, reason = S.resolve_strict(True, S.SchemaDialect(allow_anyof=False))
    degrade_ok = degraded is False and bool(reason)

    # --- 3) 逐家加关键字（机制可用，接新家只改配置）---
    custom = json.dumps(
        S.strict_keyword_subset(generated, S.SchemaDialect(forbid=frozenset({"enum"})))
    )
    custom_ok = '"enum"' not in custom

    # --- 4) 补偿控制：范围约束的下发被剥掉了，本地校验必须真的拦住越界值 ---
    spec = F.get_field("Highlights2012")
    bad = V.validate_params({"crs:Highlights2012": spec.maximum + 500})
    good = V.validate_params({"crs:Highlights2012": spec.maximum})
    local_ok = (not bad.ok) and bool(bad.errors) and good.ok

    # --- 5) 「可选」≠「可空」：缺字段要被忽略/补齐，空参数必须是**合法答案** ---
    # 外部评审提的这条很便宜也很有用：schema 从"可空联合"退化成"可以省略"之后，
    # 上一层必须真的按"省略"来处理 —— 否则有人把语义改回"可空/必填"时，
    # 这里会立刻变红，而不是等到用户的图被莫名判失败。
    sparse = V.validate_params({"crs:Exposure2012": 0.2})        # 约 90 个字段只给 1 个
    sparse_ok = sparse.ok and sparse.params == {"Exposure2012": 0.2}

    empty = V.validate_params({})                                 # 明确说「不用调」
    empty_ok = empty.ok and any("无需调整" in w for w in empty.warnings)

    missing = V.validate_params(None)                             # 没回答 ≠ 不用调
    missing_ok = (not missing.ok) and bool(missing.errors)

    # 解析器层（真实入口）：故意缺 curves / notes，params 只给一个字段
    from acb.ai.adapter import ModelAdapter, PreviewItem
    from acb.errors import SchemaValidationError

    probe_items = [
        PreviewItem(file_id="f1", filename="a.CR3", path=Path("a.CR3"), preview_jpeg=b"")
    ]
    parsed = ModelAdapter._parse_items_object(
        {"items": [{"file_id": "f1", "params": {"crs:Exposure2012": 0.2}}]}, probe_items
    )
    parser_ok = (
        len(parsed) == 1
        and parsed[0][1].params == {"Exposure2012": 0.2}
        and parsed[0][1].curves == {}
    )
    # 空 params 走完整解析路径也必须通过（不能抛错）
    parsed_empty = ModelAdapter._parse_items_object(
        {"items": [{"file_id": "f1", "params": {}}]}, probe_items
    )
    empty_parser_ok = len(parsed_empty) == 1 and parsed_empty[0][1].ok
    # 而"键缺失"必须仍然是错误 —— 否则真失败会被当成"无需调整"悄悄放过
    try:
        ModelAdapter._parse_items_object({"items": [{"file_id": "f1"}]}, probe_items)
        missing_parser_ok = False
    except SchemaValidationError:
        missing_parser_ok = True

    optional_ok = (
        sparse_ok and empty_ok and missing_ok and parser_ok and empty_parser_ok and missing_parser_ok
    )

    record(
        "schema 方言（逐家剥关键字）；被剥掉的范围约束由本地校验接住",
        bool(default_ok and strip_ok and no_union_ok and degrade_ok and custom_ok and local_ok),
        f"默认可空联合={default_ok} 剥范围约束={strip_ok} 无联合端点={no_union_ok}"
        f"（strict 退化并说明原因={degrade_ok}）逐家禁用关键字={custom_ok}；"
        f"本地拦住越界值={local_ok}"
        f"（{spec.maximum} 合法、{spec.maximum + 500} 被拒：{bad.errors[:1]}）",
    )

    record(
        "「可选」≠「可空」：缺字段被忽略、空参数是合法答案、没回答仍然是错误",
        bool(optional_ok),
        f"只给 1/90 个字段={sparse_ok} 空 params 合法且有警告={empty_ok} "
        f"键缺失仍报错={missing_ok}；解析器层：稀疏响应={parser_ok} "
        f"空 params={empty_parser_ok} 缺 params 抛错={missing_parser_ok}",
    )


def test_model_generation_sort() -> None:
    """模型名排序按"代次"而不是字母序（否则会静默选中旧一代）。

    真实事故：`-` 的码点小于 `3` → `qwen-vl-max` 排在 `qwen3-vl-plus` 前面，
    切到「千问」时自动选中了旧模型，而配置里明明把 Qwen3-VL 写在最前面。
    """
    from acb.config import model_generation, sort_model_ids

    names = ["qwen-vl-max", "qwen3-vl-plus", "qwen3-vl-max", "qwen4-flash"]
    expected = [
        "qwen4-flash",        # 代次 4 → 最前（新的在前）
        "qwen3-vl-max",       # 代次 3，同代次按名字母序
        "qwen3-vl-plus",
        "qwen-vl-max",        # 看不出代次 → 最后
    ]
    order_ok = sort_model_ids(names) == expected

    # 日期不能被当成本代次：`fun-asr-flash-2026-06-15` 里的 06 是"月"，
    # 第一版正则读出了 6，把这个语音模型顶到了所有第 6 代模型前面。
    date_ok = (
        model_generation("fun-asr-flash-2026-06-15") == 0
        and model_generation("gpt-4o-2024-08-06") == 4
        and model_generation("qwen-image-2.0-2026-03-03") == 2
        and model_generation("MiniMax/MiniMax-M2.1") == 2
        and model_generation("o1-mini") == 1
    )

    # 只有年份、没有月份的名字（外部评审提醒："你的正则要求后面必须跟 -\d{2}，
    # 所以 model-2026 会被读成第 2026 代"）。实测**不会**：代次正则要求 1–2 位数字且
    # 前后不能是数字，所以 4 位连排的数字在任何起始位置都匹配不上。
    # 这条断言是为了把"恰好没出错"变成"不许出错"——它是被显式测住的性质，不是运气。
    year_ok = (
        model_generation("model-2026") == 0
        and model_generation("some-model-2026") == 0
        and model_generation("model-2026-abc") == 0
        and model_generation("gpt-4o-2024") == 4          # 有真代次时，年份不影响它
        and model_generation("glm-4.5-flash-2026") == 4
    )

    # 小数版本号（qwen3.5-vl）：只读到 3，不是 3.5。
    # 这符合"数值比较能处理 3 < 4"的预期：顺序仍然稳定，只是"3.5 排在同代次里靠字母序"。
    minor_ok = (
        model_generation("qwen3.5-vl") == 3
        and sort_model_ids(["qwen3.5-vl", "qwen4-flash"]) == ["qwen4-flash", "qwen3.5-vl"]
    )

    record(
        "模型名代次排序（新版在前、同代次按名字；日期不算代次）",
        bool(order_ok and date_ok and year_ok and minor_ok),
        f"排序={sort_model_ids(names)}（期望 {expected}）；日期样本代次正确={date_ok}；"
        f"只有年份的名字不当代次={year_ok}；小数版本号={minor_ok}",
    )


def test_key_store_delete() -> None:
    """删除密钥：内存与密钥链都清掉、且对"来源"说真话。

    为什么必须测"说真话"这一半：环境变量是用户在系统里设的，程序删不掉。
    如果删完只报"已删除"，而下次请求又从环境变量读到同一把密钥，
    用户会以为删除功能坏了 —— 而真相是"你还有一份在环境变量里"。
    （用假 keyring 后端，绝不碰真实的 Windows 凭据管理器。）
    """
    import os

    from acb.keyring_store import KeyStore

    class _FakeKeyring:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def get_password(self, service, account):
            return self.store.get(account)

        def set_password(self, service, account, secret):
            self.store[account] = secret

        def delete_password(self, service, account):
            if account not in self.store:
                raise KeyError("not found")
            del self.store[account]

    store = KeyStore()
    store._keyring_module = _FakeKeyring()          # 换掉真实后端
    os.environ["ACB_SMOKE_ENV_KEY"] = "sk-from-environment"

    store.set("ACB_SMOKE_PLAIN_KEY", "sk-" + "a" * 24)
    store.set("ACB_SMOKE_ENV_KEY", "sk-" + "b" * 24)

    both = store.sources("ACB_SMOKE_PLAIN_KEY")
    env_both = store.sources("ACB_SMOKE_ENV_KEY")

    deleted = store.delete("ACB_SMOKE_PLAIN_KEY")
    after = store.sources("ACB_SMOKE_PLAIN_KEY")
    gone = store.get("ACB_SMOKE_PLAIN_KEY") is None

    # 有环境变量的那一个：删得掉 keyring，但删不掉环境变量 → 必须还剩 env=True
    store.delete("ACB_SMOKE_ENV_KEY")
    env_after = store.sources("ACB_SMOKE_ENV_KEY")
    env_still_readable = store.get("ACB_SMOKE_ENV_KEY") == "sk-from-environment"

    missing = store.delete("ACB_SMOKE_NEVER_SET")

    os.environ.pop("ACB_SMOKE_ENV_KEY", None)

    ok = (
        both["memory"] and both["keyring"] and not both["env"]
        and env_both["env"] and env_both["keyring"]
        and deleted and not any(after.values()) and gone
        and env_after["env"] and not env_after["keyring"] and env_still_readable
        and missing is False
    )
    record(
        "删除密钥：内存+密钥链一起清；环境变量删不掉且必须如实说明",
        bool(ok),
        f"删除前来源={both}（含环境变量的账户={env_both}）；删除后={after} 读不到={gone}；"
        f"仅环境变量时 删除后={env_after} 仍可读到={env_still_readable}；"
        f"删除不存在的账户返回 {missing}",
    )


def test_token_limit_escalation_modes() -> None:
    """空 content / 截断时的"抬高上限"必须跟着 max_tokens_mode 走。

    外部评审批得对：不同家"上限"字段名不同（max_tokens / max_completion_tokens），
    omit 模式下更是一个都不发。若抬高的动作落在服务端**不看**的字段上，
    现象是"请求照样 200，输出却比原来截得更早" —— 不报错、只变差，最难查。

    这里用打桩的 client 跑 adapter 真实的重试分支：
      · omit：**不重发**（重发也是同样结果），并给出"不支持设置上限"的可行动建议；
      · 其它模式：重发一次，且抬高的值落在该模式对应的字段里。
    """
    from acb.ai.adapter import ModelAdapter
    from acb.ai.client import RequestBudget, build_chat_request
    from acb.config import load_models_config
    from acb.errors import EmptyContentError, SchemaValidationError

    cfg = load_models_config()

    class _FakeClient:
        """只实现 _call_with_repair 用到的两个方法，并记录每次的 max_tokens_override。"""

        def __init__(self) -> None:
            self.calls: list[int | None] = []

        def chat(self, messages, *, temperature, response_format=None, max_tokens_override=None):
            self.calls.append(max_tokens_override)
            return {"choices": [{"message": {"content": ""}}]}

        def extract_message_text(self, data):
            # 真实实现会在这里抛 EmptyContentError；桩里直接抛同类异常，
            # 保证走的是生产代码里那条"空 content"分支。
            raise EmptyContentError("响应 content 为空（打桩）")

    def run(provider: str) -> tuple[int, str, dict]:
        preset = cfg.providers[provider]
        spec = preset.spec_for(provider, preset.default_models[0])
        adapter = ModelAdapter(spec, "test-key", RequestBudget(10))
        fake = _FakeClient()
        adapter.client = fake
        schema = {"type": "object", "properties": {}, "required": []}
        message = ""
        try:
            adapter._call_with_repair(
                "system", "user", [], schema,
                temperature=0.6, schema_name="probe", parser=lambda obj: obj,
            )
        except SchemaValidationError as exc:
            message = str(exc)
        last = fake.calls[-1] if fake.calls else None
        payload = build_chat_request(
            spec, "k", [{"role": "user", "content": "hi"}],
            temperature=0.6, response_format=None, max_tokens_override=last,
        ).payload
        return len(fake.calls), message, payload

    # omit（千问）：一次请求就停；文案必须说"不支持设置输出上限"，不能写"已抬高重试"
    qwen_calls, qwen_msg, qwen_payload = run("qwen")
    omit_ok = (
        qwen_calls == 1
        and "不支持设置输出上限" in qwen_msg
        and "已把" not in qwen_msg
        and "max_tokens" not in json.dumps(qwen_payload)
    )

    openai_base = cfg.providers["openai"].max_output_tokens
    oa_calls, oa_msg, oa_payload = run("openai")
    oa_ok = (
        oa_calls == 2
        and oa_payload.get("max_completion_tokens") == openai_base * 2
        and "max_tokens" not in oa_payload
        and "max_completion_tokens" in oa_msg
    )

    ds_base = cfg.providers["deepseek"].max_output_tokens
    ds_calls, ds_msg, ds_payload = run("deepseek")
    ds_ok = (
        ds_calls == 2
        and ds_payload.get("max_tokens") == ds_base * 2
        and "max_completion_tokens" not in ds_payload
        and "max_tokens" in ds_msg
    )

    # 豆包（火山方舟）：官方表里两个上限参数都在，我们选的是 max_completion_tokens
    # （"总输出长度"，豆包自带深度思考，思维链也吃预算）→ 抬高必须落在它身上。
    db_base = cfg.providers["doubao"].max_output_tokens
    db_calls, db_msg, db_payload = run("doubao")
    db_ok = (
        db_calls == 2
        and db_payload.get("max_completion_tokens") == db_base * 2
        and "max_tokens" not in db_payload
        and "max_completion_tokens" in db_msg
    )

    record(
        "抬高输出上限：字段名跟着 max_tokens_mode；omit 不空转重发",
        bool(omit_ok and oa_ok and ds_ok and db_ok),
        f"omit（千问）请求次数={qwen_calls}（应为 1）文案含「不支持设置输出上限」="
        f"{'不支持设置输出上限' in qwen_msg}；"
        f"openai 次数={oa_calls} 抬高到 {oa_payload.get('max_completion_tokens')}（{openai_base}×2）；"
        f"deepseek 次数={ds_calls} 抬高到 {ds_payload.get('max_tokens')}（{ds_base}×2）；"
        f"doubao 次数={db_calls} 抬高到 {db_payload.get('max_completion_tokens')}（{db_base}×2）",
    )


def test_sidecar_backup() -> None:
    """覆盖已有侧车之前必须留一份原件（用户反馈过"覆盖掉就回不来了"）。"""
    import os

    from acb.paths import data_root
    from acb.xmp import writer as W

    old_appdata = os.environ.get("APPDATA")
    with tempfile.TemporaryDirectory() as tmp:
        # 备份落在 <数据目录>/backups/xmp/ → 必须把 %APPDATA% 指到临时目录，
        # 否则会往用户真实数据目录里塞测试产物（这条纪律已经踩过一次）。
        os.environ["APPDATA"] = tmp
        try:
            work = Path(tmp) / "photos"
            work.mkdir()
            raw = work / "a.CR3"
            raw.write_bytes(b"raw")
            side = W.sidecar_path_for(raw)
            original = (
                '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">\n'
                ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
                '  <rdf:Description rdf:about="" crs:Exposure2012="+1.00"'
                ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"/>\n'
                ' </rdf:RDF>\n</x:xmpmeta>\n'
            )
            side.write_text(original, encoding="utf-8")

            first = W.write_sidecar(raw, {"Highlights2012": -15}, None)
            backup_dir = data_root() / "backups" / "xmp"
            backups = sorted(backup_dir.glob("*.xmp"))
            first_ok = (
                len(backups) == 1
                and backups[0].read_text(encoding="utf-8") == original
                and any("备份" in w for w in first.warnings)
            )

            # 第二次写：备份**不能被覆盖**（否则拿到的就是改过的版本，失去意义）
            W.write_sidecar(raw, {"Highlights2012": -30}, None)
            backups2 = sorted((data_root() / "backups" / "xmp").glob("*.xmp"))
            second_ok = (
                len(backups2) == 1
                and backups2[0].read_text(encoding="utf-8") == original
            )
        finally:
            if old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old_appdata

    record(
        "覆盖已有侧车前先备份原件（且只备一次）",
        bool(first_ok and second_ok),
        f"首次备份正确={first_ok}；二次不覆盖备份={second_ok}",
    )


def test_mask_coordinate_frame() -> None:
    """蒙版坐标的"横竖方向"红线（用户 2026-09-23：批量里不能出这种问题）。

    四件事：
      1. 四种 EXIF 方向下，显示帧 ↔ 存储帧换算与实测/手算一致，且往返可逆；
      2. 整份蒙版几何（线性 / 径向 / 画笔）换算同样可逆；
      3. 镜像方向必须**拒绝**（抛错），不许猜一个方向把蒙版画到错地方；
      4. 写蒙版时必须给出照片方向；只有显式声明 masks_frame="stored" 才允许原样写。
    """
    from acb.raw import orientation as O
    from acb.xmp import masks as M
    from acb.xmp import writer as W

    ok = True
    detail: list[str] = []

    # 1) "Rotate 90 CW" 那组是 2026-09-23 在 ACR 里实测验证过的（④ 落在天空上）
    for label, display, stored in (
        ("Rotate 90 CW", (0.5, 0.28), (0.28, 0.5)),
        ("Rotate 270 CW", (0.5, 0.28), (0.72, 0.5)),
        ("Rotate 180", (0.5, 0.28), (0.5, 0.72)),
        ("Horizontal (normal)", (0.5, 0.28), (0.5, 0.28)),
    ):
        got = O.display_to_stored_point(display, label)
        back = O.stored_to_display_point(got, label)
        same = got == stored and back == display
        ok = ok and same
        detail.append(f"{label} {'✓' if same else f'✗ {display}→{got}（预期 {stored}）'}")

    # 2) 整份几何的往返
    radial = {"type": "radial", "rect": (0.1, 0.2, 0.9, 0.4), "angle": 0.0}
    radial_back = M.stored_to_display_spec(M.display_to_stored_spec(radial, "Rotate 270 CW"), "Rotate 270 CW")
    radial_ok = radial_back["rect"] == radial["rect"] and abs(radial_back["angle"] - radial["angle"]) < 1e-9
    brush = {"type": "brush", "dabs": [(0.2, 0.3), (0.7, 0.8)]}
    brush_back = M.stored_to_display_spec(M.display_to_stored_spec(brush, "Rotate 90 CW"), "Rotate 90 CW")
    brush_ok = brush_back["dabs"] == brush["dabs"]
    ok = ok and radial_ok and brush_ok
    detail.append(f"径向往返{'✓' if radial_ok else '✗'}；画笔落点往返{'✓' if brush_ok else '✗'}")

    # 3) 镜像必须拒绝
    try:
        O.normalize("Mirror horizontal")
        mirror_ok = False
    except O.OrientationError:
        mirror_ok = True
    ok = ok and mirror_ok
    detail.append(f"镜像拒绝{'✓' if mirror_ok else '✗'}")

    # 4) 缺方向不许写蒙版；显式声明存储帧才允许
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "a.CR3"
        raw.write_bytes(b"raw")
        guard_ok = False
        try:
            W.write_sidecar(raw, {}, None,
                            masks=[{"type": "linear", "zero": (0.5, 1.0), "full": (0.5, 0.0)}])
        except ValueError:
            guard_ok = True
        result = W.write_sidecar(raw, {}, None,
                                 masks=[{"type": "linear", "zero": (0.5, 1.0), "full": (0.5, 0.0)}],
                                 masks_frame="stored")
        text = result.target.read_text(encoding="utf-8") if result.target.is_file() else ""
        stored_ok = 'crs:ZeroY="1"' in text and 'crs:FullY="0"' in text
    ok = ok and guard_ok and stored_ok
    detail.append(f"缺方向报错{'✓' if guard_ok else '✗'}；声明存储帧照写{'✓' if stored_ok else '✗'}")

    # 5) 真实照片读方向（有 test_data 才跑）
    real = Path(__file__).resolve().parent.parent / "test_data" / "IMG_2509.CR3"
    if real.is_file():
        from acb.raw.exiftool import ExiftoolRunner

        photo = O.read_orientation(real, ExiftoolRunner())
        read_ok = photo.label == "Rotate 90 CW" and photo.portrait
        ok = ok and read_ok
        detail.append(f"真实照片方向 {photo.label}{'✓' if read_ok else '✗'}")
    else:
        detail.append("真实照片方向（无 test_data，跳过）")

    record("蒙版坐标的横竖方向（换算/拒绝镜像/缺方向报错）", ok, "；".join(detail))


# ---------------------------------------------------------------------------
# 9. 离线调试（不联网也能验证整条链路）
# ---------------------------------------------------------------------------
def test_offline_mock() -> None:
    from acb.ai.offline import MOCK_CURVES, MOCK_PARAMS, MOCK_TRAINING, build_mock_verdict
    from acb.xmp import fields as F

    try:
        verdict = build_mock_verdict()
    except Exception as exc:
        record("离线调试：假结果结构与取值合法", False, str(exc))
        return

    # 门禁 1：假结果必须通过**生产用的同一个**校验器且无 error。
    #   warnings 允许存在（例如白平衡联动提示）——真实链路也会有，
    #   要求它有反而说明两边不同构。
    valid = verdict.ok

    # 门禁 2：字段名必须合法，但要区分两条通道。
    #   属性通道（MOCK_PARAMS）：必须是已登记且**允许 AI 写入**的字段。
    #   曲线通道（MOCK_CURVES）：必须是 CURVE_FIELDS 里的元素型字段。
    #       曲线字段的 ai_writable 是 False，这是**正常**的——它们以 XML 元素
    #       而非属性形式存在，validator 也在 ai_writable 检查之前就把它们
    #       分流到 result.curves 了。把两条通道混在一起判会误报。
    bad_fields = [
        name
        for name in MOCK_PARAMS
        if (spec := F.get_field(name)) is None or not spec.ai_writable
    ] + [name for name in MOCK_CURVES if name not in F.CURVE_FIELDS]

    # 门禁 3：取值必须在各自范围内。
    #   校验器会"夹取"轻微越界值，所以这里显式再查一遍，
    #   避免"靠容差勉强通过"掩盖真实越界。
    out_of_range = [
        name
        for name, value in verdict.params.items()
        if (spec := F.get_field(name)) is not None and F.clamp(spec, value) != value
    ]

    # 门禁 4：曲线必须已被归一化到 ACR 的 0..255 整数域。
    #   提示词里约定模型给 0..1 浮点，换算发生在 writer 侧，
    #   这一步走错会让曲线整条变形（但不会报错），属于静默故障。
    curve = verdict.curves.get("ToneCurvePV2012") or []
    curve_ok = (
        len(curve) >= 2
        and all(isinstance(pair[0], int) and isinstance(pair[1], int) for pair in curve)
        and all(0 <= value <= 255 for pair in curve for value in pair)
    )

    # 门禁 5：训练模式的假画像必须满足 schema 的 required 与非空 text_rules，
    #   否则一切到训练模式就直接抛错。
    training_required = {
        "style_summary", "text_rules", "saturation_tendency", "contrast_tendency",
        "shadow_color_cast", "hsl_habits", "lens_correction_preference",
        "excluded_reasoning",
    }
    training_ok = (
        training_required <= set(MOCK_TRAINING) and bool(MOCK_TRAINING["text_rules"])
    )

    record(
        "离线调试：假结果结构与取值合法",
        valid and not bad_fields and not out_of_range and curve_ok and training_ok,
        f"校验通过={valid} 参数字段={len(verdict.params)} 曲线点={len(curve)} "
        f"非法字段={bad_fields or '无'} 越界={out_of_range or '无'} "
        f"训练画像完整={training_ok} 警告={len(verdict.warnings)} 条",
    )


def test_offline_adapter() -> None:
    """离线适配器必须满足 pipeline 真正会用到的那几个成员。

    这是"换掉 adapter 就能跑通整条链路"这一设计的守门测试：
    一旦有人给 pipeline 加了新的 adapter 调用点而忘了同步离线替身，
    这里就会失败，而不是等到用户勾了「离线调试」才发现。
    """
    from pathlib import Path

    from acb.ai.adapter import PreviewItem
    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config

    try:
        config = load_models_config()
        adapter = OfflineAdapter(config.active_spec())

        items = [
            PreviewItem(
                file_id=f"f{index}",
                filename=f"a{index}.CR3",
                path=Path(f"a{index}.CR3"),
                preview_jpeg=b"",
            )
            for index in range(3)
        ]
        results = adapter.analyze_group(
            items, style_block="", user_prompt="", batch_stats=None, auto_match=False
        )

        # pipeline 用到的成员逐个确认存在且可用（见 output_mode 的调用点）
        members_ok = (
            isinstance(adapter.describe(), str)
            and adapter.supports_vision is True
            and adapter.supports_json_schema is True
            and hasattr(adapter, "spec")
            and hasattr(adapter.client, "budget")
            and isinstance(adapter.client.usage_summary(), str)
        )

        # 返回的数量与顺序必须与请求一致（pipeline 按 file_id 回填结果）
        results_ok = (
            len(results) == len(items)
            and [r.file_id for r in results] == [i.file_id for i in items]
            and all(r.ok for r in results)
            and all(r.params and r.curves for r in results)
        )

        # 预算占位必须能被 pipeline 覆盖（output_mode 会用真实文件数重建）
        adapter.client.budget = "REBUILT"
        budget_ok = adapter.client.budget == "REBUILT"

        training_ok = bool(adapter.analyze_training(items, "测试风格").get("text_rules"))

        record(
            "离线调试：适配器满足 pipeline 的调用契约",
            members_ok and results_ok and budget_ok and training_ok,
            f"成员齐全={members_ok} 结果对齐={results_ok} "
            f"预算可覆盖={budget_ok} 训练可用={training_ok}",
        )
    except Exception as exc:
        record("离线调试：适配器满足 pipeline 的调用契约", False, str(exc))


# ---------------------------------------------------------------------------
# 10. 重试策略（硬约束 #7 / #18）
# ---------------------------------------------------------------------------
def test_retry_policy() -> None:
    """重试策略常量之间的**对齐约束**必须成立。

    这些常量不是各自独立的，constants.py 里写明了：
        MAX_ATTEMPTS_PER_FILE = 1 次正常 + MAX_PARSE_RETRIES 次重试
        BUDGET_MULTIPLIER     = 同上
    一旦只改其中一个（例如把重试调回 3 却忘了抬永久跳过阈值），
    就会出现"重试还有名额但文件已被永久跳过"的矛盾状态——
    而这种不一致在运行期极难察觉，只能靠这题守住。
    """
    from acb.config import ModelSpec
    from acb.constants import (
        BUDGET_MULTIPLIER,
        CONNECT_TIMEOUT_S,
        MAX_ATTEMPTS_PER_FILE,
        MAX_PARSE_RETRIES,
        REQUEST_TIMEOUT_S,
    )

    attempts_ok = MAX_ATTEMPTS_PER_FILE == MAX_PARSE_RETRIES + 1
    budget_ok = BUDGET_MULTIPLIER == MAX_PARSE_RETRIES + 1

    # 连接超时必须 ≤ 总超时，否则"总超时 N 秒"是句空话：
    # 目标地址不可达时会先耗掉更长的连接超时。
    timeout_ok = CONNECT_TIMEOUT_S <= REQUEST_TIMEOUT_S

    # config 的**字段默认值**必须取自常量。
    # 这里刻意查字段默认值而不是查加载后的 spec 值：
    # 用户被允许在 models.yaml 里覆盖它（真实 API 需要 20–60 秒），
    # 那种覆盖是合法的，不该让自检失败；要守的是"代码里的默认值别写死第二份"。
    default_ok = (
        ModelSpec.model_fields["request_timeout_s"].default == REQUEST_TIMEOUT_S
    )

    record(
        "重试策略：常量间的对齐约束",
        attempts_ok and budget_ok and timeout_ok and default_ok,
        f"超时={REQUEST_TIMEOUT_S}s/连接={CONNECT_TIMEOUT_S}s "
        f"重试={MAX_PARSE_RETRIES} 永久跳过={MAX_ATTEMPTS_PER_FILE}"
        f"（应={MAX_PARSE_RETRIES + 1}）预算倍数={BUDGET_MULTIPLIER}"
        f"（应={MAX_PARSE_RETRIES + 1}）默认值同源={default_ok}",
    )




# ---------------------------------------------------------------------------
# 11. 全量重跑与进度条（两项都来自真实反馈）
# ---------------------------------------------------------------------------
def test_repeat_and_progress(raw_dir: Path | None) -> None:
    """默认必须能重复处理；进度条在 Photoshop 阶段之前不得满格。

    为什么需要真实 RAW：这两件事都要跑完整链路才能验证 ——
    断点筛选发生在扫描之后，进度条的分母也由实际待处理数决定。

    两个真实反馈：
      1. 「开始」和「续跑」连的是同一段 lambda（都传 resume=True），
         用户点「开始」也被跳过已完成文件，无法重跑；
      2. 进度分母只算到「写出 XMP」，Photoshop 刚开工进度条就已经 100%。
    """
    if not raw_dir or not raw_dir.is_dir():
        record("全量重跑与进度（未提供 --raw-dir，跳过实测）", True, "未发现样本，跳过", skipped=True)
        return

    candidates: list[Path] = []
    for pattern in ("*.CR3", "*.CR2", "*.NEF", "*.ARW", "*.DNG", "*.ORF", "*.RAF"):
        candidates.extend(sorted(raw_dir.rglob(pattern)))
    if not candidates:
        record("全量重跑与进度（无可用 RAW，跳过实测）", True, "未发现样本，跳过", skipped=True)
        return

    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.pipeline.job import JobState
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.exiftool import ExiftoolRunner

    source = candidates[0]

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "photos"
        work.mkdir()
        shutil.copy2(source, work / source.name)
        sidecar = source.with_suffix(".xmp")
        if sidecar.is_file():
            shutil.copy2(sidecar, work / sidecar.name)

        config = load_models_config()
        adapter = OfflineAdapter(config.active_spec())
        exiftool = ExiftoolRunner()
        state = JobState(directory=Path(tmp) / "state")

        # 进度上报时记录"当时处于哪个阶段"，才能判断是否提前满格。
        current_stage = ["(未开始)"]
        events: list[tuple[str, int, int]] = []

        callbacks = JobCallbacks(
            log=lambda message, level: None,
            progress=lambda done, total: events.append((current_stage[0], done, total)),
            stage=lambda text: current_stage.__setitem__(0, text),
            item_status=lambda name, status: None,
            should_stop=lambda: False,
        )

        def run(resume: bool):
            events.clear()
            current_stage[0] = "(未开始)"
            opts = OutputOptions(
                sources=[work],
                prompt="",
                resume=resume,
                # 不启动 Photoshop：本项只验进度与续跑语义，不碰外部程序。
                run_photoshop=False,
                use_cache=False,
                workers=1,
            )
            return run_output_mode(
                opts,
                adapter=adapter,
                exiftool=exiftool,
                job_state=state,
                callbacks=callbacks,
            )

        try:
            first = run(resume=False)
            second = run(resume=True)
            third = run(resume=False)
        except Exception as exc:
            record("重复处理：默认重跑，续跑才跳过", False, f"{type(exc).__name__}: {exc}")
            return

        repeat_ok = first.done == 1 and third.done == 1 and third.skipped == 0
        resume_ok = second.done == 0 and second.skipped == 1
        record(
            "重复处理：默认重跑，续跑才跳过",
            repeat_ok and resume_ok,
            f"首次 done={first.done}/skipped={first.skipped}；"
            f"续跑 done={second.done}/skipped={second.skipped}；"
            f"再重跑 done={third.done}/skipped={third.skipped}",
        )

        # 满格只能出现在最后一步（stage 为"完成"）。
        early_full = [
            (stage, done, total)
            for stage, done, total in events
            if total > 0 and done * 100 // total >= 100 and stage != "完成"
        ]
        # 收尾阶段（生成脚本）必须落在未满格的位置 —— 这正是"Photoshop 还在导出、
        # 进度条却已经 100%"那个问题的守门断言。
        post_positions = [
            (done, total) for stage, done, total in events if stage == "生成 Photoshop 脚本"
        ]
        post_ok = bool(post_positions) and all(
            done * 100 // total < 100 for done, total in post_positions
        )
        record(
            "进度条：收尾阶段之前不得满格",
            not early_full and post_ok,
            f"提前满格={early_full or '无'}；生成脚本时={post_positions or '无记录'}；"
            f"共 {len(events)} 次上报",
        )




# ---------------------------------------------------------------------------
# 12. 退出清理的安全性（本项目唯一会删文件的功能）
# ---------------------------------------------------------------------------
def test_cleanup_safety() -> None:
    """退出清理只能删垃圾，绝不能碰重要文件与用户照片。

    为什么必须在**真实的临时数据目录**里跑一遍，而不是只读代码：
    这是本项目唯一会删用户文件的功能，一次误删就是数据损失。
    测试把 %APPDATA% 指向临时目录，于是 data_root() 完全被隔离，
    可以在里面造出真实的结构，然后逐项断言"该删的删了、该留的还在"。

    顺带守住一个已经犯过的错误：白名单判定若只比较"路径是否等于白名单目录"，
    就会把白名单目录里的**子文件**一起拒掉 —— 清理静默地什么都不做。
    """
    import os

    from acb import cleanup as C
    from acb import paths as P

    old_appdata = os.environ.get("APPDATA")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["APPDATA"] = tmp
            root = P.data_root()

            # --- 造出真实的数据目录结构 ---
            (root / "cache" / "thumbs").mkdir(parents=True)
            (root / "cache" / "thumbs" / "a.jpg").write_bytes(b"x" * 100)
            (root / "cache" / "thumbs" / "b.jpg").write_bytes(b"y" * 50)
            (root / "logs").mkdir(parents=True, exist_ok=True)
            (root / "logs" / "app-2026-09-19.log").write_text("app log", encoding="utf-8")
            # 两次历史运行的脚本目录：三件套 + Photoshop 写出的日志与结果。
            old_run = root / "runs" / "20260101_120000_aaaa"
            old_run.mkdir(parents=True)
            for name in ("manifest.json", "ps_result.json"):
                (old_run / name).write_text("{}", encoding="utf-8")
            for name in ("export_batch.jsx", "run_export.bat", "ps_log.txt"):
                (old_run / name).write_text("x", encoding="utf-8")
            (root / "runs" / "20260102_130000_bbbb").mkdir(parents=True)
            (root / "runs" / "20260102_130000_bbbb" / "manifest.json").write_text(
                "{}", encoding="utf-8"
            )
            (root / "state" / "_tmp_xmp").mkdir(parents=True)
            (root / "state" / "_tmp_xmp" / "packet_x.xmp").write_text("t", encoding="utf-8")
            (root / "state" / "records.json").write_text("{}", encoding="utf-8")
            (root / "state" / "failed.json").write_text("{}", encoding="utf-8")
            (root / "styles").mkdir()
            (root / "styles" / "default_neutral.json").write_text("{}", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "models.yaml").write_text("active: x", encoding="utf-8")

            # 数据目录之外的用户照片目录
            photos = Path(tmp) / "photos"
            photos.mkdir()
            (photos / "IMG_0001.CR3").write_bytes(b"raw")
            (photos / "IMG_0001.xmp").write_text("<x/>", encoding="utf-8")

            # --- 断言 1：守卫必须拦下所有危险路径 ---
            must_reject = {
                "数据目录本身": root,
                "用户照片目录": photos,
                "用户 RAW": photos / "IMG_0001.CR3",
                "用户 XMP": photos / "IMG_0001.xmp",
                "风格档案": root / "styles" / "default_neutral.json",
                "模型配置": root / "config" / "models.yaml",
                "断点记录": root / "state" / "records.json",
                "失败记录": root / "state" / "failed.json",
            }
            leaked = [
                name for name, path in must_reject.items() if C._guard(path) is None
            ]

            # --- 断言 2：白名单目录的**子项**必须放行（否则清理等于没做）---
            must_allow = {
                "缓存文件": root / "cache" / "thumbs" / "a.jpg",
                "运行目录里的清单": old_run / "manifest.json",
                "运行目录里的脚本": old_run / "export_batch.jsx",
                "XMP 临时文件": root / "state" / "_tmp_xmp" / "packet_x.xmp",
            }
            blocked = [
                name for name, path in must_allow.items() if C._guard(path) is not None
            ]

            # --- 断言 3：跑一次真实清理 ---
            report = C.cleanup_session_junk()

            removed_ok = (
                not (root / "cache" / "thumbs" / "a.jpg").exists()
                and not (root / "cache" / "thumbs" / "b.jpg").exists()
                and not (root / "state" / "_tmp_xmp" / "packet_x.xmp").exists()
                # 运行目录是**整个删掉**的（脚本三件套 + PS 日志与结果全在里面）
                and not old_run.exists()
                and not (root / "runs" / "20260102_130000_bbbb").exists()
                # 目录本身保留（避免"清理完目录不见了"的困惑）
                and (root / "cache" / "thumbs").is_dir()
                and (root / "runs").is_dir()
            )

            # --- 断言 3b：keep_run_dir 指定的那一次必须**保留** ---------------
            # 本次导出没成功时界面会把它传进来：用户还要手动双击 run_export.bat
            # 补跑，把脚本删掉等于断了他的后路 —— 那是最不该发生的"贴心清理"。
            keep = root / "runs" / "keep_me"
            other = root / "runs" / "delete_me"
            for one in (keep, other):
                one.mkdir(parents=True)
                for name in ("manifest.json", "ps_result.json"):
                    (one / name).write_text("{}", encoding="utf-8")
                for name in ("export_batch.jsx", "run_export.bat", "ps_log.txt"):
                    (one / name).write_text("x", encoding="utf-8")
            C.cleanup_session_junk(keep_run_dir=keep)
            keep_ok = (
                (keep / "manifest.json").is_file()
                and (keep / "export_batch.jsx").is_file()
                and (keep / "run_export.bat").is_file()
                and (keep / "ps_log.txt").is_file()
                and not other.exists()
            )

            # --- 断言 4：重要文件与用户照片必须原封不动 ---
            survivors = {
                "风格档案": root / "styles" / "default_neutral.json",
                "模型配置": root / "config" / "models.yaml",
                "断点记录": root / "state" / "records.json",
                "失败记录": root / "state" / "failed.json",
                "程序日志": root / "logs" / "app-2026-09-19.log",
                "用户 RAW": photos / "IMG_0001.CR3",
                "用户 XMP": photos / "IMG_0001.xmp",
            }
            lost = [name for name, path in survivors.items() if not path.exists()]

        ok = (
            not leaked
            and not blocked
            and removed_ok
            and keep_ok
            and not lost
            and not report.skipped
        )
        record(
            "退出清理：只删垃圾，重要文件与用户照片不动",
            ok,
            f"守卫漏放={leaked or '无'}；应放行却被拦={blocked or '无'}；"
            f"垃圾已清={removed_ok}；保留待补跑的运行目录={keep_ok}；"
            f"丢失={lost or '无'}；{report.describe()}",
        )
    finally:
        if old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_appdata


# ---------------------------------------------------------------------------
# 13. 产物位置的集成验证（脚本三件套不许进出片目录）
# ---------------------------------------------------------------------------
def test_artifact_locations(raw_dir: Path | None) -> None:
    """脚本产物必须落在 <数据目录>/runs/ 下，出片目录里只剩照片。

    真实反馈：离线演练之后，源目录里多出了 export_batch.jsx、manifest.json、
    run_export.bat 三个文件，还凭空多了一个 _export_dryrun 文件夹。

    这是**集成层**的门禁：test_jsx_products 只验证 render_export_outputs 本身
    （给它一个 script_dir，看它往哪写），而"run_output_mode 究竟把
    result.export_dir 和 result.script_dir 接线成了什么"只有真跑一遍才知道。
    接线错了照样能过单元断言，这正是需要这一项的理由。
    """
    if not raw_dir or not raw_dir.is_dir():
        record("产物位置（未提供 --raw-dir，跳过实测）", True, "未发现样本，跳过", skipped=True)
        return

    candidates: list[Path] = []
    for pattern in ("*.CR3", "*.CR2", "*.NEF", "*.ARW", "*.DNG", "*.ORF", "*.RAF"):
        candidates.extend(sorted(raw_dir.rglob(pattern)))
    if not candidates:
        record("产物位置（无可用 RAW，跳过实测）", True, "未发现样本，跳过", skipped=True)
        return

    # 挑最小的那一个：本项验的是"文件落在哪"，与像素多少无关。
    source = min(candidates, key=lambda p: p.stat().st_size)

    import os  # 本文件其余部分不直接用 os，所以只在需要隔离 APPDATA 时导入

    import acb.cleanup as C
    from acb.ai.offline import OfflineAdapter
    from acb.config import load_models_config
    from acb.paths import runs_dir
    from acb.pipeline.job import JobState
    from acb.pipeline.output_mode import JobCallbacks, OutputOptions, run_output_mode
    from acb.raw.exiftool import ExiftoolRunner

    artifacts = {"export_batch.jsx", "manifest.json", "run_export.bat"}
    old_appdata = os.environ.get("APPDATA")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # 数据目录也隔离到临时区：本项会**真的**产生一个运行目录，
            # 绝不能污染用户真实的 %APPDATA%\AutoColorDiffusion\runs\。
            os.environ["APPDATA"] = str(Path(tmp) / "appdata")

            work = Path(tmp) / "photos"
            work.mkdir()
            shutil.copy2(source, work / source.name)
            sidecar = source.with_suffix(".xmp")
            if sidecar.is_file():
                shutil.copy2(sidecar, work / sidecar.name)

            config = load_models_config()
            try:
                result = run_output_mode(
                    OutputOptions(
                        sources=[work],
                        prompt="",
                        # 刻意用**默认**选项：默认行为就是"JPG 与 RAW 同层"，
                        # 本项要守的正是这个默认值不被悄悄改回去。
                        run_photoshop=False,   # 不碰外部程序
                        use_cache=False,
                        workers=1,
                    ),
                    adapter=OfflineAdapter(config.active_spec()),
                    exiftool=ExiftoolRunner(),
                    job_state=JobState(directory=Path(tmp) / "state"),
                    callbacks=JobCallbacks(),
                )
            except Exception as exc:
                record("产物位置：脚本在 runs/ 下、出片目录只剩照片", False,
                       f"{type(exc).__name__}: {exc}")
                return

            names = {p.name for p in work.iterdir()}

            # 0) 跳过 Photoshop 时，**结构化字段** ps_ok 必须是明确的 False。
            #    界面（_manual_export_needed）与命令行（_cleanup_run_artifacts）
            #    都靠它判断"要不要保留脚本目录给用户手动补跑"。
            #    【为什么必须是结构化字段】真实事故：导出失败时状态文案是
            #    "完成（成功 0/1，失败 1）"，含"成功"二字，
            #    用 `"成功" not in ps_status` 判断会把结果骗反、把 runs/ 删掉。
            skip_status_ok = result.ps_ok is False and "跳过" in result.ps_status

            # 1) 出片目录 = 源目录本身，且里面**没有**任何脚本产物
            source_clean = result.export_dir == work and not (artifacts & names)
            # 2) 也不许凭空多出 _export / _export_dryrun 子目录
            #    （真实反馈里那个多余的 _export_dryrun 文件夹就是这条）
            no_stray_dirs = not any(
                (work / name).exists() for name in ("_export", "_export_dryrun")
            )
            # 3) 脚本目录在 <数据目录>/runs/ 下，三件套齐全且都在那里
            script_ok = (
                result.script_dir is not None
                and result.script_dir.parent == runs_dir()
                and result.jsx_path.parent == result.script_dir
                and result.manifest_path.parent == result.script_dir
                and result.bat_path.parent == result.script_dir
                and all((result.script_dir / name).is_file() for name in artifacts)
            )
            # 4) manifest 里每一项的 out 都指向源目录（同层），不是 _export 子目录
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            outs_ok = bool(manifest["items"]) and all(
                Path(entry["out"]).parent == work for entry in manifest["items"]
            )
            # 5) 退出清理：需要手动补跑时保留，用完了就删掉
            C.cleanup_session_junk(keep_run_dir=result.script_dir)
            kept = (result.script_dir / "manifest.json").is_file()
            C.cleanup_session_junk()
            gone = not result.script_dir.exists()

            ok = (
                skip_status_ok
                and source_clean
                and no_stray_dirs
                and script_ok
                and outs_ok
                and kept
                and gone
            )
            record(
                "产物位置：脚本在 runs/ 下、出片目录只剩照片、退出时按需清理",
                ok,
                f"跳过 Photoshop 的 ps_status={result.ps_status!r}（需写明跳过）={skip_status_ok}；"
                f"出片目录={result.export_dir}；脚本目录={result.script_dir}；"
                f"源目录里的三件套={sorted(artifacts & names) or '无'}；"
                f"多余子目录={not no_stray_dirs}；out 全部同层={outs_ok}；"
                f"需补跑时保留={kept}；用完清理={gone}",
            )
    finally:
        if old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_appdata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto Color Diffusion 非 AI 全链路自检")
    parser.add_argument("--raw-dir", type=Path, default=None,
                        help="含有真实 RAW / XMP 的目录，用于实测预览提取与 XMP 往返")
    args = parser.parse_args(argv)

    print("=" * 90)
    print("Auto Color Diffusion 非 AI 全链路自检")
    print("=" * 90)
    print("说明：本自检不联网、不调用 AI、不改动你的照片。")
    print("      第 4–6 项在系统临时目录里操作，结束后自动清理。")
    print("=" * 90 + "\n")

    test_paths()
    print()

    # 真实数据目录的**基线快照**：本轮自检结束后必须零新增、零改动。
    # 这是常驻断言（不只是排查污染时才跑）：任何"往真实目录写东西"的新用例
    # 都会立刻变红，而不是等用户发现数据目录里多了一堆东西。
    _baseline = _datadir_snapshot()

    # 从这一行起，全部用例都在**隔离的 %APPDATA%** 下运行。
    #
    # 为什么要在 main 里做、而不是每个用例自己管：
    #   · test_paths 必须看真实路径（它验的就是"默认落在 %APPDATA%"），所以放在它之后；
    #   · 其余用例一旦碰数据目录就会留下痕迹 —— 实测踩过两次：
    #     1) 覆盖侧车前的备份功能上线后，每次跑自检都往
    #        <用户数据目录>/backups/xmp/ 里塞 a/foreign/legacy/_G4A0751 这些测试产物；
    #     2) 加载配置时回填能力字段会重写用户的 config/models.yaml。
    #   一个个用例去补隔离，迟早漏一个（漏了还看不出来）。
    #   这里一次兜住：data_root() 变成临时目录，任何"不小心写到真实数据目录"的代码
    #   都无处可写。用 atexit 还原，是为了连 `return 1` 的提前退出也覆盖到。
    import os
    import atexit

    _isolated_appdata = tempfile.TemporaryDirectory(prefix="acb_smoke_appdata_")
    _old_appdata = os.environ.get("APPDATA")
    _isolate_ok = not _real_datadir_requested()
    if _isolate_ok:
        os.environ["APPDATA"] = str(Path(_isolated_appdata.name) / "appdata")
        print(f"（自检期间 %APPDATA% 已隔离到 {os.environ['APPDATA']}，不会碰你的真实数据目录）\n")
    else:
        # 逃生舱：要验"旧配置迁移 / 能力值修正"这类**必须落在真实配置上**的行为时，
        # 隔离会把被测对象本身挡住。设 ACB_TEST_REAL_DATADIR=1 才走这条路，
        # 并且**只允许**在人工、一次性排查时用（自动化里永远不设）。
        print(
            "（⚠ ACB_TEST_REAL_DATADIR=1：本次直接用**真实数据目录**，"
            "用例可能改动你的配置/数据目录，仅限人工排查使用）\n"
        )

    def _restore_appdata() -> None:
        if _old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = _old_appdata
        _isolated_appdata.cleanup()

    atexit.register(_restore_appdata)

    test_config()
    test_color_management()
    test_config_provider_upgrade()
    test_connection_fact_refresh()
    test_discover_models()
    test_model_echo_check()
    test_api_style_dispatch()
    test_output_format_tiers()
    test_model_capability_overrides()
    test_output_escalation_and_endpoint_memory()
    print()
    test_style_prompt_priority()
    test_dimension_levels()
    test_scene_rules_and_wb_offset()
    test_output_guardrails()
    test_lens_baseline_and_program_fields()
    test_mask_pipeline_integration()
    test_mask_geometry_tolerance()
    test_style_expects_masks_but_none_returned()
    test_train_budget_allows_retry()
    test_process_version_is_written()
    test_stop_cancels_retries()
    test_stop_cancels_pending_groups()
    test_adaptive_concurrency_shrink()
    test_icon_assets()
    test_icon_from_source_framing()
    test_packaging_inventory_is_seed_only()
    test_release_tool_derives_version()
    test_exiftool_invocation_prefers_perl()
    test_token_stats_parsing()
    test_train_mode_offline_end_to_end()
    test_local_range_two_layers()
    test_timeout_escalation_and_config_upgrade()
    test_fixed_temperature_selfheal()
    test_rate_limit_gate()
    test_kimi_tier_switch()
    test_quota_429_is_not_rate_limit()
    test_jsx_editor_false_positive()
    test_permanent_skip_is_reported()
    test_regression_no_copy_and_guards()
    test_mask_structure_roundtrip()
    test_end_to_end_offline_write()
    test_builtin_styles()
    test_camera_profile_mapping()
    test_request_shape_per_provider()
    test_doubao_and_qianfan_doc_facts()
    test_schema_dialect()
    test_model_generation_sort()
    test_token_limit_escalation_modes()
    test_key_store_delete()
    test_sidecar_backup()
    test_mask_coordinate_frame()
    test_exiftool_utf8_path(args.raw_dir)
    test_dng_embedded_write()
    test_new_sidecar_camera_profile(args.raw_dir)
    print()
    test_job_key_stability()
    print()
    test_job_key_collision()
    print()
    test_export_naming()
    print()
    test_jsx_products()
    print()
    test_offline_mock()
    print()
    test_offline_adapter()
    print()
    test_retry_policy()
    print()
    test_repeat_and_progress(args.raw_dir)
    print()
    test_cleanup_safety()
    print()
    test_artifact_locations(args.raw_dir)
    print()
    test_job_state()
    print()
    test_xmp_roundtrip(args.raw_dir)
    print()
    test_exiftool_and_preview(args.raw_dir)

    # 数据目录快照差分（见 _baseline 的说明）。必须放在最后：
    # 它测的是"前面所有用例加起来有没有弄脏真实数据目录"。
    if _isolate_ok:
        _added, _changed = _datadir_diff(_baseline)
        _dirty_ok, _dirty_detail = (
            not _added and not _changed,
            f"新增 {len(_added)} 个、改动 {len(_changed)} 个"
            + (f"：新增={_added[:3]} 改动={_changed[:3]}" if (_added or _changed) else ""),
        )
    else:
        # 逃生舱打开时这条断言**不适用**：那本来就是"允许用例改真实数据目录"
        # 的模式，硬判会必然假红。这里如实说明跳过，而不是假装通过。
        _dirty_ok, _dirty_detail = True, "（ACB_TEST_REAL_DATADIR=1，本轮未隔离，快照差分不适用）"
    record("真实数据目录零新增零改动（自检不污染用户数据）", bool(_dirty_ok), _dirty_detail)

    test_snapshot_exclusion_list_is_intentional()

    failed = [name for name, ok, _ in _results if not ok]
    print("\n" + "=" * 90)
    print(f"自检完成：{len(_results) - len(failed)}/{len(_results)} 项通过")
    if _skipped:
        # 单独报出来：以前这些用例是 record(..., True, "跳过")，汇总里看不出"根本没实测"。
        print(f"其中 {len(_skipped)} 项因缺少素材**未实测**（不计入失败，但也没真的验过；"
              "加 --raw-dir <目录> 可实测）：")
        for name in _skipped:
            print(f"  – {name}")
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✗ {name}")
        return 1
    print("全部通过。可以开始实际处理照片了（建议先用界面的「离线调试」跑一遍前 3 张）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
