# -*- coding: utf-8 -*-
"""运行时路径解析。

对应打包要求 #3 / #20：
    运行时数据（logs/、cache/、styles/、keyring 凭据）不得写入 _internal
    或临时解压目录，须使用 sys.executable 所在目录的子目录或
    %APPDATA%/AutoColorDiffusion。
    所有配置、风格、缓存、日志均相对于 exe 所在目录或 %APPDATA% 解析，
    不得依赖开发期相对路径。

四类路径的语义划分：
    resource_root()  只读资源，来自 PyInstaller 的 datas（sys._MEIPASS）。
                     frozen 时为 <exe目录>/_internal，开发时为项目根。
    exe_dir()        可执行文件所在目录（开发期为项目根）。
    data_root()      可写数据根目录。默认 %APPDATA%/AutoColorDiffusion；
                     若 exe 同目录存在 portable.txt，则改为 exe_dir()/data（便携模式）。
    bundled_exiftool_candidates()  exiftool.exe 的查找顺序。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_ID, LEGACY_APP_ID
from .errors import UnsupportedPlatformError

# keyring 的服务名与 %APPDATA% 下的文件夹名共用此常量，
# 保证"凭据归属"和"数据归属"在用户视角是同一个应用。
# 真值定义在 acb/__init__.py（与 APP_DISPLAY_NAME / __version__ 放一起），
# 这里只做转发，避免出现第二份硬编码。
APP_NAME = APP_ID

# 便携模式开关文件名。存在即启用（内容不解析，看存在性即可）。
# 选择"文件名标记"而非命令行参数，是因为用户双击 exe 时无法传参。
PORTABLE_MARKER = "portable.txt"


def require_windows() -> None:
    """非 Windows 平台直接失败。

    在模块级调用而不是 import 时调用，是为了让 tools/ 下的离线脚本
    （例如纯 XML 分析）在非 Windows 上仍可被导入用于阅读代码。
    """
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            "Auto Color Diffusion 仅支持 Windows 平台。\n"
            f"当前检测到平台：{sys.platform}\n"
            "本项目不提供 macOS / Linux 实现：\n"
            "  - Photoshop 批量导出依赖 win32com.client（Windows COM）；\n"
            "  - 密钥管理依赖 Windows 凭据管理器（keyring.backends.Windows）；\n"
            "  - 打包目标为 Windows 单文件/单目录 exe。"
        )


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。

    PyInstaller 会在运行时注入 sys.frozen 与 sys._MEIPASS。
    """
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录（打包进程序的 datas）。

    对应 spec 的 datas：config/models.yaml、styles/、assets/icc/*、
    assets/jsx/export_batch.jsx。

    onedir 模式下 sys._MEIPASS 指向 <exe目录>/_internal；
    开发模式下指向项目根（acb 包的上一级）。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # 极少数 PyInstaller 版本/配置下 _MEIPASS 不存在，退化为 exe 目录。
        return exe_dir()
    return Path(__file__).resolve().parent.parent


def exe_dir() -> Path:
    """可执行文件所在目录；开发模式下为项目根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    """是否启用便携模式（数据写在 exe 同目录，便于放 U 盘携带）。"""
    try:
        return (exe_dir() / PORTABLE_MARKER).is_file()
    except OSError:
        return False


def data_root() -> Path:
    """可写数据根目录。

    便携模式：exe_dir()/data —— 用户明确要求把数据放在程序旁边。
    默认模式：%APPDATA%/AutoColorDiffusion —— 符合 Windows 惯例，
             且避免 Program Files 下的写权限问题（UAC 会直接拒绝写入）。
    """
    if is_portable():
        return exe_dir() / "data"
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # 理论上 Windows 一定有 APPDATA；缺失时退化为用户主目录，
        # 而不是静默写到 exe 旁边（那会违反打包要求 #3）。
        return Path.home() / f".{APP_NAME}"
    return Path(appdata) / APP_NAME


# --- 各功能子目录 -----------------------------------------------------------

def logs_dir() -> Path:
    """日志目录。硬约束 #13：按日期轮转，单文件 10MB，保留 5 份。"""
    return data_root() / "logs"


def runs_dir() -> Path:
    """每次导出的"运行目录"的父目录。

    一次运行产生一组文件，放在 `<数据目录>/runs/<运行标识>/` 下：
        manifest.json / export_batch.jsx / run_export.bat   ← 驱动导出的机器件
        ps_log.txt / ps_result.json                          ← Photoshop 写出的
    路径知识的单一来源在本函数 —— jsx_render 与 cleanup 都从这里取。

    【为什么不放在出片目录里】出片目录是用户的交付目录，只该有 JPG。
    这五个文件是驱动导出的"机器件"，留在那里既碍事，
    又会在打包交付时被一起发出去（用户反馈过两次）。
    它们的生命周期也比 JPG 短得多：导出跑完就没用了，关闭程序即可清理。
    """
    return data_root() / "runs"


def cache_dir() -> Path:
    """缓存根目录。"""
    return data_root() / "cache"


def thumbs_dir() -> Path:
    """缩略图缓存目录。硬约束 #16：cache/thumbs/，键与断点键同源。"""
    return cache_dir() / "thumbs"


def state_dir() -> Path:
    """任务状态目录（断点续跑用）。硬约束 #6。"""
    return data_root() / "state"


def styles_dir() -> Path:
    """风格文件目录（style_profile.json 存放处）。

    打包要求 #3 明确 styles/ 属于运行时数据，不得写 _internal，
    因此首次运行会把打包内的 styles seed 复制到这里。
    """
    return data_root() / "styles"


def config_dir() -> Path:
    """用户配置目录（models.yaml 的可写副本，便于不重新打包就换模型）。"""
    return data_root() / "config"


def export_root_default(source_dir: Path) -> Path:
    """默认输出目录。

    界面默认值 <源目录>/_export/（硬约束 #8）。
    下划线前缀是约定俗成的"程序产物目录"，便于在资源管理器里与用户素材区分。
    """
    return source_dir / "_export"


def ensure_runtime_dirs() -> None:
    """创建全部可写目录。启动时调用一次即可（exist_ok 幂等）。"""
    migrate_legacy_data()
    for d in (logs_dir(), thumbs_dir(), state_dir(), styles_dir(), config_dir()):
        d.mkdir(parents=True, exist_ok=True)


def migrate_legacy_data() -> Path | None:
    """把改名前的数据目录搬到新名字下。返回搬移后的路径（没搬则 None）。

    背景：产品从 Auto Color Bleed 改名为 Auto Color Diffusion 后，数据目录也从
    `%APPDATA%\\AutoColorBleed` 变成 `%APPDATA%\\AutoColorDiffusion`。
    如果不搬，老用户会突然"丢失"已保存的 API 密钥、训练好的风格、断点记录 ——
    这些都在数据目录里，重建成本不低。

    安全约束（任何一条不满足就什么都不做）：
        - 便携模式不搬（那里没有 %APPDATA% 的概念，且用户是明确自己放的）；
        - 新目录**已存在**就不搬 —— 说明已经迁移过或新装过，绝不合并覆盖；
        - 只做**重命名**，不做复制删除（中途失败也不会丢数据）。
    """
    if is_portable():
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None

    base = Path(appdata)
    new_root = base / APP_ID
    legacy_root = base / LEGACY_APP_ID

    try:
        if new_root.exists() or not legacy_root.is_dir():
            return None
        legacy_root.rename(new_root)
    except OSError:
        # 旧目录被别的进程占用、或权限不足 —— 迁移失败不是错误，
        # 程序照常在新目录里从零开始，用户最多需要重新填一次密钥。
        return None
    return new_root


# --- 打包资源定位 -----------------------------------------------------------

def icc_dir() -> Path:
    """预设 ICC 文件目录（sRGB / Adobe RGB(1998) / Display P3）。"""
    return resource_root() / "assets" / "icc"


def jsx_template_path() -> Path:
    """Photoshop 导出脚本模板。"""
    return resource_root() / "assets" / "jsx" / "export_batch.jsx"


# 应用图标：圆角 PNG（运行时给窗口/任务栏用）与多尺寸 ICO（打包时给 exe 用）。
# 两者都由 `python tools/make_icon.py` 生成，不手改二进制文件。
APP_ICON_PNG = "assets/icon/app-256.png"
APP_ICON_PNG_LARGE = "assets/icon/app-512.png"
APP_ICON_ICO = "assets/app.ico"


def app_icon_png_path() -> Path:
    """运行时用的圆角 PNG 图标（可能不存在：图标还没生成时不该让程序起不来）。"""
    return resource_root() / APP_ICON_PNG


def app_icon_png_large_path() -> Path:
    """大尺寸版（关于对话框 / 文档里用）。"""
    return resource_root() / APP_ICON_PNG_LARGE


def app_icon_ico_path() -> Path:
    """打包用的多尺寸 ICO（spec 的 EXE(icon=…) 读它）。"""
    return resource_root() / APP_ICON_ICO


# exiftool.exe 的文件名。Windows 上必须带 .exe 才能被 subprocess 直接执行。
EXIFTOOL_EXE = "exiftool.exe"


def bundled_exiftool_candidates() -> list[Path]:
    """捆绑 exiftool.exe 的候选路径，按优先级排列。

    用户已裁决：优先 exe 同目录 _internal/，其次 PATH。
    因此顺序为：
      1. sys._MEIPASS/exiftool.exe       （spec 的 binaries 放进 _internal/）
      2. exe_dir()/exiftool.exe          （用户手动把 exe 扔在程序旁边）
      3. exe_dir()/tools/exiftool.exe    （手动整理成 tools/ 子目录的情况）
      4. resource_root()/exiftool.exe    （开发期）
    """
    cands: list[Path] = []
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / EXIFTOOL_EXE)
    cands.append(exe_dir() / EXIFTOOL_EXE)
    cands.append(exe_dir() / "tools" / EXIFTOOL_EXE)
    cands.append(resource_root() / EXIFTOOL_EXE)
    return cands


def find_bundled_exiftool() -> Path | None:
    """返回第一个真实存在的捆绑 exiftool.exe；都不存在则 None。"""
    for p in bundled_exiftool_candidates():
        try:
            if p.is_file():
                return p
        except OSError:
            # 某些网络盘/权限异常会在这里抛出，直接跳过该候选。
            continue
    return None


def find_exiftool() -> Path | None:
    """完整的 exiftool 查找顺序：捆绑优先，其次 PATH。

    对应硬约束 #10 与用户裁决 #5。
    """
    bundled = find_bundled_exiftool()
    if bundled is not None:
        return bundled
    found = shutil.which(EXIFTOOL_EXE) or shutil.which("exiftool")
    return Path(found) if found else None


# 官方 Windows 包的目录名：启动器 exe 旁边放一份 exiftool_files\（Perl 运行时 + 模块）。
EXIFTOOL_FILES_DIR = "exiftool_files"


def exiftool_argv_prefix(exe: Path) -> list[str]:
    """给定一个 exiftool.exe，返回"该怎么调用它"的参数前缀。

    为什么要多这一层（2026-09-25 实测，发布打包时踩到）：

    官方 Windows 包是「启动器 exe + exiftool_files\\」两份东西，那个 exe 只是启动器。
    把整份放进 PyInstaller 产物的 `_internal\\` 之后，它**跑不起来**：

        > _internal\\exiftool.exe -ver
        Can't locate strict.pm in @INC (@INC contains:) at ...exiftool_files\\exiftool.pl line 10

    但同一个目录里，用 `exiftool_files\\perl.exe` 直接跑 `exiftool_files\\exiftool.pl`
    一切正常（实测：源目录、带空格的路径、Z: 盘、打包后的 _internal，四种位置全 rc=0）。
    所以这里优先走 Perl 那条路，并显式 `-I <exiftool_files\\lib>` —— 不再依赖启动器
    自己那套路径推导，也就不会再出现"装好了但 DNG 悄悄写不进去"。

    只有 exe（没有 exiftool_files\\）时退回老行为：直接执行那个 exe。
    """
    files_dir = exe.parent / EXIFTOOL_FILES_DIR
    perl = files_dir / "perl.exe"
    script = files_dir / "exiftool.pl"
    lib = files_dir / "lib"
    try:
        if perl.is_file() and script.is_file() and lib.is_dir():
            return [str(perl), "-I", str(lib), str(script)]
    except OSError:
        pass                                     # 网络盘/权限异常一律退回直接执行
    return [str(exe)]


def copy_tree_missing(src: Path, dst: Path) -> list[Path]:
    """把 src 下缺少的文件复制到 dst（已存在的不覆盖）。

    用于把打包内的 styles/、config/models.yaml 作为"种子"释放到可写目录：
    只在目标不存在时复制，从而不会覆盖用户后续的修改。
    返回实际复制的文件列表，便于日志记录。
    """
    copied: list[Path] = []
    if not src.is_dir():
        return copied
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(target)
    return copied
