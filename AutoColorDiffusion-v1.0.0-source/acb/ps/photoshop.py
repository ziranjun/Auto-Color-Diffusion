# -*- coding: utf-8 -*-
"""通过 win32com 调用 Photoshop（回答问题 a 的关键片段）。

COM 调用链
----------
    win32com.client.Dispatch("Photoshop.Application")   ← 未运行时自动拉起 PS
    app.DisplayDialogs = 3                              ← psDisplayNoDialogs，抑制模态框
    app.DoJavaScriptFile(<jsx 绝对路径>)                 ← 执行磁盘上的脚本，返回脚本的 return 值

为什么用 DoJavaScriptFile 而不是 DoJavaScript：
    DoJavaScript(script_string) 需要把整段脚本塞进 COM 字符串参数，
    会碰到转义（引号/反斜杠/中文）与参数长度上限两类问题。
    DoJavaScriptFile 只传一个路径，脚本内容由 PS 自己从磁盘读，
    既绕开转义，也让脚本可以被单独编辑与调试。

线程注意
--------
COM 要求调用线程先初始化：在 QThread 里必须显式调用
pythoncom.CoInitialize() / CoUninitialize()，否则会抛
"CoInitialize has not been called"。这一点在 worker 线程里极易踩坑。

降级方案（问题 a 要求说明）
--------------------------
按严重程度分三档，都不"假造像素"：
    1. PS 未安装（COM 报 0x80040154 / Class not registered）
       → 仍然产出 XMP + export_batch.jsx + manifest.json + run_export.bat，
         用户可拷到装了 PS 的机器上双击 bat 执行。
    2. PS 已装但 COM 不可用（被组策略禁用、ProgID 未注册）
       → 同上，并提示手动「文件 → 脚本 → 浏览」。
    3. PS 在授权对话框上挂起
       → 已设 psDisplayNoDialogs 抑制常规对话框，但授权窗口无法程序化跳过，
         因此整个 COM 调用包在 try/except 里，并把超时情况解释给用户。
    明确不做：用 rawpy/dcraw 复刻 ACR 渲染。
       ACR 的 PV5/PV6 渲染管线是闭源的，dcraw 式解码在色调曲线、
       相机配置文件（crs:CameraProfile）、镜头 LCP 校正上与 ACR 不同源，
       产出会与用户在 ACR 里看到的**不一致**——那属于误导性降级，
       不如明确告知"请用 Photoshop 导出"。
"""

from __future__ import annotations

from dataclasses import dataclass

import sys
from pathlib import Path

from ..errors import PhotoshopUnavailableError, UnsupportedPlatformError
from ..logging_setup import get_logger

log = get_logger("ps")

# Photoshop 的 ProgID。CS6 及以后的版本都用这个 ProgID。
PHOTOSHOP_PROGID = "Photoshop.Application"

# 脚本的兜底文件名。manifest 里指定的路径（软件日志目录）建不出来时会用到，
# 典型场景是把整个出片目录拷到另一台机器执行（方式 C）。
PS_LOG_FALLBACK_NAME = "ps_log.txt"
PS_RESULT_FALLBACK_NAME = "ps_result.json"

# PhotoShop 的 DisplayDialogs 常量值（对应 ExtendScript 的 DialogModes）：
#   1 = psDisplayAllDialogs   2 = psDisplayErrorDialogs   3 = psDisplayNoDialogs
PS_DISPLAY_NO_DIALOGS = 3

# COM 错误码：类未注册。这是"PS 未安装"最典型的症状。
COM_ERROR_CLASS_NOT_REGISTERED = -2147221164  # 0x80040154

_availability_cache: tuple[bool, str] | None = None


def _require_windows() -> None:
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            "Photoshop 自动导出依赖 Windows COM（win32com.client），仅在 Windows 上可用。\n"
            "本项目不做 macOS/Linux 实现。"
        )


def _parse_exe_path(raw: str) -> str | None:
    """从注册表值里解析出 exe 路径。

    LocalServer32 的值有多种写法：
        "C:\\Program Files\\Adobe\\Adobe Photoshop 2025\\Photoshop.exe"
        C:\\Program Files\\Adobe\\Adobe Photoshop 2025\\Photoshop.exe /Automation
        "C:\\...\\Photoshop.exe" "%1"
    因此不能直接当路径用：先剥引号，再截到 ".exe" 为止。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end]
    index = text.lower().find(".exe")
    if index >= 0:
        return text[: index + 4]
    return text


def _find_photoshop_exe_via_registry() -> tuple[Path | None, str]:
    """通过注册表查找 Photoshop，**不启动任何进程**。

    为什么不用 Dispatch 探测（这是修掉的一个真实隐患）：
        `win32com.client.Dispatch("Photoshop.Application")` 在 Photoshop 未运行时
        **会把 Photoshop 真的拉起来**。Photoshop 冷启动要 10–30 秒，
        于是用户双击程序后会看到界面"卡住"半天；更糟的是，
        仅为了"检查是否安装"就占用数百 MB 内存并弹出版权提示，
        完全不符合用户预期。
    因此改为查注册表：
        HKCR\\Photoshop.Application\\CLSID          -> 拿到 CLSID
        HKCR\\CLSID\\{CLSID}\\LocalServer32          -> 拿到 Photoshop.exe 路径
        校验该 exe 是否真实存在。
    真正的 Dispatch 只在 run_export_script() 里发生——那时确实需要 PS 运行。

    返回 (exe 路径, 失败原因)。exe 为 None 表示未检测到。
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{PHOTOSHOP_PROGID}\\CLSID") as key:
            clsid, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None, (
            f"注册表 HKEY_CLASSES_ROOT\\{PHOTOSHOP_PROGID}\\CLSID 不存在，"
            "通常表示本机未安装 Photoshop（或安装未完成、COM 组件被清理过）"
        )

    clsid_text = str(clsid).strip()
    if clsid_text and not clsid_text.startswith("{"):
        clsid_text = "{" + clsid_text + "}"

    for server in ("LocalServer32", "InprocServer32"):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid_text}\\{server}"
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        exe_text = _parse_exe_path(str(raw))
        if not exe_text:
            return None, f"CLSID {clsid_text} 的 {server} 值为空"
        exe = Path(exe_text)
        if exe.is_file():
            return exe, ""
        return None, f"注册表记录的 Photoshop 路径不存在或已被移动：{exe}"

    return None, f"CLSID {clsid_text} 下没有可用的 LocalServer32 / InprocServer32 记录"


def probe(refresh: bool = False) -> tuple[bool, str]:
    """探测 Photoshop 是否可用（结果缓存）。

    探测方式是**查注册表**，不会启动 Photoshop 进程 —— 详见
    _find_photoshop_exe_via_registry() 的注释。

    返回 (是否可用, 说明文本)。
    """
    global _availability_cache
    if _availability_cache is not None and not refresh:
        return _availability_cache

    _require_windows()

    try:
        import win32com.client  # noqa: F401  （仅确认 pywin32 已安装，不 Dispatch）
    except ImportError as exc:
        message = (
            f"未安装 pywin32，无法调用 Photoshop：{exc}\n"
            "请执行：python -m pip install pywin32"
        )
        _availability_cache = (False, message)
        return _availability_cache

    exe, reason = _find_photoshop_exe_via_registry()
    if exe is not None:
        message = f"检测到 Photoshop：{exe}（注册表探测，未启动 Photoshop 进程）"
        _availability_cache = (True, message)
        log.info(message)
    else:
        message = (
            "未检测到可用的 Photoshop COM 组件，程序将只产出 XMP 与导出脚本。\n"
            f"原因：{reason}\n"
            "这不影响调色参数本身——旁侧 XMP 已经写好，你也可以手动完成导出。"
        )
        _availability_cache = (False, message)
        log.warning(message)

    return _availability_cache


def candidate_result_paths(
    jsx_path: Path, result_path: Path | None = None
) -> list[Path]:
    """脚本可能写出结果文件的位置，按优先级排列。

    第一优先是 manifest 指定的位置（软件的日志目录）；
    第二是脚本同目录的兜底位置——方式 C 会把产物拷到另一台机器执行，
    那边 manifest 里的绝对路径可能建不出来，脚本就会退回脚本同目录。
    两个位置都要查，否则"拷到别的机器执行"这条路径会永远读不到结果。
    """
    paths: list[Path] = []
    if result_path is not None:
        paths.append(Path(result_path))
    paths.append(jsx_path.parent / PS_RESULT_FALLBACK_NAME)
    return paths


def describe_log_location(
    log_path: Path | None = None, jsx_path: Path | None = None
) -> str:
    """给用户看的"日志在哪"提示。"""
    if log_path is not None:
        return str(log_path)
    if jsx_path is not None:
        return str(jsx_path.parent / PS_LOG_FALLBACK_NAME)
    return "本次运行的脚本目录（软件数据目录下的 runs 子目录）"


def manual_guidance(
    script_dir: Path | None = None,
    log_path: Path | None = None,
    jsx_path: Path | None = None,
) -> str:
    """生成"如何手动完成导出"的说明（降级时展示给用户，也写进 README）。

    script_dir 是本次运行的**脚本目录**（里面有 manifest.json 与 export_batch.jsx）。
    历史上这个参数是出片目录 —— 那时脚本就生成在出片目录里；
    现在脚本统一放在软件数据目录下的运行目录，参数随之改名。
    """
    where = str(script_dir) if script_dir else "<脚本目录>"
    log_where = describe_log_location(log_path, jsx_path)
    return (
        "Photoshop 自动导出不可用，但你的调色参数已经完整生效。请按以下任一方式完成出图：\n"
        "\n"
        "方式 A（推荐，无需 Photoshop 参与批量导出）\n"
        "  在 Adobe Camera Raw 或 Lightroom 里打开这批 RAW，\n"
        "  ACR 会自动读取同目录的旁侧 .xmp 设置；全选后「同步设置」即可批量套用，\n"
        "  再用 ACR 自己的「存储图像」对话框导出 JPG。\n"
        "  注意：DNG 的设置已写回文件内部，同样会被自动读取。\n"
        "\n"
        "方式 B（用本程序已生成的脚本）\n"
        f"  1. 打开脚本目录：{where}\n"
        "  2. 双击 run_export.bat（脚本会自动查找 Photoshop.exe）；或\n"
        "     打开 Photoshop，用「文件 → 脚本 → 浏览」选择 export_batch.jsx；或\n"
        "     命令行执行：Photoshop.exe \"<完整路径>\\export_batch.jsx\"\n"
        "  3. 日志与结果就在**同一个脚本目录**里，不在出片目录里：\n"
        f"     {log_where}\n"
        "     ps_log.txt 是执行日志（哪一项失败、为什么），ps_result.json 是逐项结果。\n"
        f"  ⚠ 这个脚本目录（{where}）在**关闭程序时会被自动清理**。\n"
        "     所以要手动补跑，请在关闭程序之前做完；\n"
        "     或者现在就把整个目录复制到别处，再慢慢跑。\n"
        "\n"
        "方式 C（在另一台装了 Photoshop 的机器上执行）\n"
        "  把上面整个脚本目录拷贝过去（manifest.json 必须和 export_batch.jsx 同目录）。\n"
        "  注意 manifest 里是绝对路径，若源文件/输出位置变了，\n"
        "  请手工编辑 manifest.json 里的 raw/out 路径。\n"
        "  日志与结果会写到 manifest 里 logPath/resultPath 指定的位置；\n"
        "  若那台机器上建不出来，会自动退回脚本所在目录（同样叫 ps_log.txt / ps_result.json）。\n"
        "\n"
        "前置检查（很关键）：\n"
        "  Camera Raw 首选项 →「将图像设置存储在侧车 .xmp 文件中」必须勾选，\n"
        "  否则 Photoshop / ACR 打开 RAW 时不会读取旁侧 XMP，导出结果将不含任何调整。"
    )


@dataclass(frozen=True)
class ExportOutcome:
    """一次 Photoshop 导出的**结构化**结果。

    【为什么不能只看状态文本】真实事故：导出失败时状态文本是
    「完成（成功 0/1，失败 1）」—— 里面含"成功"二字，于是
    界面/命令行里 `"成功" not in ps_status` 这个判断得出
    "不需要手动补跑"，进而把唯一能看失败原因的 runs/<标识>/（含 ps_log.txt）
    当垃圾删掉了。

    结论：凡是"是否成功""要不要保留证据"这类**决策**，必须看结构化字段，
    绝不能对给人看的文案做子串匹配。
    """

    # 给人看的状态文本（进日志、摘要、完成对话框）
    status: str
    # 是否**全部**成功。部分失败也算 False —— 那种情况下更要留下日志。
    ok: bool
    ok_count: int = 0
    fail_count: int = 0


def run_export_script(
    jsx_path: Path,
    callbacks=None,
    *,
    result_path: Path | None = None,
    log_path: Path | None = None,
) -> ExportOutcome:
    """执行导出脚本。返回 ExportOutcome（状态文本 + 结构化的是否成功）。

    调用方**必须**用返回值的 `.ok` 去做"要不要保留脚本目录/给手动指引"这类决策，
    不要拿 `.status` 做子串匹配（原因见 ExportOutcome 的注释）。

    result_path / log_path 是 Python 侧算好、并已写进 manifest 的落盘位置
    （软件的日志目录下，文件名带运行标识）。两者都可以是 None，
    此时退回"与脚本同目录"的老行为，保证兼容。

    不抛异常（除了非 Windows）：任何失败都转成"状态文本 + 手动指引"，
    因为导出失败不应让已经成功写出的 XMP 与参数白费。
    """

    def emit(level: int, message: str) -> None:
        if callbacks is not None and hasattr(callbacks, "log"):
            try:
                callbacks.log(message, level)
            except Exception:
                pass

    try:
        available, reason = probe()
    except UnsupportedPlatformError as exc:
        return ExportOutcome(status=str(exc), ok=False)

    if not available:
        emit(30, reason)
        emit(20, manual_guidance(jsx_path.parent, log_path=log_path, jsx_path=jsx_path))
        return ExportOutcome(status=f"未执行自动导出（{reason}）", ok=False)

    if not jsx_path.is_file():
        message = f"脚本文件不存在：{jsx_path}"
        emit(40, message)
        return ExportOutcome(status=f"失败：{message}", ok=False)

    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        message = f"pywin32 不可用：{exc}"
        emit(40, message)
        return ExportOutcome(status=f"失败：{message}", ok=False)

    # 调用前先删掉上一次残留的结果文件（含兜底位置）。
    # 【为什么必须删】脚本可能因为用户手动关掉 Photoshop、或调用被中断而**没有**
    # 写出结果，此时若残留着上一次的 ps_result.json，我们就会把上次的
    # "成功 12/12"当成这一次的结果报给用户——这是最危险的一类静默错误。
    for stale in candidate_result_paths(jsx_path, result_path):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass

    # worker 线程必须显式初始化 COM（否则抛 CoInitialize has not been called）。
    pythoncom.CoInitialize()
    try:
        emit(20, f"正在通过 COM 调用 Photoshop 执行 {jsx_path.name} …")
        app = win32com.client.Dispatch(PHOTOSHOP_PROGID)
        # 抑制所有模态对话框：不设这一句，批处理会在某张图上弹窗并永久挂起。
        try:
            app.DisplayDialogs = PS_DISPLAY_NO_DIALOGS
        except Exception as exc:
            emit(30, f"无法设置 Photoshop 对话框模式（将继续尝试执行）：{exc}")

        # DoJavaScriptFile 会同步等待脚本执行完毕；返回值是脚本的 return 值
        # （我们的 jsx 用 IIFE 且没有 return，因此返回 None，属正常）。
        app.DoJavaScriptFile(str(jsx_path))

    except Exception as exc:
        hint = ""
        text = str(exc)
        # "被调用者拒绝呼叫" / "Call was rejected by callee" 通常表示
        # Photoshop 正在弹窗（授权、更新提示）或忙于其它任务。
        if "reject" in text.lower() or "被调用者拒绝" in text:
            hint = (
                "\n提示：Photoshop 可能正在显示对话框（如授权/更新提示）或正忙。"
                "请手动打开 Photoshop，关掉所有对话框后重试；"
                "或直接使用输出目录下的 run_export.bat 手动执行。"
            )
        emit(40, f"执行 Photoshop 脚本失败：{exc}{hint}")
        emit(20, manual_guidance(jsx_path.parent, log_path=log_path, jsx_path=jsx_path))
        return ExportOutcome(
            status=f"失败：Photoshop 调用出错（{text[:120]}）", ok=False
        )
    finally:
        pythoncom.CoUninitialize()

    # --- 读取脚本写出的结果文件 --------------------------------------------
    # 脚本优先写到 manifest 指定的位置（软件日志目录），建不出来才退回同目录，
    # 所以两个位置都找，顺序即优先级。
    result_file = next(
        (p for p in candidate_result_paths(jsx_path, result_path) if p.is_file()),
        None,
    )
    if result_file is not None:
        try:
            import json

            data = json.loads(result_file.read_text(encoding="utf-8"))
            ok = int(data.get("ok") or 0)
            failed = int(data.get("failed") or 0)
            total = int(data.get("total") or 0)
            fatal = str(data.get("fatal") or "").strip()
            if fatal:
                # 脚本在"读取任务清单"阶段就失败了（例如 manifest.json 解析失败）。
                # 这类错误会写出 fatal 字段并让 total 保持 0。
                # 早期版本不认这个字段，于是只会报含糊的"结果未知"，
                # 把真正的失败原因（比如脚本引擎里根本没有 JSON）完全掩盖掉。
                emit(30, f"Photoshop 脚本未能开始导出：{fatal}")
                emit(20, manual_guidance(jsx_path.parent, log_path=log_path, jsx_path=jsx_path))
                return ExportOutcome(status=f"失败：{fatal}", ok=False)
            if failed:
                log_where = describe_log_location(log_path, jsx_path)
                emit(30, f"Photoshop 导出完成：成功 {ok}/{total}，失败 {failed}。详见 {log_where}")
                # 部分失败也算"没成功"：这时候**更要**留下 runs/ 目录，
                # 因为 ps_log.txt 里写着每一项为什么失败。
                return ExportOutcome(
                    status=f"完成（成功 {ok}/{total}，失败 {failed}，详见 {log_where}）",
                    ok=False,
                    ok_count=ok,
                    fail_count=failed,
                )
            emit(20, f"Photoshop 导出完成：成功 {ok}/{total}。")
            return ExportOutcome(
                status=f"成功导出 {ok}/{total} 张", ok=True, ok_count=ok, fail_count=0
            )
        except (OSError, ValueError) as exc:
            emit(30, f"读取结果文件失败（{result_file}）：{exc}")

    # 脚本执行了但没写出结果文件：可能用户在 PS 里手动取消了，或脚本被中断。
    looked = "；".join(str(p) for p in candidate_result_paths(jsx_path, result_path))
    message = (
        "Photoshop 脚本已执行，但未找到结果文件，无法确认导出结果。\n"
        f"已查找：{looked}\n"
        f"脚本日志（若已写出）：{describe_log_location(log_path, jsx_path)}"
    )
    emit(30, message)
    return ExportOutcome(status="完成（结果未知，请查看 ps_log.txt）", ok=False)


def reset_cache() -> None:
    """清空可用性缓存（设置界面「重新检测 Photoshop」用）。"""
    global _availability_cache
    _availability_cache = None
