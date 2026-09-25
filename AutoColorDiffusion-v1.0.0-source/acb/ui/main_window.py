# -*- coding: utf-8 -*-
"""主窗口（硬约束 #8 的控件清单 + #9 的线程纪律）。

界面清单（逐条对应硬约束 #8）
    ☑ 添加照片            → 添加文件 / 添加文件夹 / 清空
    ☑ 提示词输入框        → QPlainTextEdit
    ☑ 模型下拉            → QComboBox（条目来自 connections.yaml 的连接，
                            模型 id 由各连接的 /models 拉取后缓存在 discovered_models.json）
    ☑ API key 输入        → 掩码输入框 + 保存按钮 + 状态标签（keyring 有值时只显示掩码）
    ☑ 风格下拉            → QComboBox（来自 styles/ 目录）
    ☑ 输出色彩空间        → sRGB / Adobe RGB(1998) / Display P3
    ☑ 输出图片质量        → Photoshop 刻度 0–12 的滑块（12 = 最高，0 = 极低）
    ☑ 输出目录            → 路径选择 + 「输出到源目录同层」勾选
    ☑ 进度条 + 实时日志区
    ☑ 开始 / 停止（停止 = 跳过剩余请求，已完成的保留）
    ☑ 模式切换开关        → 输出模式 / 训练模式
另外按硬约束 #6 补充了界面入口：续跑、只跑失败、清除失败记录。

本文件是**唯一**允许操作控件的模块（硬约束 #9）。
所有耗时任务的实现都在 pipeline 层，通过 workers.BaseWorker 在 QThread 中执行。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QFontMetrics, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from .. import cleanup
from .. import preferences
from ..ai.adapter import AdapterLike, ModelAdapter
from ..ai.client import RequestBudget, forget_endpoint_limit
from ..ai.offline import build_offline_adapter
from ..cache import ThumbCache
from ..config import (
    PROVIDER_LABELS,
    ConnectionSpec,
    ModelsConfig,
    connection_key_env,
    connections_path,
    delete_connection,
    detect_provider_from_key,
    load_discovered_models,
    load_models_config,
    model_sort_key,
    save_discovered_models,
    set_endpoint_limits,
    upsert_connection,
)
from ..constants import (
    BUDGET_MULTIPLIER,
    DEBUG_ONLY_STYLE_NAMES,
    DRY_RUN_LIMIT,
    COLOR_SPACE_CHOICES,
    DEFAULT_EXPORT_FORMAT,
    DEFAULT_PS_JPEG_QUALITY,
    DEFAULT_WORKERS,
    EXPORT_FORMATS,
    KIMI_PROVIDER_ID,
    KIMI_TIER_LIMITS,
    MAX_ATTEMPTS_PER_FILE,
    RAW_EXTENSIONS,
    THUMB_LONG_EDGE,
    THUMB_QUALITY,
    TRAIN_MIN_SAMPLES_WARN,
    clamp_ps_quality,
    describe_low_quality_warning,
    describe_ps_quality,
    is_lossless_format,
    is_very_low_quality,
    normalize_export_suffix,
)
from ..errors import AcbError
from ..keyring_store import KeyStore, key_problem, mask, normalize_api_key
from ..logging_setup import get_logger, setup_logging
from ..paths import (
    app_icon_png_large_path,
    config_dir,
    ensure_runtime_dirs,
    logs_dir,
    require_windows,
    styles_dir,
)
from ..pipeline.job import JobState
from ..pipeline.output_mode import JobCallbacks, OutputOptions, OutputResult, run_output_mode
from ..pipeline.style_profile import (
    is_autopilot_style_name,
    ensure_seed_styles,
    is_seed_style,
    list_styles,
    load_style_profile,
    style_file_for,
)
from ..pipeline.train_mode import TrainOptions, TrainResult, discover_pairs, run_train_mode
from ..ps import photoshop
from ..raw.exiftool import ExiftoolRunner
from ..raw.icc import describe_optional_profiles, ensure_icc_assets
from .log_panel import LogPanel
from .i18n import ensure_chinese_ui, install_chinese_translations
from .icon import apply_window_icon, load_app_icon, set_app_user_model_id
from .connection_dialog import ConnectionDialog
from .widgets import (
    PIN_PREFIX,
    STAGE_STATE_COLORS,
    Card,
    LabeledPathPicker,
    QualitySlider,
    SecretLineEdit,
    apply_mica,
    apply_theme,
    choose_directory,
    choose_raw_files,
    choose_xmp_and_raw_pairs,
    elide_middle,
    pinned_font,
)
from .workers import ModelsFetchWorker, OutputWorker, TrainWorker

log = get_logger("ui.main")

# 窗口默认尺寸。
# 依据：控件较多，1100×780 能在 1080p 屏幕上完整显示而不需要滚动，
# 同时为日志区留下足够的垂直空间（日志是本工具排障的主要手段）。
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 790

# 提示词输入框的最大高度（像素）。
# 依据：提示词通常 1–5 行；给 78px 约 4 行可视区，再多会挤压日志区。
PROMPT_MAX_HEIGHT = 78

# 「关于」对话框里图标的边长（逻辑像素）。
# 依据：QMessageBox **不会**缩放你塞进去的位图 —— 直接给 512 的图，它会按原尺寸
# 占满左侧，正文被挤成一条窄栏（用户实测反馈）。72px 与系统“信息”图标同一量级。
ABOUT_ICON_SIZE = 72


class MainWindow(QMainWindow):
    """主窗口。"""

    # 把 logging 的消息转到主线程的信号（不能用回调直接改控件：回调可能来自工作线程）。
    sig_log_bridge = pyqtSignal(str, int)

    def __init__(self) -> None:
        super().__init__()

        # 平台检查放在最前面：非 Windows 直接抛错并给出明确提示。
        require_windows()

        self.setWindowTitle(f"Auto Color Diffusion —— AI 批量调色辅助工具  v{__version__}")
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        # 窗口图标（圆角风格，由 tools/make_icon.py 生成）。
        # 取不到素材时它返回 False 且什么都不做 —— 缺图标不该拦住启动，
        # 自检会断言"图标确实装上了"，把缺素材变成一条明确的失败。
        self._icon_applied = apply_window_icon(self)

        # --- 运行时状态 ---
        self._sources: list[Path] = []
        self._models_config: ModelsConfig | None = None
        self._key_store = KeyStore()
        self._job_state = JobState()
        self._exiftool = ExiftoolRunner()
        self._worker: OutputWorker | TrainWorker | None = None
        # 模型列表拉取线程：必须**一直持有引用**，否则 QThread 被 GC 后信号就断了
        # （而且子类线程被销毁时仍可能在跑 → 进程级崩溃）。列表用于处理
        # "上一次还没拉完，用户又存了一次密钥"的情况（见 _start_model_discovery）。
        self._models_fetch_worker: ModelsFetchWorker | None = None
        self._models_fetch_workers: list[ModelsFetchWorker] = []
        # --- 用户偏好（尾缀 / 格式 / 主题 / 置顶项）---
        # 必须在 _build_ui() **之前**读出来：界面要按上次的选择预填控件。
        # 这正是用户提的痛点 —— 每次开程序都得重新输一遍尾缀。
        self._prefs = preferences.load_preferences()
        # 当前主题：False=浅色，True=深色（「外观」菜单切换）。
        self._dark = self._prefs.get("theme") == "dark"

        ensure_runtime_dirs()
        # 最近一次成功产出的输出目录，用于「打开输出目录」按钮。
        # 声明在 __init__ 而不是类体末尾：放在类体末尾虽然能跑，
        # 但会让"这个实例属性在哪初始化"变得难以检索。
        self._last_output_dir: Path | None = None
        # 本次运行的脚本产物目录（manifest.json / export_batch.jsx / run_export.bat）。
        # 单独记一份，是因为它和出片目录现在是**两个不同的地方**：
        # 脚本在软件数据目录下的 runs/<运行标识>/ 里，出片目录里只有 JPG。
        self._last_script_dir: Path | None = None
        # 本次是否需要用户手动补跑导出（自动调用 Photoshop 没成功）。
        # 这个标志决定了两件事：完成对话框里要不要给「打开脚本目录」按钮，
        # 以及退出清理时要不要保留那个脚本目录（见 _cleanup_on_exit）。
        self._manual_export_needed = False
        # 当前打开着的下拉条目菜单（见 _open_item_menu）。
        # 用 popup() 异步弹出时必须自己保活，否则会被 GC 掉。
        self._item_menu: QMenu | None = None
        # 下拉右键的事件过滤器。必须留 Python 引用：PyQt 只靠 C++ 父子关系
        # 管不住 Python 侧的重写方法，包装对象被 GC 后 eventFilter 就不再被调用。
        self._menu_filters: list[QObject] = []

        # --- 界面 ---
        # 中文化 Qt 自带文案（输入框右键菜单、QInputDialog 的 OK/Cancel 等）。
        # 放在这里而不是只在 run_app 里装：任何会建主窗口的入口（app.py、
        # 自检脚本、以后新增的入口）都自动覆盖，不会出现"新入口忘了装翻译
        # → 右键菜单又变英文"这种漏项。幂等，重复调用没有代价。
        ensure_chinese_ui()
        self._build_ui()
        self._apply_theme(dark=self._dark)

        # --- 日志（必须在界面建好之后，才能把消息显示到日志区）---
        self.sig_log_bridge.connect(self._on_log_bridge)
        setup_logging(logs_dir(), ui_callback=self._logging_callback)

        log.info("=" * 72)
        log.info("Auto Color Diffusion v%s 启动（Python %s）", __version__, sys.version.split()[0])
        log.info("日志目录：%s", logs_dir())
        log.info(
            "界面约定：缩略图长边 %d px、q%d（仅供 AI 判断，不参与出片）；"
            "默认输出质量 %s",
            THUMB_LONG_EDGE,
            THUMB_QUALITY,
            describe_ps_quality(DEFAULT_PS_JPEG_QUALITY),
        )

        # --- 环境自检 ---
        self._check_environment()

    # ========================================================================
    # 界面构建：左「照片」卡片 + 右「调色助手」卡片 + 菜单栏
    # ========================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(18)

        photos_card = self._build_photos_card()
        assistant_card = self._build_assistant_card()
        # 固定两栏最小宽度：右侧底栏（选项 + 按钮）较宽，给它留够空间；
        # 同时避免切换模式时左右比例跟着跳（用户反馈过）。
        photos_card.setMinimumWidth(360)
        assistant_card.setMinimumWidth(540)
        root.addWidget(photos_card, 4)
        root.addWidget(assistant_card, 6)

        # 隐藏对话框（由菜单触发）。必须在 _build_menu_bar 之前建好，
        # 因为菜单里的动作会引用这些按钮与面板。
        self._adv_dialog = self._build_advanced_dialog()
        self._log_dialog = self._build_log_dialog()

        self._build_menu_bar()

    def _build_photos_card(self) -> QWidget:
        """左侧卡片：文件列表 + 细进度条 + 环境小结。"""
        card = Card("照片")

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_add_files = QPushButton("添加照片")
        self.btn_add_dirs = QPushButton("添加文件夹")
        self.btn_add_pairs = QPushButton("添加训练样本")
        self.btn_add_pairs.setVisible(False)
        # 「移除选中」只从待处理列表里拿掉，**不碰磁盘上的文件**。
        # 标签刻意用"移除"而不是"删除"：选错一张时用户最怕的是误删原片，
        # 这个词必须让人一眼放心。
        self.btn_remove_selected = QPushButton("移除选中")
        self.btn_remove_selected.setToolTip(
            "把列表里选中的照片移出待处理队列（按住 Ctrl / Shift 可多选）。\n"
            "只改列表，**不会删除或修改磁盘上的任何文件**。"
        )
        self.btn_clear_files = QPushButton("清空")
        self.btn_clear_files.setObjectName("danger")
        for btn in (self.btn_add_files, self.btn_add_dirs, self.btn_add_pairs):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_remove_selected)
        buttons.addWidget(self.btn_clear_files)
        card.body.addLayout(buttons)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["文件", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(True)
            header.resizeSection(0, 260)
        card.body.addWidget(self.table, 1)

        self.lbl_file_summary = QLabel("尚未添加文件。")
        self.lbl_file_summary.setObjectName("hintLabel")
        self.lbl_file_summary.setWordWrap(True)
        card.body.addWidget(self.lbl_file_summary)

        # 细进度条：放在照片列表底下，不占太多空间。
        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.lbl_stage = QLabel("就绪")
        self.lbl_stage.setMinimumWidth(110)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.lbl_progress_text = QLabel("0 / 0")
        progress_row.addWidget(self.lbl_stage, 1)
        progress_row.addWidget(self.progress, 3)
        progress_row.addWidget(self.lbl_progress_text)
        card.body.addLayout(progress_row)

        self.lbl_env = QLabel("环境自检中…")
        self.lbl_env.setObjectName("envLabel")
        self.lbl_env.setWordWrap(True)
        card.body.addWidget(self.lbl_env)

        # 版本号必须显示在界面上：用户反馈问题时能一眼报出版本，
        # 升级后也能自己确认是不是装上了新版。文本从 __version__ 派生，无第二份。
        self.lbl_version = QLabel(f"{APP_DISPLAY_NAME}　v{__version__}")
        self.lbl_version.setObjectName("hintLabel")
        self.lbl_version.setToolTip(
            "版本号。升级时只需改 acb/__init__.py 里的 __version__ 一处。"
        )
        card.body.addWidget(self.lbl_version)

        self.btn_remove_selected.clicked.connect(self._on_remove_selected)
        self.btn_add_files.clicked.connect(self._on_add_files)
        self.btn_add_dirs.clicked.connect(self._on_add_dirs)
        self.btn_clear_files.clicked.connect(self._on_clear_files)
        self.btn_add_pairs.clicked.connect(self._on_add_pairs)
        return card

    def _build_assistant_card(self) -> QWidget:
        """右侧卡片：AI 聊天框式的「调色助手」。"""
        card = Card("调色助手")

        # 工作模式：分段控件（两个互斥按钮）。
        # 用可勾选的 QPushButton + QButtonGroup 实现互斥，而不是 QRadioButton：
        # QRadioButton 左侧总会画一个圆形指示器，在"分段控件"的视觉里是多余的。
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.rb_output = QPushButton("输出模式")
        self.rb_train = QPushButton("训练模式")
        for rb in (self.rb_output, self.rb_train):
            rb.setObjectName("seg")
            rb.setCheckable(True)
            mode_row.addWidget(rb)
        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)
        self._seg_group.addButton(self.rb_output)
        self._seg_group.addButton(self.rb_train)
        self.rb_output.setChecked(True)
        self.rb_output.toggled.connect(self._on_mode_changed)
        mode_row.addStretch(1)
        card.body.addLayout(mode_row)

        # 模型与密钥是**两种模式共用**的设置（训练模式同样要调用 AI），
        # 所以放在 option_stack 之外 —— 否则切到训练模式时整页被换掉，
        # 用户会看到"要选模型、要填密钥，但控件不见了"。
        card.body.addWidget(self._build_ai_settings())

        self.option_stack = QStackedWidget()
        self.option_stack.addWidget(self._build_output_page())
        self.option_stack.addWidget(self._build_train_page())
        card.body.addWidget(self.option_stack, 1)

        # 底栏分两行：第一行是出片选项，第二行是运行按钮。
        #
        # 为什么不是挤在一行：加上"格式"与"尾缀"两个控件后，
        # 选项 + 四个按钮的最小宽度合计已超过 1000px，在 1280 宽的窗口里
        # 硬排一行会把左右两栏的比例顶变形（用户反馈过尺寸跳变）。
        # 选项紧贴在按钮正上方，操作动线不变。
        # 底栏第 1 行：「输出」总开关 + 输出位置。
        #
        # 【为什么「输出」必须在最前】它决定的是"到底出不出片"，
        # 后面的输出目录 / 色彩空间 / 质量 / 格式 / 尾缀全是它的从属设置。
        # 以前输出目录在它上面（而且还在高级设置里），看上去像两组互不相干的设置。
        #
        # 【为什么输出位置挤在开关这行而不是单独一行】底栏每多一行就要从
        # 提示词框里抽走约 40px；开关只有 50px 宽，右边本來就空着。
        # 宽度实测：开关 50 + 位置 268 + 子目录 154 + 间距 ≈ 590px，
        # 而 1280 窗口给右侧卡片的是 ~700px，有余量（硬排一行会被压到
        # 硬裁，这个亏吃过）。
        switch_row = QHBoxLayout()
        switch_row.setSpacing(8)

        self._out_switch_options = QWidget()
        switch_box = QHBoxLayout(self._out_switch_options)
        switch_box.setContentsMargins(0, 0, 0, 0)
        switch_box.setSpacing(8)

        # 「输出」开关：勾选 = 写完 XMP 后自动调用 Photoshop 导出。
        # 【为什么从高级设置挪到这里】它是每次都要看一眼的开关 ——
        # 不勾选就只写 XMP、不导图，藏在高级设置里很容易出现"跑完了怎么没图"的困惑。
        # 而且原来那句说明写死了"导出 JPG"，现在还能导 PNG，已经不准确了。
        self.chk_run_ps = QCheckBox("输出")
        self.chk_run_ps.setChecked(True)
        self.chk_run_ps.setToolTip(
            "勾选 = 写完 XMP 后自动调用 Photoshop 导出照片。\n"
            "取消勾选 = 只写 XMP、不导出（想稍后用 Camera Raw 自己处理时用），\n"
            "后面的输出位置 / 色彩空间 / 质量 / 格式 / 尾缀会一并变灰。"
        )
        # 没勾「输出」时，后面所有输出设置都无从谈起，灰掉。
        # 「灰掉而不是隐藏」是本项目一贯的做法：隐藏会改变底栏宽度，
        # 左右两栏的比例就会跟着跳（用户反馈过）。
        self.chk_run_ps.toggled.connect(self._refresh_output_options_state)
        switch_box.addWidget(self.chk_run_ps)

        # 输出位置：也从「高级设置」里搬出来（用户要求）。
        # 与「输出」开关同一个道理：这是每次导出前都要看一眼的东西；
        # 它一旦不对，产出就跑到了自己找不到的地方（"跑完了没图"里
        # 有一半其实是跑到别的目录去了）。
        self.picker_output = LabeledPathPicker("输出目录")
        self.picker_output.setToolTip(
            "留空 = 导出的照片与源 RAW 放在同一个目录（默认）。\n"
            "填写 = 全部导出到该目录；点「浏览…」选目录。\n"
            "离线调试跑批也一样以此为准（那是你亲手点的目录）。"
        )
        self.picker_output.button.clicked.connect(self._on_pick_output_dir)
        switch_box.addWidget(self.picker_output, 1)

        self.chk_export_subdir = QCheckBox("输出到 _export 子目录")
        self.chk_export_subdir.setToolTip(
            "默认把导出的照片和 RAW 放在同一个目录，方便直接对比。\n"
            "勾上此项改为放进 <源目录>/_export/ 子目录（此时左边的输入框会被忽略）。"
        )
        self.chk_export_subdir.toggled.connect(self._on_export_subdir_toggled)
        switch_box.addWidget(self.chk_export_subdir)

        switch_row.addWidget(self._out_switch_options, 1)
        card.body.addLayout(switch_row)

        # 底栏第 2 行：色彩空间与质量。
        options_row = QHBoxLayout()
        options_row.setSpacing(8)

        self._out_quick_options = QWidget()
        quick = QHBoxLayout(self._out_quick_options)
        quick.setContentsMargins(0, 0, 0, 0)
        quick.setSpacing(8)

        # 这两个说明标签保留引用：它们跟着同排控件一起变灰。
        # QLabel 不会因为旁边的控件被禁用而自己变灰，必须显式 setEnabled。
        self.lbl_color_space = QLabel("输出色彩空间")
        quick.addWidget(self.lbl_color_space)
        self.cmb_color_space = QComboBox()
        self.cmb_color_space.addItems(list(COLOR_SPACE_CHOICES))
        self.cmb_color_space.setToolTip(
            "导出图用的色彩空间。sRGB 兼容性最好；Adobe RGB / Display P3 色域更广。"
        )
        self._calibrate_color_space_width()
        quick.addWidget(self.cmb_color_space)

        self.lbl_quality = QLabel("输出质量")
        quick.addWidget(self.lbl_quality)
        self.slider_quality = QualitySlider(compact=True)
        quick.addWidget(self.slider_quality, 1)
        options_row.addWidget(self._out_quick_options, 1)

        # 输出格式 + 文件名尾缀（两者都放在按钮那一行的左边）。
        self._out_suffix_options = QWidget()
        suffix_box = QHBoxLayout(self._out_suffix_options)
        suffix_box.setContentsMargins(0, 0, 0, 0)
        suffix_box.setSpacing(6)
        # 格式挪到这一行：上一行要留给「输出 / 色彩空间 / 质量」，
        # 挤在一起会把质量说明文字压到显示不全（用户反馈过）。
        suffix_box.addWidget(QLabel("输出格式"))
        self.cmb_format = QComboBox()
        # 下拉里只写格式代号。曾经带过"JPG（体积小，最通用）"这种括号说明，
        # 但下拉宽度有限，说明文字会被省略号截掉，反而看不清选的是哪个格式。
        # 两种格式的区别写在下面的 tooltip 里，需要时悬停即可看到。
        for fmt in EXPORT_FORMATS:
            self.cmb_format.addItem(fmt, fmt)
        self.cmb_format.setToolTip(
            "输出文件格式。\n"
            "JPG：体积最小、最通用，画质由「输出质量」滑块决定；\n"
            "PNG：无损格式，画质滑块不参与（文件更大）。"
        )
        suffix_box.addWidget(self.cmb_format)

        # 标签自己显示**实际会用的**尾缀：用户填 "edit" 时一眼能看出程序会补 "-"，
        # 填了非法字符也能立刻看到被剔除后的结果。
        self.lbl_suffix = QLabel()
        suffix_tip = (
            "导出文件名 = 原主文件名 + 尾缀。\n"
            "留空 → 默认尾缀 -1（重复导出自动变成 -2、-3）；\n"
            "填 edit → 「文件名-edit.jpg」（再导出会得到 -edit_2.jpg，不会写成 -edit-edit）。\n"
            "这里填的内容会被记住，下次打开程序自动带出。"
        )
        self.lbl_suffix.setToolTip(suffix_tip)
        suffix_box.addWidget(self.lbl_suffix)
        self.edit_suffix = QLineEdit()
        self.edit_suffix.setFixedWidth(110)
        self.edit_suffix.setPlaceholderText("留空 = -1")
        self.edit_suffix.setToolTip(suffix_tip)
        suffix_box.addWidget(self.edit_suffix)

        card.body.addLayout(options_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        # 文件名尾缀放在**按钮这一行的左边**，而不是挤在上一行。
        # 上一行同时排着「色彩空间 / 格式 / 输出质量」三组控件，再加尾缀的话
        # 整行最小宽度需求是 863px，而 1280 窗口只能给它 736px —— 于是每个控件
        # 都被压到最小宽以下，说明文字被**硬裁**（用户反馈的"输出质量后面
        # 显示不全"根子就在这里，光删括号不够）。按钮这一行的左半边本来是空的。
        action_row.addWidget(self._out_suffix_options)
        action_row.addStretch(1)
        self.btn_resume = QPushButton("续跑")
        self.btn_only_failed = QPushButton("只跑失败")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_start = QPushButton("开始")
        self.btn_start.setObjectName("primary")
        action_row.addWidget(self.btn_resume)
        action_row.addWidget(self.btn_only_failed)
        action_row.addWidget(self.btn_stop)
        action_row.addWidget(self.btn_start)
        card.body.addLayout(action_row)

        self.cmb_format.currentIndexChanged.connect(self._on_export_format_changed)
        # textChanged 而不是 editingFinished：用户可能填完就直接点「开始」，
        # 焦点没离开输入框就触发不了 editingFinished，那样偏好就丢了一次。
        self.edit_suffix.textChanged.connect(self._on_suffix_edited)
        # 其余常用选项也一并记住（用户要求：下次打开带出上次的选择）。
        # 模型与风格的回填在 _populate_models / _reload_styles 里做 ——
        # 那时两个下拉还都是空的，没东西可填。
        self.cmb_color_space.currentIndexChanged.connect(self._on_color_space_changed)
        self.slider_quality.valueChanged.connect(self._on_quality_changed)
        self.cmb_style.currentIndexChanged.connect(self._on_style_changed)
        # 按上次的选择预填控件（用户要求：下次打开自动带出）。
        self._apply_preferences_to_widgets()
        # 底栏选项的可用状态要在控件齐备后算一次（「输出」未勾选时后面全灰）。
        self._refresh_output_options_state()

        self._create_tool_actions()

        # 【开始】= 全量重跑：【不跳过】已完成文件，会重新请求 API。
        # 【续跑】= 跳过已完成的，只补跑缺的。
        self.btn_start.setToolTip(
            "全部重新处理：已完成的文件也会重新请求 API。\n"
            "想跳过上次已完成的、只补跑缺的，请用右边的「续跑」。"
        )
        self.btn_resume.setToolTip(
            "跳过已完成的文件（不重复请求 API），只处理尚未完成的。\n"
            "想连已完成的也重跑一遍，请用「开始」。"
        )
        self.btn_only_failed.setToolTip(
            "只处理上次失败过、且没达到永久跳过阈值的文件。"
        )
        self.btn_start.clicked.connect(lambda: self._on_start(resume=False, only_failed=False))
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_resume.clicked.connect(lambda: self._on_start(resume=True, only_failed=False))
        self.btn_only_failed.clicked.connect(lambda: self._on_start(resume=True, only_failed=True))
        return card

    def _build_output_page(self) -> QWidget:
        """输出模式页：风格 / 模型 / 密钥 / 提示词。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("风格"))
        self.cmb_style = QComboBox()
        self.cmb_style.setToolTip(
            "模仿哪种调色风格；不选则用内置的保守默认策略。\n"
            f"{PIN_PREFIX.strip()} = 已置顶，排在列表最前面。\n"
            "在列表里右键任意一条，可以置顶/取消置顶或删除。"
        )
        row.addWidget(self.cmb_style, 1)
        self.btn_reload_styles = QPushButton("刷新")
        # 不写死宽度：写死的是**控件**宽，而文字实际需要多宽取决于字体，
        # 字体一变就会把文字截掉（“显示”“保存”写死 48px 就是这么被截的）。
        # 按钮宽由 QSS 的 padding + 文字实测宽度自动得出。
        row.addWidget(self.btn_reload_styles)
        layout.addLayout(row)
        # 置顶/删除改成"右键列表条目"（用户要求），不再常驻按钮。
        self._install_item_menu(self.cmb_style, "style")

        layout.addWidget(QLabel("想要什么效果（可与风格叠加；留空 = 按风格）"))
        self.txt_prompt = QPlainTextEdit()
        self.txt_prompt.setMinimumHeight(140)
        self.txt_prompt.setMaximumHeight(240)
        self.txt_prompt.setPlaceholderText(
            "用大白话描述这批照片想调成什么样，例如：\n"
            "“海边日落，保留暖调，肤色别过饱和。”\n\n"
            "与风格规则冲突时以这里为准；留空则按风格走（都没选就做保守的统一校正）。"
        )
        layout.addWidget(self.txt_prompt, 1)

        self.btn_reload_styles.clicked.connect(self._reload_styles)
        return page

    def _build_ai_settings(self) -> QWidget:
        """模型 + 密钥：**输出与训练两种模式共用**，所以放在模式页之外。

        为什么必须共用同一份控件，而不是各放一份：
          - 训练模式要调用 AI 归纳风格，**同样需要模型与密钥**
            （见 _on_start 的 TrainOptions 分支，adapter 是必填参数）；
          - 如果把它们放进输出模式页，切到训练模式时整页会被 QStackedWidget 换掉，
            用户看到的是"要选模型、要填密钥，但控件消失"，只能自己猜
            "是不是得先切回输出模式配一次"；
          - 复制两份则是两个真值来源，迟早出现"两边不一致"。
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        row_provider = QHBoxLayout()
        # 说明标签也要留引用：离线调试时整组一起变灰（标签灰了，用户才知道
        # "这一整块都停用了"，而不是以为只是某个下拉坏了）。
        self.lbl_conn_caption = QLabel("连接")
        row_provider.addWidget(self.lbl_conn_caption)
        self.cmb_provider = QComboBox()
        self.cmb_provider.setToolTip(
            "连接 = 端点（base_url）+ 密钥 + 一个备注名。\n"
            "显示成「名字 @ 域名」—— 先看域名，就知道请求实际发给谁。\n"
            "内置的 12 家是**出厂连接**（用它们的官方端点与官方密钥账户名）；\n"
            "中转站 / 本地网关 / 同一家的第二把密钥，点右边的「＋」新建一条连接即可，\n"
            "各自的密钥互不覆盖。\n"
            "在列表里右键任意一条，可以删除该连接。"
        )
        self.cmb_provider.currentIndexChanged.connect(self._on_provider_changed)
        row_provider.addWidget(self.cmb_provider, 1)
        self.btn_new_connection = QPushButton("＋")
        self.btn_new_connection.setToolTip(
            "新建连接。\n"
            "用官方端点：从「预设」里挑一家，地址自动填好。\n"
            "用中转站 / 本地 Ollama / vLLM / 内网网关：预设留空，自己填 base_url ——\n"
            "这些都是 OpenAI 兼容接口，一把 key 能调十几家的模型。"
        )
        self.btn_new_connection.clicked.connect(self._on_new_connection)
        row_provider.addWidget(self.btn_new_connection)
        layout.addLayout(row_provider)
        self._install_item_menu(self.cmb_provider, "connection")

        row_model = QHBoxLayout()
        self.lbl_model_caption = QLabel("模型")
        row_model.addWidget(self.lbl_model_caption)
        self.cmb_model = QComboBox()
        self.cmb_model.setToolTip(
            "AI 模型。下拉里的每一项都是这条连接**真实接受**的模型 id。\n"
            f"预设写在：{config_dir() / 'models.yaml'}（程序提供，升级时自动补齐）\n"
            f"你的连接写在：{config_dir() / 'connections.yaml'}（只由你自己增删）\n"
            f"{PIN_PREFIX.strip()} = 已置顶，排在列表最前面。\n"
            "在列表里右键任意一条，可以置顶/取消置顶或从下拉移除。"
        )
        row_model.addWidget(self.cmb_model, 1)
        # 手动入口：服务商没有 /models 接口、或想用列表外的模型时，点这里填 id。
        self.btn_add_model = QPushButton("＋")
        self.btn_add_model.setToolTip(
            "手动添加模型 id。\n"
            "适用于：服务商没有 /models 接口，或想临时用列表外的模型。\n"
            "填写的 id 会原样传给 API，并记进 discovered_models.json（下次启动还在）。"
        )
        self.btn_add_model.clicked.connect(self._on_add_model_manually)
        row_model.addWidget(self.btn_add_model)
        layout.addLayout(row_model)
        self._install_item_menu(self.cmb_model, "model")

        # 能力红字：**只在“模型看不到图”时出现**（结果精度真的会降）。
        # 输出格式（json_schema / json_object）属于实现细节，只进日志与 tooltip ——
        # 用户明确要求过不要把这块当日志区用。
        #
        # 2026-09-24 追加一类：**你会因此白等/被限流、且只有你能决定**的事
        # （Kimi 账户档位 → 限速），见 _kimi_tier_hint_html()。它必须是可点的，
        # 因为程序猜不出你的档位（官方没有查档位的接口）。
        self.lbl_model_hint = QLabel("")
        self.lbl_model_hint.setWordWrap(True)
        self.lbl_model_hint.setStyleSheet("color: #B00020;")
        # 富文本 + 链接：默认是纯文本、点了没反应 —— 必须显式打开链接交互，
        # 并且**不要** setOpenExternalLinks（那会把 href 当网址丢给浏览器）。
        self.lbl_model_hint.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.lbl_model_hint.setOpenExternalLinks(False)
        self.lbl_model_hint.linkActivated.connect(self._on_model_hint_link)
        layout.addWidget(self.lbl_model_hint)

        row_key = QHBoxLayout()
        self.lbl_key_caption = QLabel("密钥")
        row_key.addWidget(self.lbl_key_caption)
        self.edit_key = SecretLineEdit()
        row_key.addWidget(self.edit_key, 1)
        self.btn_show_key = QPushButton("显示")
        self.edit_key.attach_toggle(self.btn_show_key)
        row_key.addWidget(self.btn_show_key)
        self.btn_save_key = QPushButton("保存")
        self.btn_save_key.setToolTip(
            "把输入的密钥保存到系统密钥链（Windows 凭据管理器）。\n"
            "密钥绝不写入任何配置文件；程序只能掩码显示它，你自己也无法从界面读出明文。"
        )
        row_key.addWidget(self.btn_save_key)
        # 「删除」：让用户能管理已保存的密钥（用户明确要求：删除需要二次确认）。
        # 放在输入框同一行，是因为它是"这个账户的密钥"上的动词，
        # 与「保存」「显示」属于同一件事的三个动作。
        self.btn_delete_key = QPushButton("删除")
        self.btn_delete_key.setToolTip(
            "删除当前连接已保存的密钥（会先弹确认框）。\n"
            "删除的是系统密钥链里该账户的条目 + 本次会话内存里的值；\n"
            "⚠ 删除**不影响**你在系统里设置的同名环境变量 —— 若存在，程序会继续读它，\n"
            "  此时提示里会明确告诉你「要彻底停用请去删那个环境变量」。"
        )
        row_key.addWidget(self.btn_delete_key)
        layout.addLayout(row_key)

        self.lbl_key_status = QLabel("")
        self.lbl_key_status.setObjectName("hintLabel")
        self.lbl_key_status.setWordWrap(True)
        layout.addWidget(self.lbl_key_status)

        self.cmb_model.currentIndexChanged.connect(self._on_model_changed)
        self.btn_save_key.clicked.connect(self._on_save_key)
        self.btn_delete_key.clicked.connect(self._on_delete_key)
        return box

    def _build_train_page(self) -> QWidget:
        """训练模式页：说明 + 风格命名。

        【为什么这里很短 —— 用户两次反馈】
        1. 命名框以前是个 120–240px 高的多行 QPlainTextEdit，而它**只用来起个名字**，
           于是占满一页、还和上下两句说明挤在一起（文字看着像重叠）。
          → 改成单行 QLineEdit，高度交给它自己。
        2. “样本少于 N 张会提示… ”这一类**行为说明**不该常驻界面：
           真发生时程序会弹提示 + 写日志，把规则事先贴在脸上只会变成噪声。
          → 界面只留“你要做什么”（放成对样本、起个名字），规则留在代码与文档里。
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(8)

        info = QLabel(
            "训练模式：添加若干「RAW + 它的 XMP」成对样本，程序会读你已有的调色设置，\n"
            "区分“你自己调的”和“相机/镜头默认的”，归纳成你的专属风格。"
        )
        info.setWordWrap(True)
        info.setObjectName("hintLabel")
        layout.addWidget(info)

        layout.addWidget(QLabel("给这个风格起个名字"))
        # 单行输入：它只负责命名（用户明确要求“没有必要这么大”）。
        self.edit_style_name = QLineEdit()
        self.edit_style_name.setPlaceholderText("例如「海边暖调人像」")
        layout.addWidget(self.edit_style_name)
        layout.addStretch(1)

        note = QLabel(
            "训练和输出共用上方的「模型 / 密钥」设置，配一次即可（训练同样要调用 AI）。\n"
            "保存后可在输出模式里直接选用；结果存为 styles/<风格名>.json"
            "（想打开这个目录：文件 → 打开风格文件夹）。"
        )
        note.setWordWrap(True)
        note.setObjectName("hintLabel")
        layout.addWidget(note)
        return page

    # --- 菜单栏与隐藏区 -----------------------------------------------------

    def _build_menu_bar(self) -> None:
        """左上角菜单栏：文件 / 外观 / 安全 / 高级设置 / 日志 / 帮助。

        分组原则（用户 2026-09-24 明确要求，之前把"运维型"动作全塞进「工具」，
        结果"打开输出目录"和"删除密钥"挤在同一个菜单里，危险动作与日常动作混在一起）：
          · 文件     —— 打开**产物**目录（输出目录 / 风格文件夹）；
          · 外观     —— 主题；
          · 安全     —— 碰凭据的动作单独一组（删除密钥是危险动作，不该混在别处）；
          · 高级设置 —— 设置类动作（打开设置 / 恢复出厂设置 / 重新自检）；
          · 日志     —— 排障时会用到的一整组（看日志 / 打开日志目录 / 清失败记录 / 清缓存）。

        ⚠ 「打开脚本目录」已**不在菜单里**（用户裁决）：它只对"自动导出失败、需要手动补跑"
        这一个场景有意义，而那个场景下完成对话框里会**当场给出**按钮（见 _show_completion_dialog）。
        把入口从菜单里去掉，是为了别让一个几乎只用一次的目录混进日常菜单。
        """
        bar = self.menuBar()

        m_file = bar.addMenu("文件")
        m_file.addAction("打开输出目录", self.btn_open_output.click)
        m_file.addAction("打开风格文件夹", self.btn_open_styles.click)

        m_appear = bar.addMenu("外观")
        self._act_light = m_appear.addAction("浅色模式")
        self._act_dark = m_appear.addAction("深色模式")
        self._act_light.setCheckable(True)
        self._act_dark.setCheckable(True)
        self._act_light.setChecked(True)
        group = QActionGroup(self)
        group.addAction(self._act_light)
        group.addAction(self._act_dark)
        self._act_light.triggered.connect(lambda: self._apply_theme(dark=False))
        self._act_dark.triggered.connect(lambda: self._apply_theme(dark=True))

        # 密钥删除管理：连接有 12 条、每条一个密钥槽，逐个切连接再删太麻烦，
        # 所以给一个"列出所有已保存的密钥"的入口（每条删前都要确认）。
        m_safe = bar.addMenu("安全")
        m_safe.addAction("删除已保存的密钥…", self._on_manage_keys)

        m_adv = bar.addMenu("高级设置")
        m_adv.addAction("打开高级设置…", self._open_advanced_dialog)
        m_adv.addAction("恢复出厂设置…", self._on_restore_factory_settings)
        m_adv.addAction("重新自检", self.btn_recheck.click)

        m_log = bar.addMenu("日志")
        m_log.addAction("查看运行日志…", self._open_log_dialog)
        m_log.addAction("打开日志目录", self.btn_open_logs.click)
        m_log.addAction("清除失败记录", self.btn_clear_failures.click)
        m_log.addAction("清除缩略图缓存", self.btn_clear_cache.click)

        m_help = bar.addMenu("帮助")
        m_help.addAction("关于", self._show_about)

    def _create_tool_actions(self) -> None:
        """创建那些"只由菜单触发"的按钮（不放进任何布局）。

        保留为实例属性，是因为运行逻辑（_set_running、_on_output_finished 等）
        与自检脚本都直接引用它们；菜单里的动作只是转发一次 click()。
        """
        self.btn_recheck = QPushButton("重新自检", self)
        self.btn_clear_failures = QPushButton("清除失败记录", self)
        self.btn_clear_cache = QPushButton("清除缩略图缓存", self)
        self.btn_open_output = QPushButton("打开输出目录", self)
        self.btn_open_script = QPushButton("打开脚本目录", self)
        self.btn_open_logs = QPushButton("打开日志目录", self)
        self.btn_open_styles = QPushButton("打开风格文件夹", self)

        # 必须显式 hide()：它们是以主窗口为父对象创建的普通子控件，
        # 不隐藏就会以默认尺寸画在窗口左上角（盖在照片卡片上），
        # 菜单调用的只是它们的 click() 信号。
        for btn in (self.btn_recheck, self.btn_clear_failures, self.btn_clear_cache,
                    self.btn_open_output, self.btn_open_script, self.btn_open_logs,
                    self.btn_open_styles):
            btn.hide()

        self.btn_recheck.clicked.connect(self._check_environment)
        self.btn_clear_failures.clicked.connect(self._on_clear_failures)
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        self.btn_open_output.clicked.connect(self._on_open_output)
        self.btn_open_script.clicked.connect(self._on_open_script)
        self.btn_open_logs.clicked.connect(lambda: self._open_path(logs_dir()))
        # 用户要"用资源管理器看一眼 / 备份一份 / 手改 models.yaml"时得有个直达入口。
        # styles 目录里是训练出来的风格，config 目录里就是 models.yaml（模型与接口地址）。
        # 这两个目录都允许被清空重建，所以这里不做存在性检查，直接 mkdir + 打开。
        self.btn_open_styles.clicked.connect(lambda: self._open_path(styles_dir()))

        # 尚未跑过任务之前，这些"打开"按钮没有目标。
        self.btn_open_output.setEnabled(False)
        self.btn_open_script.setEnabled(False)
        self.btn_open_script.setToolTip(
            "打开本次运行的脚本目录（manifest.json / export_batch.jsx / run_export.bat）。\n"
            "注意：关闭程序时该目录会被自动清理，需要手动补跑请先操作。"
        )

    def _build_advanced_dialog(self) -> QDialog:
        """高级设置对话框（像 PS 的「首选项」，日常用不到，留默认即可）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("高级设置")
        dialog.setMinimumWidth(560)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        hint = QLabel("以下选项都有安全默认值，日常使用无需改动。")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 「输出位置」（输出目录 / _export 子目录）曾经在这里，
        # 现在搬到了主界面的输出模式页：它是每次导出都要看一眼的东西，
        # 藏在“高级设置”里会被当成“程序不支持自选输出位置”。

        # --- 性能 ---
        perf_title = QLabel("性能")
        perf_title.setObjectName("cardTitle")
        layout.addWidget(perf_title)

        row_perf = QHBoxLayout()
        row_perf.addWidget(QLabel("同时处理几张"))
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 16)
        self.spin_workers.setValue(DEFAULT_WORKERS)
        self.spin_workers.setToolTip(
            f"默认 {DEFAULT_WORKERS}。带图请求每张要上万 token：并发越高越容易撞上限流或超时"
            "（qwen / GLM / Kimi 这类限制严、响应慢的模型尤其明显），"
            "表现为「跑完有几张红」而不是更快。\n"
            "程序遇到限流或超时也会**自动降一半**，但那是事后补救；"
            "要快可以自己调大，要稳就保持默认或调到 1。"
        )
        row_perf.addWidget(self.spin_workers)
        row_perf.addWidget(QLabel("最大请求数"))
        self.spin_max_requests = QSpinBox()
        self.spin_max_requests.setRange(0, 100000)
        self.spin_max_requests.setValue(0)
        self.spin_max_requests.setToolTip(
            f"0 表示自动：文件数 × {BUDGET_MULTIPLIER}（含重试）。"
            "超出上限会停止并告警，防止失控耗费。"
        )
        row_perf.addWidget(self.spin_max_requests)
        row_perf.addStretch(1)
        layout.addLayout(row_perf)

        self.chk_use_cache = QCheckBox("使用缩略图缓存（命中则跳过提取与压缩）")
        self.chk_use_cache.setChecked(True)
        layout.addWidget(self.chk_use_cache)

        self.chk_high_fidelity = QCheckBox(
            "高保真模式：跳过嵌入式预览，直接用 rawpy 解码（慢，但更接近 ACR 的中性观感）"
        )
        layout.addWidget(self.chk_high_fidelity)

        # --- 行为 ---
        act_title = QLabel("行为")
        act_title.setObjectName("cardTitle")
        layout.addWidget(act_title)

        self.chk_dng_sidecar = QCheckBox(
            "DNG 也生成旁侧 .xmp 文件（默认写入文件内部；勾选此项可能被 ACR 忽略）"
        )
        layout.addWidget(self.chk_dng_sidecar)

        # 「离线调试」= 原「离线模拟」+ 原「演练模式」合并成一个开关（用户要求）。
        #
        # 【为什么要合并】它们本来就是同一个意图的两半："在不烧钱、不碰整批照片的前提下，
        # 把整条链路走通看一遍"。分成两个勾选框时，很容易只勾一个，得到半吊子状态：
        #   · 只勾离线模拟 → 不烧钱，但 100 张全跑（写满一堆假参数）；
        #   · 只勾演练模式 → 只跑 3 张，但真的在调 API（每一步都在花钱）。
        # 合并之后只有"是否进入调试态"这一个问题，两个行为一起生效。
        #
        # 语义（写清楚，免得"置灰但没生效"这类老问题重演）：
        #   · 不联网、不需要密钥（走 OfflineAdapter，结果全是写死的假数据）；
        #   · 只处理前 3 张；出片目录走 _export_dryrun（与正式产物绝不混放）；
        #   · **仍然会写 XMP**（用户明确要保留这条验证路径），所以内容都是假的，
        #     绝不能用于交付 —— 日志与每条结果的说明里都会标注「[离线调试]」。
        self.chk_offline = QCheckBox(
            f"离线调试：不调用 AI、只处理前 {DRY_RUN_LIMIT} 张，用写死的假结果验证链路"
        )
        self.chk_offline.setToolTip(
            "调试用：勾选后不会发出任何网络请求，也不需要 API 密钥或连接。\n"
            f"只处理前 {DRY_RUN_LIMIT} 张，出片落到 _export_dryrun（与正式产物分开）。\n"
            "仍然会写 XMP，但里面的参数全是假数据，切勿用于交付。\n"
            "日志与每条结果的说明里都会标注「[离线调试]」。\n"
            "勾选期间，「连接 / 模型 / 密钥」整组会停用 —— 它们对调试结果没有任何影响。"
        )
        self.chk_offline.toggled.connect(self._refresh_ai_options_state)
        layout.addWidget(self.chk_offline)

        row_close = QHBoxLayout()
        row_close.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        row_close.addWidget(btn_close)
        layout.addLayout(row_close)

        return dialog

    def _open_advanced_dialog(self) -> None:
        self._adv_dialog.exec()

    def _build_log_dialog(self) -> QDialog:
        """日志查看对话框（日常隐藏，排障时从「日志」菜单打开）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(760, 480)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_open_dir = QPushButton("打开日志目录")
        btn_open_dir.clicked.connect(lambda: self._open_path(logs_dir()))
        row.addWidget(btn_open_dir)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        row.addWidget(btn_close)
        layout.addLayout(row)

        return dialog

    def _open_log_dialog(self) -> None:
        self._log_dialog.exec()

    def _calibrate_color_space_width(self) -> None:
        """把色彩空间下拉的最小宽压到"最长选项 + 箭头"的实测值。

        `QComboBox` 默认的最小宽是按**最长选项**算的（实测 237px）。底栏那一行
        同时排着三个标签 + 两个下拉 + 说明文字 + 滑块，按默认值算整行最小宽会
        到 897px，超过 1280 窗口能给它的 736px —— 于是每个控件都被压到最小宽
        以下，说明文字被**硬裁**。用户反馈的"XX 质量后面显示不全"就是这么来的。
        这里实测一个够用的最小值，把空间还给说明文字。
        """
        metrics = QFontMetrics(self.cmb_color_space.font())
        longest = max(COLOR_SPACE_CHOICES, key=metrics.horizontalAdvance)
        # +48：下拉箭头 + 左右内边距 + 边框的余量（QSS 里给的是 6px 内边距加圆角）。
        self.cmb_color_space.setMinimumWidth(metrics.horizontalAdvance(longest) + 48)

    def _apply_theme(self, dark: bool) -> None:
        """切换浅色 / 深色，并记住选择。"""
        from PyQt6.QtWidgets import QApplication

        self._dark = dark
        apply_theme(QApplication.instance(), dark)
        # 主题里的 font-family 会换字体，而底栏那几个宽度都是按字体**实测**出来的，
        # 所以换完主题必须重新校准一次，否则一换主题就重新出现文字被截断。
        self._calibrate_color_space_width()
        self.slider_quality.refresh_caption()
        self._act_light.setChecked(not dark)
        self._act_dark.setChecked(dark)
        self._remember("theme", "dark" if dark else "light")

    def _enable_transparent_root(self) -> None:
        """Mica 生效后，把中央控件背景设透明，让云母从卡片缝隙透出来。"""
        central = self.centralWidget()
        if central is not None:
            central.setStyleSheet("#appRoot { background: transparent; }")

    # ========================================================================
    # 日志桥
    # ========================================================================

    def _logging_callback(self, message: str, level: int) -> None:
        """由 logging handler 在工作线程中调用。

        这里**只发信号**，不触碰控件——信号会被 Qt 排队到主线程，
        由 _on_log_bridge 在主线程序列里执行（硬约束 #9）。
        """
        self.sig_log_bridge.emit(message, int(level))

    def _on_log_bridge(self, message: str, level: int) -> None:
        """主线程槽：把日志写进界面并同步状态栏。"""
        self.log_panel.append(message, level)
        self.statusBar().showMessage(message.replace("\n", " ")[:160])

    # ========================================================================
    # 环境自检
    # ========================================================================

    def _check_environment(self) -> None:
        """检查 exiftool / Photoshop / keyring / 模型配置 / 风格目录。"""
        problems: list[str] = []
        badges: list[str] = []

        # --- exiftool（硬约束 #10：启动时必须检测）---
        self._exiftool = ExiftoolRunner()
        if self._exiftool.available:
            badges.append(f"exiftool {self._exiftool.status.version}")
            log.info("exiftool 可用：%s", self._exiftool.status.path)
        else:
            problems.append("exiftool")
            log.warning("exiftool 不可用。%s", self._exiftool.status.install_guide())

        # --- Photoshop ---
        ps_ok, ps_message = photoshop.probe(refresh=True)
        if ps_ok:
            badges.append("Photoshop 可用")
        else:
            problems.append("Photoshop")
            log.warning("%s\n%s", ps_message, photoshop.manual_guidance(None))

        # --- keyring ---
        if self._key_store.keyring_available:
            badges.append("密钥链可用")
        else:
            problems.append("密钥链")
            log.warning(self._key_store.unavailable_message())

        # --- 模型配置 ---
        try:
            self._models_config = load_models_config()
        except AcbError as exc:
            problems.append("模型配置")
            log.error(str(exc))
            self._models_config = None
        else:
            self._populate_models()

        # --- 风格目录 ---
        ensure_seed_styles()
        self._reload_styles()

        # --- ICC 资源 ---
        # 补齐程序自己生成的 sRGB / Adobe RGB 等效 profile（随包分发的那两份在
        # assets/icc/ 里；这里负责"文件缺失或目录只读"时的自愈）。
        # 它内部不抛异常：色彩管理绝不能拦住程序启动。
        try:
            for icc_file in ensure_icc_assets():
                log.info("已补齐 ICC 资源：%s", icc_file)
            # 把"哪一份 profile 最终被采用"写进日志：出片颜色不对时，
            # 第一个要看的就是这一行（而不是靠猜）。
            icc_report = describe_optional_profiles()
            if "缺失" in icc_report:
                log.warning("色彩管理资源不齐全：\n%s", icc_report)
            else:
                log.debug("色彩管理资源齐全：\n%s", icc_report)
        except Exception as exc:                     # pragma: no cover - 兜底
            log.warning("检查 ICC 资源失败（不影响运行）：%s", exc)

        # --- 断点状态 ---
        stats = self._job_state.stats()
        log.info(
            "断点状态：已完成 %d 个，失败 %d 个（其中永久跳过 %d 个）。",
            stats["done"],
            stats["failed"],
            stats["permanent_skip"],
        )

        text = " | ".join(badges) if badges else "环境自检完成"
        if problems:
            text += f"　⚠ 需处理：{'、'.join(problems)}（详见日志）"
        self.lbl_env.setText(text)
        self.lbl_env.setStyleSheet("color: #B00020;" if problems else "color: #0A7A0A;")

        # 最后统一把"离线调试"的联动状态算一次：高级设置里的那个勾选框
        # 到这一步才存在（_build_adv_dialog 在 _build_ui 里建）。
        # 启动时它是未勾选的，所以这里主要是把风格下拉里"内置基准风格不可选"
        # 这件事确定下来，而不是靠 _reload_styles 里那次早期调用碰巧算对。
        self._refresh_ai_options_state()

    @staticmethod
    def _connection_display(name: str, base_url: str) -> str:
        """连接在下拉里的显示文本：「名字 @ 域名」。

        为什么把域名也显示出来：出错的根源往往是"请求发到了哪"，
        而"服务商"这个名字（比如 DeepSeek）在用户把 base_url 指向中转站之后
        就已经是**假信息**了。域名不会撒谎。
        """
        host = base_url.split("//", 1)[-1].split("/", 1)[0] or base_url
        return f"{name} @ {host}"

    def _populate_providers(self) -> None:
        """把**连接**填进「连接」下拉（保留当前选择）。

        方法名沿用旧名：偏好文件里的模型键（"deepseek::deepseek-v4-pro"）与
        整套自检都建立在"连接 id == 出厂服务商 id"这个对应关系上，不改名可以
        让已有的偏好与测试继续有效。
        """
        assert self._models_config is not None
        current = self._current_provider()
        # 还没选过时，按"上次记住的模型"推导它属于哪条连接（下次打开带出）。
        if current is None:
            remembered = str(self._prefs.get("model") or "") or None
            if remembered:
                current = self._models_config.provider_of(remembered)
        self.cmb_provider.blockSignals(True)
        self.cmb_provider.clear()
        for key, conn in self._models_config.connections.items():
            name = conn.name or PROVIDER_LABELS.get(key, conn.label) or key
            self.cmb_provider.addItem(self._connection_display(name, conn.base_url), key)
        idx = self.cmb_provider.findData(current) if current else -1
        self.cmb_provider.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_provider.blockSignals(False)

    def _current_provider(self) -> str | None:
        """当前「服务商」下拉选中的 provider id。"""
        if not hasattr(self, "cmb_provider"):
            return None
        data = self.cmb_provider.currentData()
        return str(data) if data else None

    def _on_provider_changed(self) -> None:
        """切换服务商：模型下拉只显示该服务商的模型，并刷新密钥状态。"""
        if self._models_config is None:
            return
        self._populate_models()

    def _populate_models(self) -> None:
        """填充模型下拉：只显示当前服务商的模型；隐藏项不出现，置顶项排前面。"""
        assert self._models_config is not None

        self._populate_providers()
        provider = self._current_provider()

        hidden = set(preferences.get_list("hidden_models"))
        pinned = preferences.get_list("pinned_models")
        entries = [
            (key, label)
            for key, label in self._models_config.choices()
            if key not in hidden
            and (provider is None or self._models_config.provider_of(key) == provider)
        ]
        by_key = dict(entries)
        # 置顶项按"用户点置顶的顺序"排前面，其余按配置文件里的顺序跟在后面。
        #
        # ⚠ 第二个条件必须是 `k not in ordered_keys`（或等价的 `k not in pinned`），
        # **不能**是 `k not in by_key`。曾经写错成后者：by_key 正是从 entries 建出来的，
        # 所以那个条件对每一项都为假 → 这个列表恒为空 →
        # **除非先置顶过某个模型，否则下拉里一个模型都不显示**。
        # 后果：用户打开程序看到空的「模型」下拉，既选不了模型也存不了密钥，
        # 而且什么都不报错 —— 因为配置里的 active 仍然生效，
        # CLI 与真机端到端测试全都正常，只有界面这一条路是坏的。
        ordered_keys = [k for k in pinned if k in by_key]
        # 再把"这个连接在配置里声明的默认模型"按**配置里的顺序**排前面。
        #
        # 为什么需要这一段：下面的兜底顺序是 choices()（**按条目名字母序**），
        # 而字母序跟"推荐用哪个"完全无关 ——
        # `qwen-vl-max` 会排在 `qwen3-vl-plus` 前面（'-' 的字符码小于 '3'），
        # 于是切到「千问」时自动选中那个更旧的模型，
        # 而配置里明明把 Qwen3-VL 系列写在前面并注明了官方依据。
        # 后果不是报错，而是用户以为什么都没发生：
        # 程序照旧能用，只是用的模型比配置推荐的旧一代。
        if provider is not None:
            conn = self._models_config.connections.get(provider)
            for model_id in (conn.default_models if conn is not None else []):
                key = f"{provider}::{model_id}"
                if key in by_key and key not in ordered_keys:
                    ordered_keys.append(key)
        # 其余（拉取到的模型列表可能上百条）按"代次从新到旧、同代次按名字"排。
        # 不能用字母序：'-' 的码点小于 '3'，`qwen-vl-max` 会跑到 `qwen3-vl-plus`
        # 前面（静默选中旧一代），而带上版本号的名字以后只会越来越多。
        # 规则与理由见 config.model_sort_key。
        ordered_keys += sorted(
            (k for k, _ in entries if k not in ordered_keys),
            key=lambda key: model_sort_key(key.split("::", 1)[-1]),
        )

        pinned_set = set(pinned)
        # 刷新前先把当前选中项记下来（下面 clear() 之后就问不到了）。
        previous = self._current_model_key()
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        for key in ordered_keys:
            row = self.cmb_model.count()
            marked = key in pinned_set
            self.cmb_model.addItem(
                f"{PIN_PREFIX}{by_key[key]}" if marked else by_key[key], key
            )
            if marked:
                # 置顶标识 = ★ 前缀 + 加粗（为何两者并用见 widgets.PIN_PREFIX 的注释）
                self.cmb_model.setItemData(
                    row, pinned_font(self.cmb_model.font()), Qt.ItemDataRole.FontRole
                )
            # 悬停提示：这一项是哪个连接、走哪个 base_url、输出/上下文上限各是多少 ——
            # 下拉里只显示模型 id，不靠 tooltip 用户分不清 deepseek-flash 和 gpt-4o 是哪家的；
            # 上限放在这里是因为它直接决定“单次能不能装下结果”与“会花多少钱”。
            spec = self._models_config.models.get(key)
            if spec is not None:
                conn_name = self._models_config.connection_name(key)
                caps = "视觉=支持" if spec.supports_vision else "视觉=不支持（走 caption 降级）"
                if spec.supports_json_schema:
                    fmt = "json_schema"
                elif getattr(spec, "supports_json_object", False):
                    fmt = "json_object"
                else:
                    fmt = "无（提示词 + 本地校验）"
                self.cmb_model.setItemData(
                    row,
                    f"{conn_name} · {spec.base_url}\n"
                    f"model id: {spec.model}\n请求形状: {spec.api_style}\n"
                    f"{caps}；输出格式: {fmt}\n"
                    f"输出上限: {spec.max_output_tokens} token；上下文: {spec.max_context} token",
                    Qt.ItemDataRole.ToolTipRole,
                )
        # 选中优先级：
        #   1) 刷新前的选中项（用户手动选过，要在刷新/置顶/删除后保留）
        #   2) 上次记住的模型（启动时就是靠这一条"带出上次的选择"）
        #   3) models.yaml 里的 active
        #   4) 第一项
        # 每一档都要 findData 判存在：模型可能被隐藏、或从配置里删掉了。
        remembered = str(self._prefs.get("model") or "") or None
        index = -1
        for candidate in (previous, remembered, self._models_config.active):
            if candidate:
                index = self.cmb_model.findData(candidate)
                if index >= 0:
                    break
        if index < 0 and provider is not None:
            # 兜底也别用"列表第一项"：那一项是字母序的偶然结果。
            # first_model_of_provider 会优先挑**支持视觉**的那个（见它的 docstring），
            # 这正是本工具所有链路的前提。
            first = self._models_config.first_model_of_provider(provider)
            if first is not None:
                index = self.cmb_model.findData(first[0])
        if index < 0:
            index = 0
        if index >= 0:
            self.cmb_model.setCurrentIndex(index)
        self.cmb_model.blockSignals(False)

        if hidden:
            log.info(
                "已按你的设置隐藏 %d 个模型（高级设置 → 恢复出厂设置 可找回）。",
                len(hidden),
            )
        self._on_model_changed()

    def _model_to_select_after_refresh(self, provider_key: str) -> tuple[str, object] | None:
        """拉取/保存密钥后该选中哪个模型 —— **先尊重用户已选的那个**。

        为什么不能无条件自动切换到“能看图的那个”：
        用户可能就是为了便宜、为了纯文本、为了别的特性而故意选了 V4 Pro。
        每次拉取模型都把选择改回去，用户只会觉得“我选的模型怎么老是被换掉”，
        而且很难排查。所以规则是：
          1. 当前选中的模型仍属于这条连接且在列表里 → **保留不动**；
          2. 否则（没选过 / 选中的已被删掉 / 切到别的连接）才用
             `first_model_of_provider`（它会优先挑支持视觉的那个）。
        """
        if self._models_config is None:
            return None
        current = self._current_model_key()
        if current:
            spec = self._models_config.models.get(current)
            if spec is not None and spec.provider == provider_key:
                return current, spec
        return self._models_config.first_model_of_provider(provider_key)

    def _current_model_key(self) -> str | None:
        data = self.cmb_model.currentData()
        return str(data) if data else None

    def _current_spec(self):
        if self._models_config is None:
            return None
        key = self._current_model_key()
        if key is None:
            return None
        return self._models_config.models.get(key)

    def _on_model_changed(self) -> None:
        """模型切换时刷新密钥状态（不同模型用不同的 key_env）。"""
        # 顺手记住选中的模型（下次打开自动带出）。
        # 只在配置确实加载成功时记：配置读失败时下拉是空的，
        # 此时写回空值会把手里的有效选择白白抹掉。
        if self._models_config is not None:
            self._remember("model", self._current_model_key() or "")

        spec = self._current_spec()
        if spec is None:
            # 两种"空"要分开说：配置根本没读起来 vs 读起来了但一条连接都没有模型。
            if self._models_config is not None and not self._models_config.models:
                self.lbl_key_status.setText(
                    "还没有可用模型：在「连接」里选一条、填上密钥保存，"
                    "程序会自动拉取它能调用的模型；也可以点右边的「＋」新建连接。"
                )
            else:
                self.lbl_key_status.setText("未加载模型配置")
            self.lbl_key_status.setStyleSheet("color: #B00020;")
            if hasattr(self, "lbl_model_hint"):
                self.lbl_model_hint.clear()
            return

        existing = self._key_store.get(spec.key_env)
        if existing:
            source = "系统密钥链" if self._key_store.keyring_available else "环境变量"
            dup = self._duplicate_key_hint(spec)
            self.lbl_key_status.setText(
                f"已找到密钥 {mask(existing)}（来源：{source}，账户名 {spec.key_env}）。"
                "如需替换，请在输入框中填入新值后点「保存」。" + dup
            )
            self.lbl_key_status.setStyleSheet("color: #B26A00;" if dup else "color: #0A7A0A;")
            self.edit_key.show_masked_placeholder(mask(existing))
            if dup:
                log.warning(
                    "连接「%s」与其它连接共用了同一把密钥（账户 %s）——%s",
                    self._models_config.connection_name(self._current_model_key() or ""),
                    spec.key_env,
                    dup.strip(),
                )
        else:
            hint = "（点击「保存」写入系统密钥链）" if self._key_store.keyring_available else (
                "（密钥链不可用，本次会话仅在内存中保存，重启需重输）"
            )
            self.lbl_key_status.setText(
                f"未找到密钥（账户名 {spec.key_env}）。请填写后保存。{hint}"
            )
            self.lbl_key_status.setStyleSheet("color: #B26A00;")

        # 能力提示（红字）：**只留“会让结果变差、而你必须知情”的那一条**。
        #
        # 用户反馈（两轮）：不要把这个红字区当日志用。规则：
        #   · 看不到图 → 精度真的会降，与用户期望相反 → **必须红字**；
        #   · 不支持 json_schema → 只是实现细节（程序已用 json_object +
        #     本地强校验兜底，用户无需做任何事），读起来却像“你的模型有缺陷”，
        #     而这是**服务端**的能力、用户无能为力 → 只进日志。
        #     （能力详情本来就已经写在模型下拉的 tooltip 里。）
        # 实测事实（DeepSeek 官方页面）：deepseek-v4-pro 不支持图像理解，
        # deepseek-flash 支持 —— 同目录下同模不同能力，所以判断必须看**模型**，
        # 不能只看服务商（见 config.ModelOverride）。
        caps = []
        if not spec.supports_vision:
            caps.append(
                "该模型不支持图片输入，将走 caption 降级通道（只看本地统计量，"
                "精度低于看图判断）"
            )
        # Kimi 档位提示：**每次切到 Kimi 都重新算一遍**，不记"已忽略"——
        # 用户可能今天才充值升级，那时他要看到"改回 Tier1"那个入口。
        tier_html = self._kimi_tier_hint_html(spec)
        if hasattr(self, "lbl_model_hint"):
            if caps or tier_html:
                # 用 <br> 拼富文本：只要含一个标签，QLabel 就按富文本渲染，
                # 纯文本那几段（caps）照样正常显示。
                self.lbl_model_hint.setText("<br>".join(caps + ([tier_html] if tier_html else [])))
            else:
                self.lbl_model_hint.clear()

        # 输出格式那条只写日志（排查格式问题时看日志，或悬停在模型下拉上看 tooltip）。
        if not spec.supports_json_schema:
            using = (
                "json_object + 本地强校验"
                if getattr(spec, "supports_json_object", False)
                else "提示词 + 本地强校验"
            )
            log.info("模型「%s」不支持 json_schema，将用 %s。", spec.label, using)
        if caps:
            log.warning("模型「%s」：%s", spec.label, "；".join(caps))
        if tier_html:
            log.info(
                "Kimi 档位：当前按 %s 限速（并发 %s / 每分钟 %s 次）。",
                "Tier0" if (spec.max_concurrency, spec.max_rpm) == KIMI_TIER_LIMITS["tier0"]
                else "Tier1",
                spec.max_concurrency if spec.max_concurrency is not None else "不限",
                spec.max_rpm if spec.max_rpm is not None else "不限",
            )

    def _kimi_tier_hint_html(self, spec) -> str:
        """Kimi 档位提示（富文本）；不是 Kimi 就返回空串。

        【为什么必须存在】官方按**累计充值额**限速：Tier0（未充值）并发 1 /
        RPM 3，Tier1（¥50）并发 15 / RPM 100。而官方**没有**任何查询档位的
        接口（实测：/v1/users/me/balance 只回三个余额字段；对话响应里也没有
        ratelimit 头），程序不可能替你判断。于是：
          · 出厂默认写 Tier1 —— 否则充过钱的用户白等（实测 Tier0 下 4 并发
            18 张只成 1 张，其余全是 429）；
          · 未充值的用户点一下即切 Tier0，不必先自己撞几次 429 才知道。

        【为什么每次切到 Kimi 都要重新显示】用户可能今天才充值升级，
        那时他要看到“改回 Tier1”那个入口 —— 所以这里**不记**任何
        “已忽略”状态，完全由当前生效的限速值决定显示哪一种。

        【为什么不直接读账户】因为读不到（见上）。只认两个值：恰好等于
        Tier0 的额度 → 认为他选了 Tier0；其余一律按 Tier1 提示。
        """
        if not self._is_kimi_spec(spec):
            return ""
        t0 = KIMI_TIER_LIMITS["tier0"]
        t1 = KIMI_TIER_LIMITS["tier1"]
        if (spec.max_concurrency, spec.max_rpm) == t0:
            return (
                f"当前按 Kimi <b>Tier0</b> 限速（并发 {t0[0]} / 每分钟 {t0[1]} 次）。"
                f"若你已充值升级（Tier1 起：并发 {t1[0]} / 每分钟 {t1[1]} 次），"
                f'<a href="kimi-tier1">点此改回 Tier1</a>。'
            )
        return (
            f"注意：Kimi 按「累计充值额」分档 —— 未充值的账户（Tier0）只允许 "
            f"<b>并发 {t0[0]} / 每分钟 {t0[1]} 次</b>，而当前按 Tier1"
            f"（并发 {t1[0]} / 每分钟 {t1[1]} 次）跑，会反复 429。"
            f'请注意，若权益为 Tier0，<a href="kimi-tier0">请点击此处切换为 Tier0</a>。'
        )

    @staticmethod
    def _is_kimi_spec(spec) -> bool:
        """这个模型是不是走 Kimi（自建连接指向 api.moonshot.cn 也算）。"""
        if getattr(spec, "provider", "") == KIMI_PROVIDER_ID:
            return True
        base = str(getattr(spec, "base_url", "") or "").lower()
        return "moonshot" in base or "kimi.com" in base

    def _on_model_hint_link(self, href: str) -> None:
        """红字里的链接被点击（目前只有 Kimi 档位那两个）。"""
        if href not in ("kimi-tier0", "kimi-tier1"):
            log.info("忽略了未知的提示链接：%s", href)
            return
        spec = self._current_spec()
        if spec is None or self._models_config is None:
            return
        tier = "tier0" if href == "kimi-tier0" else "tier1"
        concurrency, rpm = KIMI_TIER_LIMITS[tier]
        conn_id = self._current_provider() or spec.provider
        try:
            path = set_endpoint_limits(
                self._models_config.providers,
                conn_id,
                concurrency=concurrency,
                rpm=rpm,
            )
            # 同时作废上次从 429 学到的限流值：否则"配置与记忆取更严"会把刚选的
            # Tier1 又压回 1 个并发 —— 用户看到的就是"点了没用"。
            # 他若选错了（账户其实仍是 Tier0），下一个 429 会重新学回来（自愈）。
            forgotten = forget_endpoint_limit(
                spec.base_url, reason=f"用户在界面上选择了 {tier.upper()}"
            )
        except AcbError as exc:
            log.error("写入 Kimi 限速失败：%s", exc)
            self.lbl_model_hint.setText(f"写入限速失败：{exc}")
            return
        # 配置变了必须**重新加载**：内存里的 spec 还是旧值，不重载界面会显示
        # 跟文件不一致的状态（点一下没反应，用户会以为点错了）。
        self._models_config = load_models_config()
        self._populate_models()
        log.info(
            "Kimi 限速已改为 %s（并发 %d / 每分钟 %d 次），写入 %s。",
            tier.upper(),
            concurrency,
            rpm,
            path,
        )
        self.lbl_key_status.setText(
            f"已把限速改为 Kimi {tier.upper()}（并发 {concurrency} / 每分钟 {rpm} 次），"
            f"写入连接配置（下次启动仍然有效）"
            + ("；上次学到的限流值已作废，立即按新档位跑。" if forgotten else "。")
        )
        self.lbl_key_status.setStyleSheet("color: #0A7A0A;")

    def _duplicate_key_hint(self, spec) -> str:
        """两条连接共用**同一把**密钥时，返回一句提醒（否则返回空串）。

        【为什么要有这个提醒 —— 用户的真实困惑】
        用户提问："为什么连接切换 openai 和 deepseek，用的是同一个 key？"
        实测答案：**不是程序复用了一把 key**，而是那两个账户槽里存的本来就是同一个值
        （`DEEPSEEK_API_KEY` 与 `OPENAI_API_KEY` 逐字符相同，掩码都是 sk-****a8de）。
        密钥是按连接的 key_env 各存各的，"看起来一样"只是因为数据一样。

        但这件事**必须让程序自己说出来**：
          - 两条连接共用一把密钥 → 一起失效、一起计费、计费归属也看不出来；
          - 不说的话，用户只能从掩码猜，很容易误判成程序 bug（这次就是）。

        代价：每条连接的 key_env 都要读一次密钥链（实测本机 12 条共 **1.3 ms**，
        只在切换模型/连接时执行，可以接受）。
        """
        if self._models_config is None or not spec.key_env:
            return ""
        current = self._key_store.get(spec.key_env)
        if not current:
            return ""       # 当前连接根本没密钥，谈不上"共用"
        same: list[str] = []
        for cid, conn in self._models_config.connections.items():
            if conn.key_env == spec.key_env:
                continue
            if self._key_store.get(conn.key_env) == current:
                same.append(conn.name or cid)
        if not same:
            return ""
        names = "、".join(f"「{n}」" for n in same)
        return (
            f"　⚠ 与连接{names}用的是同一把密钥（同一个账户的额度与限流）；"
            "要给另一家单独的密钥，请用「＋」新建一条连接再填。"
        )

    # ------------------------------------------------------------------
    # 离线调试（原来的「离线模拟」+「演练模式」合并）
    # ------------------------------------------------------------------

    def _offline_debug_on(self) -> bool:
        """当前是否处于「离线调试」。

        防御性地写 hasattr：这个开关住在高级设置对话框里，
        而 _reload_styles 在启动早期就会被调用一次（那时控件可能还没建）。
        """
        chk = getattr(self, "chk_offline", None)
        return bool(chk is not None and chk.isChecked())

    def _refresh_ai_options_state(self) -> None:
        """离线调试时把「连接 / 模型 / 密钥」整组停用（单一真值来源）。

        【为什么必须停用，而不是留着让它被忽略】
        离线调试不联网、不需要密钥，这三组控件对结果**没有任何影响**。
        留着可点的控件会让人以为"选的模型/填的密钥会起作用" ——
        本项目在"看起来能用但其实被忽略"这件事上已经吃过亏
        （见 _refresh_output_options_state 里"置灰 ≠ 丢弃值"的注释）。
        这里的语义是明确的：置灰 = 不参与，且不会去读写任何密钥。

        顺带刷新风格下拉：内置基准风格只在离线调试下可选（见 _reload_styles）。
        """
        offline = self._offline_debug_on()
        widgets = (
            getattr(self, "cmb_provider", None),
            getattr(self, "btn_new_connection", None),
            getattr(self, "cmb_model", None),
            getattr(self, "btn_add_model", None),
            getattr(self, "edit_key", None),
            getattr(self, "btn_show_key", None),
            getattr(self, "btn_save_key", None),
            getattr(self, "btn_delete_key", None),
            getattr(self, "lbl_conn_caption", None),
            getattr(self, "lbl_model_caption", None),
            getattr(self, "lbl_key_caption", None),
        )
        for widget in widgets:
            if widget is not None:
                widget.setEnabled(not offline)

        if offline:
            # 状态行留给"解释"：整片变灰时用户需要一句为什么。
            if hasattr(self, "lbl_model_hint"):
                self.lbl_model_hint.clear()
            if hasattr(self, "lbl_key_status"):
                self.lbl_key_status.setText(
                    "离线调试：不调用 AI、不需要连接与密钥，所以「连接 / 模型 / 密钥」已停用。\n"
                    f"本次只处理前 {DRY_RUN_LIMIT} 张，出片落到 _export_dryrun；"
                    "XMP 照写，但内容全是假数据，别用于交付。"
                )
                self.lbl_key_status.setStyleSheet("color: #B26A00;")
        elif hasattr(self, "lbl_key_status"):
            self._on_model_changed()      # 回到真实的密钥/能力显示
            if hasattr(self, "lbl_key_status"):
                self.lbl_key_status.setStyleSheet("")   # 让 _on_model_changed 的颜色生效
        # 风格下拉的可选性随之变化（内置基准风格只在离线调试下可选）。
        if hasattr(self, "cmb_style"):
            self._reload_styles()

    def _is_debug_only_style(self, name: str) -> bool:
        """这个风格是不是「内置基准风格」（只该在离线调试里选）。

        ⚠ 判定**按名字**（DEBUG_ONLY_STYLE_NAMES），不能按"是不是种子文件"：
        种子文件有两个，用途完全不同 ——
          · default_neutral 是中性回退，日常应该选「（不选，使用内置默认策略）」，
            所以只在离线调试下可选（避免被误当成训练成果）；
          · 「AI自主决策」是**给日常用的功能**（用户明确要求），任何模式下都要能选。
        早期写的是 is_seed_style(...)，一旦加入第二个内置风格就会把它一起灰掉。
        """
        return str(name or "").strip() in DEBUG_ONLY_STYLE_NAMES

    def _on_save_key(self) -> None:
        """保存密钥（硬约束 #3：绝不写入配置文件或代码）。

        用户填了自己的 API key 后，程序先**尽力识别它属于哪个服务商**
        （见 config.detect_provider_from_key），识别出来就把密钥存到该服务商的
        账户名下，并把「模型」下拉自动切到该服务商 —— 于是 DeepSeek 的
        V4 Pro / V4.1 Flash 会在保存密钥后直接出现在选项里（用户要求）。
        识别不出来时回退到"当前选中模型所在的服务商"。
        """
        spec = self._current_spec()
        # 规范化：把粘贴带入的引号/空白/Bearer 前缀清掉；
        # 全角字符与中间空格这种"一定是拷错了"的形状直接拦住。
        # 实测教训：夹杂一个全角字符时服务端只回 401，用户会去查余额与权限，
        # 而问题其实在自己复制的那串字符里。
        secret, cleanup_notes = normalize_api_key(self.edit_key.text())
        problem = key_problem(secret)
        if problem:
            QMessageBox.warning(self, "密钥看起来不对", problem)
            return
        if cleanup_notes:
            log.info("保存密钥前清理了粘贴杂质：%s", "、".join(cleanup_notes))
        if not secret:
            QMessageBox.information(self, "未输入", "输入框为空，未做任何改动。")
            return
        if self._models_config is None:
            QMessageBox.warning(self, "无法保存", "尚未加载模型配置，请先检查 config/models.yaml。")
            return

        detected = detect_provider_from_key(secret)
        # 识别结果必须存在于「服务商」目录里才采纳；否则（比如 sk-ant- 对应
        # 的 Anthropic 没在目录里）回退到显式选择 —— 否则会弹"没有该服务商的
        # 模型"的模态框把界面卡死。
        if detected is not None and self._models_config is not None \
                and detected not in self._models_config.connections:
            detected = None
        # 识别结果（形态确定，如 AIza / sk-ant- / sk-ws / <id>.<secret>）优先，
        # 否则用「服务商」下拉里的显式选择；两者都没有才报错。
        provider = detected or self._current_provider()
        if provider is None and spec is not None and spec.provider:
            provider = spec.provider
        if provider is None:
            QMessageBox.warning(
                self,
                "未选择服务商",
                "无法判断这个密钥属于哪家服务商。\n"
                "请在「服务商」下拉里选择一家，再保存。",
            )
            return

        # 若识别出的服务商和当前选择不同，把「服务商」下拉切过去
        # （触发 _on_provider_changed → 模型列表重建为该服务商的模型）。
        if provider != self._current_provider():
            idx = self.cmb_provider.findData(provider)
            if idx >= 0:
                self.cmb_provider.setCurrentIndex(idx)

        # 保存密钥后要选中哪个模型：
        # 已经选过这条连接里的模型 → 保留用户的选择；
        # 没选过 / 选的是别家的 → 用 first_model_of_provider（优先支持视觉的）。
        target = self._model_to_select_after_refresh(provider)
        if target is None:
            QMessageBox.warning(
                self,
                "没有该服务商的模型",
                f"模型配置里没有服务商「{PROVIDER_LABELS.get(provider, provider)}」的模型。",
            )
            return

        target_key, target_spec = target
        try:
            persisted = self._key_store.set(target_spec.key_env, secret)
        except ValueError as exc:
            QMessageBox.warning(self, "密钥无法保存", str(exc))
            return
        # 说清"到底存在哪"：只在内存时绝不能说成"已保存" ——
        # 否则用户重启后密钥不见了，而程序从没提醒过（真实缺陷）。
        if persisted:
            log.info("密钥已保存到系统密钥链（%s = %s）。", target_spec.key_env, mask(secret))
            cleanup_note = f"（{'、'.join(cleanup_notes)}）" if cleanup_notes else ""
            saved_line = (
                f"密钥已保存到系统密钥链（账户名 {target_spec.key_env}，"
                f"{mask(secret)}）{cleanup_note}。"
            )
            saved_color = "#0A7A0A;"
        else:
            log.warning(
                "密钥未能写入系统密钥链，仅在本次会话内存中保存（%s = %s）。",
                target_spec.key_env,
                mask(secret),
            )
            saved_line = (
                f"⚠ 密钥链不可用（或写入失败），密钥只在**本次会话**有效"
                f"（账户名 {target_spec.key_env}，{mask(secret)}），关闭程序后需重输。"
            )
            saved_color = "#B26A00;"

        # 自动切到该服务商的第一个模型，让"识别 → 切换可用模型"生效。
        if self._current_model_key() != target_key:
            index = self.cmb_model.findData(target_key)
            if index >= 0:
                self.cmb_model.setCurrentIndex(index)

        self.edit_key.clear()
        self._on_model_changed()

        conn = self._models_config.connections.get(provider)
        name = (conn.name or PROVIDER_LABELS.get(provider, provider)) if conn else provider
        host = target_spec.base_url.split("//", 1)[-1].split("/", 1)[0]
        if detected:
            self.lbl_key_status.setText(
                f"已自动识别为「{name}」，{saved_line}"
                f"正在从 {host} 拉取可调用的模型……"
            )
        else:
            self.lbl_key_status.setText(
                f"连接「{name}」：{saved_line}"
                f"正在从 {host} 拉取可调用的模型……"
            )
        self.lbl_key_status.setStyleSheet(f"color: {saved_color}")
        log.info("连接「%s」密钥已保存，模型切换到「%s」。", name, target_spec.label)

        # 拉取该服务商真实可调用的模型列表（后台线程，不冻结界面）。
        self._start_model_discovery(provider, secret)


    # ------------------------------------------------------------------
    # 密钥的删除管理（用户要求：删除需要二次确认）
    # ------------------------------------------------------------------

    def _key_sources_text(self, account: str) -> str:
        """把"这把密钥现在从哪来"翻成人话（界面提示用）。"""
        found = self._key_store.sources(account)
        parts = []
        if found.get("memory"):
            parts.append("本次会话内存")
        if found.get("keyring"):
            parts.append("系统密钥链（凭据管理器）")
        if found.get("env"):
            parts.append(f"环境变量 {account}")
        return " + ".join(parts) if parts else "（没有找到）"

    def _delete_key_confirm_text(self, name: str, account: str, secret: str) -> str:
        """确认框正文。单独成方法是为了让自检能断言"该说的都说了"。"""
        found = self._key_store.sources(account)
        lines = [
            f"要删除连接「{name}」的密钥吗？",
            "",
            f"账户名：{account}",
            f"当前值：{mask(secret)}",
            f"来源：{self._key_sources_text(account)}",
            "",
            "删除后：",
            "  · 程序立刻不再使用它（本次会话的内存副本也会一起清掉）；",
            "  · 系统密钥链（凭据管理器）里的条目会被移除，**无法撤销**"
            "（要恢复只能重新粘贴一次密钥）；",
            "  · 你的照片、训练好的风格、模型与其它设置都不受影响。",
        ]
        if found.get("env"):
            lines += [
                "",
                f"⚠ 注意：检测到同名环境变量 {account} 也在提供密钥。"
                "环境变量是你在系统里设的，程序无权修改 —— "
                "删除之后程序仍会读到它。要彻底停用，请在系统环境变量里删掉那一条。",
            ]
        return "\n".join(lines)

    def delete_key_for(self, connection_id: str) -> tuple[bool, str]:
        """删除某条连接的密钥，返回 (是否真的删了东西, 给用户看的说明)。

        **业务逻辑单独成方法**：自检直接调它，不经过模态确认框
        （QMessageBox 在 offscreen 下会真的阻塞并挂死自检，本项目已经栽过一次）。
        调用方负责"确认"这一步（见 _confirm_and_delete_key）。
        """
        if self._models_config is None:
            return False, "尚未加载模型配置，无法删除密钥。"
        conn = self._models_config.connections.get(connection_id)
        if conn is None:
            return False, f"没有找到连接「{connection_id}」。"
        account = conn.key_env
        name = conn.name or PROVIDER_LABELS.get(connection_id, connection_id)

        before = self._key_store.sources(account)
        if not any(before.values()):
            return False, f"连接「{name}」（账户 {account}）目前没有已保存的密钥，无需删除。"

        self._key_store.delete(account)
        after = self._key_store.sources(account)
        if any(after.values()):
            # 只剩环境变量这一种可能（memory/keyring 都清掉了）。
            env_left = "环境变量" if after.get("env") else "未知来源"
            log.warning("账户 %s 的密钥已删除，但仍由%s提供。", account, env_left)
            return True, (
                f"已删除连接「{name}」在密钥链与本会话内存里的密钥（账户 {account}）；"
                f"但**同名的环境变量 {account} 仍在提供密钥**，程序会继续用它 —— "
                "要彻底停用请在系统环境变量里删除该变量，然后重启程序。"
            )
        log.info("连接「%s」的密钥已删除（账户 %s）。", name, account)
        return True, (
            f"已删除连接「{name}」的密钥（账户 {account}）。"
            "要再次使用请重新粘贴密钥并保存。"
        )

    def _confirm_and_delete_key(self, connection_id: str) -> None:
        """确认 → 删除 → 刷新界面（两个入口共用这一段）。"""
        if self._models_config is None:
            QMessageBox.warning(self, "无法删除", "尚未加载模型配置。")
            return
        conn = self._models_config.connections.get(connection_id)
        if conn is None:
            QMessageBox.warning(self, "无法删除", f"没有找到连接「{connection_id}」。")
            return
        account = conn.key_env
        name = conn.name or PROVIDER_LABELS.get(connection_id, connection_id)
        secret = self._key_store.get(account)
        if not secret:
            QMessageBox.information(
                self,
                "没有可删除的密钥",
                f"连接「{name}」（账户 {account}）目前没有已保存的密钥。",
            )
            return

        if not self._confirm_delete_key(
            self._delete_key_confirm_text(name, account, secret)
        ):
            self.lbl_key_status.setText("已取消：没有删除任何密钥。")
            self.lbl_key_status.setStyleSheet("color: #B26A00;")
            return

        ok, message = self.delete_key_for(connection_id)
        self._on_model_changed()          # 先按真实状态刷新（含 placeholder）
        if not ok:
            self.lbl_key_status.setText(message)
            self.lbl_key_status.setStyleSheet("color: #B26A00;")
            return
        if self._key_store.get(account):
            # 还有来源（环境变量）→ 输入框的"已保存密钥"占位仍然成立，不能清掉。
            self.lbl_key_status.setStyleSheet("color: #B26A00;")
        else:
            self.edit_key.clear_masked_placeholder()
            self.lbl_key_status.setStyleSheet("color: #0A7A0A;")
        self.lbl_key_status.setText(message)

    def _confirm_delete_key(self, text: str) -> bool:
        """删除前的二次确认（默认按钮是「否」）。

        默认按钮必须是"否"：这两个按钮上回车/空格就生效，
        而删除密钥不可撤销 —— 手快的人会连按两次回车把密钥删掉。
        """
        answer = QMessageBox.question(
            self,
            "确认删除密钥",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_delete_key(self) -> None:
        """「删除」按钮：删除**当前连接**的密钥。"""
        provider = self._current_provider()
        if provider is None:
            QMessageBox.information(
                self, "未选择连接", "请先在「连接」下拉里选一条，再删除它的密钥。"
            )
            return
        self._confirm_and_delete_key(provider)

    def _on_manage_keys(self) -> None:
        """「安全」菜单：列出**所有已保存密钥**的连接，选一条删除。

        为什么要这个入口：12 条出厂连接各有独立密钥槽，
        "逐个切连接再点删除"既慢又容易删错账户。
        """
        from PyQt6.QtWidgets import QInputDialog

        if self._models_config is None:
            QMessageBox.warning(self, "无法删除", "尚未加载模型配置。")
            return
        rows: list[tuple[str, str]] = []
        for cid, conn in self._models_config.connections.items():
            if any(self._key_store.sources(conn.key_env).values()):
                host = conn.base_url.split("//", 1)[-1].split("/", 1)[0]
                rows.append((f"{conn.name or cid} @ {host}（账户 {conn.key_env}）", cid))
        if not rows:
            QMessageBox.information(
                self, "没有已保存的密钥", "当前没有任何连接保存了密钥，无需删除。"
            )
            return
        labels = [label for label, _ in rows]
        chosen, accepted = QInputDialog.getItem(
            self,
            "删除已保存的密钥",
            "选择要删除密钥的连接（每一把都会先弹确认框）：",
            labels,
            0,
            False,
        )
        if not accepted or not chosen:
            return
        self._confirm_and_delete_key(rows[labels.index(str(chosen))][1])

    # ------------------------------------------------------------------
    # 连接（端点 + 密钥 + 备注名）
    # ------------------------------------------------------------------

    def _on_new_connection(self) -> None:
        """「＋」：弹对话框收集输入，再交给 _create_connection 落地。"""
        if self._models_config is None:
            QMessageBox.warning(
                self, "无法新建", "尚未加载模型配置，请先检查 config/models.yaml。"
            )
            return
        dialog = ConnectionDialog(self._models_config.providers, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._create_connection(**dialog.values())

    def _create_connection(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        preset: str = "",
    ) -> str | None:
        """把一条新连接落地：写 connections.yaml → 存密钥 → 刷新下拉 → 拉模型。

        返回新连接的 id（失败返回 None）。**对话框之外没有别的入口**，
        所以自检可以绕过模态框直接调它（`QDialog.exec()` 在 offscreen 下会挂死）。
        """
        if self._models_config is None:
            return None
        presets = self._models_config.providers
        name = (name or "").strip()
        base_url = (base_url or "").strip()
        if not name or not base_url:
            QMessageBox.warning(self, "信息不全", "连接名与 base_url 都必须填写。")
            return None
        if name in self._models_config.connections:
            QMessageBox.warning(
                self,
                "名字重复",
                f"已经有叫「{name}」的连接了。换一个名字 ——\n"
                "同名会覆盖掉原来那条连接的设置。",
            )
            return None

        template = presets.get(preset) if preset else None
        try:
            conn = ConnectionSpec(
                name=name,
                # 预设只提供"能力参数与兜底模型"：base_url 以用户填的为准（它就是事实来源）。
                label=template.label if template else name,
                base_url=base_url,
                endpoint_path=template.endpoint_path if template else "/chat/completions",
                models_endpoint=template.models_endpoint if template else "/models",
                api_style=template.api_style if template else "openai",
                key_env=connection_key_env(name),
                supports_vision=template.supports_vision if template else True,
                # 中转站/网关能力未知 → 保守取 False，走"提示词 + 本地强校验"降级通道。
                # 比反过来（谎称支持、结果服务端不认）安全得多。
                supports_json_schema=template.supports_json_schema if template else False,
                max_context=template.max_context if template else 128_000,
                max_output_tokens=template.max_output_tokens if template else 4096,
                temperature_output=template.temperature_output if template else 0.6,
                temperature_train=template.temperature_train if template else 0.2,
                image_max_bytes=template.image_max_bytes if template else 1_500_000,
                request_timeout_s=template.request_timeout_s if template else 60,
                default_models=list(template.default_models) if template else [],
                preset=preset,
            )
        except AcbError as exc:
            QMessageBox.warning(self, "无法新建连接", str(exc))
            return None
        except Exception as exc:  # pydantic ValidationError 等
            QMessageBox.warning(self, "无法新建连接", f"{type(exc).__name__}: {exc}")
            return None

        try:
            conn_id = upsert_connection(presets, conn)
        except AcbError as exc:
            QMessageBox.warning(self, "无法写入连接文件", str(exc))
            return None

        if api_key:
            # 与「保存密钥」同一条规范化 + 拦截规则：能存进去的，必须是能用的。
            normalized, cleanup_notes = normalize_api_key(api_key)
            problem = key_problem(normalized)
            if problem:
                QMessageBox.warning(self, "密钥无法保存", problem)
                # 不要拿着一把明显不对的密钥去拉模型列表：
                # 那只会多一条 401 噪音，把真正的问题（复制错了）盖过去。
                api_key = ""
            else:
                if cleanup_notes:
                    log.info("新建连接时清理了粘贴杂质：%s", "、".join(cleanup_notes))
                api_key = normalized
                try:
                    saved = self._key_store.set(conn.key_env, api_key)
                except ValueError as exc:
                    saved = False
                    QMessageBox.warning(self, "密钥无法保存", str(exc))
                log.info(
                    "连接「%s」密钥已保存到 %s（账户名 %s）%s",
                    name,
                    "系统密钥链" if saved else "本次会话内存",
                    conn.key_env,
                    mask(api_key),
                )

        # 重载配置 → 下拉里出现新连接 → 选中它 → 选它的第一个模型
        self._models_config = load_models_config()
        self._populate_models()
        idx = self.cmb_provider.findData(conn_id)
        if idx >= 0:
            self.cmb_provider.setCurrentIndex(idx)      # 触发 _on_provider_changed
        first = self._model_to_select_after_refresh(conn_id)
        if first is not None:
            index = self.cmb_model.findData(first[0])
            if index >= 0:
                self.cmb_model.setCurrentIndex(index)

        host = base_url.split("//", 1)[-1].split("/", 1)[0]
        if api_key:
            self.lbl_key_status.setText(
                f"已新建连接「{name}」（{host}，账户名 {conn.key_env}）。"
                "正在拉取它能调用的模型……"
            )
            self._start_model_discovery(conn_id, api_key)
        else:
            self.lbl_key_status.setText(
                f"已新建连接「{name}」（{host}）。填上密钥保存后即可拉取模型列表。"
            )
        self.lbl_key_status.setStyleSheet("color: #0A7A0A;")
        log.info("已新建连接「%s」→ %s（账户名 %s）", name, base_url, conn.key_env)
        return conn_id

    def _on_delete_connection(self, conn_id: str | None = None) -> None:
        """删除一条连接（密钥保留，见 config.delete_connection 的说明）。"""
        if self._models_config is None:
            return
        cid = conn_id or self._current_provider()
        conn = self._models_config.connections.get(str(cid)) if cid else None
        if conn is None:
            QMessageBox.information(self, "未选择连接", "请先在下拉里选一条连接。")
            return
        if len(self._models_config.connections) <= 1:
            # 兜底：删光了界面就没有任何模型可选，等于把自己锁在门外。
            QMessageBox.information(
                self,
                "至少保留一条连接",
                "这是最后一条连接了。想换端点的话，直接新建一条新的即可。",
            )
            return
        host = conn.base_url.split("//", 1)[-1].split("/", 1)[0]
        answer = QMessageBox.question(
            self,
            "确认删除连接",
            f"删除连接「{conn.name or cid}」（{host}）？\n\n"
            "• 只删这一条连接，其它连接不变；\n"
            "• 已经保存在系统凭据管理器里的密钥**不会**被删除；\n"
            "• 想回到出厂的连接列表（并一并重置密钥/失败记录/缓存/界面偏好）？"
            "用菜单「高级设置 → 恢复出厂设置」。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_connection(self._models_config.providers, str(cid))
        except AcbError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._models_config = load_models_config()
        self._populate_models()
        self.lbl_key_status.setText(f"已删除连接「{conn.name or cid}」。")
        self.lbl_key_status.setStyleSheet("color: #B26A00;")

    # --- 恢复出厂设置 -------------------------------------------------------

    #: 偏好键 → 人话（弹框里逐条列给用户看，不能说内部键名）。
    _PREFERENCE_LABELS: dict[str, str] = {
        "export_suffix": "输出文件名尾缀",
        "export_format": "输出格式（JPG / PNG）",
        "theme": "界面主题（浅色 / 深色）",
        "color_space": "输出色彩空间",
        "quality": "输出质量",
        "style": "上次选用的风格",
        "model": "上次选用的连接 / 模型",
        "pinned_models": "置顶的模型",
        "hidden_models": "被移除（隐藏）的模型",
        "pinned_styles": "置顶的风格",
    }

    def _changed_preferences(self) -> list[str]:
        """列出"当前值 != 出厂默认"的偏好项（中文名，供弹框逐条展示）。"""
        current = preferences.load_preferences()
        changed: list[str] = []
        for key, default in preferences.DEFAULTS.items():
            if current.get(key, default) != default:
                changed.append(self._PREFERENCE_LABELS.get(key, key))
        return changed

    def _restore_hidden_models(self) -> int:
        """把之前被移除（隐藏）的模型全部放回下拉。返回放回的数量。

        纯逻辑、不弹框（弹框版本只由「恢复出厂设置」这一条路走）——
        这样它既能在恢复出厂设置里复用，也能被自检直接调用。
        """
        hidden = preferences.get_list("hidden_models")
        if not hidden:
            return 0
        preferences.set_list("hidden_models", [])
        self._populate_models()
        log.info("已恢复 %d 个被隐藏的模型。", len(hidden))
        return len(hidden)

    def _factory_key_accounts(self) -> list[str]:
        """恢复出厂设置要清空密钥的账户名（连接 + 预设，去重保序）。

        ⚠ 必须在**删连接文件之前**调用：自建连接的账户名是 `conn:<备注名>`，
        只存在于连接文件里 —— 文件一删，这些账户名就再也枚举不出来了，
        密钥会永久留在凭据管理器里（用户以为"恢复出厂设置"已经把它清掉了）。
        """
        accounts: list[str] = []
        if self._models_config is None:
            return accounts
        candidates = list(self._models_config.connections.values()) + list(
            self._models_config.providers.values()
        )
        for conn in candidates:
            env = str(getattr(conn, "key_env", "") or "")
            if env and env not in accounts:
                accounts.append(env)
        return accounts

    def _factory_reset_plan(self) -> dict[str, object]:
        """收集"恢复出厂设置会重置什么"的事实。

        单独成方法有两个用处：① 弹框文案与真正执行读**同一份事实**，
        不会出现"文案说会删、实际没删"；② 全为空时可以明确说"已经全是出厂设置"，
        而不是让用户连点两次确认才发现什么都没发生。
        """
        accounts = self._factory_key_accounts()
        saved = [
            account
            for account in accounts
            if self._key_store.sources(account)["memory"]
            or self._key_store.sources(account)["keyring"]
        ]
        cache = ThumbCache()
        return {
            "conn_file": connections_path(),
            "hidden": preferences.get_list("hidden_models"),
            "changed": self._changed_preferences(),
            "key_accounts": accounts,
            "saved_keys": saved,
            "failures": len(self._job_state.failure_summary()),
            "cache_files": cache.stats()[0],
            "cache": cache,
        }

    def _on_restore_factory_settings(self) -> None:
        """恢复出厂设置：连接 + 密钥 + 失败记录 + 缓存 + 隐藏模型 + 界面偏好一起重置。

        **为什么要两次确认**（用户 2026-09-24 明确要求"警示 + 二次确认"）：
        这是全程序**唯一会一次性丢掉多项用户设置**的动作，而且它藏在「高级设置」里，
        误触的代价与隔壁的"清除缩略图缓存"完全不是一个量级。
        所以第一步弹的是**后果清单**（会说清丢什么、绝不碰什么），
        第二步才是"你真的确定吗，不能撤销"；两次的默认按钮都是「取消」，
        直接回车不会误放行（与极低画质确认框同一个套路）。

        2026-09-24 追加（用户要求"把已保存的密钥、失败记录、缩略图缓存也列进来"）：
        「恢复出厂」现在是**真的回到出厂**，而不是只把连接与偏好弄回去 ——
        否则用户会以为密钥已经清了、失败记录已经不挡路了，实际并没有。

        ⚠ 一条硬边界（写在弹框里，也在日志里）：
          **styles/ 里训练出来的风格文件绝不动** —— 那是用户花 API 钱训出来的成果，
          不属于"设置"，删掉是不可挽回的损失。
        """
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "任务进行中", "请先停止当前任务，再恢复出厂设置。"
            )
            return

        plan = self._factory_reset_plan()
        if not any(
            (
                plan["conn_file"].is_file(),      # type: ignore[union-attr]
                plan["hidden"],
                plan["changed"],
                plan["saved_keys"],
                plan["failures"],
                plan["cache_files"],
            )
        ):
            QMessageBox.information(self, "无需恢复", "当前已经全是出厂设置，没有可恢复的内容。")
            return

        if not self._confirm_factory_settings_warning(plan):
            log.info("用户在恢复出厂设置的警示框里选择了取消，未做任何改动。")
            return
        if not self._confirm_factory_settings_again():
            log.info("用户在恢复出厂设置的二次确认里选择了取消，未做任何改动。")
            return

        problems: list[str] = []
        # 1) 密钥：必须排在删连接文件之前（理由见 _factory_key_accounts）。
        removed_keys = 0
        leftover_env: list[str] = []
        for account in plan["key_accounts"]:      # type: ignore[union-attr]
            before = self._key_store.sources(account)
            if before["memory"] or before["keyring"]:
                self._key_store.delete(account)
                removed_keys += 1
            # 删完再查一次：环境变量里的同名密钥**程序删不掉**（那是用户在系统里设的），
            # 必须如实说出来，否则用户会以为密钥已经没了。
            if self._key_store.sources(account)["env"]:
                leftover_env.append(account)
        # 2) 连接文件 → 回到出厂连接
        conn_file = plan["conn_file"]
        if conn_file.is_file():                   # type: ignore[union-attr]
            try:
                conn_file.unlink()
            except OSError as exc:
                problems.append(f"连接文件删除失败（{exc}）")
        # 3) 隐藏的模型 + 界面偏好
        restored = self._restore_hidden_models()
        # 偏好整体写回默认值：**写一份新的默认字典**而不是"删文件"，
        # 这样"恢复出厂"之后 preferences.json 仍然是一份可读的、自解释的配置，
        # 而且旧版本留下的、早已不支持的键（例如已移除的导出格式）会被一并清掉。
        preferences.save_preferences(dict(preferences.DEFAULTS))
        self._prefs = preferences.load_preferences()
        # 4) 失败记录（含"永久跳过"档案 —— 不清掉的话这些文件下一跑仍然不会处理）
        cleared_failures = self._job_state.clear_failures()
        # 5) 缩略图缓存
        removed_cache = plan["cache"].clear()     # type: ignore[union-attr]

        # 界面要跟着变：先刷新需要重算的下拉（模型/风格按默认值重选），
        # 再把偏好回填到控件，最后把主题切回浅色。
        self._models_config = load_models_config()
        self._populate_models()
        self._reload_styles()
        self._apply_preferences_to_widgets()
        self._apply_theme(dark=False)
        # 密钥已经删了，输入框上那条"已保存密钥：sk-****"的掩码提示必须抹掉，
        # 否则界面还在说"这里存着密钥"（与真实状态相反）。
        self.edit_key.clear_masked_placeholder()
        self._on_model_changed()

        summary = (
            f"已恢复出厂设置：连接回到出厂、密钥清除 {removed_keys} 条、"
            f"失败记录清除 {cleared_failures} 条、缩略图缓存清除 {removed_cache} 个文件、"
            f"隐藏模型放回 {restored} 个、界面偏好重置 {len(plan['changed'])} 项。"  # type: ignore[arg-type]
        )
        if leftover_env:
            summary += (
                "⚠ 下列密钥由**同名环境变量**提供，程序无权删除："
                + "、".join(leftover_env)
                + "（要彻底停用请去系统里删掉这些环境变量）"
            )
        if problems:
            summary += "；" + "；".join(problems)
            log.warning("%s（用户操作：高级设置 → 恢复出厂设置）", summary)
            QMessageBox.warning(self, "部分未完成", "\n".join(problems))
        else:
            log.info("%s（用户操作：高级设置 → 恢复出厂设置）", summary)
        self.log_panel.append(
            summary + "（styles/ 里训练出来的风格文件未改动）",
            logging.WARNING if (problems or leftover_env) else logging.INFO,
        )
        self.lbl_key_status.setText("已恢复出厂设置（密钥已删除，需要重新粘贴；风格文件保留）。")
        self.lbl_key_status.setStyleSheet("color: #B26A00;")

    def _factory_settings_warning_text(self, plan: dict[str, object]) -> str:
        """恢复出厂设置的警示文案（纯函数，便于自检断言"该说的都说了"）。

        内容分两段，缺一段都不行：**会重置什么**（用户要看的后果）+ **不会动什么**
        （用户最担心的事：我训出来的风格还在不在）。
        """
        will_reset: list[str] = []
        if plan["conn_file"].is_file():           # type: ignore[union-attr]
            will_reset.append("• 连接列表：删除自定义连接文件，回到出厂的那几条")
        if plan["saved_keys"]:
            will_reset.append(
                f"• 已保存的 API 密钥：{len(plan['saved_keys'])} 条会直接删除、无法恢复，"   # type: ignore[arg-type]
                "之后要重新粘贴（环境变量里的密钥程序删不掉，会单独说明）"
            )
        if plan["hidden"]:
            will_reset.append(
                f"• 被移除的模型：{len(plan['hidden'])} 个全部放回下拉"   # type: ignore[arg-type]
            )
        if plan["changed"]:
            will_reset.append(
                "• 界面偏好：" + "、".join(plan["changed"])            # type: ignore[arg-type]
            )
        if plan["failures"]:
            will_reset.append(
                f"• 失败记录：{plan['failures']} 条全部清除（含「永久跳过」档案，"
                "清掉后这些文件会重新进入待处理队列）"
            )
        if plan["cache_files"]:
            will_reset.append(
                f"• 缩略图缓存：{plan['cache_files']} 个文件删除（下次运行重新提取，更慢但结果不变）"
            )

        return (
            "这会重置下列内容：\n"
            + "\n".join(will_reset)
            + "\n\n不会动的东西：\n"
            "• styles/ 里训练出来的风格文件（那是你的成果，不属于设置）\n"
            "• 你的照片、XMP 旁侧文件、models.yaml 配置\n\n"
            "重置后需要重新粘贴 API 密钥、重新选一次模型与风格。此操作不能撤销。"
        )

    def _confirm_factory_settings_warning(self, plan: dict[str, object]) -> bool:
        """恢复出厂设置的**第一步**：把后果清单摆出来，返回用户是否要继续。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("恢复出厂设置")
        # 文案里不含 Markdown 星号：QMessageBox 默认按纯文本画，星号会原样显示出来。
        box.setText(self._factory_settings_warning_text(plan))
        btn_go = box.addButton("继续（还要再确认一次）", QMessageBox.ButtonRole.AcceptRole)
        btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_cancel)
        box.exec()
        return box.clickedButton() is btn_go

    def _confirm_factory_settings_again(self) -> bool:
        """恢复出厂设置的**第二步**：真·二次确认（默认仍是取消）。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("再次确认")
        box.setText(
            "确认恢复到出厂设置？\n\n"
            "连接、隐藏模型、界面偏好、失败记录与缩略图缓存都会被重置；\n"
            "已保存的 API 密钥会被删除且无法恢复（之后需要重新粘贴）。\n"
            "此操作不能撤销。"
        )
        btn_yes = box.addButton("确认重置", QMessageBox.ButtonRole.AcceptRole)
        btn_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_no)
        box.exec()
        return box.clickedButton() is btn_yes

    def _start_model_discovery(self, provider_key: str, secret: str) -> None:
        """后台拉取这条连接可调用的模型列表（保存密钥 / 新建连接后自动触发）。

        ⚠ 必须查 **connections** 而不是 providers：自建连接的 base_url 可能指向
        中转站或内网网关，拿预设的官方地址去拉列表会拉到别人家的模型清单
        （而且大概率还是 401）。找不到连接时才退回预设。
        """
        if self._models_config is None:
            return
        provider = self._models_config.connections.get(provider_key) \
            or self._models_config.providers.get(provider_key)
        if provider is None:
            return
        # 保活：worker 必须持有 Python 引用，否则被 GC 后信号就断了。
        # ⚠ 不能只存一个属性：用户连存两次密钥时第二次会把第一次的引用覆盖掉，
        #   那个线程就没人持有了（QThread 被 GC → 原生崩溃）。全部挂进列表，
        #   线程一结束就从列表里移除。
        worker = ModelsFetchWorker(provider_key, provider, secret, self)
        self._models_fetch_worker = worker          # 兼容旧引用（排查/自检）
        self._models_fetch_workers.append(worker)
        worker.sig_models.connect(self._on_models_discovered)
        worker.sig_error.connect(self._on_models_fetch_error)
        worker.finished.connect(
            lambda w=worker: (
                self._models_fetch_workers.remove(w)
                if w in self._models_fetch_workers
                else None
            )
        )
        worker.start()

    def _on_models_discovered(self, provider_key: str, model_ids: list[str]) -> None:
        """/models 拉取成功：存盘 → 重建下拉 → 选中该服务商第一个模型。"""
        try:
            save_discovered_models(provider_key, model_ids)
            # 把「服务商」下拉指到刚拉取的那家：模型下拉按服务商过滤，
            # 不指过去的话，刚拉到的模型会被当前服务商的过滤条件挡住（自检抓过）。
            if hasattr(self, "cmb_provider"):
                self.cmb_provider.blockSignals(True)
                idx = self.cmb_provider.findData(provider_key)
                if idx >= 0:
                    self.cmb_provider.setCurrentIndex(idx)
                self.cmb_provider.blockSignals(False)
            self._models_config = load_models_config()
            self._populate_models()
            first = self._model_to_select_after_refresh(provider_key)
            if first is not None and first[0] != self._current_model_key():
                idx = self.cmb_model.findData(first[0])
                if idx >= 0:
                    self.cmb_model.setCurrentIndex(idx)
                    # 说明清楚“为什么替你换了模型”——否则用户只会觉得
                    # “我选的模型怎么老是被改掉”，而这是最难排查的一类抱怨。
                    log.info(
                        "拉取模型列表后自动选中「%s」（你还没选过这条连接的模型，"
                        "或原来选中的已不在列表里）。想改回自己选：在下拉里点一下即可。",
                        first[1].label,
                    )
            name = self._models_config.connection_name(provider_key)
            self.lbl_key_status.setText(
                f"已拉取连接「{name}」可调用的模型（{len(model_ids)} 个），下拉已更新。"
            )
            self.lbl_key_status.setStyleSheet("color: #0A7A0A;")
        except Exception as exc:  # noqa: BLE001 —— 拉取成功后应用失败不能让保存白做
            log.error("应用拉取到的模型列表失败：%s", exc)
            self.lbl_key_status.setText(f"模型列表已拉到，但应用失败：{exc}")
            self.lbl_key_status.setStyleSheet("color: #B00020;")

    def _on_models_fetch_error(self, message: str) -> None:
        """/models 拉取失败：回退兜底模型，并提示手动入口。"""
        log.warning("拉取模型列表失败：%s", message)
        self.lbl_key_status.setText(
            f"拉取模型列表失败（{message}）。已用配置里的兜底模型；"
            "若服务商没有 /models 接口，点「模型」右侧的「＋」手动添加。"
        )
        self.lbl_key_status.setStyleSheet("color: #B26A00;")
        try:
            self._models_config = load_models_config()   # 用 default_models 兜底
            self._populate_models()
        except Exception as exc:  # noqa: BLE001
            log.error("回退兜底模型失败：%s", exc)

    def _on_add_model_manually(self) -> None:
        """手动添加模型 id（服务商没有 /models 接口时用）。"""
        from PyQt6.QtWidgets import QInputDialog

        if self._models_config is None:
            QMessageBox.warning(self, "无法添加", "尚未加载模型配置。")
            return
        provider_key = self._models_config.provider_of(self._current_model_key() or "")
        if not provider_key:
            provider_key = next(iter(self._models_config.connections), "")
        provider = self._models_config.connections.get(provider_key)
        if provider is None:
            QMessageBox.warning(
                self, "无法添加", "当前没有可用的连接，请先在「连接」里新建一条。"
            )
            return
        conn_name = provider.name or PROVIDER_LABELS.get(provider_key, provider_key)

        model_id, ok = QInputDialog.getText(
            self,
            "手动添加模型",
            f"给连接「{conn_name}」填一个模型 id：\n"
            "（会原样传给 API；填错会得到 404 之类的报错）",
        )
        model_id = (model_id or "").strip()
        if not ok or not model_id:
            return

        existing = load_discovered_models()
        ids = list(existing.get(provider_key) or provider.default_models)
        if model_id not in ids:
            ids.append(model_id)
        save_discovered_models(provider_key, ids)
        self._models_config = load_models_config()
        self._populate_models()
        idx = self.cmb_model.findData(f"{provider_key}::{model_id}")
        if idx >= 0:
            self.cmb_model.setCurrentIndex(idx)
        self.lbl_key_status.setText(f"已添加模型「{model_id}」并选中。")
        self.lbl_key_status.setStyleSheet("color: #0A7A0A;")

    def _reload_styles(self) -> None:
        """刷新风格下拉。

        第一项固定为「（不选，使用内置默认策略）」——对应问题 e 的默认行为。
        """
        # 用 data（风格名）而不是 text 记住原选中项：置顶会让文本多出 "★ " 前缀，
        # 按文本找会以为"这一项没了"，把用户的选择默默重置掉。
        current = self.cmb_style.currentData()
        pinned = set(preferences.get_list("pinned_styles"))
        names = preferences.reorder_by_pinned(list_styles(), list(pinned))
        offline = self._offline_debug_on()
        # 重建期间屏蔽信号：clear() 会发出 currentIndexChanged(-1)，
        # 不屏蔽的话 _on_style_changed 会当场把刚记住的风格写成空字符串。
        self.cmb_style.blockSignals(True)
        self.cmb_style.clear()
        self.cmb_style.addItem("（不选，使用内置默认策略）", None)
        for name in names:
            row = self.cmb_style.count()
            marked = name in pinned
            debug_only = self._is_debug_only_style(name)
            self.cmb_style.addItem(f"{PIN_PREFIX}{name}" if marked else name, name)
            if marked:
                self.cmb_style.setItemData(
                    row, pinned_font(self.cmb_style.font()), Qt.ItemDataRole.FontRole
                )
            if debug_only:
                # 内置基准风格：**只在离线调试下可选**（用户要求）。
                # 用 model().item().setEnabled(False) 而不是 removeItem：
                # 用户要能"看见它在那儿、但点不了"，删掉反而会让人以为风格丢了。
                item = self.cmb_style.model().item(row)
                if item is not None:
                    item.setEnabled(offline)
                    item.setToolTip(
                        "内置的中性基准风格（调试用），只在勾选「离线调试」时可选。\n"
                        "日常使用请选「（不选，使用内置默认策略）」——效果一样，"
                        "而且不会被误当成训练出来的风格。"
                    )
            elif is_autopilot_style_name(name):
                # 「AI自主决策」：程序内置、任何模式下都能选、删不掉。
                item = self.cmb_style.model().item(row)
                if item is not None:
                    item.setToolTip(
                        "程序内置的「AI自主决策」：不套用任何个人风格，"
                        "由模型按每张照片本身决定调整方向与幅度。\n"
                        "它的\"不限制\"只针对**审美方向**：字段白名单、取值范围、"
                        "机器字段与相机配置的禁令照旧生效（越界会被本地校验拒收）。\n"
                        "这是内置风格，删不掉（删了下次启动也会补回来）。"
                    )
        # 选中优先级：刷新前的选中项 → 上次记住的风格 → 「不选」占位项。
        # 记得的那一个可能已经被删掉了，所以必须 findData 判存在。
        remembered = str(self._prefs.get("style") or "") or None
        index = -1
        for candidate in (current, remembered):
            if candidate:
                index = self.cmb_style.findData(candidate)
                if index >= 0:
                    break
        index = index if index >= 0 else 0
        # 不在离线调试时，**不能**让选中项停在"只该调试时可选"的风格上 ——
        # 无论它是"刷新前的选中项"还是"记忆值"。否则界面上会出现一个灰掉却
        # 仍然生效的选择，那比"不能选"更糟：用户以为没在用，实际正在用它。
        if not offline and index > 0:
            chosen = self.cmb_style.itemData(index)
            if isinstance(chosen, str) and self._is_debug_only_style(chosen):
                index = 0
        self.cmb_style.setCurrentIndex(index)
        self.cmb_style.blockSignals(False)
        # 屏蔽信号期间落的选中项要显式落盘一次。
        self._on_style_changed()
        log.info("已加载 %d 个风格文件（目录：%s）。", self.cmb_style.count() - 1, styles_dir())


    # ========================================================================
    # 用户偏好与"整理列表"（移除 / 置顶 / 删除）
    # ========================================================================

    def _remember(self, key: str, value) -> None:
        """把一条偏好写回磁盘。值没变就不写，避免每次启动都白写一遍文件。"""
        if self._prefs.get(key) == value:
            return
        self._prefs[key] = value
        preferences.update_preferences(**{key: value})

    def _apply_preferences_to_widgets(self) -> None:
        """把偏好里的"导出格式 / 尾缀"填回控件。

        启动时调用一次。**必须与 _remember 成对使用**：
        _remember 负责"控件 → 磁盘"，本方法负责"磁盘 → 控件"，
        两者合起来才是用户要的"下次打开还记得上次填的尾缀"。
        """
        saved_format = str(self._prefs.get("export_format") or DEFAULT_EXPORT_FORMAT)
        index = self.cmb_format.findData(saved_format)
        if index < 0:
            # 旧版本写下的格式可能已经不存在了 —— 例如 TIFF 在 1.0.0 里被移除
            # （Photoshop 2026 的脚本 DOM 没有 TIFFSaveOptions）。
            # 必须**显式**回到默认项，而不是"什么都不做"：什么都不做只是碰巧
            # 在启动时为 JPG，一旦此方法在别处被调用就会把当前选中留在别的格式上。
            fallback = self.cmb_format.findData(DEFAULT_EXPORT_FORMAT)
            index = fallback if fallback >= 0 else 0
            if index >= 0:
                self.cmb_format.setCurrentIndex(index)
            log.warning(
                "偏好里的导出格式 %r 已不再支持，已回退到 %s。",
                saved_format,
                self._current_export_format(),
            )
        else:
            self.cmb_format.setCurrentIndex(index)
        self.edit_suffix.setText(str(self._prefs.get("export_suffix") or ""))

        # --- 色彩空间与输出质量 ---
        # 这两个控件在启动时已经建好并填过值，所以直接回填即可。
        # （模型与风格的下拉此刻还是空的，它们的回填在 _populate_models /
        #   _reload_styles 里做。）
        saved_space = str(self._prefs.get("color_space") or "")
        if saved_space:
            space_index = self.cmb_color_space.findText(saved_space)
            if space_index >= 0:
                self.cmb_color_space.setCurrentIndex(space_index)
            else:
                log.warning(
                    "偏好里的色彩空间 %r 已不在可选列表里，保持 %s。",
                    saved_space,
                    self.cmb_color_space.currentText(),
                )
        saved_quality = self._prefs.get("quality")
        if isinstance(saved_quality, int) and not isinstance(saved_quality, bool):
            self.slider_quality.setValue(clamp_ps_quality(saved_quality))

        self._on_export_format_changed()
        # 上面 setText 若与当前文本相同就不会触发 textChanged，
        # 所以标签必须显式刷一次，否则启动时它会是空的。
        self._on_suffix_edited()

    def _current_export_format(self) -> str:
        data = self.cmb_format.currentData()
        return str(data) if data else DEFAULT_EXPORT_FORMAT

    def _current_export_suffix(self) -> str:
        return self.edit_suffix.text().strip()

    def _on_color_space_changed(self) -> None:
        """记住选中的输出色彩空间（下次打开带出）。"""
        self._remember("color_space", self.cmb_color_space.currentText())

    def _on_quality_changed(self, value: int) -> None:
        """记住选中的输出质量。"""
        self._remember("quality", int(value))

    def _on_style_changed(self) -> None:
        """记住选中的风格（空字符串 = 「不选，使用内置默认策略」）。"""
        data = self.cmb_style.currentData()
        self._remember("style", str(data) if data else "")

    def _on_export_format_changed(self) -> None:
        """格式变化时记住选择，并把对无损格式无意义的画质滑块灰掉。"""
        fmt = self._current_export_format()
        self._remember("export_format", fmt)

        # 灰掉而不是藏起来：藏起来会改变底栏宽度，左右两栏比例就会跟着跳。
        # 具体灰度由 _refresh_output_options_state 与「输出」勾选状态合并后决定。
        self._refresh_output_options_state()
        log.info(
            "导出格式：%s%s",
            fmt,
            "（无损，画质设置不参与）" if is_lossless_format(fmt) else "",
        )

    def _on_suffix_edited(self) -> None:
        """尾缀变化时立刻记住，并把"实际会用的尾缀"回显在标签上。"""
        raw = self.edit_suffix.text()
        self._remember("export_suffix", raw)
        self.lbl_suffix.setText(f"尾缀 {normalize_export_suffix(raw)}")

    def _on_remove_selected(self) -> None:
        """把表格里选中的行移出待处理列表。**不碰磁盘上的任何文件**。"""
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        if not rows:
            QMessageBox.information(
                self,
                "未选中",
                "请先在列表里选中要移除的照片（按住 Ctrl 或 Shift 可以多选）。",
            )
            return

        for row in rows:
            self.table.removeRow(row)

        # 以表格为准重建 _sources，保证两者永远一致。
        # 比"按路径从 _sources 里删"更稳：路径大小写、相对/绝对差异都不会漏删。
        remaining: list[Path] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                remaining.append(Path(item.toolTip()))
        self._sources = remaining

        self._update_file_summary()
        log.info(
            "已从列表移除 %d 个文件（当前共 %d 个；磁盘上的文件未做任何改动）。",
            len(rows),
            len(self._sources),
        )

    # ------------------------------------------------------------------
    # 下拉条目的右键菜单（置顶 / 取消置顶 / 删除）
    # ------------------------------------------------------------------

    def _install_item_menu(self, combo: QComboBox, kind: str) -> None:
        """给下拉装上"右键条目"菜单。kind = "model" / "style"。

        【为什么改成右键，而不是常驻两个按钮】置顶与删除是"偶尔用一次"的整理动作，
        常驻按钮会挤掉下拉的宽度，而且它们只能作用在**当前选中项**上 ——
        你得先选对再去点按钮，很容易作用到隔壁那一项上去。
        右键直接作用在"你正指着的那一项"，意图明确，也不用先改变当前选择。

        代价是右键菜单不可见，所以两处 tooltip 里都写明了"右键列表条目"这个用法。

        ⚠ 这里**没有**用 `customContextMenuRequested` 信号，而是用事件过滤器，
        原因见 _ComboItemMenuFilter 的文档字符串（这是一个真实的闪退教训）。
        """
        # 过滤器对象挂在 combo 上（C++ 父子），同时存进实例列表保 Python 引用。
        item_filter = _ComboItemMenuFilter(self, combo, kind)
        self._menu_filters.append(item_filter)
        view = combo.view()
        for target in (view, view.viewport(), combo):
            target.installEventFilter(item_filter)

    def _build_item_menu(self, combo: QComboBox, kind: str, row: int):
        """为第 row 项构造右键菜单；行号非法时返回 None。

        拆成"构造"与"弹出"两步，是为了让自检能检查菜单内容而**不真的弹窗**：
        `QMenu.exec` 会阻塞，在 offscreen 下会把整个自检挂死
        （QMessageBox 那次已经栽过一次，终端都失去响应）。
        """
        if row < 0 or row >= combo.count():
            return None
        value = combo.itemData(row)
        menu = QMenu(self)

        if value is None:
            # 风格的「（不选，使用内置默认策略）」是占位项，不是文件、也不能置顶。
            # 给一句灰掉的说明，总比弹出一个空菜单让人以为程序坏了。
            placeholder = menu.addAction("这是「不选」占位项，不能置顶或删除")
            placeholder.setEnabled(False)
            return menu

        if kind == "connection":
            # 连接不做"置顶/隐藏"（数量少，且删除有真实后果），只给一个删除入口。
            menu.addAction("删除这条连接…", lambda: self._delete_item(kind, value))
            return menu

        pinned_key = "pinned_models" if kind == "model" else "pinned_styles"
        is_pinned = value in preferences.get_list(pinned_key)
        menu.addAction(
            "取消置顶" if is_pinned else "置顶",
            lambda: self._toggle_pin(kind, value),
        )
        menu.addAction(
            "删除（仅从下拉移除，可恢复）" if kind == "model" else "删除（会删掉风格文件）",
            lambda: self._delete_item(kind, value),
        )
        return menu

    def _open_item_menu(self, combo: QComboBox, kind: str, row: int, global_pos) -> None:
        """弹出右键菜单。

        ⚠ 这里**必须用 popup()，不能用 exec()**。这是本项目付出过一次"闪退"代价的。

        `QMenu.exec()` 会开一个**嵌套事件循环**。而右键发生时，栈上还压着
        QComboBox 弹窗容器的 grabs 与它自己的事件处理 —— 在嵌套循环里去抢 grab，
        会跟已经在收尾的那个弹窗撞上，实测直接报 **0xC0000005 访问冲突**。
        因为进程是被系统直接终止的，Python 层根本没机会记日志，
        所以用户看到的是"右键一下就毫无征兆地消失"，日志里一行都没有。

        `popup()` 是异步的（立即返回，不嵌套事件循环），这一整类重入问题从根上消失。
        代价是菜单对象必须自己保活，否则会被 GC 掉 —— 参见 _item_menu。
        """
        menu = self._build_item_menu(combo, kind, row)
        if menu is None:
            return
        # ⚠ 这里**故意不调 combo.hidePopup()**：下拉列表要保持展开。
        #
        # 用户反馈过：右键后菜单弹出来了，但选项列表"缩回去"了 ——
        # 那是之前为了避开两个 popup 抢 grab而主动收的，属于多余动作。
        # 实际实测（走真实 QContextMenuEvent + 真的弹菜单 + 真的点菜单项）：
        #   · 不主动收 → 列表与菜单同时可见，选置顶后列表**就地重排并显示 ★**，
        #     选删除后那一项就从展开的列表里消失；全程不崩。
        #   · 菜单关闭后列表仍在；点外部时两者一起关。
        # 大前提是右键事件已经被 _ComboItemMenuFilter 吃掉（Qt 不再自己处理弹窗），
        # 否则两个 popup 的 grabs 还是会撞。
        # 上一份菜单如果还挂着，先让它体面退场（菜单以 self 为父，不倒手会越积越多）。
        if self._item_menu is not None:
            self._item_menu.deleteLater()
        self._item_menu = menu
        menu.popup(global_pos)

    def _toggle_pin(self, kind: str, value) -> None:
        """菜单里的「置顶 / 取消置顶」：转给已有的处理器（它们有确认与日志）。"""
        if kind == "model":
            self._on_pin_model(str(value))
        else:
            self._on_pin_style(str(value))

    def _delete_item(self, kind: str, value) -> None:
        """菜单里的「删除」：转给已有的处理器（它们负责确认框与边界检查）。"""
        if kind == "model":
            self._on_delete_model(str(value))
        elif kind == "connection":
            self._on_delete_connection(str(value))
        else:
            self._on_delete_style(str(value))

    def _on_pin_model(self, key: str | None = None) -> None:
        """把某个模型置顶；已置顶则取消。不传 key 时作用于当前选中项。"""
        if key is None:
            key = self._current_model_key()
        if key is None:
            QMessageBox.information(self, "未选择模型", "请先在下拉里选一个模型。")
            return
        _, pinned = preferences.toggle_pinned("pinned_models", key)
        self._populate_models()
        index = self.cmb_model.findData(key)
        if index >= 0:
            self.cmb_model.setCurrentIndex(index)
        log.info("模型「%s」%s。", key, "已置顶" if pinned else "已取消置顶")

    def _on_delete_model(self, key: str | None = None) -> None:
        """把某个模型从下拉里移除（**不改 models.yaml**，随时可恢复）。"""
        if key is None:
            key = self._current_model_key()
        spec = None
        if key is not None and self._models_config is not None:
            spec = self._models_config.models.get(key)
        if key is None or spec is None:
            QMessageBox.information(self, "未选择模型", "请先在下拉里选一个模型。")
            return

        hidden = preferences.get_list("hidden_models")
        if key in hidden:
            QMessageBox.information(
                self, "已经移除过了", f"模型「{spec.label}」已在隐藏列表里。"
            )
            return

        answer = QMessageBox.question(
            self,
            "确认移除模型",
            f"从下拉列表里移除「{spec.label}」？\n\n"
            "• models.yaml **不会被修改**，随时可以用「高级设置 → 恢复出厂设置」找回；\n"
            "• 已保存的 API 密钥也会保留。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        preferences.set_list("hidden_models", hidden + [key])
        self._populate_models()
        log.info("模型「%s」已从下拉中移除（配置未改动，可恢复）。", key)

    def _on_pin_style(self, name: str | None = None) -> None:
        """把某个风格置顶；已置顶则取消。不传 name 时作用于当前选中项。"""
        if name is None:
            data = self.cmb_style.currentData()
            if not data:
                QMessageBox.information(
                    self, "未选择风格", "「不选，使用内置默认策略」这一项不能置顶。"
                )
                return
            name = str(data)
        _, pinned = preferences.toggle_pinned("pinned_styles", name)
        self._reload_styles()
        index = self.cmb_style.findData(name)
        if index >= 0:
            self.cmb_style.setCurrentIndex(index)
        log.info("风格「%s」%s。", name, "已置顶" if pinned else "已取消置顶")

    def _on_delete_style(self, name: str | None = None) -> None:
        """删除某个风格文件（真的删掉 styles/<名字>.json）。

        不传 name 时作用于当前选中项。
        """
        if name is None:
            data = self.cmb_style.currentData()
            if not data:
                QMessageBox.information(
                    self, "未选择风格", "请先在下拉里选一个要删除的风格。"
                )
                return
            name = str(data)

        target = style_file_for(str(name))
        if target is None:
            QMessageBox.warning(
                self, "找不到文件", f"没有找到风格「{name}」对应的文件，可能已被手动删除。"
            )
            self._reload_styles()
            return

        if is_seed_style(target):
            if is_autopilot_style_name(name):
                QMessageBox.information(
                    self,
                    "这是程序自带的风格",
                    f"「{name}」是程序内置的风格（不是训练产物），"
                    "删掉后下次启动会被自动恢复，所以这里不提供删除。\n\n"
                    "它的用途：让模型按每张照片本身决定调整方向，"
                    "适合想先看看「没有任何个人偏好时模型会怎么调」的场景。\n"
                    "想有自己的风格，用训练模式产出一份即可（那份可以删）。",
                )
            else:
                QMessageBox.information(
                    self,
                    "这是程序自带的风格",
                    f"「{name}」是程序自带的内置基准风格（调试用）："
                    "删掉后下次启动会被自动恢复，所以这里不提供删除。\n\n"
                    "它只在勾选「离线调试」时可选；日常使用请选"
                    "「（不选，使用内置默认策略）」，或者训练一个自己的风格。",
                )
            return

        answer = QMessageBox.question(
            self,
            "确认删除风格",
            f"永久删除这个风格文件？\n\n{target}\n\n"
            "此操作不可撤销（要恢复只能重新训练）。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            target.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "删除失败", f"无法删除文件：\n{exc}")
            return

        # 顺手把它从置顶列表里摘掉，不然会留下一个指向不存在风格的置顶项。
        preferences.set_list(
            "pinned_styles",
            [x for x in preferences.get_list("pinned_styles") if x != name],
        )
        self._reload_styles()
        log.info("已删除风格文件：%s", target)

    def _about_text(self) -> str:
        """「关于」的正文（唯一真值来源：对话框与自检都读它，避免两处各写一份）。

        内容由用户 2026-09-24 给定（作者、反馈与赞助入口），**逐字照抄**：
        这是他的对外联络方式，不该由我改写措辞（英文那两句也保持原样）。
        版本号从 `__version__` 派生，避免"发版忘了改关于对话框"。
        """
        return (
            f"{APP_DISPLAY_NAME} V{__version__}\n"
            "Author：ZIRAN\n"
            "Bilibili：https://space.bilibili.com/1999530739\n"
            "rednote：https://www.xiaohongshu.com/user/profile/649dfbe8000000001001c834\n"
            "Github：https://github.com/ziranjun/Auto-Color-Diffusion\n"
            "afdian：https://afdian.com/a/ZIRAN2391\n"
            "You can give feedback on the app in these. I'll check it as quickly as possible.\n"
            "Thank you for your using.You can sponsor me in Github or afdian if you like, "
            "thank you very much."
        )

    def _about_icon_pixmap(self) -> QPixmap | None:
        """「关于」用的图标位图：按 ABOUT_ICON_SIZE 逻辑像素缩好；取不到素材返回 None。

        为什么必须缩：QMessageBox 拿到位图后**不会替你缩放** —— 把 512×512 的图
        直接塞进去，它会按原尺寸占满整个左侧，正文被挤成一条窄栏（用户实测反馈）。
        高 DPI 下按 devicePixelRatio 取更大的物理像素再缩，边上不会发糊。
        """
        path = app_icon_png_large_path()
        if not path.is_file():
            return None
        try:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return None
            ratio = self.devicePixelRatioF() or 1.0
            side = max(1, int(round(ABOUT_ICON_SIZE * ratio)))
            pixmap = pixmap.scaled(
                side,
                side,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap.setDevicePixelRatio(ratio)
            return pixmap
        except Exception:          # noqa: BLE001 - 图标问题不该拦住弹框
            log.warning("「关于」图标加载失败，回退到系统默认图标", exc_info=True)
            return None

    def _show_about(self) -> None:
        """关于对话框。

        标题行与链接做成**可点的**：这几行就是给人去打开的（反馈、赞助），
        纯文本会逼用户手抄 URL。QMessageBox 内部就是一个 QLabel，但它不会把
        linkActivated 转成"用系统浏览器打开"，所以要显式打开
        `openExternalLinks`（Qt 默认是 False，不设的话点了没反应）。
        """
        import re

        box = QMessageBox(self)
        box.setWindowTitle(f"关于 {APP_DISPLAY_NAME}")
        # 图标要**先缩好**再塞进去（原因见 _about_icon_pixmap）。
        icon = self._about_icon_pixmap()
        if icon is not None:
            box.setIconPixmap(icon)
        else:
            box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        # 先按纯文本拼（把换行变成 <br>），再把 http(s) 链接包成 <a>。
        # URL 里没有 & 与 <，所以不需要额外转义；真出现时下面的正则也不会误伤别的字符。
        body = self._about_text().replace("\n", "<br>")
        body = re.sub(
            r"(https?://[^\s<]+)",
            r'<a href="\1">\1</a>',
            body,
        )
        box.setText(body)
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    # ========================================================================
    # 文件列表
    # ========================================================================

    def _on_mode_changed(self) -> None:
        """模式切换（硬约束 #8 的模式切换开关）。"""
        training = self.rb_train.isChecked()
        self.option_stack.setCurrentIndex(1 if training else 0)
        self.btn_add_files.setVisible(not training)
        self.btn_add_dirs.setVisible(not training)
        self.btn_add_pairs.setVisible(training)
        # 训练模式下这些控件**灰掉而不是隐藏**：隐藏会改变底栏宽度，
        # 左右两栏的比例就会跟着跳（用户反馈过）。灰掉既能表达"用不到"，
        # 又不改变布局宽度。
        #
        # 具体灰哪些交给 _refresh_output_options_state 统一算 —— 底栏选项
        # 有两个禁用来源（训练模式 / 没勾「输出」），各写各的会互相覆盖。
        self.btn_resume.setEnabled(not training)
        self.btn_only_failed.setEnabled(not training)
        self.btn_clear_failures.setEnabled(not training)
        self._refresh_output_options_state()
        log.info("已切换到%s。", "训练模式" if training else "输出模式")

    def _refresh_output_options_state(self) -> None:
        """统一计算底栏各控件的可用状态（禁用 = 变灰，不改变布局宽度）。

        【为什么必须集中在一处算】底栏选项有两个禁用来源：
          1) 训练模式用不到输出选项；
          2) 没勾「输出」（只写 XMP 不导图）时，后面的输出位置/色彩空间/质量/
             格式/尾缀全都无从谈起。
        两处各写各的 setEnabled，后执行的那个就会把前一个的结论覆盖掉 ——
        实际表现就是"没勾输出却点亮了"或"训练模式下反而亮着"这种自相矛盾。

        “输出”勾选框本身**永远不会因为自己没勾而变灰**，否则用户再也点不回来。

        【置灰与"值还在被读"的关系】置灰只表示"现在不能改"，不代表值被丢弃：
        未勾「输出」时任务仍会读到这些控件的**当前值**去生成 Photoshop 脚本，
        但那个脚本不会被执行（run_photoshop=False），所以对产出没有任何影响。
        真正需要"置灰就必须忽略其值"的只有 _export 子目录那一处
        （见 _collect_output_options 与 _on_export_subdir_toggled）。
        """
        training = self.rb_train.isChecked()
        exporting = self.chk_run_ps.isChecked()
        wants_export = (not training) and exporting

        self.chk_run_ps.setEnabled(not training)
        # 输出位置整块一起灰（picker_output 是复合控件，禁用它会连
        # 标签 / 输入框 / 浏览按钮一起灰），子目录勾选框同理。
        for widget in (self.picker_output, self.chk_export_subdir,
                       self.lbl_color_space, self.cmb_color_space,
                       self.lbl_quality, self._out_suffix_options):
            widget.setEnabled(wants_export)

        # 画质滑块还有第二个禁用来源：无损格式（PNG）下画质设置不参与。
        lossless = is_lossless_format(self._current_export_format())
        self.slider_quality.setEnabled(wants_export and not lossless)
        if wants_export and lossless:
            self.slider_quality.setToolTip(
                f"{self._current_export_format()} 是无损格式，画质设置对它没有影响"
                "（文件体积由压缩方式决定）。"
            )
        else:
            # 恢复滑块自身的画质说明（极低画质时是警示全文）。
            self.slider_quality.refresh_caption()

    def _on_add_files(self) -> None:
        start = str(self._sources[-1].parent) if self._sources else ""
        paths = choose_raw_files(self, start)
        if paths:
            self._add_sources(paths)

    def _on_add_pairs(self) -> None:
        start = str(self._sources[-1].parent) if self._sources else ""
        paths = choose_xmp_and_raw_pairs(self, start)
        if paths:
            self._add_sources(paths)

    def _on_add_dirs(self) -> None:
        start = str(self._sources[-1].parent) if self._sources else ""
        directory = choose_directory(self, "选择包含 RAW 的文件夹（会递归扫描子目录）", start)
        if directory:
            self._add_sources([directory])

    def _add_sources(self, paths: list[Path]) -> None:
        """把路径加入列表（按绝对路径去重）。"""
        existing = {str(p.resolve()).lower() for p in self._sources}
        added = 0
        for path in paths:
            try:
                key = str(path.resolve()).lower()
            except OSError:
                continue
            if key in existing:
                continue
            existing.add(key)
            self._sources.append(path)
            added += 1
            self._append_table_row(path)
        self._update_file_summary()
        log.info("已添加 %d 个路径（当前共 %d 个）。", added, len(self._sources))

    def _append_table_row(self, path: Path) -> None:
        """在文件表里追加一行。

        踩坑记录：这里曾经写成 `elide_middle(name, 70)`，而 `name` 是未定义的变量
        （正确的是 `path.name`）。因为函数参数就叫 `path`，写错成 `name` 时
        Python 不会在导入期报错，只在**运行时**抛 NameError，
        表现为"一添加图片就闪退"——而且异常发生在 Qt 的槽函数里，
        若没接住就成了静默崩溃。
        现在补上了 GUI 级自检（tools/gui_smoke_test.py）来覆盖这类问题。
        """
        row = self.table.rowCount()
        self.table.insertRow(row)
        # 表格列显示文件名（中间省略以适配列宽），完整路径放进 tooltip。
        name_item = QTableWidgetItem(elide_middle(path.name, 70))
        name_item.setToolTip(str(path))
        status_item = QTableWidgetItem("待处理")
        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, status_item)

    def _on_clear_files(self) -> None:
        self._sources.clear()
        self.table.setRowCount(0)
        self._update_file_summary()
        log.info("已清空文件列表。")

    def _update_file_summary(self) -> None:
        supported = " ".join(ext.upper() for ext in RAW_EXTENSIONS)
        if not self._sources:
            self.lbl_file_summary.setText(f"尚未添加文件。支持的后缀：{supported}")
            return
        self.lbl_file_summary.setText(
            f"共 {len(self._sources)} 个路径。支持的后缀：{supported}（大小写不敏感，文件夹会递归扫描）"
        )

    def _set_item_status(self, filename: str, status: str) -> None:
        """按文件名更新表格状态列（主线程调用）。"""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.toolTip().endswith(filename):
                status_item = self.table.item(row, 1)
                if status_item is not None:
                    status_item.setText(status)
                return

    # ========================================================================
    # 输出目录
    # ========================================================================

    def _on_pick_output_dir(self) -> None:
        directory = choose_directory(self, "选择输出目录", self.picker_output.edit.text())
        if directory:
            self.picker_output.edit.setText(str(directory))

    def _on_export_subdir_toggled(self, checked: bool) -> None:
        """勾选「输出到 _export 子目录」后，自定义输出目录框置灰。

        这里是"置灰 + 真正忽略输入框内容"（见 _collect_output_options）：
        只置灰而仍然读取里面的文字，会让勾选看起来生效、实际却被旧路径覆盖，
        这种"控件灰了但代码还在用它的值"是最难排查的一类不一致。
        """
        self.picker_output.edit.setEnabled(not checked)
        self.picker_output.button.setEnabled(not checked)

    # ========================================================================
    # 参数收集
    # ========================================================================

    def _collect_common(self) -> tuple[AdapterLike | None, str]:
        """收集模型与密钥，返回 (adapter, 错误消息)。"""
        spec = self._current_spec()
        if spec is None:
            return None, "请先在 config/models.yaml 中配置模型（当前未加载）。"

        # 离线调试：必须在**要密钥之前**返回，否则没有可用密钥时会弹出输入框，
        # 把"想在不联网的情况下测试"这件事直接堵死（而这正是它存在的理由）。
        if self.chk_offline.isChecked():
            return build_offline_adapter(spec), ""

        # 优先用输入框里刚填的；否则从 keyring / 环境变量取。
        typed = self.edit_key.text().strip()
        if typed:
            self._key_store.set(spec.key_env, typed)
            self.edit_key.clear()
            self._on_model_changed()

        api_key = self._key_store.get(spec.key_env)
        if not api_key:
            # 硬约束 #3：仅在 keyring 中无值时弹出输入框。
            from PyQt6.QtWidgets import QInputDialog

            text, ok = QInputDialog.getText(
                self,
                "需要 API 密钥",
                f"模型「{spec.label}」需要密钥（账户名 {spec.key_env}）。\n"
                "密钥只会存入系统密钥链，绝不写入任何文件。",
                QLineEdit_EchoMode_Password(),
            )
            if not ok or not text.strip():
                return None, "未提供 API 密钥，已取消。"
            self._key_store.set(spec.key_env, text.strip())
            self._on_model_changed()
            api_key = text.strip()

        # 预算对象在这里先给一个占位；run_output_mode 会用实际文件数重建。
        adapter = ModelAdapter(spec, api_key, RequestBudget(limit=1))
        return adapter, ""

    def _collect_output_options(self, resume: bool, only_failed: bool) -> OutputOptions:
        output_dir_text = self.picker_output.edit.text().strip()
        # 勾了「输出到 _export 子目录」就说明用户要的是默认规则，忽略输入框里的
        # 残留路径；否则填了就用填的（显式指定永远优先，见 export_dir_for）。
        if self.chk_export_subdir.isChecked():
            output_dir = None
        else:
            output_dir = Path(output_dir_text) if output_dir_text else None
        style_name = self.cmb_style.currentData()
        style_data = load_style_profile(str(style_name)) if style_name else None

        return OutputOptions(
            sources=list(self._sources),
            prompt=self.txt_prompt.toPlainText(),
            style_name=str(style_name) if style_name else None,
            style_data=style_data,
            color_space=self.cmb_color_space.currentText(),
            export_format=self._current_export_format(),
            export_suffix=self._current_export_suffix(),
            ps_quality=self.slider_quality.value(),
            output_dir=output_dir,
            # 勾选框描述的是"非默认行为"，所以这里要取反。
            output_to_source_dir=not self.chk_export_subdir.isChecked(),
            recursive=True,
            resume=resume,
            only_failed=only_failed,
            dry_run=self.chk_offline.isChecked(),
            workers=self.spin_workers.value(),
            max_requests=self.spin_max_requests.value() or None,
            use_cache=self.chk_use_cache.isChecked(),
            prefer_raw_decode=self.chk_high_fidelity.isChecked(),
            dng_sidecar=self.chk_dng_sidecar.isChecked(),
            run_photoshop=self.chk_run_ps.isChecked(),
        )

    # ========================================================================
    # 开始 / 停止
    # ========================================================================

    def _confirm_low_quality(self, quality: int) -> bool:
        """弹一次极低画质确认框；返回 True 表示用户确认继续。

        为什么不用 QMessageBox.warning 的默认 Yes/No：那套按钮文字来自 Qt 的
        翻译文件，本程序没有加载中文翻译，会显示成英文 Yes/No，
        与全中文界面不一致。因此显式建按钮。

        默认按钮刻意设为「返回修改质量」：用户直接回车不会误放行，
        想继续必须刻意去点「仍然使用」。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认极低画质")
        box.setText(describe_low_quality_warning(quality))
        btn_continue = box.addButton("仍然使用（继续）",
                                     QMessageBox.ButtonRole.AcceptRole)
        btn_back = box.addButton("返回修改质量",
                                 QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_back)
        box.exec()

        if box.clickedButton() is btn_continue:
            # 用户知情后仍选择继续：必须留痕，事后查日志能看出这是有意为之，
            # 而不是程序算错了档位。
            log.warning("用户确认按极低画质（PS 刻度 %d）继续导出。", quality)
            return True

        log.warning("用户在极低画质确认框里选择返回，本次导出已取消。")
        return False

    def _on_start(self, *, resume: bool, only_failed: bool) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "任务进行中", "已有任务正在运行，请先停止或等待完成。")
            return

        if not self._sources:
            QMessageBox.warning(self, "未添加文件", "请先点击「添加照片」或「添加文件夹」。")
            return

        training = self.rb_train.isChecked()
        style_name_value = self.edit_style_name.text().strip()
        if training and not style_name_value:
            QMessageBox.warning(
                self, "需要风格命名", "训练模式需要给风格起个名字（用于保存 style_profile.json）。"
            )
            return

        adapter, error = self._collect_common()
        if adapter is None:
            QMessageBox.warning(self, "无法开始", error)
            self.log_panel.append(error, logging.WARNING)
            return

        # 极低画质必须显式确认一次，不允许静默放行。
        # 放在这里而不是滑块里，有两个原因：
        #   1. 滑块拖动会逐档经过这些值，在那里弹框会让控件无法使用；
        #   2. 这里一次任务只问一次，正好符合"确认"的语义。
        # 训练模式不产出 JPG，与画质无关，所以跳过。
        if not training and is_very_low_quality(self.slider_quality.value()):
            if not self._confirm_low_quality(self.slider_quality.value()):
                return

        self._set_running(True)
        self.progress.setValue(0)
        self.lbl_progress_text.setText("0 / 0")

        if training:
            opts = TrainOptions(
                recursive=True,
                use_cache=self.chk_use_cache.isChecked(),
                prefer_raw_decode=self.chk_high_fidelity.isChecked(),
                workers=self.spin_workers.value(),
            )
            style_name = self.edit_style_name.text().strip()
            sources = list(self._sources)
            exiftool = self._exiftool

            def job(callbacks: JobCallbacks, cancel_event: threading.Event) -> TrainResult:
                pairs = discover_pairs(sources, recursive=opts.recursive)
                log.info("发现 %d 个训练样本对。", len(pairs))
                return run_train_mode(
                    pairs,
                    opts,
                    adapter=adapter,
                    exiftool=exiftool,
                    callbacks=callbacks,
                    style_name=style_name,
                    cancel_event=cancel_event,
                )

            worker: OutputWorker | TrainWorker = TrainWorker(job, self)
            worker.sig_finished.connect(self._on_train_finished)
        else:
            opts = self._collect_output_options(resume=resume, only_failed=only_failed)
            job_state = self._job_state
            exiftool = self._exiftool

            def job(callbacks: JobCallbacks, cancel_event: threading.Event) -> OutputResult:
                return run_output_mode(
                    opts,
                    adapter=adapter,
                    exiftool=exiftool,
                    job_state=job_state,
                    callbacks=callbacks,
                    cancel_event=cancel_event,
                )

            worker = OutputWorker(job, self)
            worker.sig_finished.connect(self._on_output_finished)

        # 管线日志**不再**单独接到面板：它现在通过日志系统（文件 handler +
        # UiLogSignalHandler）同时进文件与面板，两处一致（见 workers._emit_log）。
        # 以前只接 sig_log → 面板，于是文件日志里没有任何管线消息 ——
        # 实测踩到：3 个文件被永久跳过，日志里连“跳过”都没有。
        worker.sig_progress.connect(self._on_progress)
        worker.sig_stage.connect(self.lbl_stage.setText)
        worker.sig_item.connect(self._set_item_status)
        worker.sig_error.connect(self._on_worker_error)
        worker.finished.connect(lambda: self._set_running(False))

        self._worker = worker
        mode = "训练模式" if training else "输出模式"
        log.info("=" * 72)
        # 把本次的运行模式说清楚。用户曾被"跳过已完成的文件"搞糊涂过，
        # 所以这里不写含糊的"（续跑）"，而是直接说明会不会重新请求 API。
        if training:
            mode_note = ""
        elif only_failed:
            mode_note = "（只跑失败：其余一律跳过）"
        elif resume:
            mode_note = "（续跑：跳过已完成的文件，不重复请求 API）"
        else:
            mode_note = "（全量：已完成的文件也会重新请求 API）"
        log.info("开始%s%s", mode, mode_note)
        worker.start()

    def _on_stop(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self.btn_stop.setEnabled(False)
            self.lbl_stage.setText("正在停止…")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_resume.setEnabled(not running and not self.rb_train.isChecked())
        self.btn_only_failed.setEnabled(not running and not self.rb_train.isChecked())
        self.btn_clear_failures.setEnabled(not running and not self.rb_train.isChecked())
        self.btn_add_files.setEnabled(not running)
        self.btn_add_dirs.setEnabled(not running)
        self.btn_add_pairs.setEnabled(not running)
        self.btn_clear_files.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_recheck.setEnabled(not running)

    # --- 完成回调（均在主线程） --------------------------------------------

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress.setValue(0)
            self.lbl_progress_text.setText("0 / 0")
            return
        percent = int(done * 100 / total)
        self.progress.setValue(max(0, min(100, percent)))
        self.lbl_progress_text.setText(f"{done} / {total}")

    def _set_stage_state(self, text: str, state: str) -> None:
        """把阶段文字设成醒目的状态样式（加粗 + 语义色）。

        为什么需要：原来只是把一个小标签从「正在…」改成「已完成」，
        字号与颜色都不变，用户根本注意不到（真实反馈过）。
        """
        self.lbl_stage.setText(text)
        color = STAGE_STATE_COLORS.get(state, "#000000")
        self.lbl_stage.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _show_completion_dialog(self, result: OutputResult) -> None:
        """处理结束后弹一次汇总对话框。

        为什么用对话框而不是只刷日志：批量任务结束时用户往往已经切到别的窗口，
        日志区的变化根本看不到。对话框是唯一"一定会被看见"的提示。

        手动出片的详细指引放进 setDetailedText（折叠区）而不是正文，
        这样正文保持简短可读，需要时展开即可，不用连弹两个对话框。
        """
        if result.stopped:
            title = "已停止"
            icon = QMessageBox.Icon.Warning
        elif result.failed:
            title = "处理结束（有失败）"
            icon = QMessageBox.Icon.Warning
        else:
            title = "处理完成"
            icon = QMessageBox.Icon.Information

        # summary_text() 已经包含输出目录 / Photoshop 状态 / Token 用量 / 请求预算，
        # 这里只补一条它没有的：日志到底写在哪。
        lines = [result.summary_text()]
        if result.ps_log_path:
            lines.append(f"详细日志：{result.ps_log_path}")

        # 判定只在 _on_output_finished 里做一次，这里直接复用，
        # 避免两处各写一遍条件而慢慢分叉。
        needs_manual = self._manual_export_needed

        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText("\n".join(lines))
        if needs_manual:
            # 传真实路径，指引里才能给出"日志在哪"而不是一句占位文字。
            box.setDetailedText(
                photoshop.manual_guidance(
                    result.script_dir,
                    log_path=result.ps_log_path,
                    jsx_path=result.jsx_path,
                )
            )
        open_btn = None
        if self._last_output_dir is not None:
            open_btn = box.addButton("打开输出目录", QMessageBox.ButtonRole.ActionRole)
        # 只有"需要手动补跑"时才给脚本目录按钮：平时那个目录对用户没有意义，
        # 多一个按钮只会让人犹豫该点哪个。
        script_btn = None
        if needs_manual and self._last_script_dir is not None:
            script_btn = box.addButton("打开脚本目录", QMessageBox.ButtonRole.ActionRole)
        box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if open_btn is not None and clicked is open_btn:
            self._on_open_output()
        elif script_btn is not None and clicked is script_btn:
            self._on_open_script()

    def _on_output_finished(self, result: OutputResult) -> None:
        if result.stopped:
            self._set_stage_state("已停止（已完成的已保留）", "stopped")
        elif result.failed:
            self._set_stage_state(f"完成，但有 {result.failed} 张失败", "failed")
        else:
            self._set_stage_state(f"全部完成（{result.done} 张）", "ok")
        # 走 logger 而不是只 append 面板：这一行是**事后排查的唯一线索**
        # （用户回头问“为什么少处理了几张”时，日志里必须有这张清单）。
        log.info("%s", result.summary_text())

        if result.warnings:
            log.warning("本次共有 %d 条告警，请查看上方日志或日志文件。", len(result.warnings))

        stats = self._job_state.stats()
        if stats["failed"]:
            rows = self._job_state.failure_summary()
            preview = "；".join(
                f"{Path(path).name}({count} 次{', 永久跳过' if perm else ''})"
                for path, count, _err, perm in rows[:5]
            )
            log.warning(
                "失败清单（最多显示 5 条）：%s。同一文件累计失败 %d 次将永久跳过"
                "（**任何模式下都不会再处理**），可用「清除失败记录」重置。",
                preview,
                MAX_ATTEMPTS_PER_FILE,
            )

        if result.jsx_path:
            self._last_output_dir = result.export_dir
            self._last_script_dir = result.script_dir
        self.btn_open_output.setEnabled(self._last_output_dir is not None)
        self.btn_open_script.setEnabled(self._last_script_dir is not None)

        # 自动导出没成功 = 用户需要手动补跑 → 脚本目录要留着，
        # 完成对话框里也要直接给出打开它的按钮。
        # 手动跑导出必须用刚生成的 manifest，不能沿用上一次的（内容早已不同）。
        # 只看结构化字段，**不看文案**：
        # 失败时的状态文本是"完成（成功 0/1，失败 1）"，含"成功"二字，
        # 用子串匹配会把判断骗反，进而把唯一能看原因的 runs/ 目录删掉。
        self._manual_export_needed = bool(
            result.ps_ok is False and not result.stopped and result.script_dir
        )

        # 完成提示必须醒目：小标签的变化用户注意不到（真实反馈过）。
        self._show_completion_dialog(result)

    def _on_train_finished(self, result: TrainResult) -> None:
        if result.stopped:
            # 用户自己按的停止：不是失败，不需要他查日志；只需告诉他风格没保存。
            self._set_stage_state("已停止（风格未保存）", "stopped")
            self.log_panel.append(
                "训练已停止：**不会再发送请求，风格档案未保存**（磁盘上是原来的那份）。",
                logging.WARNING,
            )
            return

        if result.profile is None:
            # 训练失败时不能显示"训练完成"：磁盘上留着上一版的风格文件，
            # 用户很容易以为训练生效了，然后奇怪"为什么风格一点没变"
            #（实测踩到：44 个样本的归纳请求读超时，风格文件还是两天前那份）。
            self._set_stage_state("训练失败（风格未保存）", "failed")
            self.log_panel.append(
                "训练未产出风格档案：**本次训练没有成功，磁盘上的风格文件保持原样**。"
                "请查看上方日志中的错误原文（超时类失败可以直接重试一次）。",
                logging.ERROR,
            )
            return

        self._set_stage_state("训练完成", "ok")

        if result.usage:
            # 与输出模式一样把用量摆出来：一次归纳要把全部样本的参数明细 + 缩略图
            # 一次发上去，是单次最贵的一发 —— 以前这一发在日志里完全看不到花费。
            self.log_panel.append(f"训练用量：{result.usage}", logging.INFO)

        self.log_panel.append(
            f"风格档案已生成：样本 {result.sample_count} 个，"
            f"统计字段 {len(result.profile.get('param_ranges') or {})} 个，"
            f"风格规则 {len(result.profile.get('text_rules') or [])} 条，"
            f"排除字段 {len(result.profile.get('ignore_fields') or [])} 个。",
            logging.INFO,
        )
        if result.saved_path:
            self.log_panel.append(f"保存位置：{result.saved_path}", logging.INFO)
            self._reload_styles()
            index = self.cmb_style.findText(str(result.profile.get("name") or ""))
            if index >= 0:
                self.cmb_style.setCurrentIndex(index)

        if result.insufficient_samples:
            QMessageBox.warning(
                self,
                "样本不足",
                f"样本不足，结果可能不稳定。\n建议至少提供 {TRAIN_MIN_SAMPLES_WARN} 个样本后重新训练。",
            )

    def _on_worker_error(self, message: str) -> None:
        self.lbl_stage.setText("出错")
        self.log_panel.append(message, logging.ERROR)
        QMessageBox.critical(self, "任务出错", message[:1200])

    # ========================================================================
    # 辅助按钮
    # ========================================================================

    def _on_clear_failures(self) -> None:
        """清除失败记录（硬约束 #18 的逃生舱）。"""
        rows = self._job_state.failure_summary()
        if not rows:
            QMessageBox.information(self, "无需清除", "当前没有失败记录。")
            return
        permanent = sum(1 for _p, _c, _e, perm in rows if perm)
        answer = QMessageBox.question(
            self,
            "确认清除",
            f"将清除 {len(rows)} 条失败记录（其中 {permanent} 条已永久跳过）。\n"
            "清除后这些文件会重新进入待处理队列。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        count = self._job_state.clear_failures()
        # 这是用户可感知的状态变更（直接决定下一跑会不会重试那些文件），
        # 因此进日志系统而不是只发面板——事后对账时以日志为准。
        log.info("已清除 %d 条失败记录（用户操作：日志 → 清除失败记录）。", count)

    def _on_clear_cache(self) -> None:
        cache = ThumbCache()
        count, total = cache.stats()
        if count == 0:
            QMessageBox.information(self, "无需清除", "缩略图缓存为空。")
            return
        answer = QMessageBox.question(
            self,
            "确认清除缩略图缓存",
            f"将删除 {count} 个缓存文件（约 {total / 1024 / 1024:.1f} MB）。\n"
            "清除后下次运行需要重新提取并压缩缩略图（更慢，但结果不变）。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = cache.clear()
        self.log_panel.append(f"已清除缩略图缓存（{removed} 个文件）。", logging.INFO)

    def _on_open_output(self) -> None:
        if self._last_output_dir is not None:
            self._open_path(self._last_output_dir)

    def _on_open_script(self) -> None:
        """打开本次运行的脚本目录（manifest / jsx / bat 都在里面）。

        _open_path 会先 mkdir 再 startfile，所以目录被清掉也不会报错——
        但那样用户会看到一个空目录且完全不知道发生了什么，
        所以这里先自己判一次并说清楚原因。
        """
        if self._last_script_dir is None:
            return
        if not self._last_script_dir.is_dir():
            self.log_panel.append(
                f"脚本目录已不存在（退出时已自动清理）：{self._last_script_dir}\n"
                "需要手动补跑导出的话，请重新跑一次，并在关闭程序之前完成。",
                logging.WARNING,
            )
            return
        self._open_path(self._last_script_dir)

    def _open_path(self, path: Path) -> None:
        """用资源管理器打开目录（Windows 专用）。"""
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  # 仅 Windows 有
        except Exception as exc:
            # 同时进文件日志："点菜单没反应"这类反馈靠界面面板事后是查不到的。
            log.warning("无法打开目录 %s：%s", path, exc)
            self.log_panel.append(f"无法打开目录 {path}：{exc}", logging.WARNING)

    # ========================================================================
    # 生命周期
    # ========================================================================

    def _detach_menu_filters(self) -> None:
        """关窗前摘掉下拉右键的事件过滤器。

        【为什么必须显式摘 —— 实测数据】把右键菜单装到第三个下拉（「连接」）之后，
        `tools/menu_crash_probe.py open` 在 8 次里有 3 次以 0xC0000005 结束，
        而且发生在**窗口关闭后的进程收尾阶段**（stdout 里 PROBE_OK 已经打出来了）；
        不装那个过滤器时 6/6 次退出码都是 0。

        过滤器是以 combo 为 C++ 父对象的 QObject：窗口销毁时 C++ 侧先把它删掉，
        而 Python 侧（self._menu_filters 里的引用）还活着 —— 收尾阶段一旦碰到
        已被销毁的 QObject 包装，就是访问冲突（本项目的老朋友，见
        _ComboItemMenuFilter 的文档字符串）。
        显式 removeEventFilter + 脱离父子关系，把这个顺序问题消掉。
        """
        for flt in list(getattr(self, "_menu_filters", [])):
            try:
                combo = getattr(flt, "_combo", None)
                targets = []
                if combo is not None:
                    view = combo.view()
                    targets = [view, view.viewport(), combo]
                for target in targets:
                    target.removeEventFilter(flt)
                flt.setParent(None)     # 交给 Python 管理，避免 C++ 父先被销毁
            except RuntimeError:
                pass                    # 底层对象已经没了：忽略
        self._menu_filters = []

    def closeEvent(self, event) -> None:  # noqa: N802  （Qt 接口）
        """关闭窗口时安全收尾：请求停止并等待线程结束。

        不等待就直接退出会导致 QThread 被销毁时仍在运行，
        Qt 会打印 "QThread: Destroyed while thread is still running"
        并可能直接崩溃。
        """
        # 先摘掉事件过滤器（见 _detach_menu_filters 的实测说明）
        self._detach_menu_filters()

        forced_close = False
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            if not self._worker.wait(15000):
                forced_close = True
                self.log_panel.append(
                    "工作线程未能在 15 秒内退出（可能正在等待网络响应），将强制关闭。",
                    logging.WARNING,
                )

        # 模型列表拉取线程同样要等：它以窗口为 parent，窗口一销毁，
        # C++ 侧对象先没，Python 侧还在跑的 QThread 就变成
        # “Destroyed while thread is still running”（实测过这类原生崩溃）。
        # 它的 run() 是一次轻量 HTTP GET，最多等 8 秒。
        for fetch in list(self._models_fetch_workers):
            if fetch.isRunning() and not fetch.wait(8000):
                log.warning("模型列表拉取线程未在 8 秒内结束，关闭时不再等待。")

        # 强制关闭时不清理：此时工作线程还活着，Photoshop 可能正在导出、
        # 正往本次运行的脚本目录里写日志。那种状态下删文件既是干扰，
        # 也可能把唯一能解释失败原因的日志删掉。
        self._cleanup_on_exit(
            skip_reason="工作线程未正常退出，跳过清理以免干扰仍在进行的导出"
            if forced_close
            else None
        )
        super().closeEvent(event)

    def _cleanup_on_exit(self, skip_reason: str | None = None) -> None:
        """退出前清掉本次会话产生的临时文件。

        清理范围与安全边界见 acb/cleanup.py 的模块注释（白名单 + 根锚定 +
        拒绝链接）：缩略图缓存、XMP 中转临时文件、本程序每次运行的脚本目录
        （manifest.json / export_batch.jsx / run_export.bat 与 Photoshop 日志）。
        **风格档案 / 断点记录 / 模型配置 / 用户照片一律不动。**

        整体包在 try 里：关闭流程绝不能因为清理失败而卡住或弹错。
        """
        if skip_reason:
            log.warning("退出清理：%s", skip_reason)
            return
        try:
            # 本次导出**没成功**时保留脚本目录：用户可能还要双击
            # run_export.bat 手动补跑，把脚本删掉等于断了他的后路。
            # 成功时不保留 —— 脚本已经跑完，留着只是垃圾。
            keep = (
                self._last_script_dir
                if self._manual_export_needed and self._last_script_dir is not None
                else None
            )
            report = cleanup.cleanup_session_junk(keep_run_dir=keep)
            if report.touched or report.errors:
                level = logging.WARNING if report.errors else logging.INFO
                message = f"退出清理：{report.describe()}"
                log.log(level, message)
                self.log_panel.append(message, level)
            # 必须在最后调用：它会关掉日志 handler（见该函数注释）。
            cleanup.delete_app_logs(report)
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不能抛
            log.warning("退出清理出错（已忽略，不影响关闭）：%s", exc)


def QLineEdit_EchoMode_Password():
    """延迟导入 QLineEdit 的密码回显枚举。

    单独抽成函数，避免在模块导入期就把 QtWidgets 的枚举取出来
    （在某些 PyQt6 版本 + 打包组合下，模块级枚举访问会因绑定顺序问题失败）。
    """
    from PyQt6.QtWidgets import QLineEdit as _QLineEdit

    return _QLineEdit.EchoMode.Password


class _ComboItemMenuFilter(QObject):
    """下拉列表里右键 → 弹出「置顶 / 删除」菜单（用事件过滤器实现）。

    【为什么不直接用 `customContextMenuRequested` 信号】这是付出过一次闪退代价的结论。

    走信号那条路时，Qt 会先把右键事件走完它**自己**的弹窗处理：QComboBox 的下拉
    是一个 Qt::Popup，身上带着鼠标/键盘 grab；而我们紧接着要在同一段调用里再弹一个
    菜单（无论 `QMenu.exec()` 还是 `popup()`），两个 popup 的 grabs 就会撞在一起 ——
    实测直接报 **0xC0000005 访问冲突**。进程是被系统直接终止的，Python 层根本没
    机会记日志，所以现象是"右键一下就毫无征兆地消失"，日志里一行都没有。

    事件过滤器可以直接 **return True 把事件吃掉**：Qt 的默认弹窗处理根本不会发生，
    之后再把菜单推迟到下一轮事件循环里弹，两条路不再交叉。

    踩坑提示：这个崩溃**无法用"直接调函数"式的测试复现**（手动 emit 信号不崩），
    必须是真实的事件派发才行 —— 所以对应的回归守卫是
    `tools/menu_crash_probe.py`（在**子进程**里发真实 QContextMenuEvent，看退出码）。
    """

    def __init__(self, window: "MainWindow", combo: QComboBox, kind: str) -> None:
        super().__init__(combo)
        self._window = window
        self._combo = combo
        self._kind = kind

    def eventFilter(self, obj, event):  # noqa: N802  （Qt 接口）
        # QComboBox 的弹窗容器把"在条目上**松开**鼠标"当成"选中这一项"，
        # 于是自己调 hidePopup() —— **右键也一样**。
        # 用户反馈的"右键后选项列表缩回去"就是这个，跟我们的代码无关，是 Qt 的行为。
        # （排查启示：只发一个 ContextMenu 事件是复现不出来的，
        #   必须补上真实的 MouseButtonPress / Release —— 上一版探针就漏了这两个，
        #   于是"修复"了一个根本没被观察到的现象。）
        # 所以把右键的按下/松开也吃掉，容器收不到就不会收列表。
        # 注：ContextMenu 事件是操作系统合成的，不依赖 Qt 的鼠标事件，
        # 所以吃掉它们不会影响右键菜单本身。
        if event.type() in (QEvent.Type.MouseButtonPress,
                            QEvent.Type.MouseButtonRelease):
            return isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.RightButton

        if event.type() != QEvent.Type.ContextMenu:
            return False

        if obj is self._combo:
            # 下拉未展开时在下拉框本身上右键：作用在当前选中项（省一次展开）
            row = self._combo.currentIndex()
            global_pos = self._combo.mapToGlobal(event.pos())
        else:
            row, global_pos = self._row_at_event(event)
            if row < 0:
                # 点在条目之间的空白处：吃掉事件但不弹菜单，
                # 免得莫名其妙作用到别的条目上
                return True

        QTimer.singleShot(
            0, lambda: self._window._open_item_menu(
                self._combo, self._kind, row, global_pos
            )
        )
        return True          # 吃掉事件：Qt 不再走它自己的弹窗处理

    def _row_at_event(self, event) -> tuple[int, object]:
        """把弹窗里的右键位置换算成 (行号, 屏幕坐标)。行号 -1 表示没点在条目上。"""
        view = self._combo.view()
        pos = event.pos()
        index = view.indexAt(pos)
        if not index.isValid():
            return -1, None
        return index.row(), view.viewport().mapToGlobal(pos)


def _install_exception_guard(window: "MainWindow") -> None:
    """把"槽函数里未捕获的 Python 异常"从"直接闪退"变成"可读的报错弹窗"。

    为什么必须装这个（这是本项目真实踩过的坑）：
        PyQt ≥ 5.5 的默认行为是——当 Python 异常逃逸出 Qt 槽函数时，
        PyQt 先打印 traceback，然后调用 qFatal() **直接终止进程**。
        于是用户看到的是"程序毫无征兆地消失"，而不是一个能读的错误。

        真实案例：main_window._append_table_row 里误用了未定义变量
        （`elide_middle(name, 70)` 应为 `path.name`），导致"一添加图片就闪退"，
        栈顶只留下一行孤零零的 NameError。定位成本极高，而它本可以是一个弹窗。

        修复方式：替换 sys.excepthook（PyQt 会调用它，替换后不再走 qFatal）。
        同时替换 threading.excepthook：工作线程里的未捕获异常原本也只是
        静默打印到 stderr，GUI 模式下 stderr 不可见，等于丢失。

    注意：这只影响 Python 层的异常。程序自身的错误处理（失败记账、
    failed.json、逐张 try/except）不受影响——它们本来就该被捕获。
    这里是最后一道安全网，用来兜住"我没预料到的编程错误"。
    """

    def handle(exc_type, exc_value, exc_tb) -> None:
        # Ctrl+C 保持默认行为（正常打印），不弹窗打断调试。
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("界面回调发生未捕获异常（已拦截，程序继续运行）：\n%s", detail)

        try:
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("发生内部错误（程序未退出）")
            box.setText(
                f"界面回调里出现未预期的错误：\n\n{exc_type.__name__}: {exc_value}\n\n"
                "程序已拦截该错误并继续运行，你可以继续操作。\n"
                "完整调用栈已写入日志文件（界面上的「打开日志目录」按钮可直达）。"
            )
            box.setDetailedText(detail)
            box.exec()
        except Exception:
            # 连弹窗都失败（例如 Qt 已在销毁过程中）时，至少日志里已经留下了记录。
            pass

    sys.excepthook = handle

    def threading_handle(args) -> None:
        handle(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = threading_handle  # type: ignore[assignment]


def _qt_argv(argv: list[str]) -> list[str]:
    """把 argv 里**属于我们自己**的同名选项摘掉，再交给 Qt。

    【为什么 —— 实测撞到的】`QApplication` 会自己先解析 `-style` / `--style`
    （Qt 的风格选项），而 `--style` 正好是**我们的**风格名选项。GUI 模式下
    两者同名，Qt 会把风格名当成 Qt 风格去加载，于是控制台上出现
    `QApplication: invalid style override 'AI自主决策' passed, ignoring it.`
    —— 用户看到的是"程序对我的参数报了句看不懂的警告"。

    只摘我们自己那几个与 Qt 撞名的键（当前只有 `--style`），
    Qt 自己的单横线选项（`-style Fusion` / `-platform offscreen`）照旧可用。
    """
    out = [argv[0]] if argv else []
    skip_next = False
    for item in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if item == "--style":
            skip_next = True       # 连它的值一起摘掉
            continue
        if item.startswith("--style="):
            continue
        out.append(item)
    return out


def run_app(argv: list[str] | None = None) -> int:
    """程序化启动入口（供 app.py 调用）。"""
    from PyQt6.QtWidgets import QApplication

    # AppUserModelID 必须在**创建 QApplication 之前**设：Windows 用它决定
    # "哪些窗口属于同一个应用"与任务栏上显示谁的图标，设晚了对本进程已建的窗口不生效。
    set_app_user_model_id()
    app = QApplication(_qt_argv(argv if argv is not None else sys.argv))
    app.setApplicationName("Auto Color Diffusion")
    # 给 QApplication 也装一份图标：窗口没建出来之前弹出的对话框
    #（以及任务栏预览）会取它。取不到就跳过 —— 缺图标不该拦住启动。
    app_icon = load_app_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    # 中文化 Qt 自带文案（输入框右键菜单、QInputDialog 的 OK/Cancel 等）。
    # 必须在构造窗口之前装：窗口构造时就会建出输入框，菜单文案在弹出时才取，
    # 但确定/取消这类被缓存的文案要早装才不会漏。
    install_chinese_translations(app)
    window = MainWindow()
    # 必须在 show() 之前安装：否则首次交互时抛出的异常仍会走 qFatal 闪退。
    _install_exception_guard(window)
    window.show()
    # Windows 11 上尽力套上 Mica 云母背景；失败就保持纯色。
    if apply_mica(window):
        window._enable_transparent_root()
    return app.exec()
