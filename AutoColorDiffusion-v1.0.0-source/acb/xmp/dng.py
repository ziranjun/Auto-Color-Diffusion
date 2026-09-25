# -*- coding: utf-8 -*-
"""DNG 内嵌 XMP 的读写（硬约束 #1 的 DNG 例外）。

硬约束原文：
    DNG：XMP 已嵌入文件内部，ACR 优先读取嵌入元数据，旁侧 .xmp 可能被忽略
    或产生冲突。因此对 .dng 一律采用「读出嵌入 XMP → 修改 → 写回文件内部」策略，
    不得生成 .dng.xmp 旁侧文件；
    若用户坚持要旁侧文件，须在界面弹出警告并记录 warn。

技术实现：必须借助 exiftool 做"整包往返"
----------------------------------------
DNG 是 TIFF 容器，XMP 以 TIFF tag 形式嵌在文件内部。Python 侧没有可靠的
纯 Python 方案能安全地就地替换这段可变长数据（会牵动 IFD 偏移），
因此必须用 exiftool：
    读：exiftool -b -xmp file.dng          → 输出完整 XMP 包字节
    改：用 writer.apply_params 在内存里改 XML
    写：exiftool -tagsfromfile new.xmp -xmp file.dng

为什么必须"整包"而不是逐字段写：
    exiftool 也支持 -XMP:Exposure2012=0.35 这样的逐字段写，但那会丢失
    蒙版、曲线、历史记录等复杂嵌套结构（exiftool 无法表达 rdf:Seq 嵌套）。
    只有整包往返才能保证用户的既有修图成果不被破坏。

安全设计
--------
写回时**不传** -overwrite_original，因此 exiftool 会自动把原文件备份为
<原名>_original。这是 DNG 改写的天然回滚点：用户一旦发现效果不对，
把 *_original 改回原名即可完全恢复。
README 中会强制提示"操作前请自行备份重要原片"。
"""

from __future__ import annotations

from pathlib import Path

from ..logging_setup import get_logger
from ..raw.exiftool import ExiftoolRunner
from . import fields as F
from . import reader as R
from . import writer as W
from .validator import CRS_PREFIX as _CRS_PREFIX

log = get_logger("xmp.dng")

# 蒙版在 `applied` 清单里的前缀（见 writer.render_xmp：
# `applied.extend(f"蒙版:{name}" for name in written)`）。
# 核对的特殊性见 write_embedded 里那段说明：蒙版是**子元素**，不是属性。
_MASK_PREFIX = "蒙版:"


def _xml_escape(value: str) -> str:
    """按 XML 属性值转义（核对写回的字节时用来对齐两种写法）。"""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


class DngWriteError(Exception):
    """DNG 内嵌 XMP 写入失败。"""


def read_embedded(raw_path: Path, exiftool: ExiftoolRunner) -> bytes | None:
    """读出 DNG 内部嵌入的 XMP 包；没有则返回 None。"""
    if not exiftool.available:
        return None
    return exiftool.read_xmp_packet(raw_path)


def write_embedded(
    raw_path: Path,
    params: dict,
    curves: dict[str, list] | None = None,
    *,
    exiftool: ExiftoolRunner,
    allow_sidecar_fallback: bool = False,
    camera_profile: str | None = None,
    profile_note: str = "",
    program_fields: dict | None = None,
    masks: list[dict] | None = None,
    orientation: str | None = None,
) -> W.XmpWriteResult:
    """按「读出 → 修改 → 写回文件内部」流程处理 DNG。

    参数：
        allow_sidecar_fallback —— exiftool 不可用时是否退化为写旁侧文件。
            默认 False：因为硬约束 #1 明确要求 DNG 不得生成旁侧文件，
            且旁侧文件在 DNG 上"可能被忽略或产生冲突"，
            静默退化会让用户以为设置生效了、实际没有——这是更糟的结果。
            只有当用户在界面上主动选择"仍要旁侧文件"时才置 True，
            此时会附带显式警告。
        camera_profile —— **仅在没有内嵌 XMP、需要新建一份时**才写进去，
            与侧车路径同一条规则（见 writer.write_sidecar）。
    """
    warnings: list[str] = []
    warnings.extend(W.validate_dng_policy(raw_path, force_sidecar_for_dng=allow_sidecar_fallback))

    # --- exiftool 不可用的处理 ----------------------------------------------
    if not exiftool.available:
        if allow_sidecar_fallback:
            # 用户已明确知情并选择旁侧文件路径：写 .dng.xmp 并显式告警。
            sidecar_target = W.embedded_sidecar_path_for(raw_path)
            data, applied, skipped, extra = W.render_xmp(
                params, curves, None, program_fields=program_fields,
                masks=masks, orientation=orientation,
            )
            sidecar_target.write_bytes(data)
            warnings.append(
                "exiftool 不可用，无法改写 DNG 内嵌 XMP，已按你的选择生成旁侧文件 "
                f"{sidecar_target.name}。注意 ACR 对 DNG 会优先读取内嵌元数据，"
                "该旁侧文件很可能不生效。"
            )
            log.warning("%s：exiftool 不可用，降级写出旁侧文件 %s", raw_path.name, sidecar_target.name)
            return W.XmpWriteResult(
                mode="sidecar",
                target=sidecar_target,
                warnings=warnings + extra,
                applied_fields=applied,
                skipped_fields=skipped,
            )
        raise DngWriteError(
            f"{raw_path.name}：这是 DNG 文件，其 XMP 嵌入在文件内部，"
            "必须使用 exiftool 才能安全改写，但当前未检测到 exiftool。\n"
            "请安装 exiftool（见程序启动时的安装指引）后重试；\n"
            "或在界面上勾选「DNG 也生成旁侧 .xmp 文件」以知情接受该文件可能不生效。"
        )

    # --- 读出嵌入 XMP -------------------------------------------------------
    embedded = read_embedded(raw_path, exiftool)
    base: R.XmpDocument | None = None
    if embedded:
        try:
            base = R.XmpDocument(embedded, source=f"{raw_path.name}!<embedded-xmp>")
            log.debug("%s：读出内嵌 XMP %d 字节，含 %d 段 rdf:Description",
                      raw_path.name, len(embedded), len(base.descriptions))
        except Exception as exc:
            # 内嵌 XMP 解析失败时**不能**覆盖它：可能含我们无法理解的扩展结构。
            raise DngWriteError(
                f"{raw_path.name} 的内嵌 XMP 无法解析（{exc}）。\n"
                "为避免破坏其中的蒙版/曲线等结构，本次已跳过该文件。"
                "如需强制重写，请先用 exiftool 导出内嵌 XMP 并人工确认。"
            ) from exc
    else:
        warnings.append(f"{raw_path.name} 没有内嵌 XMP，将新建一份写入文件内部。")

    # --- 修改 ---------------------------------------------------------------
    data, applied, skipped, extra = W.render_xmp(
        params, curves, base, camera_profile, program_fields=program_fields,
        masks=masks, orientation=orientation,
    )
    warnings.extend(extra)

    # --- 写回文件内部 -------------------------------------------------------
    ok, note = exiftool.write_xmp_packet(raw_path, data)
    if not ok:
        log.error("%s：DNG 内嵌 XMP 写回失败：%s", raw_path.name, note)
        raise DngWriteError(f"{raw_path.name}：{note}")

    # --- 写回之后必须**读回来核对** -----------------------------------------
    # exiftool 返回码 0 不等于"我们的字段进去了"：
    #   · 它可能只是重写了一遍文件（内容没变）；
    #   · 也可能因为容器里 XMP 槽位/偏移问题把包弄丢。
    # 旧代码写完就报成功，于是"参数没生效"要等用户在 ACR 里发现。
    # 这里改成：把刚写的包读回来，逐个确认本次**应用过的字段名**都在里面。
    # 只查字段名不查值：值可能被 exiftool 正规化（+0.30 / 0.3）。
    back = read_embedded(raw_path, exiftool)
    text = (back or b"").decode("utf-8", "replace")
    # 只查**属性型**字段：曲线（ToneCurvePV2012 及其三通道）是 `rdf:Seq` 元素，
    # 在 XML 里写成子元素而不是 `crs:xxx="..."`，用属性形式去查会**假失败**
    # （实测：写得好好的被自己的核对判成失败，反而把文件计入失败账本）。
    # 曲线的正确性由侧车往返用例与 `validate_file()` 负责。
    #
    # 【2026-09-24 修第二个同类假失败：蒙版】用户实测「完成 18 张，失败 2 张」，
    # 而失败原因写的是「内嵌 XMP 写后核对失败：3 个字段没进文件（蒙版:linear:…）」——
    # 实际上蒙版**已经写进去了**（下一轮“续跑”里程序自己报了“该文件已有蒙版组”）。
    # 根因：蒙版写成 <crs:MaskGroupBasedCorrections> 里的子元素，不存在
    # `crs:蒙版:linear:天空…=` 这种属性，而旧核对把 applied 里的每一项都当属性去查。
    # 现在蒙版改用 CorrectionName="… " 核对（名字里可能带 & " < >，两边都按转义比对）。
    missing: list[str] = []
    for name in applied:
        if name in F.CURVE_FIELDS:
            continue
        if name.startswith(_MASK_PREFIX):
            display = name.split(":", 2)[2] if name.count(":") >= 2 else name
            if f'CorrectionName="{display}"' not in text and f'CorrectionName="{_xml_escape(display)}"' not in text:
                missing.append(name)
            continue
        if f"{_CRS_PREFIX}{name}=" not in text:
            missing.append(name)
    if missing:
        log.error(
            "%s：内嵌 XMP 写后核对失败，缺失 %d 个字段（%s）",
            raw_path.name, len(missing), "、".join(missing[:6]),
        )
        raise DngWriteError(
            f"{raw_path.name} 的内嵌 XMP 写回后核对失败：{len(missing)} 个字段没进文件"
            f"（例如 {missing[0]}）。\n"
            "参数写进去了但没生效，比直接报错更难查，所以这里按失败处理。\n"
            f"原文件已由 exiftool 备份为 {raw_path.name}_original，可直接改回原名恢复。"
        )

    backup = raw_path.with_name(raw_path.name + "_original")
    log.info("DNG 内嵌 XMP 已更新：%s（应用 %d 个字段）；%s", raw_path.name, len(applied), note)

    return W.XmpWriteResult(
        mode="embedded",
        target=raw_path,
        warnings=warnings,
        applied_fields=applied,
        skipped_fields=skipped,
        backup=backup if backup.exists() else None,
        camera_profile=camera_profile if base is None else None,
        camera_profile_note=(profile_note if base is None else "已有内嵌 XMP，相机配置保持原值"),
        created_new=base is None,
    )
