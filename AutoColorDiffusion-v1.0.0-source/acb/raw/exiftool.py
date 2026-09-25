# -*- coding: utf-8 -*-
"""exiftool 外部二进制封装。

硬约束 #10：
    exiftool 为外部二进制，程序启动时必须检测其可用性（exiftool -ver），
    不可用时给出清晰的安装指引（含 Windows 需把 exiftool.exe 加入 PATH）
    并降级使用 rawpy 提取预览。

用户裁决 #5：
    exiftool 查找顺序 = 优先 exe 同目录 _internal/，其次 PATH，都没有则降级 rawpy。

Windows 相关细节（这些是踩过坑的地方）：
  1. **不要加 `-charset filename=utf8`**（加它反而找不到中文路径；
     详见 _BASE_ARGS 上方的实测记录）。只保留 `-charset exiftool=utf8`。
  2. 必须加 creationflags=CREATE_NO_WINDOW：
     否则 GUI 程序每次调用 exiftool 都会闪一个黑色控制台窗口。
  3. -b（binary）模式输出到 stdout 的是原始字节，
     绝不能用 text=True 解码，否则 JPEG 会被 UTF-8 解码破坏。
  4. stderr 里的 `Error:` 一律按失败处理，**不能只看返回码**
     （exiftool 可能在返回 0 的同时什么都没写）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import is_dng
from ..errors import ExiftoolUnavailableError
from ..logging_setup import get_logger
from ..paths import exiftool_argv_prefix, find_exiftool

# DNG 的 PreviewImage 允许多大。
#
# 这是**第一道快速拒绝**，不是“这是不是可用预览”的判据（两者必须分开看）：
#   · 体积：DNG 的 PreviewImage 有两种完全不同的东西 ——
#     正常 JPEG 预览（实测 DJI 的 DNG 是 459 KB 的 JFIF）与未压缩大位图
#     （可达数百 MB，比原片还大）。`-b` 一拉就把内存打爆，所以先用体积拦。
#     8 MB 远大于任何正常嵌入式预览（通常 0.2–4 MB）。
#   · 够不够用：由**实测宽高**判定（见 preview.PreviewResult.is_low_confidence：
#     长边 < THUMB_LONG_EDGE 就算低可信，并写进提示词让模型克制细节类判断）——
#     体积小也可能是 320×213 的小图，只靠体积会把“小”当“好”。
DNG_PREVIEW_MAX_BYTES = 8 * 1024 * 1024

log = get_logger("exiftool")


def _reject_stderr_errors(stderr: str | bytes, returncode: int) -> None:
    """stderr 里出现 Error 就当成失败，即使返回码是 0。

    为什么必须有这一层（实测，2026-09）：
      `-charset filename=utf8` 写中文路径时，exiftool 给出
      `Error opening directory ...` 而**返回码仍是 0**、文件一个字节都没改。
      旧代码只看返回码，于是"没写进去"被当成"写成功"（最危险的一类静默错误）。

    与 Warning 的区别（必须分清，否则会误报）：
      exiftool 把**警告**也打进 stderr（用户的真实 XMP 自带 13 类
      `Non-standard XMP property` 警告），那是正常的，不能当失败。
      只有以 `Error` 开头的行才算失败——`Error: [minor] …` 也是失败：
      实测 `[minor] Maker notes could not be parsed` 时 exiftool 直接放弃写入
      （文件 mtime 不变、也没有生成 *_original 备份）。
    """
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", "replace")
    else:
        text = stderr
    errors = [
        line.strip()
        for line in text.splitlines()
        if line.lstrip().startswith("Error")
    ]
    if errors:
        raise RuntimeError(
            f"exiftool 报错（返回码 {returncode}）：{errors[0][:300]}"
        )
    for line in text.splitlines():
        if line.lstrip().startswith("Warning"):
            log.debug("exiftool 警告：%s", line.strip()[:200])
            break


# exiftool 单次调用的超时：120 秒。
# 依据：正常提取预览 <1 秒；但 DNG 或超大 RAF 在机械硬盘上可能读到数十秒。
#       120 秒足以覆盖最慢情况，同时避免僵尸进程永久挂住。
EXIFTOOL_TIMEOUT_S = 120

# 版本检测的超时：15 秒。
# 依据：-ver 只是打印一行版本号，正常 <0.5 秒。15 秒仍未返回，
#       基本可判定该"exiftool"是个损坏或非法的可执行文件。
VERSION_TIMEOUT_S = 15

# 命名参数：-q -q 表示"静默两次"，抑制版权/加载提示。
#
# ‼ 只加 `-charset exiftool=utf8`（标签文本），**绝不能加 `-charset filename=utf8`**。
# 这条结论是 2026-09 用真实文件实测出来的（旧注释写反了，害得所有中文路径全级降级）：
#
#   Python 的 subprocess 在 Windows 上用宽字符 API 传参，exiftool 拿到的是
#   系统代码页（GBK）字节；再声明 `-charset filename=utf8` 等于让 exiftool 把已经
#   正确的 GBK 字节按 UTF-8 再解一遍 → 文件名乱码。
#
#   实测（`E:\%同步\R5m2\_G4A0172.CR3`，同一台机器、同一个 exiftool 13.59）：
#     无 charset          → rc=0，正确读到 Model=Canon EOS R5m2
#     只 exiftool=utf8    → rc=0，正确
#     现有写法（两个都带）   → rc=1，stderr「File not found - E:/%ͬ��/R5m2/...」
#   写入侧同样（写中文路径上的 JPEG 副本）：
#     无 charset          → 文件真被改动
#     两个都带             → stderr「Error opening directory ...」，**文件没动**
#
#   危害：失败被当成"标签缺失"，于是每一张中文路径的 RAW 都静默回退到 rawpy 全解码
#   （慢 1–5 秒/张），日志还把原因归错。test_data 是纯 ASCII 路径，所以一直没暴露。
_BASE_ARGS: tuple[str, ...] = ("-q", "-q", "-charset", "exiftool=utf8")


@dataclass
class ExiftoolStatus:
    """exiftool 的可用性状态，供界面与 CLI 展示。"""

    available: bool
    path: Path | None = None
    version: str | None = None
    error: str | None = None
    tried_paths: list[Path] = field(default_factory=list)

    def install_guide(self) -> str:
        """生成 Windows 下的安装指引（硬约束 #10 要求"清晰的安装指引"）。"""
        lines = [
            "未检测到 exiftool。程序已降级为 rawpy 全解码提取预览（速度较慢）。",
            "",
            "影响范围：",
            "  - 预览提取：降级为 rawpy 全解码，可正常完成，但每张约慢 1–5 秒；",
            "  - DNG 内嵌 XMP 读写：无法完成（DNG 的 XMP 在文件内部，必须用 exiftool 改写）。",
            "",
            "Windows 安装方法（任选其一）：",
            "  方法 1（推荐，免安装）：从 https://exiftool.org 下载 Windows 可执行版压缩包，",
            "        解压后把 exiftool.exe 放在本程序 exe 的同级目录（或 _internal\\ 目录）即可，",
            "        程序启动时会自动优先使用它。",
            "  方法 2（加入 PATH）：把解压出的 exiftool.exe 所在目录加入系统 PATH：",
            "        设置 → 系统 → 系统信息 → 高级系统设置 → 环境变量 →",
            "        在「用户变量」里选中 Path → 编辑 → 新建 → 粘贴目录路径 → 确定。",
            "        重开程序后生效。验证方法：新开 PowerShell 执行 exiftool -ver。",
            "  方法 3（包管理器）：用 winget 或 chocolatey 安装，",
            "        winget install exiftool   或   choco install exiftool",
        ]
        if self.tried_paths:
            lines.append("")
            lines.append("已尝试的查找位置：")
            for p in self.tried_paths:
                lines.append(f"  - {p}")
        if self.error:
            lines.append("")
            lines.append(f"最后一次失败原因：{self.error}")
        return "\n".join(lines)


class ExiftoolRunner:
    """exiftool 的命令行门面。

    设计为可注入（构造函数接受显式 exe 路径），便于单测时替换为假对象。
    """

    def __init__(self, exe: Path | None = None) -> None:
        self.status = ExiftoolStatus(available=False, path=exe)
        # 真正的调用前缀：官方 Windows 包（启动器 exe + exiftool_files\）在打包目录里
        # 靠启动器跑不起来（实测 Can't locate strict.pm），要走同目录的 perl + exiftool.pl。
        # 详见 acb/paths.py::exiftool_argv_prefix 的说明。
        self._argv: list[str] = [str(exe)] if exe is not None else []
        self._probe()

    # --- 探测 ---------------------------------------------------------------

    def _probe(self) -> None:
        """启动时检测 exiftool 可用性（硬约束 #10）。"""
        candidates: list[Path] = []
        if self.status.path is not None:
            candidates.append(self.status.path)
        found = find_exiftool()
        if found is not None and found not in candidates:
            candidates.append(found)

        self.status.tried_paths = list(candidates)
        if not candidates:
            self.status.error = "PATH 中未找到 exiftool，且程序目录下没有 exiftool.exe"
            return

        for exe in candidates:
            argv = exiftool_argv_prefix(exe)
            try:
                proc = subprocess.run(
                    [*argv, "-ver"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=VERSION_TIMEOUT_S,
                    creationflags=_no_window_flags(),
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    self.status.available = True
                    self.status.path = exe
                    self._argv = argv
                    self.status.version = proc.stdout.strip()
                    self.status.error = None
                    log.info("检测到 exiftool %s（%s）", self.status.version,
                             argv[0] if len(argv) > 1 else exe)
                    return
                self.status.error = f"{exe} 返回码 {proc.returncode}，输出：{proc.stdout.strip()[:200]}"
            except FileNotFoundError as exc:
                self.status.error = f"{exe} 不存在或无法执行：{exc}"
            except subprocess.TimeoutExpired:
                self.status.error = f"{exe} 执行 -ver 超时（{VERSION_TIMEOUT_S}s），可能不是有效的 exiftool"
            except OSError as exc:
                self.status.error = f"{exe} 执行失败：{exc}"

        log.warning("exiftool 不可用：%s", self.status.error)

    @property
    def available(self) -> bool:
        return self.status.available

    # --- 底层执行 -----------------------------------------------------------

    def _run_binary(self, args: list[str]) -> bytes:
        """以二进制模式执行，返回 stdout 原始字节。

        绝不能使用 text=True：-b 输出的是 JPEG/XMP 原始字节流。
        """
        if not self.status.available or self.status.path is None:
            raise ExiftoolUnavailableError(self.status.install_guide())
        cmd = [*self._argv, *_BASE_ARGS, *args]
        log.debug("执行：%s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=EXIFTOOL_TIMEOUT_S,
            creationflags=_no_window_flags(),
        )
        if proc.returncode not in (0, 1):
            # exiftool 的返回码 1 表示"有警告但成功了"（例如某些标签缺失），
            # 这在批量任务里很常见，不应视为失败。
            raise RuntimeError(
                f"exiftool 返回码 {proc.returncode}：{proc.stderr.decode('utf-8', 'replace')[:400]}"
            )
        _reject_stderr_errors(proc.stderr, proc.returncode)
        return proc.stdout

    def _run_text(self, args: list[str]) -> str:
        """以文本模式执行（标签查询、校验等）。"""
        if not self.status.available or self.status.path is None:
            raise ExiftoolUnavailableError(self.status.install_guide())
        cmd = [*self._argv, *_BASE_ARGS, *args]
        log.debug("执行：%s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=EXIFTOOL_TIMEOUT_S,
            creationflags=_no_window_flags(),
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"exiftool 返回码 {proc.returncode}：{proc.stderr[:400]}")
        _reject_stderr_errors(proc.stderr, proc.returncode)
        return proc.stdout

    # --- 业务方法 -----------------------------------------------------------

    def read_tags(self, path: Path, names: list[str]) -> dict[str, str]:
        """一次调用读出若干文本标签（解析 `-s -s` 的 `Name: Value` 行）。

        为什么一次读多个：每多一次进程启动约 60–90 ms，而本函数要**每张图**调用
        （用于探测机内配置），批量时开销会累加。

        解析用 `str.splitlines()` 而不是 `split("\\n")`：
        实测 exiftool 在 Windows 上按 **CRLF** 分行，而它在某些标签组合下会只给 `\\r`
        —— 只按 `\\n` 切会把两行粘成 `PictureStyle: PortraitModel: Canon EOS R5m2`。
        这个粘连我在探测阶段真踩到过，所以这里写清楚。
        """
        if not names:
            return {}
        args = ["-s", "-s", *[f"-{name}" for name in names], str(path)]
        out = self._run_text(args)
        wanted = set(names)
        found: dict[str, str] = {}
        for line in out.splitlines():
            head, sep, value = line.partition(":")
            if not sep:
                continue
            key = head.strip()
            if key in wanted and key not in found:
                found[key] = value.strip()
        return found

    def _binary_tag_size(self, path: Path, tag: str) -> int | None:
        """先问"这个二进制标签有多大"，再决定要不要 -b 拿出来。

        存在的理由：DNG 的 PreviewImage 可能是**未压缩大位图**（实测可达数百 MB），
        直接 `-b` 会把它整个拉进内存。先读一下大小（exiftool 在 -s 模式下会打印
        `(Binary data N bytes, use -b option to extract)`），就能在内存爆炸之前收手。
        读不到大小返回 None（调用方按"不确定"处理）。
        """
        try:
            out = self._run_text(["-s", "-S", f"-{tag}", str(path)])
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            log.debug("%s 读取 %s 大小失败：%s", path.name, tag, exc)
            return None
        match = re.search(r"Binary data (\d+) bytes", out)
        if not match:
            return None
        return int(match.group(1))

    def extract_binary_tag(
        self, path: Path, *tags: str, max_bytes: int | None = None
    ) -> tuple[bytes, str] | None:
        """按给定标签顺序尝试提取二进制内容。

        返回 (内容字节, 命中的标签名)；全部未命中返回 None。
        对应问题 (b) 的提取顺序：先 -PreviewImage，再 -ThumbnailImage。

        max_bytes 不为 None 时：先量大小，超过上限的标签直接跳过（不拿）。
        """
        for tag in tags:
            if max_bytes is not None:
                size = self._binary_tag_size(path, tag)
                if size is not None and size > max_bytes:
                    log.debug(
                        "%s 的 %s 有 %.1f MB，超过 %.1f MB 上限（大概率是未压缩大位图），跳过",
                        path.name,
                        tag,
                        size / 1e6,
                        max_bytes / 1e6,
                    )
                    continue
            try:
                data = self._run_binary(["-b", f"-{tag}", str(path)])
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                log.debug("%s 提取 %s 失败：%s", path.name, tag, exc)
                continue
            # exiftool 在标签不存在时不报错，只是输出为空。
            if data and len(data) > 0:
                log.debug("%s 命中 %s：%d 字节", path.name, tag, len(data))
                return data, tag
        return None

    def extract_preview_image(self, path: Path) -> tuple[bytes, str] | None:
        """提取嵌入式预览图。

        DNG 的顺序：先 -JpgFromRaw，再用**带体积上限**的 -PreviewImage。
        为什么以前完全不用 PreviewImage：某些 DNG 的它是一张未压缩的大位图，
        -b 导出后体积可达原片量级，会把内存打爆。
        但实测（DJI 的 DNG）很多 DNG 只把 JPEG 放在 PreviewImage 里
        （459 KB，标准 JFIF），一律不用就会白白回退到 rawpy（每张慢 1–2 秒）。
        折中方案：先问大小，超过 DNG_PREVIEW_MAX_BYTES 就不要 ——
        既拿到了快路径，也不会把几百 MB 拉进内存。
        """
        if is_dng(path.name):
            result = self.extract_binary_tag(path, "JpgFromRaw", "OtherImage", "ThumbnailImage")
            if result is None:
                result = self.extract_binary_tag(
                    path, "PreviewImage", max_bytes=DNG_PREVIEW_MAX_BYTES
                )
            return result
        return self.extract_binary_tag(path, "PreviewImage", "JpgFromRaw", "ThumbnailImage")

    def read_xmp_packet(self, path: Path) -> bytes | None:
        """读出文件内的完整 XMP 包（DNG 内嵌 XMP 的读取入口）。

        为什么要读"整包"而不是逐字段读：
            DNG 内部 XMP 含蒙版、曲线、历史记录等复杂嵌套结构，
            逐字段读取会丢失结构；只有整包读出 → 用 XML 修改 → 整包写回，
            才能保证用户的既有修图成果不被破坏。
        """
        try:
            data = self._run_binary(["-b", "-xmp", str(path)])
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("读取内嵌 XMP 失败 %s：%s", path.name, exc)
            return None
        return data if data else None

    def write_xmp_packet(self, target: Path, xmp_bytes: bytes) -> tuple[bool, str]:
        """把 XMP 整包写回目标文件（DNG 用）。

        安全设计（对应硬约束 #1 的"DNG 操作会直接改写用户原片"）：
            不传 -overwrite_original，因此 exiftool 会自动把原文件备份为
            <原名>_original。<这是 DNG 改写的天然回滚点，
            比程序自己复制一份备份更省磁盘且行为可预期。

        返回 (是否成功, 说明文本)。
        """
        if not self.status.available or self.status.path is None:
            return False, "exiftool 不可用，无法改写 DNG 内嵌 XMP"

        from ..paths import data_root

        # 临时 XMP 文件放在运行时数据目录下（有写权限），不用系统 temp，
        # 避免某些企业环境清理 temp 导致 exiftool 读不到源文件。
        tmp_dir = data_root() / "state" / "_tmp_xmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_xmp = tmp_dir / f"packet_{target.stem}.xmp"
        try:
            tmp_xmp.write_bytes(xmp_bytes)
            # -tagsfromfile <源> -xmp 表示：把源文件里的 XMP 整体复制到目标文件。
            # 这会把目标内部的 XMP 包**整个替换**，因此必须传入完整包。
            out = self._run_text(
                [
                    # `-m`（忽略**次要**错误）在这里**不是保险，是必需**：
                    # 实测（2026-09-23，用户的 DJI DNG）不加 -m 时 exiftool 会打印
                    #   `Error: [minor] Maker notes could not be parsed`
                    # 然后**一个字节都不写**（文件 sha 不变、没有 *_original），
                    # 于是所有 DNG 都写不进去。
                    # 加 -m 后实测：文件被正常重写、XMP 包完整落盘、*_original 备份生成，
                    # 而且用 `-validate -warning -a` 对比，标签总数不变（202→202），
                    # 只有 StripOffsets / PreviewImageStart 这类偏移与文件名/时间跟着变，
                    # 像素数据长度（StripByteCounts/PreviewImageLength）完全一致。
                    # 注意：-m 只降级 minor；真正的错误 exiftool 仍会拒绝执行，
                    # 而且调用方还会把写回结果读回来核对（见 xmp/dng.py）。
                    "-m",
                    "-tagsfromfile",
                    str(tmp_xmp),
                    "-xmp",
                    str(target),
                ]
            )
            backup = target.with_name(target.name + "_original")
            note = f"已写回内嵌 XMP，原片备份：{backup.name}" if backup.exists() else "已写回内嵌 XMP"
            log.info("%s：%s", target.name, note)
            log.debug("exiftool 输出：%s", out.strip()[:400])
            return True, note
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            return False, f"写回内嵌 XMP 失败：{exc}"
        finally:
            # 临时文件含用户元数据，用完即删，避免在 state 目录堆积。
            try:
                tmp_xmp.unlink(missing_ok=True)
            except OSError:
                pass

    def validate_file(self, path: Path) -> list[str]:
        """用 exiftool -validate 校验文件的元数据一致性。

        用于自验（见 tools/verify_xmp.py）：写完 XMP 后读回校验，
        确保没有写出 ACR 无法解析的畸形文件。
        返回问题行列表；空列表表示通过。
        """
        try:
            out = self._run_text(["-validate", "-warning", "-a", str(path)])
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            return [f"校验执行失败：{exc}"]
        problems: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # 忽略"文件已校验"这类成功提示（含 Validate 且带 OK 的行）
            if "Validate" in line and ("OK" in line or "验证" in line):
                continue
            if line.startswith("[") or "Warning" in line or "Error" in line:
                problems.append(line)
        return problems


def _no_window_flags() -> int:
    """Windows 上隐藏子进程控制台窗口。

    非 Windows 返回 0（虽然本项目不支持非 Windows，但保持函数可在任意平台
    被导入而不报错，便于离线分析 tools 脚本复用本模块）。
    """
    import sys

    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0
