# -*- coding: utf-8 -*-
"""从 RAW 自身读出「相机机内配置」，映射成 ACR 的 CameraProfile 名。

【为什么必须有这个模块 —— 用户 2026-09 的真实反馈】
用户原话：「只用『Adobe标准』的色彩配置，明明应该以已有的『相机配置』为准，
却硬生生要改它」。

实测根因（用用户自己的文件复现，不是推测）：
    · 那 5 张被处理的 RAW **没有侧车 XMP**（产出的侧车只有 460–576 字节，里面没有
      crs:CameraProfile）；
    · 同一个目录里用户自己修过的 10 个 XMP **全部**都是
      crs:CameraProfile="Camera Portrait"（外加蒙版/颜色分级/HSL）；
    · 而侧车 XMP 一旦存在，ACR 就按它渲染、不再回落到「相机默认配置」。
      → 我们写出去的最小骨架没说用哪个配置，ACR 只好用 Adobe 标准。
      → 用户的相机配置就这样被"硬生生改掉"了。

数据来源（**不猜**）：相机把机内风格写在 RAW 的元数据里，exiftool 读得到。
实测在用户自己的 17 个文件上 1:1 成立（这是本表的定标依据）：

    PictureStyle=Portrait   ↔ crs:CameraProfile="Camera Portrait"   （10/10 例外为零）
    PictureStyle=Landscape  ↔ crs:CameraProfile="Camera Landscape"  （2/2）

保守规则（沿用 `detect_provider_from_key` 的那条教训：宁可认不出，也不猜错）：
    · 只写**表里明确有**的值；
    · 表里没有（例如佳能自定风格的 "User Def. 1"、富士的 FilmMode 命名与 ACR 不一致）
      → 返回 None，并给出一句"为什么没写"的理由，让用户自己去 ACR 里确认。
      写错一个不存在的 profile 名，ACR 照样回落 Adobe 标准，用户却会以为"程序设过了"，
      那比不写更糟。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger
from .exiftool import ExiftoolRunner

log = get_logger("camera_profile")

# 机内风格标签的读取顺序（先读到的非空值优先）。
# 名字来自 exiftool 的公开标签表：佳能 PictureStyle、尼康 PictureControlName、
# 索尼 CreativeStyle、富士 FilmMode、松下 PhotoStyle、奥林巴斯 PictureMode。
STYLE_TAG_CANDIDATES: tuple[str, ...] = (
    "PictureStyle",
    "PictureControlName",
    "CreativeStyle",
    "FilmMode",
    "PhotoStyle",
    "PictureMode",
)

# 佳能：ACR 的「Camera Matching」配置名与机内 PictureStyle 同名。
# 定标依据：用户本机 12 个文件的 crs:CameraProfile 与 PictureStyle 逐一对应。
_CANON_PICTURE_STYLE: dict[str, str] = {
    "Standard": "Camera Standard",
    "Portrait": "Camera Portrait",
    "Landscape": "Camera Landscape",
    "Neutral": "Camera Neutral",
    "Faithful": "Camera Faithful",
    "Monochrome": "Camera Monochrome",
    "Auto": "Camera Auto",
    "Fine Detail": "Camera Fine Detail",
}

# 尼康：PictureControl 与 ACR 的 Camera Matching 配置名一一对应。
# （未在本机实测，采用 ACR 公开的配置名；表里没有的一律不写。）
_NIKON_PICTURE_CONTROL: dict[str, str] = {
    "Standard": "Camera Standard",
    "Neutral": "Camera Neutral",
    "Vivid": "Camera Vivid",
    "Monochrome": "Camera Monochrome",
    "Portrait": "Camera Portrait",
    "Landscape": "Camera Landscape",
    "Flat": "Camera Flat",
    "Auto": "Camera Auto",
}

# 索尼：只列与 ACR 配置名明确同名的几项（CreativeStyle 里那些
# 「Sunset / Autumn Leaves / Night View」之类各家叫法不一，宁可不写）。
_SONY_CREATIVE_STYLE: dict[str, str] = {
    "Standard": "Camera Standard",
    "Vivid": "Camera Vivid",
    "Neutral": "Camera Neutral",
    "Portrait": "Camera Portrait",
    "Landscape": "Camera Landscape",
}

# 标签 → 映射表。**没列进来的标签一律不写**（不猜）。
_MAPPINGS: dict[str, dict[str, str]] = {
    "PictureStyle": _CANON_PICTURE_STYLE,
    "PictureControlName": _NIKON_PICTURE_CONTROL,
    "CreativeStyle": _SONY_CREATIVE_STYLE,
}


@dataclass(frozen=True)
class CameraProfileGuess:
    """一次机内配置探测的结果。"""

    value: str | None = None          # 要写进 crs:CameraProfile 的值
    source_tag: str | None = None     # 来源标签，例如 "PictureStyle"
    source_value: str | None = None   # 来源取值，例如 "Portrait"
    reason: str = ""                  # 人话解释（写日志/告警用）

    @property
    def resolved(self) -> bool:
        return bool(self.value)


def guess_camera_profile(raw_path: Path, exiftool: ExiftoolRunner) -> CameraProfileGuess:
    """读 RAW 的机内风格并映射成 ACR 配置名。

    任何异常都不向上抛：这只是"锦上添花"的一步，
    读不到就返回一个 resolved=False 的结果，由调用方决定怎么提示。
    """
    if not exiftool.available:
        return CameraProfileGuess(reason="exiftool 不可用，无法读取机内配置")

    try:
        tags = exiftool.read_tags(raw_path, list(STYLE_TAG_CANDIDATES))
    except Exception as exc:  # noqa: BLE001 —— 探测失败不能影响主流程
        log.debug("%s 读取机内风格失败：%s", raw_path.name, exc)
        return CameraProfileGuess(reason=f"读取机内配置失败（{exc}）")

    for tag in STYLE_TAG_CANDIDATES:
        raw_value = (tags.get(tag) or "").strip()
        if not raw_value:
            continue
        table = _MAPPINGS.get(tag)
        if table is None:
            return CameraProfileGuess(
                source_tag=tag,
                source_value=raw_value,
                reason=(
                    f"机内设置是 {tag}={raw_value}，但该厂商的配置名映射未经核实，"
                    "为免写错已保持 ACR 默认（请在 ACR 里手动确认一次）"
                ),
            )
        mapped = table.get(raw_value)
        if mapped is None:
            return CameraProfileGuess(
                source_tag=tag,
                source_value=raw_value,
                reason=(
                    f"机内设置 {tag}={raw_value} 在映射表里没有对应项"
                    "（例如自定风格），已保持 ACR 默认"
                ),
            )
        return CameraProfileGuess(
            value=mapped,
            source_tag=tag,
            source_value=raw_value,
            reason=f"按机内设置 {tag}={raw_value} 写入 {mapped}",
        )

    return CameraProfileGuess(reason="RAW 里没有可识别的机内风格标签，已保持 ACR 默认")
