"""单元测试与验证套件：占位符校验、背景提取、硬件画像、指令模板与扫描件检测。"""

from pathlib import Path
import sys
import unittest
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.validator import PlaceholderValidator
from core.prompts import (
    prompt_structural_with_background,
)
from core.engine import extract_document_background, truncate_to_complete_sentences
from core.hardware import detect_hardware_profile


class TestPlaceholderValidator(unittest.TestCase):
    def test_normal_placeholders_preserved(self):
        src = "As shown in Eq. {{v1}}, the value is {{v2}}."
        tgt = "如公式 {{v1}} 所示，其值为 {{v2}}。"
        res = PlaceholderValidator.validate(src, tgt)
        self.assertTrue(res.is_valid)

    def test_natural_order_swap_accepted(self):
        src = "Jia et al. {{v1}} and Li et al. {{v2}} proposed the method."
        tgt = "Li 等人 {{v2}} 和 Jia 等人 {{v1}} 提出了该方法。"
        res = PlaceholderValidator.validate(src, tgt)
        self.assertTrue(res.is_valid)

    def test_missing_placeholder_rejected(self):
        src = "Jia{{v4}} presented the theory."
        tgt = "贾提出了该理论。"
        res = PlaceholderValidator.validate(src, tgt)
        self.assertFalse(res.is_valid)
        self.assertIn("缺失占位符", res.reason)

    def test_extra_placeholder_rejected(self):
        src = "The theory is sound."
        tgt = "该理论是合理的。 {{v1}}"
        res = PlaceholderValidator.validate(src, tgt)
        self.assertFalse(res.is_valid)
        self.assertIn("多余/幻觉占位符", res.reason)

    def test_empty_translation_rejected(self):
        src = "Some important text."
        tgt = ""
        res = PlaceholderValidator.validate(src, tgt)
        self.assertFalse(res.is_valid)
        self.assertIn("译文输出为空", res.reason)

    def test_truncated_output_rejected(self):
        src = "A long academic paragraph."
        tgt = "一个很长但被截断的学术段"
        res = PlaceholderValidator.validate(src, tgt, finish_reason="length")
        self.assertFalse(res.is_valid)
        self.assertIn("截断", res.reason)

    def test_unclosed_style_tag_rejected(self):
        src = "According to <style id='0'>Vaswani et al.</style> {{v1}}"
        tgt = "根据 <style id='0'>Vaswani 等人 {{v1}}"
        res = PlaceholderValidator.validate(src, tgt)
        self.assertFalse(res.is_valid)
        self.assertTrue("缺失占位符" in res.reason or "未配对闭合" in res.reason)

    def test_standalone_formula_detection(self):
        self.assertTrue(PlaceholderValidator.is_standalone_placeholder("{{v1}}"))
        self.assertTrue(PlaceholderValidator.is_standalone_placeholder("^{{v2}}."))
        self.assertTrue(PlaceholderValidator.is_standalone_placeholder("  {{v3}}  \n"))
        self.assertFalse(PlaceholderValidator.is_standalone_placeholder("Eq. {{v1}} shows"))


class TestBackgroundExtraction(unittest.TestCase):
    def test_attention_paper_background(self):
        pdf_path = ROOT / "data/attention-is-all-you-need.pdf"
        if not pdf_path.exists():
            import pymupdf
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            pdf_path = Path(temp.name) / 'attention.pdf'
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((50, 60), 'Attention Is All You Need', fontsize=20)
                page.insert_text((50, 100), 'author@example.com', fontsize=9)
                page.insert_text((50, 140), 'Abstract', fontsize=12)
                page.insert_textbox(pymupdf.Rect(50, 170, 500, 300),
                    'The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.', fontsize=11)
                doc.save(pdf_path)
        bg = extract_document_background(pdf_path)
        self.assertTrue(len(bg) > 0)
        self.assertIn("Title: Attention Is All You Need", bg)
        self.assertIn("Abstract:", bg)
        self.assertNotIn("@", bg)
        self.assertNotIn("permission", bg.lower())
        self.assertNotIn("google hereby", bg.lower())

    def test_truncate_to_complete_sentences_punctuation_integrity(self):
        long_text = (
            "Sentence one discusses the bubble expansion. "
            "Sentence two analyzes the pressure field studied by Li et al. under near-field explosion. "
            "Sentence three provides experimental validation for structural deformation. "
            "Sentence four concludes the paper with valuable recommendations."
        )
        # 限制词数为 25 词，应在第 2 句完整结束处截断，绝不可在第 3 句内部截断，更不可在 Li et al. 处误截断
        truncated = truncate_to_complete_sentences(long_text, max_words=25)
        self.assertTrue(truncated.endswith("."), f"截断结尾必须为句末标点: {truncated}")
        self.assertIn("under near-field explosion.", truncated)
        self.assertNotIn("Sentence three", truncated)
        self.assertLessEqual(len(truncated.split()), 25)

    def test_truncate_multilingual_and_fallback(self):
        # 1. 希腊语问号 (;)
        gk = "Τι είναι αυτό; Αυτό είναι ένα πείραμα. Τρίτη πρόταση."
        self.assertEqual(truncate_to_complete_sentences(gk, max_words=3), "Τι είναι αυτό;")

        # 2. 印地语 (Danda ।)
        hi = "यह एक अध्ययन है। अगला चरण शुरू होता है।"
        self.assertTrue(truncate_to_complete_sentences(hi, max_words=5).endswith("।"))

        # 3. 无标点硬截断保底 (fallback to word cut)
        no_punct = "word1 word2 word3 word4 word5 word6 word7"
        self.assertEqual(truncate_to_complete_sentences(no_punct, max_words=4), "word1 word2 word3 word4")

    def test_background_constrained_within_400_words(self):
        import pymupdf
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pdf_path = Path(temp.name) / "long_abstract.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((50, 50), "Very Long Paper Title", fontsize=18)
            page.insert_text((50, 80), "Abstract", fontsize=12)
            # 生成长摘要（由多个独立完整句子组成，使用小号字确保容纳）
            long_abs = " ".join([
                f"Sentence {i} examines the underwater shockwave dynamics and fluid structure interaction."
                for i in range(1, 40)
            ])
            page.insert_textbox(pymupdf.Rect(50, 100, 550, 750), long_abs, fontsize=7)
            doc.save(pdf_path)

        bg = extract_document_background(pdf_path, max_words=400)
        words = bg.split()
        self.assertLessEqual(len(words), 400)
        self.assertIn("Title: Very Long Paper Title", bg)
        self.assertIn("Abstract:", bg)
        # 确保截断后的摘要末尾为句末标点，绝不在句子内部切断
        self.assertTrue(bg.strip()[-1] in ".!?。！？", f"背景结尾必须是完整句尾标点: {bg[-50:]}")

    def test_book_pdf_skips_copyright_and_avoids_fake_abstract(self):
        import pymupdf
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pdf_path = Path(temp.name) / "book.pdf"
        with pymupdf.open() as doc:
            cover = doc.new_page()
            cover.insert_text((50, 120), "Underwater Explosion Dynamics", fontsize=22)
            p2 = doc.new_page()
            p2.insert_text((50, 60), "Copyright 2026 Publisher. All rights reserved. ISBN 978-0-123456-78-9.", fontsize=10)
            p3 = doc.new_page()
            p3.insert_text((50, 60), "Preface", fontsize=14)
            p3.insert_textbox(pymupdf.Rect(50, 90, 500, 300),
                              "This book introduces fundamental concepts across twelve chapters for graduate students and engineers.", fontsize=11)
            doc.save(pdf_path)

        bg = extract_document_background(pdf_path)
        self.assertIn("Title: Underwater Explosion Dynamics", bg)
        self.assertNotIn("Abstract:", bg)

    def test_cjk_spaceless_title_extracted_for_background(self):
        import pymupdf
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pdf_path = Path(temp.name) / "zh_paper.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((50, 60), "基于多普勒激光雷达的大气湍流直接测量", fontname="china-s", fontsize=18)
            page.insert_text((50, 100), "摘要：本文研究了相干多普勒激光雷达在大气边界层中的应用。", fontname="china-s", fontsize=11)
            doc.save(pdf_path)

        bg = extract_document_background(pdf_path)
        self.assertIn("基于多普勒激光雷达的大气湍流直接测量", bg)

    def test_ppt_pdf_extracts_title_without_fake_abstract(self):
        import pymupdf
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pdf_path = Path(temp.name) / "slides.pdf"
        with pymupdf.open() as doc:
            s1 = doc.new_page(width=960, height=540)
            s1.insert_text((80, 180), "Quarterly Ship Hydrodynamics Review", fontsize=28)
            s1.insert_text((80, 240), "Marine Engineering Team", fontsize=14)
            s2 = doc.new_page(width=960, height=540)
            s2.insert_text((60, 80), "Agenda", fontsize=20)
            s2.insert_text((80, 140), "- Experimental setup and shock tube calibration results across test cases.", fontsize=14)
            doc.save(pdf_path)

        bg = extract_document_background(pdf_path)
        self.assertIn("Title: Quarterly Ship Hydrodynamics Review", bg)
        self.assertNotIn("Abstract:", bg)


class TestHardwareProfile(unittest.TestCase):
    def test_hardware_detection(self):
        prof = detect_hardware_profile()
        self.assertGreaterEqual(prof.threads, 1)
        self.assertGreaterEqual(prof.parallel, 1)
        self.assertIn(prof.context_size, [4096, 8192])
        self.assertTrue(len(prof.device_summary) > 0)

    def test_cpu_only_runtime_downgrades_gpu_and_respects_ram(self):
        from unittest.mock import patch
        from core import hardware
        with patch.object(hardware, "_detect_cpu_count", return_value=8), \
             patch.object(hardware, "_detect_memory_gb", return_value=(8.0, 3.5)), \
             patch.object(hardware, "_detect_raw_gpu", return_value=(True, "NVIDIA GeForce RTX 4060", 8.0, "nvidia")), \
             patch.object(hardware, "detect_runtime_gpu_backends", return_value=set()), \
             patch.object(hardware, "_resolve_model_size_gb", return_value=1.5):
            prof = hardware.detect_hardware_profile()
        self.assertFalse(prof.has_gpu)
        self.assertEqual(prof.n_gpu_layers, 0)
        self.assertIn("CPU 推理", prof.gpu_name)
        self.assertEqual(prof.context_size, 4096)
        self.assertEqual(prof.parallel, 1)

    def test_cuda_runtime_enables_gpu_offload_with_ram_guard(self):
        from unittest.mock import patch
        from core import hardware
        with patch.object(hardware, "_detect_cpu_count", return_value=16), \
             patch.object(hardware, "_detect_memory_gb", return_value=(32.0, 16.0)), \
             patch.object(hardware, "_detect_raw_gpu", return_value=(True, "NVIDIA GeForce RTX 3090", 24.0, "nvidia")), \
             patch.object(hardware, "detect_runtime_gpu_backends", return_value={"cuda"}), \
             patch.object(hardware, "_resolve_model_size_gb", return_value=1.5):
            prof = hardware.detect_hardware_profile()
        self.assertTrue(prof.has_gpu)
        self.assertEqual(prof.n_gpu_layers, 99)
        self.assertEqual(prof.parallel, 4)
        self.assertEqual(prof.context_size, 8192)

    def test_darwin_apple_silicon_memory_and_metal(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from core import hardware
        fake_vm = SimpleNamespace(total=32 * 1024**3, available=20 * 1024**3)
        with patch.object(hardware.sys, "platform", "darwin"), \
             patch.object(hardware.platform, "machine", return_value="arm64"), \
             patch("psutil.virtual_memory", return_value=fake_vm), \
             patch.object(hardware.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="Apple M3 Pro\n")), \
             patch.object(hardware, "detect_runtime_gpu_backends", return_value={"metal"}), \
             patch.object(hardware, "_resolve_model_size_gb", return_value=1.5):
            hardware._DETECTED_RAW_GPU = None
            hardware._DETECTED_GPU = None
            try:
                prof = hardware.detect_hardware_profile()
            finally:
                hardware._DETECTED_RAW_GPU = None
                hardware._DETECTED_GPU = None
        self.assertEqual(prof.total_ram_gb, 32.0)
        self.assertEqual(prof.available_ram_gb, 20.0)
        self.assertTrue(prof.has_gpu)
        self.assertEqual(prof.n_gpu_layers, 99)
        self.assertIn("Metal", prof.gpu_name)
        self.assertEqual(prof.context_size, 8192)

    def test_nvidia_smi_picks_max_vram_and_hides_window(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from core import hardware
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="Card A, 4096\nCard B, 12288\n")

        with patch.object(hardware.shutil, "which", return_value="nvidia-smi"), \
             patch.object(hardware.os.path, "isfile", return_value=True), \
             patch.object(hardware.subprocess, "run", side_effect=fake_run):
            res = hardware._detect_nvidia_smi_gpu()
        self.assertEqual(res, (True, "Card B", 12.0, "nvidia"))
        if sys.platform == "win32":
            import subprocess
            self.assertEqual(captured.get("creationflags"), subprocess.CREATE_NO_WINDOW)

    def test_detect_runtime_gpu_backends_modular_dir(self):
        from core import hardware
        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td)
            rt_bin = rt_dir / "llama-server.exe"
            rt_bin.write_bytes(b"")
            (rt_dir / "ggml-cpu-x64.dll").write_bytes(b"")
            self.assertEqual(hardware.detect_runtime_gpu_backends(rt_bin), set())
            (rt_dir / "ggml-cuda.dll").write_bytes(b"")
            self.assertEqual(hardware.detect_runtime_gpu_backends(rt_bin), {"cuda"})

    def test_linux_affinity_cpu_count(self):
        from unittest.mock import patch
        from core import hardware
        with patch.object(hardware.os, "sched_getaffinity", return_value={0, 1, 2, 3}, create=True):
            self.assertEqual(hardware._detect_cpu_count(), 4)

    def test_cli_accepts_gpu_layers_override(self):
        import cli
        from unittest.mock import patch
        with patch.object(sys, "argv", ["cli.py", "translate", "paper.pdf", "--gpu-layers", "0"]):
            with patch.object(cli, "cmd_translate") as mock_cmd:
                cli.main()
        ns = mock_cmd.call_args[0][0]
        self.assertEqual(ns.gpu_layers, 0)


class TestPrompts(unittest.TestCase):
    def test_prompt_structural_with_background(self):
        p = prompt_structural_with_background(
            source_text="Test {{v1}}",
            background_text="Title: Sample",
            target_lang="简体中文",
        )
        self.assertIn("Title: Sample", p)
        self.assertIn("参考上面的信息", p)
        self.assertIn("严禁翻译或更改任何代码标签与变量占位符", p)
        self.assertIn("Test {{v1}}", p)
        self.assertNotIn("【背景信息】", p)
        self.assertNotIn("【待翻译文本】", p)
        self.assertNotIn("# 任务目标", p)
        self.assertNotIn("# 严格约束", p)

        p_en = prompt_structural_with_background(
            source_text="Test [v1]",
            background_text="Title: Sample",
            target_lang="Japanese",
            use_english=True,
        )
        self.assertIn("Refer to the above information", p_en)
        self.assertIn("into Japanese", p_en)
        self.assertNotIn("参考上面的信息", p_en)
        self.assertNotIn("【待翻译文本】", p_en)


if __name__ == "__main__":
    unittest.main()
