# -*- coding: utf-8 -*-
"""Photoshop 脚本产物的渲染（manifest / jsx / bat）。

产物清单（全部落在输出目录下）：
    export_batch.jsx   ExtendScript 导出脚本（从打包模板复制）
    manifest.json      任务清单：每项的 raw 路径、out 路径、色彩空间、JPEG 品质
    run_export.bat     双击即可手动执行的批处理（自动定位 Photoshop.exe）
    ps_log.txt         由 jsx 在运行时写出（逐项成功/失败记录）
    ps_result.json     由 jsx 在运行时写出（结构化结果）

为什么 jsx 是"模板 + 外部 manifest"而不是把任务硬编码进脚本：
    1. manifest 可读可改，用户能手工剔除某几项后重跑，不必重新走一遍流程；
    2. jsx 保持静态，便于单独调试与在其它机器上复用；
    3. 所有易错的命名/去重逻辑都在可测试的 Python 侧（见 output_mode.build_export_plan）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import runs_dir
from ..constants import (
    COLOR_SPACE_TO_PS_PROFILE,
    DEFAULT_COLOR_SPACE,
    DEFAULT_EXPORT_FORMAT,
    LIBJPEG_EQUIVALENT,
    describe_ps_quality,
    extension_for_format,
    is_lossless_format,
    resolve_export_format,
    resolve_ps_quality,
)
from ..logging_setup import get_logger
from ..paths import jsx_template_path

log = get_logger("ps.jsx")

MANIFEST_FILENAME = "manifest.json"
JSX_FILENAME = "export_batch.jsx"
BAT_FILENAME = "run_export.bat"

# Photoshop 侧的日志与结果文件**不落在用户的出片目录里**，统一放到软件的日志目录：
#     <数据目录>/logs/photoshop/ps_export_<run_id>.txt
#     <数据目录>/logs/photoshop/ps_result_<run_id>.json
# 原因：出片目录是用户的交付目录，只该有 JPG。这两个是软件的记账文件，
#       混在里面既碍事，又容易在打包交付时被一起发出去。
#
# 文件名带一次运行的标识，是为了让日志与某次批量一一对应；
# 不带标识（固定叫 ps_log.txt）会互相覆盖，而且"结果文件残留在那里"
# 会让 Python 把上一次的结果当成这一次的——这是最危险的一类静默错误。
# 运行目录内的固定文件名。
# 每次运行有独立目录，所以文件名不必再带运行标识；固定名字还有个好处：
# jsx 的"写不进去就退回脚本同目录"兜底逻辑里，两个目录的文件名完全一致。
PS_LOG_FILENAME = "ps_log.txt"
PS_RESULT_FILENAME = "ps_result.json"


def make_run_id(seed: Path) -> str:
    """生成一次导出运行的标识，形如 20260919_115143_a1b2。

    为什么不用纯自增序号：目录名要能一眼看出时间，便于事后翻找。
    为什么补 4 位哈希：同一秒内对同一批输入重复生成会得到**同一个** id
    （这是期望行为——复用同一个运行目录，而不是堆出一串空目录），
    而不同批次不会互相覆盖。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:4]
    return f"{stamp}_{digest}"


def script_dir_for(seed: Path) -> Path:
    """本次运行的脚本产物目录：`<数据目录>/runs/<运行标识>/`。

    seed 只用来生成稳定的运行标识（调用方通常传第一个输出文件所在目录）。
    """
    return runs_dir() / make_run_id(seed)

# manifest 结构版本。jsx 目前不校验它，但保留字段便于将来做兼容分支。
MANIFEST_VERSION = 1

# 批处理文件编码：UTF-8（配合 chcp 65001）。
# Windows 的 cmd 默认用 ANSI 代码页读 bat 文件，遇到中文路径会乱码，
# 因此必须在开头 chcp 65001 切到 UTF-8，并用 BOM 之外的纯 UTF-8 保存。
# 经验：**不要**给 bat 文件加 BOM，加了会让 cmd 把第一行当作乱码。
_BAT_TEMPLATE = """@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem =========================================================================
rem  Auto Color Diffusion —— 手动执行 Photoshop 批量导出
rem  本文件由程序自动生成。若不需要手动执行，可忽略它——
rem  程序会优先尝试通过 COM 自动调用 Photoshop。
rem =========================================================================

set "SCRIPT_DIR=%~dp0"
set "PS="

rem --- 查找方式 1：Adobe 的常见安装目录（按年份倒序，取第一个命中的）---------
for %%Y in (2026 2025 2024 2023 2022 2021 2020) do (
    if not defined PS (
        if exist "C:\\Program Files\\Adobe\\Adobe Photoshop %%Y\\Photoshop.exe" (
            set "PS=C:\\Program Files\\Adobe\\Adobe Photoshop %%Y\\Photoshop.exe"
        )
    )
)

rem --- 查找方式 2：注册表的 App Paths（能覆盖非默认安装位置）---------------
if not defined PS (
    for /f "tokens=2,*" %%A in ('reg query "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\Photoshop.exe" /ve 2^>nul ^| findstr REG_SZ') do (
        if not defined PS set "PS=%%B"
    )
)

rem --- 查找方式 3：PATH 环境变量 -------------------------------------------
if not defined PS (
    for /f "delims=" %%A in ('where Photoshop.exe 2^>nul') do (
        if not defined PS set "PS=%%A"
    )
)

if not defined PS (
    echo.
    echo [错误] 未找到 Photoshop.exe
    echo.
    echo 请手动执行导出，方法二选一：
    echo   1. 打开 Photoshop，用「文件 - 脚本 - 浏览」选择本目录下的 {jsx_name}
    echo   2. 把 Photoshop.exe 的完整路径手工填到本文件的 set "PS=" 一行
    echo.
    echo 另外请确认 Camera Raw 首选项已勾选「将图像设置存储在侧车 .xmp 文件中」，
    echo 否则导出结果不会包含任何调色参数。
    echo.
    pause
    exit /b 1
)

echo 使用 Photoshop："!PS!"
echo 脚本："%SCRIPT_DIR%{jsx_name}"
echo.

"!PS!" "%SCRIPT_DIR%{jsx_name}"

echo.
echo 执行结束（退出码 %ERRORLEVEL%）。
echo 详细结果见本目录下的 ps_log.txt 与 ps_result.json。
pause
"""


@dataclass
class ExportPlan:
    """一次脚本产物生成的路径集合。"""

    # 本次运行的脚本产物目录（在软件数据目录下，**不在出片目录里**）。
    # 里面放着 manifest.json / export_batch.jsx / run_export.bat，
    # 以及 Photoshop 写出的 ps_log.txt / ps_result.json。
    script_dir: Path
    jsx_path: Path
    manifest_path: Path
    bat_path: Path
    # Photoshop 侧要写的两个文件的实际位置（就在 script_dir 里）。
    # 调用方要把它们传给 photoshop.run_export_script，才能正确清理与读取。
    ps_log_path: Path
    ps_result_path: Path
    item_count: int
    # 本次实际使用的导出格式（JPG / PNG）与扩展名。
    # 放进来是为了让界面/日志能说清"导出的到底是什么"。
    export_format: str
    # 质量值现在只有一个权威表示（Photoshop 0–12），这个字段是仅供展示的
    # libjpeg 等效值，不参与编码。**只对 JPG 有意义**。
    libjpeg_equivalent: int
    ps_quality: int
    color_space: str
    color_space_profile: str


def resolve_quality(quality_value: int | str) -> tuple[int, int, str | None]:
    """把用户给出的质量值解析为 (Photoshop 刻度, libjpeg 等效值, 提示文字)。

    质量值的**唯一权威表示是 Photoshop 的 0–12 刻度**，因为那才是真正传给
    JPEGSaveOptions.quality 的数。libjpeg 等效值只用于界面提示与日志，
    不参与任何编码决策。

    接受旧档位名（低/中/高/最高）是为了兼容已经写好的命令行脚本；
    发生这种转换时会通过第 3 个返回值给出提示，由调用方记入日志——
    静默接受旧写法会让用户误以为旧档位仍然存在。
    """
    ps_quality, note = resolve_ps_quality(quality_value)
    return ps_quality, LIBJPEG_EQUIVALENT.get(ps_quality, -1), note


def resolve_color_space(color_space: str) -> str:
    """把界面色彩空间名解析为 Photoshop 的 ICC 配置文件描述名。"""
    profile = COLOR_SPACE_TO_PS_PROFILE.get(color_space)
    if profile is None:
        log.warning("未知色彩空间 %r，回退到 %s。", color_space, DEFAULT_COLOR_SPACE)
        profile = COLOR_SPACE_TO_PS_PROFILE[DEFAULT_COLOR_SPACE]
    return profile


def render_export_outputs(
    *,
    script_dir: Path,
    manifest_items: list[dict[str, Any]],
    color_space: str,
    quality_value: int | str,
    export_format: str = DEFAULT_EXPORT_FORMAT,
    dry_run: bool = False,
) -> ExportPlan:
    """生成 manifest.json / export_batch.jsx / run_export.bat。

    manifest_items 每项为 {"raw": 源绝对路径, "out": 输出绝对路径, "xmp": XMP 路径}
    ——输出路径已在 Python 侧算好（含 _E / _E_2 去重后缀），jsx 不再做命名逻辑。
    这些路径是**绝对路径**，所以脚本产物放在哪里都不影响出片位置。

    script_dir 是本次运行的脚本产物目录，由 script_dir_for() 算出
    （在软件数据目录下）。**刻意不是出片目录**：出片目录只该有 JPG。

    quality_value 直接就是 Photoshop 的 0–12 刻度（也接受旧档位名，见 resolve_quality）。
    """
    script_dir = Path(script_dir)
    script_dir.mkdir(parents=True, exist_ok=True)

    ps_quality, libjpeg_equivalent, quality_note = resolve_quality(quality_value)
    if quality_note:
        log.warning("质量参数：%s", quality_note)
    profile = resolve_color_space(color_space)
    export_format, format_note = resolve_export_format(export_format)
    if format_note:
        log.warning("导出格式：%s", format_note)
    extension = extension_for_format(export_format)
    lossless = is_lossless_format(export_format)

    # Photoshop 侧要写的日志与结果，就在同一个运行目录里。
    # 这样"一次运行的所有产物"在一个文件夹内，找起来不用东翻西找，
    # 关闭程序时也能整目录删掉。
    ps_log_path = script_dir / PS_LOG_FILENAME
    ps_result_path = script_dir / PS_RESULT_FILENAME

    manifest = {
        "version": MANIFEST_VERSION,
        "generated_by": "Auto Color Diffusion",
        "dry_run": bool(dry_run),
        "colorSpace": color_space,
        "colorSpaceProfile": profile,
        # 导出格式：JPG / PNG。jsx 据此选择 SaveOptions。
        "exportFormat": export_format,
        "exportExtension": extension,
        # 无损格式（PNG）不用 psJpegQuality；写进 manifest 是为了让
        # 脚本日志能说明"为什么画质值这次没起作用"。
        "lossless": lossless,
        # psJpegQuality 是**实际生效**的值，直接传给 JPEGSaveOptions.quality。
        # 仅当 exportFormat = JPG 时被使用。
        "psJpegQuality": ps_quality,
        "psJpegQualityHint": describe_ps_quality(ps_quality),
        # 仅供参考：让熟悉 libjpeg / Lightroom 的人有个体感对照，不参与编码。
        "libjpegEquivalent": libjpeg_equivalent,
        # 下面两个绝对路径告诉脚本把日志与结果写到哪。
        # 放在 manifest 里而不是写死在 jsx 里，是为了让**便携模式**也正确
        # （便携模式下数据目录在 exe 旁边，只有 Python 侧知道）。
        # 脚本侧有兜底：这两个路径写不进去时，会退回脚本同目录。
        "logPath": str(ps_log_path),
        "resultPath": str(ps_result_path),
        "items": manifest_items,
    }

    manifest_path = script_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- jsx：从打包模板复制 -------------------------------------------------
    # 用复制而不是生成字符串，是为了让用户能直接编辑 export_batch.jsx
    # 做一次性调整（例如加水印），而下一次运行又会得到干净的模板。
    template = jsx_template_path()
    if not template.is_file():
        raise FileNotFoundError(
            f"找不到 jsx 模板：{template}\n"
            "开发模式下请确认项目内存在 assets/jsx/export_batch.jsx；"
            "打包模式下请确认 spec 的 datas 已包含它。"
        )
    jsx_path = script_dir / JSX_FILENAME
    jsx_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    bat_path = script_dir / BAT_FILENAME
    bat_path.write_text(
        _BAT_TEMPLATE.replace("{jsx_name}", JSX_FILENAME),
        encoding="utf-8",
        newline="\r\n",  # bat 文件必须用 CRLF 换行，否则 cmd 可能解析异常
    )

    log.info(
        "已生成导出脚本产物：%s（%d 项，格式 %s，色彩空间 %s → %s，质量 %s）",
        script_dir,
        len(manifest_items),
        export_format,
        color_space,
        profile,
        describe_ps_quality(ps_quality),
    )

    return ExportPlan(
        script_dir=script_dir,
        jsx_path=jsx_path,
        manifest_path=manifest_path,
        bat_path=bat_path,
        ps_log_path=ps_log_path,
        ps_result_path=ps_result_path,
        item_count=len(manifest_items),
        export_format=export_format,
        libjpeg_equivalent=libjpeg_equivalent,
        ps_quality=ps_quality,
        color_space=color_space,
        color_space_profile=profile,
    )
