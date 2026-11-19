# SPDX-License-Identifier: AGPL-3.0-only
"""日常翻译工作台 UI：零心智负担即时互译、Hy-MT2 原生术语表、流式输出与本地历史记录。"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeyEvent, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSlider,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel as QtLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import i18n
from core.daily_translator import (
    AUTO_BILINGUAL_TARGET,
    DAILY_MAX_INPUT_CHARS,
    DAILY_MAX_OUTPUT_CHARS,
    DAILY_SOURCE_LANGUAGES,
    DAILY_TARGET_LANGUAGES,
    DailyGlossaryEntry,
    DailyGlossaryStore,
    DailyHistoryItem,
    DailyHistoryStore,
    DailyRuntimeManager,
    DailyTranslationResult,
    STYLE_OPTIONS,
    clean_pdf_linebreaks,
    language_display_name,
    resolve_daily_direction,
    translate_daily_text,
)
from core.offline import user_data_dir
from core.ui_widgets import (
    FlowLayout,
    QCheckBox,
    QLabel,
    QPushButton,
    THEME,
    configure_localized_combo,
    make_inline_group,
)

logger = logging.getLogger("offlinepdf-translate.daily.ui")

_DROP_TEXT_EXTS = {".txt", ".md", ".markdown", ".srt", ".vtt", ".csv", ".tsv", ".log"}


class DailyTranslationWorker(QThread):
    """后台流式日常翻译线程，复用常驻本地 llama-server。"""

    status_changed = Signal(int, str)
    partial_ready = Signal(int, str, dict)
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        runtime_manager: DailyRuntimeManager,
        request_id: int,
        text: str,
        source_lang: str,
        target_lang: str,
        style_key: str,
        glossary: list[DailyGlossaryEntry],
        use_glossary: bool,
        threads: Optional[int] = None,
        context_size: Optional[int] = None,
        n_gpu_layers: Optional[int] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.runtime_manager = runtime_manager
        self.request_id = request_id
        self.text = text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.style_key = style_key
        self.glossary = list(glossary)
        self.use_glossary = use_glossary
        self.threads = threads
        self.context_size = context_size
        self.n_gpu_layers = n_gpu_layers
        self._cancelled = False

    def request_stop(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            was_running = self.runtime_manager.is_running()
            if not was_running:
                self.status_changed.emit(self.request_id, "waking")
            runtime = self.runtime_manager.ensure_started(
                threads=self.threads,
                context_size=self.context_size,
                n_gpu_layers=self.n_gpu_layers,
                cancelled=lambda: self._cancelled,
            )
            if self._cancelled:
                return
            self.status_changed.emit(self.request_id, "translating")

            last_emit = [0.0]

            def on_partial(partial_text: str, stats: dict) -> None:
                if self._cancelled:
                    return
                now = time.monotonic()
                if now - last_emit[0] >= 0.03:
                    last_emit[0] = now
                    self.partial_ready.emit(self.request_id, partial_text, stats)

            result = translate_daily_text(
                runtime=runtime,
                text=self.text,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                style_key=self.style_key,
                glossary=self.glossary,
                use_glossary=self.use_glossary,
                on_partial=on_partial,
                cancelled=lambda: self._cancelled,
            )
            self.completed.emit(self.request_id, result)
        except Exception as exc:
            if not self._cancelled:
                logger.exception("日常翻译执行失败")
                self.failed.emit(self.request_id, str(exc))


class DailySourceTextEdit(QPlainTextEdit):
    """支持 Ctrl+Enter 快捷翻译及 TXT/MD 文件拖拽导入的输入框。"""

    submit_requested = Signal()
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (event.modifiers() & Qt.ControlModifier) and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.submit_requested.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local and Path(local).suffix.lower() in _DROP_TEXT_EXTS:
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local and Path(local).suffix.lower() in _DROP_TEXT_EXTS:
                    self.file_dropped.emit(local)
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)


def _utf16_len(text: str) -> int:
    """计算字符串在 Qt QTextDocument 中的 UTF-16 码元长度，避免含 Emoji 时光标偏移错位。"""
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


class DailyTargetTextEdit(QPlainTextEdit):
    """译文输出框：捕获用户主动上滚与键盘上翻，以便流式输出时暂停自动跟随。"""

    user_scrolled_up = Signal()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.angleDelta().y() > 0 or event.pixelDelta().y() > 0:
            self.user_scrolled_up.emit()
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Home):
            self.user_scrolled_up.emit()
        super().keyPressEvent(event)


class DailyTranslatePanel(QWidget):
    """包豪斯高反差日常翻译工作台（独立页面，零干扰 PDF 流程）。"""

    def __init__(
        self,
        hw_config_provider: Optional[Callable[[], tuple[Optional[int], Optional[int], Optional[int]]]] = None,
        is_pdf_busy_provider: Optional[Callable[[], bool]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._hw_config_provider = hw_config_provider
        self._is_pdf_busy_provider = is_pdf_busy_provider

        self.runtime_manager = DailyRuntimeManager()
        self.glossary_store = DailyGlossaryStore()
        self.history_store = DailyHistoryStore()

        self._worker: Optional[DailyTranslationWorker] = None
        self._request_seq = 0
        self._pending_request: Optional[tuple] = None
        self._restoring_settings = False
        self._last_copied_output = ""
        self._suppress_auto_translate = False
        self._last_detected_src = "en"
        self._last_resolved_tgt = "zh"
        self._last_matched_terms: list[DailyGlossaryEntry] = []
        self._source_char_count = 0
        self._target_char_count = 0
        self._target_auto_scroll = True
        self._target_updating_text = False
        self._target_streaming = False
        self._output_notice_key = ""
        self._status_mode = ""
        self._status_pct = 0
        self._engine_ui_state = ""

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(450)
        self._debounce_timer.timeout.connect(self._on_debounce_trigger)

        self._copy_flash_timer = QTimer(self)
        self._copy_flash_timer.setSingleShot(True)
        self._copy_flash_timer.setInterval(1500)
        self._copy_flash_timer.timeout.connect(self._restore_copy_button_text)

        self._idle_unload_timer = QTimer(self)
        self._idle_unload_timer.setSingleShot(True)
        self._idle_unload_timer.setInterval(10 * 60 * 1000)
        self._idle_unload_timer.timeout.connect(self._on_idle_unload_timeout)

        self._init_ui()
        self._restore_settings()
        self._refresh_glossary_list()
        self._refresh_history_list()
        self._update_direction_preview()
        self._update_engine_status_ui()

    def _translate_btn_idle_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: none;
                border-radius: 0px;
                padding: 7px 18px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
            }}
            QPushButton:disabled {{
                background-color: {THEME["surface_muted"]};
                color: {THEME["ink_muted"]};
            }}
        """

    def _translate_btn_stop_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                color: {THEME["vermilion"]};
                border: 1.5px solid {THEME["vermilion"]};
                border-radius: 0px;
                padding: 6px 18px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["vermilion"]};
                color: #ffffff;
            }}
        """

    def _copy_btn_default_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                color: {THEME["ink"]};
                border: 1px solid {THEME["ink"]};
                border-radius: 0px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["ink"]};
                color: #ffffff;
            }}
        """

    def _copy_btn_copied_style(self) -> str:
        return f"""
            QPushButton {{
                background-color: {THEME["sage_soft"]};
                color: {THEME["sage"]};
                border: 1px solid {THEME["sage_border"]};
                border-radius: 0px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 700;
            }}
        """

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # -------------------------------------------------------------------
        # 1. 顶部日常翻译控制栏（克制精简、自动互译、零心智负担）
        # -------------------------------------------------------------------
        control_frame = QFrame()
        control_frame.setObjectName("DailyControlFrame")
        control_frame.setStyleSheet(f"""
            QFrame#DailyControlFrame {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)
        ctrl_layout = FlowLayout(control_frame, margin=0, h_spacing=12, v_spacing=8, justify_last_on_first_row=True)
        ctrl_layout.setContentsMargins(16, 10, 16, 10)

        self.src_combo = configure_localized_combo(QComboBox())
        for label, code in DAILY_SOURCE_LANGUAGES:
            self.src_combo.addItem(i18n.tr(label) if code == "auto" else label, code)
        self.src_combo.currentIndexChanged.connect(self._on_language_or_style_changed)

        self.btn_swap_lang = QPushButton("⇄")
        self.btn_swap_lang.setFixedSize(32, 30)
        self.btn_swap_lang.setCursor(Qt.PointingHandCursor)
        self.btn_swap_lang.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["surface_soft"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
                font-size: 14px;
                font-weight: 700;
                color: {THEME["ink_secondary"]};
            }}
            QPushButton:hover {{
                background-color: {THEME["ink"]};
                border-color: {THEME["ink"]};
                color: #ffffff;
            }}
        """)
        self.btn_swap_lang.clicked.connect(self.on_swap_languages)

        self.tgt_combo = configure_localized_combo(QComboBox())
        for label, code in DAILY_TARGET_LANGUAGES:
            display = i18n.tr(label) if code == AUTO_BILINGUAL_TARGET else label
            self.tgt_combo.addItem(display, code)
        self.tgt_combo.currentIndexChanged.connect(self._on_language_or_style_changed)

        lbl_style = QLabel("风格:")
        lbl_style.setStyleSheet(f"border: none; font-size: 12px; font-weight: 600; color: {THEME['ink_muted']};")
        self.style_combo = configure_localized_combo(QComboBox())
        for label, key in STYLE_OPTIONS:
            self.style_combo.addItem(i18n.tr(label), key)
        self.style_combo.currentIndexChanged.connect(self._on_language_or_style_changed)

        ctrl_layout.addWidget(make_inline_group(self.src_combo, self.btn_swap_lang, self.tgt_combo, spacing=6))
        ctrl_layout.addWidget(make_inline_group(lbl_style, self.style_combo, spacing=6))

        drawer_btn_style = f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
                padding: 5px 12px;
                font-size: 12px;
                font-weight: 600;
                color: {THEME["ink_secondary"]};
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

        self.btn_toggle_glossary = QPushButton("术语表")
        self.btn_toggle_glossary.setCheckable(True)
        self.btn_toggle_glossary.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_glossary.setStyleSheet(drawer_btn_style)
        self.btn_toggle_glossary.clicked.connect(lambda: self._toggle_drawer(0))

        self.btn_toggle_history = QPushButton("历史记录")
        self.btn_toggle_history.setCheckable(True)
        self.btn_toggle_history.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_history.setStyleSheet(drawer_btn_style)
        self.btn_toggle_history.clicked.connect(lambda: self._toggle_drawer(1))

        ctrl_layout.addWidget(make_inline_group(self.btn_toggle_glossary, self.btn_toggle_history, spacing=8))
        root.addWidget(control_frame)

        # PDF 任务繁忙提示条（默认隐藏）
        self.pdf_busy_banner = QLabel("正在翻译 PDF 文档，请等待完成或停止后再使用日常翻译。")
        self.pdf_busy_banner.setWordWrap(True)
        self.pdf_busy_banner.setStyleSheet(f"""
            background-color: {THEME["ochre_soft"]};
            color: {THEME["ochre"]};
            border: 1px solid {THEME["ochre_border"]};
            padding: 8px 14px;
            font-size: 12px;
            font-weight: 600;
        """)
        self.pdf_busy_banner.setVisible(False)
        root.addWidget(self.pdf_busy_banner)

        # -------------------------------------------------------------------
        # 2. 主工作区：消除框套框的纯粹纸面双栏译写空间 + 右侧可折叠抽屉
        # -------------------------------------------------------------------
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setObjectName("DailyMainSplitter")
        self.main_splitter.setHandleWidth(12)
        self.main_splitter.setStyleSheet("QSplitter#DailyMainSplitter::handle { background: transparent; }")
        self.main_splitter.setChildrenCollapsible(False)

        self.panes_splitter = QSplitter(Qt.Horizontal)
        self.panes_splitter.setObjectName("DailyPanesSplitter")
        self.panes_splitter.setHandleWidth(12)
        self.panes_splitter.setStyleSheet("QSplitter#DailyPanesSplitter::handle { background: transparent; }")
        self.panes_splitter.setChildrenCollapsible(False)

        ghost_link_style = f"""
            QPushButton {{
                background: transparent;
                border: none;
                padding: 3px 6px;
                font-size: 11px;
                font-weight: 600;
                color: {THEME["ink_muted"]};
            }}
            QPushButton:hover {{
                color: {THEME["ink"]};
            }}
        """

        # [左栏] 原文输入卡片
        source_card = QFrame()
        source_card.setObjectName("DailySourceCard")
        source_card.setStyleSheet(f"""
            #DailySourceCard {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)
        src_vbox = QVBoxLayout(source_card)
        src_vbox.setContentsMargins(18, 14, 18, 14)
        src_vbox.setSpacing(10)

        src_header = FlowLayout(margin=0, h_spacing=6, v_spacing=6, justify_last_on_first_row=True)
        src_tag = QLabel("原文")
        src_tag.setStyleSheet(f"background-color: {THEME['surface_pill']}; color: {THEME['ink']}; border: 1px solid {THEME['border']}; font-size: 11px; font-weight: 700; padding: 2px 8px;")
        self.detected_dir_pill = QtLabel("")
        self.detected_dir_pill.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.detected_dir_pill.setStyleSheet(f"background-color: transparent; color: {THEME['ink_muted']}; font-size: 11px; font-weight: 600; padding: 2px 4px;")
        self.detected_dir_pill.setVisible(False)

        # 隐藏“粘贴并翻译”，保留属性以维持方法兼容
        self.btn_paste_translate = QPushButton("粘贴并翻译", self)
        self.btn_paste_translate.setVisible(False)
        self.btn_paste_translate.clicked.connect(self.paste_and_translate)

        self.btn_unwrap_lines = QPushButton("整理换行")
        self.btn_unwrap_lines.setToolTip("合并复制文本中多余的换行")
        self.btn_unwrap_lines.setCursor(Qt.PointingHandCursor)
        self.btn_unwrap_lines.setStyleSheet(ghost_link_style)
        self.btn_unwrap_lines.clicked.connect(self.unwrap_source_linebreaks)

        self.btn_clear_source = QPushButton("清空")
        self.btn_clear_source.setCursor(Qt.PointingHandCursor)
        self.btn_clear_source.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                padding: 3px 6px;
                font-size: 11px;
                font-weight: 600;
                color: {THEME["ink_muted"]};
            }}
            QPushButton:hover {{
                color: {THEME["vermilion"]};
            }}
        """)
        self.btn_clear_source.clicked.connect(self.clear_texts)

        src_header.addWidget(make_inline_group(src_tag, self.detected_dir_pill, spacing=6))
        src_header.addWidget(make_inline_group(self.btn_unwrap_lines, self.btn_clear_source, spacing=8))
        src_vbox.addLayout(src_header)

        src_rule = QFrame()
        src_rule.setFixedHeight(1)
        src_rule.setStyleSheet(f"background-color: {THEME['surface_muted']}; border: none;")
        src_vbox.addWidget(src_rule)

        self.source_edit = DailySourceTextEdit()
        self.source_edit.setPlaceholderText(i18n.tr("输入或粘贴需要翻译的内容"))
        self.source_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: transparent;
                border: none;
                padding: 4px 0px;
                font-size: 14.5px;
                color: {THEME["ink"]};
                selection-background-color: {THEME["cobalt"]};
                selection-color: #ffffff;
            }}
        """)
        self.source_edit.textChanged.connect(self._on_source_text_changed)
        self.source_edit.submit_requested.connect(self.trigger_translation_now)
        self.source_edit.file_dropped.connect(self._load_dropped_text_file)
        src_vbox.addWidget(self.source_edit, stretch=1)

        self.input_limit_banner = QFrame()
        self.input_limit_banner.setObjectName("DailyInputLimitBanner")
        self.input_limit_banner.setStyleSheet(f"""
            QFrame#DailyInputLimitBanner {{
                background-color: {THEME["vermilion_soft"]};
                border: 1px solid {THEME["vermilion_border"]};
                border-radius: 0px;
            }}
        """)
        limit_vbox = QVBoxLayout(self.input_limit_banner)
        limit_vbox.setContentsMargins(10, 8, 10, 8)
        limit_vbox.setSpacing(6)
        self.input_limit_label = QtLabel("")
        self.input_limit_label.setWordWrap(True)
        self.input_limit_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.input_limit_label.setStyleSheet(
            f"border: none; background: transparent; color: {THEME['vermilion']}; font-size: 11.5px; font-weight: 600;"
        )
        limit_vbox.addWidget(self.input_limit_label)

        limit_btn_row = QHBoxLayout()
        limit_btn_row.setContentsMargins(0, 0, 0, 0)
        limit_btn_row.setSpacing(6)
        self.btn_trim_source = QPushButton(f"保留前 {DAILY_MAX_INPUT_CHARS} 字")
        self.btn_trim_source.setCursor(Qt.PointingHandCursor)
        self.btn_trim_source.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["surface"]};
                color: {THEME["vermilion"]};
                border: 1px solid {THEME["vermilion_border"]};
                border-radius: 0px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["vermilion"]};
                border-color: {THEME["vermilion"]};
                color: #ffffff;
            }}
        """)
        self.btn_trim_source.clicked.connect(self.trim_source_to_limit)
        limit_btn_row.addWidget(self.btn_trim_source)
        limit_btn_row.addStretch(1)
        limit_vbox.addLayout(limit_btn_row)
        self.input_limit_banner.setVisible(False)
        src_vbox.addWidget(self.input_limit_banner)

        src_footer = FlowLayout(margin=0, h_spacing=10, v_spacing=6, justify_last_on_first_row=True)
        self.char_count_label = QtLabel(i18n.tr(f"0 / {DAILY_MAX_INPUT_CHARS} 字"))
        self.char_count_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.char_count_label.setStyleSheet(f"color: {THEME['ink_muted']}; font-size: 11px; font-weight: 600;")
        self.char_count_label.setToolTip(i18n.tr(f"单次最多支持 {DAILY_MAX_INPUT_CHARS} 字，超出请分段翻译"))

        self.chk_auto_translate = QCheckBox("自动翻译")
        self.chk_auto_translate.setChecked(True)
        self.chk_auto_translate.toggled.connect(lambda: self._save_settings())

        # 隐藏“监听剪贴板”且强制非勾选状态
        self.chk_clipboard_watch = QCheckBox("监听剪贴板", self)
        self.chk_clipboard_watch.setChecked(False)
        self.chk_clipboard_watch.setVisible(False)

        self.btn_translate = QPushButton("翻译")
        self.btn_translate.setCursor(Qt.PointingHandCursor)
        self.btn_translate.setMinimumWidth(88)
        self.btn_translate.setStyleSheet(self._translate_btn_idle_style())
        self.btn_translate.clicked.connect(self._on_translate_or_stop_clicked)

        src_footer.addWidget(self.char_count_label)
        src_footer.addWidget(self.chk_auto_translate)
        src_footer.addWidget(self.btn_translate)
        src_vbox.addLayout(src_footer)

        # [右栏] 译文输出卡片（清晰百分比进度 + 静默排版）
        target_card = QFrame()
        target_card.setObjectName("DailyTargetCard")
        target_card.setStyleSheet(f"""
            #DailyTargetCard {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-top: 2px solid {THEME["cobalt"]};
                border-radius: 0px;
            }}
        """)
        tgt_vbox = QVBoxLayout(target_card)
        tgt_vbox.setContentsMargins(18, 14, 18, 14)
        tgt_vbox.setSpacing(10)

        self.tgt_header = FlowLayout(margin=0, h_spacing=8, v_spacing=6)
        tgt_tag = QLabel("译文")
        tgt_tag.setStyleSheet(f"background-color: {THEME['cobalt_soft']}; color: {THEME['cobalt']}; border: 1px solid {THEME['cobalt_border']}; font-size: 11px; font-weight: 700; padding: 2px 8px;")

        self.metrics_pill = QtLabel("")
        self.metrics_pill.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.metrics_pill.setStyleSheet(f"background-color: transparent; color: {THEME['ink_muted']}; font-size: 11px; font-weight: 600; padding: 2px 4px;")
        self.metrics_pill.setVisible(False)

        self.glossary_hit_btn = QtLabel("")
        self.glossary_hit_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.glossary_hit_btn.setStyleSheet(f"background-color: {THEME['sage_soft']}; color: {THEME['sage']}; border: 1px solid {THEME['sage_border']}; font-size: 11px; font-weight: 700; padding: 2px 8px;")
        self.glossary_hit_btn.setVisible(False)

        self.btn_scroll_latest = QPushButton("↓ 回到最新")
        self.btn_scroll_latest.setCursor(Qt.PointingHandCursor)
        self.btn_scroll_latest.setToolTip("已暂停自动跟随，点击回到最新输出")
        self.btn_scroll_latest.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: 1px solid {THEME["ink"]};
                border-radius: 0px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
                border-color: {THEME["cobalt"]};
            }}
        """)
        self.btn_scroll_latest.setVisible(False)
        self.btn_scroll_latest.clicked.connect(self.scroll_target_to_latest)

        self.tgt_header.addWidget(tgt_tag)
        self.tgt_header.addWidget(self.metrics_pill)
        self.tgt_header.addWidget(self.glossary_hit_btn)
        self.tgt_header.addWidget(self.btn_scroll_latest)
        tgt_vbox.addLayout(self.tgt_header)

        self.tgt_rule = QFrame()
        self.tgt_rule.setFixedHeight(1)
        self.tgt_rule.setStyleSheet(f"background-color: {THEME['surface_muted']}; border: none;")
        tgt_vbox.addWidget(self.tgt_rule)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
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
        tgt_vbox.addWidget(self.progress_bar)

        self.target_edit = DailyTargetTextEdit()
        self.target_edit.setPlaceholderText(i18n.tr("译文将显示在这里"))
        self.target_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: transparent;
                border: none;
                padding: 4px 0px;
                font-size: 14.5px;
                font-weight: 500;
                color: {THEME["ink"]};
                selection-background-color: {THEME["ink"]};
                selection-color: #ffffff;
            }}
        """)
        self.target_edit.user_scrolled_up.connect(self._on_target_user_scrolled_up)
        self.target_edit.textChanged.connect(self._on_target_text_changed)
        tgt_vbar = self.target_edit.verticalScrollBar()
        tgt_vbar.sliderPressed.connect(self._on_target_slider_pressed)
        tgt_vbar.sliderReleased.connect(self._on_target_slider_released)
        tgt_vbar.actionTriggered.connect(self._on_target_scroll_action_triggered)
        tgt_vbar.valueChanged.connect(self._on_target_scroll_value_changed)
        tgt_vbar.rangeChanged.connect(self._on_target_scroll_range_changed)
        tgt_vbox.addWidget(self.target_edit, stretch=1)

        self.output_notice_banner = QtLabel("")
        self.output_notice_banner.setWordWrap(True)
        self.output_notice_banner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.output_notice_banner.setStyleSheet(f"""
            background-color: {THEME["ochre_soft"]};
            color: {THEME["ochre"]};
            border: 1px solid {THEME["ochre_border"]};
            border-radius: 0px;
            padding: 7px 10px;
            font-size: 11.5px;
            font-weight: 600;
        """)
        self.output_notice_banner.setVisible(False)
        tgt_vbox.addWidget(self.output_notice_banner)

        tgt_footer = FlowLayout(margin=0, h_spacing=8, v_spacing=6, justify_last_on_first_row=True)

        # 隐藏“自动复制”且强制非勾选状态
        self.chk_auto_copy = QCheckBox("自动复制", self)
        self.chk_auto_copy.setChecked(False)
        self.chk_auto_copy.setVisible(False)

        self.target_char_count_label = QtLabel("")
        self.target_char_count_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.target_char_count_label.setStyleSheet(f"color: {THEME['ink_muted']}; font-size: 11px; font-weight: 600;")
        self.target_char_count_label.setVisible(False)

        self.btn_add_term_from_selection = QPushButton("+ 设为术语")
        self.btn_add_term_from_selection.setCursor(Qt.PointingHandCursor)
        self.btn_add_term_from_selection.setStyleSheet(ghost_link_style)
        self.btn_add_term_from_selection.clicked.connect(self.quick_add_term_from_workspace)

        self.btn_copy_output = QPushButton("复制译文")
        self.btn_copy_output.setCursor(Qt.PointingHandCursor)
        self.btn_copy_output.setMinimumWidth(88)
        self.btn_copy_output.setStyleSheet(self._copy_btn_default_style())
        self.btn_copy_output.clicked.connect(self.copy_output_text)

        tgt_footer.addWidget(self.target_char_count_label)
        tgt_footer.addWidget(self.btn_add_term_from_selection)
        tgt_footer.addWidget(self.btn_copy_output)
        tgt_vbox.addLayout(tgt_footer)

        self.panes_splitter.addWidget(source_card)
        self.panes_splitter.addWidget(target_card)
        self.panes_splitter.setSizes([500, 500])

        # [右侧可折叠抽屉] 术语表 & 历史记录
        self.side_drawer = self._build_side_drawer()
        self.side_drawer.setVisible(False)

        self.main_splitter.addWidget(self.panes_splitter)
        self.main_splitter.addWidget(self.side_drawer)
        self.main_splitter.setSizes([820, 340])
        root.addWidget(self.main_splitter, stretch=1)

        # -------------------------------------------------------------------
        # 3. 底部最简克制的官方状态栏
        # -------------------------------------------------------------------
        status_bar = QWidget()
        status_bar.setObjectName("DailyStatusBar")
        status_bar.setStyleSheet("#DailyStatusBar { background: transparent; border: none; }")
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(2, 2, 2, 0)
        sb_layout.setSpacing(8)

        self.engine_status_dot = QFrame()
        self.engine_status_dot.setFixedSize(6, 6)
        sb_layout.addWidget(self.engine_status_dot)

        self.engine_status_label = QLabel("离线翻译 · 待命")
        self.engine_status_label.setStyleSheet(f"border: none; font-size: 11.5px; font-weight: 500; color: {THEME['ink_muted']};")
        sb_layout.addWidget(self.engine_status_label, stretch=1)

        # 隐藏“释放模型内存”按钮（改由后台空闲定时自动释放）
        self.btn_unload_model = QPushButton("释放模型内存", self)
        self.btn_unload_model.setVisible(False)
        self.btn_unload_model.clicked.connect(self.unload_model)

        root.addWidget(status_bar)

    def _build_side_drawer(self) -> QFrame:
        drawer = QFrame()
        drawer.setObjectName("DailySideDrawer")
        drawer.setMinimumWidth(300)
        drawer.setStyleSheet(f"""
            #DailySideDrawer {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
            }}
        """)
        vbox = QVBoxLayout(drawer)
        vbox.setContentsMargins(14, 12, 14, 12)
        vbox.setSpacing(10)

        # 抽屉页签切换栏
        tab_row = QHBoxLayout()
        tab_row.setSpacing(0)
        tab_style = f"""
            QPushButton {{
                background-color: {THEME["surface_soft"]};
                border: 1px solid {THEME["border"]};
                border-radius: 0px;
                padding: 5px 10px;
                font-size: 12px;
                font-weight: 700;
                color: {THEME["ink_secondary"]};
            }}
            QPushButton:checked {{
                background-color: {THEME["ink"]};
                border-color: {THEME["ink"]};
                color: #ffffff;
            }}
        """
        self.drawer_tab_glossary = QPushButton("术语表")
        self.drawer_tab_glossary.setCheckable(True)
        self.drawer_tab_glossary.setCursor(Qt.PointingHandCursor)
        self.drawer_tab_glossary.setStyleSheet(tab_style)
        self.drawer_tab_glossary.clicked.connect(lambda: self._switch_drawer_tab(0))

        self.drawer_tab_history = QPushButton("历史记录")
        self.drawer_tab_history.setCheckable(True)
        self.drawer_tab_history.setCursor(Qt.PointingHandCursor)
        self.drawer_tab_history.setStyleSheet(tab_style)
        self.drawer_tab_history.clicked.connect(lambda: self._switch_drawer_tab(1))

        self.btn_close_drawer = QPushButton("×")
        self.btn_close_drawer.setFixedSize(28, 28)
        self.btn_close_drawer.setCursor(Qt.PointingHandCursor)
        self.btn_close_drawer.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                color: {THEME["ink_muted"]};
                font-size: 16px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["vermilion_soft"]};
                color: {THEME["vermilion"]};
            }}
        """)
        self.btn_close_drawer.clicked.connect(self._close_drawer)

        tab_row.addWidget(self.drawer_tab_glossary, stretch=1)
        tab_row.addWidget(self.drawer_tab_history, stretch=1)
        tab_row.addSpacing(6)
        tab_row.addWidget(self.btn_close_drawer)
        vbox.addLayout(tab_row)

        self.drawer_stack = QStackedWidget()

        # --- Page 0: 术语表 ---
        glossary_page = QWidget()
        g_layout = QVBoxLayout(glossary_page)
        g_layout.setContentsMargins(0, 4, 0, 0)
        g_layout.setSpacing(8)

        self.chk_use_glossary = QCheckBox("翻译时启用术语表")
        self.chk_use_glossary.setChecked(True)
        self.chk_use_glossary.toggled.connect(lambda: self._save_settings())
        g_layout.addWidget(self.chk_use_glossary)

        input_style = f"""
            QLineEdit {{
                background-color: {THEME["surface"]};
                border: 1px solid {THEME["border_strong"]};
                border-radius: 0px;
                padding: 5px 8px;
                font-size: 12px;
                color: {THEME["ink"]};
            }}
            QLineEdit:focus {{
                border-color: {THEME["cobalt"]};
            }}
        """
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.glossary_src_input = QLineEdit()
        self.glossary_src_input.setPlaceholderText(i18n.tr("原文词汇"))
        self.glossary_src_input.setStyleSheet(input_style)

        self.glossary_tgt_input = QLineEdit()
        self.glossary_tgt_input.setPlaceholderText(i18n.tr("指定译文"))
        self.glossary_tgt_input.setStyleSheet(input_style)
        self.glossary_tgt_input.returnPressed.connect(self.on_add_glossary_term)

        self.btn_add_glossary = QPushButton("添加")
        self.btn_add_glossary.setCursor(Qt.PointingHandCursor)
        self.btn_add_glossary.setStyleSheet(f"""
            QPushButton {{
                background-color: {THEME["ink"]};
                color: #ffffff;
                border: none;
                padding: 6px 12px;
                font-size: 11px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {THEME["cobalt"]};
            }}
        """)
        self.btn_add_glossary.clicked.connect(self.on_add_glossary_term)

        add_row.addWidget(self.glossary_src_input, stretch=1)
        add_row.addWidget(self.glossary_tgt_input, stretch=1)
        add_row.addWidget(self.btn_add_glossary)
        g_layout.addLayout(add_row)

        self.glossary_search_input = QLineEdit()
        self.glossary_search_input.setPlaceholderText(i18n.tr("搜索术语..."))
        self.glossary_search_input.setStyleSheet(input_style)
        self.glossary_search_input.textChanged.connect(self._refresh_glossary_list)
        g_layout.addWidget(self.glossary_search_input)

        self.glossary_scroll = QScrollArea()
        self.glossary_scroll.setWidgetResizable(True)
        self.glossary_scroll.setFrameShape(QFrame.NoFrame)
        self.glossary_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.glossary_list_widget = QWidget()
        self.glossary_list_widget.setStyleSheet("background: transparent;")
        self.glossary_list_layout = QVBoxLayout(self.glossary_list_widget)
        self.glossary_list_layout.setContentsMargins(0, 0, 4, 0)
        self.glossary_list_layout.setSpacing(6)
        self.glossary_list_layout.addStretch(1)
        self.glossary_scroll.setWidget(self.glossary_list_widget)
        g_layout.addWidget(self.glossary_scroll, stretch=1)

        g_footer = FlowLayout(margin=0, h_spacing=6, v_spacing=6, justify_last_on_first_row=True)
        small_btn = f"""
            QPushButton {{
                background-color: {THEME["surface_soft"]};
                border: 1px solid {THEME["border"]};
                color: {THEME["ink_secondary"]};
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {THEME["ink"]};
                color: {THEME["ink"]};
            }}
        """
        self.btn_import_glossary = QPushButton("导入")
        self.btn_import_glossary.setCursor(Qt.PointingHandCursor)
        self.btn_import_glossary.setStyleSheet(small_btn)
        self.btn_import_glossary.clicked.connect(self.on_import_glossary)

        self.btn_export_glossary = QPushButton("导出")
        self.btn_export_glossary.setCursor(Qt.PointingHandCursor)
        self.btn_export_glossary.setStyleSheet(small_btn)
        self.btn_export_glossary.clicked.connect(self.on_export_glossary)

        self.btn_clear_glossary = QPushButton("清空")
        self.btn_clear_glossary.setCursor(Qt.PointingHandCursor)
        self.btn_clear_glossary.setStyleSheet(small_btn)
        self.btn_clear_glossary.clicked.connect(self.on_clear_glossary)

        g_footer.addWidget(make_inline_group(self.btn_import_glossary, self.btn_export_glossary, spacing=6))
        g_footer.addWidget(self.btn_clear_glossary)
        g_layout.addLayout(g_footer)

        # --- Page 1: 历史记录 ---
        history_page = QWidget()
        h_layout = QVBoxLayout(history_page)
        h_layout.setContentsMargins(0, 4, 0, 0)
        h_layout.setSpacing(8)

        self.history_search_input = QLineEdit()
        self.history_search_input.setPlaceholderText(i18n.tr("搜索历史..."))
        self.history_search_input.setStyleSheet(input_style)
        self.history_search_input.textChanged.connect(self._refresh_history_list)
        h_layout.addWidget(self.history_search_input)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setFrameShape(QFrame.NoFrame)
        self.history_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.history_list_widget = QWidget()
        self.history_list_layout = QVBoxLayout(self.history_list_widget)
        self.history_list_layout.setContentsMargins(0, 0, 2, 0)
        self.history_list_layout.setSpacing(6)
        self.history_list_layout.addStretch(1)
        self.history_scroll.setWidget(self.history_list_widget)
        h_layout.addWidget(self.history_scroll, stretch=1)

        h_footer = QHBoxLayout()
        h_footer.addStretch(1)
        self.btn_clear_history = QPushButton("清空历史")
        self.btn_clear_history.setCursor(Qt.PointingHandCursor)
        self.btn_clear_history.setStyleSheet(small_btn)
        self.btn_clear_history.clicked.connect(self.on_clear_history)
        h_footer.addWidget(self.btn_clear_history)
        h_layout.addLayout(h_footer)

        self.drawer_stack.addWidget(glossary_page)
        self.drawer_stack.addWidget(history_page)
        vbox.addWidget(self.drawer_stack, stretch=1)
        return drawer

    # -----------------------------------------------------------------------
    # 国际化刷新与设置持久化
    # -----------------------------------------------------------------------

    def retranslate(self) -> None:
        self.src_combo.setItemText(0, i18n.tr("自动识别"))
        self.tgt_combo.setItemText(0, i18n.tr("自动互译 (中 ⇄ 英)"))
        for idx, (label, _key) in enumerate(STYLE_OPTIONS):
            self.style_combo.setItemText(idx, i18n.tr(label))
        for combo in (self.src_combo, self.tgt_combo, self.style_combo):
            combo.updateGeometry()
        self.source_edit.setPlaceholderText(i18n.tr("输入或粘贴需要翻译的内容"))
        self.target_edit.setPlaceholderText(i18n.tr("译文将显示在这里"))
        self.glossary_src_input.setPlaceholderText(i18n.tr("原文词汇"))
        self.glossary_tgt_input.setPlaceholderText(i18n.tr("指定译文"))
        self.glossary_search_input.setPlaceholderText(i18n.tr("搜索术语..."))
        self.history_search_input.setPlaceholderText(i18n.tr("搜索历史..."))
        self.btn_trim_source.retranslate()
        self.btn_scroll_latest.retranslate()
        self._update_source_count_ui()
        self._update_target_count_ui()
        self._update_output_notice_ui()
        if self._last_matched_terms:
            self.glossary_hit_btn.setText(i18n.tr(f"{len(self._last_matched_terms)} 术语"))
        self._update_glossary_button_badge()
        self._update_direction_preview()
        self._render_translation_status()
        self._update_scroll_latest_button()
        self._update_engine_status_ui(self._engine_ui_state)
        self._refresh_glossary_list()
        self._refresh_history_list()

    def _settings_path(self) -> Path:
        return user_data_dir() / "daily_settings.json"

    def _save_settings(self) -> None:
        if self._restoring_settings:
            return
        try:
            payload = {
                "version": 1,
                "source_lang": self.src_combo.currentData(),
                "target_lang": self.tgt_combo.currentData(),
                "style": self.style_combo.currentData(),
                "auto_translate": self.chk_auto_translate.isChecked(),
                "clipboard_watch": False,
                "auto_copy": False,
                "use_glossary": self.chk_use_glossary.isChecked(),
            }
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.exception("保存日常翻译配置失败")

    def _restore_settings(self) -> None:
        try:
            payload = json.loads(self._settings_path().read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
        except (OSError, ValueError):
            return

        self._restoring_settings = True
        try:
            idx = self.src_combo.findData(payload.get("source_lang"))
            if idx >= 0:
                self.src_combo.setCurrentIndex(idx)
            idx = self.tgt_combo.findData(payload.get("target_lang"))
            if idx >= 0:
                self.tgt_combo.setCurrentIndex(idx)
            idx = self.style_combo.findData(payload.get("style"))
            if idx >= 0:
                self.style_combo.setCurrentIndex(idx)
            if isinstance(payload.get("auto_translate"), bool):
                self.chk_auto_translate.setChecked(payload["auto_translate"])
            self.chk_clipboard_watch.setChecked(False)
            self.chk_auto_copy.setChecked(False)
            if isinstance(payload.get("use_glossary"), bool):
                self.chk_use_glossary.setChecked(payload["use_glossary"])
        finally:
            self._restoring_settings = False

    # -----------------------------------------------------------------------
    # 抽屉控制 & PDF 协同保护
    # -----------------------------------------------------------------------

    def _toggle_drawer(self, tab_idx: int) -> None:
        if self.side_drawer.isVisible() and self.drawer_stack.currentIndex() == tab_idx:
            self._close_drawer()
            return
        self.side_drawer.setVisible(True)
        self._switch_drawer_tab(tab_idx)
        win = self.window()
        if win and not win.isMaximized() and win.width() < 1180:
            win.resize(1220, win.height())
            self.main_splitter.setSizes([860, 340])

    def _switch_drawer_tab(self, tab_idx: int) -> None:
        self.drawer_stack.setCurrentIndex(tab_idx)
        self.drawer_tab_glossary.setChecked(tab_idx == 0)
        self.drawer_tab_history.setChecked(tab_idx == 1)
        self.btn_toggle_glossary.setChecked(self.side_drawer.isVisible() and tab_idx == 0)
        self.btn_toggle_history.setChecked(self.side_drawer.isVisible() and tab_idx == 1)

    def _close_drawer(self) -> None:
        self.side_drawer.setVisible(False)
        self.btn_toggle_glossary.setChecked(False)
        self.btn_toggle_history.setChecked(False)

    def sync_pdf_busy_state(self) -> None:
        busy = bool(self._is_pdf_busy_provider and self._is_pdf_busy_provider())
        self.pdf_busy_banner.setVisible(busy)
        self._update_translate_button_enabled()

    def _update_translate_button_enabled(self) -> None:
        pdf_busy = bool(self._is_pdf_busy_provider and self._is_pdf_busy_provider())
        is_running = bool(self._worker and self._worker.isRunning())
        over_limit = self._source_char_count > DAILY_MAX_INPUT_CHARS
        self.btn_translate.setEnabled((not pdf_busy) and (is_running or not over_limit))

    def _on_idle_unload_timeout(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.runtime_manager.stop()
        self._update_engine_status_ui()

    def shutdown_runtime(self) -> None:
        """关闭窗口或启动 PDF 批量任务前，优雅中止日常翻译线程并释放常驻推理服务。"""
        self._debounce_timer.stop()
        self._idle_unload_timer.stop()
        self._pending_request = None
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(3000)
        self.runtime_manager.stop()
        self._update_engine_status_ui()

    def unload_model(self) -> None:
        self._idle_unload_timer.stop()
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(2000)
        self.runtime_manager.stop()
        self._update_engine_status_ui()

    def _update_engine_status_ui(self, state: str = "") -> None:
        self._engine_ui_state = state
        if state == "waking":
            self.engine_status_dot.setStyleSheet(f"background-color: {THEME['cobalt']}; border-radius: 3px;")
            self.engine_status_label.setText("正在准备翻译服务...")
        elif self.runtime_manager.is_running():
            self.engine_status_dot.setStyleSheet(f"background-color: {THEME['sage']}; border-radius: 3px;")
            self.engine_status_label.setText("离线翻译 · 已就绪")
        else:
            self.engine_status_dot.setStyleSheet(f"background-color: {THEME['border_strong']}; border-radius: 3px;")
            self.engine_status_label.setText("离线翻译 · 待命")
        self.btn_unload_model.setVisible(False)

    # -----------------------------------------------------------------------
    # 语言方向、输入处理与翻译触发
    # -----------------------------------------------------------------------

    def _update_direction_preview(self) -> None:
        sample = self.source_edit.toPlainText()
        src_sel = self.src_combo.currentData() or "auto"
        tgt_sel = self.tgt_combo.currentData() or AUTO_BILINGUAL_TARGET
        detected_src, resolved_tgt = resolve_daily_direction(sample, src_sel, tgt_sel)
        self._last_detected_src = detected_src
        self._last_resolved_tgt = resolved_tgt
        if not sample.strip() and src_sel == "auto":
            self.detected_dir_pill.setText("")
            self.detected_dir_pill.setVisible(False)
            return
        ui_zh = i18n._language == "zh"
        src_name = language_display_name(detected_src, ui_zh=ui_zh)
        tgt_name = language_display_name(resolved_tgt, ui_zh=ui_zh)
        self.detected_dir_pill.setText(f"{src_name} → {tgt_name}")
        self.detected_dir_pill.setVisible(True)

    def _on_language_or_style_changed(self) -> None:
        self._update_direction_preview()
        if not self._restoring_settings:
            self._save_settings()
            if self.source_edit.toPlainText().strip() and self.chk_auto_translate.isChecked():
                self.trigger_translation_now()

    def on_swap_languages(self) -> None:
        src_data = self.src_combo.currentData()
        tgt_data = self.tgt_combo.currentData()
        if tgt_data == AUTO_BILINGUAL_TARGET:
            # 将当前自动判定结果显式反转为相反方向
            new_src = self._last_resolved_tgt
            new_tgt = self._last_detected_src
        else:
            new_src = tgt_data if src_data == "auto" else tgt_data
            new_tgt = self._last_detected_src if src_data == "auto" else src_data

        src_idx = self.src_combo.findData(new_src)
        tgt_idx = self.tgt_combo.findData(new_tgt)
        self._restoring_settings = True
        try:
            if src_idx >= 0:
                self.src_combo.setCurrentIndex(src_idx)
            if tgt_idx >= 0:
                self.tgt_combo.setCurrentIndex(tgt_idx)
        finally:
            self._restoring_settings = False

        # 若译文框已有内容，顺便将译文回填到原文框，形成连续往返润色/互译体验
        out_txt = self.target_edit.toPlainText().strip()
        if out_txt:
            self._suppress_auto_translate = True
            self.source_edit.setPlainText(out_txt)
            self._suppress_auto_translate = False

        self._save_settings()
        self._update_direction_preview()
        if self.source_edit.toPlainText().strip():
            self.trigger_translation_now()

    def _render_translation_status(self) -> None:
        mode = self._status_mode
        if not mode:
            self.metrics_pill.setText("")
            self.metrics_pill.setVisible(False)
            self.progress_bar.setVisible(False)
            self.tgt_rule.setVisible(True)
            return

        if mode == "waking":
            self.metrics_pill.setText(i18n.tr("准备中..."))
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['cobalt']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.tgt_rule.setVisible(False)
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setVisible(True)
        elif mode == "translating":
            pct = max(0, min(99, int(self._status_pct)))
            txt = i18n.tr(f"翻译输出中 {pct}%") if pct > 0 else i18n.tr("翻译输出中...")
            self.metrics_pill.setText(txt)
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['cobalt']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.tgt_rule.setVisible(False)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setVisible(True)
        elif mode == "completed":
            self.metrics_pill.setText(i18n.tr("翻译完成"))
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['sage']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.progress_bar.setVisible(False)
            self.tgt_rule.setVisible(True)
        elif mode == "truncated":
            self.metrics_pill.setText(i18n.tr("输出可能不完整"))
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['ochre']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.progress_bar.setVisible(False)
            self.tgt_rule.setVisible(True)
        elif mode == "stopped":
            self.metrics_pill.setText(i18n.tr("已停止"))
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['ochre']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.progress_bar.setVisible(False)
            self.tgt_rule.setVisible(True)
        elif mode == "failed":
            self.metrics_pill.setText(i18n.tr("翻译失败"))
            self.metrics_pill.setStyleSheet(
                f"background-color: transparent; color: {THEME['vermilion']}; font-size: 11px; font-weight: 600; padding: 2px 4px;"
            )
            self.metrics_pill.setVisible(True)
            self.progress_bar.setVisible(False)
            self.tgt_rule.setVisible(True)
        self.metrics_pill.updateGeometry()
        self.tgt_header.invalidate()
        self._update_target_count_ui()

    def _update_source_count_ui(self) -> None:
        count = self._source_char_count
        self.char_count_label.setText(i18n.tr(f"{count} / {DAILY_MAX_INPUT_CHARS} 字"))
        self.char_count_label.setToolTip(i18n.tr(f"单次最多支持 {DAILY_MAX_INPUT_CHARS} 字，超出请分段翻译"))
        if count > DAILY_MAX_INPUT_CHARS:
            over = count - DAILY_MAX_INPUT_CHARS
            self.char_count_label.setStyleSheet(
                f"color: {THEME['vermilion']}; font-size: 11px; font-weight: 700;"
            )
            self.input_limit_label.setText(
                i18n.tr(f"已超出 {over} 字（单次上限 {DAILY_MAX_INPUT_CHARS} 字），请精简或分段翻译")
            )
            self.btn_trim_source.setText(f"保留前 {DAILY_MAX_INPUT_CHARS} 字")
            self.input_limit_banner.setVisible(True)
        else:
            color = (
                THEME["ochre"]
                if count >= int(DAILY_MAX_INPUT_CHARS * 0.85)
                else THEME["ink_muted"]
            )
            self.char_count_label.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
            self.input_limit_banner.setVisible(False)

    def _on_target_text_changed(self) -> None:
        self._target_char_count = len(self.target_edit.toPlainText())
        self._update_target_count_ui()

    def _update_target_count_ui(self) -> None:
        if self._target_char_count <= 0:
            self.target_char_count_label.setText("")
            self.target_char_count_label.setVisible(False)
            return
        self.target_char_count_label.setText(i18n.tr(f"{self._target_char_count} 字"))
        self.target_char_count_label.setToolTip(i18n.tr(f"单次最多输出 {DAILY_MAX_OUTPUT_CHARS} 字"))
        if self._target_char_count >= DAILY_MAX_OUTPUT_CHARS:
            color = THEME["vermilion"]
        elif self._target_char_count >= int(DAILY_MAX_OUTPUT_CHARS * 0.85) or self._status_mode == "truncated":
            color = THEME["ochre"]
        else:
            color = THEME["ink_muted"]
        self.target_char_count_label.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 600;")
        self.target_char_count_label.setVisible(True)

    def _set_output_notice(self, notice_key: str) -> None:
        self._output_notice_key = notice_key or ""
        self._update_output_notice_ui()

    def _update_output_notice_ui(self) -> None:
        notice_map = {
            "length": "部分内容受模型单次长度限制未能完整输出，建议缩短原文或分段翻译。",
            "output_limit": "译文已达单次输出上限，后续内容已停止生成，建议分段翻译。",
            "stopped_incomplete": "翻译已手动停止，当前仅显示已完成部分，并非完整译文。",
        }
        msg_key = notice_map.get(self._output_notice_key, "")
        if not msg_key:
            self.output_notice_banner.setText("")
            self.output_notice_banner.setVisible(False)
            return
        self.output_notice_banner.setText(i18n.tr(msg_key))
        self.output_notice_banner.setVisible(True)

    def _is_target_at_bottom(self) -> bool:
        vbar = self.target_edit.verticalScrollBar()
        return vbar.maximum() <= 0 or (vbar.maximum() - vbar.value()) <= 1

    def _update_scroll_latest_button(self) -> None:
        vbar = self.target_edit.verticalScrollBar()
        should_show = (
            self._target_streaming
            and (not self._target_auto_scroll)
            and vbar.maximum() > 0
        )
        self.btn_scroll_latest.setVisible(should_show)
        self.tgt_header.invalidate()

    def _on_target_user_scrolled_up(self) -> None:
        if self._target_updating_text:
            return
        if self._target_streaming and self.target_edit.verticalScrollBar().maximum() > 0:
            self._target_auto_scroll = False
            self._update_scroll_latest_button()

    def _on_target_slider_pressed(self) -> None:
        if self._target_streaming:
            self._target_auto_scroll = False
            self._update_scroll_latest_button()

    def _on_target_slider_released(self) -> None:
        self._target_auto_scroll = self._is_target_at_bottom()
        self._update_scroll_latest_button()

    def _on_target_scroll_action_triggered(self, action: int) -> None:
        if self._target_updating_text:
            return
        if action in (
            QAbstractSlider.SliderSingleStepSub,
            QAbstractSlider.SliderPageStepSub,
            QAbstractSlider.SliderToMinimum,
        ):
            if self._target_streaming and self.target_edit.verticalScrollBar().maximum() > 0:
                self._target_auto_scroll = False
                self._update_scroll_latest_button()

    def _on_target_scroll_value_changed(self, _val: int) -> None:
        if self._target_updating_text:
            return
        vbar = self.target_edit.verticalScrollBar()
        if vbar.isSliderDown():
            self._target_auto_scroll = False
            self._update_scroll_latest_button()
            return
        self._target_auto_scroll = self._is_target_at_bottom()
        self._update_scroll_latest_button()

    def _on_target_scroll_range_changed(self, _min_val: int, max_val: int) -> None:
        if self._target_updating_text:
            return
        vbar = self.target_edit.verticalScrollBar()
        if self._target_streaming and self._target_auto_scroll and not vbar.isSliderDown():
            self._target_updating_text = True
            try:
                vbar.setValue(max_val)
            finally:
                self._target_updating_text = False
        self._update_scroll_latest_button()

    def scroll_target_to_latest(self) -> None:
        """恢复流式输出自动跟随并将译文区滚动到最新底部。"""
        self._target_auto_scroll = True
        vbar = self.target_edit.verticalScrollBar()
        self._target_updating_text = True
        try:
            vbar.setValue(vbar.maximum())
        finally:
            self._target_updating_text = False
        self._update_scroll_latest_button()

    def _apply_target_text(
        self,
        new_text: str,
        *,
        follow_tail: Optional[bool] = None,
        streaming: bool = False,
    ) -> None:
        """增量更新译文文本，流式追加时保持用户主动滚屏位置与选区不被拉回顶部。"""
        doc = self.target_edit.document()
        vbar = self.target_edit.verticalScrollBar()
        prev_val = vbar.value()

        if follow_tail is None:
            should_follow = self._target_auto_scroll and not vbar.isSliderDown()
        else:
            should_follow = bool(follow_tail) and not vbar.isSliderDown()

        current_text = self.target_edit.toPlainText()
        self._target_updating_text = True
        try:
            doc.setUndoRedoEnabled(not streaming)
            if current_text == new_text:
                pass
            elif not current_text or not new_text:
                self.target_edit.setPlainText(new_text)
            elif new_text.startswith(current_text):
                cur = QTextCursor(doc)
                cur.movePosition(QTextCursor.End)
                cur.insertText(new_text[len(current_text):])
            else:
                max_prefix = min(len(current_text), len(new_text))
                prefix_len = 0
                while prefix_len < max_prefix and current_text[prefix_len] == new_text[prefix_len]:
                    prefix_len += 1
                if prefix_len == 0:
                    self.target_edit.setPlainText(new_text)
                else:
                    cur = QTextCursor(doc)
                    cur.setPosition(_utf16_len(current_text[:prefix_len]))
                    cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
                    cur.insertText(new_text[prefix_len:])

            if should_follow:
                vbar.setValue(vbar.maximum())
            else:
                vbar.setValue(min(prev_val, vbar.maximum()))
        finally:
            self._target_updating_text = False
            self._update_scroll_latest_button()

    def trim_source_to_limit(self) -> None:
        raw = self.source_edit.toPlainText()
        if len(raw) <= DAILY_MAX_INPUT_CHARS:
            return
        trimmed = raw[:DAILY_MAX_INPUT_CHARS]
        self._suppress_auto_translate = True
        self.source_edit.setPlainText(trimmed)
        self._suppress_auto_translate = False
        if trimmed.strip() and self.chk_auto_translate.isChecked():
            self.trigger_translation_now()

    def _on_source_text_changed(self) -> None:
        txt = self.source_edit.toPlainText()
        self._source_char_count = len(txt)
        self._update_source_count_ui()
        self._update_direction_preview()
        self._update_translate_button_enabled()
        if self._suppress_auto_translate:
            return
        if not txt.strip():
            self._debounce_timer.stop()
            self._cancel_active_worker()
            self._target_streaming = False
            self._target_auto_scroll = True
            self._apply_target_text("", follow_tail=False, streaming=False)
            self._set_output_notice("")
            self._status_mode = ""
            self._status_pct = 0
            self._render_translation_status()
            self.glossary_hit_btn.setVisible(False)
            return
        if self._source_char_count > DAILY_MAX_INPUT_CHARS:
            self._debounce_timer.stop()
            self._cancel_active_worker()
            return
        if self.chk_auto_translate.isChecked():
            self._debounce_timer.start()

    def _on_debounce_trigger(self) -> None:
        txt = self.source_edit.toPlainText()
        if txt.strip() and len(txt) <= DAILY_MAX_INPUT_CHARS:
            self.trigger_translation_now()

    def _on_translate_or_stop_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self._pending_request = None
            self._worker.request_stop()
            self._target_streaming = False
            self._status_mode = "stopped"
            self._render_translation_status()
            if self.target_edit.toPlainText().strip():
                self._set_output_notice("stopped_incomplete")
            self._update_scroll_latest_button()
            return
        self.trigger_translation_now()

    def _cancel_active_worker(self) -> None:
        self._request_seq += 1
        self._pending_request = None
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()

    def trigger_translation_now(self) -> None:
        self._debounce_timer.stop()
        if self._is_pdf_busy_provider and self._is_pdf_busy_provider():
            self.sync_pdf_busy_state()
            return

        text = self.source_edit.toPlainText()
        if not text.strip():
            return
        if len(text) > DAILY_MAX_INPUT_CHARS:
            self._update_source_count_ui()
            self._update_translate_button_enabled()
            return

        src_lang = self.src_combo.currentData() or "auto"
        tgt_lang = self.tgt_combo.currentData() or AUTO_BILINGUAL_TARGET
        style_key = self.style_combo.currentData() or "default"
        threads, ctx, ngl = (
            self._hw_config_provider() if self._hw_config_provider else (None, None, None)
        )

        self._request_seq += 1
        req_id = self._request_seq
        params = (
            req_id,
            text,
            src_lang,
            tgt_lang,
            style_key,
            list(self.glossary_store.entries),
            self.chk_use_glossary.isChecked(),
            threads,
            ctx,
            ngl,
        )

        if self._worker and self._worker.isRunning():
            self._pending_request = params
            self._worker.request_stop()
            return

        self._launch_worker(*params)

    def _launch_worker(
        self,
        req_id: int,
        text: str,
        src_lang: str,
        tgt_lang: str,
        style_key: str,
        glossary: list[DailyGlossaryEntry],
        use_glossary: bool,
        threads: Optional[int],
        ctx: Optional[int],
        ngl: Optional[int],
    ) -> None:
        self._idle_unload_timer.stop()
        self._target_auto_scroll = True
        self._target_streaming = True
        self._set_output_notice("")
        self.btn_scroll_latest.setVisible(False)
        self.btn_translate.setText("停止")
        self.btn_translate.setStyleSheet(self._translate_btn_stop_style())
        self.btn_translate.setEnabled(True)
        self._status_pct = 0
        self._status_mode = "translating" if self.runtime_manager.is_running() else "waking"
        self._render_translation_status()
        self.glossary_hit_btn.setVisible(False)

        worker = DailyTranslationWorker(
            runtime_manager=self.runtime_manager,
            request_id=req_id,
            text=text,
            source_lang=src_lang,
            target_lang=tgt_lang,
            style_key=style_key,
            glossary=glossary,
            use_glossary=use_glossary,
            threads=threads,
            context_size=ctx,
            n_gpu_layers=ngl,
            parent=self,
        )
        worker.status_changed.connect(self._on_worker_status)
        worker.partial_ready.connect(self._on_worker_partial)
        worker.completed.connect(self._on_worker_completed)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_status(self, req_id: int, state: str) -> None:
        if req_id != self._request_seq:
            return
        self._update_engine_status_ui(state)
        if state == "waking":
            self._status_mode = "waking"
            self._render_translation_status()
        elif state == "translating":
            self._status_mode = "translating"
            self._render_translation_status()

    def _on_worker_partial(self, req_id: int, partial_text: str, stats: dict) -> None:
        if req_id != self._request_seq:
            return
        self._apply_target_text(partial_text, streaming=True)
        pct = stats.get("progress")
        if pct is None:
            total_chunks = max(1, int(stats.get("total_chunks") or 1))
            cur_chunk = max(1, int(stats.get("chunk") or 1))
            pct = min(99, max(1, int(round((cur_chunk / total_chunks) * 100))))
        else:
            pct = min(99, max(1, int(pct)))
        self._status_mode = "translating"
        self._status_pct = max(self._status_pct, pct)
        self._render_translation_status()

    def _on_worker_completed(self, req_id: int, result: DailyTranslationResult) -> None:
        if req_id != self._request_seq:
            return
        self._target_streaming = False
        self._update_engine_status_ui()
        if result.cancelled:
            if result.translated_text:
                self._apply_target_text(result.translated_text, streaming=False)
            else:
                self.target_edit.document().setUndoRedoEnabled(True)
            self._status_mode = "stopped"
            self._render_translation_status()
            if self.target_edit.toPlainText().strip():
                self._set_output_notice("stopped_incomplete")
            else:
                self._set_output_notice("")
            self._update_scroll_latest_button()
            return

        self._apply_target_text(result.translated_text, streaming=False)
        if result.truncated:
            self._status_mode = "truncated"
            self._status_pct = 100
            self._set_output_notice(result.truncation_reason or "length")
        else:
            self._status_mode = "completed"
            self._status_pct = 100
            self._set_output_notice("")
        self._render_translation_status()
        self._update_scroll_latest_button()

        self._last_matched_terms = list(result.matched_terms)
        if result.matched_terms:
            summary = "\n".join(f"{t.source} → {t.target}" for t in result.matched_terms)
            self.glossary_hit_btn.setText(i18n.tr(f"{len(result.matched_terms)} 术语"))
            self.glossary_hit_btn.setToolTip(summary)
            self.glossary_hit_btn.setVisible(True)
        else:
            self.glossary_hit_btn.setVisible(False)

        self.history_store.add(
            source=self.source_edit.toPlainText(),
            target=result.translated_text,
            source_lang=result.detected_source_lang,
            target_lang=result.resolved_target_lang,
            style=self.style_combo.currentData() or "default",
            matched_terms=len(result.matched_terms),
        )
        self._refresh_history_list()

    def _on_worker_failed(self, req_id: int, err: str) -> None:
        if req_id != self._request_seq:
            return
        self._target_streaming = False
        self._target_auto_scroll = True
        self._update_engine_status_ui()
        self._status_mode = "failed"
        self._set_output_notice("")
        self._render_translation_status()
        key = i18n.error_key(err)
        msg = (
            i18n.message(key)
            if key in ("memory", "assets", "timeout", "context_limit", "input_too_long")
            else i18n.tr("翻译未能完成，请重试。")
        )
        self._apply_target_text(msg, follow_tail=False, streaming=False)

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._target_streaming = False
        self.target_edit.document().setUndoRedoEnabled(True)
        if self._pending_request is not None:
            params = self._pending_request
            self._pending_request = None
            if params[0] == self._request_seq:
                self._launch_worker(*params)
                return
        self.btn_translate.setText("翻译")
        self.btn_translate.setStyleSheet(self._translate_btn_idle_style())
        self._update_translate_button_enabled()
        self._update_engine_status_ui()
        self._update_scroll_latest_button()
        if self.runtime_manager.is_running():
            self._idle_unload_timer.start()

    # -----------------------------------------------------------------------
    # 快捷粘贴、整理断行、剪贴板监听与复制
    # -----------------------------------------------------------------------

    def paste_and_translate(self) -> None:
        clip = QApplication.clipboard()
        text = clip.text() if clip else ""
        if not text or not text.strip():
            return
        self._suppress_auto_translate = True
        self.source_edit.setPlainText(text)
        self._suppress_auto_translate = False
        if len(text) <= DAILY_MAX_INPUT_CHARS:
            self.trigger_translation_now()

    def unwrap_source_linebreaks(self) -> None:
        raw = self.source_edit.toPlainText()
        if not raw.strip():
            return
        cleaned = clean_pdf_linebreaks(raw)
        if cleaned != raw:
            self.source_edit.setPlainText(cleaned)
            if self.chk_auto_translate.isChecked() and len(cleaned) <= DAILY_MAX_INPUT_CHARS:
                self.trigger_translation_now()

    def clear_texts(self) -> None:
        self._debounce_timer.stop()
        self._cancel_active_worker()
        self._suppress_auto_translate = True
        self.source_edit.clear()
        self._suppress_auto_translate = False
        self._target_streaming = False
        self._target_auto_scroll = True
        self._apply_target_text("", follow_tail=False, streaming=False)
        self._set_output_notice("")
        self._source_char_count = 0
        self._update_source_count_ui()
        self._update_translate_button_enabled()
        self._status_mode = ""
        self._status_pct = 0
        self._render_translation_status()
        self.glossary_hit_btn.setVisible(False)
        self._update_direction_preview()

    def copy_output_text(self) -> None:
        text = self.target_edit.toPlainText()
        if not text.strip():
            return
        clip = QApplication.clipboard()
        if clip is not None:
            self._last_copied_output = text
            clip.setText(text)
            self.btn_copy_output.setText("✓ 已复制")
            self.btn_copy_output.setStyleSheet(self._copy_btn_copied_style())
            self._copy_flash_timer.start()

    def _restore_copy_button_text(self) -> None:
        self.btn_copy_output.setText("复制译文")
        self.btn_copy_output.setStyleSheet(self._copy_btn_default_style())

    def _on_clipboard_changed(self) -> None:
        return

    def _load_dropped_text_file(self, path_str: str) -> None:
        try:
            content = Path(path_str).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, i18n.tr("执行失败"), str(exc))
            return
        if len(content) > 100000:
            content = content[:100000]
        self._suppress_auto_translate = True
        self.source_edit.setPlainText(content)
        self._suppress_auto_translate = False
        if len(content) <= DAILY_MAX_INPUT_CHARS and content.strip():
            self.trigger_translation_now()

    # -----------------------------------------------------------------------
    # 术语表操作
    # -----------------------------------------------------------------------

    def _update_glossary_button_badge(self) -> None:
        count = len(self.glossary_store.entries)
        base = i18n.tr("术语表")
        self.btn_toggle_glossary.setText(f"{base} ({count})" if count else base)

    def quick_add_term_from_workspace(self) -> None:
        src_sel = self.source_edit.textCursor().selectedText().replace("\u2029", " ").strip()
        tgt_sel = self.target_edit.textCursor().selectedText().replace("\u2029", " ").strip()
        if not src_sel and not tgt_sel:
            raw_src = self.source_edit.toPlainText().strip()
            raw_tgt = self.target_edit.toPlainText().strip()
            if raw_src and "\n" not in raw_src and len(raw_src) <= 40:
                src_sel = raw_src
                if raw_tgt and "\n" not in raw_tgt and len(raw_tgt) <= 60:
                    tgt_sel = raw_tgt

        self.side_drawer.setVisible(True)
        self._switch_drawer_tab(0)
        if src_sel:
            self.glossary_src_input.setText(src_sel)
        if tgt_sel:
            self.glossary_tgt_input.setText(tgt_sel)
        if src_sel and not tgt_sel:
            self.glossary_tgt_input.setFocus()
        else:
            self.glossary_src_input.setFocus()

    def on_add_glossary_term(self) -> None:
        src = self.glossary_src_input.text().strip()
        tgt = self.glossary_tgt_input.text().strip()
        if not src or not tgt:
            return
        self.glossary_store.add_or_update(src, tgt)
        self.glossary_src_input.clear()
        self.glossary_tgt_input.clear()
        self.glossary_src_input.setFocus()
        self._refresh_glossary_list()
        if self.source_edit.toPlainText().strip() and self.chk_auto_translate.isChecked():
            self.trigger_translation_now()

    def on_import_glossary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            i18n.tr("导入"),
            "",
            "Glossary (*.json *.csv *.tsv *.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            self.glossary_store.import_file(Path(path))
            self._refresh_glossary_list()
        except Exception as exc:
            QMessageBox.warning(self, i18n.tr("执行失败"), str(exc))

    def on_export_glossary(self) -> None:
        if not self.glossary_store.entries:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            i18n.tr("导出"),
            "daily_glossary.json",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            self.glossary_store.export_json(Path(path))
        except Exception as exc:
            QMessageBox.warning(self, i18n.tr("执行失败"), str(exc))

    def on_clear_glossary(self) -> None:
        if not self.glossary_store.entries:
            return
        self.glossary_store.clear()
        self._refresh_glossary_list()

    def _clear_layout_items(self, layout: QVBoxLayout) -> None:
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _refresh_glossary_list(self) -> None:
        self._update_glossary_button_badge()
        self._clear_layout_items(self.glossary_list_layout)
        kw = self.glossary_search_input.text().strip().lower()
        entries = self.glossary_store.entries
        if kw:
            entries = [e for e in entries if kw in e.source.lower() or kw in e.target.lower()]

        if not entries:
            empty_lbl = QtLabel(i18n.tr("暂无术语"))
            empty_lbl.setWordWrap(True)
            empty_lbl.setAlignment(Qt.AlignCenter)
            empty_lbl.setStyleSheet(f"border: none; color: {THEME['ink_muted']}; font-size: 12px; padding: 28px 8px;")
            self.glossary_list_layout.insertWidget(0, empty_lbl)
            return

        for entry in entries:
            row_frame = QFrame()
            row_frame.setStyleSheet(f"""
                QFrame {{
                    background-color: {THEME["surface_soft"]};
                    border: 1px solid {THEME["border"]};
                    border-radius: 0px;
                }}
            """)
            hbox = QHBoxLayout(row_frame)
            hbox.setContentsMargins(10, 6, 8, 6)
            hbox.setSpacing(6)

            chk = QCheckBox("")
            chk.setChecked(entry.enabled)
            chk.toggled.connect(lambda checked, eid=entry.id: self._on_glossary_entry_toggled(eid, checked))

            lbl = QtLabel(f"{entry.source}  →  {entry.target}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"border: none; font-size: 12px; font-weight: 600; color: {THEME['ink']};")

            btn_del = QPushButton("×")
            btn_del.setFixedSize(20, 20)
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: none;
                    color: {THEME["ink_muted"]};
                    font-size: 14px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background: {THEME["vermilion_soft"]};
                    color: {THEME["vermilion"]};
                }}
            """)
            btn_del.clicked.connect(lambda _=False, eid=entry.id: self._on_delete_glossary_entry(eid))

            hbox.addWidget(chk)
            hbox.addWidget(lbl, stretch=1)
            hbox.addWidget(btn_del)
            self.glossary_list_layout.insertWidget(self.glossary_list_layout.count() - 1, row_frame)

    def _on_glossary_entry_toggled(self, entry_id: str, enabled: bool) -> None:
        self.glossary_store.set_enabled(entry_id, enabled)

    def _on_delete_glossary_entry(self, entry_id: str) -> None:
        self.glossary_store.remove(entry_id)
        self._refresh_glossary_list()

    # -----------------------------------------------------------------------
    # 历史记录列表
    # -----------------------------------------------------------------------

    def _refresh_history_list(self) -> None:
        self._clear_layout_items(self.history_list_layout)
        kw = self.history_search_input.text().strip().lower()
        items = self.history_store.items
        if kw:
            items = [i for i in items if kw in i.source.lower() or kw in i.target.lower()]

        if not items:
            empty_lbl = QtLabel(i18n.tr("暂无翻译记录"))
            empty_lbl.setWordWrap(True)
            empty_lbl.setAlignment(Qt.AlignCenter)
            empty_lbl.setStyleSheet(f"border: none; color: {THEME['ink_muted']}; font-size: 12px; padding: 28px 8px;")
            self.history_list_layout.insertWidget(0, empty_lbl)
            return

        ui_zh = i18n._language == "zh"
        for item in items[:80]:
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background-color: {THEME["surface_soft"]};
                    border: 1px solid {THEME["border"]};
                    border-radius: 0px;
                }}
            """)
            vbox = QVBoxLayout(card)
            vbox.setContentsMargins(10, 8, 10, 8)
            vbox.setSpacing(4)

            top = QHBoxLayout()
            top.setSpacing(6)
            ts_str = datetime.fromtimestamp(item.timestamp).strftime("%m-%d %H:%M")
            src_name = language_display_name(item.source_lang, ui_zh=ui_zh)
            tgt_name = language_display_name(item.target_lang, ui_zh=ui_zh)
            badge = QtLabel(f"{src_name} → {tgt_name} · {ts_str}")
            badge.setStyleSheet(f"border: none; font-size: 10px; font-weight: 700; color: {THEME['ink_muted']};")
            top.addWidget(badge)
            top.addStretch(1)

            mini_btn = f"""
                QPushButton {{
                    background-color: {THEME["surface"]};
                    border: 1px solid {THEME["border"]};
                    color: {THEME["ink_secondary"]};
                    padding: 2px 7px;
                    font-size: 10px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    border-color: {THEME["ink"]};
                    color: {THEME["ink"]};
                }}
            """
            btn_use = QPushButton("填入")
            btn_use.setCursor(Qt.PointingHandCursor)
            btn_use.setStyleSheet(mini_btn)
            btn_use.clicked.connect(lambda _=False, it=item: self._restore_history_item(it))

            btn_cp = QPushButton("复制")
            btn_cp.setCursor(Qt.PointingHandCursor)
            btn_cp.setStyleSheet(mini_btn)
            btn_cp.clicked.connect(lambda _=False, txt=item.target: self._copy_history_text(txt))

            btn_del = QPushButton("×")
            btn_del.setFixedSize(20, 20)
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: none;
                    color: {THEME["ink_muted"]};
                    font-size: 13px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background: {THEME["vermilion_soft"]};
                    color: {THEME["vermilion"]};
                }}
            """)
            btn_del.clicked.connect(lambda _=False, iid=item.id: self._delete_history_item(iid))

            top.addWidget(btn_use)
            top.addWidget(btn_cp)
            top.addWidget(btn_del)
            vbox.addLayout(top)

            src_preview = item.source.strip().replace("\n", " ")[:70]
            tgt_preview = item.target.strip().replace("\n", " ")[:90]
            lbl_src = QtLabel(src_preview)
            lbl_src.setStyleSheet(f"border: none; font-size: 11px; color: {THEME['ink_secondary']};")
            lbl_tgt = QtLabel(tgt_preview)
            lbl_tgt.setWordWrap(True)
            lbl_tgt.setStyleSheet(f"border: none; font-size: 12px; font-weight: 600; color: {THEME['ink']};")

            vbox.addWidget(lbl_src)
            vbox.addWidget(lbl_tgt)
            self.history_list_layout.insertWidget(self.history_list_layout.count() - 1, card)

    def _restore_history_item(self, item: DailyHistoryItem) -> None:
        self._suppress_auto_translate = True
        self.source_edit.setPlainText(item.source)
        self._suppress_auto_translate = False
        self._target_streaming = False
        self._target_auto_scroll = True
        self._set_output_notice("")
        self._status_mode = ""
        self._render_translation_status()
        self._apply_target_text(item.target, follow_tail=False, streaming=False)
        self._update_direction_preview()

    def _copy_history_text(self, text: str) -> None:
        clip = QApplication.clipboard()
        if clip is not None:
            self._last_copied_output = text
            clip.setText(text)

    def _delete_history_item(self, item_id: str) -> None:
        self.history_store.remove(item_id)
        self._refresh_history_list()

    def on_clear_history(self) -> None:
        if not self.history_store.items:
            return
        self.history_store.clear()
        self._refresh_history_list()
