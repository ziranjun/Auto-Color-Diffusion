# -*- coding: utf-8 -*-
"""镜头校正的「逐图基线」（实施步骤 4）。

【为什么镜头校正不能交给 AI，也不能由我们"统一设一个值"】
1. ACR 的镜头校正**依赖它自己匹配到的配置文件**。实测样本里两种情形并存：
     LensProfileName="Camera Settings" + LensProfileIsEmbedded="True"   （用机内嵌入的）
     LensProfileName="Adobe (Canon EF-S 18-135mm f/3.5-5.6 IS USM)"    （用 Adobe 的 .lcp）
   外加一个与之配套的 `LensProfileDigest` 摘要。我们**绝不能**去写
   Name/Filename/Digest/IsEmbedded —— 写错会让 ACR 认为"配置文件与摘要不匹配"，
   整份设置都可能被判为不一致。所以那一层完全交给 ACR。
2. 剩下的开关（是否启用配置文件校正、是否自动消除色差、畸变/晕影缩放、去边）
   **每张照片应当保持它自己的状态**：用户可能对某张关掉了配置文件校正，
   而批量工具若统一写成"开"，就等于把他的取舍覆盖掉。
3. 所以这一层的做法是：**逐图读这张照片自己的基线**（优先它自己的侧车，
   其次相机默认基线 crd:），只补缺失的开关，已有值一律不动。

实测（用户 8 份样本，两台机器）这组开关高度一致，可作为默认基线的交叉验证：
    LensProfileEnable=1、LensProfileSetup=LensDefaults、AutoLateralCA=1、
    LensProfileDistortionScale=100、LensProfileVignettingScale=100、
    LensManualDistortionAmount=0、DefringePurpleAmount=0、DefringeGreenAmount=0
其中两台 R5m2 的侧车里还带 crd:LensProfileEnable=1（相机出厂默认基线，
是"用户没改过"的最强证据，见 style_profile 的三层差分）。#
# 【2026-09-23 用户裁决】镜头晕影（VignetteAmount）也在这一组里：
# "我改是因为有我的判断，AI 就让他动'效果'菜单里晕影就可以了" ——
# 所以它按逐图基线继承；AI 只能动 PostCropVignetteAmount（效果 · 裁剪后晕影）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("raw.lens")

# 由程序按"逐图基线"处理的镜头字段（一律 ai_writable=False，见 fields.py）：
# 配置文件本身的三个字段（Name/Filename/Digest）不在其中——它们**完全交给 ACR**。
LENS_SWITCH_FIELDS: tuple[str, ...] = (
    "LensProfileEnable",
    "LensProfileSetup",
    "AutoLateralCA",
    "LensProfileDistortionScale",
    "LensProfileVignettingScale",
    "LensManualDistortionAmount",
    "VignetteAmount",
    "DefringePurpleAmount",
    "DefringeGreenAmount",
    "DefringePurpleHueLo",
    "DefringePurpleHueHi",
    "DefringeGreenHueLo",
    "DefringeGreenHueHi",
)

# 明确**不写**的字段（交给 ACR 自己去匹配与摘要）：
LENS_NEVER_WRITE: tuple[str, ...] = (
    "LensProfileName",
    "LensProfileFilename",
    "LensProfileDigest",
    "LensProfileIsEmbedded",
)


# 一张照片既没有侧车、也没有内嵌设置时的兜底：
# **沿用 ACR 自己的默认**（而不是“什么都不写”）。
# 为什么不能什么都不写：侧车一旦存在，ACR 就不再回落到“机内默认”；
# 缺了这两个开关就相当于把镜头校正关掉 —— 那是把用户的片子改坏了。
# 依据（全部来自用户样本）：8/8 样本都是 LensProfileEnable=1、AutoLateralCA=1，
# 其中 2 份还带 crd:LensProfileEnable=1（相机出厂默认基线，是“用户没改过”的最强证据）。
ACR_LENS_DEFAULT: dict[str, str] = {
    "LensProfileEnable": "1",
    "AutoLateralCA": "1",
}


@dataclass(frozen=True)
class LensBaseline:
    """一张照片的镜头校正开关基线。"""

    values: dict[str, str] = field(default_factory=dict)
    source: str = ""
    crs_values: dict[str, str] = field(default_factory=dict)
    crd_values: dict[str, str] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return bool(self.values)

    def note(self) -> str:
        if not self.values:
            return "这张照片没有可用的镜头基线（侧车与 crd: 里都没有相关字段），镜头开关保持原样"
        keys = "、".join(f"{k}={v}" for k, v in sorted(self.values.items()))
        return f"镜头基线来自{self.source}：{keys}"


def read_lens_baseline(
    crs_raw: dict[str, str] | None = None,
    crd_raw: dict[str, str] | None = None,
    *,
    source: str = "",
) -> LensBaseline:
    """从已经读好的 crs/crd 原始属性里取出镜头开关基线。

    优先级：**这张照片自己的 crs 值 > 相机默认基线 crd 值**。
    crs 是用户当前的取舍（他可能关掉了某张的配置文件校正），crd 只是出厂默认。
    """
    crs = {k: v for k, v in (crs_raw or {}).items() if k in LENS_SWITCH_FIELDS and v not in ("", None)}
    crd = {k: v for k, v in (crd_raw or {}).items() if k in LENS_SWITCH_FIELDS and v not in ("", None)}
    if crs:
        return LensBaseline(values=crs, source=source or "该照片的侧车", crs_values=crs, crd_values=crd)
    if crd:
        return LensBaseline(
            values=crd,
            source=f"{source or '该照片'} 的相机默认基线（crd:，用户没改过这一段）",
            crs_values=crs,
            crd_values=crd,
        )
    return LensBaseline(source=source or "")


def baseline_for_raw(raw_path: Path, exiftool: Any = None) -> LensBaseline:
    """读一张 RAW 的镜头基线（侧车优先；DNG 走内嵌 XMP）。

    两者都没有时**沿用 ACR 默认开关**（而不是什么都不写）：侧车一旦存在，
    ACR 就不再回落到机内默认，缺开关等于把镜头校正关掉 —— 那是把片子改坏。
    依据：8/8 样本 LensProfileEnable=1、AutoLateralCA=1（其中 2 份还带 crd: 基线）。
    """
    from ..constants import is_dng
    from ..xmp import reader as R
    from ..xmp import writer as W

    doc = None
    try:
        if is_dng(raw_path.name) and exiftool is not None and getattr(exiftool, "available", False):
            packet = exiftool.read_xmp_packet(raw_path)
            if packet:
                doc = R.read_xmp_bytes(packet, source=f"{raw_path.name}!<embedded>")
        else:
            sidecar = W.sidecar_path_for(raw_path)
            if sidecar.is_file():
                doc = R.read_xmp_file(sidecar)
                return read_lens_baseline(doc.crs_raw(), doc.crd_raw(), source=sidecar.name)
    except Exception as exc:  # noqa: BLE001 —— 读基线失败不能影响主流程
        log.debug("%s 读取镜头基线失败：%s", raw_path.name, exc)
        return LensBaseline(values=dict(ACR_LENS_DEFAULT), source=f"读取失败（{exc}），按 ACR 默认")

    if doc is None:
        return LensBaseline(
            values=dict(ACR_LENS_DEFAULT),
            source="无侧车也无内嵌设置，按 ACR 默认（8/8 样本与 crd: 基线一致）",
        )
    baseline = read_lens_baseline(doc.crs_raw(), doc.crd_raw(), source=f"{raw_path.name}!<embedded>")
    if not baseline.resolved:
        return LensBaseline(values=dict(ACR_LENS_DEFAULT), source="内嵌设置里没有镜头开关，按 ACR 默认")
    return baseline


def missing_from(existing: dict[str, str], baseline: LensBaseline) -> dict[str, Any]:
    """基线里**目标文件还没有的**那部分（只补缺，绝不覆盖已有值）。

    为什么只补缺：侧车是"用户的真相"。如果一个文件里已经有
    `LensProfileEnable="0"`（用户主动关掉了），批量工具把它改成 1 就是覆盖用户取舍。
    """
    result: dict[str, Any] = {}
    for name, text in baseline.values.items():
        if name in existing:
            continue
        spec_value: Any = text
        # 数值型字段转成数字，交给 fields.format_value 统一格式化（含 + 号规则）。
        from ..xmp import fields as F

        spec = F.get_field(name)
        if spec is not None and spec.is_numeric:
            try:
                spec_value = float(text)
            except (TypeError, ValueError):
                continue
        result[name] = spec_value
    return result
