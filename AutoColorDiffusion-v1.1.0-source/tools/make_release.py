# -*- coding: utf-8 -*-
"""一键发布：守门检查 → 写「构建信息.txt」→ 打开发布压缩包（默认放桌面）。

用法
----
    python tools/make_release.py                  # 用 acb.__version__ 当版本号，输出到桌面
    python tools/make_release.py --out D:\\发布     # 换个输出目录
    python tools/make_release.py --no-zip         # 只做守门与构建信息（不压缩）

为什么需要这一步（三个"只有发布时才暴露"的问题）
--------------------------------------------------
1. **把开发者的东西打进包**：照片、侧车 XMP、训练出来的风格、日志、缓存。
   整目录打包 `styles/` 就是这么泄漏的 —— 所以先跑 `tools/check_package.py` 守门，
   非零退出就中止发布，绝不"先发了再说"。
2. **忘了改版本号**：包名、exe 属性、"关于"对话框三处对不上。
   这里的版本号**只从 `acb.__version__` 派生**，不写第二处。
3. **用户问"我下的这份是不是最新的"**：构建信息里带 exe 与压缩包的 SHA-256，
   谁都能自己核对（README 里也写了怎么核）。

产物形态：onedir 目录压成一个 zip（绿色免安装版）。不做单文件 exe、不做安装包，
理由见 build.bat 顶部与 README「打包为 Windows exe」一节。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb import APP_DISPLAY_NAME, __version__  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "dist" / "AutoColorDiffusion"
INNER_DIR = "AutoColorDiffusion"          # 压缩包内的那一层目录名
TOP_LEVEL_DOCS = ("README.md", "NOTICE.txt", "LICENSE")


def sha256_of(path: Path, chunk: int = 1024 * 1024) -> str:
    """算文件指纹（大文件分块读，避免一次吃进内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def write_build_info(package_dir: Path) -> Path:
    """写「构建信息.txt」：版本、构建时间、体积、exe 指纹、包内有什么/没有什么。"""
    files = [f for f in package_dir.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    exe = package_dir / "AutoColorDiffusion.exe"
    py_version = sys.version.split()[0]
    try:
        import PyInstaller

        pyinstaller = PyInstaller.__version__
    except Exception:                                # noqa: BLE001 - 版本信息拿不到不影响发布
        pyinstaller = "未知"

    text = f"""{APP_DISPLAY_NAME} v{__version__} —— 构建信息
============================================================================
构建时间   ：{time.strftime('%Y-%m-%d %H:%M:%S')}（本机时间）
平台       ：Windows x64
Python     ：{py_version}
PyInstaller：{pyinstaller}
打包形态   ：onedir（绿色免安装版，解压即用）
程序体积   ：{total / 1024 / 1024:.1f} MB（{len(files)} 个文件）
AutoColorDiffusion.exe SHA-256：
  {sha256_of(exe) if exe.is_file() else '（找不到 exe）'}

这个包里有什么
------------------------------------------------------------------------------
  AutoColorDiffusion.exe   双击运行（无控制台窗口）
  _internal\\              运行库与内置资源（不要删、不要单独移动）
  README.md               完整说明（安装、用法、常见问题）
  NOTICE.txt              第三方组件与致谢
  LICENSE                 许可条款（Copyright and Permission Notice v1.0.0）

这个包里**没有**什么（发布前由 tools/check_package.py 逐项扫过）
------------------------------------------------------------------------------
  · 开发者的照片、RAW 原片、XMP 侧车（按 .cr3/.dng/.jpg/.xmp 等后缀穷举扫描）
  · 测试数据，以及训练/测试出来的风格档案（_internal\\styles 里只有两个内置风格）
  · 日志、缩略图缓存、断点状态、备份目录
  · 开发脚本与环境（.venv、tools、acb.spec、build.bat、requirements.txt）

运行时数据放在哪里
------------------------------------------------------------------------------
  %APPDATA%\\AutoColorDiffusion\\   配置（models.yaml）、日志、缓存、
                                    断点状态、训练出的风格档案
  便携模式：在 exe 同目录放一个空的 portable.txt，数据就写到 <exe目录>\\data\\

首次使用建议
------------------------------------------------------------------------------
  1. 先读 README.md 的「快速开始」；
  2. 界面上先加 1~3 张照片、勾选「离线调试（演练）」跑一遍，确认链路通；
  3. DNG 的内嵌 XMP 读写用包里自带的 ExifTool（`_internal\\exiftool_files\\`），无需另装。
"""
    target = package_dir / "构建信息.txt"
    target.write_text(text, encoding="utf-8")
    return target


def zip_package(package_dir: Path, zip_path: Path) -> int:
    """把产物目录压成一个 zip（内含一层 AutoColorDiffusion\\ 目录）。"""
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                zf.write(path, Path(INNER_DIR) / path.relative_to(package_dir))
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键发布：守门检查 + 构建信息 + 打压缩包")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST,
                        help=f"产物目录（默认 {DEFAULT_DIST.relative_to(ROOT)}）")
    parser.add_argument("--out", type=Path, default=Path.home() / "Desktop",
                        help="压缩包输出目录（默认：桌面）")
    parser.add_argument("--no-zip", action="store_true", help="只做守门与构建信息，不压缩")
    args = parser.parse_args(argv)

    package_dir: Path = args.dist
    if not (package_dir / "AutoColorDiffusion.exe").is_file():
        print(f"找不到产物：{package_dir}")
        print("先打包：.venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean acb.spec")
        return 2

    # 1) 守门：不通过就别发布（宁可发不出去，也不能把开发者的东西发出去）
    check = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_package.py"), str(package_dir)],
        cwd=str(ROOT), text=True, encoding="utf-8", errors="replace",
    )
    if check.returncode != 0:
        print("守门检查未通过，中止发布。")
        return check.returncode

    # 2) 构建信息 + 顶层说明文档（PyInstaller 会把 datas 放进 _internal，这里再放一份到最外层）
    info = write_build_info(package_dir)
    copied: list[str] = []
    for name in TOP_LEVEL_DOCS:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, package_dir / name)
            copied.append(name)
    print(f"已写入 {info.name}" + (f"；并复制 {', '.join(copied)}" if copied else ""))

    if args.no_zip:
        return 0

    # 3) 压缩包
    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / f"AutoColorDiffusion-v{__version__}-win64.zip"
    count = zip_package(package_dir, zip_path)
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"已压缩 {count} 个文件 → {zip_path}")
    print(f"压缩包体积 {size_mb:.1f} MB")
    print(f"压缩包 SHA-256：{sha256_of(zip_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
