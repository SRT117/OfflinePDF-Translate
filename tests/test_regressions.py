import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from core.translator import (
    HunyuanLocalTranslator,
    TranslationQualityError,
    clean_translated_candidate,
    pad_structural_tokens,
    sanitize_placeholders,
    recover_missing_placeholders,
    align_reference_newlines,
    remove_spurious_newlines,
)
from core.validator import PlaceholderValidator
from core.delivery import publish_pair, validate_pdf
from core.jobs import run_document_job, JobCancelled, JobFailed
from core.offline import (
    ensure_offline_assets_ready,
    OfflineAssetError,
    resolve_translation_model,
    validate_gguf_eos,
)


class ReplyRuntime:
    def __init__(self, content='', reason='stop', error=None):
        self.content, self.reason, self.error = content, reason, error
        self.prompts = []

    def chat_completion(self, **kwargs):
        self.prompts.append(kwargs['prompt'])
        if self.error:
            raise self.error
        return {'choices': [{'message': {'content': self.content}, 'finish_reason': self.reason}]}


def make_pdf(path, text='Verified output'):
    import pymupdf
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), text)
        doc.save(path)


class TranslationTests(unittest.TestCase):
    def test_language_selection(self):
        # 1. 非中文源语 (lang_in='en')：非中文目标语用英文官方名，中文/民族语系目标语用中文名
        for code, name in [('en', 'English'), ('ja', 'Japanese'), ('zh-Hant', '繁体中文'),
                           ('fr', 'French'), ('de', 'German'), ('ko', 'Korean'), ('zh', '简体中文')]:
            with self.subTest(lang_in='en', code=code):
                runtime = ReplyRuntime('Translated')
                HunyuanLocalTranslator(runtime, lang_in='en', lang_out=code).do_translate('Sample')
                self.assertIn(name, runtime.prompts[0])
        # 2. 中文源语 (lang_in='zh')：全部采用官方中文模板与中文语言全称
        for code, name in [('en', '英语'), ('ja', '日语'), ('fr', '法语'), ('de', '德语'), ('ko', '韩语')]:
            with self.subTest(lang_in='zh', code=code):
                runtime = ReplyRuntime('Translated')
                HunyuanLocalTranslator(runtime, lang_in='zh', lang_out=code).do_translate('示例')
                self.assertIn(name, runtime.prompts[0])

    def test_unknown_language_rejected(self):
        with self.assertRaises(ValueError):
            HunyuanLocalTranslator(ReplyRuntime(), lang_out='unknown')

    def test_invalid_output_never_returned(self):
        for content, reason in [('这是没有公式的中文译文', 'stop'),
                                ('额外公式 {{v1}} {{v2}} {{v3}}', 'stop'),
                                ('已经截断 {{v1}} {{v2}}', 'length'),
                                ('', 'stop')]:
            with self.subTest(content=content):
                translator = HunyuanLocalTranslator(ReplyRuntime(content, reason), max_retries=1)
                result = translator.do_translate('A {{v1}} and B {{v2}}.')
                self.assertIsInstance(result, str)
                self.assertEqual(len(translator.quality_issues), 1)

    def test_unchanged_english_not_counted_as_success(self):
        text = 'This method improves translation performance.'
        translator = HunyuanLocalTranslator(ReplyRuntime(text), max_retries=0)
        result = translator.do_translate(text)
        self.assertEqual(result, text)
        self.assertEqual(translator.stats['success_calls'], 0)

    def test_service_failure_not_returned_as_source(self):
        translator = HunyuanLocalTranslator(ReplyRuntime(error=ConnectionError('offline')), max_retries=1)
        result = translator.do_translate('This is a full paragraph.')
        self.assertEqual(result, 'This is a full paragraph.')
        self.assertEqual(len(translator.quality_issues), 1)

    def test_wrong_script_not_counted_as_translation(self):
        translator = HunyuanLocalTranslator(ReplyRuntime('A differently worded English sentence.'), max_retries=0)
        result = translator.do_translate('This is a full paragraph.')
        self.assertEqual(result, 'This is a full paragraph.')
        self.assertEqual(len(translator.quality_issues), 1)

    def test_retry_notification_does_not_exceed_max_retries(self):
        events = []
        translator = HunyuanLocalTranslator(
            ReplyRuntime('这是中文但缺少占位符'),
            max_retries=2,
            on_block_translated=lambda ev: events.append(ev),
        )
        translator.do_translate('Testing formula [v1] in paragraph.')
        retry_pending_attempts = [ev.get('attempt') for ev in events if ev.get('status') == 'retry_pending']
        self.assertNotIn(3, retry_pending_attempts)
        self.assertEqual(retry_pending_attempts, [1, 2])
        self.assertEqual(events[-1].get('status'), 'fallback_repaired')

    def test_exact_paragraph_cache_reuse(self):
        events = []
        runtime = ReplyRuntime("《应用物理杂志》文章发布于 aip.org/aip/jap")
        translator = HunyuanLocalTranslator(
            runtime,
            on_block_translated=lambda ev: events.append(ev),
        )
        header_text = "Journal of AppliedPhysics ARTICLE pubs.aip.org/aip/jap"
        # 第一次翻译（应调用底层模型推理）
        res1 = translator.do_translate(header_text)
        self.assertEqual(res1, "《应用物理杂志》文章发布于 aip.org/aip/jap")
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(translator.stats["cache_hits"], 0)

        # 第二次翻译相同内容（应命中缓存，0 次大模型调用）
        res2 = translator.do_translate(header_text)
        self.assertEqual(res2, "《应用物理杂志》文章发布于 aip.org/aip/jap")
        self.assertEqual(len(runtime.prompts), 1)  # prompt 依然只有 1 次！
        self.assertEqual(translator.stats["cache_hits"], 1)
        self.assertIn("命中完全重复内容缓存", events[-1]["reason"])

    def test_pad_structural_tokens(self):
        raw = "similarity[v1] (scale ratio)<8.3%.[v2] and <style id='1'>code</style>done"
        padded = pad_structural_tokens(raw)
        self.assertIn("similarity [v1] ", padded)
        self.assertIn("[v2] .", padded)
        self.assertIn(" <style id='1'>", padded)
        self.assertIn("</style> ", padded)

    def test_pad_structural_tokens_multi_tokens(self):
        raw = "water, [v1] , [v2] and other fluids. [v3] [v4] The next step"
        padded = pad_structural_tokens(raw)
        self.assertIn("[v1] [v2] ,", padded)
        self.assertIn("[v3] [v4] .", padded)

    def test_sanitize_placeholders_repairs_bracket_contamination(self):
        source = "Distance [v1] and factor [v2] were tested."
        candidate = "距离 [R/v1] 与系数 【v2】 经过了测试。"
        sanitized, stripped = sanitize_placeholders(source, candidate)
        self.assertEqual(sanitized, "距离 [v1] 与系数 [v2] 经过了测试。")
        self.assertEqual(stripped, [])

    def test_recover_missing_variable_subscript(self):
        source = "reaches critical bending moment M [v1] , next phase."
        candidate = "达到临界弯矩Ms时，进入下一阶段。"
        recovered = recover_missing_placeholders(source, candidate, ["[v1]"])
        self.assertIn("M [v1]", recovered)

    def test_pad_structural_tokens_removes_overbar_ligatures(self):
        raw = "replaced by ffiffiffiffiffiffix2 þ R2 and x (\x03L < x < L) and R/R0 ¼ 6"
        padded = pad_structural_tokens(raw)
        self.assertNotIn("ffiffi", padded)
        self.assertIn("x2 + R2", padded)
        self.assertIn("(-L < x < L)", padded)
        self.assertIn("R/R0 = 6", padded)

    def test_pad_structural_tokens_preserves_reference_newlines(self):
        raw = "[v1] Author A, Paper 1.\n[v2] Author B, Paper 2.\n[v3] Author C, Paper 3."
        padded = pad_structural_tokens(raw)
        self.assertIn("\n", padded)
        lines = padded.strip().splitlines()
        self.assertEqual(len(lines), 3)

    def test_single_line_source_cleans_candidate_newlines(self):
        source = (
            "thickness [v1] and reduced stiffener counts, [v2] which alter cross-sectional bending strength. "
            "Moreover, the larger the dimension is, the more distorted scale models tend to be. [v3] "
            "Meanwhile, the dynamic yield strength is affected by scaling coefficient. [v4]"
        )
        # 模拟大模型在单段正文中每遇到引用或标点就错误切行
        candidate = (
            "厚度 [v1] 以及减小的支撑件数量 [v2]，这些因素会改变截面弯曲强度。\n"
            "此外，实际船舶结构的尺寸越大，比例模型往往出现几何变形。\n"
            "[v3] 同时，比例模型与实际船舶的动态屈服强度受缩放系数的影响。\n"
            "[v4]"
        )
        aligned = align_reference_newlines(source, candidate)
        self.assertNotIn("\n", aligned)
        self.assertNotIn("\r", aligned)
        self.assertIn("截面弯曲强度。此外", aligned)
        self.assertIn("几何变形。 [v3] 同时", aligned)

    def test_remove_spurious_newlines(self):
        # 1. 中文标点/汉字之间换行完全消除
        cjk_text = "这是第一句。\n这是第二句。\n\n这是第三句。"
        self.assertEqual(remove_spurious_newlines(cjk_text), "这是第一句。这是第二句。这是第三句。")

        # 2. 西文单词之间换行折叠为单个空格
        en_text = "This is a sentence\nwith broken\n\nwords."
        self.assertEqual(remove_spurious_newlines(en_text), "This is a sentence with broken words.")

        # 3. 混排占位符换行平滑处理
        mixed_text = "变形。\n[v3] 同时"
        self.assertEqual(remove_spurious_newlines(mixed_text), "变形。 [v3] 同时")

    def test_align_reference_newlines(self):
        source = "[v1] Author A, Paper 1.\n[v2] Author B, Paper 2.\n[v3] Author C, Paper 3."
        candidate = "[v1] 作者A，论文1 [v2]。 作者B，论文2 [v3]。 作者C，论文3。"
        aligned = align_reference_newlines(source, candidate)
        lines = aligned.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[v1]"))
        self.assertTrue(lines[1].startswith("[v2]"))
        self.assertTrue(lines[2].startswith("[v3]"))
        self.assertNotIn("[v2]。", aligned)

    def test_align_reference_newlines_with_inline_symbol_offset(self):
        source = "[v1] Entry 1.\n[v2] Paper with AUSM[v3]scheme.\n[v4] Entry 3."
        # LLM dropped inline [v3], numbering the next entry as [v3] instead of [v4]
        candidate = "[v1] 条目1\n[v2] AUSM方案论文\n[v3] 条目3"
        aligned = align_reference_newlines(source, candidate)
        lines = aligned.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[v1]"))
        self.assertTrue(lines[1].startswith("[v2]"))
        self.assertTrue(lines[2].startswith("[v4]"))

    def test_recover_missing_placeholders_with_anchor_word(self):
        source = "[v1] Entry 1. [v2] Paper with AUSM [v3] scheme. [v4] Entry 3."
        candidate = "[v1] 条目1\n[v2] AUSM方案论文\n[v4] 条目3"
        recovered = recover_missing_placeholders(source, candidate, ["[v3]"])
        self.assertIn("AUSM [v3]", recovered)
        self.assertNotIn("条目3 [v3]", recovered)

    def test_clean_translated_candidate_strips_prompt_leak(self):
        leaked = (
            "【重要注意】 上一次翻译遗漏了以下占位符：['[v1]']！\n"
            "严禁翻译、删除或更改任何代码标签、变量占位符，必须原样完整保留所有占位符及其对应位置。请注意，只需输出翻译后的结果，不要额外解释：\n\n"
            "这是有效翻译正文。"
        )
        self.assertEqual(clean_translated_candidate(leaked), "这是有效翻译正文。")

    def test_clean_translated_candidate_single_line_leak(self):
        single_line_leak = (
            "严禁翻译或更改任何代码标签、变量占位符（如 [v1]、[v2] 等），必须原样保留其原有的格式与位置。请注意，只需输出翻译后的结果，不要额外解释。其中，e [v1] 为等效塑性应变"
        )
        self.assertEqual(clean_translated_candidate(single_line_leak), "其中，e [v1] 为等效塑性应变")

        colon_leak = "译文：这是有效翻译正文。"
        self.assertEqual(clean_translated_candidate(colon_leak), "这是有效翻译正文。")

        half_bracket_leak = "【待翻译文本]\n摘要"
        self.assertEqual(clean_translated_candidate(half_bracket_leak), "摘要")

    def test_short_heading_uses_title_only_background_and_rejects_expansion(self):
        class SeqRuntime:
            def __init__(self, replies):
                self.replies = list(replies)
                self.prompts = []
            def chat_completion(self, **kwargs):
                self.prompts.append(kwargs['prompt'])
                return {'choices': [{'message': {'content': self.replies.pop(0)}, 'finish_reason': 'stop'}]}

        full_bg = "Title: Underwater Explosion Study\n\nAbstract: The after flow plays an important role in ship damage across multiple near-field cases."
        # 1. Short heading gets only Title line, NOT the Abstract body
        rt1 = SeqRuntime(["摘要"])
        tr1 = HunyuanLocalTranslator(rt1)
        tr1.set_background(full_bg)
        self.assertEqual(tr1.do_translate("ABSTRACT"), "摘要")
        self.assertIn("Title: Underwater Explosion Study", rt1.prompts[0])
        self.assertNotIn("Abstract: The after flow", rt1.prompts[0])

        # 2. If short heading still bloats abnormally (>60 chars), retry strips background and recovers
        bloated = "【摘要】水下爆炸产生的后流对船体梁损伤具有重要作用，尤其是在近场爆炸中的多个实验工况条件下均表现出显著的非线性效应与能量输入。"
        rt2 = SeqRuntime([bloated, "摘要"])
        tr2 = HunyuanLocalTranslator(rt2, max_retries=1)
        tr2.set_background(full_bg)
        self.assertEqual(tr2.do_translate("ABSTRACT"), "摘要")
        self.assertEqual(tr2.stats["retry_calls"], 1)
        self.assertEqual(tr2.stats["fallback_calls"], 0)

    def test_prompt_leak_never_enters_translated_output(self):
        leaked_response = (
            "【重要注意】 占位符遗漏警告！\n"
            "请将以下文本准确翻译为简体中文：\n\n"
            "水下爆炸测试 [v1] 结果表明结构完好。"
        )
        runtime = ReplyRuntime(content=leaked_response)
        translator = HunyuanLocalTranslator(runtime, lang_out='zh', max_retries=0)
        res = translator.do_translate("Underwater test [v1] indicates intact structure.")
        self.assertEqual(res, "水下爆炸测试 [v1] 结果表明结构完好。")
        self.assertNotIn("【重要注意】", res)
        self.assertNotIn("请将以下文本", res)

    def test_valid_structural_translation(self):
        text = '这是对应公式 [v1] 的说明。'
        translator = HunyuanLocalTranslator(ReplyRuntime(text))
        self.assertEqual(translator.do_translate('Explanation of [v1].'), text)
        self.assertEqual(translator.quality_issues, [])

    def test_clean_translated_candidate_strips_japanese_and_english_leaks(self):
        ja_leak_1 = (
            "翻訳やコードタグ、変数のプレースホルダ（例：-5/3、-5/3 など）の変更は厳禁です。"
            "これらをそのまま完全に保持し、欠落させないようにし、相対的な位置にも注意してください。"
            "説明は不要で、翻訳後の結果のみを出力してください。 要約：大気乱流パラメータは重要です。"
        )
        self.assertEqual(clean_translated_candidate(ja_leak_1), "要約：大気乱流パラメータは重要です。")

        ja_leak_2 = "【翻訳対象テキスト】 大気境界層における主な動きの形態は乱流です。"
        self.assertEqual(clean_translated_candidate(ja_leak_2), "大気境界層における主な動きの形態は乱流です。")

        en_leak = "Translate the following segment into French, without additional explanation:\nLe résultat est valide."
        self.assertEqual(clean_translated_candidate(en_leak), "Le résultat est valide.")

    def test_remove_spurious_newlines_handles_japanese_kana(self):
        ja_text = "大気乱流の\nパラメータを\n直接測定する。"
        self.assertEqual(remove_spurious_newlines(ja_text), "大気乱流のパラメータを直接測定する。")

    def test_structural_prompt_includes_background_when_set(self):
        runtime = ReplyRuntime('这是带有背景的公式 [v1] 译文。')
        translator = HunyuanLocalTranslator(runtime)
        translator.set_background('Title: Underwater Explosion Study\n\nAbstract: Full-scale ship response.')
        translator.do_translate('The hull response [v1] was recorded.')
        prompt_used = runtime.prompts[0]
        self.assertIn('参考上面的信息', prompt_used)
        self.assertIn('Underwater Explosion Study', prompt_used)
        self.assertIn('[v1]', prompt_used)
        self.assertNotIn('【背景信息】', prompt_used)
        self.assertNotIn('【待翻译文本】', prompt_used)

    def test_auto_source_language_detection_and_inline_url_protection(self):
        from core.translator import detect_text_language, protect_inline_urls, restore_inline_urls
        self.assertEqual(detect_text_language("这是一段中文学术论文摘要。"), "zh")
        self.assertEqual(detect_text_language("これは日本語の論文です。"), "ja")
        self.assertEqual(detect_text_language("This is an English paragraph."), "en")

        # lang_in='auto': 中文源文本译为日语应走中文官方指令；英文源文本译为日语应走英文官方指令
        rt_zh = ReplyRuntime("これは中国語の要約です。")
        tr_auto_zh = HunyuanLocalTranslator(rt_zh, lang_in="auto", lang_out="ja")
        tr_auto_zh.do_translate("这是一段用于测试自动语言识别的中文论文正文内容。")
        self.assertIn("将以下文本翻译为 日语", rt_zh.prompts[0])

        rt_en = ReplyRuntime("これは英語の要約です。")
        tr_auto_en = HunyuanLocalTranslator(rt_en, lang_in="auto", lang_out="ja")
        tr_auto_en.do_translate("This is an English paragraph for testing auto language routing.")
        self.assertIn("Translate the following segment into Japanese", rt_en.prompts[0])

        # 混排段落内 URL/DOI 保护，防止模型改写路径或数字
        mixed = "Published at https://doi.org/10.5194/amt-17-1837-2024, © Authors."
        protected, mapping = protect_inline_urls(mixed)
        self.assertNotIn("doi.org", protected)
        self.assertIn("[u1]", protected)
        self.assertEqual(restore_inline_urls("Publié à [u1], © Auteurs.", mapping), "Publié à https://doi.org/10.5194/amt-17-1837-2024, © Auteurs.")

        rt_url = ReplyRuntime("Publié à [u1], © Auteurs.")
        tr_url = HunyuanLocalTranslator(rt_url, lang_in="en", lang_out="fr")
        out_url = tr_url.do_translate(mixed)
        self.assertIn("https://doi.org/10.5194/amt-17-1837-2024", out_url)

    def test_extended_language_detection_and_auto_to_english_suspect_check(self):
        from core.prompts import SUPPORTED_LANGUAGES
        from core.translator import detect_text_language

        # 1. 所有 UI 支持的语言代码均在底层语言映射表中
        for _, code in SUPPORTED_LANGUAGES:
            self.assertIn(code.lower(), HunyuanLocalTranslator.language_names)
            self.assertIn(code.lower(), HunyuanLocalTranslator.language_names_en)

        # 2. 民族语系与扩展脚本识别（确保 auto 模式下民族语系走中文官方提示词模板）
        self.assertEqual(detect_text_language("བོད་སྐད་ཀྱི་ཚོད་ལྟའི་ཡིག་ཆ།"), "bo")
        self.assertEqual(detect_text_language("ئۇيغۇرچە تەرجىمە سىنىقى"), "ug")
        self.assertEqual(detect_text_language("Қазақ тіліндегі ғылыми мақала"), "kk")
        self.assertEqual(detect_text_language("Українська наукова стаття"), "uk")
        self.assertEqual(detect_text_language("Đây là một đoạn văn tiếng Việt."), "vi")
        self.assertEqual(detect_text_language("Die Straße ist sehr groß."), "de")

        rt_bo = ReplyRuntime("This is a Tibetan test document.")
        tr_bo = HunyuanLocalTranslator(rt_bo, lang_in="auto", lang_out="en")
        tr_bo.do_translate("བོད་སྐད་ཀྱི་ཚོད་ལྟའི་ཡིག་ཆ།")
        self.assertIn("将以下文本翻译为 英语", rt_bo.prompts[0])

        # 3. auto -> en 场景下，非英语拉丁语文本若原样未翻译，不得因回退判为 en 而跳过漏译校验
        fr_text = "Cette méthode améliore la performance de traduction."
        tr_fr = HunyuanLocalTranslator(ReplyRuntime(fr_text), lang_in="auto", lang_out="en", max_retries=0)
        tr_fr.do_translate(fr_text)
        self.assertEqual(tr_fr.stats["success_calls"], 0)
        self.assertEqual(len(tr_fr.quality_issues), 1)

        de_ascii_text = "Die Methode verbessert das Ergebnis der Analyse."
        tr_de = HunyuanLocalTranslator(ReplyRuntime(de_ascii_text), lang_in="auto", lang_out="en", max_retries=0)
        tr_de.do_translate(de_ascii_text)
        self.assertEqual(tr_de.stats["success_calls"], 0)

        # 4. 泰语、印地语等扩展脚本作为源语言时，原样未译也应被漏译复检捕获
        th_text = "วิธีการนี้ช่วยปรับปรุงประสิทธิภาพการแปลเอกสาร"
        tr_th = HunyuanLocalTranslator(ReplyRuntime(th_text), lang_in="auto", lang_out="en", max_retries=0)
        tr_th.do_translate(th_text)
        self.assertEqual(tr_th.stats["success_calls"], 0)

    def test_non_chinese_pair_uses_english_official_prompt(self):
        runtime = ReplyRuntime('乱流パラメータ [v1] の結果。')
        translator = HunyuanLocalTranslator(runtime, lang_in='en', lang_out='ja')
        translator.set_background('Title: Underwater Explosion Study')
        translator.do_translate('The hull response [v1] was recorded.')
        prompt_used = runtime.prompts[0]
        self.assertIn('Refer to the above information', prompt_used)
        self.assertIn('into Japanese', prompt_used)
        self.assertNotIn('参考上面的信息', prompt_used)
        self.assertNotIn('日语', prompt_used)

    def test_brace_format_change_rejected(self):
        self.assertFalse(PlaceholderValidator.validate('A {{v1}}', '甲 {v1}').is_valid)

    def test_missing_finish_reason_rejected(self):
        self.assertFalse(PlaceholderValidator.validate('Hello', '你好', None).is_valid)

    def test_citation_punctuation_anchoring(self):
        raw = "whipping vibrations,[v4]and contact explosion (R/R[v5]) may cause rupture.[v6]However"
        padded = pad_structural_tokens(raw)
        self.assertIn("vibrations [v4] ,", padded)
        self.assertIn("rupture [v6] .", padded)

    def test_variable_definition_punctuation_never_swapped(self):
        raw = "where [v1]is the frequency, [v2]is the Kolmogorov constant,[v3]is the dissipation rate, and [v4]is the power-law exponent."
        padded = pad_structural_tokens(raw)
        self.assertIn("[v2] is the Kolmogorov", padded)
        self.assertIn("[v3] is the dissipation", padded)
        self.assertNotIn("frequency [v2] ,", padded)

    def test_standalone_url_bypasses_model_and_never_flagged_suspect(self):
        runtime = ReplyRuntime("SHOULD_NOT_BE_CALLED")
        translator = HunyuanLocalTranslator(runtime)
        res = translator.do_translate("https://doi.org/10.5194/amt-17-1837-2024")
        self.assertEqual(res, "https://doi.org/10.5194/amt-17-1837-2024")
        self.assertEqual(len(runtime.prompts), 0)
        self.assertEqual(len(translator.quality_issues), 0)

    def test_recover_missing_placeholders_anchoring(self):
        from core.translator import recover_missing_placeholders
        src = "Far-field explosions cause vibrations,[v4] while near-field cause distortion.[v9] Finally rupture occurs."
        tgt = "远场爆炸引起振动。近场爆炸导致变形。最终发生破裂。"
        recovered = recover_missing_placeholders(src, tgt, ["[v4]", "[v9]"])
        self.assertIn("[v4]", recovered)
        self.assertIn("[v9]", recovered)
        # Verify [v4] is anchored near the first sentence, NOT tacked to the very end of the paragraph
        self.assertFalse(recovered.endswith("[v4]"))
        self.assertFalse(recovered.endswith("[v4] [v9]"))

    def test_hyphenation_normalization(self):
        raw = "dis- tance decreases. Experi-\nmental results show dam- age."
        padded = pad_structural_tokens(raw)
        self.assertIn("distance", padded)
        self.assertIn("Experimental", padded)
        self.assertIn("damage", padded)


class DeliveryTests(unittest.TestCase):
    def test_empty_delivery_never_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(RuntimeError):
                publish_pair({}, root / 'input.pdf')
            self.assertFalse((root / 'input.translated.pdf').exists())

    def test_single_pdf_is_not_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_pdf(root / 'mono.pdf')
            with self.assertRaises(RuntimeError):
                publish_pair({'mono_pdf_path': root / 'mono.pdf'}, root / 'input.pdf')
            self.assertFalse((root / 'input.translated.pdf').exists())

    def test_corrupt_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'broken.pdf'
            path.write_bytes(b'not a pdf')
            with self.assertRaises(Exception):
                validate_pdf(path)

    def test_publish_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ['mono', 'dual']:
                make_pdf(root / f'{name}.pdf')
            result = {f'{name}_pdf_path': root / f'{name}.pdf' for name in ['mono', 'dual']}
            dest_dual = root / 'input.bilingual.pdf'
            dest_mono = root / 'input.translated.pdf'
            dest_dual.write_bytes(b'previous dual')
            dest_mono.write_bytes(b'previous mono')
            original_replace = os.replace
            def fail_second(source, target):
                if Path(source).name == '1.pdf':
                    raise OSError('simulated disk failure')
                return original_replace(source, target)
            with patch('core.delivery.os.replace', side_effect=fail_second):
                with self.assertRaises(OSError):
                    publish_pair(result, root / 'input.pdf')
            self.assertEqual(dest_dual.read_bytes(), b'previous dual')
            self.assertEqual(dest_mono.read_bytes(), b'previous mono')
            published = publish_pair(result, root / 'input.pdf')
            for value in published.values():
                self.assertEqual(validate_pdf(value), 1)


class OfflineTests(unittest.TestCase):
    def test_network_guard_blocks_external_dns_and_connections(self):
        code = '''
from core.offline import restrict_network_to_loopback
import socket
restrict_network_to_loopback()
for action in [lambda: socket.getaddrinfo('example.com', 443),
               lambda: socket.create_connection(('8.8.8.8', 443))]:
    try: action()
    except OSError as e: assert '离线模式' in str(e)
    else: raise AssertionError('external network allowed')
assert socket.getaddrinfo('127.0.0.1', 1234)
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_bundle_fails_without_download(self):
        with tempfile.TemporaryDirectory() as td:
            with patch('babeldoc.assets.assets.get_fastest_upstream_for_font', side_effect=AssertionError('network used')):
                with self.assertRaises(OfflineAssetError):
                    ensure_offline_assets_ready(td, Path(td) / 'cache')

    def test_corrupt_or_incomplete_model_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'assets').mkdir()
            (root / 'models').mkdir()
            (root / 'assets/model_manifest.json').write_text(json.dumps({'name': 'model.gguf', 'sha3_256': 'bad'}))
            (root / 'models/model.gguf').write_bytes(b'GGUF')
            with self.assertRaises(OfflineAssetError):
                ensure_offline_assets_ready(root, root / 'cache')

    def test_resolve_translation_model_uses_manifest_or_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'assets').mkdir()
            (root / 'models').mkdir()
            q5 = root / 'models/Hy-MT2-1.8B.Q5_K_M.gguf'
            q5.write_bytes(b'GGUF' + b'\x00' * (1024 * 1024))
            self.assertEqual(resolve_translation_model(root), q5)
            (root / 'assets/model_manifest.json').write_text(json.dumps({'name': q5.name, 'sha3_256': 'x'}))
            self.assertEqual(resolve_translation_model(root), q5)

    def test_validate_gguf_eos_rejects_broken_hunyuan_eos(self):
        import struct
        def build_gguf(eos_id: int) -> bytes:
            def w_str(s: str) -> bytes:
                b = s.encode('utf-8')
                return struct.pack('<Q', len(b)) + b
            tokens = [f't{i}' for i in range(120021)]
            tokens[120020] = '<｜hy_place▁holder▁no▁2｜>'
            buf = bytearray(b'GGUF' + struct.pack('<IQQ', 3, 0, 3))
            buf += w_str('general.architecture') + struct.pack('<I', 8) + w_str('hunyuan-dense')
            buf += w_str('tokenizer.ggml.eos_token_id') + struct.pack('<I', 4) + struct.pack('<I', eos_id)
            buf += w_str('tokenizer.ggml.tokens') + struct.pack('<IIQ', 9, 8, len(tokens))
            for tok in tokens:
                buf += w_str(tok)
            return bytes(buf)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / 'bad.gguf'
            bad.write_bytes(build_gguf(3))
            with self.assertRaisesRegex(OfflineAssetError, 'eos_token_id=3'):
                validate_gguf_eos(bad)
            good = Path(td) / 'good.gguf'
            good.write_bytes(build_gguf(120020))
            validate_gguf_eos(good)

    def test_ensure_offline_assets_ready_refreshes_updated_bundled_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'assets' / 'fonts').mkdir(parents=True)
            (root / 'models').mkdir()
            (root / 'assets' / 'model_manifest.json').write_text(json.dumps({'name': 'model.gguf'}))
            (root / 'models' / 'model.gguf').write_bytes(b'GGUF' + b'\x00' * (1024 * 1024))
            bundled_font = root / 'assets' / 'fonts' / 'demo.ttf'
            bundled_font.write_bytes(b'v1-font')
            cache_base = root / 'cache'
            fake_required = {'fonts': [{'name': 'demo.ttf'}]}
            with patch('babeldoc.assets.assets.generate_all_assets_file_list', return_value=fake_required):
                ensure_offline_assets_ready(root, cache_base, force_verify=True)
                cached_font = cache_base / 'fonts' / 'demo.ttf'
                self.assertEqual(cached_font.read_bytes(), b'v1-font')

                # Simulate an upgraded bundle replacing the asset with new bytes & mtime
                bundled_font.write_bytes(b'v2-updated-font')
                os.utime(bundled_font, (time.time() + 10, time.time() + 10))
                ensure_offline_assets_ready(root, cache_base, force_verify=True)
                self.assertEqual(cached_font.read_bytes(), b'v2-updated-font')


class JobRuntime:
    model_path = 'model'
    runtime_bin = 'server'
    threads, context_size, parallel, n_gpu_layers, port = 1, 4096, 1, 0, 12345
    log_path = 'test.log'
    def __init__(self): self.stopped = False
    def stop(self): self.stopped = True


def stalled_job(channel, kwargs, options):
    time.sleep(30)


def crashing_job(channel, kwargs, options):
    os._exit(7)


def finished_job(channel, kwargs, options):
    channel.put(('progress', {'type': 'progress_update', 'overall_progress': 50}))
    channel.put(('result', {'result': 'ok'}))


def job_with_helper(channel, kwargs, options):
    helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    channel.put(('progress', {'helper_pid': helper.pid}))
    time.sleep(30)


class ProcessTests(unittest.TestCase):
    def test_cancel_terminates_helpers_too(self):
        import psutil
        helpers = []
        with self.assertRaises(JobCancelled):
            run_document_job(JobRuntime(), 'input', 'output', target=job_with_helper,
                             progress_callback=lambda event: helpers.append(event['helper_pid']),
                             cancelled=lambda: bool(helpers))
        self.assertEqual(len(helpers), 1)
        deadline = time.monotonic() + 2
        while psutil.pid_exists(helpers[0]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(psutil.pid_exists(helpers[0]))

    def test_watchdog_kills_stalled_document(self):
        runtime = JobRuntime()
        with self.assertRaises(JobFailed):
            run_document_job(runtime, 'input', 'output', inactivity_limit=1, target=stalled_job)
        self.assertTrue(runtime.stopped)

    def test_cancel_kills_stalled_document(self):
        started = time.monotonic()
        with self.assertRaises(JobCancelled):
            run_document_job(JobRuntime(), 'input', 'output',
                             cancelled=lambda: time.monotonic() - started > 0.3, target=stalled_job)
        self.assertLess(time.monotonic() - started, 5)

    def test_crash_not_success(self):
        with self.assertRaises(JobFailed):
            run_document_job(JobRuntime(), 'input', 'output', target=crashing_job)

    def test_progress_and_result(self):
        progress = []
        result = run_document_job(JobRuntime(), 'input', 'output',
                                  progress_callback=progress.append, target=finished_job)
        self.assertEqual(result, {'result': 'ok'})
        self.assertEqual(progress[0]['overall_progress'], 50)

    def test_posix_process_tree_close_ignores_permission_error(self):
        from core import process_tree
        with patch.object(process_tree.sys, 'platform', 'linux'), \
             patch.object(process_tree.signal, 'SIGKILL', 9, create=True), \
             patch.object(process_tree.os, 'killpg', side_effect=PermissionError('race'), create=True):
            tree = process_tree.ProcessTree(999999)
            tree.close()

    def test_cross_platform_user_data_dir_and_llama_path_lookup(self):
        from core import offline, llama_runtime
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            xdg = home / 'xdg_data'
            with patch.object(offline.sys, 'platform', 'linux'), \
                 patch.object(offline.Path, 'home', return_value=home), \
                 patch.dict(offline.os.environ, {'XDG_DATA_HOME': str(xdg)}, clear=True):
                self.assertEqual(offline.user_data_dir(), xdg / 'OfflinePDF Translate')

            with patch.object(offline.sys, 'platform', 'darwin'), \
                 patch.object(offline.Path, 'home', return_value=home), \
                 patch.dict(offline.os.environ, {}, clear=True):
                self.assertEqual(offline.user_data_dir(), home / 'Library' / 'Application Support' / 'OfflinePDF Translate')

            model_file = home / 'm.gguf'
            model_file.write_bytes(b'GGUF')
            fake_bin = home / 'llama-server'
            fake_bin.write_bytes(b'')
            with patch.object(llama_runtime.sys, 'platform', 'linux'), \
                 patch.object(llama_runtime.shutil, 'which', return_value=str(fake_bin)):
                rt = llama_runtime.LlamaServerRuntime(model_path=model_file, log_path=home / 'rt.log')
                self.assertEqual(rt.runtime_bin, fake_bin.resolve())


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, root, events, issues=None):
        from core import engine
        async def stream(config):
            for event in events:
                yield event
        translator = SimpleNamespace(stats={}, quality_issues=issues or [], set_background=lambda text: None)
        original_exists = Path.exists
        def model_exists(path):
            return path.name == 'doclayout_yolo_docstructbench_imgsz1024.onnx' or original_exists(path)
        with patch.object(Path, 'exists', model_exists), \
             patch.object(engine, 'ensure_offline_assets_ready'), \
             patch.object(engine, 'restrict_network_to_loopback'), \
             patch.object(engine, 'extract_document_background', return_value=''), \
             patch('babeldoc.docvision.doclayout.OnnxModel'), \
             patch.object(engine, 'TranslationConfig'), \
             patch.object(engine, 'HunyuanLocalTranslator', return_value=translator), \
             patch('babeldoc.format.pdf.high_level.async_translate', stream):
            return await engine.translate_document_async(root / 'input.pdf', root, SimpleNamespace(parallel=1))

    async def test_no_finish_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, 'finish'):
                await self.execute(Path(td), [])

    async def test_swallowed_paragraph_failure_blocks_document(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_pdf(root / 'mono.pdf')
            make_pdf(root / 'dual.pdf')
            res = await self.execute(
                root,
                [{'type': 'finish', 'translate_result': {'mono_pdf_path': root / 'mono.pdf', 'dual_pdf_path': root / 'dual.pdf'}}],
                [{'reason': 'bad placeholder'}]
            )
            self.assertEqual(res['status'], 'success')
            self.assertFalse((root / 'quality_issues.json').exists())

    async def test_finish_without_pdfs_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, '交付物'):
                await self.execute(Path(td), [{'type': 'finish', 'translate_result': {}}])

    async def test_encrypted_pdf_fails_before_translation(self):
        import pymupdf
        from core import engine
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / 'secret.pdf'
            with pymupdf.open() as doc:
                doc.new_page().insert_text((50, 50), 'Classified')
                doc.save(pdf_path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='user')
            with patch.object(engine, 'ensure_offline_assets_ready'), \
                 patch.object(engine, 'restrict_network_to_loopback'):
                with self.assertRaisesRegex(RuntimeError, '加密'):
                    await engine.translate_document_async(pdf_path, Path(td), SimpleNamespace(parallel=1))

    async def test_scanned_pdf_without_text_fails_with_clear_message(self):
        import pymupdf
        from core import engine
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / 'scanned.pdf'
            with pymupdf.open() as doc:
                doc.new_page()  # No extractable text layer, matching image-only scanned PDFs
                doc.save(pdf_path)
            with patch.object(engine, 'ensure_offline_assets_ready'), \
                 patch.object(engine, 'restrict_network_to_loopback'):
                with self.assertRaisesRegex(RuntimeError, '不支持扫描版'):
                    await engine.translate_document_async(pdf_path, Path(td), SimpleNamespace(parallel=1))

    async def test_no_paragraphs_error_maps_to_scanned_pdf_message(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, '不支持扫描版'):
                await self.execute(Path(td), [{'type': 'error', 'error': 'The document contains no paragraphs.'}])


class InstallerTests(unittest.TestCase):
    def _section_text(self, iss_text: str, section: str) -> str:
        marker = f'[{section}]'
        start = iss_text.index(marker) + len(marker)
        rest = iss_text[start:]
        next_idx = len(rest)
        for line_start in range(len(rest)):
            if (line_start == 0 or rest[line_start - 1] == '\n') and rest[line_start:].startswith('['):
                next_idx = line_start
                break
        return rest[:next_idx]

    def test_uninstaller_cleans_user_data_registry_and_temp_without_prompt(self):
        iss_path = Path(__file__).resolve().parents[1] / 'scripts' / 'installer.iss'
        iss_text = iss_path.read_text(encoding='utf-8')

        uninstall_delete = self._section_text(iss_text, 'UninstallDelete')
        for expected in (
            r'{app}\__pycache__',
            r'{app}\var',
            r'{app}\logs',
            r'{app}\tmp',
            r'Type: dirifempty; Name: "{app}"',
            r'{localappdata}\OfflinePDF Translate',
        ):
            self.assertIn(expected, uninstall_delete)

        install_delete = self._section_text(iss_text, 'InstallDelete')
        self.assertNotIn(r'{localappdata}\OfflinePDF Translate', install_delete)

        registry = self._section_text(iss_text, 'Registry')
        self.assertIn(r'Subkey: "Software\OfflinePDF"; Flags: dontcreatekey uninsdeletekey', registry)

        code = self._section_text(iss_text, 'Code')
        self.assertIn('usPostUninstall', code)
        self.assertIn('CleanResidualAppState', code)
        self.assertIn("'offlinepdf_stage_'", code)
        self.assertIn("'offlinepdf_cli_'", code)


if __name__ == '__main__':
    unittest.main()
