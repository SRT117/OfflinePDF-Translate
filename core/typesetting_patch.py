# SPDX-License-Identifier: AGPL-3.0-only
"""BabelDOC 排版引擎增强补丁：修复整词折行 Bug、支持多语言音节连字符断词与两端对齐。

针对 BabelDOC 0.6.2 原生 Typesetting 的三个核心排版缺陷提供纯离线、零额外依赖的完整修复：
1. 修复 `_layout_typesetting_units` 中 `current_x + unit_width + width_before_next_break_point > box.x2`
   重复累加当前字符宽度导致西文/俄文单词从窄首字母后被劈开（如 `с / егментов`、`в / ыбран`）及提前换行的问题；
2. 增加纯离线多语言连字符断词（支持拉丁语系与西里尔语系音节规则，若环境已装 `pyphen` 则优先使用）
   以及复合连字符词（如 `power-law`）自然断行，显著改善窄双栏文献行尾大块留白；
3. 增加排版级两端对齐（Full Justification）：对多行正文的非末行、非硬换行行，通过词间空格均匀拉伸
   （配合非连写字母极微字距补偿与反“河流效应”安全上限）对齐到 `box.x2`，同时保护标题、页眉页脚、
   表格、公式、代码及连写复杂文字（阿拉伯/天城体/泰文等）不被错误拉伸。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache
import logging
import re
import statistics
import unicodedata

logger = logging.getLogger("offlinepdf-translate.typesetting")

_EPS = 1e-4

# 不应执行两端对齐的版面区域标签（标题、页眉页脚、表格、代码、独立公式、幻灯片等）
_NO_JUSTIFY_LABELS: frozenset[str] = frozenset({
    "title",
    "doc_title",
    "paragraph_title",
    "title_hybrid",
    "figure_title",
    "table_title",
    "chart_title",
    "header",
    "footer",
    "page_header",
    "page_footer",
    "page_header_hybrid",
    "page_footer_hybrid",
    "page_number_hybrid",
    "abandon",
    "number",
    "seal",
    "table",
    "table_cell",
    "table_cell_hybrid",
    "table_text",
    "wireless_table_cell",
    "wired_table_cell",
    "table_footnote",
    "form_or_table_hybrid",
    "algorithm",
    "code_algo_hybrid",
    "formula",
    "formula_hybrid",
    "isolate_formula",
    "formula_caption",
    "figure",
    "figure_text",
    "figure_text_hybrid",
    "presentation_slide_hybrid",
    "advertisement_hybrid",
    "manga_or_comic_hybrid",
    "author_info_hybrid",
})

# 额外补充俄/法/德等欧洲语言常用书名引号
_EXTRA_HUNG_PUNCT: frozenset[str] = frozenset({"»", "›"})
_EXTRA_NO_LINE_END_PUNCT: frozenset[str] = frozenset({"«", "‹", "„"})

# 西里尔语元音与不可置于行首的修饰字母
_CYRILLIC_VOWELS: frozenset[str] = frozenset("аеёиоуыэюяєіїүөә")
_CYRILLIC_NO_START: frozenset[str] = frozenset("ьъй")
_CYRILLIC_SONORANTS: frozenset[str] = frozenset("рлмнй")

# 拉丁语不可拆分的常见二合/三合辅音与常见合法音节首辅音群
_LATIN_INSEPARABLE_DIGRAPHS: frozenset[str] = frozenset({
    "ch", "sh", "th", "ph", "gh", "qu", "ck", "wh", "ng", "gn", "cz", "sz", "rz", "dz",
})
_LATIN_ONSET_BLENDS: frozenset[str] = frozenset({
    "bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr",
    "pl", "pr", "sc", "sk", "sl", "sm", "sn", "sp", "st",
    "sw", "tr", "tw", "wr", "ch", "sh", "th", "ph", "qu",
})
_LATIN_VOWEL_DIGRAPHS: frozenset[str] = frozenset({
    "aa", "ae", "ai", "ao", "au", "ea", "ee", "ei", "eo", "eu",
    "ia", "ie", "io", "iu", "oa", "oe", "oi", "oo", "ou", "ua",
    "ue", "ui", "uo", "ay", "ey", "oy", "uy",
})

# 可选 pyphen 支持（零强依赖；若用户环境已安装则自动复用其 TeX/Hunspell 字典）
try:
    import pyphen as _pyphen  # type: ignore[import-not-found]
except Exception:
    _pyphen = None

_PYPHEN_LANG_MAP: dict[str, str] = {
    "en": "en_US",
    "ru": "ru_RU",
    "uk": "uk_UA",
    "de": "de_DE",
    "fr": "fr_FR",
    "es": "es_ES",
    "pt": "pt_PT",
    "it": "it_IT",
    "nl": "nl_NL",
    "pl": "pl_PL",
    "cs": "cs_CZ",
    "tr": "tr_TR",
    "id": "id_ID",
}


def _is_latin_letter(ch: str) -> bool:
    if not ch or not ch.isalpha():
        return False
    code = ord(ch)
    return (
        (0x0041 <= code <= 0x005A)
        or (0x0061 <= code <= 0x007A)
        or (0x00C0 <= code <= 0x024F)
        or (0x1E00 <= code <= 0x1EFF)
    )


def _is_cyrillic_letter(ch: str) -> bool:
    if not ch or not ch.isalpha():
        return False
    code = ord(ch)
    return (0x0400 <= code <= 0x052F) or (0x2DE0 <= code <= 0x2DFF) or (0xA640 <= code <= 0xA69F)


def _is_non_joining_alphabetic(ch: str) -> bool:
    """判断字符是否属于允许极微字距微调的非连写拼音字母（拉丁/西里尔/希腊/数字）。"""
    if not ch:
        return False
    code = ord(ch)
    return (
        _is_latin_letter(ch)
        or _is_cyrillic_letter(ch)
        or (0x0370 <= code <= 0x03FF)
        or ("0" <= ch <= "9")
    )


def _is_latin_vowel(ch: str) -> bool:
    decomp = unicodedata.normalize("NFD", ch.lower())
    return bool(decomp) and decomp[0] in "aeiouy"


def _detect_word_script(word: str) -> str | None:
    """仅对纯单一字母体系（纯拉丁或纯西里尔）且非全大写的词返回脚本标识。"""
    if not word or word.isupper():
        return None
    if all(_is_cyrillic_letter(ch) for ch in word):
        return "cyrillic"
    if all(_is_latin_letter(ch) for ch in word):
        return "latin"
    return None


@lru_cache(maxsize=32)
def _get_pyphen_dic(lang_code: str):
    if _pyphen is None:
        return None
    key = (lang_code or "").strip().lower().split("-")[0]
    mapped = _PYPHEN_LANG_MAP.get(key)
    if not mapped:
        return None
    try:
        return _pyphen.Pyphen(lang=mapped, left=3, right=3)
    except Exception:
        return None


def _cyrillic_hyphen_positions(word: str, min_left: int = 3, min_right: int = 3) -> list[int]:
    """按俄语/斯拉夫西里尔语标准移行规则（правила переноса）计算合法连字符切分位置。"""
    lower = word.lower()
    n = len(lower)
    if n < min_left + min_right:
        return []

    vowel_indices = [idx for idx, ch in enumerate(lower) if ch in _CYRILLIC_VOWELS]
    if len(vowel_indices) < 2:
        return []

    candidates: set[int] = set()
    for vi in range(len(vowel_indices) - 1):
        v1 = vowel_indices[vi]
        v2 = vowel_indices[vi + 1]
        cluster_start = v1 + 1
        cluster_len = v2 - cluster_start

        if cluster_len == 0:
            # 相邻元音之间（如 со-от-вет-ствен-но、ра-дио-ак-тив-ный）
            split_pos = v2
        elif cluster_len == 1:
            ch = lower[cluster_start]
            # ь / ъ / й 绝不能留在下一行行首，必须跟前一个字母走
            split_pos = cluster_start + 1 if ch in _CYRILLIC_NO_START else cluster_start
        else:
            # 2 个及以上辅音簇：优先保留 ь/ъ/й 在左侧；双写辅音中间切；响音后切；否则首辅音后切
            first_c = lower[cluster_start]
            second_c = lower[cluster_start + 1]
            if first_c in _CYRILLIC_NO_START:
                split_pos = cluster_start + 1
            elif second_c in _CYRILLIC_NO_START:
                split_pos = cluster_start + 2
            elif first_c == second_c:
                split_pos = cluster_start + 1
            elif first_c in _CYRILLIC_SONORANTS:
                split_pos = cluster_start + 1
            else:
                split_pos = cluster_start + 1

        # 避免切点紧邻 ь/ъ/й 导致修饰符落到行首
        while split_pos < v2 and lower[split_pos] in _CYRILLIC_NO_START:
            split_pos += 1

        if min_left <= split_pos <= n - min_right:
            left_has_vowel = any(ch in _CYRILLIC_VOWELS for ch in lower[:split_pos])
            right_has_vowel = any(ch in _CYRILLIC_VOWELS for ch in lower[split_pos:])
            if left_has_vowel and right_has_vowel and lower[split_pos] not in _CYRILLIC_NO_START:
                candidates.add(split_pos)

    return sorted(candidates)


def _latin_hyphen_positions(word: str, min_left: int = 3, min_right: int = 3) -> list[int]:
    """按拉丁语系通用音节结构（最大声母原则 + 不可分二合辅音保护）计算连字符切分位置。"""
    lower = word.lower()
    n = len(lower)
    if n < min_left + min_right:
        return []

    is_v = [_is_latin_vowel(ch) for ch in lower]
    # 合并连续元音簇为音节核
    nuclei: list[tuple[int, int]] = []
    idx = 0
    while idx < n:
        if is_v[idx]:
            start = idx
            while idx < n and is_v[idx]:
                idx += 1
            nuclei.append((start, idx))
        else:
            idx += 1

    if len(nuclei) < 2:
        return []

    candidates: set[int] = set()
    for ni in range(len(nuclei) - 1):
        _, c_start = nuclei[ni]
        c_end, _ = nuclei[ni + 1]
        c_len = c_end - c_start
        cluster = lower[c_start:c_end]

        if c_len == 1:
            split_pos = c_start
        elif c_len == 2:
            if cluster in _LATIN_INSEPARABLE_DIGRAPHS:
                split_pos = c_start
            else:
                split_pos = c_start + 1
        elif c_len >= 3:
            if cluster.endswith("sch"):
                split_pos = c_end - 3
            elif cluster.startswith("sch") and c_len >= 4:
                split_pos = c_start
            elif cluster[-2:] in _LATIN_ONSET_BLENDS:
                split_pos = c_end - 2
            elif cluster[:2] in _LATIN_INSEPARABLE_DIGRAPHS:
                split_pos = c_start + 2
            else:
                split_pos = c_start + 1
        else:
            continue

        if min_left <= split_pos <= n - min_right:
            # 防止把元音二合字母劈开（虽按核切分已避免，仍作防御校验）
            pair = lower[split_pos - 1 : split_pos + 1]
            if pair in _LATIN_VOWEL_DIGRAPHS:
                continue
            left_has_v = any(is_v[:split_pos])
            right_has_v = any(is_v[split_pos:])
            if left_has_v and right_has_v:
                candidates.add(split_pos)

    return sorted(candidates)


def find_hyphenation_positions(
    word: str,
    lang_code: str = "",
    min_left: int = 3,
    min_right: int = 3,
) -> list[int]:
    """返回单词内部所有合法的连字符切分下标（升序）。"""
    script = _detect_word_script(word)
    if script is None or len(word) < min_left + min_right:
        return []

    dic = _get_pyphen_dic(lang_code)
    if dic is not None:
        try:
            positions = [
                pos
                for pos in dic.positions(word)
                if min_left <= pos <= len(word) - min_right
            ]
            if positions:
                return sorted(set(positions))
        except Exception:
            pass

    if script == "cyrillic":
        return _cyrillic_hyphen_positions(word, min_left=min_left, min_right=min_right)
    return _latin_hyphen_positions(word, min_left=min_left, min_right=min_right)


def _unit_advance(unit, scale: float) -> float:
    """计算单元在指定 scale 下放置后的水平前进距离（与 `relocate().box.x2 - x` 一致）。"""
    width = unit.width * scale
    if unit.formular is not None:
        x_off = getattr(unit.formular, "x_offset", None) or 0.0
        return width + x_off * scale
    return width


def _is_compound_break_hyphen(units: list, idx: int, run_start: int) -> bool:
    """判断 `units[idx]` 是否为复合词内部可断行的连字符（如 `power-law`，排除负号 `-5`）。"""
    if idx - run_start < 2:
        return False
    ch = units[idx].try_get_unicode()
    if ch not in ("-", "–"):
        return False
    if idx + 1 >= len(units):
        return False
    prev_ch = units[idx - 1].try_get_unicode() or ""
    next_u = units[idx + 1]
    next_ch = next_u.try_get_unicode() or ""
    return (
        bool(prev_ch)
        and prev_ch.isalnum()
        and (not next_u.can_break_line)
        and bool(next_ch)
        and next_ch.isalnum()
    )


def _find_unbreakable_run_end(units: list, start_idx: int) -> int:
    """从 `start_idx` 出发找到当前不可拆分词段的结束下标（半开区间 `[start_idx, end)`）。"""
    n = len(units)
    if start_idx >= n or units[start_idx].can_break_line:
        return start_idx
    j = start_idx
    while j < n and not units[j].can_break_line:
        if _is_compound_break_hyphen(units, j, start_idx):
            # 允许在复合词连字符之后断行（`power-` + `law`）
            return j + 1
        j += 1
    return j


def _trailing_punct_width(units: list, after_idx: int, scale: float) -> float:
    """统计紧跟词尾的连续悬挂/闭合标点总宽度（防止 `).` 等组合整体突出右栏边界）。"""
    total = 0.0
    j = after_idx
    n = len(units)
    while j < n and units[j].is_hung_punctuation and not units[j].is_space:
        ch = units[j].try_get_unicode()
        if ch == "\n":
            break
        total += _unit_advance(units[j], scale)
        j += 1
    return total


@dataclass
class _LineItem:
    unit: object
    x: float
    width: float
    height: float
    is_space: bool
    is_cjk: bool
    is_formula: bool
    unicode: str | None


@dataclass
class _LayoutLine:
    y: float
    start_x: float
    items: list[_LineItem]
    ended_by_hard_break: bool = False


def _should_justify_paragraph(
    paragraph,
    box,
    lines: list[_LayoutLine],
    font_size: float,
    scale: float,
) -> bool:
    """判断段落是否适合执行两端对齐（保护单行、标题、页眉页脚、表格、极窄框与竖排）。"""
    if len(lines) < 2:
        return False
    if getattr(paragraph, "vertical", False):
        return False
    box_width = box.x2 - box.x
    scaled_fs = max(font_size * scale, 1.0)
    if box_width < max(80.0, scaled_fs * 10.0):
        return False

    label = (getattr(paragraph, "layout_label", None) or "").strip().lower()
    if label in _NO_JUSTIFY_LABELS:
        return False

    return True


def _justify_line(
    line: _LayoutLine,
    box_x2: float,
    font_size: float,
    scale: float,
    base_space_width: float,
    is_cjk_lang: bool,
) -> None:
    """对单行非末行执行两端对齐：优先拉伸词间空格，必要时对非连写字母做极微字距补偿。"""
    if line.ended_by_hard_break:
        return

    # 剥离行尾残余空白，确保最后一个可见字形贴齐右边界
    while line.items and line.items[-1].is_space:
        line.items.pop()

    if len(line.items) < 2:
        return

    last = line.items[-1]
    line_right_x = last.x + last.width
    slack = box_x2 - line_right_x
    if slack <= 0.25:
        return

    avail_width = box_x2 - line.start_x
    if avail_width <= 0:
        return
    fill_ratio = (line_right_x - line.start_x) / avail_width

    scaled_fs = max(font_size * scale, 1.0)
    items = line.items

    # 识别行内有效词间空格槽位（前后均为可见元素，连续空格合并为一个拉伸点）
    space_slots: list[int] = []
    for idx in range(1, len(items) - 1):
        if items[idx].is_space and not items[idx - 1].is_space:
            space_slots.append(idx)

    visible_count = sum(1 for it in items if not it.is_space)
    cjk_count = sum(1 for it in items if it.is_cjk)
    cjk_dominant = is_cjk_lang and visible_count > 0 and (cjk_count / visible_count) >= 0.45

    if space_slots and not cjk_dominant:
        # 非中文/空格分词语言：如果行填充率过低（如被超大公式挤出半行空白），不强拉制造“河流”
        if fill_ratio < 0.62:
            return

        effective_sp = max(base_space_width, scaled_fs * 0.24)
        # 单个词间空格最多额外拉伸约 2.5 倍空格宽（~0.68 em），避免刺眼大空洞
        max_extra_per_space = max(effective_sp * 2.5, scaled_fs * 0.68)
        space_budget = len(space_slots) * max_extra_per_space

        # 仅对非连写拼音字母之间的间隙允许极微字距补偿（<= 0.035 em，肉眼几乎无感）
        track_slots: list[int] = []
        for idx in range(1, len(items)):
            prev_it = items[idx - 1]
            curr_it = items[idx]
            if prev_it.is_space or curr_it.is_space:
                continue
            if prev_it.is_formula or curr_it.is_formula:
                continue
            if _is_non_joining_alphabetic(prev_it.unicode or "") and _is_non_joining_alphabetic(
                curr_it.unicode or ""
            ):
                track_slots.append(idx)

        max_track_per_gap = scaled_fs * 0.035
        track_budget = len(track_slots) * max_track_per_gap

        if slack > space_budget + track_budget:
            # 极端窄行且极少空格：宁可保持自然左对齐，也不拉出破坏可读性的巨大空白
            return

        space_alloc = min(slack, space_budget)
        extra_per_space = space_alloc / len(space_slots)
        leftover = slack - space_alloc
        extra_per_track = (leftover / len(track_slots)) if (leftover > 0.05 and track_slots) else 0.0

        space_set = set(space_slots)
        track_set = set(track_slots) if extra_per_track > 0.0 else set()

        offset = 0.0
        for idx, item in enumerate(items):
            if idx in track_set:
                offset += extra_per_track
            item.x += offset
            if idx in space_set:
                offset += extra_per_space
        return

    # CJK 主导行（或无空格长行）：仅在高填充率且极小微调范围内做柔和字距均摊
    if cjk_dominant and fill_ratio >= 0.80:
        cjk_gaps: list[int] = []
        for idx in range(1, len(items)):
            prev_it = items[idx - 1]
            curr_it = items[idx]
            # 绝不拆开嵌入的英文/俄文单词内部，也不在标点与紧邻字符间硬塞缝隙
            if prev_it.unit.is_cannot_appear_in_line_end_punctuation:
                continue
            if curr_it.unit.is_hung_punctuation:
                continue
            if prev_it.is_space or curr_it.is_space or prev_it.is_cjk or curr_it.is_cjk:
                cjk_gaps.append(idx)

        if not cjk_gaps:
            return
        max_cjk_extra = scaled_fs * 0.12
        if slack > len(cjk_gaps) * max_cjk_extra:
            return
        extra_per_gap = slack / len(cjk_gaps)
        gap_set = set(cjk_gaps)
        offset = 0.0
        for idx, item in enumerate(items):
            if idx in gap_set:
                offset += extra_per_gap
            item.x += offset


def _make_hyphen_unit(typesetting_self, anchor_unit, TypesettingUnit):
    """基于锚点字符创建样式完全一致的连字符 `TypesettingUnit`。"""
    font = anchor_unit.font
    if font is None or not font.has_glyph(ord("-")):
        font = (
            typesetting_self.font_mapper.map(anchor_unit.original_font, "-")
            if anchor_unit.original_font is not None
            else None
        ) or typesetting_self.font_mapper.base_font
    if font is None or not font.has_glyph(ord("-")):
        return None
    return TypesettingUnit(
        unicode="-",
        font=font,
        original_font=anchor_unit.original_font,
        font_size=anchor_unit.font_size,
        style=anchor_unit.style,
        xobj_id=anchor_unit.xobj_id,
        debug_info=anchor_unit.debug_info,
    )


def _try_pick_hyphen_split(
    typesetting_self,
    units: list,
    start_idx: int,
    run_end: int,
    current_x: float,
    line_start_x: float,
    box_x2: float,
    scale: float,
    TypesettingUnit,
    line_space_count: int = 4,
    scaled_font_size: float = 9.0,
) -> tuple[int, object] | None:
    """在当前行剩余空间内寻找最优音节连字符切分点，返回 `(split_offset, hyphen_unit)` 或 `None`。

    兼容词段首尾附带标点/括号/引号（如 `турбулентности,`、`лидаром,`、`(приблизительно)`、`«метод»`），
    仅对内部纯字母词干按音节断词，保持前导开括号与词干前半绑定、尾随标点与词干后半绑定。
    """
    word_units = units[start_idx:run_end]
    if any(u.unicode is None or len(u.unicode) != 1 for u in word_units):
        return None

    raw_text = "".join(u.unicode for u in word_units)
    raw_len = len(raw_text)

    # 剥离词段首尾标点/括号/引号，定位纯字母词干 [core_start, core_end)
    core_start = 0
    while core_start < raw_len and not raw_text[core_start].isalpha():
        if raw_text[core_start].isdigit():
            return None
        core_start += 1
    core_end = raw_len
    while core_end > core_start and not raw_text[core_end - 1].isalpha():
        if raw_text[core_end - 1].isdigit():
            return None
        core_end -= 1

    core_word = raw_text[core_start:core_end]
    core_len = len(core_word)
    word_width = sum(_unit_advance(u, scale) for u in word_units)
    avail_line_width = box_x2 - line_start_x
    if avail_line_width <= 0:
        return None

    # 判断是否真的有必要断词：
    # 1) 结合当前行已有空格数计算“纯靠词间空格即可舒适吸收的留白上限”，
    #    防止行内只有 2~3 个空格（德语/俄语长词行）时误以为 88% 够满而跳过断词；
    # 2) 如果当前行是空行且单个词本身比整栏还宽，则必须尝试断词（否则只能强行缩小整段字号）。
    is_empty_line = abs(current_x - line_start_x) <= _EPS
    filled_before = (current_x - line_start_x) / avail_line_width
    word_exceeds_full_line = word_width > avail_line_width + _EPS
    remaining_width = box_x2 - current_x
    comfortable_slack = min(
        avail_line_width * 0.12,
        max(line_space_count, 0) * max(scaled_font_size, 1.0) * 0.45,
    )

    if not is_empty_line and remaining_width <= comfortable_slack:
        return None

    min_word_len = 6 if word_exceeds_full_line else 7
    if core_len < min_word_len:
        return None

    positions = find_hyphenation_positions(
        core_word,
        lang_code=getattr(typesetting_self, "lang_code", ""),
        min_left=3,
        min_right=3,
    )
    fallback_positions = find_hyphenation_positions(
        core_word,
        lang_code=getattr(typesetting_self, "lang_code", ""),
        min_left=2,
        min_right=3 if not word_exceeds_full_line else 2,
    )
    # 优先尝试 min_left>=3 的切分点（从长到短），若均放不下再回退到标准排版允许的 2 字母音节（如 `Ge-`, `ex-`, `ис-`）
    ordered_positions = list(reversed(positions)) + [
        p for p in reversed(fallback_positions) if p not in positions
    ]
    if not ordered_positions:
        return None

    # 预先计算含前导标点在内的前缀宽度，贪心选择能放进 box_x2 的最长合法前缀
    prefix_widths = [0.0]
    for u in word_units:
        prefix_widths.append(prefix_widths[-1] + _unit_advance(u, scale))

    for core_pos in ordered_positions:
        split_offset = core_start + core_pos
        hyphen_unit = _make_hyphen_unit(typesetting_self, word_units[split_offset - 1], TypesettingUnit)
        if hyphen_unit is None:
            continue
        hyphen_w = _unit_advance(hyphen_unit, scale)
        if prefix_widths[split_offset] + hyphen_w <= remaining_width + _EPS:
            # 确认断词后该行填充率确实有明显提升（或达到高填充率，或本身是超宽长词必须断行）
            new_fill = (current_x + prefix_widths[split_offset] + hyphen_w - line_start_x) / avail_line_width
            if word_exceeds_full_line or new_fill >= 0.88 or new_fill >= max(filled_before + 0.08, 0.72):
                return split_offset, hyphen_unit

    return None


def _patched_get_width_before_next_break_point(
    self,
    typesetting_units: list,
    scale: float,
) -> float:
    if not typesetting_units or typesetting_units[0].can_break_line:
        return 0.0
    run_end = _find_unbreakable_run_end(typesetting_units, 0)
    return sum(_unit_advance(u, scale) for u in typesetting_units[:run_end])


def _patched_layout_typesetting_units(
    self,
    typesetting_units: list,
    box,
    scale: float,
    line_skip: float,
    paragraph,
    use_english_line_break: bool = True,
):
    """增强版段落排版布局：准确整词折行 + 智能连字符断词 + 多语言两端对齐。"""
    from babeldoc.format.pdf.document_il.midend.typesetting import TypesettingUnit

    if not typesetting_units:
        return [], True

    # 1. 计算字号众数（防空列表异常）
    font_sizes = []
    for unit in typesetting_units:
        if unit.font_size:
            font_sizes.append(unit.font_size)
        if unit.char and unit.char.pdf_style and unit.char.pdf_style.font_size:
            font_sizes.append(unit.char.pdf_style.font_size)
    font_sizes.sort()
    if font_sizes:
        font_size = statistics.mode(font_sizes)
    else:
        font_size = 10.0

    # 基准空格宽度：优先取当前目标字体真实空格宽，中英混排间距沿用 0.5em 基准
    cjk_half_em = self.font_mapper.base_font.char_lengths("你", font_size * scale)[0] * 0.5
    real_space_w = self.font_mapper.base_font.char_lengths(" ", font_size * scale)[0]
    if real_space_w <= 0:
        real_space_w = font_size * scale * 0.28

    # 2. 计算行高（众数回退均值）
    unit_heights = [unit.height for unit in typesetting_units if unit.try_get_unicode() != "\n"]
    if not unit_heights:
        avg_height = font_size * scale
    elif len(unit_heights) == 1:
        avg_height = unit_heights[0] * scale
    else:
        try:
            avg_height = statistics.mode(unit_heights) * scale
        except statistics.StatisticsError:
            avg_height = sum(unit_heights) / len(unit_heights) * scale

    box = copy.deepcopy(box)
    lang_code_upper = (getattr(self, "lang_code", "") or "").upper()
    is_cjk_effective = bool(getattr(self, "is_cjk", False)) or ("YUE" in lang_code_upper)

    # 3. 检测显式换行符与列表/编号段首，避免误加首行缩进
    has_explicit_newlines = any(u.try_get_unicode() == "\n" for u in typesetting_units)
    first_char = ""
    for u in typesetting_units:
        ch = u.try_get_unicode()
        if ch and not ch.isspace():
            first_char = ch
            break
    is_list_or_numbered = bool(re.match(r"^[\d\[\(（【•\-*]", first_char))

    first_indent = 0.0
    if paragraph.first_line_indent and not has_explicit_newlines and not is_list_or_numbered:
        candidate_indent = cjk_half_em * 4.0 if is_cjk_effective else font_size * scale * 1.4
        if (box.x2 - (box.x + candidate_indent)) > font_size * scale * 6.0:
            first_indent = candidate_indent

    current_x = box.x + first_indent
    current_y = box.y2 - avg_height
    current_line_heights: list[float] = []
    current_line = _LayoutLine(y=current_y, start_x=current_x, items=[], ended_by_hard_break=False)
    lines: list[_LayoutLine] = [current_line]

    all_units_fit = True
    last_unit = None
    consecutive_hyphens = 0
    max_optical_overhang = font_size * scale * 0.35

    def _start_new_line(hard_break: bool) -> bool:
        nonlocal current_x, current_y, current_line_heights, current_line, last_unit, all_units_fit
        current_line.ended_by_hard_break = hard_break
        if hard_break:
            max_h = max(current_line_heights) if current_line_heights else avg_height
            try:
                mode_h = statistics.mode(current_line_heights) if current_line_heights else avg_height
            except statistics.StatisticsError:
                mode_h = (
                    sum(current_line_heights) / len(current_line_heights)
                    if current_line_heights
                    else avg_height
                )
        else:
            if not current_line_heights:
                return False
            max_h = max(current_line_heights)
            mode_h = statistics.mode(current_line_heights)

        current_x = box.x
        current_y -= max(mode_h * line_skip, max_h * 1.05)
        current_line_heights = []
        last_unit = None
        current_line = _LayoutLine(y=current_y, start_x=box.x, items=[], ended_by_hard_break=False)
        lines.append(current_line)
        if current_y < box.y:
            all_units_fit = False
        return True

    def _place_unit(u) -> None:
        nonlocal current_x, last_unit
        u_adv = _unit_advance(u, scale)
        u_h = u.height * scale
        ch = u.try_get_unicode()
        current_line.items.append(
            _LineItem(
                unit=u,
                x=current_x,
                width=u_adv,
                height=u_h,
                is_space=u.is_space,
                is_cjk=u.is_cjk_char,
                is_formula=(u.formular is not None),
                unicode=ch,
            )
        )
        if not u.is_space:
            current_line_heights.append(u_h)
        prev_x = current_x
        current_x += u_adv
        if prev_x > current_x:
            logger.warning(f"坐标回绕！！！TypesettingUnit: {u.box}, ")
        last_unit = u

    n = len(typesetting_units)
    i = 0
    while i < n:
        unit = typesetting_units[i]
        ch = unit.try_get_unicode()

        # 显式换行符
        if ch == "\n":
            _start_new_line(hard_break=True)
            consecutive_hyphens = 0
            i += 1
            continue

        # 跳过行首空白
        if not current_line.items and unit.is_space:
            i += 1
            continue

        # 中西文混排自动加半角微间距
        if (
            last_unit is not None
            and current_line.items
            and (last_unit.is_cjk_char ^ unit.is_cjk_char)
            and not last_unit.mixed_character_blacklist
            and not unit.mixed_character_blacklist
            and ch != " "
            and last_unit.try_get_unicode() != " "
            and last_unit.try_get_unicode() not in ("。", "！", "？", "；", "：", "，")
        ):
            current_x += cjk_half_em * 0.5

        unit_width = _unit_advance(unit, scale)

        # 情形 A：整词折行检查（仅在不可拆分词段起点一次性校验整个词段，彻底消除重复累加与半词劈裂）
        if use_english_line_break and not unit.can_break_line:
            run_end = _find_unbreakable_run_end(typesetting_units, i)
            run_width = sum(_unit_advance(u, scale) for u in typesetting_units[i:run_end])
            trail_w = _trailing_punct_width(typesetting_units, run_end, scale)
            effective_overhang = max(0.0, trail_w - max_optical_overhang)
            exceeds_line = (current_x + run_width + effective_overhang) > (box.x2 + _EPS)

            if exceeds_line:
                # 优先尝试对长单词执行音节连字符断词
                hyphen_pick = None
                if consecutive_hyphens < 3 or (run_width > (box.x2 - current_line.start_x) + _EPS):
                    line_spaces = sum(
                        1
                        for idx_s in range(1, len(current_line.items))
                        if current_line.items[idx_s].is_space and not current_line.items[idx_s - 1].is_space
                    )
                    # 排除刚放在行尾的尾随空格（不计入词间拉伸槽位）
                    if current_line.items and current_line.items[-1].is_space:
                        line_spaces = max(0, line_spaces - 1)
                    hyphen_pick = _try_pick_hyphen_split(
                        self,
                        typesetting_units,
                        i,
                        run_end,
                        current_x,
                        current_line.start_x,
                        box.x2,
                        scale,
                        TypesettingUnit,
                        line_space_count=line_spaces,
                        scaled_font_size=font_size * scale,
                    )

                if hyphen_pick is not None:
                    split_offset, hyphen_unit = hyphen_pick
                    for k in range(i, i + split_offset):
                        _place_unit(typesetting_units[k])
                    _place_unit(hyphen_unit)
                    if not _start_new_line(hard_break=False):
                        return [], False
                    consecutive_hyphens += 1
                    i += split_offset
                    continue

                # 无法或无需断词：将整个词段整体移到下一行
                if not _start_new_line(hard_break=False):
                    return [], False
                consecutive_hyphens = 0
                # 不递增 i，在崭新一行重新放置该词段（若整行仍放不下则继续触发断词或返回 False 缩字号）
                continue

            # 整个词段放得下：一次性放置整个词段
            for k in range(i, run_end):
                _place_unit(typesetting_units[k])
            consecutive_hyphens = 0
            i = run_end
            continue

        # 情形 B：行尾禁止孤立的开引号/开括号（如 `(`、`«`、`“`）需跟随后续首词一起折行
        if unit.is_cannot_appear_in_line_end_punctuation and current_line.items:
            next_run_end = _find_unbreakable_run_end(typesetting_units, i + 1) if use_english_line_break else (i + 1)
            if next_run_end > i + 1:
                next_run_w = sum(_unit_advance(u, scale) for u in typesetting_units[i + 1 : next_run_end])
                if current_x + unit_width + next_run_w > box.x2 + _EPS:
                    # 若后续词可直接在开括号之后合法断词，则允许留下开括号；否则与词整体换行
                    can_hyphen_after = _try_pick_hyphen_split(
                        self,
                        typesetting_units,
                        i + 1,
                        next_run_end,
                        current_x + unit_width,
                        current_line.start_x,
                        box.x2,
                        scale,
                        TypesettingUnit,
                    )
                    if can_hyphen_after is None:
                        if not _start_new_line(hard_break=False):
                            return [], False
                        consecutive_hyphens = 0
                        continue
            elif current_x + unit_width * 2.0 > box.x2 + _EPS:
                if not _start_new_line(hard_break=False):
                    return [], False
                consecutive_hyphens = 0
                continue

        # 情形 C：普通单字符 / 汉字 / 公式 / 空格 / 标点
        if not unit.is_hung_punctuation and (current_x + unit_width > box.x2 + _EPS):
            if not _start_new_line(hard_break=False):
                return [], False
            consecutive_hyphens = 0
            if unit.is_space:
                i += 1
                continue

        _place_unit(unit)
        i += 1

    # 标记最后一行（最后一行永远左对齐，不拉伸）
    while len(lines) > 1 and not lines[-1].items:
        lines.pop()
    if lines:
        lines[-1].ended_by_hard_break = True

    # 4. 两端对齐调整阶段（仅调整每行内水平坐标 x，绝不改变行数与溢出判定）
    if _should_justify_paragraph(paragraph, box, lines, font_size, scale):
        for line in lines:
            _justify_line(
                line=line,
                box_x2=box.x2,
                font_size=font_size,
                scale=scale,
                base_space_width=real_space_w,
                is_cjk_lang=is_cjk_effective,
            )

    # 5. 统一对每个排版单元执行一次精确 relocate（确保公式/曲线/表单变换无二次叠加）
    typeset_units = []
    for line in lines:
        for item in line.items:
            typeset_units.append(item.unit.relocate(item.x, line.y, scale))

    return typeset_units, all_units_fit


_PATCH_INSTALLED = False


def install_typesetting_patch() -> bool:
    """幂等安装 BabelDOC 排版引擎补丁（支持原版 babeldoc==0.6.2 与便携运行时）。"""
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return False

    from babeldoc.format.pdf.document_il.midend import typesetting as ts_mod

    # 1. 扩展欧洲语言引号的避头尾标点集合
    orig_hung = ts_mod.TypesettingUnit.calc_is_hung_punctuation
    orig_no_end = ts_mod.TypesettingUnit.calc_is_cannot_appear_in_line_end_punctuation

    def _patched_calc_is_hung_punctuation(self):
        if orig_hung(self):
            return True
        ch = self.try_get_unicode()
        return bool(ch and ch in _EXTRA_HUNG_PUNCT)

    def _patched_calc_is_cannot_appear_in_line_end_punctuation(self):
        if orig_no_end(self):
            return True
        ch = self.try_get_unicode()
        return bool(ch and ch in _EXTRA_NO_LINE_END_PUNCT)

    ts_mod.TypesettingUnit.calc_is_hung_punctuation = _patched_calc_is_hung_punctuation
    ts_mod.TypesettingUnit.calc_is_cannot_appear_in_line_end_punctuation = (
        _patched_calc_is_cannot_appear_in_line_end_punctuation
    )

    # 2. 确保显式换行符 `\n` 在原版 babeldoc 0.6.2 中不会被 FontMapper 过滤丢弃（线程安全）
    from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper

    orig_font_map = FontMapper.map

    def _patched_font_map(self, original_font, char_unicode):
        if char_unicode == "\n":
            return self.base_font
        return orig_font_map(self, original_font, char_unicode)

    FontMapper.map = _patched_font_map

    # 3. 替换折行与两端对齐核心布局方法
    ts_mod.Typesetting._get_width_before_next_break_point = _patched_get_width_before_next_break_point
    ts_mod.Typesetting._layout_typesetting_units = _patched_layout_typesetting_units

    _PATCH_INSTALLED = True
    logger.info("已挂载 BabelDOC 多语言排版增强补丁（整词折行修复 + 连字符断词 + 两端对齐）")
    return True
