# -*- coding: utf-8 -*-
"""发布前守门：扫描打包产物，确认「该有的都有、不该有的一样都没有」。

用法
----
    python tools/check_package.py dist\\AutoColorDiffusion
    python tools/check_package.py dist\\AutoColorDiffusion --json report.json

它回答两个问题（这两个问题靠肉眼翻几百个文件是查不出来的）：

1. **该有的都在吗** —— models.yaml、两个内置风格种子、jsx 模板、图标、
   ICC profile、Qt 中文翻译、README / NOTICE。少一样，用户那边就会表现为
   "某个功能悄悄不工作"（配色不对、菜单变英文、DNG 写不进去……）。
2. **不该有的一样都没有吗** —— 开发者自己的照片、测试数据、训练出来的风格
   档案、日志、缩略图缓存、临时脚本、.venv。打包时"顺手把整个目录打进去"
   是这类泄漏最常见的成因（`styles/` 曾经就是整目录打包的），
   所以这里按**文件名与后缀**穷举扫描，而不是抽查。

为什么不做成自检里的一条：它需要一个**已经构建好的**产物目录，而自检
（smoke_test / gui_smoke_test）是在源码树上跑的，拿不到 dist。所以它是
release 流程里单独的一步 —— 打包完立刻跑它，非零退出就别发布。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acb.constants import AUTOPILOT_STYLE_NAME, DEFAULT_STYLE_NAME  # noqa: E402
from acb.paths import exiftool_argv_prefix  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 产物里必须存在的文件（相对产物根目录，用 / 分隔）。
REQUIRED_FILES = (
    "AutoColorDiffusion.exe",
    "_internal/config/models.yaml",
    f"_internal/styles/{DEFAULT_STYLE_NAME}.json",
    f"_internal/styles/{AUTOPILOT_STYLE_NAME}.json",
    "_internal/assets/jsx/export_batch.jsx",
    "_internal/assets/icon/app-256.png",
    "_internal/assets/icon/app-512.png",
    "_internal/assets/icc/sRGB.icc",
    "_internal/assets/icc/AdobeRGB1998-compatible.icc",
    "_internal/PyQt6/Qt6/translations/qtbase_zh_CN.qm",
    "_internal/README.md",
    "_internal/NOTICE.txt",
    # 许可证必须随包（许可条款第三条 a 款：每一份拷贝都要带完整许可证文本）
    "_internal/LICENSE",
    # exiftool：**整份**捆绑（启动器 exe + exiftool_files\ 里的 Perl 运行时）。
    # 只拷 exe 是经典坑：官方那个 exe 只是个启动器，缺了 exiftool_files 就跑不起来，
    # 表现为“DNG 写不进去”而界面上看不出原因。所以三个文件都点名要，
    # 并且下面会真的调一次 `exiftool.exe -ver`。
    "_internal/exiftool.exe",
    "_internal/exiftool_files/exiftool.pl",
    "_internal/exiftool_files/LICENSE",
)

# 允许出现在 _internal/styles/ 里的**只有**内置种子；多一个就是泄漏。
ALLOWED_STYLE_FILES = (f"{DEFAULT_STYLE_NAME}.json", f"{AUTOPILOT_STYLE_NAME}.json")

# 照片 / 侧车 / 测试素材的后缀：产物里出现任何一个都说明打包范围错了。
FORBIDDEN_SUFFIXES = (
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".raf",
    ".rw2", ".rwl", ".raw", ".dng", ".pef", ".srw", ".3fr", ".iiq", ".x3f", ".mrw",
    ".jpg", ".jpeg", ".jpe", ".png.tmp", ".xmp", ".psd", ".tif", ".tiff", ".heic",
    ".log", ".pyc.tmp", ".bak", ".db-journal",
)

# 目录 / 文件名里出现这些词就是泄漏（大小写不敏感）。
# 注意：png/jpg 之类的后缀已在上面的列表里，这里只管"名字"。
FORBIDDEN_NAMES = (
    "test_data", "testdata", "sample_photos", "samples", ".venv", "venv",
    "__pycache__", ".git", ".vscode", "_tmp_", "failed.json", "backups",
    "thumbs", "acb_smoke_appdata", "acb_gui_appdata",
    # 设计稿源图（用户的原创文件）与开发期文件：都不该出现在成品里
    "source.png", "source-circles", "acb.spec", "build.bat", "requirements.txt",
)

# 允许的 .png（图标）没有被 FORBIDDEN_SUFFIXES 拦下：png 不在禁列里。
# 但缩略图缓存目录名叫 thumbs/cache，靠 FORBIDDEN_NAMES 兜住。


def scan(package_dir: Path) -> tuple[list[str], list[str], dict[str, object]]:
    """返回 (失败项, 提示项, 统计)。"""
    failures: list[str] = []
    notes: list[str] = []
    files = [p for p in package_dir.rglob("*") if p.is_file()]
    total_bytes = sum(p.stat().st_size for p in files)

    for rel in REQUIRED_FILES:
        if not (package_dir / rel).is_file():
            failures.append(f"缺少必需文件：{rel}")

    for path in files:
        rel = path.relative_to(package_dir).as_posix()
        lowered = rel.lower()
        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            failures.append(f"发现不该打包的文件（后缀 {suffix}）：{rel}")
        for bad in FORBIDDEN_NAMES:
            if bad in lowered:
                failures.append(f"发现不该打包的名字（含 {bad!r}）：{rel}")
        if lowered.startswith("_internal/styles/"):
            if path.name not in ALLOWED_STYLE_FILES:
                failures.append(f"styles/ 里有非内置风格（会把开发者的风格发给用户）：{rel}")

    # 顶层目录清单：只允许 exe + _internal（PyInstaller onedir 的标准形态），
    # 外加 tools/make_release.py 发布时放到最外层的那三个文件。
    top = sorted({p.name for p in package_dir.iterdir()})
    allowed_top = {"AutoColorDiffusion.exe", "_internal",
                   "README.md", "NOTICE.txt", "LICENSE", "构建信息.txt"}
    extra_top = [n for n in top if n not in allowed_top]
    if extra_top:
        failures.append(f"产物根目录出现了预期之外的东西：{extra_top}")

    exe = package_dir / "AutoColorDiffusion.exe"
    stats: dict[str, object] = {
        "package": str(package_dir),
        "files": len(files),
        "size_mb": round(total_bytes / 1024 / 1024, 1),
        "exe_mb": round(exe.stat().st_size / 1024 / 1024, 1) if exe.is_file() else None,
        "top_level": top,
        "styles": sorted(p.name for p in (package_dir / "_internal" / "styles").glob("*")
                         if p.is_file()) if (package_dir / "_internal" / "styles").is_dir() else [],
    }

    # 真的调一次捆绑的 exiftool：确认它是**可用的整份**，而不是一个跑不动的壳。
    # 用 exiftool_argv_prefix() 而不是直接跑 exiftool.exe —— 那正是程序运行时走的路径
    # （官方 Windows 包的启动器 exe 在打包目录里跑不起来，得走同目录的 perl）。
    bundled_et = package_dir / "_internal" / "exiftool.exe"
    if bundled_et.is_file():
        argv = exiftool_argv_prefix(bundled_et)
        how = " + ".join(Path(a).name for a in argv)
        try:
            probe = subprocess.run([*argv, "-ver"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60,
                                   stdin=subprocess.DEVNULL)
            version = (probe.stdout or "").strip()
            if probe.returncode == 0 and version[:1].isdigit():
                stats["exiftool"] = f"{version}（{how}）"
            else:
                failures.append(
                    "捆绑的 exiftool 跑不起来："
                    f"rc={probe.returncode} stdout={version[:120]!r} "
                    f"stderr={(probe.stderr or '').strip()[:200]!r} "
                    f"（调用方式：{how}）—— 通常是只拷了 exiftool.exe、漏了 exiftool_files\\"
                )
                stats["exiftool"] = "失败"
        except Exception as exc:                     # noqa: BLE001 - 探针失败就是检查失败
            failures.append(f"捆绑的 exiftool 无法执行（{how}）：{exc}")
            stats["exiftool"] = "失败"

    return failures, notes, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前扫描打包产物：该有的都有、不该有的都没有")
    parser.add_argument("package", type=Path, help="产物目录，如 dist\\AutoColorDiffusion")
    parser.add_argument("--json", type=Path, default=None, help="把统计写到这个 JSON 文件")
    args = parser.parse_args(argv)

    package_dir = args.package
    if not package_dir.is_dir():
        print(f"找不到产物目录：{package_dir}")
        print("先打包：.venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean acb.spec")
        return 2

    failures, notes, stats = scan(package_dir)
    if args.json is not None:
        args.json.write_text(
            json.dumps({"stats": stats, "failures": failures, "notes": notes},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("=" * 78)
    print(f"打包产物检查：{package_dir}")
    print("=" * 78)
    print(f"  文件数 {stats['files']}，合计 {stats['size_mb']} MB（exe {stats['exe_mb']} MB）")
    print(f"  根目录：{stats['top_level']}")
    print(f"  styles/：{stats['styles']}")
    print(f"  捆绑的 exiftool：{stats.get('exiftool', '未捆绑')}")
    for note in notes:
        print(f"  提示：{note}")
    if failures:
        print()
        for item in failures:
            print(f"  [失败] {item}")
        print()
        print(f"检查未通过：{len(failures)} 项。别发布这份产物。")
        return 1
    print()
    print("检查通过：必需资源齐全，且没有发现照片 / 测试数据 / 训练风格 / 日志 / 缓存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
