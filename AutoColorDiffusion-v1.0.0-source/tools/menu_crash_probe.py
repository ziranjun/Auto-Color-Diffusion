# -*- coding: utf-8 -*-
"""「右键下拉条目」硬崩溃探针 —— 在**子进程**里真实走一遍右键弹菜单。

为什么需要这么个奇怪的东西
--------------------------
进程内的自检抓不住访问冲突（0xC0000005）：一旦发生，测试进程自己就被系统终止了，
上面的 try/except、异常守卫统统没机会运行。真实事故：
在 QComboBox 的下拉弹窗里右键，弹菜单时用了 `QMenu.exec()`（嵌套事件循环），
与弹窗正在收尾的 grabs 撞上 → 访问冲突 → 程序瞬间消失，
**日志里连一行都没有**（Python 层根本没被调用到）。

所以判定方式只能是"把这一串操作放进子进程跑，看它能不能正常退出"：
    退出码 0                    → 没崩
    0xC0000005 (3221225477)     → 访问冲突
    0xC00000FD (3221225725)     → 栈溢出

用法
----
    python tools/menu_crash_probe.py open     # 下拉展开时右键条目（真实崩溃路径）
    python tools/menu_crash_probe.py closed   # 下拉未展开时右键下拉框
    python tools/menu_crash_probe.py all      # 三个装了菜单的下拉各走一遍（连接/模型/风格）
退出码 0 且 stdout 含 "PROBE_OK" 才算通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 平台与数据目录隔离：必须在导入 PyQt6 / 创建 QApplication **之前**。
# 沿用调用方给的 QT_QPA_PLATFORM（自检套件用 offscreen），默认也用 offscreen。
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="acb_probe_appdata_")

# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print 中文或 ✓/✗ 会抛
# UnicodeEncodeError —— 那会把"未通过清单"整段变成 traceback，等于把最关键的信息
# 藏起来（本项目真的踩到过）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QTimer   # noqa: E402
from PyQt6.QtGui import QContextMenuEvent                        # noqa: E402
from PyQt6.QtTest import QTest                                   # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu                  # noqa: E402

import acb.ui.main_window as MW                                  # noqa: E402


def main() -> int:
    variant = sys.argv[1] if len(sys.argv) > 1 else "open"
    # 拼错变体名（例如 opne）以前会静默跑 closed 分支 —— 测的东西完全变了，
    # 而 stdout 里照样有 PROBE_OK。白名单外一律报用法并给非零退出码。
    if variant not in ("open", "closed", "all"):
        print(f"未知变体：{variant!r}（可选：open / closed / all）", flush=True)
        return 2
    if variant == "all":
        return main_all()
    app = QApplication([])
    window = MW.MainWindow()
    window.show()
    QTest.qWait(150)

    combo = window.cmb_style
    row = combo.findData("default_neutral")
    if row < 0:
        row = 1
    view = combo.view()
    combo.setCurrentIndex(row)

    if variant == "open":
        combo.showPopup()
        QTest.qWait(250)
        target = view.viewport()
        rect = view.visualRect(view.model().index(row, 0))
        pos = rect.center()
        to_global = target.mapToGlobal(pos)
    else:
        target = combo
        pos = combo.rect().center()
        to_global = combo.mapToGlobal(pos)

    # 关键：按**真实右键的事件顺序**发：按下 → 松开 → ContextMenu。
    #
    # ⚠ 只发 ContextMenu 是不够的（这是踩过的坑）：QComboBox 的弹窗容器会在
    #   **右键松开**时把下拉收起来（它把"在条目上松开鼠标"当成"选中这一项"），
    #   用户看到的"右键后列表缩回去"就是这么来的。
    #   上一版探针只发了 ContextMenu，于是完全看不见这个行为，
    #   导致修复了个没有被观察到的现象。
    if variant == "open":
        QTest.mousePress(target, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, pos)
        QTest.mouseRelease(target, Qt.MouseButton.RightButton,
                           Qt.KeyboardModifier.NoModifier, pos)
        QTest.qWait(80)
        print(f"  右键按下+松开后：下拉列表仍展开={view.isVisible()}", flush=True)

    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, to_global)
    QCoreApplication.postEvent(target, event)

    # 菜单是 popup() 异步弹的，所以要给它时间；期间任何硬崩溃都会终止本进程。
    QTest.qWait(700)

    popup = app.activePopupWidget()
    opened = isinstance(popup, QMenu)
    texts = [a.text() for a in popup.actions()] if opened else []
    # 下拉列表**必须还开着**（用户反馈过"右键会让选项列表缩回去"）。
    # 不主动 hidePopup() 才是对的 —— 这条断言就是防它被无意中加回来。
    dropdown_kept = view.isVisible()

    if opened:
        print(f"变体={variant} 菜单已弹出：{texts}", flush=True)
        print(f"  菜单弹出时下拉列表仍展开={dropdown_kept}", flush=True)
        # 真的点一下第一个动作，确保"点完之后"的路径也不会崩、且列表仍在。
        popup.actions()[0].trigger()
        QTest.qWait(300)
        print(f"  点完菜单项后下拉列表仍展开={view.isVisible()}", flush=True)
    else:
        print(f"变体={variant} 菜单未弹出（popup={type(popup).__name__ if popup else None}）",
              flush=True)

    pinneds = [combo.itemText(i) for i in range(combo.count())
               if combo.itemText(i).startswith("★ ")]
    print(f"  置顶项：{pinneds}", flush=True)

    window.close()
    QTest.qWait(100)

    # 判定：必须真的弹出过菜单、下拉列表必须还在，且整个进程活着走到这里
    if not opened:
        print("PROBE_FAIL：右键没有弹出菜单", flush=True)
        return 1
    if variant == "open" and not dropdown_kept:
        print("PROBE_FAIL：菜单弹出时下拉列表被收起来了", flush=True)
        return 1
    print("PROBE_OK", flush=True)
    return 0


def exercise(combo, row: int, label: str) -> tuple[bool, bool]:
    """对某个下拉真实走一遍：展开 → 右键按下/松开 → ContextMenu → 弹菜单 → 点一下。

    返回 (是否弹出过菜单, 菜单弹出时下拉是否仍展开)。

    【为什么每个装了菜单的下拉都要过这一遍】这类崩溃（0xC0000005）是**静默**的：
    进程被系统直接终止，日志里一行都没有。它只在"真实事件派发 + 真实弹窗"下出现，
    而且**与数量有关** —— 实测把菜单装到第三个下拉（「连接」）之后，
    退出码在 8 次里有 3 次变成 0xC0000005；只测一个下拉是看不见的。
    """
    app = QApplication.instance()
    view = combo.view()
    combo.setCurrentIndex(row)
    combo.showPopup()
    QTest.qWait(200)
    target = view.viewport()
    pos = view.visualRect(view.model().index(row, 0)).center()
    QTest.mousePress(target, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(target, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.qWait(60)
    QCoreApplication.postEvent(
        target, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, target.mapToGlobal(pos))
    )
    QTest.qWait(600)
    popup = app.activePopupWidget() if app else None
    if not isinstance(popup, QMenu):
        return False, view.isVisible()
    actions = popup.actions()
    print(f"  [{label}] 菜单：{[a.text() for a in actions]}", flush=True)
    # ⚠ 只触发"置顶"这类安全动作，**绝不**触发"删除"：
    #   删除会弹 QMessageBox 确认框，在 offscreen 下它是真的模态并阻塞等待点击 ——
    #   整个探针会挂死（实测：`all` 变体卡满 300 秒超时）。
    #   删除路径另有覆盖：gui_smoke_test 里把 QMessageBox.question 换成桩再调。
    safe = [a for a in actions if a.text() in ("置顶", "取消置顶")]
    if safe:
        safe[0].trigger()
    else:
        popup.close()
    QTest.qWait(250)
    return True, view.isVisible()


def main_all() -> int:
    """把**每个**装了下拉右键菜单的下拉都真实走一遍（防"数量增加"这类崩溃）。"""
    app = QApplication([])
    window = MW.MainWindow()
    window.show()
    QTest.qWait(150)
    combos = [
        ("连接", window.cmb_provider, 0),
        ("模型", window.cmb_model, 0),
        ("风格", window.cmb_style, max(0, window.cmb_style.findData("default_neutral"))),
    ]
    ok = True
    for label, combo, row in combos:
        if combo.count() == 0:
            print(f"  [{label}] 跳过：下拉是空的", flush=True)
            continue
        opened, kept = exercise(combo, min(row, combo.count() - 1), label)
        print(f"  [{label}] 弹出菜单={opened} 下拉保持展开={kept}", flush=True)
        ok = ok and opened and kept
    window.close()
    QTest.qWait(150)
    print("PROBE_OK" if ok else "PROBE_FAIL：有下拉没能弹出菜单或列表被收起", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
