# SPDX-License-Identifier: AGPL-3.0-only
"""多语言排版补丁回归测试：整词折行、连字符断词、两端对齐、公式与非正文保护。"""

from collections import defaultdict
from pathlib import Path
import sys
import unittest

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from babeldoc.format.pdf.document_il import (
    Box,
    GraphicState,
    PdfCharacter,
    PdfFormula,
    PdfParagraph,
    PdfStyle,
    VisualBbox,
)
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting, TypesettingUnit

from core.typesetting_patch import find_hyphenation_positions, install_typesetting_patch


def _load_font(filename: str, fallback: str) -> pymupdf.Font:
    font_path = ROOT / "assets" / "fonts" / filename
    if font_path.is_file():
        font = pymupdf.Font(fontfile=str(font_path))
    else:
        font = pymupdf.Font(fallback)
    font.font_id = filename
    return font


class _DummyMapper:
    def __init__(self, font: pymupdf.Font):
        self.base_font = font

    def map(self, original_font, char_unicode: str):
        return self.base_font


class _DummyTypesetting(Typesetting):
    def __init__(self, lang_code: str, font: pymupdf.Font):
        self.font_mapper = _DummyMapper(font)
        self.lang_code = lang_code.upper()
        self.is_cjk = any(k in self.lang_code for k in ("ZH", "JA", "JP", "KR", "CN", "HK", "TW"))


def _make_units(text: str, font: pymupdf.Font, font_size: float = 8.8) -> list[TypesettingUnit]:
    style = PdfStyle(font_id=font.font_id, font_size=font_size, graphic_state=GraphicState())
    return [
        TypesettingUnit(unicode=ch, font=font, font_size=font_size, style=style, xobj_id=-1)
        for ch in text
    ]


def _group_lines(units: list[TypesettingUnit]) -> list[list[TypesettingUnit]]:
    by_y: dict[float, list[TypesettingUnit]] = defaultdict(list)
    for u in units:
        y = u.y if u.unicode is not None else u.box.y
        by_y[round(y, 2)].append(u)
    return [by_y[y] for y in sorted(by_y.keys(), reverse=True)]


def _line_text(line_units: list[TypesettingUnit]) -> str:
    parts = []
    for u in line_units:
        ch = u.try_get_unicode()
        parts.append(ch if ch is not None else "[F]")
    return "".join(parts)


def _line_right(line_units: list[TypesettingUnit]) -> float:
    return max(u.box.x2 for u in line_units)


class TestTypesettingPatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_typesetting_patch()
        cls.font_latin = _load_font("NotoSerif-Regular.ttf", "helv")
        cls.font_cjk = _load_font("SourceHanSerifCN-Regular.ttf", "cjk")

    def test_word_never_splits_after_narrow_initial_letter(self):
        """复现 `с / егментов` 与 `в / ыбран` 的窄首字母后强行拆词缺陷并验证彻底消除。"""
        ts = _DummyTypesetting("RU", self.font_latin)
        # 故意构造多个宽度使 `выбран` 和 `сегментов` 精准落在行尾边界附近
        text = (
            "диапазон частот был выбран как совпадение между двумя диапазонами в процессе сравнения "
            "и мы провели расчеты для сегментов времени в данном исследовании."
        )
        for col_w in range(130, 240, 3):
            box = Box(x=20.0, y=0.0, x2=20.0 + col_w, y2=400.0)
            para = PdfParagraph(box=box, first_line_indent=False, layout_label="plain text")
            units = _make_units(text, self.font_latin, 8.8)
            laid_out, fit = ts._layout_typesetting_units(units, box, 1.0, 1.3, para, True)
            self.assertTrue(fit)
            lines = [_line_text(lu) for lu in _group_lines(laid_out)]
            for idx in range(len(lines) - 1):
                cur_line = lines[idx].rstrip()
                nxt_line = lines[idx + 1].lstrip()
                # 绝不能出现无连字符的半词劈裂（如 `в` / `ыбран`、`с` / `егментов`）
                self.assertFalse(
                    cur_line.endswith(" в") and nxt_line.startswith("ыбран"),
                    f"col_w={col_w} 出现无连字符劈词: {cur_line!r} | {nxt_line!r}",
                )
                self.assertFalse(
                    cur_line.endswith(" с") and nxt_line.startswith("егментов"),
                    f"col_w={col_w} 出现无连字符劈词: {cur_line!r} | {nxt_line!r}",
                )

    def test_russian_body_paragraph_is_justified_and_last_line_left_aligned(self):
        ts = _DummyTypesetting("RU", self.font_latin)
        text = (
            "Необходимы точные данные, где N — целое число. Поэтому в данной статье мы провели расчеты FFT "
            "для 2^N × 5 сегментов времени (то есть примерно 20 минут) с использованием данных, полученных "
            "с ветрового лидара, и 2^14 × 0,1 секунды (то есть примерно 27 минут) для данных, полученных "
            "с ультразвукового анемометра. Согласно закону Найквиста, наивысшие частоты спектров "
            "турбулентности, которые могут быть измерены ультразвуковым анемометром и ветровым лидаром, "
            "составляют 5 и 0,1 Гц соответственно."
        )
        box = Box(x=30.0, y=0.0, x2=265.0, y2=400.0)
        para = PdfParagraph(box=box, first_line_indent=False, layout_label="plain text")
        units = _make_units(text, self.font_latin, 8.8)
        laid_out, fit = ts._layout_typesetting_units(units, box, 1.0, 1.3, para, True)
        self.assertTrue(fit)
        lines = _group_lines(laid_out)
        self.assertGreaterEqual(len(lines), 6)

        # 所有非末行应精准贴齐右边界 box.x2
        for idx, line_units in enumerate(lines[:-1]):
            right_x = _line_right(line_units)
            self.assertAlmostEqual(
                right_x,
                box.x2,
                delta=0.6,
                msg=f"第 {idx + 1} 行未对齐到右边界: right_x={right_x:.2f}, box.x2={box.x2:.2f}, text={_line_text(line_units)!r}",
            )

        # 末行必须保持左对齐，不被强行拉伸到右边界
        last_right = _line_right(lines[-1])
        self.assertLess(last_right, box.x2 - 5.0)

    def test_english_and_german_justification_and_hyphenation(self):
        en_text = (
            "We compared the wind speed data of the wind lidar at a height of 330 m with the ultrasonic "
            "anemometer data at a height of 320 m, which are within the 30 m resolution of the wind lidar data. "
            "Figure 2a–c shows a comparison of turbulence spectra in the three directions (U, V, W) obtained "
            "with the wind lidar and ultrasonic anemometer."
        )
        de_text = (
            "Die direkte Messung atmosphärischer Turbulenzparameter mit einem kohärenten Doppler-Windlidar "
            "ermöglicht eine hochaufgelöste Untersuchung der Grenzschichtdynamik und der meteorologischen "
            "Geschwindigkeitsmessungen unter unterschiedlichen Bedingungen."
        )
        for lang, text in (("EN", en_text), ("DE", de_text)):
            with self.subTest(lang=lang):
                ts = _DummyTypesetting(lang, self.font_latin)
                box = Box(x=30.0, y=0.0, x2=265.0, y2=400.0)
                para = PdfParagraph(box=box, first_line_indent=True, layout_label="plain text")
                units = _make_units(text, self.font_latin, 8.8)
                laid_out, fit = ts._layout_typesetting_units(units, box, 1.0, 1.3, para, True)
                self.assertTrue(fit)
                lines = _group_lines(laid_out)
                self.assertGreaterEqual(len(lines), 4)
                for idx, line_units in enumerate(lines[:-1]):
                    self.assertAlmostEqual(_line_right(line_units), box.x2, delta=0.6, msg=f"{lang} line {idx}")

    def test_hyphenation_rules_quality(self):
        # 西里尔语：绝不在 ь/ъ/й 之前断词
        for word in ("использованием", "ультразвукового", "случайный", "турбулентности"):
            positions = find_hyphenation_positions(word, "RU")
            self.assertTrue(positions, f"{word} 应有合法连字符切点")
            for p in positions:
                self.assertNotIn(word[p].lower(), ("ь", "ъ", "й"))

        # 德语/英语：sch 不被劈成 s-ch，短词和全大写缩写不断词
        de_pos = find_hyphenation_positions("meteorologischen", "DE")
        for p in de_pos:
            self.assertFalse(
                "meteorologischen"[p - 1 : p + 2] == "sch",
                f"meteorologischen 不应将 sch 劈开: pos={p}",
            )
        self.assertEqual(find_hyphenation_positions("FFT", "EN"), [])
        self.assertEqual(find_hyphenation_positions("lidar", "EN"), [])

    def test_titles_headers_and_explicit_newlines_not_over_justified(self):
        ts = _DummyTypesetting("EN", self.font_latin)
        box = Box(x=30.0, y=0.0, x2=265.0, y2=400.0)
        text = (
            "Directly measuring atmospheric turbulence parameters using coherent Doppler wind lidar "
            "across multiple altitude levels"
        )

        # 标题区域必须保持左对齐，不拉伸空格
        title_para = PdfParagraph(box=box, first_line_indent=False, layout_label="title")
        title_units = _make_units(text, self.font_latin, 10.0)
        laid_title, _ = ts._layout_typesetting_units(title_units, box, 1.0, 1.3, title_para, True)
        title_lines = _group_lines(laid_title)
        self.assertGreaterEqual(len(title_lines), 2)
        # 标题第一行应保持自然左对齐（存在自然行尾空隙，不被拉到 box.x2）
        self.assertLess(_line_right(title_lines[0]), box.x2 - 2.0)

        # 含显式硬换行的行（如参考文献/条目结束行）绝不被拉伸
        list_text = "First short item line.\nSecond short item line.\nThird short item line."
        list_para = PdfParagraph(box=box, first_line_indent=False, layout_label="reference")
        list_units = _make_units(list_text, self.font_latin, 8.8)
        laid_list, _ = ts._layout_typesetting_units(list_units, box, 1.0, 1.3, list_para, True)
        list_lines = _group_lines(laid_list)
        self.assertEqual(len(list_lines), 3)
        for lu in list_lines:
            self.assertLess(_line_right(lu), box.x2 - 40.0)

    def test_inline_formula_relocates_once_and_justifies_cleanly(self):
        ts = _DummyTypesetting("EN", self.font_latin)
        box = Box(x=30.0, y=0.0, x2=240.0, y2=400.0)
        para = PdfParagraph(box=box, first_line_indent=False, layout_label="plain text")

        style = PdfStyle(font_id=self.font_latin.font_id, font_size=8.8, graphic_state=GraphicState())
        f_char = PdfCharacter(
            pdf_character_id=1,
            char_unicode="x",
            box=Box(x=0.0, y=0.0, x2=18.0, y2=9.0),
            visual_bbox=VisualBbox(box=Box(x=0.0, y=0.0, x2=18.0, y2=9.0)),
            pdf_style=style,
            scale=1.0,
            vertical=False,
            advance=18.0,
            xobj_id=-1,
        )
        formula = PdfFormula(
            box=Box(x=0.0, y=0.0, x2=18.0, y2=9.0),
            pdf_character=[f_char],
            x_offset=0.0,
            y_offset=0.0,
            x_advance=0.0,
        )
        units = (
            _make_units("In this study the variable ", self.font_latin, 8.8)
            + [TypesettingUnit(formular=formula)]
            + _make_units(
                " represents the measured velocity component across overlapping frequency ranges in the boundary layer.",
                self.font_latin,
                8.8,
            )
        )
        laid_out, fit = ts._layout_typesetting_units(units, box, 1.0, 1.3, para, True)
        self.assertTrue(fit)
        lines = _group_lines(laid_out)
        self.assertGreaterEqual(len(lines), 3)
        # 确认公式单元完整保留且非末行已对齐
        formula_units = [u for u in laid_out if u.formular is not None]
        self.assertEqual(len(formula_units), 1)
        self.assertGreaterEqual(formula_units[0].box.x, box.x)
        self.assertLessEqual(formula_units[0].box.x2, box.x2 + 0.6)
        self.assertAlmostEqual(_line_right(lines[0]), box.x2, delta=0.6)

    def test_cjk_mixed_paragraph_justifies_gently_without_breaking_latin(self):
        ts = _DummyTypesetting("ZH", self.font_cjk)
        box = Box(x=30.0, y=0.0, x2=265.0, y2=400.0)
        para = PdfParagraph(box=box, first_line_indent=False, layout_label="plain text")
        text = (
            "我们将高度 330 m 处的测风激光雷达风速数据与高度 320 m 处的超声风速仪数据进行了对比，"
            "二者均处于测风激光雷达 30 m 空间分辨率范围之内。图 2a–c 展示了三个方向的湍流功率谱对比结果。"
        )
        units = _make_units(text, self.font_cjk, 8.8)
        laid_out, fit = ts._layout_typesetting_units(units, box, 1.0, 1.5, para, True)
        self.assertTrue(fit)
        lines = _group_lines(laid_out)
        self.assertGreaterEqual(len(lines), 3)
        for line_units in lines[:-1]:
            self.assertAlmostEqual(_line_right(line_units), box.x2, delta=0.6)

    def test_engine_imports_and_installs_patch_automatically(self):
        import core.engine  # noqa: F401
        from core import typesetting_patch

        self.assertTrue(typesetting_patch._PATCH_INSTALLED)
        self.assertIs(Typesetting._layout_typesetting_units, typesetting_patch._patched_layout_typesetting_units)


if __name__ == "__main__":
    unittest.main()
