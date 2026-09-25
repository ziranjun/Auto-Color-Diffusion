# -*- coding: utf-8 -*-
"""应用图标（圆角风格）的加载与任务栏身份设置。

图标从哪来
----------
`assets/icon/app-256.png`（圆角 PNG）+ `assets/app.ico`（多尺寸 ICO），
两者都由 `python tools/make_icon.py` 生成。**不手改二进制**：
要换图标就换 `assets/icon/source.png` 再跑一次那个脚本。

为什么单独一个模块
------------------
  · 图标有**三个消费方**：窗口、任务栏（Windows 的 AppUserModelID）、打包的 exe。
    三处各写一遍 `QIcon(...)` 迟早分叉（尤其打包后路径不同）；
  · 图标缺失绝不能拦住程序启动：源图还没生成、打包漏带文件时，
    应该"没有图标"而不是抛异常 —— 所以这里的每个函数都不抛。

任务栏为什么需要 AppUserModelID
------------------------------
Windows 用 AppUserModelID 决定"哪些窗口属于同一个应用"以及**用谁的图标**。
不设置时，从源码运行的窗口会挂在 python.exe 名下 —— 任务栏上显示的是 Python 的图标，
右键菜单里也是 Python。设成我们自己的 APP_ID 后，任务栏与固定到开始菜单都用我们的图标。
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import APP_ID
from ..logging_setup import get_logger
from ..paths import app_icon_png_path

log = get_logger("ui.icon")

_icon_cache: object | None = None       # QIcon 只建一次（Qt 侧不需要多份）
_cache_ready = False


def load_app_icon():
    """返回 QIcon；素材缺失或 Qt 不可用时返回 None（调用方直接跳过即可）。"""
    global _icon_cache, _cache_ready
    if _cache_ready:
        return _icon_cache
    _cache_ready = True
    _icon_cache = None

    path = app_icon_png_path()
    if not path.is_file():
        log.info("没有找到图标文件 %s（用 tools/make_icon.py 生成），本次不加窗口图标。", path)
        return None
    try:
        from PyQt6.QtGui import QIcon

        icon = QIcon(str(path))
        if icon.isNull():
            log.warning("图标文件存在但 Qt 打不开：%s", path)
            return None
    except Exception as exc:      # noqa: BLE001 - 图标问题不该影响启动
        log.warning("加载图标失败（忽略）：%s", exc)
        return None
    _icon_cache = icon
    return icon


def apply_window_icon(window) -> bool:
    """把图标装到窗口上。返回是否装上了（自检要用这个值）。"""
    icon = load_app_icon()
    if icon is None:
        return False
    window.setWindowIcon(icon)
    return True


def set_app_user_model_id(app_id: str = APP_ID) -> bool:
    """设置 Windows 的 AppUserModelID（任务栏图标/分组用）。非 Windows 或失败都返回 False。

    ⚠ 必须在**创建任何窗口之前**调用，否则对本进程的第一个窗口不生效。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
        return True
    except Exception as exc:      # noqa: BLE001 - 只是任务栏观感，失败不该影响运行
        log.debug("设置 AppUserModelID 失败（忽略）：%s", exc)
        return False


def icon_files_present() -> tuple[bool, bool]:
    """(圆角 PNG 在不在, 多尺寸 ICO 在不在) —— 自检用来区分"没生成"和"生成错了"。"""
    from ..paths import app_icon_ico_path

    png: Path = app_icon_png_path()
    ico: Path = app_icon_ico_path()
    return png.is_file(), ico.is_file()
