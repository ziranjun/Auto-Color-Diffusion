# -*- coding: utf-8 -*-
"""照片的显示方向（EXIF Orientation）：读取、校验、显示帧 ↔ 存储帧换算。

【为什么单独一个模块】用户 2026-09-23 的明确要求：
    「你要注意这个问题，不要到时候在批量的时候出现这种问题（图像横竖显示方向）」

背景（本会话实测认定）：
  · ACR 把蒙版的几何坐标写在**存储帧**里 —— 也就是传感器原始方向、
    没有按 EXIF Orientation 旋转过的那一版；
  · 人眼、AI、我们的界面想的都是**显示帧**（"天空在上方"）；
  · 同一批照片里方向可能**各不相同**：实测 8 张样本里
    5 张是 Rotate 270 CW、1 张 Rotate 90 CW、1 张 Horizontal (normal)、1 张无方向。
    所以**必须逐张读、逐张换算**，绝不能写死一个方向。

2026-09-23 的 ACR 实测验证：在 IMG_2509（Rotate 90 CW）上，
把显示帧坐标 (0.5, 0.28)【顶部天空】换算成存储帧 (0.28, 0.5) 后写进侧车，
ACR 里蒙版正好落在天空上 —— 换算方向由此确认。

支持的取值（EXIF Orientation 的标准写法）：
    Horizontal (normal) / Rotate 90 CW / Rotate 180 / Rotate 270 CW
镜像类（Mirror …）**明确拒绝**：宁可报错，也不猜一个方向把蒙版画到错的地方。
"""

from __future__ import annotations
from ..i18n import T, TF

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..logging_setup import get_logger

log = get_logger("raw.orientation")

Point = tuple[float, float]


class OrientationError(RuntimeError):
    """照片方向无法安全处理时抛出（镜像、未知取值）。"""


# 显示帧 ← 存储帧 的映射（归一化坐标，原点左上、y 向下）
_STORED_TO_DISPLAY: dict[str, Callable[[float, float], Point]] = {
    "Horizontal (normal)": lambda x, y: (x, y),
    "Rotate 90 CW": lambda x, y: (1.0 - y, x),
    "Rotate 180": lambda x, y: (1.0 - x, 1.0 - y),
    "Rotate 270 CW": lambda x, y: (y, 1.0 - x),
}

# 存储帧 ← 显示帧（上面的逆映射，单独写出来比运行时求逆直观）
_DISPLAY_TO_STORED: dict[str, Callable[[float, float], Point]] = {
    "Horizontal (normal)": lambda u, v: (u, v),
    "Rotate 90 CW": lambda u, v: (v, 1.0 - u),
    "Rotate 180": lambda u, v: (1.0 - u, 1.0 - v),
    "Rotate 270 CW": lambda u, v: (1.0 - v, u),
}

# 便于日志/界面显示：这个方向看过去是横还是竖
_IS_PORTRAIT = {
    "Horizontal (normal)": False,
    "Rotate 90 CW": True,
    "Rotate 180": False,
    "Rotate 270 CW": True,
    # exiftool 也常见这种写法，等价于 90 CW
    "Rotate 90 CW (Portrait)": True,
}

_ALIASES = {
    "rotate 90 cw": "Rotate 90 CW",
    "rotate 90 cw (portrait)": "Rotate 90 CW",
    "rotate 270 cw": "Rotate 270 CW",
    "rotate 180": "Rotate 180",
    "horizontal (normal)": "Horizontal (normal)",
    "normal": "Horizontal (normal)",
    "": "Horizontal (normal)",
    "undefined": "Horizontal (normal)",
    # exiftool 把**无效取值**（不在 EXIF 合法的 1–8 内）写成 `Unknown (0)` 这种形式。
    # 【2026-09-26 实测】小米 REDMI Turbo 4 的 HEIC 写的正是 `Orientation = 0`。
    # 旧行为：`normalize()` 报错 → 带蒙版的写出**整张失败**
    #（错误：无法识别的照片方向：'Unknown (0)'）。
    # 现在：0 不描述任何旋转/镜像，任何软件都按“未旋转”处理
    #（PIL 的 `exif_transpose` 同样忽略 0）—— 把它当 Horizontal (normal) 与事实一致，
    # 而且**必须**如此：我们的预览与送给 ACR 的中转 TIFF 都由同一个解码产出，
    # 两边保持“未旋转”反而是自洽的；镜像取值仍然后面单独拒掉。
    "unknown (0)": "Horizontal (normal)",
}


def normalize(orientation: str | None) -> str:
    """把 exiftool 读到的取值标准化；不支持的一律抛 OrientationError（不猜）。

    唯一“宽容”的一类：exiftool 对**无效取值**打印的 `Unknown (N)`（如小米 HEIC 的 0）。
    它不描述任何旋转/镜像，按“未旋转”处理与所有查看器（含 PIL）的行为一致；
    相比之下，为了一个无效标签把整张照片判成失败才是真损失（真实发生过）。
    """
    key = (orientation or "").strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    if key.startswith("unknown"):
        return "Horizontal (normal)"
    if key.startswith("mirror"):
        raise OrientationError(
            TF(
                "照片方向是镜像（{0}），本程序暂不处理："
                "镜像会同时翻转蒙版坐标，写错位置比什么都不写更糟。",
                repr(orientation),
            )
        )
    raise OrientationError(TF("无法识别的照片方向：{0}", repr(orientation)))


def rotation_degrees(orientation: str | None) -> int:
    """显示时需要顺时针旋转多少度（0 / 90 / 180 / 270）。"""
    return {"Horizontal (normal)": 0, "Rotate 90 CW": 90,
            "Rotate 180": 180, "Rotate 270 CW": 270}[normalize(orientation)]


def is_portrait(orientation: str | None) -> bool:
    """按显示帧看，画面是竖构图吗。"""
    return _IS_PORTRAIT.get(normalize(orientation), False)


def _round(point: Point) -> Point:
    return round(float(point[0]), 6), round(float(point[1]), 6)


def display_to_stored_point(point: Point, orientation: str | None) -> Point:
    """显示帧归一化坐标 → 存储帧归一化坐标。"""
    return _round(_DISPLAY_TO_STORED[normalize(orientation)](float(point[0]), float(point[1])))


def stored_to_display_point(point: Point, orientation: str | None) -> Point:
    """存储帧归一化坐标 → 显示帧归一化坐标（用于读懂用户既有蒙版）。"""
    return _round(_STORED_TO_DISPLAY[normalize(orientation)](float(point[0]), float(point[1])))


@dataclass(frozen=True)
class PhotoOrientation:
    """一张照片的方向信息（读一次，全流程复用）。"""

    label: str
    source: str = ""          # 取值来源说明（哪个标签、还是默认）
    raw_value: str | None = None

    @property
    def rotation(self) -> int:
        return rotation_degrees(self.label)

    @property
    def portrait(self) -> bool:
        return is_portrait(self.label)


def read_orientation(raw_path: Path, exiftool: Any = None) -> PhotoOrientation:
    """读 RAW 的 EXIF 方向（读不到就当 Horizontal (normal)，并记一条 debug）。

    任何异常都不向上抛：方向读取失败时按"未旋转"处理，同时把结果记在 source 里，
    调用方可以据此决定是否要提示用户。
    """
    if exiftool is None or not getattr(exiftool, "available", False):
        return PhotoOrientation("Horizontal (normal)", source=T("exiftool 不可用，按未旋转处理"))
    try:
        tags = exiftool.read_tags(raw_path, ["Orientation"])
    except Exception as exc:  # noqa: BLE001 —— 读方向失败不该中断批处理
        log.debug(T("%s 读取方向失败：%s"), raw_path.name, exc)
        return PhotoOrientation("Horizontal (normal)", source=f"读取失败（{exc}）")
    raw_value = (tags.get("Orientation") or "").strip()
    try:
        label = normalize(raw_value)
    except OrientationError as exc:
        log.warning("%s：%s", raw_path.name, exc)
        raise
    if raw_value.strip().lower().startswith("unknown"):
        # 取值不在 EXIF 合法的 1–8 内（实测小米 HEIC 写 0）。不静默：留一行可追溯的记录。
        log.debug(T("%s：方向的取值是 %s（不在 1–8 内），按未旋转处理"),
                  raw_path.name, raw_value)
        return PhotoOrientation("Horizontal (normal)",
                                source=TF("EXIF Orientation 无效取值（{0}），按未旋转处理",
                                          raw_value),
                                raw_value=raw_value)
    return PhotoOrientation(label, source="EXIF Orientation", raw_value=raw_value or None)
