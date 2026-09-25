# -*- coding: utf-8 -*-
"""复用控件。

重点：文件选择对话框的后缀筛选（硬约束 #17）
------------------------------------------------
Qt 的 QFileDialog.setNameFilters 在 Windows 上对后缀是**大小写敏感**的
（不敏感的是文件系统，不是筛选器字符串）。
真实相机产出的扩展名绝大多数是**大写**（.CR3 / .NEF / .ARW / .DNG），
因此如果只写小写模式，用户会看到"文件夹里明明有照片，对话框里却是空的"。

本模块同时做两件事来保证这件事落地：
    1. 生成筛选器时**大小写两套后缀都列出**（12 种格式 × 2 = 24 个模式）；
    2. 提供「显示所有文件」兜底选项，万一某台机器上的 Qt 行为异常，
       用户仍能通过"所有文件"选到目标。

另一个重点：输出质量滑块（QualitySlider）
------------------------------------------------
质量值的**唯一权威表示是 Photoshop 的 0–12 刻度**，因此界面直接用滑块暴露这
13 档，不再经过 libjpeg 语义的档位名中转。原因见 constants 里 PS_JPEG_QUALITY_MIN
的注释（档位名与真实产出会对不上）。
"""

from __future__ import annotations

from pathlib import Path

import sys

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    DEFAULT_PS_JPEG_QUALITY,
    LOW_QUALITY_WARN_BADGE,
    LOW_QUALITY_WARN_COLOR,
    PS_JPEG_QUALITY_MAX,
    PS_JPEG_QUALITY_MIN,
    RAW_EXTENSIONS,
    all_suffixes_for_dialog,
    clamp_ps_quality,
    describe_low_quality_warning,
    describe_ps_quality,
    is_very_low_quality,
)

# 白名单后缀的展示文本（大写形式，给用户看）。
_SUFFIX_DISPLAY = " ".join(ext.upper() for ext in RAW_EXTENSIONS)

# 文字四周的呼吸位（px）。QSS 里 QLabel 的左右内边距用的就是这个值。
#
# 【为什么必须留】CJK 字形的墨迹几乎顶到笔起点：实测“就绪”在 125% 缩放下，
# 最左那一列只有 2 个抗锯齿像素，而且**正好压在控件第 0 像素列上**。
# 墨迹总量并没有丢（把绘制起点从 0 挪到 1.2px，墨量恒为 156），
# 但看上去就像最左边那一笔被削掉了（用户反馈过）。
# 留一点呼吸位，墨迹就不会贴着控件边界画。
LABEL_TEXT_PADDING = 2


def quality_caption(value: int, *, compact: bool) -> str:
    """滑块上方那一行说明文字（含极低画质的警示徽标）。

    【为什么单独抽出来】"画什么文字"与"需要多宽"必须来自同一处：
    分开写就会出现"宽度按旧文案算、文字按新文案画"，于是又截断。

    极低画质那几档**只写徽标 + 数值**：徽标已经说明很危险了，
    再拼上"极限压缩"会把这一行撑到 234px，结果最重要的警示反倒被截断。
    完整警告（含后果与“这个值是合法的”）放在悬停说明里。
    """
    if is_very_low_quality(value):
        return f"{LOW_QUALITY_WARN_BADGE}　{value} / {PS_JPEG_QUALITY_MAX}"
    return describe_ps_quality(value, compact=compact)


def _caption_required_width(font: QFont, *, compact: bool) -> int:
    """这一行在**所有档位**下需要的最大宽度（实测像素，不是字符数）。

    量字体而不是量字符：换字体、改缩放比例、改文案都不用动代码。
    本项目就吃过写死宽度的亏 —— 原先写 150，而 12 档（正好是默认值）需要 156，
    一打开程序就能看到"12 / 12　最高质…"。

    极低画质那几档要按**加粗**字体量：那个状态的内联样式把字重改了，
    加粗后更宽，按常规字重量出来的宽度会不够（这个也踩过）。
    """
    bold = QFont(font)
    bold.setBold(True)
    regular_metrics = QFontMetrics(font)
    bold_metrics = QFontMetrics(bold)
    widest = 0
    for value in range(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX + 1):
        text = quality_caption(value, compact=compact)
        metrics = bold_metrics if is_very_low_quality(value) else regular_metrics
        widest = max(widest, metrics.horizontalAdvance(text))
    # 再加 QSS 给 QLabel 的左右内边距：内边距是**控件宽度**的一部分，
    # 不计进来的话，算出来的是“文字刚好放得下”的宽度，实际还会短 4px 而被截断。
    return widest + 2 * LABEL_TEXT_PADDING


def raw_file_filter() -> str:
    """构造 QFileDialog 的筛选器字符串。

    形态：RAW 照片 (*.cr3 *.CR3 *.cr2 *.CR2 ...);;所有文件 (*)
    """
    patterns = " ".join(all_suffixes_for_dialog())
    return f"RAW 照片 ({patterns});;所有文件 (*)"


def choose_raw_files(parent: QWidget | None = None, start_dir: str = "") -> list[Path]:
    """打开"添加照片"文件选择框，返回选中的路径。"""
    files, _ = QFileDialog.getOpenFileNames(
        parent,
        f"选择 RAW 照片（支持：{_SUFFIX_DISPLAY}）",
        start_dir,
        raw_file_filter(),
    )
    return [Path(f) for f in files]


def choose_directory(parent: QWidget | None = None, caption: str = "选择文件夹", start_dir: str = "") -> Path | None:
    """选择一个文件夹。"""
    directory = QFileDialog.getExistingDirectory(parent, caption, start_dir)
    return Path(directory) if directory else None


def choose_xmp_and_raw_pairs(parent: QWidget | None = None, start_dir: str = "") -> list[Path]:
    """训练模式：选择 RAW 或 XMP 文件（配对逻辑由 pipeline 负责）。"""
    patterns = " ".join(all_suffixes_for_dialog() + ["*.xmp", "*.XMP"])
    files, _ = QFileDialog.getOpenFileNames(
        parent,
        "选择训练样本（RAW 与其同名的 XMP 旁侧文件）",
        start_dir,
        f"RAW 与 XMP ({patterns});;RAW 照片 ({' '.join(all_suffixes_for_dialog())});;XMP 设置 (*.xmp *.XMP);;所有文件 (*)",
    )
    return [Path(f) for f in files]


class SecretLineEdit(QLineEdit):
    """密钥输入框。

    行为（硬约束 #3）：
        - 未填时用 placeholder 展示掩码形式（sk-****xxxx）；
        - 输入时用密码回显模式，避免肩窥；
        - 提供「显示」切换，便于用户核对粘贴是否正确。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setPlaceholderText("在此粘贴 API 密钥（不会写入任何文件，仅存入系统密钥链）")
        self._toggle_button: QPushButton | None = None

    def attach_toggle(self, button: QPushButton) -> None:
        """把一个按钮绑定为"显示/隐藏"切换。"""
        self._toggle_button = button
        button.setCheckable(True)
        button.toggled.connect(self._on_toggle)

    def _on_toggle(self, checked: bool) -> None:
        self.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        if self._toggle_button is not None:
            self._toggle_button.setText("隐藏" if checked else "显示")

    def show_masked_placeholder(self, masked: str) -> None:
        """把已保存的密钥以掩码形式展示在 placeholder 里。

        刻意**不**把真实密钥填进输入框：输入框内容可能被"复制到剪贴板"
        或屏幕共享泄露，而 placeholder 只是提示"这里已有一个密钥"。
        用户若想替换，直接在框里输入新值即可。
        """
        self.setPlaceholderText(f"已保存密钥：{masked}（留空表示继续使用）")
        self.clear()

    def clear_masked_placeholder(self) -> None:
        """把 placeholder 恢复成"没有已保存密钥"的原始文案。

        删除密钥后必须调用它：否则输入框还写着「已保存密钥：sk-****a1b2」，
        而实际上密钥已经被删掉了 —— 用户会以为删除没生效。
        界面上的每一处提示都必须与真实状态一致（本项目的老规矩）。
        """
        self.setPlaceholderText("在此粘贴 API 密钥（不会写入任何文件，仅存入系统密钥链）")
        self.clear()


class LabeledPathPicker(QWidget):
    """一行：[标签] [路径输入框] [浏览按钮]。"""

    def __init__(
        self,
        label: str,
        button_text: str = "浏览…",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(label)
        self._label.setMinimumWidth(88)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("留空则使用默认位置")
        self.button = QPushButton(button_text)
        self.button.setFixedWidth(80)

        layout.addWidget(self._label)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)


class QualitySlider(QWidget):
    """输出 JPG 质量滑块：直接使用 Photoshop 的 0–12 刻度。

    【为什么是滑块而不是四档下拉】这是本项目做过的一次重要修正：
        原方案的下拉是 libjpeg 语义的四档（低/中/高/最高），需要再映射到
        Photoshop 的 0–12。但 PS 只有 13 档，四档压进去后必然有档位撞车、
        产出完全相同的文件；而真实的 libjpeg q100 在 PS 里根本无法表达
        （上限 12 约等于 libjpeg 97），于是"最高"这个档位名与实际产出对不上。
        改成直接暴露 0–12 后，用户选什么、Photoshop 就得到什么。

    控件构成：一行"数值 + 定性描述 + libjpeg 等效值"的实时文字，加一根带刻度的
    滑块。文字一律由 constants.describe_ps_quality() 生成，**不在这里复制一份
    文案**——以前就是界面文案与常量表两份数据，改了常量表界面就开始说谎。
    """

    # 值变化信号（转发内部 QSlider 的）。
    # 界面层要"记住用户选的输出质量"，但不应该去戳内部属性 self.slider ——
    # 那是实现细节，一旦以后换控件就会静默失效。这里开一个正式出口。
    valueChanged = pyqtSignal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        compact: bool = False,
    ) -> None:
        super().__init__(parent)
        # compact=True 用于"与开始按钮同一行"的底栏：那里空间紧张，
        # 说明文字与滑块都收窄、并且不画刻度。
        self._compact = compact

        # 说明文字。最小宽度在 refresh_caption() 里按**当前字体**实测后设置，
        # 不能只在这里算一次：构造时主题（QSS 里的 font-family）还没装上，
        # 量到的是默认字体，主题一装字体就变，原先算好的宽度就不够了。
        self._caption = QLabel()

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(PS_JPEG_QUALITY_MIN, PS_JPEG_QUALITY_MAX)
        self.slider.setValue(DEFAULT_PS_JPEG_QUALITY)
        # 每一档画一个刻度：13 档跨度不大，全部标出比只标端点更有指导性。
        # 紧凑模式不画刻度：底栏高度有限，刻度线反而显得拥挤。
        if not compact:
            self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            self.slider.setTickInterval(1)
        # singleStep / pageStep 都设为 1：默认 pageStep 是 10，
        # 按一下方向键或翻页键会直接跳十档，在 0–12 的尺度上等于跳过整个区间。
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        # 紧凑模式下滑块只要 96px：底栏那一行同时挤着四个控件，
        # 滑块给得太多就会把说明文字挤到显示不全（反过来说，滑块本身
        # 是 stretch 的，窗口宽时它会自己变长，不差这一点最小宽度）。
        self.slider.setMinimumWidth(96 if compact else 240)
        self.slider.setToolTip(
            f"Photoshop 的 JPEG 质量刻度，范围 {PS_JPEG_QUALITY_MIN}–{PS_JPEG_QUALITY_MAX}；"
            f"数值越大画质越高、文件越大。{PS_JPEG_QUALITY_MAX} 是 Photoshop 的上限。"
        )
        self.slider.valueChanged.connect(self._refresh_caption)
        self.slider.valueChanged.connect(self.valueChanged.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._caption)
        layout.addWidget(self.slider)

        self._refresh_caption(self.slider.value())

    def value(self) -> int:
        """当前 Photoshop 质量刻度（0–12）。"""
        return self.slider.value()

    def setValue(self, value: int) -> None:  # noqa: N802 —— 跟随 Qt 的驼峰命名
        """设置质量值。超出 0–12 会被**夹紧**而不是抛异常。

        夹紧而非报错，是因为这个值可能来自用户手写的命令行参数或旧配置文件，
        为了一个越界数字直接让程序失败，体验上得不偿失；实际使用的值会打印在日志里。
        """
        self.slider.setValue(clamp_ps_quality(value))

    def _refresh_caption(self, value: int) -> None:
        """滑块移动时刷新说明文字；极低画质时标红并加警示前缀。

        为什么这里只标红、不弹模态框：滑块是连续控件，用户从 12 拖到 0
        会逐档经过每一个值，在 valueChanged 里弹框会让拖动直接卡死
        （而且是连续弹十几次）。真正的拦截放在"开始导出"那一步，
        一次任务只问一次。
        """
        text = quality_caption(value, compact=self._compact)
        # 完整说明（含 libjpeg 等效值）放 tooltip：主文案为了放得下已经把
        # 括号里的内容去掉了，信息不能就这么丢掉。
        full = describe_ps_quality(value)
        # 最小宽度必须**在这里**校正（而不是只在构造时算一次）：
        # 字体可能已经变了（主题刚装上 / 换了主题）。值没变就不重复设，
        # 否则每拖一下滑块都会触发一次布局重排。
        needed = _caption_required_width(self._caption.font(), compact=self._compact)
        if needed != self._caption.minimumWidth():
            self._caption.setMinimumWidth(needed)
        if is_very_low_quality(value):
            self._caption.setText(text)
            self._caption.setStyleSheet(
                f"color: {LOW_QUALITY_WARN_COLOR}; font-weight: bold;"
            )
            # 这一行放不下完整警告，悬停时给出全文。
            self._caption.setToolTip(describe_low_quality_warning(value))
        else:
            self._caption.setText(text)
            # 必须显式清空：内联样式会一直粘在控件上，
            # 否则用户把滑块拖回高档后标签仍然是红的。
            self._caption.setStyleSheet("")
            self._caption.setToolTip(full)

    def caption_text(self) -> str:
        """当前说明文字（含可能的警示徽标）。"""
        return self._caption.text()

    def refresh_caption(self) -> None:
        """按当前值重新刷一遍说明文字（同时校正它需要的最小宽度）。

        对外暴露，是因为**换主题后必须再调一次**：QSS 里的 font-family 会改变
        字体，字体一变，"文字需要多宽"就变了。构造时量到的是默认字体，
        不重算的话换完主题又会截断。
        """
        self._refresh_caption(self.slider.value())

    def caption_visible_width(self) -> int:
        """说明文字真正能被看到的宽度（把**父级裁剪**算进去）。

        自己沿父链求交集，而不用 `visibleRegion()`：
        窗口还没 `show()` 时 visibleRegion() 是空的，于是每一档都会被判成
        "被裁"——自检会误报（这个假结论又白跑了一轮）。
        沿父链求交集不依赖窗口是否已显示。
        """
        window = self._caption.window()
        rect = QRect(
            self._caption.mapTo(window, QPoint(0, 0)), self._caption.size()
        )
        parent = self._caption.parentWidget()
        while parent is not None:
            rect = rect.intersected(
                QRect(parent.mapTo(window, QPoint(0, 0)), parent.size())
            )
            parent = parent.parentWidget()
        return max(0, rect.width())

    def caption_fits(self) -> bool:
        """当前说明文字是否**真的**能完整显示。

        用户反馈的"输出质量滑块在 12 时显示不完整"就靠这一条守住。
        """
        metrics = QFontMetrics(self._caption.font())
        return metrics.horizontalAdvance(self._caption.text()) <= self.caption_visible_width()

    def caption_tooltip(self) -> str:
        """当前说明文字的悬停提示。"""
        return self._caption.toolTip()

    def is_warning_shown(self) -> bool:
        """当前是否真的处于警示视觉状态（徽标 + 红字样式都生效）。

        暴露成方法而不是让调用方去读 _caption 的样式字符串，是为了把
        "警示长什么样"这件事只定义在这一处。自检脚本只关心"警示显示了没有"，
        不应因为以后换了主题色就跟着改。
        """
        return (
            LOW_QUALITY_WARN_BADGE in self._caption.text()
            and LOW_QUALITY_WARN_COLOR in self._caption.styleSheet()
        )


# 状态文字的颜色。与 LIGHT_THEME_QSS 一样属于**界面样式**，所以放在本模块，
# 而不是 constants.py（那里放的是会影响行为与产出的数值）。
# 三色在项目使用的浅色主题（白底）上对比度均 > 4.5:1，满足 WCAG AA。
STAGE_STATE_COLORS: dict[str, str] = {
    "ok": "#1e8449",        # 深绿：全部成功
    "failed": "#c0392b",    # 深红：有失败（与"极低画质"警示色同值，保持警示语义一致）
    "stopped": "#b9770e",   # 琥珀：用户主动停止，不是错误，不该用红色
}

# 置顶条目的视觉标识。
#
# 为什么要"星号 + 加粗"两样一起用：★ (U+2605) 依赖 Windows 的字符回退
# （Qt6 在 Windows 上走 DirectWrite，会逐字符找有该字形的字体）才画得出来。
# 万一某个环境下回退失败，加粗仍然能让用户一眼看出哪几条是置顶的——
# 标识退化但不丢失。只靠一个特殊字符的话，缺字形就完全看不出置顶了。
PIN_PREFIX = "★ "


def pinned_font(base: QFont) -> QFont:
    """置顶条目用的字体（加粗），与 PIN_PREFIX 配合构成置顶标识。"""
    font = QFont(base)
    font.setBold(True)
    return font


def heading_font() -> QFont:
    """区段标题字体：加粗、略大。白底黑字主题下靠字重区分层级。

    踩坑记录：`QFont()` 默认构造出来的对象**没有设定字号**，
    此时 `pointSize()` 返回 **-1**（Qt 用 -1 表示"跟随系统默认"）。
    直接写 `setPointSize(font.pointSize() + 1)` 就会变成 `setPointSize(0)`，
    Qt 会打印 `QFont::setPointSize: Point size <= 0 (-1), must be greater than 0`，
    而且字体实际不会变大——等于白写还刷屏。
    正确做法是先判断字号有效再 +1；无效时不动字号，只保留加粗。
    """
    font = QFont()
    font.setBold(True)
    current_size = font.pointSize()
    if current_size > 0:
        font.setPointSize(current_size + 1)
    return font


def elide_middle(text: str, limit: int = 60) -> str:
    """中间省略的长路径显示。"""
    if len(text) <= limit:
        return text
    keep = (limit - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


def make_status_badge(text: str, ok: bool) -> QLabel:
    """生成一个状态徽标（用于 exiftool / Photoshop / keyring 的检测结果）。"""
    label = QLabel(text)
    label.setWordWrap(True)
    color = "#0a7a0a" if ok else "#b00020"
    label.setStyleSheet(f"color: {color};")
    return label


# ============================================================================
# Windows 11 Fluent 主题（浅色 / 深色）
# ============================================================================
# 设计要点：
#   - 强调色用 Win11 默认蓝 #0078D4；
#   - 圆角统一 6–8px；分层靠"背景色差 + 细边框 + 柔和投影"，不靠生硬描边；
#   - 字体优先 Segoe UI Variable，回退到系统无衬线。
# QSS 无法表达 box-shadow，所以卡片的柔和投影由 Card 控件（QGraphicsDropShadowEffect）
# 呈现；悬停/按下的颜色变化由 :hover / :pressed 伪状态提供。
ACCENT = "#0078D4"          # Win11 默认强调色
ACCENT_HOVER = "#106EBE"    # 悬停略深
ACCENT_PRESSED = "#005A9E"  # 按下更深


def fluent_qss(dark: bool) -> str:
    """生成 Fluent 风格样式表。dark=True 为深色模式。"""
    if dark:
        base = "#202020"
        card = "#2C2C2C"
        text = "#FFFFFF"
        subtext = "#A8A8A8"
        border = "#3D3D3D"
        input_bg = "#1E1E1E"
        hover = "#363636"
        pressed = "#3A3A3A"
        table_grid = "#3A3A3A"
        header_bg = "#2C2C2C"
        selection_bg = "#0F4C78"
        scroll_bg = "#2C2C2C"
        scroll_handle = "#5A5A5A"
        disabled_fg = "#6E6E6E"
        disabled_bg = "#333333"
        seg_checked_bg = "#0F4C78"
    else:
        base = "#F3F3F3"
        card = "#FFFFFF"
        text = "#1B1B1B"
        subtext = "#5D5D5D"
        border = "#E0E0E0"
        input_bg = "#FFFFFF"
        hover = "#F5F5F5"
        pressed = "#EBEBEB"
        table_grid = "#F0F0F0"
        header_bg = "#FAFAFA"
        selection_bg = "#D6E4F7"
        scroll_bg = "#FFFFFF"
        scroll_handle = "#C4C4C4"
        disabled_fg = "#A0A0A0"
        disabled_bg = "#FAFAFA"
        seg_checked_bg = "#D6E4F7"

    return f"""
QWidget {{
    font-family: "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
    font-size: 13px;
    color: {text};
}}
QMainWindow, #appRoot {{
    background-color: {base};
}}
QFrame#card {{
    background-color: {card};
    border: 1px solid {border};
    border-radius: 8px;
}}
QLabel {{
    /* 左右各留一点呼吸位：CJK 字形墨迹顶到笔起点，贴着控件边界时
       最左那一列会被抗锯齿压成 2 个像素，看起来像被切掉（用户反馈过）。
       数值与 widgets.LABEL_TEXT_PADDING 保持一致。 */
    padding-left: 2px;
    padding-right: 2px;
}}
QLabel#cardTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {text};
}}
QLabel#hintLabel {{
    color: {subtext};
}}
QLabel#envLabel {{
    color: {subtext};
    font-size: 12px;
}}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {input_bg};
    color: {text};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: 0;
    width: 22px;
}}
QPushButton {{
    background-color: {card};
    color: {text};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}}
QPushButton:hover {{ background-color: {hover}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background-color: {pressed}; }}
QPushButton:disabled {{ color: {disabled_fg}; background-color: {disabled_bg}; border-color: {border}; }}
/* 禁用态一律走"变灰"表达（不隐藏、不改布局宽度）。
   必须给 :disabled 显式配色：QSS 里写死的 color 不受调色板禁用组影响，
   少了这几条，控件被 setEnabled(False) 之后颜色一点不变（用户根本看不出来）。 */
QLabel:disabled, QCheckBox:disabled {{ color: {disabled_fg}; }}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled {{
    color: {disabled_fg};
    background-color: {disabled_bg};
    border-color: {border};
}}
QPushButton#primary {{
    background-color: {ACCENT};
    color: #FFFFFF;
    border: 1px solid {ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#primary:disabled {{ background-color: #7FB4E6; border-color: #7FB4E6; color: #F0F0F0; }}
QPushButton#danger {{ color: #C42B1C; border-color: #C42B1C; }}
QProgressBar {{
    border: 0;
    border-radius: 3px;
    background-color: {hover};
    text-align: center;
    color: {subtext};
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}
QTableWidget, QListWidget {{
    background-color: {card};
    border: 1px solid {border};
    border-radius: 6px;
    gridline-color: {table_grid};
    selection-background-color: {selection_bg};
    selection-color: {text};
}}
QHeaderView::section {{
    background-color: {header_bg};
    color: {text};
    border: 0;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
    padding: 6px;
    font-weight: 600;
}}
QCheckBox {{ spacing: 8px; color: {text}; }}
QPushButton#seg {{
    background-color: {card};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 16px;
}}
QPushButton#seg:checked {{
    background-color: {seg_checked_bg};
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton#seg:hover {{ border-color: {ACCENT}; }}
QMenuBar {{ background-color: {card}; color: {text}; border-bottom: 1px solid {border}; }}
QMenuBar::item {{ background: transparent; padding: 6px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background-color: {hover}; }}
QMenu {{ background-color: {card}; color: {text}; border: 1px solid {border}; border-radius: 8px; }}
QMenu::item {{ padding: 6px 26px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {hover}; }}
QDialog {{ background-color: {base}; }}
QScrollBar:vertical {{ background: {scroll_bg}; width: 12px; border: 0; }}
QScrollBar::handle:vertical {{ background: {scroll_handle}; border-radius: 6px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {scroll_bg}; height: 12px; border: 0; }}
QScrollBar::handle:horizontal {{ background: {scroll_handle}; border-radius: 6px; min-width: 24px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolTip {{ background-color: {card}; color: {text}; border: 1px solid {border}; border-radius: 4px; }}
"""


def apply_theme(app, dark: bool) -> None:
    """把整个应用的样式切成浅色 / 深色。"""
    app.setStyleSheet(fluent_qss(dark))


FLUENT_LIGHT_QSS = fluent_qss(False)
FLUENT_DARK_QSS = fluent_qss(True)
# 兼容旧引用：旧代码里 `LIGHT_THEME_QSS` 就是"默认浅色主题"。
LIGHT_THEME_QSS = FLUENT_LIGHT_QSS


class Card(QFrame):
    """Fluent 卡片：8px 圆角 + 柔和投影。

    QSS 没有 box-shadow，所以柔和投影用 QGraphicsDropShadowEffect 实现；
    圆角与底色由 QSS 的 `QFrame#card` 规则提供。
    布局通过 `card.body`（QVBoxLayout）往里加内容。
    """

    def __init__(self, title: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 32))
        self.setGraphicsEffect(shadow)

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)

        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.body.addWidget(label)


def apply_mica(window) -> bool:
    """尽力给窗口套上 Windows 11 的 Mica 云母背景。返回是否成功。

    DwmSetWindowAttribute 是 Windows 11（build ≥ 22000）的半公开 API，
    旧系统没有它。这里用 ctypes 直调，任何一步失败都静默降级为纯色背景。
    成功之后，调用方应把主窗口中央控件的背景设为透明，让云母透出来；
    卡片保持不透明，保证文字可读。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        build = int(sys.getwindowsversion().build or 0)
        if build < 22000:
            return False

        # 未导出的常量：DWMWA_SYSTEMBACKDROP_TYPE = 38；DWMSBT_MAINWINDOW = 2。
        dwmapi = ctypes.WinDLL("dwmapi")
        hwnd = int(window.winId())
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(38),
            ctypes.byref(ctypes.c_int(2)),
            ctypes.sizeof(ctypes.c_int),
        )
        return True
    except Exception:
        return False
