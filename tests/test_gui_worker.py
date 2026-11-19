import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from core import i18n
from gui import BatchTranslationWorker, TaskItem, MainWindow
from core.jobs import JobCancelled
from test_regressions import make_pdf


def _isolated_data_env(data_dir: str) -> dict[str, str]:
    """Override the data-dir variable so CI env vars never leak into tests."""
    return {'OFFLINEPDF_DATA_DIR': data_dir}


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._data_dir.cleanup)
        env_patch = patch.dict(os.environ, _isolated_data_env(self._data_dir.name))
        env_patch.start()
        self.addCleanup(env_patch.stop)

        settings_path = str(Path(self._data_dir.name) / 'settings.ini')
        real_qsettings = QSettings
        orig_lang = i18n._language

        def restore_lang():
            i18n._language = orig_lang

        self.addCleanup(restore_lang)
        i18n._language = 'zh'
        real_qsettings(settings_path, real_qsettings.Format.IniFormat).setValue('interface_language', 'zh')
        qsettings_patch = patch.object(
            i18n,
            'QSettings',
            side_effect=lambda *args, **kwargs: real_qsettings(settings_path, real_qsettings.Format.IniFormat),
        )
        qsettings_patch.start()
        self.addCleanup(qsettings_patch.stop)

    def make_worker(self, root, count=1):
        tasks = [TaskItem(str(i), root / f'paper{i}.pdf', 'waiting') for i in range(count)]
        return BatchTranslationWorker(tasks, 'en', 'zh', 1, 1, 4096, 0)

    def test_no_outputs_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            worker = self.make_worker(Path(td))
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime'), patch('gui.set_system_keep_awake'), \
                 patch('core.jobs.run_document_job', return_value={'result': {}}):
                worker.run()
            self.assertEqual(worker.tasks[0].status, 'error')
            self.assertIsNone(worker.tasks[0].mono_pdf)

    def test_restart_failure_does_not_skip_next_document(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worker = self.make_worker(root, 2)
            runtime = MagicMock()
            runtime.start.side_effect = [RuntimeError('startup failed'), None]
            def translate(*args, **kwargs):
                stage = Path(args[2])
                result = {}
                for name in ['mono', 'dual']:
                    file = stage / f'{name}.pdf'
                    make_pdf(file)
                    result[f'{name}_pdf_path'] = str(file)
                kwargs['progress_callback']({'type': 'progress_update', 'stage': 'Translation', 'overall_progress': 65})
                return {'result': result}
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime', return_value=runtime), \
                 patch('gui.set_system_keep_awake'), patch('core.jobs.run_document_job', side_effect=translate):
                worker.run()
            self.assertEqual([t.status for t in worker.tasks], ['error', 'finished'])
            self.assertTrue(Path(worker.tasks[1].dual_pdf).is_file())

    def test_constructor_failure_does_not_emit_finished(self):
        with tempfile.TemporaryDirectory() as td:
            worker = self.make_worker(Path(td))
            emitted = []
            worker.fatal_error.connect(lambda message: emitted.append('fatal'))
            worker.batch_finished.connect(lambda: emitted.append('finished'))
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime', side_effect=RuntimeError('missing model')), \
                 patch('gui.set_system_keep_awake'):
                worker.run()
            self.assertEqual(emitted, ['fatal'])

    def test_review_does_not_publish_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worker = self.make_worker(root)
            def failed(*args, **kwargs):
                (Path(args[2]) / 'quality_issues.json').write_text('[]')
                raise RuntimeError('需复核：占位符缺失')
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime'), patch('gui.set_system_keep_awake'), \
                 patch('core.jobs.run_document_job', side_effect=failed):
                worker.run()
            self.assertEqual(worker.tasks[0].status, 'review')
            self.assertFalse((root / 'paper0.quality.json').exists())

    def test_stopped_job_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            worker = self.make_worker(Path(td))
            stopped, done = [], []
            worker.batch_stopped.connect(lambda: stopped.append(True))
            worker.batch_finished.connect(lambda: done.append(True))
            def cancel(*args, **kwargs):
                worker.request_stop()
                raise JobCancelled()
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime'), patch('gui.set_system_keep_awake'), \
                 patch('core.jobs.run_document_job', side_effect=cancel):
                worker.run()
            self.assertEqual(worker.tasks[0].status, 'stopped')
            self.assertEqual((stopped, done), ([True], []))

    def test_failed_batch_summary_is_honest(self):
        window = MainWindow()
        try:
            window.tasks = [TaskItem('1', Path('failed.pdf'), 'error')]
            window.on_batch_finished()
            self.assertIn('成功 0，失败 1', window.global_status_label.text())
        finally:
            window.close()

    def test_live_stream_follows_active_task_and_switches(self):
        window = MainWindow()
        try:
            t1 = TaskItem('t1', Path('paper1.pdf'), 'waiting')
            t2 = TaskItem('t2', Path('paper2.pdf'), 'waiting')
            window.tasks = [t1, t2]
            window.task_blocks = {'t1': [], 't2': []}
            from gui import TaskCardWidget
            c1 = TaskCardWidget(t1)
            c2 = TaskCardWidget(t2)
            window.card_widgets = {'t1': c1, 't2': c2}

            # 1. 模拟 Task 1 启动执行
            t1.status = 'processing'
            t1.progress = 10
            window.on_task_updated('t1')
            self.assertEqual(window.active_task_id, 't1')
            self.assertEqual(window.current_inspecting_task_id, 't1')
            self.assertIn('paper1.pdf', window.live_panel.task_indicator.text())

            # Task 1 推入 2 个文本块
            window.on_block_translated('t1', 'hello 1', '你好 1', 1, 'success')
            window.on_block_translated('t1', 'hello 2', '你好 2', 2, 'success')
            self.assertEqual(window.live_panel.block_count, 2)

            # 2. 模拟 Task 1 完成，Task 2 启动执行
            t1.status = 'finished'
            t1.progress = 100
            window.on_task_updated('t1')

            t2.status = 'processing'
            t2.progress = 15
            window.on_task_updated('t2')

            # 验证：右侧实时翻译流自动随当前翻译项目切换到 Task 2
            self.assertEqual(window.active_task_id, 't2')
            self.assertEqual(window.current_inspecting_task_id, 't2')
            self.assertIn('paper2.pdf', window.live_panel.task_indicator.text())
            self.assertEqual(window.live_panel.block_count, 0)
            self.assertTrue(c2._is_selected)
            self.assertFalse(c1._is_selected)

            # Task 2 推入文本块
            window.on_block_translated('t2', 'world 1', '世界 1', 1, 'success')
            self.assertEqual(window.live_panel.block_count, 1)

            # 3. 模拟后台还在跑 Task 2 时，用户点击查看 Task 1 历史
            window.worker = MagicMock()
            window.worker.isRunning.return_value = True
            window.on_card_selected('t1')

            self.assertEqual(window.current_inspecting_task_id, 't1')
            self.assertEqual(window.user_pinned_task_id, 't1')
            self.assertIn('paper1.pdf', window.live_panel.task_indicator.text())
            self.assertEqual(window.live_panel.block_count, 2)
            self.assertFalse(window.live_panel.btn_back_to_live.isHidden())

            # 4. 点击“返回实时流”
            window.on_back_to_live_clicked()
            self.assertEqual(window.current_inspecting_task_id, 't2')
            self.assertIsNone(window.user_pinned_task_id)
            self.assertIn('paper2.pdf', window.live_panel.task_indicator.text())
            self.assertTrue(window.live_panel.btn_back_to_live.isHidden())
        finally:
            window.close()

    @unittest.skipUnless((Path(__file__).resolve().parent.parent / "assets/fonts/GoNotoKurrent-Regular.ttf").is_file(), "Bundled fonts are not included in a source checkout")
    def test_bundled_interface_font_and_complex_script_warning(self):
        from PySide6.QtGui import QFontDatabase
        window = MainWindow()
        try:
            self.assertIn('Go Noto Kurrent-Regular', QFontDatabase.families())
            window.lang_combo.setCurrentIndex(window.lang_combo.findData('ar'))
            self.assertFalse(window.script_warning.isHidden())
            window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('en'))
            self.assertIn('shaping', window.script_warning.text())
            window.lang_combo.setCurrentIndex(window.lang_combo.findData('ja'))
            self.assertTrue(window.script_warning.isHidden())
        finally:
            window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('zh'))
            window.close()

    def test_ui_branding_and_stream_toggle(self):
        window = MainWindow()
        try:
            # 1. 验证程序标题与描述
            self.assertEqual(window.windowTitle(), "OfflinePDF Translate — 本地离线PDF文献翻译")
            self.assertEqual(window.btn_start.text(), "开始翻译")

            # 2. 验证翻译流默认隐藏与展开按钮状态
            self.assertTrue(window.live_panel.isHidden())
            self.assertEqual(window.btn_toggle_stream.text(), "展开翻译流")

            # 3. 点击展开翻译流
            window.toggle_stream_panel()
            QApplication.processEvents()
            self.assertFalse(window.live_panel.isHidden())
            self.assertEqual(window.btn_toggle_stream.text(), "收起翻译流")

            # 4. 点击收起翻译流
            window.toggle_stream_panel()
            QApplication.processEvents()
            self.assertTrue(window.live_panel.isHidden())
            self.assertEqual(window.btn_toggle_stream.text(), "展开翻译流")

            # 5. 验证图标文件存在
            icon_ico = Path(__file__).resolve().parent.parent / "assets/icon.ico"
            icon_png = Path(__file__).resolve().parent.parent / "assets/icon.png"
            self.assertTrue(icon_ico.is_file())
            self.assertTrue(icon_png.is_file())
        finally:
            window.close()

    def test_live_stream_user_scroll_does_not_snap_to_top(self):
        window = MainWindow()
        try:
            window.resize(1200, 800)
            window.show()
            window.toggle_stream_panel()
            QApplication.processEvents()
            panel = window.live_panel
            vbar = panel.scroll_area.verticalScrollBar()

            # 1. 连续添加若干段落，处于最顶端时应保持 auto_scroll = True
            for i in range(5):
                panel.add_translation_block(i + 1, f"Source text paragraph number {i + 1} " * 5, f"目标语言译文正文第 {i + 1} 段 " * 5)
                for _ in range(3):
                    QApplication.processEvents()

            self.assertTrue(panel.is_auto_scroll)
            self.assertEqual(vbar.value(), 0)
            self.assertTrue(panel.btn_scroll_latest.isHidden())
            self.assertGreater(vbar.maximum(), 50)

            # 2. 模拟用户向下滚动查看历史段落
            vbar.setValue(150)
            for _ in range(3):
                QApplication.processEvents()
            self.assertFalse(panel.is_auto_scroll)
            self.assertEqual(vbar.value(), 150)

            # 3. 后台产生新段落翻译
            panel.add_translation_block(6, "Source text paragraph 6 " * 5, "目标语言译文正文第 6 段 " * 5)
            for _ in range(3):
                QApplication.processEvents()

            # 核心断言：绝对不能自动跳回顶部 0！
            self.assertGreater(vbar.value(), 0)
            self.assertFalse(panel.is_auto_scroll)
            self.assertEqual(panel.unseen_count, 1)
            self.assertFalse(panel.btn_scroll_latest.isHidden())
            self.assertIn("(+1)", panel.btn_scroll_latest.text())

            # 再来一段新段落
            panel.add_translation_block(7, "Source text paragraph 7 " * 5, "目标语言译文正文第 7 段 " * 5)
            for _ in range(3):
                QApplication.processEvents()
            self.assertEqual(panel.unseen_count, 2)
            self.assertIn("(+2)", panel.btn_scroll_latest.text())
            self.assertGreater(vbar.value(), 0)

            # 4. 点击“回到最新”按钮，应平滑切回顶部并恢复自动跟随
            panel.btn_scroll_latest.click()
            for _ in range(3):
                QApplication.processEvents()

            self.assertEqual(vbar.value(), 0)
            self.assertTrue(panel.is_auto_scroll)
            self.assertEqual(panel.unseen_count, 0)
            self.assertTrue(panel.btn_scroll_latest.isHidden())

            # 5. 用户再次滚动离开，随后手动滚回顶部
            vbar.setValue(120)
            for _ in range(3):
                QApplication.processEvents()
            self.assertFalse(panel.is_auto_scroll)

            vbar.setValue(0)
            for _ in range(3):
                QApplication.processEvents()
            self.assertTrue(panel.is_auto_scroll)
            self.assertTrue(panel.btn_scroll_latest.isHidden())
        finally:
            window.close()

    def test_translation_stream_progress_differs_from_overall_progress(self):
        """测试并验证：左侧任务卡片反映包含版面解析与输出的全局总体进度，而右侧实时翻译流只展示纯翻译百分比。"""
        window = MainWindow()
        try:
            t = TaskItem('task_test', Path('research.pdf'), 'waiting')
            window.tasks = [t]
            window.task_blocks = {'task_test': []}
            from gui import TaskCardWidget
            card = TaskCardWidget(t)
            window.card_widgets = {'task_test': card}
            window.active_task_id = 'task_test'
            window.current_inspecting_task_id = 'task_test'

            # 1. 模拟版面解析阶段 (LayoutParser)
            # 左侧进度：总体推进至 8%（解析阶段上限占 10%），处于“版面解析”阶段
            # 右侧翻译流：翻译尚未开始，翻译进度应为 0%
            t.status = 'processing'
            t.stage_text = '版面解析'
            t.progress = 8
            t.translation_progress = 0
            t.translation_total = 100
            window.on_task_updated('task_test')

            self.assertEqual(card.progress_bar.value(), 8)
            self.assertIn('版面解析 8%', card.status_pill.text())
            self.assertEqual(window.live_panel.progress_line.value(), 0)
            self.assertIn('(0%)', window.live_panel.task_indicator.text())

            # 2. 模拟段落翻译阶段 (Translate Paragraphs)
            # 总体进度：10% (前序解析) + 85% * 45% = 48%，而段落翻译本身完成了 45% (45/100)
            t.stage_text = '段落翻译'
            t.progress = 48
            t.translation_progress = 45
            t.translation_current = 45
            window.on_task_updated('task_test')

            # 核心断言：左右进度解耦！左侧卡片显示 48%，右侧翻译流显示 45%
            self.assertEqual(card.progress_bar.value(), 48)
            self.assertIn('段落翻译 48%', card.status_pill.text())
            self.assertEqual(window.live_panel.progress_line.value(), 45)
            self.assertIn('(45%)', window.live_panel.task_indicator.text())
            self.assertIn('/100 个文本块', window.live_panel.count_badge.text())

            # 3. 模拟排版输出阶段 (Typesetting)
            # 总体进度 96%，段落翻译已 100% 结束
            t.stage_text = '排版渲染'
            t.progress = 96
            t.translation_progress = 100
            window.on_task_updated('task_test')

            self.assertEqual(card.progress_bar.value(), 96)
            self.assertIn('排版渲染 96%', card.status_pill.text())
            self.assertEqual(window.live_panel.progress_line.value(), 100)
            self.assertIn('(100%)', window.live_panel.task_indicator.text())
        finally:
            window.close()

    def test_stage_weights_allocation_in_worker(self):
        """测试 Worker 内部的权重配比：解析阶段 <= 10%，翻译阶段 10%~95%，排版输出阶段 >= 95%。"""
        with tempfile.TemporaryDirectory() as td:
            worker = self.make_worker(Path(td))
            task = worker.tasks[0]

            def fake_translate(*args, **kwargs):
                cb = kwargs['progress_callback']
                # 1. 模拟版面解析阶段
                cb({'type': 'progress_update', 'stage': 'Parse Page Layout', 'stage_progress': 50, 'overall_progress': 20})
                self.assertLessEqual(task.progress, 10)
                self.assertEqual(task.translation_progress, 0)
                self.assertEqual(task.stage_text, '版面解析')

                # 2. 模拟段落翻译阶段开始 (总 100 块，翻译完 50 块)
                cb({'type': 'progress_start', 'stage': 'Translate Paragraphs', 'stage_total': 100, 'stage_current': 0, 'stage_progress': 0})
                self.assertEqual(task.translation_progress, 0)
                self.assertEqual(task.progress, 10)

                cb({'type': 'progress_update', 'stage': 'Translate Paragraphs', 'stage_total': 100, 'stage_current': 50, 'stage_progress': 50.0})
                # 总体进度 = 10 + 0.85 * 50 = 52%
                self.assertEqual(task.translation_progress, 50)
                self.assertEqual(task.progress, 52)
                self.assertEqual(task.stage_text, '段落翻译')

                # 3. 模拟排版输出阶段
                cb({'type': 'progress_update', 'stage': 'Typesetting', 'stage_progress': 50, 'overall_progress': 97})
                self.assertEqual(task.translation_progress, 100)
                self.assertGreaterEqual(task.progress, 95)
                self.assertEqual(task.stage_text, '排版渲染')

                stage = Path(args[2])
                res = {}
                for name in ['mono', 'dual']:
                    f = stage / f'{name}.pdf'
                    make_pdf(f)
                    res[f'{name}_pdf_path'] = str(f)
                return {'result': res}

            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime'), patch('gui.set_system_keep_awake'), \
                 patch('core.jobs.run_document_job', side_effect=fake_translate):
                worker.run()

            self.assertEqual(task.status, 'finished')
            self.assertEqual(task.progress, 100)
            self.assertEqual(task.translation_progress, 100)

    def test_scanned_pdf_error_message_and_tooltip_styling(self):
        from core import i18n
        from gui import TaskCardWidget

        self.assertEqual(i18n.error_key('BabelDOC 翻译失败: The document contains no paragraphs.'), 'scan')
        self.assertEqual(i18n.error_key('RuntimeError: 不支持扫描版 PDF'), 'scan')

        window = MainWindow()
        try:
            window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('zh'))
            self.assertIn('QToolTip', window.styleSheet())
            self.assertIn('QScrollArea { background: transparent; }', window.scroll_area.styleSheet())

            task = TaskItem(
                'scan_task',
                Path('scanned.pdf'),
                'error',
                error_msg='Traceback (most recent call last):\n  File "engine.py", line 1\nRuntimeError: 不支持扫描版 PDF',
            )
            card = TaskCardWidget(task)
            self.assertIn('不支持扫描版的 PDF', card.error_hint.text())
            self.assertEqual(card.status_pill.toolTip(), '错误详情: RuntimeError: 不支持扫描版 PDF')
            self.assertIn('#TaskNameLabel', card.name_label.styleSheet())
            self.assertIn('#StatusPill', card.status_pill.styleSheet())
        finally:
            window.close()

    def test_source_lang_and_hardware_settings_persisted_and_scanned_preflight_skips_runtime(self):
        import json
        import pymupdf
        from core.offline import user_data_dir

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scanned_pdf = root / 'scan_only.pdf'
            with pymupdf.open() as doc:
                doc.new_page()
                doc.save(scanned_pdf)

            runtime = MagicMock()
            tasks = [TaskItem('s1', scanned_pdf, 'waiting')]
            worker = BatchTranslationWorker(tasks, 'auto', 'ja', 4, 2, 8192, 0)
            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime', return_value=runtime), \
                 patch('gui.set_system_keep_awake'):
                worker.run()

            self.assertEqual(tasks[0].status, 'error')
            self.assertIn('不支持扫描版', tasks[0].error_msg)
            runtime.start.assert_not_called()

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            w1 = MainWindow()
            try:
                self.assertEqual(w1.source_lang_combo.currentData(), 'auto')
                w1.source_lang_combo.setCurrentIndex(w1.source_lang_combo.findData('zh'))
                w1.auto_hw_checkbox.setChecked(False)
                w1.threads_spin.setValue(6)
                w1.parallel_spin.setValue(2)
                w1.gpu_layers_spin.setValue(32)
                w1._save_queue()
            finally:
                w1.close()

            saved = json.loads((user_data_dir() / 'queue.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['source_language'], 'zh')
            self.assertFalse(saved['auto_hardware'])
            self.assertEqual(saved['threads'], 6)
            self.assertEqual(saved['n_gpu_layers'], 32)

            w2 = MainWindow()
            try:
                self.assertEqual(w2.source_lang_combo.currentData(), 'zh')
                self.assertFalse(w2.auto_hw_checkbox.isChecked())
                self.assertEqual(w2.threads_spin.value(), 6)
                self.assertEqual(w2.parallel_spin.value(), 2)
                self.assertEqual(w2.gpu_layers_spin.value(), 32)
                self.assertEqual(w2._daily_hw_config()[2], 32)
            finally:
                w2.close()

    def test_non_windows_open_local_path_uses_popen_without_crashing(self):
        import gui
        with tempfile.TemporaryDirectory() as td:
            sample = Path(td) / 'out.pdf'
            sample.write_bytes(b'%PDF-1.4')
            window = MainWindow()
            try:
                with patch.object(gui.sys, 'platform', 'linux'), \
                     patch.object(gui.subprocess, 'Popen', side_effect=FileNotFoundError('no xdg-open')) as mock_popen:
                    window.open_pdf_file(str(sample))
                mock_popen.assert_called_once()
            finally:
                window.close()

    def test_remove_queued_task_while_worker_running(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                t1 = TaskItem('t1', Path('active.pdf'), 'processing', progress=20)
                t2 = TaskItem('t2', Path('queued.pdf'), 'waiting')
                window._add_task(t1)
                window._add_task(t2)
                window.active_task_id = 't1'
                window.worker = MagicMock()
                window.worker.isRunning.return_value = True

                # 用户点击了排队中的任务并正在检视它，随后点击删除
                window.on_card_selected('t2')
                self.assertEqual(window.current_inspecting_task_id, 't2')
                self.assertFalse(window.live_panel.btn_back_to_live.isHidden())

                window.remove_task('t2')

                # 排队任务应被删除，且不再保留在列表和卡片映射中
                self.assertEqual([t.id for t in window.tasks], ['t1'])
                self.assertNotIn('t2', window.card_widgets)
                self.assertNotIn('t2', window.task_blocks)
                # 右侧应自动切回当前执行中的任务并刷新进度总数
                self.assertEqual(window.current_inspecting_task_id, 't1')
                self.assertIsNone(window.user_pinned_task_id)
                self.assertTrue(window.live_panel.btn_back_to_live.isHidden())
                self.assertIn('1/1', window.global_status_label.text())

                # 正在处理中的任务仍不允许通过 remove_task 删除
                window.remove_task('t1')
                self.assertEqual([t.id for t in window.tasks], ['t1'])
            finally:
                window.close()

    def test_worker_skips_removed_queued_task_and_handles_preceding_removal(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t0 = TaskItem('t0', root / 'done.pdf', 'finished')
            t1 = TaskItem('t1', root / 'p1.pdf', 'waiting')
            t2 = TaskItem('t2', root / 'p2.pdf', 'waiting')
            t3 = TaskItem('t3', root / 'p3.pdf', 'waiting')
            tasks = [t0, t1, t2, t3]
            lock = threading.Lock()
            worker = BatchTranslationWorker(tasks, 'en', 'zh', 1, 1, 4096, 0, tasks_lock=lock)
            progress_events = []
            worker.batch_progress.connect(lambda idx, total, pct: progress_events.append((idx, total, pct)))

            ran = []

            def translate(*args, **kwargs):
                pdf_path = Path(args[1])
                ran.append(pdf_path.name)
                if pdf_path.name == 'p1.pdf':
                    # 模拟运行期间删除前置已完成任务 t0 和排队中任务 t2
                    with lock:
                        tasks[:] = [t for t in tasks if t.id not in ('t0', 't2')]
                kwargs['progress_callback']({'type': 'progress_update', 'stage': 'Translation', 'overall_progress': 50})
                stage = Path(args[2])
                result = {}
                for name in ['mono', 'dual']:
                    file = stage / f'{name}.pdf'
                    make_pdf(file)
                    result[f'{name}_pdf_path'] = str(file)
                return {'result': result}

            with patch('core.offline.resolve_translation_model', return_value=Path('models/mock.gguf')), \
                 patch('core.llama_runtime.LlamaServerRuntime'), patch('gui.set_system_keep_awake'), \
                 patch('core.jobs.run_document_job', side_effect=translate):
                worker.run()

            self.assertEqual(ran, ['p1.pdf', 'p3.pdf'])
            self.assertEqual([(t.id, t.status) for t in tasks], [('t1', 'finished'), ('t3', 'finished')])
            self.assertTrue(all(1 <= idx <= total for idx, total, _ in progress_events))
            self.assertEqual(progress_events[-1], (2, 2, 100))

    def test_language_switch_no_text_clipping_and_complete_duration_i18n(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton
        from core import i18n
        from gui import TaskCardWidget

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                t_err = TaskItem('e1', Path('research.pdf'), 'error', error_msg='文件不存在')
                t_stop = TaskItem('s1', Path('stopped.pdf'), 'stopped')
                t_done = TaskItem('d1', Path('done.pdf'), 'finished', mono_pdf='a.pdf', dual_pdf='b.pdf')
                for t in (t_err, t_stop, t_done):
                    window._add_task(t)
                window._update_queue_ui()
                window.auto_hw_checkbox.setChecked(False)
                window.resize(740, 720)
                window.show()

                def assert_no_clipping(tag):
                    for cls_type in (QPushButton, QCheckBox, QComboBox, QLabel):
                        for widget in window.findChildren(cls_type):
                            if not widget.isVisible():
                                continue
                            txt = widget.currentText() if isinstance(widget, QComboBox) else widget.text()
                            if txt == '×':
                                continue
                            if isinstance(widget, QLabel) and widget.wordWrap():
                                continue
                            sh = widget.sizeHint().width()
                            self.assertGreaterEqual(
                                widget.width(),
                                sh - 1,
                                f'[{tag}] {type(widget).__name__} clipped: text={txt!r} width={widget.width()} < sizeHint={sh}',
                            )

                for _, code in i18n.LANGUAGES:
                    window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData(code))
                    for _ in range(5):
                        QApplication.processEvents()
                    assert_no_clipping(f'pdf-{code}')

                # 英文模式下不应残留“待执行”中文片段
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('en'))
                for _ in range(5):
                    QApplication.processEvents()
                self.assertEqual(window.card_widgets['e1'].duration_pill.text(), 'Interrupted · Pending')
                self.assertEqual(window.card_widgets['s1'].duration_pill.text(), 'Stopped · Pending')
                self.assertNotIn('待执行', window.card_widgets['e1'].duration_pill.text())

                # 展开实时流面板后切换各语言也应无截断
                window.toggle_stream_panel()
                for _, code in i18n.LANGUAGES:
                    window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData(code))
                    for _ in range(5):
                        QApplication.processEvents()
                    assert_no_clipping(f'live-{code}')

                # 切换到日常翻译页与侧边抽屉也应无截断且动态计数词条随语言更新
                window.switch_mode(1)
                window.daily_panel._toggle_drawer(0)
                for _, code in i18n.LANGUAGES:
                    window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData(code))
                    for _ in range(5):
                        QApplication.processEvents()
                    assert_no_clipping(f'daily-{code}')
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('en'))
                for _ in range(5):
                    QApplication.processEvents()
                self.assertEqual(window.daily_panel.char_count_label.text(), '0 / 5000 chars')

                # 验证下拉框及其弹出列表使用浅色高反差配色（不随 windows11 样式发黑，且容器样式不穿透剥离边框）
                self.assertIn('QComboBox QAbstractItemView', window.styleSheet())
                self.assertIn('#InlineGroup', window.source_lang_combo.parentWidget().styleSheet())
                for combo in (
                    window.ui_language_combo,
                    window.source_lang_combo,
                    window.lang_combo,
                    window.daily_panel.src_combo,
                    window.daily_panel.tgt_combo,
                    window.daily_panel.style_combo,
                ):
                    view = combo.view()
                    self.assertNotEqual(combo.style().objectName().lower(), 'windows11')
                    self.assertEqual(view.palette().base().color().name(), '#ffffff')
                    self.assertEqual(view.palette().text().color().name(), '#121212')
                    self.assertGreater(view.window().palette().window().color().lightness(), 200)
            finally:
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData('zh'))
                window.close()

    def test_restore_queue_prunes_missing_relative_ghost_tasks_and_duplicates(self):
        import json
        from core.offline import user_data_dir

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            real_missing = (Path(data_dir) / 'real_paper.pdf').resolve()
            queue_path = user_data_dir() / 'queue.json'
            queue_path.write_text(json.dumps({
                'version': 1,
                'source_language': 'auto',
                'target_language': 'zh',
                'tasks': [
                    {'path': 'research.pdf', 'status': 'error', 'error': '文件不存在'},
                    {'path': 'done.pdf', 'status': 'finished', 'mono_pdf': 'a.pdf', 'dual_pdf': 'b.pdf'},
                    {'path': str(real_missing), 'status': 'waiting'},
                    {'path': str(real_missing), 'status': 'waiting'},
                ],
            }), encoding='utf-8')

            window = MainWindow()
            try:
                self.assertEqual([t.file_path for t in window.tasks], [real_missing])
                self.assertEqual(window.tasks[0].status, 'error')
                saved = json.loads(queue_path.read_text(encoding='utf-8'))
                self.assertEqual(len(saved['tasks']), 1)
                self.assertEqual(saved['tasks'][0]['path'], str(real_missing))
            finally:
                window.close()

    def test_language_combo_popups_cap_height_and_show_scrollbar(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

        window = MainWindow()
        try:
            window.resize(800, 720)
            window.show()
            for _ in range(3):
                QApplication.processEvents()

            combos = [
                ('pdf_source', 0, window.source_lang_combo),
                ('pdf_target', 0, window.lang_combo),
                ('daily_source', 1, window.daily_panel.src_combo),
                ('daily_target', 1, window.daily_panel.tgt_combo),
            ]
            for name, mode_idx, combo in combos:
                window.switch_mode(mode_idx)
                for _ in range(3):
                    QApplication.processEvents()

                opt = QStyleOptionComboBox()
                combo.initStyleOption(opt)
                self.assertEqual(
                    combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, opt, combo),
                    0,
                    f'{name} should disable Fusion menu-style popup',
                )
                self.assertEqual(combo.maxVisibleItems(), 12, name)
                self.assertEqual(combo.view().verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded, name)

                try:
                    combo.showPopup()
                    for _ in range(3):
                        QApplication.processEvents()
                    view = combo.view()
                    popup = view.window()
                    self.assertLessEqual(popup.height(), 460, f'{name} popup too tall: {popup.height()}')
                    self.assertTrue(view.verticalScrollBar().isVisible(), f'{name} missing vertical scrollbar')
                    self.assertGreater(view.verticalScrollBar().maximum(), 0, name)
                    self.assertFalse(view.horizontalScrollBar().isVisible(), f'{name} unexpected horizontal scrollbar')
                finally:
                    combo.hidePopup()
        finally:
            window.close()
