# -*- coding: utf-8 -*-
"""一键备份：把当前版本的**源码快照**放到项目所在盘的根目录（本项目是 Z:\\）。

产物形态（与 Z:\\AutoColorDiffusion-v1.0.0-source 完全一致，三个一组的"三件套"）
------------------------------------------------------------------------------
    Z:\\AutoColorDiffusion-v<版本>-source\\            解压好的源码目录（能直接打开自检）
    Z:\\AutoColorDiffusion-v<版本>-source.zip         同一份内容的压缩包（长期存放）
    Z:\\AutoColorDiffusion-v<版本>-source.sha256.txt  校验文件：版本/时间/文件数/关键 SHA-256
                                                      + 逐文件 SHA-256

用法
----
    python tools/make_backup.py                  # 备份到项目所在盘根目录（Z:\\）
    python tools/make_backup.py --out D:\\备份     # 换个位置
    python tools/make_backup.py --list            # 只列出"会备份什么/会排除什么"，不写文件

为什么是"目录 + 压缩包 + 校验文件"三件套
----------------------------------------
1. **这个仓库没有 git**：改坏了没有第二份历史可回滚。目录版用来"马上翻某一行"，
   压缩包版用来长期存放（不会被顺手改动），校验文件用来证明"这份没被改过"。
2. **目录与压缩包必须同源**：压缩包是从**刚写好的那个目录**压出来的，
   不是重新遍历工作区 —— 否则会出现"目录里是 A、压缩包里是 B"这种最难查的不一致。
3. **不能整目录复制**：工作区里还有 .venv（约 350 MB）、dist/build（约 240 MB 构建产物）、
   test_data / test_data_v101（GB 级 RAW 素材）、tools/_tmp_*.py（排查草稿）。
   所以这里用**排除清单**而不是包含清单 —— 以后新增源码目录时不会因为忘了加白名单而漏备份
   （漏备份远比多备份危险）。
4. **照片/视频/密钥类文件命中就中止，不是跳过**：源码备份里出现 .cr3/.jpg/.xmp/.key
   这类文件，说明排除逻辑被人改了或绕过；这时候"少备份一点"远不如"停下来看一眼"。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb import APP_DISPLAY_NAME, __version__  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE_NAME = f"AutoColorDiffusion-v{__version__}-source"

# 整目录排除：都不是源码，且体积大或可重建（理由见文件头第 3 条）。
EXCLUDE_DIRS = {
    ".venv",             # 依赖环境，requirements.txt 能重建
    "dist",              # 构建产物（发布 zip 已单独存在桌面）
    "build",             # PyInstaller 中间产物
    "__pycache__",       # 字节码
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "test_data",         # 作者自己的 RAW 素材（约 1.1 GB）
    "test_data_v101",    # 上一轮的测试素材（约 137 MB）
}

# .vscode 里**只留** settings.json：tasks.json 是各人本机的调试任务清单，
# 进快照只会让"这份备份能不能原样重跑"变得说不清（v1.0.0 的快照也是这么做的）。
KEEP_VSCODE = ("settings.json",)

# 按文件名排除（大小写不敏感）：字节码与编辑器/排查残留。
EXCLUDE_FILE_GLOBS = (
    "*.pyc", "*.pyo",
    "Thumbs.db", ".DS_Store",
    "build_out.txt",     # 排查时重定向出来的日志
)

# 只在 tools/ 下排除的草稿（--keep-tmp 可关掉）。
TMP_TOOLS_GLOBS = ("_tmp_*",)

# 命中即**中止**：照片、视频、侧车、密钥、日志、压缩包。
# 注意 .png/.ico 不在列 —— assets/icon 里的图标与设计源图是源码的一部分。
FORBIDDEN_SUFFIXES = (
    # 照片 / RAW
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".raf",
    ".rw2", ".rwl", ".raw", ".dng", ".pef", ".srw", ".3fr", ".iiq", ".x3f", ".mrw",
    ".jpg", ".jpeg", ".jpe", ".heic", ".hif", ".tif", ".tiff", ".psd", ".xmp",
    # 视频
    ".mov", ".mp4", ".avi", ".mkv",
    # 密钥 / 凭据
    ".key", ".pem", ".pfx", ".p12", ".kdbx", ".env",
    # 日志 / 归档
    ".log", ".zip", ".7z", ".rar",
)


def should_skip(path: Path, keep_tmp: bool) -> bool:
    """判断单条路径是否排除（只看相对 ROOT 的部分）。"""
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
        return True
    if path.is_dir() and path.name in EXCLUDE_DIRS:
        return True
    name_lower = path.name.lower()
    if any(fnmatch.fnmatch(name_lower, pat.lower()) for pat in EXCLUDE_FILE_GLOBS):
        return True
    if rel.parts[0] == ".vscode" and path.name not in KEEP_VSCODE:
        return True
    if not keep_tmp and rel.parts[0] == "tools":
        if any(fnmatch.fnmatch(path.name, pat) for pat in TMP_TOOLS_GLOBS):
            return True
    return False


def collect(keep_tmp: bool) -> list[Path]:
    """收集要备份的文件（按相对路径排序，保证三次产物顺序一致）。"""
    files = [p for p in ROOT.rglob("*")
             if p.is_file() and not should_skip(p, keep_tmp)]
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def find_forbidden(files: list[Path]) -> list[str]:
    """找出混进来的照片/密钥类文件（相对路径）。有就是逻辑出问题，调用方应中止。"""
    hits = []
    for path in files:
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            hits.append(path.relative_to(ROOT).as_posix())
    return hits


def split_top_level(files: list[Path]) -> tuple[list[str], list[str]]:
    """把条目分成「顶层目录」和「根目录文件」两类，用于生成"包含："那一行。"""
    dirs: list[str] = []
    root_files: list[str] = []
    seen: set[str] = set()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if "/" in rel:
            top = rel.split("/")[0] + "/"
            if top not in seen:
                seen.add(top)
                dirs.append(top)
        else:
            root_files.append(rel)
    return dirs, root_files


def sha256_of(path: Path, chunk: int = 1024 * 1024) -> str:
    """算文件指纹（分块读，避免大文件一次吃进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_tree(files: list[Path], target_dir: Path) -> None:
    """把文件按原相对路径复制到 target_dir（先清空，保证不残留上一版的文件）。"""
    if target_dir.exists():
        shutil.rmtree(target_dir, onexc=_force_remove)
    for path in files:
        dest = target_dir / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _force_remove(func, path, exc) -> None:  # noqa: ANN001 - shutil 回调签名固定
    """删不掉就先去掉只读属性再删（Windows 上偶尔会碰上只读/被占用的旧产物）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    try:
        func(path)
    except OSError:
        pass


def zip_tree(source_dir: Path, zip_path: Path) -> int:
    """把目录压成 zip（包内含一层 <BASE_NAME>\\ 目录）。返回条目数。"""
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                arcname = Path(BASE_NAME) / path.relative_to(source_dir)
                zf.write(path, str(arcname))
                count += 1
    return count


def sha256_text(files: list[Path], source_dir: Path, zip_path: Path) -> str:
    """校验文件的全文：抬头（版本/时间/位置/文件数/包含/排除/关键指纹）+ 逐文件哈希。"""
    dirs, root_files = split_top_level(files)
    release_zip = Path.home() / "Desktop" / f"AutoColorDiffusion-v{__version__}-win64.zip"
    exe = ROOT / "dist" / "AutoColorDiffusion" / "AutoColorDiffusion.exe"

    def fingerprint_line(label: str, path: Path) -> str:
        if path.is_file():
            return f"{label}：{sha256_of(path)}"
        return f"{label}：（本次未生成，跳过）"

    exclude_line = ("排除：" + " / ".join(sorted(EXCLUDE_DIRS)) + " /")
    exclude_line2 = ("      " + " / ".join(EXCLUDE_FILE_GLOBS)
                     + " / .vscode/tasks.json / tools/_tmp_*")
    header = f"""{APP_DISPLAY_NAME} v{__version__} —— 源代码备份校验文件
============================================================================
备份时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（本机时间）
备份位置：{source_dir}（目录） 与 {zip_path.name}（压缩包）
文件数：{len(files)}

包含：{" ".join(dirs)} + 根目录的
      {" ".join(root_files)}

{exclude_line}
{exclude_line2}
      照片·侧车·视频·密钥类文件（按后缀穷举扫过，命中即中止）

{ fingerprint_line("源码压缩包 SHA-256", zip_path) }
{ fingerprint_line("发布包（桌面）SHA-256", release_zip) }  [{release_zip.name}]
{ fingerprint_line("发布 exe SHA-256", exe) }

怎么用这份备份
  1) 解压/复制到任意位置（目录版可直接打开看）；
  2) python -m venv .venv
     .venv\\Scripts\\python.exe -m pip install -r requirements.txt
  3) .venv\\Scripts\\python.exe tools\\smoke_test.py
     （没有 test_data\\ 时，"需要真实素材"的那几项记「未实测」而不是失败）
  4) .venv\\Scripts\\python.exe app.py
  （要处理真实照片请自备 exiftool：解压到 vendor\\exiftool\\ 或设 ACB_EXIFTOOL_DIR）

逐文件 SHA-256（格式：哈希  相对路径）
----------------------------------------------------------------------------
"""
    body = "\n".join(
        f"{sha256_of(source_dir / p.relative_to(ROOT))}  {p.relative_to(ROOT).as_posix()}"
        for p in files
    )
    return header + body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把源码打成「目录 + 压缩包 + sha256 校验文件」")
    parser.add_argument("--out", type=Path, default=ROOT.parent,
                        help=f"备份输出目录（默认：项目所在盘根目录 {ROOT.parent}）")
    parser.add_argument("--list", action="store_true", help="只列出备份/排除内容，不写文件")
    parser.add_argument("--keep-tmp", action="store_true", help="连 tools/_tmp_*.py 一起备份")
    args = parser.parse_args(argv)

    files = collect(args.keep_tmp)
    total_bytes = sum(p.stat().st_size for p in files)

    forbidden = find_forbidden(files)
    if forbidden:
        print("中止：备份清单里出现不该有的文件（照片/视频/密钥/日志/压缩包）：")
        for rel in forbidden[:20]:
            print("   ", rel)
        print("请检查 EXCLUDE_DIRS / EXCLUDE_FILE_GLOBS —— 宁可备份不全，也不能把这些打进去。")
        return 3

    if args.list:
        dirs, root_files = split_top_level(files)
        print(f"版本 {__version__}：将备份 {len(files)} 个文件，{total_bytes / 1024 / 1024:.1f} MB")
        print(f"顶层目录：{' '.join(dirs)}")
        print(f"根目录文件：{' '.join(root_files)}")
        print(f"排除目录：{'、'.join(sorted(EXCLUDE_DIRS))}")
        print(f"排除文件：{'、'.join(EXCLUDE_FILE_GLOBS)}、.vscode/tasks.json、tools/_tmp_*")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    source_dir = args.out / BASE_NAME
    zip_path = args.out / f"{BASE_NAME}.zip"
    sha_path = args.out / f"{BASE_NAME}.sha256.txt"

    copy_tree(files, source_dir)
    count = zip_tree(source_dir, zip_path)
    sha_path.write_text(sha256_text(files, source_dir, zip_path), encoding="utf-8")

    print(f"已复制 {len(files)} 个源码文件 → {source_dir}")
    print(f"已压缩 {count} 个文件 → {zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"已写入校验文件 → {sha_path}")
    print(f"源码压缩包 SHA-256：{sha256_of(zip_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
