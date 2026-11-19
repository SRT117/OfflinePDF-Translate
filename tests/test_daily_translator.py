"""日常翻译功能全链路单元测试：自动互译、Hy-MT2 术语表干预、断点清理、分块与 GUI 集成。"""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from core import i18n
from core.daily_translator import (
    AUTO_BILINGUAL_TARGET,
    DAILY_MAX_INPUT_CHARS,
    DAILY_MAX_OUTPUT_CHARS,
    DailyGlossaryEntry,
    DailyGlossaryStore,
    DailyHistoryStore,
    DailyTranslationResult,
    InputTooLongError,
    clean_pdf_linebreaks,
    compute_chunk_max_tokens,
    compute_safe_chunk_chars,
    detect_daily_source_language,
    estimate_token_count,
    match_glossary_terms,
    resolve_daily_direction,
    split_daily_chunks,
    translate_daily_text,
)
from core.llama_runtime import LlamaServerRuntime
from core.prompts import build_daily_prompt
from gui import MainWindow


def _isolated_data_env(data_dir: str) -> dict[str, str]:
    """Override every supported data-dir variable so CI env vars never leak into tests."""
    return {
        "OFFLINEPDF_DATA_DIR": data_dir,
        "PDF2ZH_DATA_DIR": data_dir,
        "LITMTRANS_DATA_DIR": data_dir,
    }


class StreamingMockRuntime:
    """模拟支持 SSE 流式输出的本地推理运行时。"""

    def __init__(self, reply_map=None, default_reply="这是模拟译文。"):
        self.reply_map = reply_map or {}
        self.default_reply = default_reply
        self.prompts = []
        self.stream_calls = 0

    def chat_completion_stream(self, **kwargs):
        prompt = kwargs["prompt"]
        self.prompts.append(prompt)
        self.stream_calls += 1
        cancelled = kwargs.get("cancelled")
        reply = self.default_reply
        target_segment = prompt.splitlines()[-1]
        for key, val in self.reply_map.items():
            if key in target_segment:
                reply = val
                break
        # 逐 3 个字符流式吐出，模拟真实 SSE delta
        for i in range(0, len(reply), 3):
            if cancelled and cancelled():
                return
            yield reply[i : i + 3]


class DailyTranslatorCoreTests(unittest.TestCase):
    def test_auto_bilingual_direction_routing(self):
        # 英文 / 日文 / 混合偏英文 -> 自动译为简体中文
        self.assertEqual(resolve_daily_direction("Transformer attention mechanism is effective.", "auto", AUTO_BILINGUAL_TARGET), ("en", "zh"))
        self.assertEqual(resolve_daily_direction("これはテストです。", "auto", AUTO_BILINGUAL_TARGET), ("ja", "zh"))
        # 中文 / 中英夹杂偏中文 -> 自动译为英文
        self.assertEqual(resolve_daily_direction("请帮我把这段话翻译得更地道一些。", "auto", AUTO_BILINGUAL_TARGET), ("zh", "en"))
        self.assertEqual(resolve_daily_direction("请解释一下 Transformer 的核心原理", "auto", AUTO_BILINGUAL_TARGET), ("zh", "en"))
        # 显式指定目标语言时尊重用户选择
        self.assertEqual(resolve_daily_direction("Hello world", "auto", "ja"), ("en", "ja"))
        self.assertEqual(detect_daily_source_language("안녕하세요 반갑습니다"), "ko")

    def test_hy_mt2_official_prompt_format(self):
        # 1. 中文语向带术语表 + 风格
        p_zh = build_daily_prompt(
            source_text="Zero-shot reasoning improves robustness.",
            target_lang="简体中文",
            glossary_matches=[("Zero-shot", "零样本"), ("robustness", "鲁棒性")],
            style_key="academic",
            use_english=False,
        )
        self.assertIn("参考下面的翻译：", p_zh)
        self.assertIn("Zero-shot 翻译成 零样本", p_zh)
        self.assertIn("robustness 翻译成 鲁棒性", p_zh)
        self.assertIn("注意翻译的风格要严格符合【", p_zh)
        self.assertIn("Zero-shot reasoning improves robustness.", p_zh)

        # 2. 非中文语向 (EN -> JA) 使用官方英文提示词模板
        p_en = build_daily_prompt(
            source_text="Zero-shot reasoning.",
            target_lang="Japanese",
            glossary_matches=[("Zero-shot", "ゼロショット")],
            style_key="default",
            use_english=True,
        )
        self.assertIn("Reference the following translations:", p_en)
        self.assertIn("Zero-shot translates to ゼロショット", p_en)
        self.assertIn("Translate the following text into Japanese", p_en)
        self.assertNotIn("参考下面的翻译", p_en)

    def test_glossary_matching_boundaries_and_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store_path = Path(td) / "glossary.json"
            store = DailyGlossaryStore(store_path)
            e1 = store.add_or_update("AI", "人工智能")
            e2 = store.add_or_update("attention mechanism", "注意力机制")
            e3 = store.add_or_update("大模型", "LLM", source_lang="zh", target_lang="en")
            e4 = store.add_or_update("disabled_term", "禁用词")
            store.set_enabled(e4.id, False)

            # "wait" 不应误匹配词边界词汇 "AI"
            matched_none = match_glossary_terms("Please wait for the disabled_term.", store.entries, "en", "zh")
            self.assertEqual(matched_none, [])

            # 精确命中 "AI" 和 "attention mechanism"，且按长度降序优先
            matched = match_glossary_terms(
                "Modern AI relies on the attention mechanism.",
                store.entries,
                source_lang="en",
                target_lang="zh",
            )
            self.assertEqual([m.source for m in matched], ["attention mechanism", "AI"])

            # 测试 TSV 导入与重新加载持久化
            tsv_file = Path(td) / "terms.tsv"
            tsv_file.write_text("source\ttarget\nRAG\t检索增强生成\nKV Cache\t键值缓存\n", encoding="utf-8")
            imported = store.import_file(tsv_file)
            self.assertEqual(imported, 2)

            reloaded = DailyGlossaryStore(store_path)
            sources = {e.source for e in reloaded.entries}
            self.assertIn("RAG", sources)
            self.assertIn("KV Cache", sources)
            self.assertIn("AI", sources)
            self.assertFalse(next(e for e in reloaded.entries if e.id == e4.id).enabled)
            self.assertEqual(e1.target, "人工智能")
            self.assertEqual(e2.target, "注意力机制")
            self.assertEqual(e3.target, "LLM")

    def test_clean_pdf_linebreaks_preserves_paragraphs_lists_and_code(self):
        messy = (
            "This method sig-\n"
            "nificantly improves\n"
            "accuracy.\n"
            "\n"
            "- First bullet\n"
            "  continues here\n"
            "\n"
            "```python\n"
            "def foo():\n"
            "    return 1\n"
            "```"
        )
        cleaned = clean_pdf_linebreaks(messy)
        self.assertIn("This method significantly improves accuracy.", cleaned)
        self.assertIn("\n\n- First bullet continues here\n\n", cleaned)
        self.assertIn("```python\ndef foo():\n    return 1\n```", cleaned)

    def test_chunking_passthrough_code_blocks_and_preserves_separators(self):
        text = (
            "First paragraph.\n\n"
            "```bash\n"
            "echo 'do not translate'\n"
            "```\n\n"
            "Second paragraph with https://example.com/docs/v1 inside."
        )
        chunks = split_daily_chunks(text, max_chunk_chars=250)
        self.assertEqual(len(chunks), 3)
        self.assertFalse(chunks[0].is_code_block)
        self.assertTrue(chunks[1].is_code_block)
        self.assertFalse(chunks[2].is_code_block)

        runtime = StreamingMockRuntime(
            reply_map={
                "First paragraph": "第一段。",
                "Second paragraph": "第二段包含 [u1]。",
            }
        )
        partials = []
        result = translate_daily_text(
            runtime=runtime,
            text=text,
            source_lang="en",
            target_lang="zh",
            on_partial=lambda txt, st: partials.append((txt, st)),
        )
        self.assertEqual(runtime.stream_calls, 2)  # 代码块不送模型
        self.assertEqual(
            result.translated_text,
            "第一段。\n\n```bash\necho 'do not translate'\n```\n\n第二段包含 https://example.com/docs/v1。",
        )
        self.assertGreater(len(partials), 0)

    def test_history_store_dedup_and_cap(self):
        with tempfile.TemporaryDirectory() as td:
            hist_path = Path(td) / "history.json"
            hist = DailyHistoryStore(hist_path, max_items=3)
            hist.add("Hello", "你好", "en", "zh")
            # 连续完全相同条目去重
            hist.add("Hello", "你好", "en", "zh")
            self.assertEqual(len(hist.items), 1)
            hist.add("Two", "二", "en", "zh")
            hist.add("Three", "三", "en", "zh")
            hist.add("Four", "四", "en", "zh")
            self.assertEqual(len(hist.items), 3)
            self.assertEqual([i.source for i in hist.items], ["Four", "Three", "Two"])

            reloaded = DailyHistoryStore(hist_path)
            self.assertEqual(len(reloaded.items), 3)

    def test_translate_daily_text_strips_leaks_and_supports_cancellation(self):
        rt = StreamingMockRuntime(default_reply="将以下文本翻译为 简体中文：\n\n这是干净的译文。")
        res = translate_daily_text(rt, "This is clean output.", source_lang="en", target_lang="zh")
        self.assertEqual(res.translated_text, "这是干净的译文。")

        # 独立 URL 直接返回不调用模型
        rt_url = StreamingMockRuntime()
        url_partials = []
        res_url = translate_daily_text(
            rt_url,
            "https://github.com/Tencent-Hunyuan/Hy-MT2",
            on_partial=lambda txt, st: url_partials.append((txt, st)),
        )
        self.assertEqual(rt_url.stream_calls, 0)
        self.assertEqual(res_url.translated_text, "https://github.com/Tencent-Hunyuan/Hy-MT2")
        self.assertEqual(url_partials[-1][1]["progress"], 100)

    def test_daily_progress_percentage_is_monotonic_and_reaches_100(self):
        long_text = ("First sentence for multi-chunk progress check. " * 12).strip() + "\n\n" + (
            "Second sentence for multi-chunk progress check. " * 12
        ).strip()
        rt = StreamingMockRuntime(default_reply="这是用于验证百分比进度平滑递增的译文内容。" * 4)
        partials = []
        translate_daily_text(
            runtime=rt,
            text=long_text,
            source_lang="en",
            target_lang="zh",
            max_chunk_chars=260,
            on_partial=lambda txt, st: partials.append((txt, st)),
        )
        progresses = [st["progress"] for _, st in partials]
        self.assertGreater(len(progresses), 2)
        self.assertTrue(all(1 <= p <= 100 for p in progresses))
        self.assertEqual(progresses, sorted(progresses))
        self.assertLess(progresses[0], 100)
        self.assertEqual(progresses[-1], 100)

    def test_context_aware_chunking_and_dynamic_max_tokens(self):
        # 4096 窗口对 ZH->EN 更保守，8192 窗口放宽到 900
        safe_4k_zh = compute_safe_chunk_chars(4096, "zh", "en", sample_text="这是一个中文测试文本。")
        safe_8k_zh = compute_safe_chunk_chars(8192, "zh", "en", sample_text="这是一个中文测试文本。")
        self.assertLessEqual(safe_4k_zh, 600)
        self.assertEqual(safe_8k_zh, 900)
        self.assertGreaterEqual(safe_4k_zh, 200)

        # 短句不盲目申请 4096 tokens，且 prompt + max_tokens 永不超上下文窗口
        short_max = compute_chunk_max_tokens(
            chunk_text="Hello world",
            prompt_text="将以下文本翻译为 简体中文：\n\nHello world",
            source_lang="en",
            target_lang="zh",
            context_size=4096,
        )
        self.assertEqual(short_max, 256)

        tight_prompt = "x " * 400
        tight_max = compute_chunk_max_tokens(
            chunk_text="中文" * 300,
            prompt_text=tight_prompt,
            source_lang="zh",
            target_lang="en",
            context_size=1024,
        )
        self.assertLessEqual(estimate_token_count(tight_prompt) + 64 + tight_max, 1024)

        # Prompt 超出 55% 上下文时自动剥离历史滑窗上文与多余术语
        class CtxAwareMockRuntime:
            def __init__(self, context_size=1024):
                self.context_size = context_size
                self.calls = []

            def chat_completion_stream(self, **kwargs):
                self.calls.append(kwargs)
                yield "译文。"

        rt = CtxAwareMockRuntime(context_size=1024)
        many_terms = [DailyGlossaryEntry(id=f"t{i}", source=f"Term{i}", target=f"术语{i}") for i in range(18)]
        src = "First paragraph. " + " ".join(f"Term{i}" for i in range(18)) + ".\n\nSecond paragraph " + " ".join(f"Term{i}" for i in range(18)) + "."
        # 故意注入极长第一段，使第二段带入滑窗上文与大量术语时触发降级保护
        res = translate_daily_text(rt, src, source_lang="en", target_lang="zh", glossary=many_terms, max_chunk_chars=260)
        self.assertFalse(res.truncated)
        self.assertTrue(all(call["max_tokens"] < 1024 for call in rt.calls))

    def test_input_too_long_and_output_truncation_detection(self):
        rt = StreamingMockRuntime()
        with self.assertRaises(InputTooLongError):
            translate_daily_text(rt, "A" * (DAILY_MAX_INPUT_CHARS + 1))

        # 模拟 llama-server 返回 finish_reason="length"
        class LengthTruncatedRuntime:
            context_size = 4096

            def chat_completion_stream(self, **kwargs):
                stream_meta = kwargs.get("stream_meta")
                yield "第一部分译文"
                yield "未完..."
                if stream_meta is not None:
                    stream_meta["finish_reason"] = "length"

        res_len = translate_daily_text(LengthTruncatedRuntime(), "Hello world", source_lang="en", target_lang="zh")
        self.assertTrue(res_len.truncated)
        self.assertEqual(res_len.truncated_chunks, 1)
        self.assertEqual(res_len.completed_chunks, 1)
        self.assertEqual(res_len.truncation_reason, "length")

        # 模拟超过 max_output_chars 提前停止后续分块
        long_src = ("Sentence one for limit test. " * 10).strip() + "\n\n" + ("Sentence two for limit test. " * 10).strip()
        rt_out = StreamingMockRuntime(default_reply="这是一段足够长的模拟译文内容用于测试输出字数限制。")
        partials = []
        res_out = translate_daily_text(
            rt_out,
            long_src,
            source_lang="en",
            target_lang="zh",
            max_chunk_chars=220,
            max_output_chars=24,
            on_partial=lambda txt, st: partials.append((txt, st)),
        )
        self.assertTrue(res_out.truncated)
        self.assertEqual(res_out.truncation_reason, "output_limit")
        self.assertLessEqual(len(res_out.translated_text), 24)
        self.assertEqual(rt_out.stream_calls, 1)
        self.assertTrue(all(len(txt) <= 24 for txt, _ in partials))

    def test_runtime_stream_captures_finish_reason_and_context_http_error(self):
        import io
        import urllib.error

        with tempfile.TemporaryDirectory() as td:
            model_file = Path(td) / "mock.gguf"
            model_file.write_bytes(b"GGUF")
            fake_rt_dir = Path(td) / "rt"
            fake_rt_dir.mkdir()
            fake_bin = fake_rt_dir / ("llama-server.exe" if os.name == "nt" else "llama-server")
            fake_bin.write_bytes(b"")
            if os.name == "nt":
                for dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
                    (fake_rt_dir / dll).write_bytes(b"")

            runtime = LlamaServerRuntime(model_path=model_file, runtime_bin=fake_bin, log_path=Path(td) / "srv.log")

            sse_lines = [
                b'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n',
                b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"length"}]}\n',
                b'data: [DONE]\n',
            ]

            class FakeResp:
                def __iter__(self):
                    return iter(sse_lines)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            meta = {}
            with patch.object(runtime.opener, "open", return_value=FakeResp()):
                chunks = list(runtime.chat_completion_stream("hi", stream_meta=meta))
            self.assertEqual("".join(chunks), "Hello world")
            self.assertEqual(meta["finish_reason"], "length")
            self.assertEqual(runtime.last_stream_finish_reason, "length")

            # HTTP 400 context overflow error
            http_err = urllib.error.HTTPError(
                url="http://127.0.0.1/v1/chat/completions",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"exceed_context_size: prompt exceeds n_ctx"}}'),
            )
            with patch.object(runtime.opener, "open", side_effect=http_err):
                with self.assertRaises(RuntimeError) as ctx_err:
                    list(runtime.chat_completion_stream("too long"))
            self.assertIn("上下文", str(ctx_err.exception))


class DailyTranslatorGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._settings_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_dir.cleanup)
        settings_path = str(Path(self._settings_dir.name) / "settings.ini")
        real_qsettings = QSettings
        orig_lang = i18n._language

        def restore_lang():
            i18n._language = orig_lang

        self.addCleanup(restore_lang)
        i18n._language = "zh"
        real_qsettings(settings_path, real_qsettings.Format.IniFormat).setValue("interface_language", "zh")
        qsettings_patch = patch.object(
            i18n,
            "QSettings",
            side_effect=lambda *args, **kwargs: real_qsettings(settings_path, real_qsettings.Format.IniFormat),
        )
        qsettings_patch.start()
        self.addCleanup(qsettings_patch.stop)

    def test_mode_switching_preserves_pdf_and_wires_daily_panel(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                # 默认停留在 PDF 文献翻译页，不干扰原有行为
                self.assertEqual(window.mode_stack.currentIndex(), 0)
                self.assertTrue(window.btn_mode_pdf.isChecked())
                self.assertFalse(window.btn_mode_daily.isChecked())

                # 切换到日常翻译页
                window.switch_mode(1)
                QApplication.processEvents()
                self.assertEqual(window.mode_stack.currentIndex(), 1)
                self.assertTrue(window.btn_mode_daily.isChecked())
                self.assertFalse(window.btn_mode_pdf.isChecked())
                self.assertTrue(window.btn_add_files.isHidden())
                self.assertTrue(window.btn_clear_list.isHidden())

                # 测试 PDF 与日常翻译的源/目标语言下拉框均完整覆盖所有支持语种
                dp = window.daily_panel
                from core.prompts import SUPPORTED_LANGUAGES
                expected_codes = [code for _, code in SUPPORTED_LANGUAGES]
                pdf_src_codes = [window.source_lang_combo.itemData(i) for i in range(window.source_lang_combo.count())]
                pdf_tgt_codes = [window.lang_combo.itemData(i) for i in range(window.lang_combo.count())]
                daily_src_codes = [dp.src_combo.itemData(i) for i in range(dp.src_combo.count())]
                daily_tgt_codes = [dp.tgt_combo.itemData(i) for i in range(dp.tgt_combo.count())]
                self.assertEqual(pdf_src_codes, ["auto", *expected_codes])
                self.assertEqual(pdf_tgt_codes, expected_codes)
                self.assertEqual(daily_src_codes, ["auto", *expected_codes])
                self.assertEqual(daily_tgt_codes, [AUTO_BILINGUAL_TARGET, *expected_codes])

                # 测试日常翻译默认值：自动识别 + 智能互译 + 自动翻译开启
                self.assertEqual(dp.src_combo.currentData(), "auto")
                self.assertEqual(dp.tgt_combo.currentData(), AUTO_BILINGUAL_TARGET)
                self.assertTrue(dp.chk_auto_translate.isChecked())

                # 测试快捷添加术语并触发列表更新
                dp.glossary_src_input.setText("Speculative Decoding")
                dp.glossary_tgt_input.setText("投机采样")
                dp.on_add_glossary_term()
                self.assertEqual(len(dp.glossary_store.entries), 1)
                self.assertIn("(1)", dp.btn_toggle_glossary.text())

                # 测试语言切换会同步更新日常翻译 UI 文本
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("en"))
                QApplication.processEvents()
                self.assertEqual(window.btn_mode_daily.text(), "Daily Translate")
                self.assertIn("Glossary", dp.btn_toggle_glossary.text())
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("zh"))
                QApplication.processEvents()

                # 模拟 PDF 后台正在运行，日常翻译面板主动锁止避免竞争资源
                window.worker = MagicMock()
                window.worker.isRunning.return_value = True
                dp.sync_pdf_busy_state()
                self.assertFalse(dp.pdf_busy_banner.isHidden())
                self.assertFalse(dp.btn_translate.isEnabled())
                window.worker = None
                dp.sync_pdf_busy_state()
                self.assertTrue(dp.pdf_busy_banner.isHidden())
                self.assertTrue(dp.btn_translate.isEnabled())

                # 切回 PDF 模式
                window.switch_mode(0)
                QApplication.processEvents()
                self.assertEqual(window.mode_stack.currentIndex(), 0)
            finally:
                window.close()

    def test_daily_panel_mocked_translation_flow_and_history(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                window.switch_mode(1)
                dp = window.daily_panel
                dp.glossary_store.add_or_update("Hy-MT2", "混元翻译2")
                dp._refresh_glossary_list()

                mock_rt = StreamingMockRuntime(default_reply="混元翻译2 是一款高效的本地翻译模型。")
                with patch.object(dp.runtime_manager, "ensure_started", return_value=mock_rt), \
                     patch.object(dp.runtime_manager, "is_running", return_value=True):
                    dp._suppress_auto_translate = True
                    dp.source_edit.setPlainText("Hy-MT2 is an efficient local translation model.")
                    dp._suppress_auto_translate = False
                    dp.trigger_translation_now()
                    # 等待后台 QThread 结束并分发信号
                    self.assertIsNotNone(dp._worker)
                    dp._worker.wait(5000)
                    for _ in range(10):
                        QApplication.processEvents()

                self.assertIn("混元翻译2", dp.target_edit.toPlainText())
                self.assertFalse(dp.glossary_hit_btn.isHidden())
                self.assertEqual(dp.metrics_pill.text(), "翻译完成")
                self.assertTrue(dp.progress_bar.isHidden())
                self.assertNotIn("tok/s", dp.metrics_pill.text())
                self.assertEqual(len(dp.history_store.items), 1)
                self.assertIn("Hy-MT2 翻译成 混元翻译2", mock_rt.prompts[0])

                # 历史记录显示人类可读语言名称与“填入”按钮
                dp._toggle_drawer(1)
                QApplication.processEvents()
                hist_texts = [lbl.text() for lbl in dp.history_list_widget.findChildren(type(dp.detected_dir_pill))]
                self.assertTrue(any("英语 → 简体中文" in t for t in hist_texts))
                hist_btns = [b.text() for b in dp.history_list_widget.findChildren(type(dp.btn_translate))]
                self.assertIn("填入", hist_btns)
                self.assertNotIn("回填", hist_btns)
            finally:
                window.close()

    def test_minimal_low_cognitive_load_controls_and_settings_enforcement(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            settings_file = Path(data_dir) / "daily_settings.json"
            settings_file.write_text(
                json.dumps({"version": 1, "clipboard_watch": True, "auto_copy": True}),
                encoding="utf-8",
            )
            window = MainWindow()
            try:
                window.switch_mode(1)
                QApplication.processEvents()
                dp = window.daily_panel

                # 隐藏项必须保持隐藏且强制未勾选
                self.assertTrue(dp.btn_paste_translate.isHidden())
                self.assertTrue(dp.chk_clipboard_watch.isHidden())
                self.assertFalse(dp.chk_clipboard_watch.isChecked())
                self.assertTrue(dp.chk_auto_copy.isHidden())
                self.assertFalse(dp.chk_auto_copy.isChecked())
                self.assertTrue(dp.btn_unload_model.isHidden())
                self.assertTrue(dp.detected_dir_pill.isHidden())

                # 持久化配置必须强制保持 clipboard_watch=False, auto_copy=False
                dp._save_settings()
                saved = json.loads(settings_file.read_text(encoding="utf-8"))
                self.assertFalse(saved["clipboard_watch"])
                self.assertFalse(saved["auto_copy"])

                # 引擎状态与占位符必须极简无极客术语
                self.assertEqual(dp.engine_status_label.text(), "离线翻译 · 待命")
                for forbidden in ("Hy-MT2", "唤醒", "秒级响应", "零等待"):
                    self.assertNotIn(forbidden, dp.engine_status_label.text())
                src_ph = dp.source_edit.placeholderText()
                tgt_ph = dp.target_edit.placeholderText()
                for forbidden in ("TXT/MD", "代码块", "流式输出"):
                    self.assertNotIn(forbidden, src_ph + tgt_ph)
            finally:
                window.close()

    def test_translation_progress_percentage_status_and_i18n(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                window.switch_mode(1)
                dp = window.daily_panel
                dp._request_seq = 1

                # 1. 准备阶段
                with patch.object(dp.runtime_manager, "is_running", return_value=False):
                    dp._on_worker_status(1, "waking")
                self.assertEqual(dp.engine_status_label.text(), "正在准备翻译服务...")
                self.assertEqual(dp.metrics_pill.text(), "准备中...")
                self.assertFalse(dp.progress_bar.isHidden())

                # 2. 翻译输出中（仅显示百分比，不显示 tok/s 或 5/8）
                with patch.object(dp.runtime_manager, "is_running", return_value=True):
                    dp._on_worker_status(1, "translating")
                    dp._on_worker_partial(1, "部分译文", {"progress": 62, "tok_s": 28.4, "chunk": 5, "total_chunks": 8})
                self.assertEqual(dp.engine_status_label.text(), "离线翻译 · 已就绪")
                self.assertEqual(dp.metrics_pill.text(), "翻译输出中 62%")
                self.assertEqual(dp.progress_bar.value(), 62)
                self.assertFalse(dp.progress_bar.isHidden())
                self.assertNotIn("tok/s", dp.metrics_pill.text())
                self.assertNotIn("5/8", dp.metrics_pill.text())
                self.assertTrue(dp.btn_unload_model.isHidden())

                # 3. 切换界面语言至英文时，翻译状态与引擎状态由 retranslate 自动同步更新
                with patch.object(dp.runtime_manager, "is_running", return_value=True):
                    window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("en"))
                    QApplication.processEvents()
                self.assertEqual(dp.metrics_pill.text(), "Translating 62%")
                self.assertEqual(dp.engine_status_label.text(), "Offline translation · Ready")
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("zh"))
                QApplication.processEvents()
            finally:
                window.close()

    def test_streaming_auto_scroll_and_user_scroll_pause_resume(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QWheelEvent

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                window.resize(1040, 600)
                window.switch_mode(1)
                window.show()
                QApplication.processEvents()
                dp = window.daily_panel
                dp.target_edit.resize(360, 140)
                QApplication.processEvents()

                dp._request_seq = 1
                dp._target_auto_scroll = True
                dp._target_streaming = True

                # 1. 初始多行流式输出自动跟随到底部
                lines_35 = "\n".join(f"Line {i:02d}: translated content 🚀" for i in range(35))
                dp._on_worker_partial(1, lines_35, {"progress": 40})
                QApplication.processEvents()
                vbar = dp.target_edit.verticalScrollBar()
                self.assertGreater(vbar.maximum(), 0)
                self.assertEqual(vbar.value(), vbar.maximum())
                self.assertTrue(dp.btn_scroll_latest.isHidden())

                # 2. 用户主动向上滚动后，暂停自动跟随并显示「↓ 回到最新」
                wheel_up = QWheelEvent(
                    QPoint(10, 10),
                    QPoint(10, 10),
                    QPoint(0, 120),
                    QPoint(0, 120),
                    Qt.NoButton,
                    Qt.NoModifier,
                    Qt.ScrollUpdate,
                    False,
                )
                dp.target_edit.wheelEvent(wheel_up)
                vbar.setValue(3)
                QApplication.processEvents()
                self.assertFalse(dp._target_auto_scroll)
                self.assertFalse(dp.btn_scroll_latest.isHidden())
                self.assertEqual(dp.btn_scroll_latest.text(), "↓ 回到最新")

                # 3. 后续流式增量 + 含 Emoji 后缀修正 + 完成，均不得将用户视图拉回顶部或底部
                lines_45 = lines_35 + "\n" + "\n".join(f"Line {i:02d}: more output" for i in range(35, 45))
                dp._on_worker_partial(1, lines_45, {"progress": 75})
                QApplication.processEvents()
                self.assertEqual(vbar.value(), 3)

                # 含 Emoji 前缀替换测试
                lines_edited = lines_45 + "\nTail 🎉 [u1]"
                dp._on_worker_partial(1, lines_edited, {"progress": 85})
                QApplication.processEvents()
                self.assertEqual(vbar.value(), 3)
                lines_restored = lines_45 + "\nTail 🎉 https://example.com"
                dp._on_worker_partial(1, lines_restored, {"progress": 95})
                QApplication.processEvents()
                self.assertEqual(dp.target_edit.toPlainText(), lines_restored)
                self.assertEqual(vbar.value(), 3)

                # 4. 点击「↓ 回到最新」立即恢复到底部并自动跟随
                dp.btn_scroll_latest.click()
                QApplication.processEvents()
                self.assertTrue(dp._target_auto_scroll)
                self.assertEqual(vbar.value(), vbar.maximum())
                self.assertTrue(dp.btn_scroll_latest.isHidden())

                # 5. 完成时若用户再次滚到上方，完成后也保留位置且隐藏「↓ 回到最新」
                vbar.setValue(2)
                QApplication.processEvents()
                res = DailyTranslationResult(
                    translated_text=lines_restored + "\nFinal line",
                    detected_source_lang="en",
                    resolved_target_lang="zh",
                    matched_terms=[],
                    chunks_count=1,
                    elapsed_ms=100,
                    tokens_generated=50,
                    tokens_per_sec=10.0,
                )
                dp._on_worker_completed(1, res)
                QApplication.processEvents()
                self.assertEqual(vbar.value(), 2)
                self.assertTrue(dp.btn_scroll_latest.isHidden())
            finally:
                window.close()

    def test_input_limit_warning_blocking_trim_and_output_completeness_banner(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, _isolated_data_env(data_dir)):
            window = MainWindow()
            try:
                window.switch_mode(1)
                window.show()
                QApplication.processEvents()
                dp = window.daily_panel

                # 1. 输入超过 5000 字：显示红字提示、警告栏并禁用翻译按钮，不触发翻译
                dp._suppress_auto_translate = True
                dp.source_edit.setPlainText("A" * (DAILY_MAX_INPUT_CHARS + 25))
                dp._suppress_auto_translate = False
                QApplication.processEvents()

                self.assertEqual(dp.char_count_label.text(), f"{DAILY_MAX_INPUT_CHARS + 25} / {DAILY_MAX_INPUT_CHARS} 字")
                self.assertFalse(dp.input_limit_banner.isHidden())
                self.assertIn("25", dp.input_limit_label.text())
                self.assertFalse(dp.btn_translate.isEnabled())
                dp.trigger_translation_now()
                self.assertIsNone(dp._worker)

                # 2. 点击「保留前 5000 字」恢复正常并可翻译
                dp.chk_auto_translate.setChecked(False)
                dp.btn_trim_source.click()
                QApplication.processEvents()
                self.assertEqual(len(dp.source_edit.toPlainText()), DAILY_MAX_INPUT_CHARS)
                self.assertTrue(dp.input_limit_banner.isHidden())
                self.assertTrue(dp.btn_translate.isEnabled())

                # 3. 截断完成显示「输出可能不完整」及说明横幅
                dp._request_seq = 2
                res_trunc = DailyTranslationResult(
                    translated_text="截断的译文",
                    detected_source_lang="en",
                    resolved_target_lang="zh",
                    matched_terms=[],
                    chunks_count=1,
                    elapsed_ms=50,
                    tokens_generated=128,
                    tokens_per_sec=20.0,
                    truncated=True,
                    truncated_chunks=1,
                    completed_chunks=1,
                    truncation_reason="length",
                )
                dp._on_worker_completed(2, res_trunc)
                QApplication.processEvents()
                self.assertEqual(dp.metrics_pill.text(), "输出可能不完整")
                self.assertFalse(dp.output_notice_banner.isHidden())
                self.assertIn("长度限制", dp.output_notice_banner.text())
                self.assertEqual(dp.target_char_count_label.text(), "5 字")

                # 4. 切换英文语言同步刷新全部新增文案
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("en"))
                QApplication.processEvents()
                self.assertEqual(dp.char_count_label.text(), f"{DAILY_MAX_INPUT_CHARS} / {DAILY_MAX_INPUT_CHARS} chars")
                self.assertEqual(dp.metrics_pill.text(), "Output may be incomplete")
                self.assertIn("truncated", dp.output_notice_banner.text().lower())
                window.ui_language_combo.setCurrentIndex(window.ui_language_combo.findData("zh"))
                QApplication.processEvents()

                # 5. 手动停止保留已有部分输出并给出提示
                dp._request_seq = 3
                dp._on_worker_partial(3, "部分已完成译文", {"progress": 50})
                res_cancel = DailyTranslationResult(
                    translated_text="部分已完成译文",
                    detected_source_lang="en",
                    resolved_target_lang="zh",
                    matched_terms=[],
                    chunks_count=2,
                    elapsed_ms=40,
                    tokens_generated=20,
                    tokens_per_sec=10.0,
                    cancelled=True,
                )
                dp._on_worker_completed(3, res_cancel)
                QApplication.processEvents()
                self.assertEqual(dp.metrics_pill.text(), "已停止")
                self.assertFalse(dp.output_notice_banner.isHidden())
                self.assertIn("手动停止", dp.output_notice_banner.text())
                self.assertEqual(dp.target_edit.toPlainText(), "部分已完成译文")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
