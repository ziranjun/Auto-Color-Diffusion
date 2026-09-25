# -*- coding: utf-8 -*-
"""轻量用户偏好存储（JSON 文件，跟着数据目录走）。

为什么不用 QSettings
--------------------
QSettings 在 Windows 上默认写**注册表**，而本程序有便携模式（exe 同目录放
`portable.txt`）。注册表里的设置不会跟着 U 盘走，换台机器就丢；把它放进数据目录
则便携模式天然生效，也与本程序其余数据（日志/缓存/风格/状态）落在同一处。

存放位置与结构
--------------
    <数据目录>/preferences.json

    {
      "export_suffix": "edit",          # 用户上次填的尾缀（空 = 用默认 -1）
      "export_format": "JPG",           # JPG / PNG
      "theme": "light",                 # light / dark
      "pinned_models": ["gateway"],     # 置顶的模型（按顺序排在下拉最前）
      "hidden_models": ["x"],           # 从下拉里移除的模型（可恢复）
      "pinned_styles": ["海边暖调"]      # 置顶的风格
    }

设计取舍
--------
  - 所有字段都可缺失，读不到就用默认值：这个文件被手改坏、被删掉，
    都不该让程序起不来（它只是偏好，不是业务数据）；
  - 写入走"读-改-写 + 临时文件替换"，避免中途崩溃留下半截 JSON；
  - 加锁：界面线程与工作线程都可能碰它，写操作必须串行。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Final

from .logging_setup import get_logger
from .paths import data_root

log = get_logger("preferences")

PREFERENCES_FILENAME: Final[str] = "preferences.json"

# 已知键的默认值。新增偏好时在这里补一行即可，
# 读取方用 get_str/get_list 时也会带上自己的兜底默认值。
DEFAULTS: Final[dict[str, Any]] = {
    "export_suffix": "",
    "export_format": "JPG",
    "theme": "light",
    # 下面四项是"上次用过的选项"，下次打开自动带出（用户要求）。
    # 空字符串 / None 都表示"没存过"，由读取方决定默认值。
    "color_space": "",
    "quality": None,
    "style": "",          # 风格名；空 = 用内置默认策略
    "model": "",          # models.yaml 里的条目名；空 = 用配置里的 active
    "pinned_models": [],
    "hidden_models": [],
    "pinned_styles": [],
}

_lock = threading.Lock()


def preferences_path() -> Path:
    """偏好文件路径（必须每次现算：测试与便携模式会改 APPDATA/exe 位置）。"""
    return data_root() / PREFERENCES_FILENAME


def load_preferences() -> dict[str, Any]:
    """读取全部偏好。文件不存在/损坏时返回空字典（而不是抛异常）。"""
    path = preferences_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("偏好文件无法解析，已按空配置处理（%s）：%s", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def save_preferences(data: dict[str, Any]) -> None:
    """整体写入偏好。**不抛异常**：偏好写不进去不该影响正常使用。"""
    path = preferences_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        log.warning("偏好保存失败（已忽略，不影响本次使用）：%s", exc)


def update_preferences(**changes: Any) -> dict[str, Any]:
    """读-改-写：只更新给定键，其余原样保留。返回合并后的完整字典。"""
    with _lock:
        data = load_preferences()
        data.update(changes)
        save_preferences(data)
        return data


def get_str(key: str, default: str = "") -> str:
    """取一个字符串偏好（类型不对就回退默认值）。"""
    value = load_preferences().get(key)
    if isinstance(value, str):
        return value
    return default


def get_int(key: str, default: int) -> int:
    """取一个整数偏好（类型不对就回退默认值）。

    必须显式排除 bool：`isinstance(True, int)` 为真，
    否则手写坏的 `"quality": true` 会静默变成 1（几乎等于最低质量）。
    """
    value = load_preferences().get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def get_list(key: str) -> list[str]:
    """取一个字符串列表偏好（过滤掉非字符串项，保证调用方不必再校验）。"""
    value = load_preferences().get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def set_list(key: str, items: list[str]) -> list[str]:
    """写入一个字符串列表偏好并返回它（去重且保序）。"""
    unique: list[str] = []
    for item in items:
        if item and item not in unique:
            unique.append(item)
    update_preferences(**{key: unique})
    return unique


def toggle_pinned(key: str, item: str) -> tuple[list[str], bool]:
    """把 item 在"置顶列表"里加上或去掉。返回 (新列表, 现在的状态)。

    置顶是**开关**语义：已经在最前面就取消置顶，否则插到最前面。
    这比"只能加不能减"好用得多 —— 用户点两次就复原，不需要额外按钮。
    """
    current = get_list(key)
    if current and current[0] == item:
        # 已经在最前 → 取消置顶（不是把它挪到第二，那样用户还得再点一次）
        new_list = [x for x in current if x != item]
        pin_state = False
    else:
        new_list = [item] + [x for x in current if x != item]
        pin_state = True
    set_list(key, new_list)
    return new_list, pin_state


def reorder_by_pinned(items: list[str], pinned: list[str]) -> list[str]:
    """按置顶列表重排：置顶项按置顶顺序排前面，其余保持原顺序。"""
    if not pinned:
        return list(items)
    pinned_present = [name for name in pinned if name in items]
    remaining = [name for name in items if name not in pinned_present]
    return pinned_present + remaining
