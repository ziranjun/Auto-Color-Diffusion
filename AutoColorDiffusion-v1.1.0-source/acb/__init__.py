# -*- coding: utf-8 -*-
"""Auto Color Diffusion —— AI 批量调色辅助工具。

本包只支持 Windows。任何平台相关模块在非 Windows 上都会抛 NotImplementedError，
详见 acb/paths.py::require_windows。
"""

# 版本号同时用于 style_profile.json 的 version 字段与日志头部。
# 采用 主.次.修订 三段式；训练产出的风格文件会记录此版本，
# 以便未来结构升级时判断是否需要迁移。
#
# 升级时只改这一行：界面标题、日志头部、--version、style_profile 的 version
# 字段全部从它派生，不存在第二份硬编码。
__version__ = "1.1.0"

# 给用户看的名字（窗口标题、界面标题、关于对话框）。
APP_DISPLAY_NAME = "Auto Color Diffusion"

# 给系统看的标识：%APPDATA% 下的文件夹名、keyring 服务名、exe 文件名。
# 刻意用无空格的驼峰形式 —— Windows 凭据管理器的服务名与目录名都不适合带空格。
APP_ID = "AutoColorDiffusion"

# 改名前的旧标识。仅用于把老用户的数据目录迁移过来（见 paths.migrate_legacy_data）。
LEGACY_APP_ID = "AutoColorBleed"

# 进程启动时刻（单调时钟），用最小代价回答"这次启动到底花了多久"。
#
# 【为什么需要它】用户反馈过"点重启后半天不开窗口"，而日志里只有各阶段的
# 时间戳——分不清"解释器/导入慢"还是"某个探测卡住了"。有这个基准值，
# 日志里那一行 `启动耗时：…` 就能直接把两者分开（实测那次就是探测卡了 15 秒）。
# 放在包入口：只记一次，且早于任何业务代码。
import time as _time

PROCESS_START = _time.monotonic()


def process_age() -> float:
    """从进程启动到现在过了多少秒（单调时钟，不受系统改时间影响）。"""
    return _time.monotonic() - PROCESS_START


__all__ = ["__version__", "APP_DISPLAY_NAME", "APP_ID", "LEGACY_APP_ID", "process_age"]
