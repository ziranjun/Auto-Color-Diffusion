# -*- coding: utf-8 -*-
"""GUI 级自检 —— 覆盖"非 AI 全链路自检"够不到的那一半。

为什么必须有这个工具（它是被一个真实事故逼出来的）
--------------------------------------------------
`tools/smoke_test.py` 只测非界面逻辑，所以它全绿的时候，
界面仍然可能"一添加图片就闪退"——真实发生过：

    main_window._append_table_row 里误用了未定义变量
    （`elide_middle(name, 70)` 应为 `path.name`），
    用户点「添加照片」后程序直接消失。

为什么那个 bug 破坏力特别大：
  1. 未定义变量只在**运行时**报错，导入期与 compileall 都发现不了；
  2. PyQt ≥5.5 对"逃出槽函数的 Python 异常"的默认处理是调 qFatal()
     **直接终止进程**，用户看到的是闪退而不是可读报错；
  3. 异常发生在 Qt 的槽里，静态分析工具当时也没报出来。

因此本工具做三件事：
  A. 用 offscreen 平台真实构造主窗口，并**走真实的槽函数路径**
     （monkeypatch 掉原生文件对话框，其余流程一字不改）；
  B. 安装 Qt 消息处理器，抓取 QFont 之类的 C++ 侧警告——
     这类警告不会出现在 Python traceback 里，只能靠消息处理器捕获；
  C. 验证异常守卫：故意在槽里抛异常，断言进程**存活**且弹窗被调用，
     而不是 qFatal 闪退。

用法
----
    python tools/gui_smoke_test.py
    python tools/gui_smoke_test.py --raw-dir test_data   # 用真实文件跑一遍
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 必须在导入 PyQt6 / 创建 QApplication **之前**设置平台插件。
# 用 offscreen 才能在没有显示器的环境（CI、SSH、本工具的自动化运行）里跑。
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 允许以 `python tools/gui_smoke_test.py` 直接运行
# 控制台/管道的代码页可能是 GBK（Windows 默认 936），直接 print "✓/✗" 会抛
# UnicodeEncodeError —— 那会把"未通过清单"变成一段 traceback，等于把最关键的信息
# 藏起来（本文件真的踩到过这个坑）。统一把标准输出切到 UTF-8 并替换掉不能编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QRect, QtMsgType, qInstallMessageHandler  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

# --- Qt 消息捕获 -----------------------------------------------------------
# Qt 的 warning 走 qWarning，不会出现在 Python traceback 里。
# 那个 "QFont::setPointSize: Point size <= 0 (-1)" 就是这么来的。
_qt_messages: list[tuple[str, str]] = []

# offscreen 平台插件的已知无害警告（与我们的代码无关，纯平台限制）。
_BENIGN_QT_MESSAGE_MARKERS = (
    "propagateSizeHints",     # offscreen 插件不支持窗口尺寸提示传播
    "QStandardPaths",         # 无桌面环境时的 XDG 路径提示
    "Fontconfig",             # 无字体配置
    "qt.qpa.",                # 平台插件自身的调试信息
)

# 环境相关警告：由"测试/运行时环境"引起，不是程序逻辑缺陷。
# 单独归类而不是直接算作 benign，是为了**仍然把它显示出来**——
# 屏蔽和分类是两回事：前者会掩盖问题，后者让人知道发生了什么。
# 每一项都必须写清"为什么与程序无关"。
_ENVIRONMENT_QT_MESSAGE_MARKERS = (
    # PyQt6 的 Qt6 wheel 不再随附字体，且 offscreen 插件在 Windows 上
    # 没有 fontconfig 可以回退，于是 QFontDatabase 找不到字体目录。
    # 正常双击运行（windows 平台插件）时使用系统字体，不会出现这条。
    "Cannot find font directory",
)


def _is_benign(message: str) -> bool:
    return any(marker in message for marker in _BENIGN_QT_MESSAGE_MARKERS)


def _is_environmental(message: str) -> bool:
    return any(marker in message for marker in _ENVIRONMENT_QT_MESSAGE_MARKERS)


def _qt_message_handler(mode: QtMsgType, context, message: str) -> None:
    """把所有 Qt 消息收进列表，稍后统一判定。"""
    kind = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "fatal",
    }.get(mode, "unknown")
    _qt_messages.append((kind, message))


# --- 结果收集 --------------------------------------------------------------
PASS = "[通过]"
FAIL = "[失败]"
_results: list[tuple[str, bool, str]] = []
_skipped: list[str] = []


def record(name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> None:
    """记一条 GUI 自检结果；`skipped=True` = "条件不满足、根本没实测"（见 smoke_test 的说明）。"""
    _results.append((name, ok, detail))
    if skipped:
        _skipped.append(name)
    print(f"{PASS if ok else FAIL} {name}" + (f"\n        {detail}" if detail else ""))


# ===========================================================================
# 测试项
# ===========================================================================

def test_construct_window(app: QApplication):
    """构造主窗口。这一步会跑环境自检，能同时暴露导入期与启动期的问题。"""
    from acb.ui.main_window import MainWindow

    try:
        window = MainWindow()
    except Exception as exc:
        import traceback

        record("构造主窗口", False,
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
        return None
    record("构造主窗口（含环境自检）", True)
    return window


def _candidate_files(raw_dir: Path | None) -> list[Path]:
    """挑一批用于测试的文件（优先用真实样本，否则造临时文件）。"""
    directory = raw_dir or (Path(__file__).resolve().parent.parent / "test_data")
    candidates: list[Path] = []
    if directory.is_dir():
        candidates = sorted(
            p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() in (".cr3", ".cr2", ".nef", ".dng", ".xmp")
        )
    if candidates:
        return candidates

    tmp = Path(tempfile.mkdtemp(prefix="acb_gui_"))
    made: list[Path] = []
    for name in ("a.CR3", "b.CR3", "海边 01.cr3"):
        p = tmp / name
        p.write_bytes(b"x")
        made.append(p)
    return made


def test_add_sources(window, candidates: list[Path]) -> None:
    """添加文件 —— 这就是原先闪退的那条路径。"""
    try:
        window._add_sources(candidates)
    except Exception as exc:
        import traceback

        record("_add_sources 添加文件", False,
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1000:]}")
        return

    rows = window.table.rowCount()
    record(
        f"_add_sources 添加 {len(candidates)} 个文件 → 表格 {rows} 行",
        rows == len(candidates),
        f"期望 {len(candidates)} 行，实际 {rows} 行",
    )

    # 表格单元格必须真的有内容（空字符串说明显示逻辑写坏了）
    first_item = window.table.item(0, 0)
    first_text = first_item.text() if first_item is not None else ""
    tooltip = first_item.toolTip() if first_item is not None else ""
    record(
        "文件表首行显示文件名且 tooltip 为完整路径",
        bool(first_text) and bool(tooltip),
        f"单元格文本={first_text!r}",
    )

    # --- 去重：重复添加同一批不应新增行 ---
    try:
        window._add_sources(candidates)
        record(
            "_add_sources 重复添加同一路径不新增行",
            window.table.rowCount() == rows,
            f"仍为 {window.table.rowCount()} 行",
        )
    except Exception as exc:
        record("_add_sources 重复添加同一路径不新增行", False, str(exc))

    # --- 状态列更新 ---
    try:
        window._set_item_status(candidates[0].name, "已完成")
        status_item = window.table.item(0, 1)
        status_text = status_item.text() if status_item is not None else ""
        record("_set_item_status 更新状态列", status_text == "已完成", f"状态列={status_text!r}")
    except Exception as exc:
        record("_set_item_status 更新状态列", False, str(exc))


def test_slot_path_add_files(window) -> None:
    """走**真实槽函数** _on_add_files（monkeypatch 掉原生文件对话框）。

    只测 _add_sources 是不够的：真正的崩溃点是槽函数被 Qt 调用时的路径，
    而 PyQt 对槽内异常的处理方式（qFatal 终止进程）与直接调用完全不同。
    """
    from acb.ui import main_window as mw

    raw_dir = Path(__file__).resolve().parent.parent / "test_data"
    picked = sorted(raw_dir.glob("*.CR3"))[:2] if raw_dir.is_dir() else []

    if not picked:
        record("_on_add_files 槽路径（test_data 中无 .CR3，跳过）", True, skipped=True)
        return

    original = mw.choose_raw_files
    mw.choose_raw_files = lambda parent=None, start_dir="": list(picked)  # type: ignore[assignment]

    try:
        window._on_clear_files()
        window._on_add_files()
    except Exception as exc:
        import traceback

        record("_on_add_files 槽路径", False,
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1000:]}")
        return
    finally:
        mw.choose_raw_files = original  # type: ignore[assignment]

    after = window.table.rowCount()
    record(
        "_on_add_files 槽路径（经 Qt 槽调用链）",
        after == len(picked) > 0,
        f"加入 {len(picked)} 个 → 表格 {after} 行",
    )


def test_mode_switch(window) -> None:
    """模式切换：输出 ↔ 训练。"""
    try:
        for training in (True, False, True, False):
            window.rb_train.setChecked(training)
            window.rb_output.setChecked(not training)
            expected_index = 1 if training else 0
            if window.option_stack.currentIndex() != expected_index:
                record(
                    "模式切换（输出↔训练）",
                    False,
                    f"training={training} 但 stack index={window.option_stack.currentIndex()}",
                )
                return
        record("模式切换（输出↔训练，含控件可见性）", True)
    except Exception as exc:
        record("模式切换（输出↔训练）", False, str(exc))


def test_option_row_layout(window) -> None:
    """底栏三行的构成与顺序（用户明确要求的布局）。

    行 1：「输出」总开关 + 输出目录 + 输出到 _export 子目录
    行 2：输出色彩空间 + 输出质量
    行 3：输出格式 + 尾缀（左边），运行按钮在右边

    三个要点都有来历：
      · 「输出」开关原本在高级设置里，说明写死了"完成后自动调用 Photoshop 导出 JPG"
        —— 现在还能导 PNG，那句话已经不准确；而且它是每次都要看一眼的开关。
      · 「输出」必须排在**所有输出设置之前**：它决定"到底出不出片"，
        后面的输出位置/色彩空间/质量/格式/尾缀都是它的从属设置。
        以前输出目录在它上面，看上去像两组互不相干的设置（用户指出过）。
      · 「格式」从行 2 挪到行 3：挤在一起会把质量说明文字压到显示不全。
    """
    from PyQt6.QtWidgets import QLabel

    switch = window._out_switch_options
    quick = window._out_quick_options
    suffix = window._out_suffix_options
    adv = window._adv_dialog

    labels = [lbl.text() for lbl in quick.findChildren(QLabel)]
    suffix_labels = [lbl.text() for lbl in suffix.findChildren(QLabel)]

    # 顺序用**窗口坐标**验，而不是只验"在同一个容器里"：
    # 同一个容器里也可能把开关摆在最后。
    #
    # 必须先 show()：窗口没显示时 Qt 根本不排布局，mapTo() 对所有控件都返回 0，
    # 三行会"看起来同高"，断言假失败（布局没算过这个坑踩过好几次了）。
    # offscreen 平台下 show() 不会真弹窗。
    window.show()
    QApplication.processEvents()
    y_switch = window.chk_run_ps.mapTo(window, window.chk_run_ps.rect().topLeft()).y()
    y_quick = window.cmb_color_space.mapTo(window, window.cmb_color_space.rect().topLeft()).y()
    y_suffix = window.cmb_format.mapTo(window, window.cmb_format.rect().topLeft()).y()
    order_ok = y_switch < y_quick < y_suffix

    ok = (
        # 「输出」开关在主界面上、已离开高级设置
        window.chk_run_ps.text() == "输出"
        and window.chk_run_ps.isChecked()
        and not adv.isAncestorOf(window.chk_run_ps)
        and "JPG" not in window.chk_run_ps.text()      # 旧的不准确描述不能再回来
        # 行 1：开关在最前，后面才是输出位置两个控件
        and switch.isAncestorOf(window.chk_run_ps)
        and switch.isAncestorOf(window.picker_output)
        and switch.isAncestorOf(window.chk_export_subdir)
        # 行 2：色彩空间与质量
        and quick.isAncestorOf(window.cmb_color_space)
        and quick.isAncestorOf(window.slider_quality)
        and "输出色彩空间" in labels
        and "输出质量" in labels
        # 行 3：输出格式 + 尾缀
        and suffix.isAncestorOf(window.cmb_format)
        and suffix.isAncestorOf(window.lbl_suffix)
        and suffix.isAncestorOf(window.edit_suffix)
        and "输出格式" in suffix_labels
        and "格式" not in suffix_labels                 # 已改名为「输出格式」
        and order_ok                                    # 输出开关在所有输出设置上方
    )
    record(
        "底栏布局：「输出」在最前（位置/色彩空间/质量/格式依次在后）",
        ok,
        f"行序（纵坐标）=开关{y_switch} < 色彩空间{y_quick} < 格式{y_suffix} → {order_ok}；"
        f"输出位置与开关同行={switch.isAncestorOf(window.picker_output)} "
        f"已离开高级设置={not adv.isAncestorOf(window.chk_run_ps)} "
        f"行2标签={labels} 行3标签={suffix_labels}",
    )


def test_output_toggle_disables_options(window) -> None:
    """未勾选「输出」时，后面的色彩空间/质量/格式/尾缀全部变灰不可选。

    要点有两个：
      1) 变灰而不是隐藏 —— 隐藏会改变底栏宽度，左右两栏比例会跟着跳；
      2) 「输出」勾选框**自己**必须保持可选，否则取消勾选后就再也点不回来了。
    这两个禁用来源（训练模式 / 未勾输出）必须合并计算，
    各写各的 setEnabled 会互相覆盖，所以这里两种情形都验一遍。
    """
    was_checked = window.chk_run_ps.isChecked()
    was_training = window.rb_train.isChecked()

    def sub_enabled() -> dict[str, bool]:
        return {
            "输出目录": window.picker_output.isEnabled(),
            "输出目录输入框": window.picker_output.edit.isEnabled(),
            "_export子目录": window.chk_export_subdir.isEnabled(),
            "色彩空间": window.cmb_color_space.isEnabled(),
            "色彩空间标签": window.lbl_color_space.isEnabled(),
            "质量滑块": window.slider_quality.isEnabled(),
            "质量标签": window.lbl_quality.isEnabled(),
            "格式": window.cmb_format.isEnabled(),
            "尾缀": window.edit_suffix.isEnabled(),
        }

    try:
        window.rb_output.setChecked(True)
        window.cmb_format.setCurrentIndex(window.cmb_format.findData("JPG"))
        window.chk_run_ps.setChecked(True)
        checked_state = sub_enabled()
        box_kept = window.chk_run_ps.isEnabled()

        window.chk_run_ps.setChecked(False)
        unchecked_state = sub_enabled()
        box_still_clickable = window.chk_run_ps.isEnabled()
        widths = (window.cmb_color_space.width(), window.cmb_format.width(),
                  window.slider_quality.width())

        window.chk_run_ps.setChecked(True)
        restored_state = sub_enabled()

        # 训练模式：包括勾选框在内全部不能用（但依然只是变灰，不隐藏）。
        window.rb_train.setChecked(True)
        train_state = sub_enabled()
        chk_in_train = window.chk_run_ps.isEnabled()
        window.rb_output.setChecked(True)
        back_state = sub_enabled()

        ok = (
            box_kept
            and all(checked_state.values())
            and not any(unchecked_state.values())
            and box_still_clickable                      # 自己不能被自己灰掉
            and all(restored_state.values())
            and not any(train_state.values())
            and not chk_in_train
            and all(back_state.values())                 # 切回输出模式后能恢复
            and widths == (window.cmb_color_space.width(), window.cmb_format.width(),
                           window.slider_quality.width())   # 变灰不改布局宽度
        )
        record(
            "未勾选「输出」时后面选项变灰（勾选框自身仍可点、宽度不变）",
            ok,
            f"勾选={checked_state} 未勾选={unchecked_state} "
            f"勾选框仍可点={box_still_clickable} 训练模式={train_state} "
            f"训练模式勾选框可点={chk_in_train}",
        )
    finally:
        window.rb_output.setChecked(not was_training)
        window.rb_train.setChecked(was_training)
        window.chk_run_ps.setChecked(was_checked)


def test_autopilot_style_available(window) -> None:
    """内置「AI自主决策」：随时可选（不限于离线调试）、删不掉、与默认基准风格互不干扰。

    用户要求：内置风格（界面中不可删除）+ 选中即让 AI 自主决策，不预设偏好。
    这条用例守的是**两个内置风格的差异**：default_neutral 只在离线调试下可选，
    「AI自主决策」是日常功能。曾经的实现用 is_seed_style(...) 判断"调试专用"，
    一旦加入第二个内置风格就会把它一起灰掉 —— 所以这里必须分别断言。
    """
    from acb.constants import AUTOPILOT_STYLE_NAME, DEFAULT_STYLE_NAME

    row = window.cmb_style.findData(AUTOPILOT_STYLE_NAME)
    exists = row >= 0
    enabled_offline_off = (
        exists and window.cmb_style.model().item(row).isEnabled() is True
    )
    tip = str(window.cmb_style.model().item(row).toolTip()) if exists else ""

    # 关着离线调试时，两个内置风格的可选性必须相反
    window.chk_offline.setChecked(False)
    window._reload_styles()
    base_row = window.cmb_style.findData(DEFAULT_STYLE_NAME)
    base_disabled = (
        base_row >= 0 and window.cmb_style.model().item(base_row).isEnabled() is False
    )
    # 选中它必须真的选得上（而且不会被弹回占位项）
    window.cmb_style.setCurrentIndex(window.cmb_style.findData(AUTOPILOT_STYLE_NAME))
    chosen = window.cmb_style.currentData() == AUTOPILOT_STYLE_NAME
    collected = window._collect_output_options(resume=False, only_failed=False)
    style_name_ok = collected.style_name == AUTOPILOT_STYLE_NAME
    style_data_ok = bool(collected.style_data) and (
        collected.style_data.get("name") == AUTOPILOT_STYLE_NAME
    )

    # 删不掉：走真实的删除路径，文件必须还在
    from acb.paths import styles_dir

    with _StubDialogs():
        window._on_delete_style(AUTOPILOT_STYLE_NAME)
    not_deleted = (styles_dir() / f"{AUTOPILOT_STYLE_NAME}.json").is_file()

    ok = (
        exists
        and enabled_offline_off
        and base_disabled
        and chosen
        and style_name_ok
        and style_data_ok
        and not_deleted
        and "自主" in tip
        and "不限制" in tip
    )
    record(
        "内置「AI自主决策」：随时可选 / 能选中并生效 / 不可删除",
        ok,
        f"存在={exists} 非调试下可用={enabled_offline_off} 基准风格同时被禁用={base_disabled} "
        f"能选中={chosen} 参数里带上它={style_name_ok and style_data_ok} "
        f"删除被拒（文件仍在）={not_deleted} 提示含自主与不限制={('自主' in tip, '不限制' in tip)}",
    )


def test_api_key_deletion(window) -> None:
    """删除已保存的密钥：二次确认、默认按钮是「否」、删完说真话。

    用户要求：为了 API Key 安全，用户应能对已记录的 key 做删除管理（删除需二次确认）。
    这条用例覆盖四个真实分支：
      1. 取消确认 → 密钥必须**原样还在**（不能"点了就删"）；
      2. 确认删除 → 密钥链条目与本次会话内存都清掉，界面提示与 placeholder 跟着变；
      3. 环境变量来源 → 删完必须**如实说明删不掉**（程序仍会读到它）；
      4. 没有密钥时 → 不删任何东西，也不报错。
    """
    from acb.keyring_store import mask

    from PyQt6.QtWidgets import QMessageBox as _QMB

    fake = _InMemoryKeyring()
    orig_module = window._key_store._keyring_module
    orig_discover = window._start_model_discovery
    saved_env = os.environ.get("OPENAI_API_KEY")
    try:
        window._key_store._keyring_module = fake
        window._start_model_discovery = lambda *a, **k: None   # 不发网络
        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("openai"))
        secret = "sk-" + "c" * 24
        window._key_store.set("OPENAI_API_KEY", secret)

        # 确认框正文：账户名、掩码、来源、不可撤销，四样都要说
        text = window._delete_key_confirm_text("OpenAI", "OPENAI_API_KEY", secret)
        text_ok = (
            "OPENAI_API_KEY" in text and mask(secret) in text
            and "来源" in text and "无法撤销" in text
        )

        # 1) 取消 → 什么都不删
        with _StubDialogs(_QMB.StandardButton.No):
            window._on_delete_key()
        cancelled_kept = window._key_store.get("OPENAI_API_KEY") == secret
        cancelled_note = "已取消" in window.lbl_key_status.text()

        # 2) 确认 → 删掉（密钥链 + 内存），提示与 placeholder 同步
        with _StubDialogs(_QMB.StandardButton.Yes):
            window._on_delete_key()
        deleted = window._key_store.get("OPENAI_API_KEY") is None
        keyring_cleared = "OPENAI_API_KEY" not in fake.store
        status_deleted = "已删除" in window.lbl_key_status.text()
        # placeholder 不能再写着"已保存密钥"（否则用户会以为没删掉）
        placeholder_ok = "已保存密钥" not in window.edit_key.placeholderText()

        # 3) 环境变量来源：删得掉 keyring，但要如实说明环境变量仍在
        os.environ["OPENAI_API_KEY"] = "sk-" + "d" * 24
        window._key_store.set("OPENAI_API_KEY", "sk-" + "e" * 24)
        env_text = window._delete_key_confirm_text("OpenAI", "OPENAI_API_KEY", "sk-" + "e" * 24)
        env_warned = "环境变量" in env_text
        with _StubDialogs(_QMB.StandardButton.Yes):
            window._on_delete_key()
        env_honest = (
            "环境变量" in window.lbl_key_status.text()
            and window._key_store.get("OPENAI_API_KEY") == "sk-" + "d" * 24
        )

        # 4) 完全没有密钥时：不许误删、不许误报，只弹一个说明框
        #    （提示走的是对话框而不是状态行，所以这里要**抓住对话框的正文**，
        #     不能去读 lbl_key_status —— 我第一版就是读状态行，于是这条假失败）
        os.environ.pop("OPENAI_API_KEY", None)
        window._key_store.delete("OPENAI_API_KEY")
        infos: list[str] = []
        orig_info = _QMB.information
        _QMB.information = staticmethod(
            lambda *a, **k: infos.append(str(a[2]) if len(a) > 2 else "")
            or _QMB.StandardButton.Ok
        )
        try:
            window._on_delete_key()
        finally:
            _QMB.information = orig_info
        no_key_note = any("没有已保存的密钥" in text for text in infos)

        ok = (
            text_ok and cancelled_kept and cancelled_note
            and deleted and keyring_cleared and status_deleted and placeholder_ok
            and env_warned and env_honest and no_key_note
        )
        detail = (
            f"确认文案完整={text_ok}；取消后密钥仍在={cancelled_kept} 有取消提示={cancelled_note}；"
            f"确认后已删除={deleted} 密钥链已清={keyring_cleared} 提示={'已删除' in window.lbl_key_status.text()} "
            f"占位已复位={placeholder_ok}；环境变量来源警告={env_warned} 删后如实说明={env_honest}；"
            f"无密钥时提示={no_key_note}"
        )
    except Exception as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        # ⚠ 顺序很重要：**先在假后端还在的时候**把测试密钥清干净，再还原后端。
        # 反过来写（先还原再删）会拿真实凭据管理器下手 —— 这条真实事故已经发生过一次
        # （那次删掉的是用户真实的 OPENAI_API_KEY，靠 DEEPSEEK 槽里逐字符相同的值才恢复回来）。
        window._key_store.delete("OPENAI_API_KEY")
        window._key_store._keyring_module = orig_module
        window._start_model_discovery = orig_discover
        if saved_env is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = saved_env

    record("删除密钥：二次确认（默认否）/ 取消不删 / 删后说真话（含环境变量）", bool(ok), detail)


def test_offline_debug_mode(window) -> None:
    """「离线调试」：两个旧开关合并成一个；开启后 AI 选项整组停用。

    用户要求（原话拆成三条）：
      1. 「离线模式」与「调试模式」合并，改名「离线调试」；
      2. 为了方便调试而存在的那个风格文件（内置基准 default_neutral）
         不能在界面里删除，且**仅在离线调试下可选**、其它时候变灰；
      3. 开启离线调试后，关于 AI 的那些选项变灰无法选择。
    """
    from acb.constants import DRY_RUN_LIMIT

    def ai_widgets():
        # 「关于 AI 的那些选项」= 连接 / 模型 / 密钥 三整行（含标签与「＋」按钮）
        return [
            window.cmb_provider, window.btn_new_connection,
            window.cmb_model, window.btn_add_model,
            window.edit_key, window.btn_show_key, window.btn_save_key,
            window.btn_delete_key,
            window.lbl_conn_caption, window.lbl_model_caption, window.lbl_key_caption,
        ]

    try:
        # 旧的两个勾选框必须只剩一个（合并掉的不能还留着）
        merged = getattr(window, "chk_dry_run", None) is None
        label_ok = "离线调试" in window.chk_offline.text()
        tip_ok = str(DRY_RUN_LIMIT) in window.chk_offline.toolTip() and "XMP" in window.chk_offline.toolTip()

        # ---- 关闭态：AI 选项可用；内置基准风格被禁用且选不上 ----
        window.chk_offline.setChecked(False)
        off_opts = window._collect_output_options(resume=False, only_failed=False)
        off_ai_enabled = all(w.isEnabled() for w in ai_widgets())
        off_not_dry = off_opts.dry_run is False

        seed_row = window.cmb_style.findData("default_neutral")
        seed_exists = seed_row >= 0
        seed_disabled = (
            seed_exists and window.cmb_style.model().item(seed_row).isEnabled() is False
        )
        # 就算"记忆里"或"刷新前"是它，也不许停在它上面（灰掉却生效比不能选更糟）
        window.cmb_style.setCurrentIndex(seed_row)
        window._reload_styles()
        no_sticky = window.cmb_style.currentData() is None and window.cmb_style.currentIndex() == 0

        # ---- 开启态：AI 选项整组停用；内置基准风格变为可选；dry_run 随之打开 ----
        window.chk_offline.setChecked(True)
        on_opts = window._collect_output_options(resume=False, only_failed=False)
        on_ai_disabled = all(not w.isEnabled() for w in ai_widgets())
        on_dry = on_opts.dry_run is True          # 合并后：离线调试 = 只处理前 3 张
        on_hint = "离线调试" in window.lbl_key_status.text()

        seed_row2 = window.cmb_style.findData("default_neutral")
        seed_selectable = (
            seed_row2 >= 0 and window.cmb_style.model().item(seed_row2).isEnabled() is True
        )
        window.cmb_style.setCurrentIndex(seed_row2)
        seed_chosen = window.cmb_style.currentData() == "default_neutral"

        # ---- 关回去：状态必须完全恢复（不能留下"粘住"的禁用）----
        window.chk_offline.setChecked(False)
        back_ai_enabled = all(w.isEnabled() for w in ai_widgets())
        back_opts = window._collect_output_options(resume=False, only_failed=False)
        back_ok = back_opts.dry_run is False and back_ai_enabled
        # 关掉之后，之前选中的内置基准风格必须被弹回「不选」
        back_no_seed = window.cmb_style.currentData() is None

        detail = (
            f"旧开关已合并={merged} 名称={label_ok} 工具提示含上限与 XMP={tip_ok} | "
            f"关：AI 可用={off_ai_enabled} 非 dry_run={off_not_dry} "
            f"基准风格存在={seed_exists} 已禁用={seed_disabled} 不粘住={no_sticky} | "
            f"开：AI 全停用={on_ai_disabled} dry_run={on_dry} 有说明={on_hint} "
            f"基准风格可选={seed_selectable} 能选中={seed_chosen} | "
            f"关回：{back_ok} 基准风格被弹回={back_no_seed}"
        )
        ok = all((
            merged, label_ok, tip_ok,
            off_ai_enabled, off_not_dry, seed_exists, seed_disabled, no_sticky,
            on_ai_disabled, on_dry, on_hint, seed_selectable, seed_chosen,
            back_ok, back_no_seed,
        ))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    finally:
        window.chk_offline.setChecked(False)

    record("离线调试：两开关已合并 / AI 选项整组停用 / 内置基准风格仅调试下可选", ok, detail)


def test_output_dir_on_main_window(window) -> None:
    """输出位置（输出目录 / _export 子目录）必须在主界面上，不在「高级设置」里。

    曾经的缺陷：这两个控件住在高级设置对话框里，用户会以为
    “程序不能自选输出位置”，而它一旦不对，产出就跑到了自己找不到的地方。
    与「输出」开关同一个道理，所以两个断言写法也一致：
      · 在主界面底栏里（isAncestorOf）；且**不在**高级设置里。
    另外守住“勾了子目录就把输入框置灰”这条（置灰了就不能再用它的值，
    值的一致性由 test_collect_options 守）。
    """
    switch = window._out_switch_options      # 底栏第 1 行：「输出」开关 + 输出位置
    adv = window._adv_dialog
    was_checked = window.chk_export_subdir.isChecked()
    try:
        ok = (
            switch.isAncestorOf(window.picker_output)
            and switch.isAncestorOf(window.chk_export_subdir)
            and not adv.isAncestorOf(window.picker_output)
            and not adv.isAncestorOf(window.chk_export_subdir)
        )

        # 勾了子目录 → 自定义目录框必须置灰（而不是“看着灰了却还在用”）。
        window.chk_export_subdir.setChecked(True)
        greyed = (not window.picker_output.edit.isEnabled()
                  and not window.picker_output.button.isEnabled())
        window.chk_export_subdir.setChecked(False)
        active = (window.picker_output.edit.isEnabled()
                  and window.picker_output.button.isEnabled())

        record(
            "输出位置在主界面：输出目录 + _export 子目录（已离开高级设置）",
            ok and greyed and active,
            f"在主界面底栏={switch.isAncestorOf(window.picker_output)} "
            f"在高级设置={adv.isAncestorOf(window.picker_output)} "
            f"勾选后置灰={greyed} 取消后可用={active}",
        )
    finally:
        window.chk_export_subdir.setChecked(was_checked)


def test_chinese_context_menu(window) -> None:
    """Qt 自带文案必须是中文：输入框右键菜单 + 标准对话框按钮。

    曾经的缺陷：密钥输入框右键出来的是 Undo / Redo / Cut / Copy / Paste /
    Delete / Select All，整块界面就这一处是英文。

    这些文案**不在我们的代码里**，是 Qt 提供的，靠 QTranslator 加载
    qtbase_zh_CN.qm 才会变中文 —— 所以这里不能只验"代码里调了安装函数"，
    必须验"菜单真的变成中文了"（翻译文件缺失、加载路径写错都会失败）。
    """
    from PyQt6.QtWidgets import QLineEdit, QMessageBox

    def is_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    editor = QLineEdit("abc")
    menu = editor.createStandardContextMenu()
    items = [a.text() for a in menu.actions() if a.text()]
    menu.deleteLater()
    # 去掉 "\tCtrl+Z" 这类快捷键后缀后再看文案
    plain = [t.split("\t")[0].replace("&", "") for t in items]

    box = QMessageBox()
    box.setStandardButtons(
        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
    )
    buttons = {b.text() for b in box.buttons()}

    ok = (
        len(items) >= 7                       # 标准菜单就是 7 项，少一项说明取值方式变了
        and all(is_chinese(t) for t in plain)
        and buttons == {"确定", "取消"}        # 不是 Yuk/Cancel，也不是 OK/Cancel
    )
    record(
        "Qt 自带文案中文化：输入框右键菜单 + 标准对话框按钮",
        ok,
        f"右键菜单={plain} 标准按钮={sorted(buttons)}",
    )


def test_ai_settings_shared(window) -> None:
    """模型 / 密钥必须**两种模式都在**（训练模式同样要调用 AI）。

    曾经的缺陷：模型与密钥控件住在输出模式页里，切到训练模式时整页被
    QStackedWidget 换掉 → 用户看到的是"要选模型、要填密钥，但控件不见了"，
    而 _on_start 的训练分支**照样**要求 adapter，等于逼用户自己去猜
    "是不是得先切回输出模式配一次"。
    """
    shared = ("cmb_model", "edit_key", "btn_save_key", "lbl_key_status")

    # 用 isVisibleTo(window) 而不是 isVisible()/isHidden()，两个都不能用：
    #   1) 本套自检**故意不调用 window.show()**（避免在有显示器时弹出窗口），
    #      而 Qt 的 isVisible() 要求控件与它的所有祖辈都"可显示"，所以窗口没 show
    #      时它对所有子控件都返回 False —— 断言会假失败；
    #   2) isHidden() 只看控件自己有没有被隐藏，而 QStackedWidget 隐藏的是
    #      **页本身**（它的直接子控件），页里的孙控件并没有被显式隐藏 ——
    #      所以 isHidden() 会误判"被切走那一页里的控件仍然可见"。
    #   isVisibleTo(祖先) 正好两者兼顾：不要求窗口已显示，但会把祖辈的隐藏算进来。
    def usable(name: str) -> bool:
        widget = getattr(window, name)
        return widget.isVisibleTo(window) and widget.isEnabled()

    try:
        seen: dict[bool, dict[str, bool]] = {}
        for training in (True, False):
            if training:
                window.rb_train.setChecked(True)
            else:
                window.rb_output.setChecked(True)
            seen[training] = {name: usable(name) for name in shared}

        # 各页专属的控件仍然只在各自页上（没把两页搞成一样）
        window.rb_train.setChecked(True)
        train_only = (
            window.edit_style_name.isVisibleTo(window)
            and not window.txt_prompt.isVisibleTo(window)
        )
        window.rb_output.setChecked(True)
        output_only = (
            window.cmb_style.isVisibleTo(window)
            and window.txt_prompt.isVisibleTo(window)
        )

        ok = (
            all(seen[True].values())
            and all(seen[False].values())
            and train_only
            and output_only
        )
        record(
            "模式切换：模型/密钥两种模式都在（训练模式同样要调用 AI）",
            ok,
            f"训练={seen[True]} 输出={seen[False]} "
            f"训练页专属={train_only} 输出页专属={output_only}",
        )
    except Exception as exc:
        record("模式切换：模型/密钥两种模式都在", False, str(exc))


def test_menu_layout(window) -> None:
    """菜单结构：六组，从左到右为 文件 / 外观 / 安全 / 高级设置 / 日志 / 帮助。

    这是用户 2026-09-24 明确列出的分组（v1.0.x 曾把"运维型"动作全塞进「工具」，
    结果"打开输出目录"与"删除已保存的密钥"挤在同一个菜单里）。三条不变量：
      · 分组与**顺序**就是用户给的那六项（顺序会直接影响肌肉记忆，不能随手挪）；
      · 「打开脚本目录」**不在任何菜单里** —— 它只对"自动导出失败、需要手动补跑"
        有意义，那个场景由完成对话框当场给按钮；
      · 仍然**不提供「打开配置文件夹」** —— 里面是 models.yaml，误触改坏会导致下次启动报错。
    """
    from acb.paths import styles_dir

    opened: list[Path] = []
    real = window._open_path
    try:
        window._open_path = lambda p: opened.append(p)  # type: ignore[assignment]
        window.btn_open_styles.click()
    except Exception as exc:
        record("菜单结构（六组，用户指定的顺序）", False, str(exc))
        return
    finally:
        window._open_path = real

    expected = ["文件", "外观", "安全", "高级设置", "日志", "帮助"]
    menus = [a.text() for a in window.menuBar().actions()]
    by_menu: dict[str, list[str]] = {}
    for action in window.menuBar().actions():
        sub = action.menu()
        if sub is not None:
            by_menu[action.text()] = [a.text() for a in sub.actions() if a.text()]
    all_items = [item for items in by_menu.values() for item in items]

    want = {
        "文件": ["打开输出目录", "打开风格文件夹"],
        "外观": ["浅色模式", "深色模式"],
        "安全": ["删除已保存的密钥…"],
        "高级设置": ["打开高级设置…", "恢复出厂设置…", "重新自检"],
        "日志": ["查看运行日志…", "打开日志目录", "清除失败记录", "清除缩略图缓存"],
        "帮助": ["关于"],
    }
    mismatch = {k: by_menu.get(k) for k, v in want.items() if by_menu.get(k) != v}
    no_script_entry = not any("脚本目录" in item for item in all_items)

    ok = (
        menus == expected
        and not mismatch
        and opened == [Path(styles_dir())]
        and no_script_entry
        and "打开配置文件夹" not in all_items
    )
    record(
        "菜单结构：文件/外观/安全/高级设置/日志/帮助（六组，顺序与内容按用户要求）",
        ok,
        f"菜单={menus} 不一致={mismatch or '无'} 打开={[str(p) for p in opened]} "
        f"含脚本目录入口={not no_script_entry}",
    )


def test_factory_settings_reset(window) -> None:
    """恢复出厂设置：**两次确认，任一次取消都不许改动**（用户 2026-09-24 明确要求）。

    这是全程序唯一会一次性丢掉多项设置的动作，而且藏在「高级设置」里，
    误触代价与隔壁的"清除缩略图缓存"完全不是一个量级 —— 所以三件事都要验：
      · 警示文案必须把"会重置什么"（连接/密钥/隐藏模型/偏好/失败记录/缓存）
        与"绝不动什么（风格文件）"都写出来；
      · 第一步取消、或第二步取消 → 偏好、连接文件、密钥、失败记录、缓存**全原样**；
      · 两次都确认 → 上面六项一起重置（密钥真的从凭据管理器里删了）。
    真弹框在 offscreen 下会永久阻塞，所以两个确认步骤各自是一个方法，这里直接打桩。
    """
    from acb import preferences as P
    from acb.cache import ThumbCache
    from acb.config import connections_path, load_models_config
    from acb.pipeline.job import ScanItem

    snapshot = dict(P.load_preferences())
    was_dark = window._dark
    orig_first = window._confirm_factory_settings_warning
    orig_second = window._confirm_factory_settings_again
    conn_file = connections_path()
    old_conn = conn_file.read_bytes() if conn_file.is_file() else None

    victim = "openai::gpt-4o"
    # 拿一条真实连接的密钥槽做实验（密钥后端已在 main() 里换成内存假实现，碰不到真凭据）
    account = next(iter(window._models_config.connections.values())).key_env
    cache = ThumbCache()
    dummy = cache.thumb_path("_gui_smoke_reset_probe")
    failure_keys_before = len(window._job_state.failure_summary())
    try:
        # 造出"被改过"的状态：自定义连接文件 + 隐藏模型 + 主题/尾缀/置顶
        # + 已保存的密钥 + 一条失败记录 + 一个缓存文件
        conn_file.parent.mkdir(parents=True, exist_ok=True)
        conn_file.write_text("connections: {}\n", encoding="utf-8")
        P.update_preferences(
            theme="dark",
            export_suffix="_tmp_reset",
            hidden_models=[victim],
            pinned_styles=["_tmp_reset_style"],
        )
        window._prefs = P.load_preferences()
        window._key_store.set(account, "sk-gui-smoke-reset")
        window._job_state.mark_failed(
            ScanItem(path=Path("reset_probe.CR3"), key="gui_smoke_reset_probe",
                     size=1, mtime_ns=1),
            "自检造的失败记录",
        )
        dummy.write_bytes(b"not-a-real-jpeg")
        key_saved_before = bool(window._key_store.sources(account)["keyring"])
        files_before = cache.stats()[0]

        # 0) 警示文案：这时候六项都"有东西"，必须逐项点名 + 明确"绝不动什么"
        text = window._factory_settings_warning_text(window._factory_reset_plan())
        text_ok = (
            "已保存的 API 密钥" in text
            and "失败记录" in text
            and "缩略图缓存" in text
            and "被移除的模型" in text
            and "界面偏好" in text
            and "不会动的东西" in text
            and "styles/" in text
            and "**" not in text          # QMessageBox 是纯文本，星号会原样画出来
        )

        def untouched() -> bool:
            return (
                conn_file.is_file()
                and P.get_str("theme") == "dark"
                and P.get_str("export_suffix") == "_tmp_reset"
                and P.get_list("hidden_models") == [victim]
                and P.get_list("pinned_styles") == ["_tmp_reset_style"]
                and bool(window._key_store.sources(account)["keyring"])
                and window._job_state.failure_summary()
                and dummy.is_file()
            )

        # 1) 第一步（警示框）就取消
        window._confirm_factory_settings_warning = lambda *a, **k: False
        window._confirm_factory_settings_again = lambda: True
        window._on_restore_factory_settings()
        first_cancel_ok = untouched()

        # 2) 第一步同意、第二步（再次确认）取消 —— 这正是"二次确认"存在的意义
        window._confirm_factory_settings_warning = lambda *a, **k: True
        window._confirm_factory_settings_again = lambda: False
        window._on_restore_factory_settings()
        second_cancel_ok = untouched()

        # 3) 两次都确认 → 六项一起重置
        window._confirm_factory_settings_again = lambda: True
        window._on_restore_factory_settings()
        after = window._key_store.sources(account)
        reset_ok = (
            not conn_file.is_file()                                    # 连接回到出厂
            and not (after["memory"] or after["keyring"])               # 密钥真的删了
            and not window._job_state.failure_summary()                 # 失败记录清了
            and not dummy.is_file()                                     # 缓存清了
            and P.get_list("hidden_models") == []                       # 隐藏模型放回
            and P.get_list("pinned_styles") == []                       # 置顶清掉
            and P.get_str("theme") == "light"                           # 主题回浅色
            and P.get_str("export_suffix") == ""
            and window.cmb_model.findData(victim) >= 0                  # 真的回到下拉里了
            and window._prefs.get("theme") == "light"
            and window._dark is False
            and window._act_light.isChecked()
        )

        # 4) 文案必须跟着**真实状态**走：重置后没有密钥/失败记录/隐藏模型了，
        #    那几行就不该再出现（否则用户会以为"点一下还会再删一遍什么"）；
        #    而只剩缓存文件时，缓存那一条要如实出现。
        text_after = window._factory_settings_warning_text(window._factory_reset_plan())
        dummy.write_bytes(b"x")
        text_cache_only = window._factory_settings_warning_text(window._factory_reset_plan())
        text_follows_state = (
            "已保存的 API 密钥" not in text_after
            and "失败记录" not in text_after
            and "缩略图缓存" in text_cache_only
            and "已保存的 API 密钥" not in text_cache_only
            and "不会动的东西" in text_after
        )

        record(
            "恢复出厂设置：两次确认、任一次取消都不改动；确认后连接/密钥/失败记录/缓存/隐藏模型/偏好一起重置",
            first_cancel_ok and second_cancel_ok and reset_ok and text_ok and key_saved_before
            and files_before >= 1 and text_follows_state,
            f"第一次取消后原样={first_cancel_ok} 第二次取消后原样={second_cancel_ok} "
            f"确认后已重置={reset_ok}（含密钥/失败记录/缓存）警示文案完整={text_ok} "
            f"文案随真实状态变化={text_follows_state}",
        )
    except Exception as exc:
        record("恢复出厂设置：两次确认与重置效果", False, f"{type(exc).__name__}: {exc}")
    finally:
        window._confirm_factory_settings_warning = orig_first
        window._confirm_factory_settings_again = orig_second
        # 把"被改过的状态"还原：后面的用例依赖真实的偏好 / 连接文件
        P.save_preferences(snapshot)
        window._prefs = P.load_preferences()
        if old_conn is None:
            conn_file.unlink(missing_ok=True)
        else:
            conn_file.write_bytes(old_conn)
        dummy.unlink(missing_ok=True)
        window._job_state.clear_failures()
        window._models_config = load_models_config()
        window._populate_models()
        window._reload_styles()
        window._apply_preferences_to_widgets()
        window._apply_theme(dark=was_dark)


def test_progress_bounds(window) -> None:
    """进度条边界：total=0 与 done>total 都不能崩、不能出现负数。"""
    try:
        window._on_progress(0, 0)
        zero_ok = window.progress.value() == 0
        window._on_progress(5, 3)      # done > total（去重后可能发生）
        over_value = window.progress.value()
        window._on_progress(-1, 10)
        neg_value = window.progress.value()
        record(
            "进度条边界（total=0 / done>total / done<0）",
            zero_ok and over_value == 100 and neg_value >= 0,
            f"0/0={0 if zero_ok else '异常'} done>total→{over_value} done<0→{neg_value}",
        )
    except Exception as exc:
        record("进度条边界", False, str(exc))


def test_collect_options(window) -> None:
    """参数收集：输出模式要能组装出合法的选项对象。"""
    try:
        from acb.constants import PS_JPEG_QUALITY_MAX, PS_JPEG_QUALITY_MIN

        opts = window._collect_output_options(resume=True, only_failed=False)
        ok = (
            opts.resume is True
            and opts.only_failed is False
            and opts.workers >= 1
            and opts.color_space in ("sRGB", "Adobe RGB(1998)", "Display P3")
            # 界面滑块的值必须**原样**进入输出选项，且落在 0–12 内。
            # 只检查范围是不够的：范围对了但取值没接上滑块（比如永远取默认值）
            # 同样是个真 bug，所以额外要求它与控件当前值一致。
            and PS_JPEG_QUALITY_MIN <= opts.ps_quality <= PS_JPEG_QUALITY_MAX
            and opts.ps_quality == window.slider_quality.value()
        )
        record("_collect_output_options 组装输出选项", ok,
               f"质量={opts.ps_quality} 并发={opts.workers}")

        # --- 出片目录规则：默认同层；勾上子目录后必须**忽略**输入框 ---------
        # 真实反馈：默认塞进 _export 子目录让用户找不到图，所以默认改成同层。
        # 另一半守住"控件灰了就不能再用它的值"这条一致性：
        # 只置灰输入框却仍读它的文字，会让勾选看起来生效、实际被旧路径覆盖。
        from pathlib import Path as _Path

        window.chk_export_subdir.setChecked(False)
        window.picker_output.edit.setText("")
        base = window._collect_output_options(resume=False, only_failed=False)
        window.picker_output.edit.setText(r"D:\自定义输出")
        window.chk_export_subdir.setChecked(True)
        subdir_opt = window._collect_output_options(resume=False, only_failed=False)
        window.chk_export_subdir.setChecked(False)
        custom_opt = window._collect_output_options(resume=False, only_failed=False)
        window.picker_output.edit.setText("")

        dirs_ok = (
            base.output_to_source_dir is True
            and base.output_dir is None
            and subdir_opt.output_to_source_dir is False
            # 输入框里仍有残留路径，也必须被忽略（否则勾选形同虚设）
            and subdir_opt.output_dir is None
            and custom_opt.output_to_source_dir is True
            and custom_opt.output_dir == _Path(r"D:\自定义输出")
        )
        record(
            "出片目录规则：默认同层 / 勾选子目录后忽略输入框 / 未勾选时输入框优先",
            dirs_ok,
            f"默认同层={base.output_to_source_dir} 勾选后={subdir_opt.output_to_source_dir} "
            f"勾选后目录={subdir_opt.output_dir} 显式={custom_opt.output_dir}",
        )
    except Exception as exc:
        record("_collect_output_options 组装输出选项", False, str(exc))


def test_start_vs_resume(window) -> None:
    """回归测试：「开始」必须全量重跑，「续跑」才跳过已完成。

    真实事故：这两个按钮连的是**同一段** lambda（都传 resume=True），
    于是用户点「开始」时已完成的文件被静默跳过，完全无法重复处理。

    这里直接点按钮、截获 _on_start 的实参，验证三个入口确实各不相同。
    只做参数校验，不真正启动任务（所以不会碰网络或 Photoshop）。
    """
    calls: list[tuple[bool, bool]] = []
    real = window._on_start

    def spy(*, resume: bool, only_failed: bool) -> None:
        calls.append((resume, only_failed))

    try:
        window._on_start = spy
        window.btn_start.click()
        window.btn_resume.click()
        window.btn_only_failed.click()
    finally:
        window._on_start = real

    ok = (
        len(calls) == 3
        and calls[0] == (False, False)   # 开始 = 全量重跑
        and calls[1] == (True, False)    # 续跑 = 跳过已完成
        and calls[2] == (True, True)     # 只跑失败
    )
    record(
        "按钮语义：开始=全量重跑 / 续跑=跳过已完成 / 只跑失败",
        ok,
        f"开始→{calls[0] if calls else '未触发'} "
        f"续跑→{calls[1] if len(calls) > 1 else '未触发'} "
        f"只跑失败→{calls[2] if len(calls) > 2 else '未触发'}",
    )


def test_cleanup_on_exit(window) -> None:
    """退出清理的接线不能出错。

    关闭流程里抛异常会让窗口关不掉（或者更糟：进程卡住不退）。
    这里用 ACB_NO_CLEANUP=1 让它变成空操作 ——
    自检脚本**不应该有"删掉用户真实缓存"这种副作用**。
    （真正的删除逻辑由 smoke_test 的「退出清理」在隔离的临时数据目录里验证。）
    """
    import os

    os.environ["ACB_NO_CLEANUP"] = "1"
    try:
        window._cleanup_on_exit()
        # 带跳过原因时也必须安全返回（强制关闭走的就是这条分支）。
        window._cleanup_on_exit(skip_reason="测试用跳过原因")
    except Exception as exc:
        record("退出清理接线（关闭时调用不抛异常）", False, f"{type(exc).__name__}: {exc}")
        return
    finally:
        os.environ.pop("ACB_NO_CLEANUP", None)

    record("退出清理接线（关闭时调用不抛异常）", True, "已设 ACB_NO_CLEANUP=1 跳过实际删除")

    # 没跑过任务之前，脚本目录不存在，「打开脚本目录」必须是禁用的 ——
    # 留一个点了没反应的按钮比没有按钮更让人困惑。
    # （_last_script_dir 由 _on_output_finished 在任务结束后写入。）
    record(
        "「打开脚本目录」初始禁用（尚未产生脚本）",
        not window.btn_open_script.isEnabled(),
        f"enabled={window.btn_open_script.isEnabled()}",
    )


def test_open_script(window) -> None:
    """「打开脚本目录」必须安全：目录没了就只提示，不能悄悄打开一个空目录。

    脚本目录在退出时会被清理，而用户很可能在关闭之后才想起来"哦我要手动补跑"。
    这时按钮如果还是无脑 startfile，他就会盯着一个空文件夹发呆，
    完全不知道脚本去哪了 —— 所以这里必须走"提示 + 不打开"这条分支。
    """
    import shutil
    import tempfile
    from pathlib import Path as _Path

    opened: list = []
    real = window._open_path
    tmp_root = _Path(tempfile.mkdtemp(prefix="acb_gui_script_"))
    gone = tmp_root / "already_cleaned"
    try:
        window._open_path = lambda p: opened.append(p)  # type: ignore[assignment]

        # 1) 还没跑过任何任务 → 直接返回
        window._last_script_dir = None
        window._on_open_script()

        # 2) 目录已被退出清理删掉 → 只提示，不打开
        shutil.rmtree(gone, ignore_errors=True)
        window._last_script_dir = gone
        window._on_open_script()

        # 3) 目录存在 → 正常打开
        window._last_script_dir = tmp_root
        window._on_open_script()
    except Exception as exc:
        record("「打开脚本目录」：目录不存在时只提示、不打开", False,
               f"{type(exc).__name__}: {exc}")
        return
    finally:
        window._open_path = real  # type: ignore[assignment]
        window._last_script_dir = None
        shutil.rmtree(tmp_root, ignore_errors=True)

    ok = opened == [tmp_root]
    record(
        "「打开脚本目录」：目录不存在时只提示、不打开",
        ok,
        f"实际打开={opened}（期望只打开 {tmp_root}）",
    )


def test_clear_files(window) -> None:
    try:
        window._on_clear_files()
        record("_on_clear_files 清空列表", window.table.rowCount() == 0,
               f"剩余 {window.table.rowCount()} 行")
    except Exception as exc:
        record("_on_clear_files 清空列表", False, str(exc))


def test_quality_warning(window) -> None:
    """回归测试：极低画质必须标红告警，且拖回高值后必须彻底恢复。

    盯住两个真实的风险点：
      1. 标红用的是内联 setStyleSheet。如果回到高值时忘了清空，标签会一直红着——
         用户会以为高档位有问题，实际只是样式粘住了控件。
      2. 阈值判定差一错误：要么漏警告（静默放行，正是本次要防的），
         要么把正常档位也标红（狼来了，用户开始无视警告）。
    """
    from acb.constants import (
        DEFAULT_PS_JPEG_QUALITY,
        PS_JPEG_QUALITY_MAX,
        PS_JPEG_QUALITY_MIN,
        PS_JPEG_QUALITY_WARN_MAX,
        is_very_low_quality,
    )

    slider = window.slider_quality
    shown = False

    try:
        # ⚠ 必须先 show() 让布局真正算一遍：不 show 时控件几何是"没算过"的状态，
        # 量出来的可见宽度会明显偏大（实测 640 vs 真实 184），
        # 于是这条守卫会变成空转 —— 布局真被挤坏它也照样通过。
        # offscreen 下 show() 不会真的弹窗，测完 hide() 收回去。
        window.show()
        QApplication.processEvents()
        shown = True

        # 最低档：必须显示警示，并且带悬停说明（一行放不下全文）
        slider.setValue(PS_JPEG_QUALITY_MIN)
        low_ok = slider.is_warning_shown() and bool(slider.caption_tooltip())
        low_text = slider.caption_text()

        # 阈值 + 1：必须没有任何警示残留（验证清空路径），
        # 但**悬停说明要保留完整版**：主文案为了放得下已经把括号里的内容去掉了
        # （含 libjpeg 等效值），那部分信息不能就这么丢掉。
        slider.setValue(PS_JPEG_QUALITY_WARN_MAX + 1)
        normal_tip = slider.caption_tooltip()
        just_above_ok = (
            not slider.is_warning_shown() and "libjpeg" in normal_tip
        )

        # 最高档：同样必须干净
        slider.setValue(PS_JPEG_QUALITY_MAX)
        high_ok = not slider.is_warning_shown() and "libjpeg" in slider.caption_tooltip()

        # 每一档的说明文字都必须**真的完整显示**。
        # 这是用户反馈过的问题（"输出质量滑块在 12 时显示不完整"）：
        # 12 恰好是默认档，一打开程序就能碰到。
        clipped: list[int] = []
        visible_width = 0
        for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX + 1):
            slider.setValue(value)
            QApplication.processEvents()
            visible_width = slider.caption_visible_width()
            if not slider.caption_fits():
                clipped.append(value)
        # 顺带确认"测量工具本身"没失效：可见宽度不能是 0，
        # 否则说明布局还没算出来，上面那条断言就成了空转。
        fitted_ok = not clipped and visible_width > 0

        # 阈值边界：阈值本身要警告，阈值 + 1 不能警告
        edge_ok = (
            is_very_low_quality(PS_JPEG_QUALITY_WARN_MAX)
            and not is_very_low_quality(PS_JPEG_QUALITY_WARN_MAX + 1)
        )

        record(
            "质量滑块：极低画质标红与恢复 + 每一档说明文字都完整显示",
            low_ok and just_above_ok and high_ok and edge_ok and fitted_ok,
            f"{PS_JPEG_QUALITY_MIN} 档警示={low_ok}（文字「{low_text}」）"
            f"{PS_JPEG_QUALITY_WARN_MAX + 1} 档已清除={just_above_ok} "
            f"{PS_JPEG_QUALITY_MAX} 档已清除={high_ok} 阈值边界={edge_ok} "
            f"可见宽={visible_width} 被截断的档位={clipped or '无'}",
        )
    except Exception as exc:
        record("质量滑块：极低画质标红与恢复", False, str(exc))
    finally:
        if shown:
            window.hide()
            QApplication.processEvents()
        # 还原默认值，避免影响后续用例（例如 _collect_output_options 读滑块值）
        slider.setValue(DEFAULT_PS_JPEG_QUALITY)


def test_low_quality_confirm(window) -> None:
    """回归测试：极低画质确认框必须真的能拦住，而且默认按钮是「返回」。

    为什么必须测：这个确认框是"不许静默放行"的**唯一实际执行点**。
    如果它永远返回 True（例如 clickedButton() 的比较写错、或者把
    `is` 写成 `==`），那么滑块标红就只是个装饰——用户以为被拦住了，其实没有。

    三个场景都要覆盖，特别是场景 C：
      A. 点「返回修改质量」→ 必须返回 False（取消本次导出）
      B. 点「仍然使用（继续）」→ 必须返回 True
      C. 直接关窗（clickedButton 为 None）→ **必须按拒绝处理**。
         否则用户手滑关掉窗口就等于静默放行了，正好漏掉这次要防的情况。
    """
    from PyQt6.QtWidgets import QMessageBox

    real_exec = QMessageBox.exec
    real_clicked = QMessageBox.clickedButton
    seen: dict[str, object] = {}

    def fake_exec(self) -> int:
        # 不真弹窗（offscreen 下弹了会阻塞），只把关键文案记下来供断言。
        seen["buttons"] = [b.text() for b in self.buttons()]
        seen["title"] = self.windowTitle()
        seen["text"] = self.text()
        default_button = self.defaultButton()
        seen["default"] = default_button.text() if default_button else ""
        return 0

    def clicked_at(index: int | None):
        def inner(self):
            buttons = self.buttons()
            if index is None or not (0 <= index < len(buttons)):
                return None
            return buttons[index]
        return inner

    try:
        QMessageBox.exec = fake_exec

        QMessageBox.clickedButton = clicked_at(1)          # A: 「返回修改质量」
        declined = window._confirm_low_quality(0) is False

        QMessageBox.clickedButton = clicked_at(0)          # B: 「仍然使用（继续）」
        accepted = window._confirm_low_quality(0) is True

        QMessageBox.clickedButton = clicked_at(None)       # C: 直接关窗
        closed_safely = window._confirm_low_quality(0) is False

        buttons = seen.get("buttons") or []
        # 按钮必须是中文：Qt 自带的 Yes/No 在没加载中文翻译时会显示英文，
        # 与全中文界面不一致。
        chinese_buttons = len(buttons) == 2 and all(
            any("\u4e00" <= ch <= "\u9fff" for ch in text) for text in buttons
        )
        # 默认按钮必须是「返回修改质量」：用户直接回车不会误放行。
        default_is_reject = seen.get("default") == "返回修改质量"
        # 文案必须说明"这个取值本身是合法的"，否则用户会以为程序在报错。
        text_is_honest = "合法" in str(seen.get("text", ""))

        record(
            "极低画质确认框：拦得住、默认不误放行",
            declined and accepted and closed_safely
            and chinese_buttons and default_is_reject and text_is_honest,
            f"拒绝={declined} 继续={accepted} 关窗按拒绝={closed_safely} "
            f"中文按钮={chinese_buttons} 默认=「{seen.get('default')}」 "
            f"文案说明合法={text_is_honest} 标题=「{seen.get('title')}」",
        )
    except Exception as exc:
        record("极低画质确认框：拦得住、默认不误放行", False, str(exc))
    finally:
        QMessageBox.exec = real_exec
        QMessageBox.clickedButton = real_clicked


def test_font_helper() -> None:
    """回归测试：heading_font() 不得触发 QFont 的 setPointSize 警告。

    原始 bug：`QFont()` 默认构造对象没有字号，`pointSize()` 返回 -1，
    于是 `setPointSize(-1 + 1)` = `setPointSize(0)`，Qt 打印
    "QFont::setPointSize: Point size <= 0 (-1), must be greater than 0"。
    """
    from acb.ui.widgets import heading_font

    baseline = len(_qt_messages)
    try:
        font = heading_font()
    except Exception as exc:
        record("heading_font() 不触发 QFont setPointSize 警告", False, str(exc))
        return

    new_messages = [m for _k, m in _qt_messages[baseline:]]
    font_warnings = [m for m in new_messages if "setPointSize" in m or "Point size" in m]
    record(
        "heading_font() 不触发 QFont setPointSize 警告",
        not font_warnings and font.bold(),
        f"字号={font.pointSize()} 粗体={font.bold()} 新警告={font_warnings}",
    )


def test_qt_argv_hygiene() -> None:
    """交给 Qt 的 argv 必须把**我们自己的** `--style` 摘掉。

    原始现象（实测）：GUI 模式下 `python app.py --style AI自主决策` 会在控制台打出
    `QApplication: invalid style override 'AI自主决策' passed, ignoring it.`
    —— `-style` / `--style` 是 **Qt 自己的**选项（它比我们更早解析），
    而 `--style` 同时是我们的风格名选项，于是风格名被当成 Qt 主题名。
    用户看到的是"程序对我的参数报了一句看不懂的警告"。

    这里同时钉住两件事：我们的 `--style` 被摘掉、Qt 自己的 `-style Fusion`
    与 `-platform offscreen` **照旧通得过**（摘多了就是把别人的功能删了）。
    """
    from acb.ui.main_window import _qt_argv

    cases = [
        (["app.py", "--style", "AI自主决策"], ["app.py"]),
        (["app.py", "--style=AI自主决策"], ["app.py"]),
        (["app.py", "--style", "x", "--no-gui"], ["app.py", "--no-gui"]),
        (["app.py", "--no-gui", "--style", "x"], ["app.py", "--no-gui"]),
        # Qt 自己的选项必须原样保留
        (["app.py", "-style", "Fusion"], ["app.py", "-style", "Fusion"]),
        (["app.py", "-platform", "offscreen"], ["app.py", "-platform", "offscreen"]),
        (["app.py"], ["app.py"]),
        ([], []),
    ]
    bad = [(a, _qt_argv(a), want) for a, want in cases if _qt_argv(a) != want]
    record(
        "交给 Qt 的 argv：摘掉我们的 --style，保留 Qt 自己的选项",
        not bad,
        f"用例={len(cases)} 条；不符预期={bad if bad else '无'}",
    )


def test_exception_guard() -> None:
    """异常守卫：槽内异常必须"不导致进程退出"，而不是 qFatal 闪退。

    做法：临时把 QMessageBox.exec 换成空实现（避免弹窗阻塞自动化），
    然后让一个真实的 Qt 槽抛异常，观察是否被守卫接住。
    """
    from PyQt6.QtWidgets import QMessageBox, QPushButton

    from acb.ui import main_window as mw

    if not hasattr(mw, "_install_exception_guard"):
        record("异常守卫", False, "main_window 中没有 _install_exception_guard")
        return

    calls: list[tuple[str, str]] = []
    original_exec = QMessageBox.exec

    def fake_exec(self):
        calls.append((self.windowTitle(), self.text()))
        return 0

    QMessageBox.exec = fake_exec  # type: ignore[assignment]

    try:
        mw._install_exception_guard(None)  # type: ignore[arg-type]

        button = QPushButton()

        def boom() -> None:
            raise RuntimeError("模拟槽函数内的编程错误")

        button.clicked.connect(boom)
        try:
            button.click()
        except Exception as exc:
            record("异常守卫拦截槽内异常", False, f"异常逸出到调用方：{exc}")
            return
    finally:
        QMessageBox.exec = original_exec  # type: ignore[assignment]

    hooked = sys.excepthook is not sys.__excepthook__
    record(
        "异常守卫：槽内异常不导致进程退出，且弹窗被调用",
        bool(calls) and hooked,
        f"弹窗调用 {len(calls)} 次；sys.excepthook 已替换={hooked}",
    )


def test_qt_messages() -> None:
    """汇总 Qt 消息：分三类处理。

      - critical / fatal          → 一律失败（进程级问题）
      - warning（非 benign/环境）  → 一律失败。很可能是我们的代码写错了，
                                     例如 `QFont::setPointSize: Point size <= 0`
                                     那条；这类消息不会出现在 Python traceback
                                     里，只能靠 qInstallMessageHandler 捕获。
      - 环境相关 warning          → 显示但不失败（offscreen 下 Qt wheel
                                     不自带字体目录）。分类而不是屏蔽：
                                     它仍然会打印出来，只是不判为缺陷。
    """
    failed_messages = [
        (kind, msg)
        for kind, msg in _qt_messages
        if kind in ("warning", "critical", "fatal")
        and not _is_benign(msg)
        and not _is_environmental(msg)
    ]
    environmental = [
        (kind, msg)
        for kind, msg in _qt_messages
        if kind in ("warning", "critical", "fatal") and _is_environmental(msg)
    ]

    if failed_messages:
        lines = [f"[{kind}] {msg}" for kind, msg in failed_messages[:12]]
        record(
            f"Qt 消息检查（发现 {len(failed_messages)} 条非预期警告/错误）",
            False,
            "\n        ".join(lines),
        )
        return

    detail = f"共 {len(_qt_messages)} 条消息，其中 {len(environmental)} 条为环境相关（不计为缺陷）"
    for _kind, msg in environmental[:3]:
        detail += "\n        [环境] " + msg.splitlines()[0]
    record("Qt 消息检查（无未预期的警告/错误）", True, detail)

class _InMemoryKeyring:
    """内存版 keyring 后端：自检期间顶替真实的 Windows 凭据管理器。

    只实现 KeyStore 用到的三个方法（get/set/delete_password），语义与真实后端一致：
    条目不存在时 `delete_password` 抛异常（真实 keyring 就是这个行为，
    KeyStore.delete 里专门为此把异常降级成 DEBUG 日志）。
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise KeyError(f"no such entry: {service}/{account}")
        del self.store[(service, account)]


class _StubDialogs:
    """上下文管理器：把 QMessageBox 的静态方法换成"立即返回"的桩。

    为什么必须这么做：本套自检跑在 offscreen 平台下，``QMessageBox.information()``
    会**真的建一个模态对话框并阻塞等待点击** —— 在无人值守的自检里就是永久挂死。
    （本项目真踩到了：新加的"未选中任何行就给提示"这条路径把整个自检卡死。）

    用法::

        with _StubDialogs():                 # question 一律回答 Yes
            window._on_delete_model()

        with _StubDialogs(_QMB.StandardButton.No):   # 一律回答 No
            window._on_delete_model()
    """

    def __init__(self, answer=None):
        from PyQt6.QtWidgets import QMessageBox

        self._cls = QMessageBox
        self._answer = (
            answer if answer is not None else QMessageBox.StandardButton.Yes
        )
        self._saved: dict[str, object] = {}

    def __enter__(self):
        cls = self._cls
        ok = lambda *a, **k: cls.StandardButton.Ok          # noqa: E731
        answer = lambda *a, **k: self._answer               # noqa: E731
        for name, fn in (
            ("information", ok),
            ("warning", ok),
            ("critical", ok),
            ("about", ok),
            ("question", answer),
        ):
            existing = getattr(cls, name, None)
            if existing is not None:
                self._saved[name] = existing
            setattr(cls, name, staticmethod(fn))
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(self._cls, name, fn)
        return False


def test_format_and_suffix(window) -> None:
    """导出格式与文件名尾缀：控件、联动、以及"写进磁盘"。"""
    from acb import preferences as P
    from acb.constants import EXPORT_FORMATS

    try:
        fmt_items = [
            window.cmb_format.itemData(i) for i in range(window.cmb_format.count())
        ]
        formats_ok = fmt_items == list(EXPORT_FORMATS)
        # 显示文本必须**就是格式代号**：曾经写过"JPG（体积小，最通用）"，
        # 但下拉宽度有限，说明会被省略号截掉，反而看不清选的是哪个。
        # 两种格式的区别写在 tooltip 里。
        labels_ok = [
            window.cmb_format.itemText(i) for i in range(window.cmb_format.count())
        ] == list(EXPORT_FORMATS)

        # 切到 PNG：画质滑块必须灰掉 —— PNG 是无损的，画质对它没有意义。
        # 灰掉而不是藏起来，是为了不改变底栏宽度（否则左右两栏比例会跳）。
        window.cmb_format.setCurrentIndex(window.cmb_format.findData("PNG"))
        lossless_disabled = not window.slider_quality.isEnabled()
        lossless_opts = window._collect_output_options(resume=False, only_failed=False)

        # 切回 JPG：滑块必须恢复可用
        window.cmb_format.setCurrentIndex(window.cmb_format.findData("JPG"))
        jpg_enabled = window.slider_quality.isEnabled()
        jpg_opts = window._collect_output_options(resume=False, only_failed=False)

        # 尾缀：留空 → 默认 -1；填 edit → -edit，且标签回显实际使用的尾缀
        window.edit_suffix.setText("")
        empty_label = window.lbl_suffix.text()
        empty_opts = window._collect_output_options(resume=False, only_failed=False)
        window.edit_suffix.setText("edit")
        edit_label = window.lbl_suffix.text()
        edit_opts = window._collect_output_options(resume=False, only_failed=False)

        saved = P.load_preferences()

        ok = (
            formats_ok
            and labels_ok
            and lossless_disabled
            and lossless_opts.export_format == "PNG"
            and jpg_enabled
            and jpg_opts.export_format == "JPG"
            and empty_label.endswith("-1")
            and empty_opts.export_suffix == ""
            and edit_label.endswith("-edit")
            and edit_opts.export_suffix == "edit"
            # 用户要求：下次打开要记得，所以必须真的落盘（不能只存在内存里）
            and saved.get("export_suffix") == "edit"
            and saved.get("export_format") == "JPG"
        )
        record(
            "导出格式与尾缀：格式下拉、画质联动、偏好落盘",
            ok,
            f"格式项={fmt_items} 下拉文本只有代号={labels_ok} "
            f"无损格式时画质禁用={lossless_disabled} "
            f"空尾缀标签={empty_label!r} edit标签={edit_label!r} 落盘={saved}",
        )
    except Exception as exc:
        record("导出格式与尾缀：三格式下拉、画质联动、偏好落盘", False, str(exc))


def test_preferences_restart(window) -> None:
    """重启要能带出上次的尾缀/格式（用户明确要求）。

    这里验的是"磁盘 → 控件"这一半：把偏好改成别的值，再走一次回填路径，
    控件就该跟着变。与 test_format_and_suffix 的"控件 → 磁盘"合起来覆盖往返。

    （不真的再构造一个 MainWindow：那会二次调用 setup_logging 并把第一个窗口的
      日志桥顶掉，属于测试自身制造的副作用。）
    """
    from acb import preferences as P

    try:
        P.update_preferences(export_suffix="keeptest", export_format="PNG")
        window._prefs = P.load_preferences()
        window._apply_preferences_to_widgets()

        ok = (
            window.edit_suffix.text() == "keeptest"
            and window._current_export_format() == "PNG"
            and "keeptest" in window.lbl_suffix.text()
            and not window.slider_quality.isEnabled()   # PNG 同样无损
        )

        # 升级场景：上个版本写下的格式可能已经不存在（例如 TIFF 已被移除）。
        # 不能因此让下拉框变成"无选中"—— 那种状态会让后续取值逻辑拿到空值。
        # 正确行为是安静地回退到 JPG，并留下日志。
        P.update_preferences(export_format="TIFF")
        window._prefs = P.load_preferences()
        window._apply_preferences_to_widgets()
        stale_ok = (
            window._current_export_format() == "JPG"
            and window.cmb_format.currentIndex() >= 0
            and window._collect_output_options(
                resume=False, only_failed=False
            ).export_format == "JPG"
        )

        # 复原，免得影响后面的用例
        P.update_preferences(export_suffix="", export_format="JPG")
        window._prefs = P.load_preferences()
        window._apply_preferences_to_widgets()

        record(
            "偏好持久化：重新读入后尾缀/格式被带回控件",
            ok,
            f"尾缀={window.edit_suffix.text()!r} 格式={window._current_export_format()}",
        )
        record(
            "偏好持久化：旧配置里已移除的格式（TIFF）静默回退到 JPG",
            stale_ok,
            f"回退后格式={window._current_export_format()} "
            f"下拉索引={window.cmb_format.currentIndex()}",
        )
    except Exception as exc:
        record("偏好持久化：重新读入后尾缀/格式被带回控件", False, str(exc))


def test_more_preferences_restart(window) -> None:
    """色彩空间 / 输出质量 / 风格 / 模型 也要能跨重启带出（用户要求）。

    与 test_preferences_restart 互补：那一条只管格式与尾缀。
    两个方向都要测：
        控件 → 磁盘（改动即时落盘）
        磁盘 → 控件（重启时的回填路径）
    """
    from acb import preferences as P
    from acb.constants import COLOR_SPACE_CHOICES, DEFAULT_PS_JPEG_QUALITY
    from acb.paths import styles_dir

    # 用**用户自建风格**做样本，而不是内置基准风格 default_neutral ——
    # 后者现在只在离线调试下可选（用户要求），拿它测"跨重启记不记得住"
    # 本身就不再是有效场景了。
    user_style = "_偏好测试风格_"
    style_file = styles_dir() / f"{user_style}.json"
    try:
        style_file.write_text('{"name": "' + user_style + '"}', encoding="utf-8")
        window._reload_styles()
        model_keys = [window.cmb_model.itemData(i) for i in range(window.cmb_model.count())]
        if not model_keys or window.cmb_style.findData(user_style) < 0:
            record("偏好持久化：色彩空间/质量/风格/模型", False,
                   f"前置数据不足：模型={model_keys} 风格项={user_style}")
            return
        target_model = model_keys[-1]           # 故意取最后一个，避免"碰巧是默认值"
        target_space = list(COLOR_SPACE_CHOICES)[-1]
        target_quality = 5                      # 既不是默认 12，也不算极低画质

        # ---- 方向一：控件 → 磁盘 ----
        window.cmb_color_space.setCurrentIndex(
            window.cmb_color_space.findText(target_space)
        )
        window.slider_quality.setValue(target_quality)
        window.cmb_style.setCurrentIndex(window.cmb_style.findData(user_style))
        window.cmb_model.setCurrentIndex(window.cmb_model.findData(target_model))
        saved = P.load_preferences()
        to_disk = (
            saved.get("color_space") == target_space
            and saved.get("quality") == target_quality
            and saved.get("style") == user_style
            and saved.get("model") == target_model
        )

        # ---- 方向二：磁盘 → 控件（重启时走的就是这几个调用）----
        # ⚠ 必须把选中项清成 -1，才真的相当于"刚启动"。
        # 因为 _populate_models / _reload_styles 的选中优先级是
        #   刷新前的选中项 → 记忆值 → 默认
        # 只把控件设成"默认那一项"的话，它依旧是个有效选中项，会盖过记忆值，
        # 于是"恢复成功"与"本来就没变"分不出来（这条我写过一次错的）。
        window.cmb_color_space.setCurrentIndex(0)
        window.slider_quality.setValue(DEFAULT_PS_JPEG_QUALITY)
        window.cmb_style.setCurrentIndex(-1)
        window.cmb_model.setCurrentIndex(-1)
        # 写一份"上次关闭时留下的"偏好
        P.update_preferences(color_space=target_space, quality=target_quality,
                             style=user_style, model=target_model)
        window._prefs = P.load_preferences()
        window._apply_preferences_to_widgets()
        window._populate_models()
        window._reload_styles()
        to_widgets = (
            window.cmb_color_space.currentText() == target_space
            and window.slider_quality.value() == target_quality
            and window.cmb_style.currentData() == user_style
            and window.cmb_model.currentData() == target_model
        )

        # ---- 边界：偏好里记的风格已经被删掉 → 回到「不选」占位项，而不是报错/空白 ----
        window.cmb_style.setCurrentIndex(-1)          # 同上，先变成"刚启动"
        P.update_preferences(style="_这个风格不存在_")
        window._prefs = P.load_preferences()
        window._reload_styles()
        stale_ok = (
            window.cmb_style.currentData() is None
            and window.cmb_style.currentIndex() == 0
        )

        # ---- 两个标签的新名字（用户要求改名）----
        from PyQt6.QtWidgets import QLabel

        label_texts = [lbl.text() for lbl in window.findChildren(QLabel)]
        labels_ok = (
            "输出色彩空间" in label_texts
            and "输出质量" in label_texts
            and "出片色彩" not in label_texts
            and "画质" not in label_texts
        )

        # 复原，免得影响后面的用例
        P.update_preferences(color_space="", quality=None, style="", model="")
        window._prefs = P.load_preferences()
        window._apply_preferences_to_widgets()
        window.cmb_style.setCurrentIndex(-1)
        window.cmb_model.setCurrentIndex(-1)
        window._reload_styles()
        window._populate_models()

        ok = to_disk and to_widgets and stale_ok and labels_ok
        record(
            "偏好持久化：色彩空间/输出质量/风格/模型跨重启带出（含已删焦风格回退）",
            ok,
            f"控件→磁盘={to_disk} 磁盘→控件={to_widgets} "
            f"已删焦风格回退占位项={stale_ok} 标签已改名={labels_ok}",
        )
    except Exception as exc:
        record("偏好持久化：色彩空间/质量/风格/模型", False, str(exc))


def test_remove_selected(window) -> None:
    """选错一张照片时要能在界面里移除（且**不动磁盘上的文件**）。"""
    import tempfile as _tempfile
    from pathlib import Path as _Path

    try:
        tmp = _Path(_tempfile.mkdtemp(prefix="acb_gui_rm_"))
        files = []
        for name in ("a.CR3", "b.CR3", "c.CR3"):
            p = tmp / name
            p.write_bytes(b"x")
            files.append(p)

        with _StubDialogs():
            window._on_clear_files()
            window._add_sources(files)
            table_before = window.table.rowCount()
            sources_before = len(window._sources)

            # 没选中任何行时必须给出提示、且什么都不改
            window.table.clearSelection()
            window._on_remove_selected()
            untouched = window.table.rowCount() == table_before

            # 选中第 2 行并移除
            window.table.selectRow(1)
            window._on_remove_selected()
            table_after = window.table.rowCount()

            window._on_clear_files()

        # 关键：磁盘上的文件必须一个都没少（这是"移除"不是"删除"）
        still_on_disk = all(p.is_file() for p in files)

        ok = (
            table_before == 3
            and sources_before == 3
            and untouched
            and table_after == 2
            and len(window._sources) == 0   # 上面已清空
            and still_on_disk
        )
        record(
            "移除选中：只出队不删文件、未选中时给提示",
            ok,
            f"移除前={table_before} 未选中不动={untouched} 移除后={table_after} "
            f"磁盘文件仍在={still_on_disk}",
        )
    except Exception as exc:
        record("移除选中：只出队不删文件、未选中时给提示", False, str(exc))


def test_train_page_layout_and_hint_hygiene(window) -> None:
    """训练页不能“文字和输入框挤在一起”，红字区不能当日志用。

    用户反馈（带截图，三条）：
      1. “该模型不支持 json_schema，将用 json_object + 本地强校验”是**日志**，
         不该占界面；
      2. “样本少于 10 张时会提示… ”这类**行为说明**也不该常驻界面；
      3. 训练模式的命名框只负责起个名字，不该占满一页、更不该和说明文字叠在一起。

    所以这里锁三件事：命名框是单行、训练页的可见文字不再包含那两句、
    红字区只在“模型看不到图”时出现。几何断言必须先 show()（不显示时 Qt 不排布局）。
    """
    from PyQt6.QtWidgets import QLabel as _QLabel, QLineEdit as _QLineEdit

    window.rb_train.setChecked(True)
    QApplication.processEvents()

    # 1) 命名框：单行，而且不高（以前是 120–240px 的多行框）
    box = window.edit_style_name
    single_line = isinstance(box, _QLineEdit)

    # 2) 训练页上的可见文字不许再有那两句行为说明
    page = window.option_stack.currentWidget()
    texts = [lbl.text() for lbl in page.findChildren(_QLabel)]
    joined = "\n".join(texts)
    no_warn_rule = "样本少于" not in joined and "结果可能不稳定" not in joined
    #   但“你要做什么”必须还在（三个要点：成对样本 / 起名字 / 共用密钥设置）
    keeps_essentials = (
        "成对样本" in joined and "起个名字" in joined and "模型 / 密钥" in joined
    )

    # 3) 几何：先 show() 让 Qt 真的排布局，再查同一页里有没有矩形相交
    window.show()
    QApplication.processEvents()
    rects = [
        (w, w.mapTo(page, w.rect().topLeft()), w.size())
        for w in page.findChildren(QWidget)
        if w.isVisibleTo(page) and w.width() > 1 and w.height() > 1
    ]
    overlaps = []
    for i, (wa, pa, sa) in enumerate(rects):
        rect_a = QRect(pa, sa)
        for wb, pb, sb in rects[i + 1:]:
            rect_b = QRect(pb, sb)
            inter = rect_a.intersected(rect_b)
            # 只看“真叠在一起”，相邻贴边不算（容器与子控件天然包含，跳过父子关系）
            if inter.width() > 2 and inter.height() > 2 and not (
                wa.isAncestorOf(wb) or wb.isAncestorOf(wa)
            ):
                overlaps.append(f"{type(wa).__name__}×{type(wb).__name__}")
    name_box_small = box.height() <= 48
    window.hide()

    # 4) 红字区：json_object-only 的模型不该在界面播报 json_schema
    from acb.config import ModelSpec

    fake = ModelSpec(
        label="文案检查", base_url="https://example.com/v1", model="m",
        key_env="K", provider="deepseek", supports_vision=True,
        supports_json_schema=False, supports_json_object=True, max_context=4096,
    )
    orig_spec = window._current_spec
    try:
        window._current_spec = lambda: fake
        window._on_model_changed()
        quiet = "json_schema" not in window.lbl_model_hint.text()
        # 而“看不到图”必须仍然红字（这条真的影响结果精度）
        blind = fake.model_copy(update={"supports_vision": False})
        window._current_spec = lambda: blind
        window._on_model_changed()
        vision_shown = "不支持图片输入" in window.lbl_model_hint.text()
    finally:
        window._current_spec = orig_spec
        window._on_model_changed()

    # 还原成输出模式：不给后面的用例留隐藏耦合（它们默认在输出模式里跑）。
    window.rb_output.setChecked(True)
    QApplication.processEvents()

    record(
        "训练页：命名框单行不占地、无“样本不足”行为说明、红字区不播报 json_schema",
        bool(single_line and no_warn_rule and keeps_essentials and not overlaps
             and name_box_small and quiet and vision_shown),
        f"单行输入框={single_line} 去掉了行为说明={no_warn_rule} "
        f"保留必要说明={keeps_essentials} 命名框高度={box.height()}px 矩形重叠={overlaps or '无'} "
        f"红字不提 json_schema={quiet} 看不到图仍红字={vision_shown}",
    )


def test_provider_catalog_and_key_detection(window) -> None:
    """模型目录应是真实服务商（DeepSeek/OpenAI），没有"占位"；保存密钥后识别并切换。

    用户明确要求：去掉"OpenAI 兼容（通用占位）"之类的条目；
    填入自己的 API key 后，程序应识别服务商并把「模型」切到该服务商的模型。
    """
    from acb.config import detect_provider_from_key

    detect_ok = (
        detect_provider_from_key("sk-" + "0123456789abcdef0123456789abcdef") is None
        and detect_provider_from_key("sk-ABCdef123") is None          # sk- 认不出，交回「服务商」选择
        and detect_provider_from_key("sk-wsABCDEFGHIJKLMNOP") == "qwen"    # 千问实际形态（连字符）
        and detect_provider_from_key("sk_abcDEF123") is None         # sk_ 不是千问特征（用户纠正过）
        and detect_provider_from_key("bce-v3/ALTAK-abc/def") == "qianfan"
        and detect_provider_from_key("sk-or-v1-abcdef") == "openrouter"
        and detect_provider_from_key("gsk_abcdef") == "groq"
        and detect_provider_from_key("xai-abcdef") == "xai"
        and detect_provider_from_key("sk-proj-ABCDEFGHIJKLMNOP") == "openai"
        and detect_provider_from_key("sk-ant-api03-xxxx") == "anthropic"
        and detect_provider_from_key("AIzaSyxxxxxxxxxxxx") == "gemini"
        and detect_provider_from_key("1234567890.abcdSecret") == "glm"
    )
    labels = [window.cmb_model.itemText(i) for i in range(window.cmb_model.count())]
    no_placeholder = not any("占位" in t or "示例" in t for t in labels)

    # 联动部分：假密钥库 + 关闭网络拉取，避免写到真实凭据 / 发网络请求。
    saved: dict[str, str] = {}
    orig_set, orig_get = window._key_store.set, window._key_store.get
    orig_discover = window._start_model_discovery
    try:
        window._key_store.set = lambda a, s: saved.__setitem__(a, s) or True
        window._key_store.get = lambda a: saved.get(a)
        window._start_model_discovery = lambda *a, **k: None
        # 显式选择 DeepSeek 服务商（sk- 开头无法自动识别，用户必须选）
        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("deepseek"))
        window.edit_key.setText("sk-" + "0123456789abcdef0123456789abcdef")
        window._on_save_key()
        # 自动选中的应当是**支持视觉**的那个（官方功能表：deepseek-flash 支持图像理解、
        # deepseek-v4-pro 不支持）。本工具的每条链路都靠缩略图判断，
        # 自动选中一个不能看图的模型会把精度默默降一级。
        switched = window.cmb_model.currentData() == "deepseek::deepseek-flash"
        saved_account = saved.get("DEEPSEEK_API_KEY") == "sk-" + "0123456789abcdef0123456789abcdef"
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._start_model_discovery = orig_discover

    record(
        "服务商目录与密钥识别：占位已移除、显式选服务商后保存到对应账户",
        detect_ok and no_placeholder and switched and saved_account,
        f"识别规则={detect_ok} 占位已移除={no_placeholder} "
        f"保存后选中（应为支持视觉的 flash）={window.cmb_model.currentData()} "
        f"存入账户={list(saved)}",
    )


def test_provider_selector_qwen(window) -> None:
    """切换「服务商」到千问后，模型下拉只显示千问模型；密钥存到 DASHSCOPE_API_KEY。

    这是上一个"千问 key 被误判成 DeepSeek"的真实 bug 的回归守卫：
    sk- 开头的 key 无法自动区分服务商，必须靠用户显式选择。
    """
    saved: dict[str, str] = {}
    orig_set, orig_get = window._key_store.set, window._key_store.get
    orig_discover = window._start_model_discovery
    try:
        window._key_store.set = lambda a, s: saved.__setitem__(a, s) or True
        window._key_store.get = lambda a: saved.get(a)
        window._start_model_discovery = lambda *a, **k: None

        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("qwen"))
        # 断言"只显示千问的模型"，而不是写死某个 id：
        # 默认模型列表会随官方文档更新（2026-09 核对官方《图像与视频理解》后，
        # 默认从 qwen-vl-max/plus 改成 Qwen3-VL 系列），写死名字会让每次更新都假失败。
        expected_first = window._models_config.providers["qwen"].default_models[0]
        only_qwen = window.cmb_model.count() > 0 and all(
            window.cmb_model.itemData(i).startswith("qwen::") for i in range(window.cmb_model.count())
        )

        window.edit_key.setText("sk-" + "0123456789abcdef0123456789abcdef")
        window._on_save_key()
        switched = window.cmb_model.currentData() == f"qwen::{expected_first}"
        saved_account = saved.get("DASHSCOPE_API_KEY") == "sk-" + "0123456789abcdef0123456789abcdef"
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._start_model_discovery = orig_discover

    record(
        "服务商切换：选千问后只显示千问模型、密钥存到 DASHSCOPE_API_KEY",
        only_qwen and switched and saved_account,
        f"只显示千问={only_qwen} 保存后选中={window.cmb_model.currentData()} "
        f"（期望 qwen::{expected_first}）存入账户={list(saved)}",
    )


def test_key_paste_hygiene(window) -> None:
    """粘贴的密钥要能被清理/拦下，且"存在哪里"必须说真话。

    两类真实情况：
      1. 从网页/控制台复制时带上引号、行尾空白、"Bearer " 前缀 ——
         带着它们发出去只会得到 401，用户却会去查余额与权限；
      2. 全角字符或中间空格 —— 一定是拷错了，必须在保存前拦下来。

    还有一条更容易被忽略的：keyring 不可用时密钥**只在内存里**，
    状态栏如果写"已保存"就是误导 —— 用户重启后发现密钥不见了，
    而程序从没提醒过（这是一个真实缺陷，已修）。
    """
    saved: dict[str, str] = {}
    persisted_flag = {"ok": True}
    orig_set, orig_get = window._key_store.set, window._key_store.get
    orig_discover = window._start_model_discovery
    try:
        def fake_set(account, secret):
            saved[account] = secret
            return persisted_flag["ok"]

        window._key_store.set = fake_set
        window._key_store.get = lambda a: saved.get(a)
        window._start_model_discovery = lambda *a, **k: None

        # 1) 带引号 + 前后空白的密钥 → 存进去的是干净的
        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("deepseek"))
        with _StubDialogs():
            window.edit_key.setText('  "sk-abcdef123456"  ')
            window._on_save_key()
        cleaned = saved.get("DEEPSEEK_API_KEY") == "sk-abcdef123456"

        # 2) 全角字符 → 拦住（不写入任何账户）
        before = dict(saved)
        with _StubDialogs():
            window.edit_key.setText("sk-ab\uff43d123456")
            window._on_save_key()
        blocked = saved == before

        # 3) keyring 写不进去时，状态栏不能写"已保存"，要说清只在本次会话
        persisted_flag["ok"] = False
        with _StubDialogs():
            window.edit_key.setText("sk-abcdef123456")
            window._on_save_key()
        status = window.lbl_key_status.text()
        honest = "本次会话" in status and "已保存到系统密钥链" not in status
        # 4) 能持久化时反过来必须写"已保存到系统密钥链"
        persisted_flag["ok"] = True
        with _StubDialogs():
            window.edit_key.setText("sk-abcdef123456")
            window._on_save_key()
        positive = "已保存到系统密钥链" in window.lbl_key_status.text()
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._start_model_discovery = orig_discover

    record(
        "密钥粘贴杂质清理/拦截 + 存储位置如实报告",
        bool(cleaned and blocked and honest and positive),
        f"引号空白被清理={cleaned} 全角被拦={blocked} 未持久化时说明本次会话={honest} "
        f"持久化时明确说密钥链={positive}",
    )


def test_provider_auto_detect_from_key(window) -> None:
    """填形态确定的 key 时，自动把「服务商」切过去（"更自动"）。

    sk- 开头无法区分服务商，但 AIza→Gemini、sk-ws→千问、<数字id>.<secret>→GLM
    这些是确定唯一的，识别出来就自动切换，用户不用手选。
    """
    saved: dict[str, str] = {}
    orig_set, orig_get = window._key_store.set, window._key_store.get
    orig_discover = window._start_model_discovery
    try:
        window._key_store.set = lambda a, s: saved.__setitem__(a, s) or True
        window._key_store.get = lambda a: saved.get(a)
        window._start_model_discovery = lambda *a, **k: None

        # 1) Gemini key（AIza 开头）→ 自动切到 gemini
        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("deepseek"))
        window.edit_key.setText("AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ")
        window._on_save_key()
        gemini_ok = window.cmb_provider.currentData() == "gemini" and bool(saved.get("GOOGLE_API_KEY"))

        # 2) 千问 key（sk-ws 开头，用户确认的实际形态）→ 自动切到 qwen
        window.cmb_provider.setCurrentIndex(window.cmb_provider.findData("deepseek"))
        window.edit_key.setText("sk-wsABCDEFGHIJKLMNOPQRSTUVWXYZ")
        window._on_save_key()
        qwen_ok = window.cmb_provider.currentData() == "qwen" and bool(saved.get("DASHSCOPE_API_KEY"))
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._start_model_discovery = orig_discover

    record(
        "密钥自动识别：AIza→Gemini、sk-ws→千问，自动切换服务商",
        gemini_ok and qwen_ok,
        f"Gemini自动切换={gemini_ok} 千问自动切换={qwen_ok} 账户={list(saved)}",
    )


def test_pipeline_log_bridge_and_skip_summary(window) -> None:
    """管线日志必须同时进**文件日志**与界面面板；完成汇总要点名被跳过的文件。

    【为什么加这条 —— 真实投诉】用户：「我又进行了一轮测试，发现 DNG 文件依然没有
    被处理」。查证结果：那 3 个 DNG 因更早一轮的写回失败被记成「永久跳过」（键与
    目录无关），所以后来任何模式都不处理它们；而当时的输出只有一句
    「跳过 3 张（共 21 张）」，日志文件里连"跳过"都没有 —— 因为管线消息以前
    只走 `sig_log` → 日志面板，**文件日志收不到**。

    这条用例把两件事钉住：
      ① `BaseWorker` 的 callbacks 走 logging（⇒ 落盘），并且界面面板也照样能看到；
      ② `OutputResult.summary_text()` / `_on_output_finished` 会把被跳过的文件名
         与恢复路径写进日志系统（不是只显示在一个数字里）。
    """
    import logging

    from acb.pipeline.output_mode import OutputResult
    from acb.ui.workers import BaseWorker

    captured: list[tuple[int, str]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            captured.append((record.levelno, record.getMessage()))

    handler = _Capture(level=logging.DEBUG)
    logging.getLogger().addHandler(handler)
    ok = True
    detail: list[str] = []
    try:
        # 1) 管线消息走 logging（⇒ 会落进文件日志）
        worker = BaseWorker()
        callbacks = worker._make_callbacks()
        callbacks.info("自检：管线 INFO 消息")
        callbacks.warn("自检：管线 WARN 消息")
        file_side = (
            any("管线 INFO 消息" in msg for _level, msg in captured)
            and any("管线 WARN 消息" in msg for _, msg in captured)
        )
        # 2) 同一条消息也要出现在界面日志面板（日志系统 → UiLogSignalHandler → 面板）
        before = window.log_panel.text.toPlainText().count("自检：管线面板通路")
        callbacks.info("自检：管线面板通路")
        panel_side = window.log_panel.text.toPlainText().count("自检：管线面板通路") > before
        ok = ok and file_side and panel_side
        detail.append(f"文件日志能收到管线消息={file_side}；界面面板也能收到={panel_side}")

        # 3) 完成汇总必须点名被永久跳过的文件 + 给恢复路径，并进日志系统
        fake = OutputResult(total=3, done=2, skipped=1, permanent_skips=["DJI_0001.DNG"])
        summary = fake.summary_text()
        summary_ok = "DJI_0001.DNG" in summary and "清除失败记录" in summary
        # ⚠ _show_completion_dialog 里是 box.exec()（真模态对话框）→ offscreen 下会永久
        #   阻塞（我第一次写这条用例就这么把整套自检挂死了）。自检里必须换成空实现。
        original_dialog = window._show_completion_dialog
        window._show_completion_dialog = lambda result: None
        try:
            window._on_output_finished(fake)
        finally:
            window._show_completion_dialog = original_dialog
        logged = any("DJI_0001.DNG" in msg for _level, msg in captured)
        ok = ok and summary_ok and logged
        detail.append(f"汇总点名+恢复路径={summary_ok}；汇总进了日志系统={logged}")
    finally:
        logging.getLogger().removeHandler(handler)

    record("管线日志同时进文件与界面 + 被跳过的文件必须点名", ok, "；".join(detail))


def test_kimi_tier_hint(window) -> None:
    """Kimi 档位红字：每次切到 Kimi 都提示、**可点击**、点一下真的改配置。

    背景（用户 2026-09-24 要求）：官方按累计充值额分档限速，但**没有**查询档位的
    接口（实测余额接口只回余额、对话响应无 ratelimit 头）—— 所以程序猜不出来，
    只能：出厂按 Tier1、界面上给未充值的人一个一键切 Tier0 的入口。
    这条用例钉住"入口真的存在且真的能用"，而不仅仅是显示一段文字：
      · 文案里必须有可点的链接（href），否则用户点什么都没反应；
      · 点击必须真的把连接文件改掉，并且**立刻**反映到文案上（不重载配置就会
        看起来"点了没反应"）；
      · 切到别的服务商后这段提示必须消失（它不是全局横幅）；
      · 再切回 Kimi 仍按当前档位显示（不能记"已忽略"，否则升级后找不到入口）。
    """
    import yaml

    from acb.config import connections_path, load_models_config

    ok = True
    detail: list[str] = []

    # 1) 切到 Kimi：应显示"当前按 Tier1 跑、可切 Tier0" + 可点链接
    window._on_models_discovered("kimi", ["kimi-k3", "kimi-k2.6"])
    idx = window.cmb_model.findData("kimi::kimi-k2.6")
    if idx >= 0:
        window.cmb_model.setCurrentIndex(idx)
    text = window.lbl_model_hint.text()
    flags = window.lbl_model_hint.textInteractionFlags()
    clickable = bool(flags & flags.LinksAccessibleByMouse)
    shown = 'href="kimi-tier0"' in text and "Tier0" in text
    ok = ok and clickable and shown
    detail.append(
        f"切到 Kimi 显示档位提示且链接可点={'✓' if shown and clickable else '✗'}"
        f"（原文：{text[:60]}…）"
    )

    # 2) 点一下 → Tier0：连接文件真被改，文案立刻变成"可改回 Tier1"
    tier_file = None
    try:
        window._on_model_hint_link("kimi-tier0")
        path = connections_path()
        conn = yaml.safe_load(path.read_text(encoding="utf-8"))["connections"]["kimi"]
        tier_file = (conn["max_concurrency"], conn["max_rpm"])
        text0 = window.lbl_model_hint.text()
        switched = tier_file == (1, 3) and 'href="kimi-tier1"' in text0
    except Exception as exc:  # noqa: BLE001 —— 自检要把失败原因带出来
        switched, text0 = False, f"{type(exc).__name__}: {exc}"
    ok = ok and switched
    detail.append(f"点击切 Tier0 → 文件 {tier_file}，文案已切换 {'✓' if switched else '✗'}")

    # 3) 再点回 Tier1（升到 Tier1 的用户要能改回来）
    try:
        window._on_model_hint_link("kimi-tier1")
        conn = yaml.safe_load(connections_path().read_text(encoding="utf-8"))["connections"]["kimi"]
        back = (conn["max_concurrency"], conn["max_rpm"]) == (15, 100)
    except Exception as exc:  # noqa: BLE001
        back = False
        detail.append(f"改回 Tier1 时报错：{type(exc).__name__}: {exc}")
    ok = ok and back
    detail.append(f"点击改回 Tier1（升档后仍有入口）{'✓' if back else '✗'}")

    # 4) 切到别的服务商 → 这段提示必须消失；切回 Kimi → 仍在
    window._on_models_discovered("deepseek", ["deepseek-flash"])
    gone = "Tier0" not in window.lbl_model_hint.text()
    window._on_models_discovered("kimi", ["kimi-k2.6"])
    again = 'href="kimi-tier0"' in window.lbl_model_hint.text()
    ok = ok and gone and again
    detail.append(f"切走即隐藏={gone}；切回仍显示（不记'已忽略'）={again}")

    # 5) 点了必须**真的立刻生效**：先塞一条"上次学到的 1/3"，再点 Tier1 ——
    #    若不清掉学习值，"配置与记忆取严"会让用户选完 Tier1 仍被压到 1 并发，
    #    表现为"点了没用"。这条就是钉住它。
    from acb.ai.client import _GATES, _gate_for, load_endpoint_limits, remember_endpoint_limit

    base = "https://api.moonshot.cn/v1"
    remember_endpoint_limit(base, concurrency=1, rpm=3, evidence="concurrency: 1")
    _GATES.clear()          # 丢掉进程内缓存的闸门，重新从"记忆 + 配置"装一次
    stale = _gate_for(load_models_config().models["kimi::kimi-k2.6"])
    stale_gate = (stale.limit, stale.rpm) == (1, 3)
    window._on_models_discovered("kimi", ["kimi-k2.6"])
    window._on_model_hint_link("kimi-tier1")
    fresh = _gate_for(window._models_config.models["kimi::kimi-k2.6"])
    effective = (fresh.limit, fresh.rpm) == (15, 100) and not load_endpoint_limits().get(base)
    ok = ok and stale_gate and effective
    detail.append(
        f"点击后立即按新档位跑（学习值已作废）：旧闸门 {stale.limit}/{stale.rpm} → "
        f"新闸门 {fresh.limit}/{fresh.rpm} {'✓' if stale_gate and effective else '✗'}"
    )

    record("Kimi 档位红字（可点击的一键切换，真写配置且立即生效）", ok, "\n        ".join(detail))


def test_model_discovery_and_vision_hint(window) -> None:
    """拉取 /models 成功后重建下拉；选了无视觉模型时下面红字提示（用户要求）。"""
    from acb.config import ModelSpec, load_discovered_models

    # 1) 模拟拉取成功（直接走回调，不碰网络）
    window._on_models_discovered("deepseek", ["deepseek-v4-pro", "deepseek-flash", "deepseek-chat"])
    labels = [window.cmb_model.itemText(i) for i in range(window.cmb_model.count())]
    discovered_ok = {"deepseek-v4-pro", "deepseek-flash", "deepseek-chat"} <= set(labels)
    # 自动选中的要**能看图**：官方功能表写明 deepseek-v4-pro 不支持图像理解，
    # 所以即使它排在拉取结果的第一位，也不该被自动选中（同目录里 flash 支持）。
    selected = window.cmb_model.currentData() == "deepseek::deepseek-flash"
    persisted = load_discovered_models().get("deepseek") == ["deepseek-v4-pro", "deepseek-flash", "deepseek-chat"]

    # 2) 无视觉模型 → 下面出现红字提示
    fake = ModelSpec(
        label="no-vision-model",
        base_url="https://example.com/v1",
        model="no-vision-model",
        key_env="DEEPSEEK_API_KEY",
        provider="deepseek",
        supports_vision=False,
        supports_json_schema=False,
        max_context=4096,
    )
    orig_spec = window._current_spec
    try:
        window._current_spec = lambda: fake
        window._on_model_changed()
        hint_ok = "不支持图片输入" in window.lbl_model_hint.text()
    finally:
        window._current_spec = orig_spec
        window._on_model_changed()   # 恢复真实模型的提示状态

    # 3) 用户已明确选过的模型，拉取列表**不许**把它改掉。
    #    为什么：用户可能就是为了便宜/纯文本/别的特性才选 V4 Pro，
    #    每次拉取都把选择改回 Flash，他只会觉得“我选的模型怎么老是被换掉”（极难排查）。
    window._on_models_discovered("deepseek", ["deepseek-flash", "deepseek-v4-pro"])
    idx = window.cmb_model.findData("deepseek::deepseek-v4-pro")
    window.cmb_model.setCurrentIndex(idx)
    user_choice = window.cmb_model.currentData()
    window._on_models_discovered("deepseek", ["deepseek-flash", "deepseek-v4-pro"])
    kept = window.cmb_model.currentData() == user_choice == "deepseek::deepseek-v4-pro"

    # 4) 只有选中的那个从列表里消失了，才允许自动换（换成能看图的 flash）
    window._on_models_discovered("deepseek", ["deepseek-flash"])
    fallback = window.cmb_model.currentData() == "deepseek::deepseek-flash"

    record(
        "模型拉取：重建下拉、自动选能看图的、但不覆盖用户已选、无视觉给红字",
        discovered_ok and selected and persisted and hint_ok and kept and fallback,
        f"拉取后含真实id={discovered_ok} 首次自动选中（应为 flash）="
        f"{'deepseek::deepseek-flash' if selected else '其他'} "
        f"已持久化={persisted} 红字提示={hint_ok} 用户已选不被覆盖={kept} "
        f"选中消失后才自动换={fallback}",
    )


def test_model_list_filled(window) -> None:
    """模型下拉必须被**完整填满**，且选中配置文件里的 active。

    这条守卫是被一个真 bug 换来的：曾经的排序代码里写了一句
    `[k for k, _ in entries if k not in by_key]`，而 by_key 正是从 entries 建的 ——
    条件恒为假，列表恒为空 → 除非先置顶过某个模型，下拉里一个模型都没有。
    用户看到的是空的「模型」下拉，既选不了模型也存不了密钥，且**全程无报错**。
    更糟的是自检里当时有"模型少于 2 个就跳过实测"的提前 return，
    于是这个用例安静地"通过"了，把 bug 藏了一整轮。
    """
    from acb.config import load_models_config
    from acb import preferences as P

    try:
        # 模拟"刚启动、还没有任何选中项"：
        # _populate_models 的选中优先级是「刷新前的选中项 → 记忆值 → active」，
        # 所以必须先把选中项清成 -1 并且不留下记忆值，
        # 这时才应该回落到配置里的 active。
        window.cmb_model.setCurrentIndex(-1)
        P.update_preferences(model="")
        window._prefs = P.load_preferences()
        window._populate_models()
        config = load_models_config()
        # 下拉现在只显示「当前服务商」的模型（默认第一个服务商 = deepseek）
        provider = window.cmb_provider.currentData()
        expected = [key for key, spec in config.models.items() if spec.provider == provider]
        got = [window.cmb_model.itemData(i) for i in range(window.cmb_model.count())]
        first = config.first_model_of_provider(provider)
        ok = (
            window.cmb_model.count() == len(expected)
            and sorted(got) == sorted(expected)
            and (first is None or window.cmb_model.currentData() == first[0])
        )
        record(
            "模型下拉：只显示当前服务商的模型且默认选中第一个",
            ok,
            f"服务商={provider} 配置={len(expected)} 个 下拉={window.cmb_model.count()} 个 "
            f"当前选中={window.cmb_model.currentData()}",
        )
    except Exception as exc:
        record("模型下拉：只显示当前服务商的模型且默认选中第一个", False, str(exc))


def test_connection_model(window) -> None:
    """连接 = 端点 + 密钥 + 备注名：自建连接、同一家多把密钥、删除只删一条。

    【为什么把这条件立在 GUI 层】这是两处真问题在界面侧的落地：
      1. 中转站 / 本地网关用户**不该被迫选一家厂商** —— 一选就错：
         既决定了 base_url（错的），又决定了密钥存进哪个槽（也是错的）；
      2. "一键一槽"的存法（DEEPSEEK_API_KEY 这种）在"同一家两把密钥"时必崩，
         冲突只是被推迟到某一天爆发。
    所以这里要证明的是：**两条同 base_url 的连接，密钥各存各的、互不覆盖**，
    而且自建连接绝不写进出厂槽位（那正是上次"千问 key 进了 DEEPSEEK 槽"的错法）。
    """
    from PyQt6.QtWidgets import QMessageBox

    from acb.config import connection_key_env, connections_path, load_connections, load_models_config

    saved: dict[str, str] = {}
    orig_set, orig_get = window._key_store.set, window._key_store.get
    orig_discover = window._start_model_discovery
    orig_question, orig_info = QMessageBox.question, QMessageBox.information
    detail = ""
    ok = False
    try:
        window._key_store.set = lambda a, s: saved.__setitem__(a, s) or True
        window._key_store.get = lambda a: saved.get(a)
        window._start_model_discovery = lambda *a, **k: None
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

        # 0) 出厂连接：id 仍是服务商 id（偏好文件里的 "deepseek::…" 与旧用例都靠这个对应关系）
        factory_ids = [window.cmb_provider.itemData(i) for i in range(window.cmb_provider.count())]
        factory_ok = "deepseek" in factory_ids and "qwen" in factory_ids
        # 显示文本必须带域名：把 base_url 指向中转站后，"服务商名"就是假信息了，域名不会撒谎
        display_ok = all(
            " @ " in window.cmb_provider.itemText(i) for i in range(window.cmb_provider.count())
        )

        # 1) 自建两条连接：同 base_url（中转站 / 同一家的两把密钥都长这样）
        cid_a = window._create_connection(
            "中转-A", "https://relay.example.com/v1", api_key="sk-relay-aaa"
        )
        cid_b = window._create_connection(
            "中转-B", "https://relay.example.com/v1", api_key="sk-relay-bbb"
        )
        conns = load_models_config().connections
        created_ok = (
            cid_a == "中转-A"
            and cid_b == "中转-B"
            # 出厂连接必须还在：新建连接不能把默认那批吃掉
            and all(k in conns for k in ("deepseek", "qwen", "中转-A", "中转-B"))
            and conns["中转-A"].base_url == "https://relay.example.com/v1"
        )

        # 2) 核心：两把密钥各存各的槽位，且**没有**写进出厂槽位
        keys_ok = (
            saved.get(connection_key_env("中转-A")) == "sk-relay-aaa"
            and saved.get(connection_key_env("中转-B")) == "sk-relay-bbb"
            and "DEEPSEEK_API_KEY" not in saved
            and "OPENAI_API_KEY" not in saved
        )

        # 3) 下拉里能看到，格式是「名字 @ 域名」
        labels = [window.cmb_provider.itemText(i) for i in range(window.cmb_provider.count())]
        shown_ok = "中转-A @ relay.example.com" in labels
        selected_ok = window.cmb_provider.currentData() == "中转-B"

        # 4) 真的落盘了（重启后还在）
        on_disk = load_connections(load_models_config().providers)
        disk_ok = "中转-A" in on_disk and "中转-B" in on_disk and connections_path().is_file()

        # 5) 删除一条：只删这一条
        window._on_delete_connection("中转-A")
        after = load_models_config().connections
        delete_ok = "中转-A" not in after and "中转-B" in after and "deepseek" in after

        # 6) 不能把自己锁在门外：删到只剩一条时拒绝再删
        cids = list(load_models_config().connections)
        for cid in cids[:-1]:
            window._on_delete_connection(cid)
        left = list(load_models_config().connections)
        window._on_delete_connection(left[0])
        guard_ok = list(load_models_config().connections) == left

        detail = (
            f"出厂连接={factory_ok} 显示带域名={display_ok} 新建两条={created_ok} "
            f"密钥分开存={keys_ok} 出现在下拉={shown_ok} 选中新建的={selected_ok} "
            f"已落盘={disk_ok} 删除只删一条={delete_ok} 保留最后一条={guard_ok}"
        )
        ok = all((
            factory_ok, display_ok, created_ok, keys_ok, shown_ok, selected_ok,
            disk_ok, delete_ok, guard_ok,
        ))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._start_model_discovery = orig_discover
        QMessageBox.question, QMessageBox.information = orig_question, orig_info
        # 还原成出厂连接：后面的用例依赖"出厂连接一套齐全"这个前提
        connections_path().unlink(missing_ok=True)
        window._models_config = load_models_config()
        window._populate_models()

    record("连接模型：自建中转连接 / 同家两把密钥互不覆盖 / 删除只删一条", ok, detail)


def test_duplicate_key_warning(window) -> None:
    """两条连接共用同一把密钥时，界面必须自己说出来。

    【用户的真实困惑】"为什么连接切换 openai 和 deepseek，用的是同一个 key？"
    实测答案是：**不是程序复用了一把密钥**，而是那两个账户槽里存的本来就是同一个值
    （DEEPSEEK_API_KEY 与 OPENAI_API_KEY 逐字符相同）。密钥是按连接的 key_env
    各存各的，"看起来一样"只是因为数据一样 —— 但用户只能看到掩码，很容易
    误判成程序的 bug。所以这个判断必须由程序做并显示出来。
    """
    saved = {
        "DEEPSEEK_API_KEY": "sk-" + "same" * 8,
        "OPENAI_API_KEY": "sk-" + "same" * 8,      # 与上面**完全相同**
        "DASHSCOPE_API_KEY": "sk-ws" + "different" * 4,
    }
    orig_set, orig_get = window._key_store.set, window._key_store.get
    detail = ""
    ok = False
    try:
        window._key_store.set = lambda a, s: saved.__setitem__(a, s) or True
        window._key_store.get = lambda a: saved.get(a)

        def status_for(cid: str) -> str:
            idx = window.cmb_provider.findData(cid)
            window.cmb_provider.setCurrentIndex(idx)
            # 显式重算一次：`setCurrentIndex` 只有"索引真的变了"才会发信号，
            # 而这条用例跑在前面的用例之后，当前连接**可能已经是它**，
            # 于是状态行会停留在上一轮的文本上 → 假失败（实测被新用例的排序变化暴露出来）。
            # 断言"状态行反映当前连接"这件事，本来就不该依赖"上一个用例把连接留在了别处"。
            window._on_model_changed()
            return window.lbl_key_status.text()

        # 1) 与另一条连接同值 → 必须提醒，并且用"注意色"而不是"一切正常"的绿色
        deepseek_text = status_for("deepseek")
        warned = "同一把密钥" in deepseek_text and "「OpenAI」" in deepseek_text
        warned_color = "#B26A00" in window.lbl_key_status.styleSheet()

        # 2) 反向也成立（切到 OpenAI 时要提到 DeepSeek）
        openai_text = status_for("openai")
        warned_both_ways = "同一把密钥" in openai_text and "「DeepSeek」" in openai_text

        # 3) 值不同的连接不能被误报
        qwen_text = status_for("qwen")
        no_false_positive = "同一把密钥" not in qwen_text and "#0A7A0A" in window.lbl_key_status.styleSheet()

        # 4) 改掉其中一个值后，提醒要消失（否则就是"粘住"的假提示）
        saved["OPENAI_API_KEY"] = "sk-" + "other" * 9
        fixed_text = status_for("deepseek")
        cleared = "同一把密钥" not in fixed_text

        # 5) 没有密钥的连接不参与比较（也不能崩）
        del saved["DASHSCOPE_API_KEY"]
        empty_text = status_for("qwen")
        empty_ok = "未找到密钥" in empty_text and "同一把密钥" not in empty_text

        detail = (
            f"提醒出现={warned} 用注意色={warned_color} 双向提醒={warned_both_ways} "
            f"不误报={no_false_positive} 换掉后消失={cleared} 无密钥不比较={empty_ok}"
        )
        ok = all((warned, warned_color, warned_both_ways, no_false_positive, cleared, empty_ok))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        window._key_store.set, window._key_store.get = orig_set, orig_get
        window._populate_models()

    record("密钥复用提醒：两条连接同一把 key 时明说，值不同/无 key 时不误报", ok, detail)


def test_item_context_menu(window) -> None:
    """下拉条目的右键菜单：置顶 / 取消置顶 / 删除，以及置顶标识 ★ + 加粗。

    为什么改成右键（用户要求）：常驻两个按钮只能作用在"当前选中项"上，
    得先选对再去点，很容易作用到隔壁那一项；右键直接作用在"你正指着的那一项"。
    """
    import json

    from PyQt6.QtCore import QCoreApplication, Qt
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu

    from acb import preferences as P
    from acb.paths import styles_dir
    from acb.ui.widgets import PIN_PREFIX

    opened: list = []
    real_popup = QMenu.popup
    real_exec = QMenu.exec
    # 菜单只被"构造 + 记录"，不真的弹（异步 popup 虽然不阻塞，
    # 但真弹出来会成为活动弹窗，干扰后面的用例）
    QMenu.popup = lambda self, *a, **k: opened.append(self)  # type: ignore[assignment]
    QMenu.exec = lambda self, *a, **k: opened.append(self)   # type: ignore[assignment]

    try:
        with _StubDialogs():
            # ---- 右键菜单内容：未置顶项 → 置顶 / 删除 ----
            last = window.cmb_model.count() - 1
            key = window.cmb_model.itemData(last)
            menu = window._build_item_menu(window.cmb_model, "model", last)
            unpinned_texts = [a.text() for a in menu.actions()]
            menu_ok = unpinned_texts[0] == "置顶" and "删除" in unpinned_texts[1]

            # ---- 行号越界 → 不构造菜单（不能作用到别的项上）----
            bounds_ok = (
                window._build_item_menu(window.cmb_model, "model", -1) is None
                and window._build_item_menu(window.cmb_model, "model", 9999) is None
            )

            # ---- 走"真的右键"这条路：弹出 → 点菜单第一项 = 置顶 ----
            window.cmb_model.setCurrentIndex(last)
            window._open_item_menu(window.cmb_model, "model", last, None)
            opened[0].actions()[0].trigger()

            first = window.cmb_model.itemData(0)
            font = window.cmb_model.itemData(0, Qt.ItemDataRole.FontRole)
            pin_ok = (
                first == key
                and window.cmb_model.itemText(0).startswith(PIN_PREFIX)
                and font is not None
                and font.bold()
                and window.cmb_model.currentData() == key   # 选择不能丢
            )

            # ---- 置顶后菜单第一项要变成「取消置顶」----
            menu = window._build_item_menu(window.cmb_model, "model", 0)
            label_ok = menu.actions()[0].text() == "取消置顶"

            # ---- 再走一次右键 → 点「取消置顶」→ 标识消失 ----
            window._open_item_menu(window.cmb_model, "model", 0, None)
            opened[-1].actions()[0].trigger()
            unpin_ok = (
                P.get_list("pinned_models") == []
                and not any(
                    window.cmb_model.itemText(i).startswith(PIN_PREFIX)
                    for i in range(window.cmb_model.count())
                )
            )

            # ---- 删除模型（确认框一律 Yes）：只从下拉移除，可恢复 ----
            before = window.cmb_model.count()
            victim = window.cmb_model.itemData(before - 1)
            window._open_item_menu(window.cmb_model, "model", before - 1, None)
            opened[-1].actions()[1].trigger()
            removed_ok = (
                window.cmb_model.findData(victim) < 0
                and window.cmb_model.count() == before - 1
                and victim in P.get_list("hidden_models")
            )
            window._restore_hidden_models()
            restored_ok = (
                window.cmb_model.findData(victim) >= 0
                and window.cmb_model.count() == before
            )

            # ---- 风格：占位项不能置顶/删除（给灰掉的说明，而不是空菜单）----
            placeholder = window._build_item_menu(window.cmb_style, "style", 0)
            placeholder_ok = (
                len(placeholder.actions()) == 1
                and not placeholder.actions()[0].isEnabled()
            )

            # ---- 风格：造一个临时风格，走右键置顶 → 删除 ----
            styles_dir().mkdir(parents=True, exist_ok=True)
            style_file = styles_dir() / "_gui_smoke_style.json"
            style_file.write_text(
                json.dumps({"name": "_gui_smoke_style", "sample_count": 0},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            P.set_list("pinned_styles", [])
            window._reload_styles()

            row = window.cmb_style.findData("_gui_smoke_style")
            window.cmb_style.setCurrentIndex(row)
            window._open_item_menu(window.cmb_style, "style", row, None)
            opened[-1].actions()[0].trigger()          # 置顶
            s_font = window.cmb_style.itemData(1, Qt.ItemDataRole.FontRole)
            style_pinned = (
                window.cmb_style.itemData(1) == "_gui_smoke_style"
                and window.cmb_style.itemText(1).startswith(PIN_PREFIX)
                and s_font is not None
                and s_font.bold()
                and window.cmb_style.currentData() == "_gui_smoke_style"
            )

            row = window.cmb_style.findData("_gui_smoke_style")
            window._open_item_menu(window.cmb_style, "style", row, None)
            opened[-1].actions()[1].trigger()          # 删除
            style_deleted = (
                not style_file.exists()
                and window.cmb_style.findData("_gui_smoke_style") < 0
                and "_gui_smoke_style" not in P.get_list("pinned_styles")
            )

            # ---- 内置风格必须拒绝删除（删了下次启动也会被种子补齐）----
            seed_row = window.cmb_style.findData("default_neutral")
            seed_refused = True
            if seed_row >= 0:
                window._open_item_menu(window.cmb_style, "style", seed_row, None)
                opened[-1].actions()[1].trigger()
                seed_refused = (styles_dir() / "default_neutral.json").is_file()

            # ---- 右键接线：发一个**真实的 QContextMenuEvent**，
            #      必须被事件过滤器接住并转成菜单。
            #      （不能用 customContextMenuRequested —— 那条路会让 Qt 的弹窗 grabs
            #        与菜单的 grabs 撞上，实测直接 0xC0000005 闪退，见
            #        _ComboItemMenuFilter 的注释。）
            opened.clear()
            view = window.cmb_style.view()
            view.setCurrentIndex(view.model().index(1, 0))
            rect = view.visualRect(view.model().index(1, 0))
            QCoreApplication.postEvent(
                view.viewport(),
                QContextMenuEvent(
                    QContextMenuEvent.Reason.Mouse, rect.center(),
                    view.viewport().mapToGlobal(rect.center()),
                ),
            )
            QApplication.processEvents()
            QApplication.processEvents()
            wired_ok = (
                len(opened) == 1
                and [a.text() for a in opened[0].actions()][0] in ("置顶", "取消置顶")
                # 模型 + 风格 + 连接，三个下拉各一个事件过滤器
                and len(window._menu_filters) == 3
            )

            P.set_list("pinned_styles", [])
            P.set_list("pinned_models", [])
            P.set_list("hidden_models", [])
            window._reload_styles()
            window._populate_models()

        ok = all((
            menu_ok, bounds_ok, pin_ok, label_ok, unpin_ok, removed_ok, restored_ok,
            placeholder_ok, style_pinned, style_deleted, seed_refused, wired_ok,
        ))
        record(
            "下拉右键菜单：置顶/取消置顶/删除 + 置顶标识（★ 加粗、排最前、选择不丢）",
            ok,
            f"菜单项={menu_ok} 越界返回None={bounds_ok} 模型置顶={pin_ok} "
            f"标签变取消置顶={label_ok} 取消置顶={unpin_ok} 删除={removed_ok} "
            f"恢复={restored_ok} 占位项禁用={placeholder_ok} 风格置顶={style_pinned} "
            f"风格删除={style_deleted} 内置拒删={seed_refused} 右键接线={wired_ok}",
        )
    except Exception as exc:
        record("下拉右键菜单：置顶/取消置顶/删除 + 置顶标识", False, str(exc))
    finally:
        QMenu.popup = real_popup      # type: ignore[assignment]
        QMenu.exec = real_exec         # type: ignore[assignment]


def test_rightclick_crash_probe(window) -> None:
    """「右键下拉条目」不得导致**进程级崩溃** —— 必须放在子进程里跑。

    为什么不能写成普通的进程内用例：这类崩溃是访问冲突（0xC0000005），
    进程会被系统直接终止，本用例里的 try/except、异常守卫、
    甚至日志句柄都没机会执行 —— 真实事故就是"右键风格条目 → 程序瞬间消失，
    日志里一行都没有"。所以只能把这一串操作放进子进程，看它能不能正常退出。

    探针里发的是**真实 QContextMenuEvent**（跟用户按右键同一条路），
    而不是手动调函数 —— 手动调函数复现不出这个崩溃，这一点已经实测确认。
    """
    import subprocess

    probe = Path(__file__).resolve().parent / "menu_crash_probe.py"
    results: list[str] = []
    ok = True
    for variant in ("open", "closed"):
        try:
            # ⚠ 不能用 text=True：那会用**系统 locale（这里是 GBK）**解码，
            # 而探针会打印中文 → 子进程读线程抛 UnicodeDecodeError，
            # stdout 直接变成空串（退出码却依然是 0，于是断言莫名其妙地失败）。
            proc = subprocess.run(
                [sys.executable, str(probe), variant],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=300, cwd=str(probe.parent.parent),
            )
        except subprocess.TimeoutExpired:
            ok = False
            results.append(f"{variant}=超时")
            continue

        good = proc.returncode == 0 and "PROBE_OK" in (proc.stdout or "")
        ok = ok and good
        if proc.returncode == 3221225477:
            results.append(f"{variant}=★崩溃 0xC0000005 访问冲突")
        else:
            results.append(f"{variant}={'通过' if good else f'rc={proc.returncode}'}")

    record(
        "下拉右键：真实右键事件不得导致访问冲突闪退（子进程探针）",
        ok,
        "；".join(results),
    )


def test_version_shown(window) -> None:
    """界面必须显示版本号（用户要求：方便以后升级）。"""
    from acb import __version__

    try:
        text = window.lbl_version.text()
        title = window.windowTitle()
        ok = (
            __version__ in text
            and __version__ in title
            and "Auto Color Diffusion" in text
        )
        record("界面显示产品名与版本号", ok, f"角标={text!r} 标题={title!r}")
    except Exception as exc:
        record("界面显示产品名与版本号", False, str(exc))


def test_about_content(window) -> None:
    """「关于」的内容必须与用户给定的文案逐字一致（作者 + 5 个反馈/赞助入口）。

    为什么值得测：这几行是**对外联络方式**（反馈、赞助），写错一个字用户就找不到人；
    版本号必须从 `__version__` 派生（否则发版时会忘了改关于对话框）。
    """
    from acb import __version__

    lines = window._about_text().splitlines()
    urls = [
        "https://space.bilibili.com/1999530739",
        "https://www.xiaohongshu.com/user/profile/649dfbe8000000001001c834",
        "https://github.com/ziranjun/Auto-Color-Diffusion",
        "https://afdian.com/a/ZIRAN2391",
    ]
    ok = (
        lines[0] == f"Auto Color Diffusion V{__version__}"
        and "Author：ZIRAN" in window._about_text()
        and all(url in window._about_text() for url in urls)
        and "You can give feedback on the app in these." in window._about_text()
        and "You can sponsor me in Github or afdian" in window._about_text()
    )
    record(
        "关于对话框：作者 / Bilibili / rednote / Github / afdian 与版本号",
        ok,
        f"首行={lines[0]!r} 行数={len(lines)} 链接齐全={all(url in window._about_text() for url in urls)}",
    )


def test_about_icon_is_scaled(window) -> None:
    """「关于」里的图标必须先缩到几十像素，不能把 512 的原图直接交出去。

    踩过的坑：QMessageBox **不会**缩放传给 setIconPixmap 的位图 —— 512×512 的图
    会按原尺寸占满左侧，正文被挤成一条窄栏（用户实测反馈）。这条门禁守住"缩过"。
    """
    from PIL import Image

    from acb.paths import app_icon_png_large_path

    pixmap = window._about_icon_pixmap()
    detail: list[str] = []
    ok = pixmap is not None and not pixmap.isNull()
    if ok:
        ratio = pixmap.devicePixelRatio() or 1.0
        logical = pixmap.width() / ratio
        with Image.open(app_icon_png_large_path()) as image:
            source = image.size[0]
        # 确实缩过（小于源图）且不超过 128 逻辑像素 —— 再大就会挤正文
        ok = source > logical and logical <= 128
        detail.append(
            f"源图 {source}px → 对话框 {logical:.0f} 逻辑像素"
            f"（物理 {pixmap.width()}px，dpr={ratio:g}）"
        )
    else:
        detail.append("取不到图标位图（素材缺失）")
    record("关于对话框：图标先缩到 72 逻辑像素再塞进去（不是把 512 原图丢进去挤正文）", ok,
           "；".join(detail))


def test_app_icon(window) -> None:
    """窗口图标：素材在就要**真的装上**（三个消费方各验一遍）。

    为什么要单测：图标有三个消费方 —— 窗口、QApplication（任务栏/对话框）、
    打包的 exe。以前三处都没接，界面上看不到任何异常，只是"没有图标"，
    这种缺陷只有靠断言才守得住。另外 AppUserModelID 必须在建 QApplication
    之前设，这里只验它可被调用（真实生效要看任务栏，自动化里验不了）。
    """
    from acb.paths import app_icon_ico_path, app_icon_png_path
    from acb.ui.icon import icon_files_present, load_app_icon, set_app_user_model_id

    png_ok, ico_ok = icon_files_present()
    try:
        # 1) 窗口上装的图标不是空图标
        icon = window.windowIcon()
        window_icon_ok = (not icon.isNull()) if png_ok else False
        # 2) 加载器返回同一个（缓存）且能给出尺寸
        loaded = load_app_icon()
        sizes = loaded.availableSizes() if loaded is not None else []
        loader_ok = (loaded is not None) and any(s.width() >= 32 for s in sizes)
        # 3) 打包用的 ICO：必须真的是多尺寸（任务栏/资源管理器/Alt-Tab 各取所需）
        ico_sizes: list[tuple[int, int]] = []
        if ico_ok:
            from PIL import Image

            with Image.open(app_icon_ico_path()) as ico:
                ico_sizes = sorted(ico.info.get("sizes", set()))
        ico_multi = len(ico_sizes) >= 4 and (16, 16) in ico_sizes and (256, 256) in ico_sizes
        # 4) PNG 必须是**圆角**：四角透明、中心不透明
        corners_transparent = False
        if png_ok:
            from PIL import Image

            with Image.open(app_icon_png_path()) as png:
                rgba = png.convert("RGBA")
                w, h = rgba.size
                corners = [
                    rgba.getpixel((0, 0)), rgba.getpixel((w - 1, 0)),
                    rgba.getpixel((0, h - 1)), rgba.getpixel((w - 1, h - 1)),
                ]
                center = rgba.getpixel((w // 2, h // 2))
                corners_transparent = all(c[3] == 0 for c in corners) and center[3] == 255
        # 5) AppUserModelID 至少可调用（非 Windows 返回 False 不算失败）
        set_app_user_model_id()
        ok = png_ok and ico_ok and window_icon_ok and loader_ok and ico_multi and corners_transparent
        record(
            "应用图标：圆角 PNG 装上窗口 + ICO 多尺寸 + 四角透明",
            ok,
            f"PNG={app_icon_png_path().name}({png_ok}) ICO={app_icon_ico_path().name}({ico_ok}) "
            f"窗口图标非空={window_icon_ok} 加载器={loader_ok} ICO 尺寸={ico_sizes or '无'} "
            f"四角透明且中心不透明={corners_transparent}",
        )
    except Exception as exc:
        record("应用图标：圆角 PNG 装上窗口 + ICO 多尺寸 + 四角透明", False,
               f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Auto Color Diffusion 的 GUI 级自检（offscreen，无需显示器、不联网）",
    )
    parser.add_argument("--raw-dir", type=Path, default=None,
                        help="含有真实 RAW 的目录（默认使用项目内 test_data）")
    args = parser.parse_args(argv)

    print("=" * 90)
    print("Auto Color Diffusion —— GUI 级自检")
    print("=" * 90)
    print("说明：使用 Qt offscreen 平台插件，无需显示器、不联网、不调用 AI。")
    print("      会真实构造主窗口并走真实的槽函数路径。")
    print("=" * 90 + "\n")

    qInstallMessageHandler(_qt_message_handler)

    # 把数据目录隔离到临时区：本套自检会真实构造主窗口，而主窗口启动时就会
    # 写偏好文件（preferences.json），新增的用例还会往 styles/ 里放测试风格。
    # 不做隔离就会污染用户真实的 %APPDATA%\AutoColorDiffusion
    # —— 自检脚本不该有这种副作用（与 ACB_NO_CLEANUP 同一个理由）。
    # ⚠ 必须在 `QApplication(...)` **之前**设：Qt 构造时会缓存标准路径
    #   （QStandardPaths），之后再改环境变量对它已经不生效了。
    _isolated_appdata = tempfile.mkdtemp(prefix="acb_gui_appdata_")
    _old_appdata = os.environ.get("APPDATA")
    os.environ["APPDATA"] = _isolated_appdata

    app = QApplication.instance() or QApplication([])

    window = test_construct_window(app)
    if window is None:
        print("\n" + "=" * 90)
        print("[总体失败] 主窗口无法构造，后续测试已跳过。")
        return 1

    # 把密钥后端整体换成内存假实现 —— **本套自检绝不允许碰真实的 Windows 凭据管理器**。
    #
    # 【为什么必须在这里统一做，而不是靠各用例自觉 —— 真实事故】
    # 有一条用例的清理代码在 `finally` 里先把真实后端还原、再调 `_key_store.delete(...)`，
    # 于是它真的删掉了用户凭据管理器里那条 OPENAI_API_KEY。
    # 只在用例内部打补丁，就永远有"补丁范围没盖到清理路径"的缝隙；
    # 在入口一次性接管，任何用例（包括它的事后清理）都碰不到真实凭据。
    _real_keyring_module = window._key_store._keyring_module
    window._key_store._keyring_module = _InMemoryKeyring()
    _keyring_isolated = not isinstance(_real_keyring_module, _InMemoryKeyring)
    record(
        "自检期间密钥后端已换成内存实现（不碰真实凭据管理器）",
        bool(_keyring_isolated),
        f"原后端 = {type(_real_keyring_module).__name__ if _real_keyring_module else 'None'}",
    )

    candidates = _candidate_files(args.raw_dir)

    print()
    test_add_sources(window, candidates)
    print()
    test_slot_path_add_files(window)
    print()
    test_mode_switch(window)
    test_ai_settings_shared(window)
    print()
    test_progress_bounds(window)
    print()
    test_collect_options(window)
    test_option_row_layout(window)
    test_output_toggle_disables_options(window)
    test_offline_debug_mode(window)
    test_autopilot_style_available(window)
    test_api_key_deletion(window)
    test_output_dir_on_main_window(window)
    print()
    test_start_vs_resume(window)
    print()
    test_cleanup_on_exit(window)
    test_open_script(window)
    test_menu_layout(window)
    test_factory_settings_reset(window)
    print()
    test_format_and_suffix(window)
    print()
    test_preferences_restart(window)
    test_more_preferences_restart(window)
    print()
    test_remove_selected(window)
    print()
    test_model_list_filled(window)
    test_provider_catalog_and_key_detection(window)
    test_provider_selector_qwen(window)
    test_provider_auto_detect_from_key(window)
    test_key_paste_hygiene(window)
    test_train_page_layout_and_hint_hygiene(window)
    test_model_discovery_and_vision_hint(window)
    test_pipeline_log_bridge_and_skip_summary(window)
    test_kimi_tier_hint(window)
    test_connection_model(window)
    test_duplicate_key_warning(window)
    test_item_context_menu(window)
    test_rightclick_crash_probe(window)
    print()
    test_version_shown(window)
    test_about_content(window)
    test_about_icon_is_scaled(window)
    test_app_icon(window)
    print()
    test_chinese_context_menu(window)
    print()
    test_quality_warning(window)
    print()
    test_low_quality_confirm(window)
    print()
    test_clear_files(window)
    print()
    test_font_helper()
    test_qt_argv_hygiene()
    print()
    test_exception_guard()
    print()
    test_qt_messages()

    # 真的关一次窗。
    #
    # 【为什么必须加这一步 —— 实测踩到的洞】
    # 三个下拉都装了右键菜单事件过滤器（_ComboItemMenuFilter），
    # 而摘除它们的代码在 `closeEvent → _detach_menu_filters()` 里。
    # 自检以前从不调 close()，于是进程直接结束时那些过滤器还挂在 combo 上 ——
    # 实测 11 次里有 1 次以 0xC0000005（访问冲突）结束，而且**结论行已经打完了**，
    # 看起来像“全部通过”却是个失败退出（只有退出码能戳穿）。
    # 用户真实使用时一定会关窗，所以这不是产品缺陷；但自检必须走完整路径，
    # 否则就会把这类问题留到以后（而且是最难查的那类）。
    window.close()

    # 还原密钥后端（自检期间被换成内存实现，见前面那段说明）
    window._key_store._keyring_module = _real_keyring_module

    # 把隔离的数据目录还回去（临时目录留着，便于事后查看）
    if _old_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _old_appdata

    print("\n" + "=" * 90)
    failed = [name for name, ok, _ in _results if not ok]
    print(f"GUI 自检完成：{len(_results) - len(failed)}/{len(_results)} 项通过")
    if _skipped:
        print(f"其中 {len(_skipped)} 项因环境/素材不足**未实测**（不计入失败，但也没真的验过）：")
        for name in _skipped:
            print(f"  – {name}")
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✗ {name}")
        return 1
    print("全部通过。界面层无阻塞性问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
