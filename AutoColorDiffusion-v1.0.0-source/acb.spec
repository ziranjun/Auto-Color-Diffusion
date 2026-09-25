# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
#  Auto Color Diffusion —— PyInstaller 打包配置
# ----------------------------------------------------------------------------
#  使用方式（推荐用 build.bat，它会先建 venv 装依赖再调用本文件）：
#      python -m PyInstaller --noconfirm --clean acb.spec
#
#  说明：spec 文件名（acb.spec）与产物名（AutoColorDiffusion.exe / dist\AutoColorDiffusion\）
#        是两回事，不必一致 —— 产物名由下方 EXE(...) 与 COLLECT(...) 的 name 参数决定。
#
#  目标形态：**onedir**（优先于 onefile）
#      启动快（1–2 秒）、排障容易（缺 dll 一眼可见）、更新只需替换改动文件。
#      onefile 每次启动都要解压到 %TEMP%\_MEIxxxx，首次 10–30 秒，
#      且被杀软反复扫描的误报率更高。权衡细节见 build.bat 顶部注释。
#
#  运行时数据不在这里（打包要求 #3）
#      本 spec 只处理**只读资源**（datas / binaries）。
#      日志、缓存、风格、断点状态、keyring 凭据全部由 acb/paths.py 解析到
#      %APPDATA%\AutoColorDiffusion 或便携模式下的 <exe目录>\data，
#      绝不写入 _internal 或 PyInstaller 的临时解压目录。
# ============================================================================

import os
from pathlib import Path

# SPECPATH 是 PyInstaller 注入的变量，指向 spec 文件所在目录（即项目根）。
PROJECT_ROOT = Path(SPECPATH)  # noqa: F821  （由 PyInstaller 注入）

# --- 1. datas：只读资源 -----------------------------------------------------
# 每一项是 (源路径, 目标目录)。onedir 模式下目标目录相对于 _internal\。
# 这些路径必须与 acb/paths.py 中的解析逻辑严格对应：
#     models.yaml   -> resource_root()/config/models.yaml   （acb/config.py）
#     styles/       -> resource_root()/styles/              （style_profile.ensure_seed_styles）
#     assets/icc/   -> resource_root()/assets/icc/          （acb/raw/icc.py）
#     assets/jsx/   -> resource_root()/assets/jsx/          （acb/paths.jsx_template_path）
#
# ⚠ styles 是**逐个列名**加入的（见下方 SEED_STYLE_FILES），不整目录打包：
#   发布出去的包必须只含两个内置种子，不含开发者自己的风格档案。
# 内置风格种子：**只列这两个**，绝不整目录打包。
# 为什么必须逐个列名：ensure_seed_styles() 会把打包 styles/ 里的**每一个** json
# 释放到用户的风格目录，is_seed_style() 还会把它们当成"不可删的内置风格" ——
# 整目录打包 = 把开发者自己训练/测试的风格发给所有用户（发布检查项之一，
# 另一条防线是 tools/check_package.py 会扫成品目录）。
# 这两个文件名必须与 acb/constants.py 的 DEFAULT_STYLE_NAME / AUTOPILOT_STYLE_NAME
# 保持一致，tools/smoke_test.py 有一条断言盯着这件事（名字一改就红）。
SEED_STYLE_FILES = ("default_neutral.json", "AI自主决策.json")

datas = [
    (str(PROJECT_ROOT / "config" / "models.yaml"), "config"),
    (str(PROJECT_ROOT / "assets" / "jsx" / "export_batch.jsx"), "assets/jsx"),
]

for _seed_name in SEED_STYLE_FILES:
    _seed_path = PROJECT_ROOT / "styles" / _seed_name
    if _seed_path.is_file():
        datas.append((str(_seed_path), "styles"))
    else:
        print("=" * 78)
        print(f"[警告] 未找到内置风格种子 styles/{_seed_name}，将不会捆绑它。")
        print("       影响：用户目录里不会释放这个内置风格（不中断打包）。")
        print("=" * 78)

# ICC 目录：逐个文件加入而不是整目录加入。
# 目录里放的是**本程序自己生成**的 profile（不是 Adobe 的原文件）：
#   sRGB.icc                    —— LittleCMS 公开定义构造
#   AdobeRGB1998-compatible.icc —— 按 Adobe 公开色度参数自算的等效 profile
#     （命名遵循 Adobe 官方要求的 "compatible with Adobe RGB (1998)" 措辞；
#      Adobe 官方那份受协议限制、本程序不捆绑，找到就会优先用系统的）
# 逐个文件加入是因为目录可能为空（例如刚克隆下来还没跑过一次），
# 整目录加入时若目录不存在，PyInstaller 会直接报错中断打包。
_icc_dir = PROJECT_ROOT / "assets" / "icc"
if _icc_dir.is_dir():
    for _icc_file in sorted(_icc_dir.glob("*.icc")):
        datas.append((str(_icc_file), "assets/icc"))

# 应用图标：圆角 PNG（运行时窗口/任务栏用）与多尺寸 ICO（exe 图标用）。
# 两者都由 `python tools/make_icon.py` 生成（源图放 assets/icon/source.png）。
# 缺失时不中断打包：程序会以"没有图标"运行，而不是起不来。
_icon_dir = PROJECT_ROOT / "assets" / "icon"
if _icon_dir.is_dir():
    for _png in sorted(_icon_dir.glob("app-*.png")):
        datas.append((str(_png), "assets/icon"))

# 若项目根有 README / NOTICE / LICENSE，一并带上：
#   · 拿到成品的人不装 Python 也能看到说明与版权；
#   · LICENSE 是**许可条款要求**的（第三条第 a 款：每一份拷贝都要带完整许可证文本）。
for _extra in ("README.md", "NOTICE.txt", "LICENSE"):
    _p = PROJECT_ROOT / _extra
    if _p.is_file():
        datas.append((str(_p), "."))

# --- 1b. Qt 自带的中文翻译（qtbase_zh_CN.qm 等）----------------------------
# 【为什么必须带上】输入框右键菜单（Undo/Redo/Cut/…）、QInputDialog 的
# 「OK / Cancel」、QMessageBox 的标准按钮这些文案**不在我们的代码里**，
# 是 Qt 提供的，靠 QTranslator 加载 qtbase_zh_CN.qm 才会变中文。
# 不打进包里的话，开发时中文、打包后变英文 —— 典型的"只在成品里出现"的缺陷。
# 只带需要的那两个文件（整个 translations 目录有几十 MB，没必要）。
_qt_translations = None
try:
    import PyQt6 as _PyQt6  # noqa: N813

    _qt_translations = Path(_PyQt6.__file__).parent / "Qt6" / "translations"
except Exception:  # noqa: BLE001
    _qt_translations = None

if _qt_translations is not None and _qt_translations.is_dir():
    for _qm in ("qtbase_zh_CN.qm", "qt_zh_CN.qm"):
        _p = _qt_translations / _qm
        if _p.is_file():
            datas.append((str(_p), "PyQt6/Qt6/translations"))
    print(f"[提示] 已捆绑 Qt 中文翻译：{_qt_translations}")
else:
    print("=" * 78)
    print("[警告] 未找到 PyQt6 的 translations 目录，将不会捆绑中文翻译。")
    print("       影响：输入框右键菜单与标准对话框会显示英文（界面其余部分不受影响）。")
    print("=" * 78)


# --- 2. exiftool：整份捆绑（exe + exiftool_files/）--------------------------
# 为什么捆绑：DNG 的内嵌 XMP 读写**必须**要有 exiftool。让用户自己去官网下载、
# 解压、放进目录，是"装好了但某个功能悄悄不可用"的典型来源，所以发布版直接带上。
#
# ⚠ 必须是**整份**：官方 Windows 包的 exiftool.exe 只是个启动器，真正的实现在旁边的
#    exiftool_files\（Perl 运行时 + 模块，实测 508 个文件 / 32.9 MB）。只拷 exe 会得到
#    "找不到 exiftool_files" 的报错。exiftool_files\LICENSE 就是它的许可证，随包分发
#    正好满足 GPL/Artistic 的随附要求（NOTICE.txt 里也写明了）。
#
# 从哪里找（按顺序，找到第一个就停；目录里必须同时有 exiftool.exe 与 exiftool_files\）：
#   1. 环境变量 ACB_EXIFTOOL_DIR —— 想固定用某个版本时用
#   2. <项目根>\vendor\exiftool   —— 把官方压缩包解压到这里（推荐；vendor 不进版本库）
#   3. <项目根>                   —— 老做法：exiftool.exe 直接放项目根
#   4. C:\Tools\ExifTool          —— 本机常见安装位置（开发机默认就是它）
import os

binaries = []
_exiftool_candidates = []
if os.environ.get("ACB_EXIFTOOL_DIR"):
    _exiftool_candidates.append(Path(os.environ["ACB_EXIFTOOL_DIR"]))
_exiftool_candidates += [
    PROJECT_ROOT / "vendor" / "exiftool",
    PROJECT_ROOT,
    Path(r"C:\Tools\ExifTool"),
]

_exiftool_dir = None
for _cand in _exiftool_candidates:
    if (_cand / "exiftool.exe").is_file() and (_cand / "exiftool_files").is_dir():
        _exiftool_dir = _cand
        break

if _exiftool_dir is not None:
    datas.append((str(_exiftool_dir / "exiftool.exe"), "."))
    datas.append((str(_exiftool_dir / "exiftool_files"), "exiftool_files"))
    _n = len([p for p in (_exiftool_dir / "exiftool_files").rglob("*") if p.is_file()])
    print(f"[提示] 将整份捆绑 exiftool：{_exiftool_dir}（exiftool_files 里 {_n} 个文件）")
else:
    # 不中断打包：程序在运行时会检测并给出安装指引，并降级用 rawpy 提取预览。
    # 唯一受影响的只有 DNG 内嵌 XMP 的读写（那必须要有 exiftool）。
    print("=" * 78)
    print("[警告] 没找到完整的 exiftool（exiftool.exe + exiftool_files\\），将不会捆绑。")
    print("       影响：DNG 内嵌 XMP 的读写不可用（旁侧 XMP 与 JPG 导出不受影响）。")
    print("       修法：从 https://exiftool.org 下载 Windows 版，解压到")
    print("             <项目根>\\vendor\\exiftool\\ 后重新打包；")
    print("             或设环境变量 ACB_EXIFTOOL_DIR 指向现成的目录。")
    print("       已找过：", [str(p) for p in _exiftool_candidates])
    print("=" * 78)


# --- 3. hiddenimports -------------------------------------------------------
# 为什么需要显式声明：
#   PyInstaller 的静态分析找不到"运行期才导入"或"通过字符串导入"的模块。
#   本项目有三处典型情况：
#     - keyring 的后端是插件式动态发现的（keyring.backends.Windows 靠 entry_points 注册）；
#     - PIL 的编解码器与 ImageCms 是插件式加载；
#     - pydantic v2 内部会导入 pydantic_core 及其扩展模块。
#   漏掉它们的症状是：打包后运行时报 ModuleNotFoundError，而开发环境一切正常。
hiddenimports = [
    # --- 本项目自己的包（动态导入路径）---
    "acb",
    "acb.ai.adapter",
    "acb.ai.client",
    "acb.ai.prompts",
    "acb.ai.schema",
    "acb.pipeline.output_mode",
    "acb.pipeline.train_mode",
    "acb.pipeline.style_profile",
    "acb.pipeline.job",
    "acb.ps.jsx_render",
    "acb.ps.photoshop",
    "acb.raw.exiftool",
    "acb.raw.icc",
    "acb.raw.preview",
    "acb.raw.stats",
    "acb.ui.i18n",
    "acb.ui.main_window",
    "acb.ui.workers",
    "acb.xmp.dng",
    "acb.xmp.reader",
    "acb.xmp.writer",
    "acb.xmp.validator",
    # --- PyQt6 ---
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    # --- keyring 及其 Windows 后端 ---
    # 注意：这里只能写**模块**名。曾经写过 "keyring.backends.Windows.WinVaultKeyring"
    # （那是类名，不是模块），PyInstaller 会报 “Hidden import … not found” 的 ERROR ——
    # 功能其实没坏（模块 keyring.backends.Windows 已经打进包了），但一条常驻的假错误
    # 会让真正的缺失被淹没，所以删掉它。
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",
    "keyring.backends.null",
    "keyring.errors",
    # --- pydantic v2 ---
    "pydantic",
    "pydantic_core",
    "pydantic_core._pydantic_core",
    "annotated_types",
    "typing_extensions",
    # --- yaml ---
    "yaml",
    "_yaml",
    # --- rawpy（内含 libraw 的二进制扩展）---
    "rawpy",
    "rawpy._rawpy",
    # --- Pillow 及其关键插件 ---
    "PIL",
    "PIL.Image",
    "PIL.ImageCms",
    "PIL.ImageOps",
    "PIL.JpegImagePlugin",
    "PIL.TiffImagePlugin",
    "PIL.PngImagePlugin",
    "PIL.BmpImagePlugin",
    "PIL.WebPImagePlugin",
    "PIL._imaging",
    "PIL._imagingcms",
    # --- 网络 ---
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
    "certifi",
    # --- numpy（rawpy / stats 依赖）---
    "numpy",
]

# Windows 专有的 COM 支持（pywin32）。在非 Windows 上跳过，避免交叉打包时报错。
if os.name == "nt":
    hiddenimports += [
        "pythoncom",
        "pywintypes",
        "win32com",
        "win32com.client",
        "win32api",
    ]


# --- 4. excludes：明确排除用不到的大件，压小体积 -----------------------------
# 每一项都是"确认用不到"的库，排除它们能显著减小产物（尤其 matplotlib/scipy）。
excludes = [
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "setuptools",
    "pip",
    "tkinter",
    "PyQt5",
    "PySide2",
    "PySide6",
    "test",
    "unittest",
]


block_cipher = None

a = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoColorDiffusion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # 不用 UPX：压缩后的 exe 被杀软误报的概率显著上升
    console=False,       # GUI 程序，不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标：用 assets/app.ico（多尺寸：16/24/32/48/64/128/256）。
    # 由 tools/make_icon.py 生成；文件不存在时才退回 PyInstaller 默认图标。
    icon=str(PROJECT_ROOT / "assets" / "app.ico")
    if (PROJECT_ROOT / "assets" / "app.ico").is_file()
    else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AutoColorDiffusion",
)
