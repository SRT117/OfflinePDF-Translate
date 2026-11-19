# SPDX-License-Identifier: AGPL-3.0-only
"""共用的本地化基础 Qt 控件包装器与自适应流式布局，确保界面语言切换时自动重译并防止文字截断。"""

from pathlib import Path
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox as QtCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel as QtLabel,
    QLayout,
    QPushButton as QtPushButton,
    QSizePolicy,
    QStyleFactory,
    QWidget,
)

from core import i18n


# ---------------------------------------------------------------------------
# 瑞士包豪斯 2.0 设计令牌 (谦虚、自信、大气、低眩光)
# ---------------------------------------------------------------------------
THEME = {
    "canvas": "#F5F3EE",        # 典藏暖纸底色
    "surface": "#FFFFFF",       # 卡片纯纸白
    "surface_soft": "#FAF9F6",  # 柔和次级表面
    "surface_muted": "#EFECE6", # 抽屉/输入衬底
    "surface_pill": "#F2EFE9",  # 静默徽标底色
    "border": "#DCD8CE",        # 1px 瑞士网格发丝线
    "border_strong": "#B8B2A6", # 悬停/次强边界
    "ink": "#161616",           # 主墨色
    "ink_secondary": "#57534E", # 暖石灰次级文字
    "ink_muted": "#8C867C",     # 静默辅助文字
    "cobalt": "#1D4ED8",        # 进行中 / 聚焦 / 缓存
    "cobalt_soft": "#EFF6FF",
    "cobalt_border": "#BFDBFE",
    "sage": "#15803D",          # 完成 / 校验通过
    "sage_soft": "#ECFDF5",
    "sage_border": "#BBF7D0",
    "ochre": "#B45309",         # 警告 / 停止 / 修复
    "ochre_soft": "#FFFBEB",
    "ochre_border": "#FDE68A",
    "vermilion": "#C82820",     # 报错 / 中止
    "vermilion_soft": "#FEF2F2",
    "vermilion_border": "#FECACA",
}


def ui_icon_url(filename: str) -> str:
    """返回 assets/ui 下 PNG 图标的 QSS url() 路径（正斜杠，兼容 Qt StyleSheet）。"""
    path = Path(__file__).resolve().parent.parent / "assets" / "ui" / filename
    return path.as_posix()


def draw_bauhaus_empty_emblem(size: int = 44, active: bool = False) -> QPixmap:
    """绘制极简自信的包豪斯双页对译几何徽标（纯矢量 QPainter，2x HiDPI 清晰渲染）。"""
    dpr = 2
    px = QPixmap(size * dpr, size * dpr)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)

    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    ink = QColor(THEME["cobalt"] if active else THEME["ink"])
    muted_line = QColor(THEME["cobalt_border"] if active else THEME["border_strong"])
    accent_blue = QColor(THEME["cobalt"])
    accent_red = QColor(THEME["vermilion"])

    # 后页（原文献层）
    p.setPen(QPen(muted_line, 1.5))
    p.setBrush(QColor(THEME["surface_soft"]))
    p.drawRect(QRectF(6, 5, 22, 28))

    # 前页（译文层，微错位体现双语对照）
    p.setPen(QPen(ink, 1.6))
    p.setBrush(QColor(THEME["surface"]))
    p.drawRect(QRectF(15, 11, 23, 28))

    # 三原色克制几何点睛：左下钴蓝条（原文流）与右上朱红角标（译文印记）
    p.setPen(Qt.NoPen)
    p.setBrush(accent_blue)
    p.drawRect(QRectF(19, 20, 12, 2.5))
    p.setBrush(muted_line)
    p.drawRect(QRectF(19, 25.5, 15, 2))
    p.drawRect(QRectF(19, 30.5, 10, 2))
    p.setBrush(accent_red)
    p.drawRect(QRectF(32, 11, 6, 6))

    p.end()
    return px


def display_ui_text(text):
    text = i18n.tr(text)
    if isinstance(text, str):
        # These decorative symbols are not in the bundled fonts.
        for symbol in ("⚡ ", "⚓ ", "✕ ", "⏱ "):
            text = text.replace(symbol, "")
    return text


def build_light_bauhaus_palette() -> QPalette:
    """构造低眩光高可读的浅色瑞士包豪斯调色板，避免跟随系统深色主题或 windows11 样式发黑。"""
    pal = QPalette()
    bg = QColor(THEME["canvas"])
    white = QColor("#ffffff")
    ink = QColor("#121212")
    muted = QColor(THEME["ink_muted"])
    pal.setColor(QPalette.Window, bg)
    pal.setColor(QPalette.WindowText, ink)
    pal.setColor(QPalette.Base, white)
    pal.setColor(QPalette.AlternateBase, bg)
    pal.setColor(QPalette.Text, ink)
    pal.setColor(QPalette.Button, white)
    pal.setColor(QPalette.ButtonText, ink)
    pal.setColor(QPalette.Highlight, QColor(THEME["cobalt"]))
    pal.setColor(QPalette.HighlightedText, white)
    pal.setColor(QPalette.ToolTipBase, white)
    pal.setColor(QPalette.ToolTipText, ink)
    pal.setColor(QPalette.Disabled, QPalette.Text, muted)
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, muted)
    return pal


def configure_localized_combo(combo: QComboBox, max_visible_items: int = 12) -> QComboBox:
    """让下拉框随语言切换自动重算内容宽度，并防止被父布局挤压截断、弹窗发黑或超屏展开。"""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    combo.setMaxVisibleItems(max_visible_items)
    # Qt6 Windows 11 原生样式在自定义 QComboBox QSS 时会把弹出菜单画成黑底，切换为 Fusion 并显式赋予浅色调色板
    cur_style = QApplication.style()
    if cur_style is not None and cur_style.objectName().lower() == "windows11":
        fusion = QStyleFactory.create("Fusion")
        if fusion is not None:
            combo.setStyle(fusion)
    pal = build_light_bauhaus_palette()
    combo.setPalette(pal)
    view = combo.view()
    if view is not None:
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        if cur_style is not None and cur_style.objectName().lower() == "windows11":
            fusion_view = QStyleFactory.create("Fusion")
            if fusion_view is not None:
                view.setStyle(fusion_view)
        view.setPalette(pal)
        popup = view.window()
        if popup is not None:
            popup.setPalette(pal)
    return combo


def make_inline_group(*widgets: QWidget, spacing: int = 6) -> QWidget:
    """将标签与控件绑定为一个不可拆分的紧凑内联组（选择器限定自身，避免样式穿透子控件）。"""
    container = QWidget()
    container.setObjectName("InlineGroup")
    container.setStyleSheet("#InlineGroup { background: transparent; border: none; }")
    container.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for w in widgets:
        layout.addWidget(w)
    return container


def refresh_layouts(root: QWidget) -> None:
    """自底向上失效并刷新容器布局，确保语言切换后尺寸即时同步。"""
    from PySide6.QtWidgets import QLayout

    for lay in root.findChildren(QLayout):
        lay.invalidate()
    for child in root.findChildren(QWidget):
        child.updateGeometry()
    root.updateGeometry()
    if root.layout() is not None:
        root.layout().invalidate()
        root.layout().activate()


class FlowLayout(QLayout):
    """自适应换行布局：空间充足时单行排布，不足时自动折行，绝不挤压截断控件文字。"""

    def __init__(
        self,
        parent: QWidget = None,
        margin: int = 0,
        h_spacing: int = 8,
        v_spacing: int = 6,
        justify_last_on_first_row: bool = False,
    ):
        super().__init__(parent)
        self._items = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._justify_last_on_first_row = justify_last_on_first_row
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):
        while self._items:
            self.takeAt(0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        margins = self.contentsMargins()
        visible = self._visible_items()
        if not visible:
            return QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        total_w = sum(it.sizeHint().width() for it in visible) + self._h_spacing * max(0, len(visible) - 1)
        max_h = max(it.sizeHint().height() for it in visible)
        return QSize(total_w + margins.left() + margins.right(), max_h + margins.top() + margins.bottom())

    def minimumSize(self):
        margins = self.contentsMargins()
        size = QSize(0, 0)
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _visible_items(self):
        visible = []
        for item in self._items:
            wid = item.widget()
            if wid is not None and wid.isHidden():
                continue
            if wid is None and item.isEmpty():
                continue
            visible.append(item)
        return visible

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        visible = self._visible_items()

        lines: list[list[tuple[object, QSize]]] = []
        cur_line: list[tuple[object, QSize]] = []
        cur_x = effective.x()

        for item in visible:
            hint = item.sizeHint()
            item_w = min(hint.width(), max(1, effective.width())) if effective.width() > 0 else hint.width()
            item_size = QSize(item_w, hint.height())
            needed_x = cur_x + item_w + (self._h_spacing if cur_line else 0)
            if cur_line and needed_x - 1 > effective.right():
                lines.append(cur_line)
                cur_line = [(item, item_size)]
                cur_x = effective.x() + item_w
            else:
                if cur_line:
                    cur_x += self._h_spacing + item_w
                else:
                    cur_x += item_w
                cur_line.append((item, item_size))
        if cur_line:
            lines.append(cur_line)

        y = effective.y()
        for line_idx, line in enumerate(lines):
            line_h = max((sz.height() for _, sz in line), default=0)
            if not test_only:
                x = effective.x()
                for idx, (item, sz) in enumerate(line):
                    if (
                        self._justify_last_on_first_row
                        and line_idx == 0
                        and len(lines) == 1
                        and len(line) > 1
                        and idx == len(line) - 1
                    ):
                        item_x = max(x, effective.right() - sz.width() + 1)
                    else:
                        item_x = x
                    item_y = y + (line_h - sz.height()) // 2
                    item.setGeometry(QRect(QPoint(item_x, item_y), sz))
                    x = item_x + sz.width() + self._h_spacing
            y += line_h + (self._v_spacing if line_idx < len(lines) - 1 else 0)

        return (y - rect.y()) + margins.bottom()


class _LocalizedText:
    def __init__(self, text="", *args, **kwargs):
        self._source_text = text if isinstance(text, str) else ""
        super().__init__(display_ui_text(text), *args, **kwargs)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

    def setText(self, text):
        self._source_text = text
        super().setText(display_ui_text(text))
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            if parent.layout() is not None:
                parent.layout().invalidate()
            parent.updateGeometry()

    def setToolTip(self, text):
        self._source_tooltip = text
        super().setToolTip(i18n.tr(text))

    def retranslate(self):
        super().setText(display_ui_text(self._source_text))
        if hasattr(self, "_source_tooltip"):
            super().setToolTip(i18n.tr(self._source_tooltip))
        self.updateGeometry()


class QLabel(_LocalizedText, QtLabel):
    pass


class QPushButton(_LocalizedText, QtPushButton):
    pass


class QCheckBox(_LocalizedText, QtCheckBox):
    pass


class WrappingFilenameLabel(QtLabel):
    """支持无空格长文件名（如 snake_case_paper.pdf）自动折行的标签，绝不撑破左侧卡片视口。"""

    def __init__(self, text: str = "", parent: QWidget = None):
        super().__init__(text, parent)
        self._raw_text = text
        self._last_wrap_width = -1
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(0)

    def text(self) -> str:
        return self._raw_text

    def setText(self, text: str) -> None:
        self._raw_text = text or ""
        self._last_wrap_width = -1
        self._rewrap_for_width(self.contentsRect().width())

    def hasHeightForWidth(self) -> bool:
        return True

    def _compute_wrapped_lines(self, width: int) -> list[str]:
        if not self._raw_text:
            return [""]
        avail = max(40, width)
        fm = self.fontMetrics()
        # 优先在空格、下划线、连字符、点号后折行，保持单词完整；仅当单个片段超宽时才按字符硬折行
        tokens: list[str] = []
        buf = []
        for ch in self._raw_text:
            buf.append(ch)
            if ch in (" ", "_", "-", "."):
                tokens.append("".join(buf))
                buf.clear()
        if buf:
            tokens.append("".join(buf))

        lines: list[str] = []
        cur = ""
        for tok in tokens:
            cand = cur + tok
            if cur and fm.horizontalAdvance(cand) > avail:
                lines.append(cur)
                cur = tok
            else:
                cur = cand
            if fm.horizontalAdvance(cur) > avail and len(cur) > 1:
                # 单个片段过长时退回字符级折行
                opt = QTextOption()
                opt.setWrapMode(QTextOption.WrapAnywhere)
                layout = QTextLayout(cur, self.font())
                layout.setTextOption(opt)
                layout.beginLayout()
                sub_lines = []
                while True:
                    line = layout.createLine()
                    if not line.isValid():
                        break
                    line.setLineWidth(avail)
                    sub_lines.append(cur[line.textStart():line.textStart() + line.textLength()])
                layout.endLayout()
                if len(sub_lines) > 1:
                    lines.extend(sub_lines[:-1])
                    cur = sub_lines[-1]
        if cur:
            lines.append(cur)
        return lines or [self._raw_text]

    def heightForWidth(self, width: int) -> int:
        margins = self.contentsMargins()
        inner_w = max(40, width - margins.left() - margins.right())
        lines = self._compute_wrapped_lines(inner_w)
        fm = self.fontMetrics()
        line_h = max(fm.lineSpacing(), fm.height())
        return len(lines) * line_h + margins.top() + margins.bottom() + 2

    def sizeHint(self) -> QSize:
        w = self.width() if self.width() > 40 else 260
        return QSize(min(260, w), self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(40, fm.lineSpacing() + 4)

    def _rewrap_for_width(self, width: int) -> None:
        if width <= 20:
            if super().text() != self._raw_text:
                super().setText(self._raw_text)
            return
        if width == self._last_wrap_width:
            return
        self._last_wrap_width = width
        wrapped = "\n".join(self._compute_wrapped_lines(width))
        if super().text() != wrapped:
            super().setText(wrapped)
            self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rewrap_for_width(self.contentsRect().width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._rewrap_for_width(self.contentsRect().width())
