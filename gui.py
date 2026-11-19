# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass
import logging
import json
from collections import deque
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Dict, List, Optional

def _bundled_python_candidates() -> list[Path]:
    root_dir = Path(__file__).resolve().parent
    return (
        [root_dir / 'runtime/python/python.exe', root_dir / 'runtime/python/pythonw.exe']
        if sys.platform == 'win32'
        else [root_dir / 'runtime/python/bin/python3', root_dir / 'runtime/python/bin/python']
    )


if __name__ == '__main__':
    bundled_candidates = _bundled_python_candidates()
    primary_bundled = next((p for p in bundled_candidates if p.is_file()), None)
    if primary_bundled is not None:
        resolved_exe = Path(sys.executable).resolve()
        if resolved_exe not in {p.resolve() for p in bundled_candidates if p.exists()}:
            raise SystemExit(subprocess.call([str(primary_bundled), str(Path(__file__).resolve()), *sys.argv[1:]]))

from core.offline import configure_cache, user_data_dir

if '--check-runtime' in sys.argv:
    import importlib.metadata
    print(f'Python: {sys.executable}')
    print(f'BabelDOC: {importlib.metadata.version("babeldoc")}')
    for pkg in ('PySide6', 'PySide6-Essentials'):
        try:
            print(f'PySide6: {importlib.metadata.version(pkg)} ({pkg})')
            break
        except importlib.metadata.PackageNotFoundError:
            continue
    raise SystemExit(0)

configure_cache()

logger = logging.getLogger("offlinepdf-translate.gui")


def set_system_keep_awake(enabled: bool) -> None:
    """跨平台挂机防休眠保活：翻译期间阻止操作系统睡眠，完成后自动恢复。"""
    from core.jobs import set_system_keep_awake as _keep_awake
    _keep_awake(enabled)

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QFontDatabase,
    QKeySequence,
    QMouseEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel as QtLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

from core.hardware import HardwareProfile, detect_hardware_profile
from core.prompts import COMPLEX_SCRIPT_LANGS, SUPPORTED_LANGUAGES

from core import i18n
from core.ui_widgets import (
    FlowLayout,
    QCheckBox,
    QLabel,
    QPushButton,
    THEME,
    WrappingFilenameLabel,
    _LocalizedText,
    build_light_bauhaus_palette,
    configure_localized_combo,
    display_ui_text,
    draw_bauhaus_empty_emblem,
    make_inline_group,
    refresh_layouts,
    ui_icon_url,
)



def _resolve_app_icon() -> Optional[Path]:
    assets_dir = Path(__file__).resolve().parent / "assets"
    order = ("icon.ico", "icon.png") if sys.platform == "win32" else ("icon.png", "icon.ico")
    for name in order:
        cand = assets_dir / name
        if cand.is_file():
            return cand
    return None


def register_interface_fonts():
    """Use bundled fonts, so UI rendering does not depend on system fonts."""
    if getattr(register_interface_fonts, '_loaded', False):
        return
    font_dir = Path(__file__).resolve().parent / 'assets' / 'fonts'
    for name in ('GoNotoKurrent-Regular.ttf', 'GoNotoKurrent-Bold.ttf', 'SourceHanSansCN-Regular.ttf'):
        if (font_dir / name).is_file():
            QFontDatabase.addApplicationFont(str(font_dir / name))
    register_interface_fonts._loaded = True


class TranslationCancelledException(Exception):
    """用户手动中止翻译异常。"""
    pass


STAGE_NAME_MAP = {
    "Parse PDF and Create Intermediate Representation": "解析文档",
    "Detect Scanned PDF": "扫描检测",
    "Parse Page Layout": "版面解析",
    "Parse Table": "表格解析",
    "Parse Paragraphs": "段落切分",
    "Parse Formulas and Styles": "公式解析",
    "Extract Terms": "提取术语",
    "Translate Paragraphs": "段落翻译",
    "Typesetting": "排版渲染",
    "Add Fonts": "嵌入字体",
    "Generate drawing instructions": "生成图元",
    "Subset font": "精简字体",
    "Save PDF": "保存输出",
}


def format_stage_name(stage: str) -> str:
    """将底层引擎英文处理阶段映射为工整专业的中文描述。"""
    if not stage:
        return "处理中"
    if stage in STAGE_NAME_MAP:
        return STAGE_NAME_MAP[stage]
    for k, v in STAGE_NAME_MAP.items():
        if k.lower() in stage.lower():
            return v
    return stage


@dataclass
class TaskItem:
    """批量翻译任务条目。"""
    id: str
    file_path: Path
    status: str  # "waiting", "processing", "finished", "stopped", "error", "review"
    progress: int = 0  # 文献总体进度 (含版面解析、翻译与排版输出, 0-100)
    translation_progress: int = 0  # 翻译流专用：纯段落翻译百分比 (0-100)
    translation_current: int = 0  # 当前已翻译文本块数量
    translation_total: int = 0  # 本文献待翻译文本块总数
    stage_text: str = "等待中"
    error_msg: str = ""
    dual_pdf: Optional[str] = None
    mono_pdf: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def file_size_str(self) -> str:
        try:
            size_mb = self.file_path.stat().st_size / (1024 * 1024)
            return f"{size_mb:.2f} MB"
        except Exception:
            return ""

    @property
    def duration_seconds(self) -> float:
        if self.start_time is None:
            return 0.0
        if self.end_time is not None:
            return max(0.0, self.end_time - self.start_time)
        return max(0.0, time.monotonic() - self.start_time)

    @property
    def duration_str(self) -> str:
        if self.start_time is None:
            return "待执行"
        secs = int(self.duration_seconds)
        m, s = divmod(secs, 60)
        if m > 0:
            return f"{m}分{s:02d}秒"
        return f"{s}秒"


# ---------------------------------------------------------------------------
# 包豪斯现代主义任务卡片 (纯几何工整、自动换行抗遮挡、精确耗时)
# ---------------------------------------------------------------------------

class TaskCardWidget(QFrame):
    """单篇文献任务卡片，具备自动换行、抗遮挡自适应与包豪斯几何排版。"""

    open_file_signal = Signal(str)
    open_dir_signal = Signal(str)
    remove_signal = Signal(str)
    card_selected_signal = Signal(str)

    def __init__(self, task: TaskItem, parent=None):
        super().__init__(parent)
        self.task = task
        self._is_selected = False
        self.setObjectName("TaskCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.init_ui()
        self.update_state()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.card_selected_signal.emit(self.task.id)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool):
        self._is_selected = selected
        self._update_border_style()

    def _update_border_style(self):
        if self._is_selected:
            self.setStyleSheet(f"""
                #TaskCard {{
                    background-color: {THEME["surface"]};
                    border: 1.5px solid {THEME["ink"]};
                    border-left: 3px solid {THEME["cobalt"]};
                    border-radius: 0px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                #TaskCard {{
                    background-color: {THEME["surface"]};
                    border: 1px solid {THEME["border"]};
                    border-radius: 0px;
                }}
                #TaskCard:hover {{
                    border-color: {THEME["border_strong"]};
                    background-color: {THEME["surface_soft"]};
                }}
            """)

    def init_ui(self):
        self._update_border_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # 头部行：静默几何徽标 + 任意宽度自适应折行文件名 + 隐形移除按钮
        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        header_row.setAlignment(Qt.AlignTop)

        self.badge_icon = QLabel("PDF")
        self.badge_icon.setFixedSize(34, 22)
        self.badge_icon.setAlignment(Qt.AlignCenter)
        self.badge_icon.setStyleSheet(f"""
            background-color: {THEME["surface_pill"]};
            color: {THEME["ink_secondary"]};
            border: 1px solid {THEME["border"]};
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.4px;
            border-radius: 0px;
        """)
        header_row.addWidget(self.badge_icon, alignment=Qt.AlignTop)

        # 长文件名（含无空格下划线命名）自动折行，绝不撑破或裁切右侧控件
        self.name_label = WrappingFilenameLabel(self.task.file_path.name)
        self.name_label.setObjectName("TaskNameLabel")
        self.name_label.setStyleSheet(f"""
            QLabel#TaskNameLabel {{
                font-size: 13.5px;
                font-weight: 700;
                color: {THEME["ink"]};
            }}
        """)
        self.name_label.setToolTip(str(self.task.file_path))
        header_row.addWidget(self.name_label, stretch=1)

        # 移除单项按钮（平时静默隐退，悬停方显红）
        self.btn_remove = QPushButton("×")
        self.btn_remove.setFixedSize(22, 22)
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {THEME["ink_muted"]};
                font-size: 16px;
                font-weight: 600;
                border-radius: 0px;
            }}
            QPushButton:hover {{
                background: {THEME["vermilion_soft"]};
                color: {THEME["vermilion"]};
            }}
        """)
        self.btn_remove.clicked.connect(lambda: self.remove_signal.emit(self.task.id))
        header_row.addWidget(self.btn_remove, alignment=Qt.AlignTop)

        layout.addLayout(header_row)

        # 次级行：克制流式信息块（耗时 + 状态 + 文件大小）
        meta_widget = QWidget()
        meta_widget.setObjectName("MetaWidget")
        meta_widget.setStyleSheet("#MetaWidget { background: transparent; }")
        meta_row = FlowLayout(meta_widget, margin=0, h_spacing=6, v_spacing=4)

        self.duration_pill = QLabel("待执行")
        self.duration_pill.setObjectName("DurationPill")
        self.duration_pill.setFixedHeight(22)
        self.duration_pill.setStyleSheet(f"""
            QLabel#DurationPill {{
                background-color: {THEME["surface_pill"]};
                color: {THEME["ink_secondary"]};
                font-size: 11px;
                font-weight: 600;
                padding: 2px 8px;
                border-radius: 0px;
            }}
        """)
        meta_row.addWidget(self.duration_pill)

        self.status_pill = QLabel("排队中")
        self.status_pill.setObjectName("StatusPill")
        self.status_pill.setFixedHeight(22)
        self.status_pill.setStyleSheet(f"""
            QLabel#StatusPill {{
                background-color: {THEME["surface_pill"]};
                color: {THEME["ink_secondary"]};
                border: 1px solid {THEME["border"]};
                font-size: 11px;
                font-weight: 700;
                padding: 2px 8px;
                border-radius: 0px;
            }}
        """)
        meta_row.addWidget(self.status_pill)

        self.sub_label = QLabel(f"{self.task.file_size_str}")
        self.sub_label.setStyleSheet(f"font-size: 11px; font-weight: 500; color: {THEME['ink_muted']}; margin-left: 2px;")
        meta_row.addWidget(self.sub_label)

        layout.addWidget(meta_widget)

        # 快捷交付动作按钮组（完成时展开，主次清晰）
        self.actions_widget = QWidget()
        self.actions_widget.setObjectName("CardActionsWidget")
        self.actions_widget.setStyleSheet("#CardActionsWidget { background: transparent; }")
        actions_layout = FlowLayout(self.actions_widget, margin=0, h_spacing=6, v_spacing=6)
        actions_layout.setContentsMargins(0, 2, 0, 0)

        btn_primary_card_style = f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                border: 1px solid {THEME["ink"]};
                border-radius: 0px;
                padding: 4px 11px;
                font-size: 11px;
                font-weight: 700;
                color: {THEME["surface"]};
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
                border-color: {THEME["cobalt"]};
            }}
        """
        btn_card_style = f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border_strong"]};
                border-radius: 0px;
                padding: 4px 11px;
                font-size: 11px;
                font-weight: 700;
                color: {THEME["ink"]};
            }}
            QPushButton:hover {{
                background-color: {THEME["surface_pill"]};
                border-color: {THEME["ink"]};
            }}
        """

        self.btn_dual = QPushButton("ui.bilingual")
        self.btn_dual.setCursor(Qt.PointingHandCursor)
        self.btn_dual.setStyleSheet(btn_primary_card_style)
        self.btn_dual.clicked.connect(lambda: self.open_file_signal.emit(self.task.dual_pdf))

        self.btn_mono = QPushButton("ui.translated")
        self.btn_mono.setCursor(Qt.PointingHandCursor)
        self.btn_mono.setStyleSheet(btn_card_style)
        self.btn_mono.clicked.connect(lambda: self.open_file_signal.emit(self.task.mono_pdf))

        self.btn_dir = QPushButton("ui.open_folder")
        self.btn_dir.setCursor(Qt.PointingHandCursor)
        self.btn_dir.setStyleSheet(btn_card_style)
        self.btn_dir.clicked.connect(lambda: self.open_dir_signal.emit(str(Path(self.task.mono_pdf).parent if self.task.mono_pdf else self.task.file_path.parent)))

        actions_layout.addWidget(self.btn_dual)
        actions_layout.addWidget(self.btn_mono)
        actions_layout.addWidget(self.btn_dir)
        self.actions_widget.setVisible(False)
        layout.addWidget(self.actions_widget)
        self.error_hint = QtLabel()
        self.error_hint.setWordWrap(True)
        self.error_hint.setStyleSheet(f'color: {THEME["vermilion"]}; font-size: 12px; font-weight: 500;')
        self.error_hint.hide()
        layout.addWidget(self.error_hint)

        # 静默优雅的 4px 瑞士细条进度轨（钴蓝进度，无粗黑框）
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {THEME["surface_muted"]};
                border: none;
                border-radius: 0px;
            }}
            QProgressBar::chunk {{
                background-color: {THEME["cobalt"]};
                border-radius: 0px;
            }}
        """)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

    def update_duration_display(self):
        """定时器动态驱动耗时显示。"""
        st = self.task.status
        dur_str = self.task.duration_str
        if st == "processing":
            self.duration_pill.setText(f"⏱ {dur_str}")
            self.duration_pill.setStyleSheet(f"""
                QLabel#DurationPill {{
                    background-color: {THEME["cobalt_soft"]};
                    color: {THEME["cobalt"]};
                    font-size: 11px;
                    font-weight: 700;
                    padding: 2px 8px;
                    border-radius: 0px;
                }}
            """)
        elif st == "finished":
            self.duration_pill.setText(f"✓ 耗时 {dur_str}")
            self.duration_pill.setStyleSheet(f"""
                QLabel#DurationPill {{
                    background-color: {THEME["surface_pill"]};
                    color: {THEME["ink_secondary"]};
                    font-size: 11px;
                    font-weight: 600;
                    padding: 2px 8px;
                    border-radius: 0px;
                }}
            """)
        elif st == "stopped":
            self.duration_pill.setText(f"已停止 · {dur_str}")
            self.duration_pill.setStyleSheet(f"""
                QLabel#DurationPill {{
                    background-color: {THEME["ochre_soft"]};
                    color: {THEME["ochre"]};
                    font-size: 11px;
                    font-weight: 700;
                    padding: 2px 8px;
                    border-radius: 0px;
                }}
            """)
        elif st in ("error", "review"):
            self.duration_pill.setText(f"中断 · {dur_str}")
            self.duration_pill.setStyleSheet(f"""
                QLabel#DurationPill {{
                    background-color: {THEME["vermilion_soft"]};
                    color: {THEME["vermilion"]};
                    font-size: 11px;
                    font-weight: 700;
                    padding: 2px 8px;
                    border-radius: 0px;
                }}
            """)
        else:
            self.duration_pill.setText("待执行")
            self.duration_pill.setStyleSheet(f"""
                QLabel#DurationPill {{
                    background-color: {THEME["surface_pill"]};
                    color: {THEME["ink_muted"]};
                    font-size: 11px;
                    font-weight: 600;
                    padding: 2px 8px;
                    border-radius: 0px;
                }}
            """)

    def update_state(self):
        st = self.task.status
        self.error_hint.setVisible(st in ('error', 'review'))
        if st in ('error', 'review'):
            self.error_hint.setText(i18n.message(i18n.error_key(self.task.error_msg, st)))
        self.progress_bar.setValue(self.task.progress)
        self.update_duration_display()

        if st == "waiting":
            self.status_pill.setText("排队中")
            self.status_pill.setToolTip("")
            self.status_pill.setStyleSheet(f"QLabel#StatusPill {{ background-color: {THEME['surface_pill']}; color: {THEME['ink_secondary']}; border: 1px solid {THEME['border']}; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 0px; }}")
            self.status_pill.setVisible(True)
            self.actions_widget.setVisible(False)
            self.btn_remove.setVisible(True)
            self.progress_bar.setVisible(False)
        elif st == "processing":
            self.status_pill.setText(f"{self.task.stage_text} {self.task.progress}%")
            self.status_pill.setToolTip("")
            self.status_pill.setStyleSheet(f"QLabel#StatusPill {{ background-color: {THEME['cobalt']}; color: #ffffff; border: 1px solid {THEME['cobalt']}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 0px; }}")
            self.status_pill.setVisible(True)
            self.actions_widget.setVisible(False)
            self.btn_remove.setVisible(False)
            self.progress_bar.setVisible(True)
        elif st == "finished":
            self.status_pill.setText("完成")
            self.status_pill.setToolTip("")
            self.status_pill.setStyleSheet(f"QLabel#StatusPill {{ background-color: {THEME['sage_soft']}; color: {THEME['sage']}; border: 1px solid {THEME['sage_border']}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 0px; }}")
            self.status_pill.setVisible(True)
            self.actions_widget.setVisible(True)
            self.btn_remove.setVisible(True)
            self.progress_bar.setVisible(False)
        elif st == "stopped":
            self.status_pill.setText("已停止")
            self.status_pill.setToolTip("")
            self.status_pill.setStyleSheet(f"QLabel#StatusPill {{ background-color: {THEME['ochre_soft']}; color: {THEME['ochre']}; border: 1px solid {THEME['ochre_border']}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 0px; }}")
            self.status_pill.setVisible(True)
            self.actions_widget.setVisible(False)
            self.btn_remove.setVisible(True)
            self.progress_bar.setVisible(False)
        elif st in ("error", "review"):
            self.status_pill.setText("需复核" if st == "review" else "处理失败")
            err_lines = [ln.strip() for ln in (self.task.error_msg or "").splitlines() if ln.strip()]
            err_summary = err_lines[-1] if err_lines else self.task.error_msg
            self.status_pill.setToolTip("错误详情: " + err_summary)
            self.status_pill.setStyleSheet(f"QLabel#StatusPill {{ background-color: {THEME['vermilion_soft']}; color: {THEME['vermilion']}; border: 1px solid {THEME['vermilion_border']}; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 0px; }}")
            self.status_pill.setVisible(True)
            self.actions_widget.setVisible(False)
            self.btn_remove.setVisible(True)
            self.progress_bar.setVisible(False)


# ---------------------------------------------------------------------------
# 包豪斯纯几何空状态视图 (谦虚留白、零视觉噪音、自信聚焦)
# ---------------------------------------------------------------------------

class EmptyHeroView(QFrame):
    """文献任务空态展示区。采用克制瑞士纸面网格与极简几何引导。"""

    files_dropped = Signal(list)
    select_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self._is_drag_over = False

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(36, 54, 36, 54)
        layout.setSpacing(14)

        self.icon_box = QtLabel()
        self.icon_box.setAlignment(Qt.AlignCenter)
        self.icon_box.setPixmap(draw_bauhaus_empty_emblem(44, active=False))
        self.icon_box.setStyleSheet("background: transparent;")
        layout.addWidget(self.icon_box, alignment=Qt.AlignCenter)

        self.title_label = QLabel("ui.add_pdfs")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.title_label.setStyleSheet(f"font-size: 17px; font-weight: 800; color: {THEME['ink']}; background: transparent;")
        layout.addWidget(self.title_label)

        self.sub_label = QLabel("ui.drop_hint")
        self.sub_label.setAlignment(Qt.AlignCenter)
        self.sub_label.setWordWrap(True)
        self.sub_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.sub_label.setStyleSheet(f"font-size: 12.5px; color: {THEME['ink_secondary']}; background: transparent;")
        layout.addWidget(self.sub_label)

        self.btn_select = QPushButton("ui.select")
        self.btn_select.setMinimumSize(156, 40)
        self.btn_select.setCursor(Qt.PointingHandCursor)
        self.btn_select.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: none;
                border-radius: 0px;
                padding: 0 24px;
                font-size: 13px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
            }}
        """)
        self.btn_select.clicked.connect(self.select_clicked.emit)
        layout.addWidget(self.btn_select, alignment=Qt.AlignCenter)

        self._update_style()

    def _update_style(self):
        if self._is_drag_over:
            border = f"1.5px dashed {THEME['cobalt']}"
            bg = THEME["cobalt_soft"]
        else:
            border = f"1.5px dashed {THEME['border_strong']}"
            bg = THEME["surface_soft"]

        self.icon_box.setPixmap(draw_bauhaus_empty_emblem(44, active=self._is_drag_over))
        self.setStyleSheet(f"""
            EmptyHeroView {{
                border: {border};
                border-radius: 0px;
                background-color: {bg};
            }}
            EmptyHeroView:hover {{
                border-color: {THEME["ink"]};
                background-color: {THEME["surface"]};
            }}
        """)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.select_clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                self._is_drag_over = True
                self._update_style()
                event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._is_drag_over = False
        self._update_style()

    def dropEvent(self, event: QDropEvent):
        self._is_drag_over = False
        self._update_style()
        files = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.toLocalFile().lower().endswith(".pdf")
        ]
        if files:
            self.files_dropped.emit(files)
            event.acceptProposedAction()


# ---------------------------------------------------------------------------
# 右侧：实时翻译流与成果核查面板 (最新段落置顶呈现、失败与重试全量展示)
# ---------------------------------------------------------------------------

class TranslationBlockCard(QFrame):
    """单个段落/文本块的实时双语对照呈现卡片（瑞士网格极简排版，聚焦译文可读性）。"""

    def __init__(self, index: int, source_text: str, target_text: str, status: str = "success", reason: str = "", attempt: int = 1, parent=None):
        super().__init__(parent)
        self.setObjectName("BlockCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.init_ui(index, source_text, target_text, status, reason, attempt)

    def init_ui(self, index: int, source_text: str, target_text: str, status: str, reason: str, attempt: int):
        is_error = status == "retry_pending" or status == "failed"
        border_color = THEME["vermilion_border"] if is_error else THEME["border"]

        self.setStyleSheet(f"""
            #BlockCard {{
                background-color: {THEME["surface"]};
                border: 1px solid {border_color};
                border-radius: 0px;
            }}
            #BlockCard:hover {{
                border-color: {THEME["border_strong"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # 头部：序号与状态徽标
        header = QHBoxLayout()
        header.setSpacing(8)

        idx_label = QLabel(f"#{index:02d}")
        idx_label.setStyleSheet(f"""
            font-size: 11px;
            font-weight: 800;
            color: {THEME["ink_secondary"]};
            background-color: {THEME["surface_pill"]};
            padding: 2px 7px;
        """)
        header.addWidget(idx_label)

        type_label = QLabel("正文段落" if len(source_text) > 40 else "标题 / 图注")
        type_label.setStyleSheet(f"font-size: 11px; color: {THEME['ink_muted']}; font-weight: 600;")
        header.addWidget(type_label)

        header.addStretch(1)

        # 柔和克制的状态徽标
        if status == "success":
            if "复用" in reason or "缓存" in reason:
                status_text = "复用已有译文"
                status_style = f"background-color: {THEME['cobalt_soft']}; color: {THEME['cobalt']}; border: 1px solid {THEME['cobalt_border']};"
            else:
                status_text = "✓ 已完成"
                status_style = f"background-color: {THEME['sage_soft']}; color: {THEME['sage']}; border: 1px solid {THEME['sage_border']};"
        elif status == "retry_success":
            status_text = "✓ 重新翻译完成"
            status_style = f"background-color: {THEME['surface_pill']}; color: {THEME['ink']}; border: 1px solid {THEME['border_strong']};"
        elif status == "retry_pending":
            status_text = "重新翻译中..."
            status_style = f"background-color: {THEME['vermilion_soft']}; color: {THEME['vermilion']}; border: 1px solid {THEME['vermilion_border']};"
        elif status == "fallback_repaired":
            status_text = "已保留格式"
            status_style = f"background-color: {THEME['ochre_soft']}; color: {THEME['ochre']}; border: 1px solid {THEME['ochre_border']};"
        else:
            status_text = "保留原文"
            status_style = f"background-color: {THEME['surface_pill']}; color: {THEME['ink_secondary']}; border: 1px solid {THEME['border']};"

        st_lbl = QLabel(status_text)
        st_lbl.setStyleSheet(f"""
            font-size: 10.5px;
            font-weight: 700;
            {status_style}
            padding: 2px 8px;
            border-radius: 0px;
        """)
        header.addWidget(st_lbl)

        layout.addLayout(header)

        if reason and (is_error or "修复" in reason or "重试" in reason):
            logger.info("Translation diagnostic: %s", reason)

        # 原文行（静默衬托）
        src_tag = QLabel("ui.original")
        src_tag.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {THEME['ink_muted']}; letter-spacing: 0.6px;")
        layout.addWidget(src_tag)

        src_lbl = QtLabel(source_text)
        src_lbl.setWordWrap(True)
        src_lbl.setMinimumWidth(0)
        src_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        src_lbl.setStyleSheet(f"""
            font-size: 12px;
            color: {THEME["ink_secondary"]};
            background-color: {THEME["surface_soft"]};
            padding: 8px 10px;
            border-left: 2px solid {THEME["border_strong"]};
        """)
        src_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(src_lbl)

        # 译文行（视觉绝对焦点）
        tgt_tag = QLabel("ui.translation")
        tgt_tag.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {THEME['cobalt']}; letter-spacing: 0.6px; margin-top: 2px;")
        layout.addWidget(tgt_tag)

        tgt_lbl = QtLabel(target_text)
        tgt_lbl.setWordWrap(True)
        tgt_lbl.setMinimumWidth(0)
        tgt_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        tgt_lbl.setStyleSheet(f"""
            font-size: 13.5px;
            color: {THEME["ink"]};
            font-weight: 500;
            background-color: transparent;
            padding: 2px 0px;
        """)
        tgt_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(tgt_lbl)


class LiveInspectionPanel(QFrame):
    """右侧：实时翻译流与成果核查面板（克制瑞士网格视窗）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LiveInspectionPanel")
        self.block_count = 0
        self._total_blocks = 0
        self.is_auto_scroll = True
        self.unseen_count = 0
        self._loading_batch = False
        self._pending_blocks = deque(maxlen=100)
        self.init_ui()

    def init_ui(self):
        self.setStyleSheet(f"""
            #LiveInspectionPanel {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部栏：标题 + 活跃任务名 + 块数指标
        header_top = QHBoxLayout()
        header_top.setSpacing(10)

        title = QLabel("ui.live")
        title.setStyleSheet(f"font-size: 15px; font-weight: 800; color: {THEME['ink']};")
        header_top.addWidget(title)

        self.task_indicator = QLabel("ui.wait_start")
        self.task_indicator.setWordWrap(True)
        self.task_indicator.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.task_indicator.setStyleSheet(f"font-size: 12px; font-weight: 500; color: {THEME['ink_secondary']};")
        header_top.addWidget(self.task_indicator, stretch=1)

        self.count_badge = QLabel("0 个文本块")
        self.count_badge.setStyleSheet(f"""
            background-color: {THEME["surface_pill"]};
            color: {THEME["ink_secondary"]};
            border: 1px solid {THEME["border"]};
            font-size: 11px;
            font-weight: 700;
            padding: 3px 9px;
            border-radius: 0px;
        """)
        header_top.addWidget(self.count_badge)

        layout.addLayout(header_top)

        actions_row = FlowLayout(margin=0, h_spacing=8, v_spacing=4)

        self.btn_scroll_latest = QPushButton("ui.latest")
        self.btn_scroll_latest.setCursor(Qt.PointingHandCursor)
        self.btn_scroll_latest.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: 1px solid {THEME["ink"]};
                font-size: 11px;
                font-weight: 700;
                padding: 3px 10px;
                border-radius: 0px;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
                border-color: {THEME["cobalt"]};
                color: #ffffff;
            }}
        """)
        self.btn_scroll_latest.setToolTip("已暂停自动置顶，点击立即回到最新实时翻译段落")
        self.btn_scroll_latest.setVisible(False)
        self.btn_scroll_latest.clicked.connect(self.scroll_to_latest)
        actions_row.addWidget(self.btn_scroll_latest)

        self.btn_back_to_live = QPushButton("ui.back_live")
        self.btn_back_to_live.setCursor(Qt.PointingHandCursor)
        self.btn_back_to_live.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["cobalt_soft"]};
                color: {THEME["cobalt"]};
                border: 1px solid {THEME["cobalt_border"]};
                font-size: 11px;
                font-weight: 700;
                padding: 3px 10px;
                border-radius: 0px;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
                color: #ffffff;
            }}
        """)
        self.btn_back_to_live.setToolTip("后台正在翻译另一篇文献，点击切回正在进行的实时翻译流")
        self.btn_back_to_live.setVisible(False)
        actions_row.addWidget(self.btn_back_to_live)

        layout.addLayout(actions_row)
        live_limit_hint = QLabel("仅显示最近 100 条")
        live_limit_hint.setStyleSheet(f"color: {THEME['ink_muted']}; font-size: 11px;")
        layout.addWidget(live_limit_hint)

        # 静默 4px 钴蓝进度轨
        self.progress_line = QProgressBar()
        self.progress_line.setFixedHeight(4)
        self.progress_line.setTextVisible(False)
        self.progress_line.setValue(0)
        self.progress_line.setStyleSheet(f"""
            QProgressBar {{
                background-color: {THEME["surface_muted"]};
                border: none;
                border-radius: 0px;
            }}
            QProgressBar::chunk {{
                background-color: {THEME["cobalt"]};
                border-radius: 0px;
            }}
        """)
        layout.addWidget(self.progress_line)

        # 核心堆叠容器：Page 0 为空态占位，Page 1 为翻译流滚动区
        self.stack = QStackedWidget()

        # Page 0: 初始等待态几何占位视图
        self.placeholder_widget = QWidget()
        placeholder_vbox = QVBoxLayout(self.placeholder_widget)
        placeholder_vbox.setAlignment(Qt.AlignCenter)
        placeholder_vbox.setContentsMargins(24, 60, 24, 60)
        placeholder_vbox.setSpacing(12)

        art_label = QtLabel()
        art_label.setAlignment(Qt.AlignCenter)
        art_label.setPixmap(draw_bauhaus_empty_emblem(38, active=False))
        art_label.setStyleSheet("background: transparent;")
        placeholder_vbox.addWidget(art_label, alignment=Qt.AlignCenter)

        desc_label = QLabel("ui.live_ready")
        desc_label.setAlignment(Qt.AlignCenter)
        desc_label.setWordWrap(True)
        desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        desc_label.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {THEME['ink']};")
        placeholder_vbox.addWidget(desc_label)

        sub_desc = QLabel("ui.live_hint")
        sub_desc.setAlignment(Qt.AlignCenter)
        sub_desc.setWordWrap(True)
        sub_desc.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sub_desc.setStyleSheet(f"font-size: 12px; color: {THEME['ink_secondary']};")
        placeholder_vbox.addWidget(sub_desc)

        self.stack.addWidget(self.placeholder_widget)

        # Page 1: 主滚动区域 (展示流式翻译卡片，最新段落默认置顶呈现)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; }")

        vbar = self.scroll_area.verticalScrollBar()
        vbar.valueChanged.connect(self._on_scroll_value_changed)
        vbar.sliderReleased.connect(self._on_slider_released)

        self.blocks_container = QWidget()
        self.blocks_container.setStyleSheet("background: transparent;")
        self.blocks_layout = QVBoxLayout(self.blocks_container)
        self.blocks_layout.setContentsMargins(0, 0, 6, 0)
        self.blocks_layout.setSpacing(10)
        self.blocks_layout.addStretch(1)

        self.scroll_area.setWidget(self.blocks_container)
        self.stack.addWidget(self.scroll_area)

        layout.addWidget(self.stack, stretch=1)

    def _on_scroll_value_changed(self, val: int):
        if self._loading_batch:
            return
        if self.scroll_area.verticalScrollBar().isSliderDown():
            self.is_auto_scroll = False
            return
        # 只要用户滚动回到最顶端（<= 20px 容差），自动恢复跟随最新
        if val <= 20:
            self.scroll_to_latest()
        else:
            self.is_auto_scroll = False

    def _on_slider_released(self):
        val = self.scroll_area.verticalScrollBar().value()
        if val <= 20:
            self.scroll_to_latest()
        else:
            self.is_auto_scroll = False

    def scroll_to_latest(self):
        """Resume live updates, applying buffered blocks once before scrolling."""
        self.is_auto_scroll = True
        self.unseen_count = 0
        self.btn_scroll_latest.setVisible(False)
        self._loading_batch = True
        try:
            while self._pending_blocks:
                self._insert_block(*self._pending_blocks.popleft())
            self.scroll_area.verticalScrollBar().setValue(0)
        finally:
            self._loading_batch = False

    def set_task_info(self, filename: str, progress: int = 0, status: str = "processing", total_blocks: int = 0):
        self.progress_line.setValue(progress)
        if total_blocks > 0:
            self._total_blocks = total_blocks
            self.count_badge.setText(f"{self.block_count}/{total_blocks} 个文本块")
        elif self.block_count > 0:
            self.count_badge.setText(f"{self.block_count} 个文本块")
        else:
            self.count_badge.setText("0 个文本块")

        if status == "processing":
            self.task_indicator.setText(f"处理中: {filename} ({progress}%)")
        elif status == "finished":
            self.task_indicator.setText(f"已完成: {filename}")
        elif status == "stopped":
            self.task_indicator.setText(f"已停止: {filename}")
        elif status in ("error", "review"):
            self.task_indicator.setText(f"{'需复核' if status == 'review' else '处理失败'}: {filename}")
        else:
            self.task_indicator.setText(f"等待中: {filename}")

    def set_active_task(self, filename: str, progress: int = 0, total_blocks: int = 0):
        self.set_task_info(filename, progress, status="processing", total_blocks=total_blocks)

    def add_translation_block(self, index: int, source_text: str, target_text: str, status: str = "success", reason: str = "", attempt: int = 1):
        self.stack.setCurrentIndex(1)
        self.block_count += 1
        if self._total_blocks > 0:
            self.count_badge.setText(f"{self.block_count}/{self._total_blocks} 个文本块")
        else:
            self.count_badge.setText(f"{self.block_count} 个文本块")

        block = (index or self.block_count, source_text, target_text, status, reason, attempt)
        if not self.is_auto_scroll and not self._loading_batch:
            # Leave the existing layout untouched while the user reads or drags.
            self._pending_blocks.append(block)
            self.unseen_count += 1
            self.btn_scroll_latest.setText(f"↑ 回到最新 (+{self.unseen_count})")
            self.btn_scroll_latest.setVisible(True)
            return
        self._insert_block(*block)
        if self.is_auto_scroll and not self._loading_batch:
            self.scroll_area.verticalScrollBar().setValue(0)

    def _insert_block(self, index, source_text, target_text, status, reason, attempt):
        card = TranslationBlockCard(index, source_text, target_text, status, reason, attempt, self.blocks_container)
        self.blocks_layout.insertWidget(0, card)
        while self.blocks_layout.count() > 101:  # 100 cards plus stretch
            item = self.blocks_layout.takeAt(self.blocks_layout.count() - 2)
            if item and item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        card.updateGeometry()
        self.blocks_layout.activate()

    def reset_view(self, task_name: str = "", status: str = "processing"):
        # 清除所有现有卡片
        for i in reversed(range(self.blocks_layout.count() - 1)):
            item = self.blocks_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                self.blocks_layout.removeWidget(w)
                w.deleteLater()

        self.block_count = 0
        self._total_blocks = 0
        self.count_badge.setText("0 个文本块")
        self.progress_line.setValue(0)
        self.is_auto_scroll = True
        self.unseen_count = 0
        self._pending_blocks.clear()
        self.btn_scroll_latest.setVisible(False)
        if self.scroll_area:
            self.scroll_area.verticalScrollBar().setValue(0)

        if task_name:
            self.set_task_info(task_name, 0, status=status)
            self.stack.setCurrentIndex(1)
        else:
            self.task_indicator.setText("ui.wait_start")
            self.stack.setCurrentIndex(0)


# ---------------------------------------------------------------------------
# 后台批量执行工作线程
# ---------------------------------------------------------------------------

class BatchTranslationWorker(QThread):
    """后台批量排版翻译调度引擎 (支持实时段落事件上报、失败与重试全量透传)。"""

    task_updated = Signal(str)  # task_id
    batch_progress = Signal(int, int, int)  # current_idx, total, overall_percent
    batch_finished = Signal()
    batch_stopped = Signal()
    fatal_error = Signal(str)
    block_translated = Signal(str, str, str, int, str, str, int)  # task_id, source, target, index, status, reason, attempt

    def __init__(
        self,
        tasks: List[TaskItem],
        lang_in: str,
        lang_out: str,
        threads: Optional[int],
        parallel: Optional[int],
        context_size: Optional[int],
        n_gpu_layers: Optional[int],
        parent=None,
        tasks_lock: Optional[threading.Lock] = None,
    ):
        super().__init__(parent)
        self.tasks = tasks
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.threads = threads
        self.parallel = parallel
        self.context_size = context_size
        self.n_gpu_layers = n_gpu_layers

        self._is_stop_requested = False
        self._tasks_lock = tasks_lock or threading.Lock()
        self.current_runtime = None
        self.last_heartbeat = time.monotonic()

    def request_stop(self):
        self._is_stop_requested = True

    def _batch_position(self, task: TaskItem):
        with self._tasks_lock:
            total = max(1, len(self.tasks))
            try:
                pos = self.tasks.index(task)
            except ValueError:
                pos = 0
            return pos, total

    def run(self):
        from core.jobs import run_document_job, JobCancelled
        from core.delivery import publish_pair
        root = Path(__file__).resolve().parent
        runtime = None
        set_system_keep_awake(True)
        try:
            from core.llama_runtime import LlamaServerRuntime
            from core.offline import resolve_translation_model
            runtime = LlamaServerRuntime(
                model_path=resolve_translation_model(root),
                threads=self.threads, context_size=self.context_size,
                parallel=self.parallel, n_gpu_layers=self.n_gpu_layers)
            self.current_runtime = runtime

            processed_ids = set()
            while True:
                if self._is_stop_requested:
                    break
                with self._tasks_lock:
                    task = next((t for t in self.tasks if t.id not in processed_ids), None)
                    if task is None:
                        break
                    processed_ids.add(task.id)
                    if task.status not in ("waiting", "stopped", "error", "review"):
                        continue

                    start_stage = "准备中"
                    task.status, task.stage_text, task.progress = "processing", start_stage, 0
                    task.translation_progress = 0
                    task.translation_current = 0
                    task.translation_total = 0
                    task.start_time = time.monotonic()
                    task.end_time = None
                    task.error_msg = ""
                    task.dual_pdf = task.mono_pdf = None
                self.task_updated.emit(task.id)

                preflight_ok = False
                try:
                    from core.engine import preflight_check_pdf
                    if task.file_path.is_file() or not hasattr(run_document_job, '__module__') or run_document_job.__module__ == 'core.jobs':
                        preflight_check_pdf(task.file_path)
                    preflight_ok = True
                    if runtime:
                        runtime.start(cancelled=lambda: self._is_stop_requested)
                    with tempfile.TemporaryDirectory(prefix="offlinepdf_stage_") as td:
                        def on_progress(event, task=task):
                            self.last_heartbeat = time.monotonic()
                            if event.get('type') == 'block_translated':
                                task.translation_current += 1
                                if task.translation_total > 0:
                                    calc_prog = int(round(task.translation_current * 100 / task.translation_total))
                                    task.translation_progress = min(99, max(task.translation_progress, calc_prog))
                                    # 同步动态推进总体进度（解析占 10%，翻译占 85%）
                                    calc_overall = 10 + int(round(0.85 * task.translation_progress))
                                    task.progress = min(95, max(task.progress, calc_overall))
                                    self.task_updated.emit(task.id)
                                self.block_translated.emit(
                                    task.id,
                                    event.get('source', ''),
                                    event.get('target', ''),
                                    int(event.get('index', 0)),
                                    event.get('status', 'success'),
                                    event.get('reason', ''),
                                    int(event.get('attempt', 1)),
                                )
                            elif event.get('type', '').startswith('progress_'):
                                raw_stage = event.get('stage', '处理中')
                                task.stage_text = format_stage_name(raw_stage)

                                stage_lower = raw_stage.lower()
                                is_trans_stage = ("translate" in stage_lower or "翻译" in stage_lower)
                                is_post_stage = any(k in stage_lower for k in ("typeset", "font", "draw", "subset", "save", "排版", "输出"))

                                if is_trans_stage:
                                    stg_tot = int(event.get('stage_total', 0) or 0)
                                    stg_cur = int(event.get('stage_current', 0) or 0)
                                    if stg_tot > 0:
                                        task.translation_total = stg_tot
                                    if stg_cur > 0:
                                        task.translation_current = max(task.translation_current, stg_cur)

                                    if event.get('type') == 'progress_end':
                                        task.translation_progress = 100
                                    elif event.get('type') == 'progress_start':
                                        task.translation_progress = 0
                                    else:
                                        stg_prog = event.get('stage_progress', 0)
                                        prog = int(round(stg_prog))
                                        if task.translation_total > 0 and task.translation_current < task.translation_total:
                                            task.translation_progress = min(99, max(0, prog))
                                        elif task.translation_total > 0 and task.translation_current >= task.translation_total:
                                            task.translation_progress = 100
                                        else:
                                            task.translation_progress = min(99, max(0, prog))

                                    # 翻译阶段：前序解析占 10%，翻译占 85%（总体推进到 10% ~ 95%）
                                    calc_overall = 10 + int(round(0.85 * task.translation_progress))
                                    task.progress = min(95, max(10, calc_overall))
                                elif is_post_stage:
                                    task.translation_progress = 100
                                    # 翻译后排版输出阶段：95% ~ 99%
                                    raw_overall = int(event.get('overall_progress', 95))
                                    task.progress = min(99, max(95, raw_overall))
                                else:
                                    # 翻译前解析阶段（版面分析、切分等）：耗时仅数秒，严格占比 10% (0% ~ 10%)
                                    if task.translation_progress < 100:
                                        task.translation_progress = 0
                                    raw_overall = int(event.get('overall_progress', 0))
                                    if raw_overall <= 10:
                                        task.progress = max(0, raw_overall)
                                    else:
                                        # 若底层库未重设权重，按比例归一压缩到 0 ~ 10%
                                        task.progress = min(10, max(0, int(round(raw_overall * 10 / 38.5))))

                                self.task_updated.emit(task.id)
                            pos, total = self._batch_position(task)
                            overall = int((pos + task.progress / 100) / total * 100)
                            self.batch_progress.emit(pos + 1, total, overall)

                        summary = run_document_job(
                            runtime, task.file_path, Path(td), self.lang_in, self.lang_out,
                            progress_callback=on_progress,
                            cancelled=lambda: self._is_stop_requested)
                        if self._is_stop_requested:
                            raise JobCancelled('用户已停止')
                        paths = publish_pair(summary['result'], task.file_path)

                    task.dual_pdf, task.mono_pdf = paths.get('dual_pdf_path'), paths.get('mono_pdf_path')
                    task.status, task.stage_text, task.progress = "finished", "已完成", 100
                    task.translation_progress = 100
                    task.end_time = time.monotonic()
                except Exception as e:
                    task.end_time = time.monotonic()
                    if self._is_stop_requested or isinstance(e, JobCancelled):
                        task.status, task.stage_text = "stopped", "已停止"
                        self.task_updated.emit(task.id)
                        break
                    task.status = 'review' if '需复核' in str(e) else 'error'
                    task.stage_text = '需复核' if task.status == 'review' else '失败'
                    task.error_msg = str(e)
                    logger.exception('文献处理失败: %s', task.file_path)
                    if runtime and preflight_ok:
                        runtime.stop()

                self.task_updated.emit(task.id)
                pos, total = self._batch_position(task)
                self.batch_progress.emit(pos + 1, total, int((pos + 1) / total * 100))
        except Exception as e:
            if not self._is_stop_requested:
                self.fatal_error.emit(str(e))
                return
        finally:
            if runtime:
                runtime.stop()
            self.current_runtime = None
            set_system_keep_awake(False)

        if self._is_stop_requested:
            self.batch_stopped.emit()
        else:
            self.batch_finished.emit()


# ---------------------------------------------------------------------------
# 主工作台窗口：包豪斯工业级高反差双栏工作台
# ---------------------------------------------------------------------------

class MainWindow(QWidget):
    """OfflinePDF Translate 双栏专业学术翻译工作台。"""

    def __init__(self):
        super().__init__()
        register_interface_fonts()
        self.tasks: List[TaskItem] = []
        self._tasks_lock = threading.Lock()
        self.card_widgets: Dict[str, TaskCardWidget] = {}
        self.task_blocks: Dict[str, List[dict]] = {}
        self.current_inspecting_task_id: Optional[str] = None
        self.active_task_id: Optional[str] = None
        self.user_pinned_task_id: Optional[str] = None
        self.worker: Optional[BatchTranslationWorker] = None
        self._close_when_stopped = False
        self._restoring_queue = False
        self._saved_statuses = {}
        self.auto_profile: HardwareProfile = detect_hardware_profile()

        # 耗时动态刷新定时器 (1秒精确定时)
        self.duration_timer = QTimer(self)
        self.duration_timer.setInterval(1000)
        self.duration_timer.timeout.connect(self._on_duration_tick)
        self.duration_timer.start()

        self.setAcceptDrops(True)
        i18n.set_language(i18n.saved_language())
        self.init_ui()
        self._restore_queue()

    def _on_duration_tick(self):
        """定时刷新所有执行中任务的耗时显示。"""
        for t in self.tasks:
            if t.status == "processing":
                card = self.card_widgets.get(t.id)
                if card:
                    card.update_duration_display()

    def init_ui(self):
        self.setWindowTitle(i18n.tr("OfflinePDF Translate — 本地离线PDF文献翻译"))
        icon_path = _resolve_app_icon()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(800, 720)
        self.setMinimumSize(680, 500)

        # 瑞士包豪斯 2.0 全局样式表（温暖纸底、1px 发丝网格、无 3D 斜面、克制自信的视觉焦点）
        check_icon = ui_icon_url("check_white.png")
        chevron_down = ui_icon_url("chevron_down.png")
        chevron_up = ui_icon_url("chevron_up.png")
        self.setStyleSheet(f"""
            QWidget {{
                font-family: "Go Noto Kurrent-Regular";
                font-size: 13px;
                color: {THEME["ink"]};
                background-color: {THEME["canvas"]};
            }}
            QLabel {{
                background-color: transparent;
            }}
            QComboBox, QSpinBox {{
                border: 1px solid {THEME["border_strong"]};
                border-radius: 0px;
                padding: 5px 26px 5px 10px;
                background-color: {THEME["surface"]};
                color: {THEME["ink"]};
                font-weight: 600;
            }}
            QComboBox {{
                combobox-popup: 0;
            }}
            QSpinBox {{
                padding: 5px 24px 5px 10px;
            }}
            QComboBox:hover, QSpinBox:hover {{
                border-color: {THEME["ink"]};
            }}
            QComboBox:focus, QSpinBox:focus {{
                border-color: {THEME["cobalt"]};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border: none;
                background: transparent;
            }}
            QComboBox::down-arrow {{
                width: 11px;
                height: 11px;
                image: url("{chevron_down}");
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                subcontrol-origin: border;
                width: 18px;
                border: none;
                background: transparent;
            }}
            QSpinBox::up-button {{
                subcontrol-position: top right;
            }}
            QSpinBox::down-button {{
                subcontrol-position: bottom right;
            }}
            QSpinBox::up-arrow {{
                width: 9px;
                height: 9px;
                image: url("{chevron_up}");
            }}
            QSpinBox::down-arrow {{
                width: 9px;
                height: 9px;
                image: url("{chevron_down}");
            }}
            QComboBox QAbstractItemView {{
                background-color: #ffffff;
                color: #121212;
                border: 1px solid {THEME["ink"]};
                border-radius: 0px;
                padding: 3px;
                selection-background-color: {THEME["ink"]};
                selection-color: #ffffff;
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                min-height: 26px;
                padding: 4px 10px;
                color: #121212;
                background-color: #ffffff;
            }}
            QComboBox QAbstractItemView::item:hover,
            QComboBox QAbstractItemView::item:selected {{
                background-color: {THEME["ink"]};
                color: #ffffff;
            }}
            QCheckBox {{
                spacing: 7px;
                font-weight: 600;
                color: {THEME["ink_secondary"]};
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border-radius: 0px;
                border: 1.5px solid {THEME["border_strong"]};
                background-color: {THEME["surface"]};
            }}
            QCheckBox::indicator:hover {{
                border-color: {THEME["ink"]};
            }}
            QCheckBox::indicator:checked {{
                border-color: {THEME["ink"]};
                background-color: {THEME["ink"]};
                image: url("{check_icon}");
            }}
            /* 静默细雅的 6px 滚动条，彻底消除沉重黑条 */
            QScrollBar:vertical {{
                border: none;
                background: transparent;
                width: 6px;
                margin: 2px 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {THEME["border_strong"]};
                min-height: 28px;
                border-radius: 0px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {THEME["ink_secondary"]};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QSplitter::handle {{
                background-color: transparent;
                width: 14px;
            }}
            QToolTip, QLabel#qtooltip_label {{
                background-color: #ffffff;
                color: {THEME["ink"]};
                border: 1px solid {THEME["ink"]};
                padding: 5px 9px;
                font-family: "Go Noto Kurrent-Regular";
                font-size: 12px;
                font-weight: 500;
                border-radius: 0px;
            }}
        """)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(24, 16, 24, 18)
        root_layout.setSpacing(14)

        # -------------------------------------------------------------------
        # 1. 顶部应用导航栏（谦虚内敛、底部分割发丝线统领全栏）
        # -------------------------------------------------------------------
        top_bar = FlowLayout(margin=0, h_spacing=12, v_spacing=8, justify_last_on_first_row=True)

        app_title = QLabel("OfflinePDF")
        app_title.setStyleSheet(f"font-size: 20px; font-weight: 800; letter-spacing: -0.4px; color: {THEME['ink']};")

        app_badge = QLabel("TRANSLATE")
        app_badge.setStyleSheet(f"""
            background-color: {THEME["surface_muted"]};
            color: {THEME["ink_secondary"]};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.6px;
            padding: 3px 7px;
            border-radius: 0px;
        """)

        mode_btn_style = f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                color: {THEME["ink_secondary"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
                padding: 5px 14px;
                font-size: 12.5px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                border-color: {THEME["ink"]};
                color: {THEME["ink"]};
            }}
            QPushButton:checked {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border-color: {THEME["ink"]};
            }}
        """
        self.btn_mode_pdf = QPushButton("文献翻译")
        self.btn_mode_pdf.setCheckable(True)
        self.btn_mode_pdf.setChecked(True)
        self.btn_mode_pdf.setCursor(Qt.PointingHandCursor)
        self.btn_mode_pdf.setStyleSheet(mode_btn_style)
        self.btn_mode_pdf.clicked.connect(lambda: self.switch_mode(0))

        self.btn_mode_daily = QPushButton("日常翻译")
        self.btn_mode_daily.setCheckable(True)
        self.btn_mode_daily.setChecked(False)
        self.btn_mode_daily.setCursor(Qt.PointingHandCursor)
        self.btn_mode_daily.setStyleSheet(mode_btn_style)
        self.btn_mode_daily.clicked.connect(lambda: self.switch_mode(1))

        mode_widget = make_inline_group(self.btn_mode_pdf, self.btn_mode_daily, spacing=0)
        brand_group = make_inline_group(app_title, app_badge, spacing=10)
        top_bar.addWidget(brand_group)
        top_bar.addWidget(mode_widget)

        self.ui_language_combo = configure_localized_combo(QComboBox())
        self.ui_language_combo.setToolTip("Language / 语言 / 言語 / 언어 / Язык")
        self.ui_language_combo.setStyleSheet(f"""
            QComboBox {{
                border: 1px solid {THEME["border"]};
                background-color: {THEME["surface"]};
                color: {THEME["ink_secondary"]};
                font-size: 12px;
                font-weight: 600;
                padding: 5px 24px 5px 10px;
            }}
            QComboBox:hover {{
                border-color: {THEME["border_strong"]};
                color: {THEME["ink"]};
            }}
        """)
        for name, code in i18n.LANGUAGES:
            self.ui_language_combo.addItem(name, code)
        self.ui_language_combo.setCurrentIndex(self.ui_language_combo.findData(i18n.saved_language()))
        self.ui_language_combo.currentIndexChanged.connect(self.on_ui_language_changed)

        # 顶部操作按钮（谦虚发丝边框与幽灵清空按钮）
        self.btn_add_files = QPushButton("ui.add")
        self.btn_add_files.setCursor(Qt.PointingHandCursor)
        self.btn_add_files.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border_strong"]};
                border-radius: 0px;
                padding: 5px 13px;
                font-weight: 700;
                font-size: 12px;
                color: {THEME["ink"]};
            }}
            QPushButton:hover {{
                background-color: {THEME["ink"]};
                border-color: {THEME["ink"]};
                color: #ffffff;
            }}
        """)
        self.btn_add_files.clicked.connect(self.on_open_file_dialog)
        self.btn_add_files.setVisible(False)

        self.btn_clear_list = QPushButton("ui.clear")
        self.btn_clear_list.setCursor(Qt.PointingHandCursor)
        self.btn_clear_list.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                padding: 5px 8px;
                color: {THEME["ink_muted"]};
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                color: {THEME["vermilion"]};
            }}
        """)
        self.btn_clear_list.clicked.connect(self.on_clear_all_tasks)
        self.btn_clear_list.setVisible(False)

        top_actions_group = QWidget()
        top_actions_group.setObjectName("TopActionsGroup")
        top_actions_group.setStyleSheet("#TopActionsGroup { background: transparent; }")
        top_actions_layout = QHBoxLayout(top_actions_group)
        top_actions_layout.setContentsMargins(0, 0, 0, 0)
        top_actions_layout.setSpacing(8)
        top_actions_layout.addWidget(self.ui_language_combo)
        top_actions_layout.addWidget(self.btn_add_files)
        top_actions_layout.addWidget(self.btn_clear_list)
        top_bar.addWidget(top_actions_group)

        root_layout.addLayout(top_bar)

        header_rule = QFrame()
        header_rule.setFixedHeight(1)
        header_rule.setStyleSheet(f"background-color: {THEME['border']}; border: none;")
        root_layout.addWidget(header_rule)

        # -------------------------------------------------------------------
        # 2. 顶部模式容器：Page 0 文献翻译 (PDF) vs Page 1 日常翻译
        # -------------------------------------------------------------------
        self.mode_stack = QStackedWidget()
        self.pdf_page = QWidget()
        pdf_page_layout = QVBoxLayout(self.pdf_page)
        pdf_page_layout.setContentsMargins(0, 0, 0, 0)
        pdf_page_layout.setSpacing(14)

        # 中部核心区域：左右分栏 (Splitter，留白呼吸、零裁切)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # [左侧] 任务队列栏 (Tasks Panel)
        left_container = QWidget()
        left_container.setMinimumWidth(360)
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        # 左栏标头（含展开/收起翻译流控制按钮）
        left_header = QHBoxLayout()
        left_title = QLabel("ui.documents")
        left_title.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {THEME['ink']};")
        left_header.addWidget(left_title)
        left_header.addStretch(1)

        self.btn_toggle_stream = QPushButton("ui.show_live")
        self.btn_toggle_stream.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_stream.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
                padding: 4px 11px;
                font-weight: 600;
                font-size: 11.5px;
                color: {THEME["ink_secondary"]};
            }}
            QPushButton:hover {{
                border-color: {THEME["ink"]};
                color: {THEME["ink"]};
            }}
        """)
        self.btn_toggle_stream.clicked.connect(self.toggle_stream_panel)
        left_header.addWidget(self.btn_toggle_stream)

        left_layout.addLayout(left_header)

        # 左侧自适应双模态容器：空态 vs 任务列表
        self.stack = QStackedWidget()

        # 页面 0: 空态拖拽
        self.empty_hero = EmptyHeroView(self)
        self.empty_hero.files_dropped.connect(self.add_files_to_queue)
        self.empty_hero.select_clicked.connect(self.on_open_file_dialog)
        self.stack.addWidget(self.empty_hero)

        # 页面 1: 任务卡片滚动列表
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; }")

        self.list_container = QWidget()
        self.list_container.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)

        self.scroll_area.setWidget(self.list_container)
        self.stack.addWidget(self.scroll_area)

        left_layout.addWidget(self.stack, stretch=1)

        # [右侧] 实时翻译流与成果核查栏 (默认折叠，点击“展开翻译流”后展示)
        self.live_panel = LiveInspectionPanel(self)
        self.live_panel.setMinimumWidth(440)
        self.live_panel.btn_back_to_live.clicked.connect(self.on_back_to_live_clicked)
        self.live_panel.setVisible(False)

        # 将左右加入 Splitter
        self.splitter.addWidget(left_container)
        self.splitter.addWidget(self.live_panel)
        self.splitter.setSizes([460, 680])

        pdf_page_layout.addWidget(self.splitter, stretch=1)

        # -------------------------------------------------------------------
        # 3. 硬件手动微调抽屉（默认折叠，仅高级用户按需展开，安静退居底层）
        # -------------------------------------------------------------------
        self.manual_hw_widget = QFrame()
        self.manual_hw_widget.setObjectName("ManualHwDrawer")
        self.manual_hw_widget.setStyleSheet(f"""
            QFrame#ManualHwDrawer {{
                background-color: {THEME["surface_muted"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)
        manual_vbox = QVBoxLayout(self.manual_hw_widget)
        manual_vbox.setContentsMargins(14, 10, 14, 10)
        manual_vbox.setSpacing(6)

        manual_grid = FlowLayout(margin=0, h_spacing=12, v_spacing=6)

        t_label = QLabel("ui.threads")
        t_label.setStyleSheet(f"color: {THEME['ink_secondary']}; font-size: 12px; font-weight: 600;")
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 64)
        self.threads_spin.setValue(self.auto_profile.threads)
        self.threads_spin.valueChanged.connect(lambda: self._save_queue())

        p_label = QLabel("ui.parallel")
        p_label.setStyleSheet(f"color: {THEME['ink_secondary']}; font-size: 12px; font-weight: 600;")
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(self.auto_profile.parallel)
        self.parallel_spin.valueChanged.connect(lambda: self._save_queue())

        c_label = QLabel("ui.context")
        c_label.setStyleSheet(f"color: {THEME['ink_secondary']}; font-size: 12px; font-weight: 600;")
        self.ctx_combo = configure_localized_combo(QComboBox())
        self.ctx_combo.addItem("4096", 4096)
        self.ctx_combo.addItem("8192", 8192)
        idx = self.ctx_combo.findData(self.auto_profile.context_size)
        if idx >= 0:
            self.ctx_combo.setCurrentIndex(idx)
        self.ctx_combo.currentIndexChanged.connect(lambda: self._save_queue())

        g_label = QLabel("ui.gpu_layers")
        g_label.setStyleSheet(f"color: {THEME['ink_secondary']}; font-size: 12px; font-weight: 600;")
        self.gpu_layers_spin = QSpinBox()
        self.gpu_layers_spin.setRange(0, 99)
        self.gpu_layers_spin.setValue(self.auto_profile.n_gpu_layers)
        self.gpu_layers_spin.valueChanged.connect(lambda: self._save_queue())

        manual_grid.addWidget(make_inline_group(t_label, self.threads_spin))
        manual_grid.addWidget(make_inline_group(p_label, self.parallel_spin))
        manual_grid.addWidget(make_inline_group(c_label, self.ctx_combo))
        manual_grid.addWidget(make_inline_group(g_label, self.gpu_layers_spin))
        manual_vbox.addLayout(manual_grid)

        self.hw_rec_hint = QLabel(
            f"(推荐: {self.auto_profile.threads}核/{self.auto_profile.parallel}槽/"
            f"{self.auto_profile.context_size}窗口/{self.auto_profile.n_gpu_layers}层)"
        )
        self.hw_rec_hint.setWordWrap(True)
        self.hw_rec_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.hw_rec_hint.setStyleSheet(f"color: {THEME['ink_muted']}; font-size: 11px; font-weight: 500;")
        manual_vbox.addWidget(self.hw_rec_hint)

        self.manual_hw_widget.setVisible(False)
        pdf_page_layout.addWidget(self.manual_hw_widget)

        # -------------------------------------------------------------------
        # 4. 底部控制台（简洁、聚焦：选好语言即可一键开始）
        # -------------------------------------------------------------------
        footer_frame = QFrame()
        footer_frame.setObjectName("FooterFrame")
        footer_frame.setStyleSheet(f"""
            QFrame#FooterFrame {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)
        footer_layout = QVBoxLayout(footer_frame)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        footer_layout.setSpacing(10)

        footer_top_row = FlowLayout(margin=0, h_spacing=14, v_spacing=8, justify_last_on_first_row=True)

        # 语言选择（源语言与目标语言完整对齐 Hy-MT2-1.8B 官方支持语种）
        source_label = QLabel("ui.source")
        source_label.setStyleSheet(f"border: none; color: {THEME['ink_secondary']}; font-size: 12.5px; font-weight: 700;")

        self.source_lang_combo = configure_localized_combo(QComboBox())
        self.source_lang_combo.addItem(i18n.tr("自动识别"), "auto")
        for label, code in SUPPORTED_LANGUAGES:
            self.source_lang_combo.addItem(label, code)
        self.source_lang_combo.currentIndexChanged.connect(lambda: self._save_queue())

        lang_label = QLabel("ui.target")
        lang_label.setStyleSheet(f"border: none; color: {THEME['ink_secondary']}; font-size: 12.5px; font-weight: 700;")

        self.lang_combo = configure_localized_combo(QComboBox())
        for label, code in SUPPORTED_LANGUAGES:
            self.lang_combo.addItem(label, code)
        self.lang_combo.currentIndexChanged.connect(lambda: self._save_queue())

        self.auto_hw_checkbox = QCheckBox("ui.auto_hardware")
        self.auto_hw_checkbox.setStyleSheet(f"QCheckBox {{ border: none; color: {THEME['ink_muted']}; font-size: 12px; font-weight: 600; }}")
        self.auto_hw_checkbox.setChecked(True)
        self.auto_hw_checkbox.setToolTip(f"画像: {self.auto_profile.device_summary}")
        self.auto_hw_checkbox.toggled.connect(self.on_auto_hw_toggled)

        footer_top_row.addWidget(make_inline_group(source_label, self.source_lang_combo))
        footer_top_row.addWidget(make_inline_group(lang_label, self.lang_combo))
        footer_top_row.addWidget(self.auto_hw_checkbox)
        footer_layout.addLayout(footer_top_row)

        footer_bottom_row = QHBoxLayout()
        footer_bottom_row.setSpacing(12)

        # 中部：总体状态微文字与静默 4px 钴蓝进度轨
        self.global_status_label = QLabel("")
        self.global_status_label.setWordWrap(True)
        self.global_status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.global_status_label.setStyleSheet(f"border: none; color: {THEME['ink_secondary']}; font-size: 12px; font-weight: 600;")

        self.global_progress = QProgressBar()
        self.global_progress.setFixedHeight(4)
        self.global_progress.setMinimumWidth(80)
        self.global_progress.setTextVisible(False)
        self.global_progress.setValue(0)
        self.global_progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: {THEME["surface_muted"]};
                border: none;
                border-radius: 0px;
            }}
            QProgressBar::chunk {{
                background-color: {THEME["cobalt"]};
                border-radius: 0px;
            }}
        """)
        self.global_progress.setVisible(False)

        footer_bottom_row.addWidget(self.global_status_label, stretch=1)
        footer_bottom_row.addWidget(self.global_progress, stretch=1)

        # 核心控制按钮：唯一鲜明的主行动焦点
        self.btn_stop = QPushButton("ui.stop")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setMinimumWidth(96)
        self.btn_stop.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                color: {THEME["vermilion"]};
                border: 1.5px solid {THEME["vermilion"]};
                border-radius: 0px;
                padding: 9px 20px;
                font-size: 13px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["vermilion"]};
                color: #ffffff;
            }}
        """)
        self.btn_stop.clicked.connect(self.on_stop_requested)
        self.btn_stop.setVisible(False)

        self.btn_start = QPushButton("ui.start")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setMinimumWidth(120)
        self.btn_start.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: none;
                border-radius: 0px;
                padding: 9px 24px;
                font-size: 13px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
            }}
            QPushButton:disabled {{
                background-color: {THEME["surface_muted"]};
                color: {THEME["ink_muted"]};
            }}
        """)
        self.btn_start.clicked.connect(self.on_start_requested)

        footer_bottom_row.addWidget(self.btn_stop)
        footer_bottom_row.addWidget(self.btn_start)
        footer_layout.addLayout(footer_bottom_row)

        self.script_warning = QLabel('该语言的 PDF 文字方向或组合可能不正确，生成后请检查。')
        self.script_warning.setWordWrap(True)
        self.script_warning.setStyleSheet(f'color: {THEME["ochre"]}; font-size: 12px;')
        pdf_page_layout.addWidget(self.script_warning)
        pdf_page_layout.addWidget(footer_frame)
        self.lang_combo.currentIndexChanged.connect(self._update_script_warning)
        self._update_script_warning()

        from core.daily_ui import DailyTranslatePanel
        self.daily_panel = DailyTranslatePanel(
            hw_config_provider=self._daily_hw_config,
            is_pdf_busy_provider=lambda: bool(self.worker and self.worker.isRunning()),
            parent=self,
        )
        self.mode_stack.addWidget(self.pdf_page)
        self.mode_stack.addWidget(self.daily_panel)
        root_layout.addWidget(self.mode_stack, stretch=1)

        QShortcut(QKeySequence("Ctrl+1"), self).activated.connect(lambda: self.switch_mode(0))
        QShortcut(QKeySequence("Ctrl+2"), self).activated.connect(lambda: self.switch_mode(1))

    # -----------------------------------------------------------------------
    # 交互槽函数与业务逻辑
    # -----------------------------------------------------------------------

    def _daily_hw_config(self):
        if self.auto_hw_checkbox.isChecked():
            prof = detect_hardware_profile()
            return prof.threads, prof.context_size, prof.n_gpu_layers
        return self.threads_spin.value(), self.ctx_combo.currentData(), self.gpu_layers_spin.value()

    def switch_mode(self, mode_idx: int):
        mode_idx = 1 if mode_idx == 1 else 0
        self.mode_stack.setCurrentIndex(mode_idx)
        self.btn_mode_pdf.setChecked(mode_idx == 0)
        self.btn_mode_daily.setChecked(mode_idx == 1)
        if mode_idx == 1:
            self.btn_add_files.setVisible(False)
            self.btn_clear_list.setVisible(False)
            self.daily_panel.sync_pdf_busy_state()
            if not self.isMaximized() and self.width() < 980:
                self.resize(1040, max(self.height(), 700))
            self.daily_panel.source_edit.setFocus()
        else:
            self._update_queue_ui()

    def _update_script_warning(self):
        self.script_warning.setVisible(self.lang_combo.currentData() in COMPLEX_SCRIPT_LANGS)

    def on_ui_language_changed(self):
        i18n.set_language(self.ui_language_combo.currentData())
        self.setWindowTitle(i18n.tr("OfflinePDF Translate — 本地离线PDF文献翻译"))
        if hasattr(self, "source_lang_combo"):
            self.source_lang_combo.setItemText(0, i18n.tr("自动识别"))
            self.source_lang_combo.updateGeometry()
        for combo_name in ("ui_language_combo", "lang_combo", "ctx_combo"):
            combo = getattr(self, combo_name, None)
            if combo is not None:
                combo.updateGeometry()
        for widget in self.findChildren(QWidget):
            if isinstance(widget, _LocalizedText):
                widget.retranslate()
        for card in self.card_widgets.values():
            card.update_state()
        if hasattr(self, "daily_panel"):
            self.daily_panel.retranslate()
        refresh_layouts(self)
        if not self.isMaximized():
            min_sz = self.minimumSizeHint()
            if self.width() < min_sz.width() or self.height() < min_sz.height():
                target_w = max(self.width(), min_sz.width())
                target_h = max(self.height(), min_sz.height())
                screen = self.screen()
                if screen is not None:
                    avail = screen.availableGeometry()
                    target_w = min(target_w, avail.width())
                    target_h = min(target_h, avail.height())
                self.resize(target_w, target_h)

    def toggle_stream_panel(self):
        """展开/收起右侧实时翻译流与核查面板。"""
        is_expanded = not self.live_panel.isHidden()
        if is_expanded:
            self.live_panel.setVisible(False)
            self.btn_toggle_stream.setText("ui.show_live")
            if not self.isMaximized() and self.width() > 820:
                self.resize(max(800, self.minimumSizeHint().width()), self.height())
        else:
            self.live_panel.setVisible(True)
            self.btn_toggle_stream.setText("ui.hide_live")
            if not self.isMaximized() and self.width() < 1040:
                self.resize(1180, self.height())
            self.splitter.setSizes([460, 680])

    def on_auto_hw_toggled(self, checked: bool):
        self.manual_hw_widget.setVisible(not checked)
        if not checked and not self._restoring_queue:
            self.auto_profile = detect_hardware_profile()
            self.threads_spin.setValue(self.auto_profile.threads)
            self.parallel_spin.setValue(self.auto_profile.parallel)
            idx = self.ctx_combo.findData(self.auto_profile.context_size)
            if idx >= 0:
                self.ctx_combo.setCurrentIndex(idx)
            self.gpu_layers_spin.setValue(self.auto_profile.n_gpu_layers)
            self.hw_rec_hint.setText(
                f"(推荐: {self.auto_profile.threads}核/{self.auto_profile.parallel}槽/"
                f"{self.auto_profile.context_size}窗口/{self.auto_profile.n_gpu_layers}层)"
            )
            self.auto_hw_checkbox.setToolTip(f"画像: {self.auto_profile.device_summary}")
        if not self._restoring_queue:
            self._save_queue()
        if self.layout() is not None:
            self.layout().activate()

    def _save_queue(self):
        if self._restoring_queue:
            return
        try:
            path = user_data_dir() / 'queue.json'
            payload = {
                'version': 1,
                'source_language': self.source_lang_combo.currentData(),
                'target_language': self.lang_combo.currentData(),
                'auto_hardware': self.auto_hw_checkbox.isChecked(),
                'threads': self.threads_spin.value(),
                'parallel': self.parallel_spin.value(),
                'context_size': self.ctx_combo.currentData(),
                'n_gpu_layers': self.gpu_layers_spin.value(),
                'tasks': [
                    {
                        'path': str(t.file_path),
                        'status': t.status,
                        'mono_pdf': t.mono_pdf,
                        'dual_pdf': t.dual_pdf,
                        'error': t.error_msg,
                    }
                    for t in self.tasks
                ],
            }
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, path)
        except (OSError, TypeError):
            logger.exception('Unable to save the task queue')

    def _restore_queue(self):
        try:
            payload = json.loads((user_data_dir() / 'queue.json').read_text(encoding='utf-8'))
            version = payload.get('version') if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not isinstance(version, int) or isinstance(version, bool) or version < 1:
                return
        except (OSError, ValueError):
            return
        self._restoring_queue = True
        pruned_ghost_tasks = False
        try:
            src_index = self.source_lang_combo.findData(payload.get('source_language'))
            if src_index >= 0:
                self.source_lang_combo.setCurrentIndex(src_index)
            index = self.lang_combo.findData(payload.get('target_language'))
            if index >= 0:
                self.lang_combo.setCurrentIndex(index)
            if isinstance(payload.get('threads'), int):
                self.threads_spin.setValue(max(1, min(64, payload['threads'])))
            if isinstance(payload.get('parallel'), int):
                self.parallel_spin.setValue(max(1, min(16, payload['parallel'])))
            ctx_index = self.ctx_combo.findData(payload.get('context_size'))
            if ctx_index >= 0:
                self.ctx_combo.setCurrentIndex(ctx_index)
            if isinstance(payload.get('n_gpu_layers'), int):
                self.gpu_layers_spin.setValue(max(0, min(99, payload['n_gpu_layers'])))
            if isinstance(payload.get('auto_hardware'), bool):
                self.auto_hw_checkbox.setChecked(payload['auto_hardware'])
            seen_paths = set()
            for item in payload.get('tasks', []):
                if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                    pruned_ghost_tasks = True
                    continue
                path = Path(item['path'])
                # Real user documents added via dialog or drag-and-drop are always absolute paths;
                # skip non-existent relative paths left by external scripts or unisolated tests.
                if not path.is_absolute() and not path.is_file():
                    pruned_ghost_tasks = True
                    continue
                try:
                    resolved_key = os.path.normcase(str(path.resolve())) if path.is_absolute() else os.path.normcase(str(path))
                except OSError:
                    resolved_key = os.path.normcase(str(path))
                if resolved_key in seen_paths:
                    pruned_ghost_tasks = True
                    continue
                seen_paths.add(resolved_key)
                status = item.get('status', 'waiting')
                if status == 'processing':
                    status = 'stopped'
                if status not in ('waiting', 'stopped', 'finished', 'error', 'review'):
                    status = 'waiting'
                if not path.is_file():
                    status = 'error'
                    item['error'] = '文件不存在'
                mono, dual = item.get('mono_pdf'), item.get('dual_pdf')
                if status == 'finished' and not (mono and dual and Path(mono).is_file() and Path(dual).is_file()):
                    status = 'waiting'
                task = TaskItem(uuid.uuid4().hex, path, status, mono_pdf=mono,
                                dual_pdf=dual, error_msg=item.get('error', ''))
                self._add_task(task)
            self._update_queue_ui()
        except (OSError, TypeError, ValueError):
            logger.exception('Unable to restore part of the task queue')
        finally:
            self._restoring_queue = False
        if pruned_ghost_tasks:
            self._save_queue()

    def _add_task(self, task):
        with self._tasks_lock:
            self.tasks.append(task)
        self.task_blocks[task.id] = deque(maxlen=100)
        card = TaskCardWidget(task)
        card.open_file_signal.connect(self.open_pdf_file)
        card.open_dir_signal.connect(self.open_directory)
        card.remove_signal.connect(self.remove_task)
        card.card_selected_signal.connect(self.on_card_selected)
        self.card_widgets[task.id] = card
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def on_open_file_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, i18n.tr("选择待翻译学术 PDF 文献"), "", i18n.tr("PDF 文件 (*.pdf);;所有文件 (*.*)")
        )
        if paths:
            self.add_files_to_queue(paths)

    def add_files_to_queue(self, paths: List[str]):
        new_count = 0
        existing_paths = {t.file_path.resolve() for t in self.tasks}

        for p_str in paths:
            p = Path(p_str).resolve()
            if not p.exists() or p.suffix.lower() != ".pdf":
                continue
            if p in existing_paths:
                continue

            task_id = uuid.uuid4().hex
            task = TaskItem(id=task_id, file_path=p, status="waiting")
            self._add_task(task)
            existing_paths.add(p)
            new_count += 1

        if new_count > 0:
            self._update_queue_ui()
            self._refresh_running_batch_progress()

    def on_back_to_live_clicked(self):
        """恢复自动跟随正在执行的活跃任务。"""
        self.user_pinned_task_id = None
        if self.active_task_id:
            self.switch_to_task(self.active_task_id, is_user_pin=False)

    def switch_to_task(self, task_id: str, is_user_pin: bool = False):
        """切换右侧翻译流与成果核查面板呈现的目标文献。"""
        task = next((t for t in self.tasks if t.id == task_id), None)
        if not task:
            return

        self.current_inspecting_task_id = task_id

        # 若用户手动点击卡片，且该卡片不是当前活跃执行的任务，则视为用户锁定检视
        is_running = bool(self.worker and self.worker.isRunning())
        if is_user_pin and is_running and self.active_task_id and (task_id != self.active_task_id):
            self.user_pinned_task_id = task_id
        elif not is_user_pin:
            self.user_pinned_task_id = None

        # 更新卡片高亮选中态
        for cid, card in self.card_widgets.items():
            card.set_selected(cid == task_id)

        # 显隐“返回实时流”按钮
        is_viewing_active = (not self.active_task_id) or (self.current_inspecting_task_id == self.active_task_id)
        self.live_panel.btn_back_to_live.setVisible(is_running and not is_viewing_active)

        # 重置面板并载入当前任务的全部已翻译块
        blocks = self.task_blocks.get(task_id, [])
        self.live_panel.reset_view(task.file_path.name, status=task.status)
        self.live_panel.set_task_info(
            task.file_path.name,
            task.translation_progress,
            status=task.status,
            total_blocks=task.translation_total,
        )

        self.live_panel._loading_batch = True
        try:
            # 逆序填充：最先发生的最后插，保证最新段落在最上面
            for b in reversed(blocks):
                self.live_panel.add_translation_block(
                    b.get("index", 0),
                    b.get("source", ""),
                    b.get("target", ""),
                    b.get("status", "success"),
                    b.get("reason", ""),
                    b.get("attempt", 1),
                )
        finally:
            self.live_panel._loading_batch = False
            self.live_panel.scroll_to_latest()

    def on_card_selected(self, task_id: str):
        """点击卡片在右侧展示该文献已有的翻译块。"""
        self.switch_to_task(task_id, is_user_pin=True)

    def _refresh_running_batch_progress(self):
        if not (self.worker and self.worker.isRunning() and self.active_task_id):
            return
        with self._tasks_lock:
            total = len(self.tasks)
            for pos, t in enumerate(self.tasks):
                if t.id == self.active_task_id:
                    overall = int((pos + t.progress / 100) / max(1, total) * 100)
                    self.on_batch_progress(pos + 1, total, overall)
                    break

    def remove_task(self, task_id: str):
        is_running = bool(self.worker and self.worker.isRunning())
        with self._tasks_lock:
            task = next((t for t in self.tasks if t.id == task_id), None)
            if not task:
                return
            if is_running and (task.status == "processing" or task.id == self.active_task_id):
                return
            self.tasks[:] = [t for t in self.tasks if t.id != task_id]

        self.task_blocks.pop(task_id, None)
        self._saved_statuses.pop(task_id, None)
        card = self.card_widgets.pop(task_id, None)
        if card:
            self.list_layout.removeWidget(card)
            card.deleteLater()

        if self.current_inspecting_task_id == task_id:
            self.current_inspecting_task_id = None
            self.user_pinned_task_id = None
            if is_running and self.active_task_id and any(t.id == self.active_task_id for t in self.tasks):
                self.switch_to_task(self.active_task_id, is_user_pin=False)
            else:
                self.live_panel.btn_back_to_live.setVisible(False)
                self.live_panel.reset_view("")

        self._update_queue_ui()
        self._refresh_running_batch_progress()

    def on_clear_all_tasks(self):
        if self.worker and self.worker.isRunning():
            return

        for card in self.card_widgets.values():
            self.list_layout.removeWidget(card)
            card.deleteLater()

        self.card_widgets.clear()
        with self._tasks_lock:
            self.tasks.clear()
        self.task_blocks.clear()
        self._saved_statuses.clear()
        self.current_inspecting_task_id = None
        self.active_task_id = None
        self.user_pinned_task_id = None
        self.live_panel.btn_back_to_live.setVisible(False)
        self.live_panel.reset_view("")
        self._update_queue_ui()

    def _update_queue_ui(self):
        self._save_queue()
        count = len(self.tasks)
        if count == 0:
            self.stack.setCurrentIndex(0)
            self.btn_add_files.setVisible(False)
            self.btn_clear_list.setVisible(False)
            self.btn_start.setEnabled(False)
            self.global_status_label.setText("")
            self.global_progress.setVisible(False)
        else:
            self.stack.setCurrentIndex(1)
            in_pdf_mode = (not hasattr(self, "mode_stack")) or self.mode_stack.currentIndex() == 0
            self.btn_add_files.setVisible(in_pdf_mode)
            self.btn_clear_list.setVisible(in_pdf_mode)

            self.btn_start.setEnabled(not (self.worker and self.worker.isRunning()))

    def on_start_requested(self):
        if not self.tasks or (self.worker and self.worker.isRunning()):
            return

        if hasattr(self, "daily_panel"):
            self.daily_panel.shutdown_runtime()

        # 硬件参数决策
        if self.auto_hw_checkbox.isChecked():
            self.auto_profile = detect_hardware_profile()
            self.auto_hw_checkbox.setToolTip(f"画像: {self.auto_profile.device_summary}")
            threads = self.auto_profile.threads
            parallel = self.auto_profile.parallel
            context_size = self.auto_profile.context_size
            n_gpu_layers = self.auto_profile.n_gpu_layers
        else:
            threads = self.threads_spin.value()
            parallel = self.parallel_spin.value()
            context_size = self.ctx_combo.currentData()
            n_gpu_layers = self.gpu_layers_spin.value()

        lang_in = self.source_lang_combo.currentData() or "auto"
        lang_out = self.lang_combo.currentData()
        self._save_queue()
        self.source_lang_combo.setEnabled(False)
        self.lang_combo.setEnabled(False)

        # UI 状态切换为运行态
        self.btn_start.setVisible(False)
        self.btn_stop.setVisible(True)
        self.btn_add_files.setEnabled(True)
        self.btn_clear_list.setEnabled(False)
        self.global_progress.setVisible(True)
        self.global_progress.setValue(0)
        self.global_status_label.setText("正在准备翻译服务...")
        self.active_task_id = None
        self.user_pinned_task_id = None

        # 启动后台批量执行线程
        self.worker = BatchTranslationWorker(
            tasks=self.tasks,
            lang_in=lang_in,
            lang_out=lang_out,
            threads=threads,
            parallel=parallel,
            context_size=context_size,
            n_gpu_layers=n_gpu_layers,
            tasks_lock=self._tasks_lock,
        )
        self.worker.task_updated.connect(self.on_task_updated)
        self.worker.batch_progress.connect(self.on_batch_progress)
        self.worker.batch_finished.connect(self.on_batch_finished)
        self.worker.batch_stopped.connect(self.on_batch_stopped)
        self.worker.fatal_error.connect(self.on_fatal_error)
        self.worker.block_translated.connect(self.on_block_translated)
        self.worker.finished.connect(self._reset_control_buttons)
        self.worker.start()

    def on_stop_requested(self):
        """用户点击随时停止。"""
        if self.worker and self.worker.isRunning():
            self.global_status_label.setText("正在停止翻译...")
            self.btn_stop.setEnabled(False)
            self.worker.request_stop()

    def on_block_translated(self, task_id: str, source: str, target: str, index: int, status: str, reason: str = "", attempt: int = 1):
        """接收实时段落翻译事件并推入右侧流（最新段落置顶呈现）。"""
        block_entry = {
            "source": source,
            "target": target,
            "index": index,
            "status": status,
            "reason": reason,
            "attempt": attempt,
        }
        if task_id not in self.task_blocks:
            self.task_blocks[task_id] = deque(maxlen=100)
        self.task_blocks[task_id].append(block_entry)

        # 若处于未手动锁定的自动跟随态，且当前不在该任务，则自动切入当前翻译任务
        if self.user_pinned_task_id is None and self.current_inspecting_task_id != task_id:
            self.active_task_id = task_id
            self.switch_to_task(task_id, is_user_pin=False)
            return

        # 若当前正在查看该任务，则动态追加最新文本块并刷新状态
        if self.current_inspecting_task_id == task_id:
            task = next((t for t in self.tasks if t.id == task_id), None)
            if task:
                self.live_panel.set_task_info(
                    task.file_path.name,
                    task.translation_progress,
                    status=task.status,
                    total_blocks=task.translation_total,
                )
            self.live_panel.add_translation_block(index, source, target, status, reason, attempt)

    def on_task_updated(self, task_id: str):
        card = self.card_widgets.get(task_id)
        if card:
            card.update_state()
        task = next((t for t in self.tasks if t.id == task_id), None)
        if not task:
            return
        if self._saved_statuses.get(task_id) != task.status:
            self._saved_statuses[task_id] = task.status
            self._save_queue()

        # 检查是否切换到新的活跃任务
        if task.status == "processing":
            if self.active_task_id != task_id:
                self.active_task_id = task_id
                if self.user_pinned_task_id is None:
                    self.switch_to_task(task_id, is_user_pin=False)
                    return

        # 若当前正在查看该任务，同步更新其标题与阶段进度（翻译流展示纯翻译百分比）
        if self.current_inspecting_task_id == task_id:
            self.live_panel.set_task_info(
                task.file_path.name,
                task.translation_progress,
                status=task.status,
                total_blocks=task.translation_total,
            )

        # 实时更新“返回实时流”按钮状态
        is_running = bool(self.worker and self.worker.isRunning())
        is_viewing_active = (not self.active_task_id) or (self.current_inspecting_task_id == self.active_task_id)
        self.live_panel.btn_back_to_live.setVisible(is_running and not is_viewing_active)

    def on_batch_progress(self, current_idx: int, total: int, overall_percent: int):
        self.global_progress.setValue(overall_percent)
        self.global_status_label.setText(f"正在处理第 {current_idx}/{total} 篇文献 ({overall_percent}%)")

    def on_batch_finished(self):
        self.active_task_id = None
        self.user_pinned_task_id = None
        self.live_panel.btn_back_to_live.setVisible(False)
        self.global_progress.setValue(100)
        done = sum(t.status == "finished" for t in self.tasks)
        failed = sum(t.status == "error" for t in self.tasks)
        review = sum(t.status == "review" for t in self.tasks)
        self.global_status_label.setText(f"处理结束：成功 {done}，失败 {failed}，需复核 {review}")
        self._update_queue_ui()
        if self.current_inspecting_task_id:
            cur_task = next((t for t in self.tasks if t.id == self.current_inspecting_task_id), None)
            if cur_task:
                self.live_panel.set_task_info(
                    cur_task.file_path.name,
                    cur_task.translation_progress,
                    status=cur_task.status,
                    total_blocks=cur_task.translation_total,
                )

    def on_batch_stopped(self):
        self.active_task_id = None
        self.user_pinned_task_id = None
        self.live_panel.btn_back_to_live.setVisible(False)
        self.global_status_label.setText("已停止翻译")
        self._update_queue_ui()
        if self.current_inspecting_task_id:
            cur_task = next((t for t in self.tasks if t.id == self.current_inspecting_task_id), None)
            if cur_task:
                self.live_panel.set_task_info(
                    cur_task.file_path.name,
                    cur_task.translation_progress,
                    status=cur_task.status,
                    total_blocks=cur_task.translation_total,
                )

    def on_fatal_error(self, err: str):
        logger.error("Fatal translation error: %s", err)
        for task in self.tasks:
            if task.status == 'processing':
                task.status, task.stage_text, task.error_msg = 'error', '失败', str(err)
                self.on_task_updated(task.id)
        self.global_status_label.setText("运行中断")
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(self, i18n.tr("执行失败"), f"{i18n.tr('处理过程中发生严重错误:')}\n{i18n.detail(str(err))}")

    def _reset_control_buttons(self):
        self.source_lang_combo.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self._save_queue()
        self.btn_stop.setVisible(False)
        self.btn_stop.setEnabled(True)
        self.btn_start.setVisible(True)
        self.btn_start.setEnabled(True)
        self.btn_add_files.setEnabled(True)
        self.btn_clear_list.setEnabled(True)
        if hasattr(self, "daily_panel"):
            self.daily_panel.sync_pdf_busy_state()

    # -----------------------------------------------------------------------
    # 文件与目录打开
    # -----------------------------------------------------------------------

    def closeEvent(self, event):
        if hasattr(self, "daily_panel"):
            self.daily_panel.shutdown_runtime()
        if self.worker and self.worker.isRunning():
            if not self._close_when_stopped:
                self._close_when_stopped = True
                self.worker.finished.connect(self.close)
            self.worker.request_stop()
            self.global_status_label.setText("正在停止，结束后将关闭窗口")
            event.ignore()
        else:
            self.duration_timer.stop()
            event.accept()

    def _open_local_path(self, target: Optional[str]) -> None:
        if not target or not Path(target).exists():
            return
        try:
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target], start_new_session=True)
            else:
                subprocess.Popen(["xdg-open", target], start_new_session=True)
        except OSError as exc:
            logger.warning("无法打开路径 %s: %s", target, exc)

    def open_pdf_file(self, path_str: Optional[str]):
        self._open_local_path(path_str)

    def open_directory(self, dir_str: Optional[str]):
        self._open_local_path(dir_str)

    # -----------------------------------------------------------------------
    # 窗口级别拖拽支持
    # -----------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if hasattr(self, "mode_stack") and self.mode_stack.currentIndex() != 0:
            return
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if hasattr(self, "mode_stack") and self.mode_stack.currentIndex() != 0:
            return
        files = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.toLocalFile().lower().endswith(".pdf")
        ]
        if files:
            self.add_files_to_queue(files)
            event.acceptProposedAction()


_APP_MUTEX_HANDLE = None


def main():
    import multiprocessing
    multiprocessing.freeze_support()

    # Windows 任务栏应用分组与独立图标关联机制，并注册安装/升级检测互斥锁
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('offlinepdf.translate.desktop')
            global _APP_MUTEX_HANDLE
            _APP_MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, 'OfflinePDFTranslateAppMutex')
        except Exception:
            pass

    app = QApplication(sys.argv)
    if "Fusion" in QStyleFactory.keys():
        app.setStyle("Fusion")
    app.setPalette(build_light_bauhaus_palette())

    # 全局设置应用包豪斯图标
    icon_path = _resolve_app_icon()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))

    log_dir = user_data_dir() / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(log_dir / 'gui.log', encoding='utf-8')])
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
