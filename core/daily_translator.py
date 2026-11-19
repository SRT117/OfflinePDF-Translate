# SPDX-License-Identifier: AGPL-3.0-only
"""日常翻译核心引擎：零心智负担自动互译、Hy-MT2 术语表干预、长文保护分段与历史记录。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import inspect
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Callable, Iterable, List, Optional, Tuple

from .hardware import detect_hardware_profile
from .llama_runtime import LlamaServerRuntime
from .offline import restrict_network_to_loopback, resolve_translation_model, user_data_dir
from .prompts import DAILY_INFERENCE_PARAMS, SUPPORTED_LANGUAGES, build_daily_prompt, should_use_chinese_prompt
from .translator import (
    HunyuanLocalTranslator,
    clean_translated_candidate,
    detect_text_language,
    is_standalone_url_or_code,
    protect_inline_urls,
    restore_inline_urls,
)

logger = logging.getLogger("offlinepdf-translate.daily")

AUTO_BILINGUAL_TARGET = "auto_bi"

DAILY_MAX_INPUT_CHARS = 5000
DAILY_MAX_OUTPUT_CHARS = 20000


class InputTooLongError(ValueError):
    """输入文本超出日常翻译单次字数上限。"""

# 语言选项列表（供日常翻译下拉框使用，完整对齐 Hy-MT2-1.8B 官方支持语种）
DAILY_SOURCE_LANGUAGES: list[tuple[str, str]] = [
    ("自动识别", "auto"),
    *SUPPORTED_LANGUAGES,
]

DAILY_TARGET_LANGUAGES: list[tuple[str, str]] = [
    ("自动互译 (中 ⇄ 英)", AUTO_BILINGUAL_TARGET),
    *SUPPORTED_LANGUAGES,
]

STYLE_OPTIONS: list[tuple[str, str]] = [
    ("默认", "default"),
    ("学术", "academic"),
    ("正式", "formal"),
    ("口语", "casual"),
    ("精简", "concise"),
]

_DAILY_LEAK_RE = re.compile(
    r"^(?:"
    r"参考下面的翻译\s*[：:]\s*(?:\n.*?(?=将以下文本翻译为))?"
    r"|Reference the following translations\s*[：:]\s*(?:\n.*?(?=Translate the following text into))?"
    r"|(?:请)?将以下文本翻译为[^\n]*?[：:]"
    r"|(?:Please )?Translate the following text into[^\n]*?[：:]"
    r"|注意不需要翻译上面的背景信息[。.]?"
    r"|Do not translate the context above[。.]?"
    r")\s*",
    re.IGNORECASE | re.DOTALL,
)

_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
_JA_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_KO_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
_LIST_LINE_RE = re.compile(r"^[ \t]*(?:[-*+•]|#{1,6}\s|\d{1,3}[.)]\s|> )")
_FENCE_OPEN_RE = re.compile(r"^([`~]{3,})")


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 1. PDF 复制断行智能清理 & 文本保护分块
# ---------------------------------------------------------------------------

def clean_pdf_linebreaks(text: str) -> str:
    """清理从 PDF / 论文中复制文本常见的断词连字符与硬换行，同时保留代码块、段落空行与列表结构。"""
    if not text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    # 切分代码块与普通文本段，代码块原样不动
    lines = normalized.split("\n")
    segments: list[tuple[bool, list[str]]] = []
    buf: list[str] = []
    in_code = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        stripped = line.lstrip()
        if not in_code:
            m = _FENCE_OPEN_RE.match(stripped)
            if m:
                if buf:
                    segments.append((False, buf))
                    buf = []
                in_code = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                buf.append(line)
            else:
                buf.append(line)
        else:
            buf.append(line)
            if re.match(rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$", stripped):
                segments.append((True, buf))
                buf = []
                in_code = False
    if buf:
        segments.append((in_code, buf))

    out_parts: list[str] = []
    for is_code, seg_lines in segments:
        if is_code:
            out_parts.append("\n".join(seg_lines))
            continue
        out_parts.append(_unwrap_prose_lines(seg_lines))

    return "\n".join(out_parts)


def _unwrap_prose_lines(lines: list[str]) -> str:
    """将普通散文中的单换行平滑合并，保留空行分段与 Markdown 列表/标题。"""
    result_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if result_lines and result_lines[-1] != "":
                result_lines.append("")
            continue

        if not result_lines or result_lines[-1] == "" or _LIST_LINE_RE.match(line):
            result_lines.append(line.strip())
            continue

        prev = result_lines[-1]
        if _LIST_LINE_RE.match(prev) and len(prev) > 60:
            pass
        elif _LIST_LINE_RE.match(prev):
            # 短标题或列表项后接新行时，若上一行以句末标点结束则保持独立，否则并入
            if re.search(r"[.!?。！？:：]$", prev):
                result_lines.append(line.strip())
                continue

        cur = line.strip()
        # 跨行英文连字符断词 (e.g. "optimiza-\ntion" -> "optimization")
        if re.search(r"[A-Za-z]{2,}-$", prev) and re.match(r"^[a-z]{2,}", cur):
            result_lines[-1] = prev[:-1] + cur
            continue

        # 判断连接符：中日韩字符首尾相接不加空格，其余加单个空格
        prev_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\u3000-\u303f\uff01-\uffee]$", prev))
        cur_cjk = bool(re.match(r"^[\u3040-\u30ff\u3400-\u9fff\u3000-\u303f\uff01-\uffee]", cur))
        joiner = "" if (prev_cjk and cur_cjk) else " "
        result_lines[-1] = prev + joiner + cur

    return "\n".join(result_lines)


@dataclass
class DailyTextChunk:
    index: int
    text: str
    leading_sep: str
    is_code_block: bool = False


def _split_by_code_fences(normalized: str) -> list[tuple[str, bool]]:
    lines = normalized.split("\n")
    segments: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_code = False
    fence_char = ""
    fence_len = 0

    def flush(is_code: bool) -> None:
        if buf:
            segments.append(("\n".join(buf), is_code))
            buf.clear()

    for line in lines:
        stripped = line.lstrip()
        if not in_code:
            m = _FENCE_OPEN_RE.match(stripped)
            if m:
                flush(False)
                in_code = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                buf.append(line)
            else:
                buf.append(line)
        else:
            buf.append(line)
            if re.match(rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$", stripped):
                flush(True)
                in_code = False
                fence_char = ""
                fence_len = 0

    flush(in_code)
    return segments


def _split_large_paragraph(paragraph: str, max_chars: int) -> list[tuple[str, str]]:
    if len(paragraph) <= max_chars:
        return [(paragraph, "")]

    tokens = re.split(r"((?<=[.!?。！？])\s+|\n)", paragraph)
    results: list[tuple[str, str]] = []
    current = ""
    pending_sep = ""

    def push() -> None:
        nonlocal current, pending_sep
        if current:
            results.append((current, pending_sep))
            current = ""
            pending_sep = ""

    for idx, part in enumerate(tokens):
        if not part:
            continue
        if idx % 2 == 1:
            if current and len(current) + len(part) <= max_chars:
                current += part
            else:
                push()
                pending_sep = part
            continue

        if len(part) > max_chars:
            push()
            rem = part
            first_sub = True
            while len(rem) > max_chars:
                cut = rem.rfind(" ", 0, max_chars)
                if cut < max_chars // 2:
                    cut = max_chars
                slice_txt = rem[:cut]
                sep_for_this = pending_sep if first_sub else " "
                results.append((slice_txt, sep_for_this))
                pending_sep = ""
                first_sub = False
                rem = rem[cut:].lstrip(" ")
            if rem:
                current = rem
                if not first_sub:
                    pending_sep = " "
        elif not current:
            current = part
        elif len(current) + len(part) <= max_chars:
            current += part
        else:
            push()
            current = part

    push()
    return results


def split_daily_chunks(text: str, max_chunk_chars: int = 900, preserve_code_blocks: bool = True) -> list[DailyTextChunk]:
    """将长文本智能分块，保护围栏代码块原样透传，并无损保留段落空行分隔符。"""
    if not text:
        return []
    max_chunk_chars = max(200, int(max_chunk_chars))
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_segments = _split_by_code_fences(normalized) if preserve_code_blocks else [(normalized, False)]

    chunks: list[DailyTextChunk] = []
    next_leading_sep = ""

    for seg_idx, (seg_text, is_code) in enumerate(raw_segments):
        seg_sep = "" if seg_idx == 0 else "\n"
        if is_code:
            chunks.append(DailyTextChunk(
                index=len(chunks),
                text=seg_text,
                leading_sep=next_leading_sep + seg_sep,
                is_code_block=True,
            ))
            next_leading_sep = ""
            continue

        # 提取段落段首和段尾换行，确保 chunk.text 不带首尾换行符而被译文清理误删
        lead_match = re.match(r"^\n+", seg_text)
        lead_nls = lead_match.group(0) if lead_match else ""
        body = seg_text[len(lead_nls):]
        trail_match = re.search(r"\n+$", body)
        trail_nls = trail_match.group(0) if trail_match else ""
        if trail_nls:
            body = body[:-len(trail_nls)]

        if not body:
            next_leading_sep += seg_sep + lead_nls + trail_nls
            continue

        para_parts = re.split(r"(\n{2,})", body)
        current_text = ""
        current_sep = next_leading_sep + seg_sep + lead_nls
        next_leading_sep = ""

        def flush_prose() -> None:
            nonlocal current_text, current_sep
            if current_text:
                chunks.append(DailyTextChunk(
                    index=len(chunks),
                    text=current_text,
                    leading_sep=current_sep,
                    is_code_block=False,
                ))
                current_text = ""
                current_sep = ""

        for i, piece in enumerate(para_parts):
            if piece == "":
                continue
            if i % 2 == 1:
                if current_text:
                    next_para = para_parts[i + 1] if i + 1 < len(para_parts) else ""
                    if next_para and len(current_text) + len(piece) + len(next_para) <= max_chunk_chars:
                        current_text += piece
                    else:
                        flush_prose()
                        current_sep = piece
                else:
                    current_sep += piece
                continue

            if len(piece) > max_chunk_chars:
                flush_prose()
                sub_chunks = _split_large_paragraph(piece, max_chunk_chars)
                for s_idx, (sub_txt, sub_sep) in enumerate(sub_chunks):
                    chunks.append(DailyTextChunk(
                        index=len(chunks),
                        text=sub_txt,
                        leading_sep=(current_sep + sub_sep) if s_idx == 0 else sub_sep,
                        is_code_block=False,
                    ))
                    current_sep = ""
            elif not current_text:
                current_text = piece
            elif len(current_text) + len(piece) <= max_chunk_chars:
                current_text += piece
            else:
                flush_prose()
                current_text = piece

        flush_prose()
        next_leading_sep = current_sep + trail_nls

    if next_leading_sep and chunks:
        chunks[-1].text += next_leading_sep

    return chunks


def extract_trailing_context(text: str, max_chars: int = 180) -> str:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) <= max_chars:
        return cleaned
    tail = cleaned[-max_chars:]
    m = re.search(r"(?:[.!?。！？]\s+|\n+)([\s\S]+)$", tail)
    if m and len(m.group(1).strip()) >= 20:
        return m.group(1).strip()
    sp = tail.find(" ")
    if 0 < sp < 32:
        return tail[sp + 1:].strip()
    return tail.strip()


# ---------------------------------------------------------------------------
# 2. 语种识别与“零思考”中英智能互译路由
# ---------------------------------------------------------------------------

def detect_daily_source_language(text: str) -> str:
    """对日常输入做精准语种判定（区分中/英/日/韩/俄/阿/印/泰，处理中英混排比例）。"""
    sample = (text or "").strip()
    if not sample:
        return "en"
    if _JA_KANA_RE.search(sample):
        return "ja"
    if _KO_HANGUL_RE.search(sample):
        return "ko"
    cjk_count = len(_CJK_CHAR_RE.findall(sample))
    latin_chars = sum(len(w) for w in _LATIN_WORD_RE.findall(sample))
    if cjk_count > 0:
        # 哪怕是中英夹杂（如“请解释一下 Transformer 的原理”），只要中文汉字占主体或 >=3 个汉字且超过英文单词数，判为中文
        latin_words = len(_LATIN_WORD_RE.findall(sample))
        if cjk_count >= max(2, latin_words) or cjk_count * 2 >= latin_chars:
            return "zh"
    return detect_text_language(sample)


def resolve_daily_direction(text: str, source_lang: str = "auto", target_lang: str = AUTO_BILINGUAL_TARGET) -> tuple[str, str]:
    """解析最终的 (effective_source_lang, effective_target_lang)。

    当 target_lang == 'auto_bi'（智能互译）时：
    - 若源文本为中文系（zh / zh-hant / yue），自动译为 English ('en')；
    - 否则（英/日/韩/法/德等）自动译为简体中文 ('zh')。
    """
    src_code = (source_lang or "auto").strip()
    detected = detect_daily_source_language(text) if src_code.lower() in ("", "auto") else src_code

    tgt_code = (target_lang or AUTO_BILINGUAL_TARGET).strip()
    if tgt_code == AUTO_BILINGUAL_TARGET:
        if detected.lower() in ("zh", "zh-cn", "zh-hant", "zh-tw", "yue"):
            resolved_tgt = "en"
        else:
            resolved_tgt = "zh"
    else:
        resolved_tgt = tgt_code

    return detected, resolved_tgt


def language_display_name(code: str, ui_zh: bool = True) -> str:
    low = (code or "").lower()
    if ui_zh:
        return HunyuanLocalTranslator.language_names.get(low, code)
    return HunyuanLocalTranslator.language_names_en.get(low, code)


# ---------------------------------------------------------------------------
# 3. 术语表管理与精准触发（Hy-MT2 原生术语干预）
# ---------------------------------------------------------------------------

@dataclass
class DailyGlossaryEntry:
    id: str
    source: str
    target: str
    source_lang: str = "auto"
    target_lang: str = "auto"
    enabled: bool = True
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["DailyGlossaryEntry"]:
        if not isinstance(raw, dict):
            return None
        src = str(raw.get("source") or "").strip()
        tgt = str(raw.get("target") or "").strip()
        if not src or not tgt:
            return None
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
            source=src,
            target=tgt,
            source_lang=str(raw.get("source_lang") or "auto"),
            target_lang=str(raw.get("target_lang") or "auto"),
            enabled=bool(raw.get("enabled", True)),
            note=str(raw.get("note") or ""),
        )


def match_glossary_terms(
    text: str,
    entries: Iterable[DailyGlossaryEntry],
    source_lang: str = "auto",
    target_lang: str = "zh",
    max_terms: int = 20,
) -> list[DailyGlossaryEntry]:
    """仅筛选当前文本中真正出现的已启用术语，避免浪费上下文与干扰小模型。"""
    if not text or not text.strip():
        return []

    src_norm = (source_lang or "auto").strip().lower()
    tgt_norm = (target_lang or "").strip().lower()

    matched: list[DailyGlossaryEntry] = []
    seen_keys: set[str] = set()

    # 优先匹配较长词组，避免短词抢占语义
    sorted_entries = sorted(entries, key=lambda e: len(e.source.strip()), reverse=True)
    for entry in sorted_entries:
        if not entry.enabled:
            continue
        src = entry.source.strip()
        tgt = entry.target.strip()
        if not src or not tgt:
            continue

        e_src = (entry.source_lang or "auto").strip().lower()
        e_tgt = (entry.target_lang or "auto").strip().lower()
        if e_src not in ("", "auto", "any") and src_norm not in ("", "auto") and e_src != src_norm:
            continue
        if e_tgt not in ("", "auto", "any") and tgt_norm and e_tgt != tgt_norm:
            continue

        # 纯 ASCII 词条走词边界匹配，避免 "AI" 误命中 "wait"；CJK/符号走子串忽略大小写匹配
        is_ascii_word = bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9 _\-/.]*[A-Za-z0-9]$|^[A-Za-z0-9]$", src))
        if is_ascii_word:
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(src)}(?![A-Za-z0-9])", re.IGNORECASE)
        else:
            pat = re.compile(re.escape(src), re.IGNORECASE)

        if pat.search(text):
            dedupe = src.casefold()
            if dedupe not in seen_keys:
                seen_keys.add(dedupe)
                matched.append(entry)
                if len(matched) >= max_terms:
                    break

    return matched


class DailyGlossaryStore:
    """持久化日常翻译术语表（支持增删改、去重、JSON/CSV/TSV 导入导出）。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (user_data_dir() / "daily_glossary.json")
        self.entries: list[DailyGlossaryEntry] = []
        self.load()

    def load(self) -> None:
        self.entries = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw_list = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for raw in raw_list:
                entry = DailyGlossaryEntry.from_dict(raw)
                if entry:
                    self.entries.append(entry)
        except (OSError, ValueError, TypeError):
            logger.warning("读取术语表失败，将使用空列表: %s", self.path)

    def save(self) -> None:
        payload = {"version": 1, "entries": [asdict(e) for e in self.entries]}
        _atomic_write_json(self.path, payload)

    def add_or_update(
        self,
        source: str,
        target: str,
        source_lang: str = "auto",
        target_lang: str = "auto",
        note: str = "",
    ) -> DailyGlossaryEntry:
        src = source.strip()
        tgt = target.strip()
        if not src or not tgt:
            raise ValueError("原文术语与目标译词均不能为空")
        for existing in self.entries:
            if existing.source.casefold() == src.casefold() and existing.target_lang.lower() == target_lang.lower():
                existing.source = src
                existing.target = tgt
                existing.enabled = True
                if note:
                    existing.note = note.strip()
                self.save()
                return existing
        entry = DailyGlossaryEntry(
            id=uuid.uuid4().hex[:12],
            source=src,
            target=tgt,
            source_lang=source_lang or "auto",
            target_lang=target_lang or "auto",
            enabled=True,
            note=note.strip(),
        )
        self.entries.insert(0, entry)
        self.save()
        return entry

    def set_enabled(self, entry_id: str, enabled: bool) -> None:
        for e in self.entries:
            if e.id == entry_id:
                e.enabled = bool(enabled)
                self.save()
                return

    def remove(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        self.save()

    def clear(self) -> None:
        self.entries.clear()
        self.save()

    def import_file(self, file_path: Path) -> int:
        """支持导入 JSON（对象列表或 {'entries': [...]}）及 CSV/TSV 两列术语文件，返回导入条数。"""
        file_path = Path(file_path)
        raw_text = file_path.read_text(encoding="utf-8-sig")
        imported = 0
        if file_path.suffix.lower() == ".json":
            data = json.loads(raw_text)
            items = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in items:
                if isinstance(item, dict):
                    src = str(item.get("source") or item.get("src") or "").strip()
                    tgt = str(item.get("target") or item.get("tgt") or "").strip()
                    if src and tgt:
                        self.add_or_update(
                            src,
                            tgt,
                            source_lang=str(item.get("source_lang") or "auto"),
                            target_lang=str(item.get("target_lang") or "auto"),
                            note=str(item.get("note") or ""),
                        )
                        imported += 1
            return imported

        # CSV / TSV / TXT（支持 Tab、逗号或 -> 分隔）
        delimiter = "\t" if "\t" in raw_text or file_path.suffix.lower() == ".tsv" else ","
        reader = csv.reader(io.StringIO(raw_text), delimiter=delimiter)
        for idx, row in enumerate(reader):
            if len(row) < 2:
                # 兼容 "source -> target" 纯文本行
                line = row[0].strip() if row else ""
                if "->" in line:
                    parts = [p.strip() for p in line.split("->", 1)]
                    if len(parts) == 2 and parts[0] and parts[1]:
                        self.add_or_update(parts[0], parts[1])
                        imported += 1
                continue
            src, tgt = row[0].strip(), row[1].strip()
            if not src or not tgt:
                continue
            if idx == 0 and src.lower() in ("source", "src", "原文", "term") and tgt.lower() in ("target", "tgt", "译文", "translation"):
                continue
            note = row[2].strip() if len(row) > 2 else ""
            self.add_or_update(src, tgt, note=note)
            imported += 1
        return imported

    def export_json(self, file_path: Path) -> int:
        payload = {"version": 1, "entries": [asdict(e) for e in self.entries]}
        _atomic_write_json(Path(file_path), payload)
        return len(self.entries)


# ---------------------------------------------------------------------------
# 4. 本地翻译历史记录
# ---------------------------------------------------------------------------

@dataclass
class DailyHistoryItem:
    id: str
    source: str
    target: str
    source_lang: str
    target_lang: str
    style: str
    matched_terms: int
    timestamp: float

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["DailyHistoryItem"]:
        if not isinstance(raw, dict):
            return None
        src = str(raw.get("source") or "")
        tgt = str(raw.get("target") or "")
        if not src.strip() or not tgt.strip():
            return None
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
            source=src,
            target=tgt,
            source_lang=str(raw.get("source_lang") or "auto"),
            target_lang=str(raw.get("target_lang") or "zh"),
            style=str(raw.get("style") or "default"),
            matched_terms=int(raw.get("matched_terms") or 0),
            timestamp=float(raw.get("timestamp") or time.time()),
        )


class DailyHistoryStore:
    """持久化日常翻译历史（自动去重、上限 200 条、支持关键词检索）。"""

    def __init__(self, path: Optional[Path] = None, max_items: int = 200):
        self.path = Path(path) if path else (user_data_dir() / "daily_history.json")
        self.max_items = max_items
        self.items: list[DailyHistoryItem] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw_list = data.get("items", []) if isinstance(data, dict) else []
            for raw in raw_list:
                item = DailyHistoryItem.from_dict(raw)
                if item:
                    self.items.append(item)
        except (OSError, ValueError, TypeError):
            logger.warning("读取翻译历史失败: %s", self.path)

    def save(self) -> None:
        payload = {"version": 1, "items": [asdict(i) for i in self.items[:self.max_items]]}
        _atomic_write_json(self.path, payload)

    def add(
        self,
        source: str,
        target: str,
        source_lang: str,
        target_lang: str,
        style: str = "default",
        matched_terms: int = 0,
    ) -> Optional[DailyHistoryItem]:
        src_clean = (source or "").strip()
        tgt_clean = (target or "").strip()
        if not src_clean or not tgt_clean:
            return None
        # 跳过与最近一条完全相同的连续重复记录
        if self.items:
            top = self.items[0]
            if top.source.strip() == src_clean and top.target.strip() == tgt_clean and top.target_lang == target_lang:
                return top
        item = DailyHistoryItem(
            id=uuid.uuid4().hex[:12],
            source=source,
            target=target,
            source_lang=source_lang,
            target_lang=target_lang,
            style=style,
            matched_terms=matched_terms,
            timestamp=time.time(),
        )
        self.items.insert(0, item)
        if len(self.items) > self.max_items:
            self.items = self.items[:self.max_items]
        self.save()
        return item

    def remove(self, item_id: str) -> None:
        self.items = [i for i in self.items if i.id != item_id]
        self.save()

    def clear(self) -> None:
        self.items.clear()
        self.save()


# ---------------------------------------------------------------------------
# 5. 日常翻译管线与常驻本地模型管理
# ---------------------------------------------------------------------------

def clean_daily_output(raw_text: str) -> str:
    """清理模型输出中偶尔泄漏的提示词前缀或围栏。"""
    cleaned = clean_translated_candidate(raw_text or "")
    cleaned = _DAILY_LEAK_RE.sub("", cleaned).strip()
    return cleaned


@dataclass
class DailyTranslationResult:
    translated_text: str
    detected_source_lang: str
    resolved_target_lang: str
    matched_terms: list[DailyGlossaryEntry]
    chunks_count: int
    elapsed_ms: int
    tokens_generated: int
    tokens_per_sec: float
    cancelled: bool = False
    truncated: bool = False
    truncated_chunks: int = 0
    completed_chunks: int = 0
    truncation_reason: str = ""


_CJK_LANG_CODES = {"zh", "zh-cn", "zh-hant", "zh-tw", "yue", "ja", "ko"}


def estimate_token_count(text: str) -> int:
    """按字符脚本类别保守估算 Hy-MT2-1.8B 分词消耗，防止上下文溢出。"""
    if not text:
        return 0
    total = 0.0
    for ch in text:
        code = ord(ch)
        if code < 128:
            total += 0.40
        elif (
            (0x3000 <= code <= 0x9FFF)
            or (0xAC00 <= code <= 0xD7AF)
            or (0xFF00 <= code <= 0xFFEF)
        ):
            total += 0.85
        else:
            total += 1.50
    return max(1, math.ceil(total))


def compute_safe_chunk_chars(
    context_size: int,
    source_lang: str,
    target_lang: str,
    sample_text: str = "",
) -> int:
    """结合推理窗口、指令冗余及语言膨胀比计算安全的单块源字符上限。"""
    ctx = max(1024, int(context_size or 4096))
    overhead = 640 if ctx >= 4096 else 320
    usable = max(512, ctx - overhead)

    s_is_cjk = (source_lang or "").strip().lower() in _CJK_LANG_CODES
    t_is_cjk = (target_lang or "").strip().lower() in _CJK_LANG_CODES
    if s_is_cjk and not t_is_cjk:
        exp_ratio = 2.6
        quality_cap = 900 if ctx >= 8192 else 600
    elif not s_is_cjk and t_is_cjk:
        exp_ratio = 1.4
        quality_cap = 900 if ctx >= 8192 else 700
    else:
        exp_ratio = 1.8
        quality_cap = 900 if ctx >= 8192 else 700

    max_in_tokens = int(usable / (1.0 + exp_ratio))
    sample = (sample_text or "")[:1000]
    if sample:
        tok_per_char = max(0.35, estimate_token_count(sample) / len(sample))
    else:
        tok_per_char = 0.85 if s_is_cjk else 0.45

    safe_chars = int(max_in_tokens / tok_per_char)
    return max(200, min(quality_cap, safe_chars))


def compute_chunk_max_tokens(
    chunk_text: str,
    prompt_text: str,
    source_lang: str,
    target_lang: str,
    context_size: int,
) -> int:
    """根据当前块提示词占用与语向倍率计算安全 max_tokens，防短文失控生成与窗口溢出。"""
    ctx = max(1024, int(context_size or 4096))
    prompt_tokens = estimate_token_count(prompt_text) + 64
    ctx_available = max(64, ctx - prompt_tokens - 64)

    src_tokens = estimate_token_count(chunk_text)
    s_is_cjk = (source_lang or "").strip().lower() in _CJK_LANG_CODES
    t_is_cjk = (target_lang or "").strip().lower() in _CJK_LANG_CODES
    if s_is_cjk and not t_is_cjk:
        mult = 3.2
    elif not s_is_cjk and t_is_cjk:
        mult = 2.0
    else:
        mult = 2.5

    desired = max(256, math.ceil(src_tokens * mult) + 128)
    hard_cap = min(2048, int(DAILY_INFERENCE_PARAMS.get("max_tokens", 4096)))
    return min(ctx_available, desired, hard_cap)


def _estimate_expected_chars(src_text: str, src_lang: str, tgt_lang: str) -> float:
    src_len = max(1, len((src_text or "").strip()))
    s_is_cjk = (src_lang or "").lower() in _CJK_LANG_CODES
    t_is_cjk = (tgt_lang or "").lower() in _CJK_LANG_CODES
    if not s_is_cjk and t_is_cjk:
        ratio = 0.45
    elif s_is_cjk and not t_is_cjk:
        ratio = 2.6
    else:
        ratio = 1.0
    return max(8.0, src_len * ratio)


def translate_daily_text(
    runtime: LlamaServerRuntime,
    text: str,
    source_lang: str = "auto",
    target_lang: str = AUTO_BILINGUAL_TARGET,
    style_key: str = "default",
    glossary: Optional[list[DailyGlossaryEntry]] = None,
    use_glossary: bool = True,
    max_chunk_chars: Optional[int] = None,
    on_partial: Optional[Callable[[str, dict], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    max_input_chars: Optional[int] = DAILY_MAX_INPUT_CHARS,
    max_output_chars: Optional[int] = DAILY_MAX_OUTPUT_CHARS,
) -> DailyTranslationResult:
    """执行日常文本翻译，支持自动互译、Hy-MT2 原生术语表、风格切换、代码块直通、字数/上下文保护与流式输出。"""
    started = time.monotonic()
    if max_input_chars is not None and text and len(text) > max_input_chars:
        raise InputTooLongError(f"输入字数超出单次上限 ({len(text)}/{max_input_chars} 字)")

    if not text or not text.strip():
        return DailyTranslationResult(
            translated_text="",
            detected_source_lang="en",
            resolved_target_lang="zh",
            matched_terms=[],
            chunks_count=0,
            elapsed_ms=0,
            tokens_generated=0,
            tokens_per_sec=0.0,
        )

    detected_src, resolved_tgt = resolve_daily_direction(text, source_lang=source_lang, target_lang=target_lang)

    # 独立 URL / 邮箱直接原样返回，省掉无意义的模型调用
    if is_standalone_url_or_code(text):
        out_text = text[:max_output_chars] if max_output_chars is not None and len(text) > max_output_chars else text
        was_truncated = len(out_text) < len(text)
        if on_partial:
            on_partial(out_text, {"tokens": 0, "tok_s": 0.0, "chunk": 1, "total_chunks": 1, "progress": 100})
        return DailyTranslationResult(
            translated_text=out_text,
            detected_source_lang=detected_src,
            resolved_target_lang=resolved_tgt,
            matched_terms=[],
            chunks_count=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            tokens_generated=0,
            tokens_per_sec=0.0,
            truncated=was_truncated,
            truncated_chunks=1 if was_truncated else 0,
            completed_chunks=1,
            truncation_reason="output_limit" if was_truncated else "",
        )

    use_english_prompt = not should_use_chinese_prompt(detected_src, resolved_tgt)
    tgt_key = resolved_tgt.lower()
    if tgt_key not in HunyuanLocalTranslator.language_names:
        raise ValueError(f"不支持的目标语言: {resolved_tgt}")
    target_lang_desc = (
        HunyuanLocalTranslator.language_names_en[tgt_key]
        if use_english_prompt
        else HunyuanLocalTranslator.language_names[tgt_key]
    )

    ctx_size = max(1024, int(getattr(runtime, "context_size", 4096) or 4096))
    safe_chunk_chars = compute_safe_chunk_chars(ctx_size, detected_src, resolved_tgt, sample_text=text)
    effective_chunk_chars = (
        min(int(max_chunk_chars), safe_chunk_chars) if max_chunk_chars is not None else safe_chunk_chars
    )

    chunks = split_daily_chunks(text, max_chunk_chars=effective_chunk_chars, preserve_code_blocks=True)
    all_matched_terms: list[DailyGlossaryEntry] = []
    seen_term_ids: set[str] = set()
    pieces: list[str] = []
    prev_context_text = ""
    total_tokens = 0
    was_cancelled = False
    truncated = False
    truncated_chunks = 0
    completed_chunks = 0
    truncation_reason = ""

    chunk_weights = [max(1, len(c.text.strip())) for c in chunks]
    total_weight = max(1, sum(chunk_weights))
    completed_weight = 0.0
    max_progress_pct = 0

    def calc_progress(cur_weight: float, sub_ratio: float, is_final_done: bool = False) -> int:
        nonlocal max_progress_pct
        if is_final_done:
            max_progress_pct = 100
            return 100
        raw = int(round(((completed_weight + cur_weight * max(0.0, min(1.0, sub_ratio))) / total_weight) * 100))
        pct = max(max_progress_pct, min(99, max(1, raw)))
        max_progress_pct = pct
        return pct

    def current_tps() -> float:
        secs = time.monotonic() - started
        if secs <= 0.05 or total_tokens == 0:
            return 0.0
        return round(total_tokens / secs, 1)

    for chunk in chunks:
        if cancelled and cancelled():
            was_cancelled = True
            break

        cur_len = sum(len(p) for p in pieces)
        if max_output_chars is not None and cur_len >= max_output_chars:
            truncated = True
            truncation_reason = truncation_reason or "output_limit"
            break

        cur_weight = chunk_weights[chunk.index]
        is_last_chunk = (chunk.index + 1 == len(chunks))

        if chunk.leading_sep:
            if max_output_chars is not None and cur_len + len(chunk.leading_sep) > max_output_chars:
                remain = max(0, max_output_chars - cur_len)
                pieces.append(chunk.leading_sep[:remain])
                truncated = True
                truncation_reason = truncation_reason or "output_limit"
                break
            pieces.append(chunk.leading_sep)
            cur_len += len(chunk.leading_sep)

        if chunk.is_code_block or not chunk.text.strip():
            block_txt = chunk.text
            hit_block_limit = False
            if max_output_chars is not None and cur_len + len(block_txt) > max_output_chars:
                remain = max(0, max_output_chars - cur_len)
                block_txt = block_txt[:remain]
                hit_block_limit = True
                truncated = True
                truncation_reason = truncation_reason or "output_limit"
            pieces.append(block_txt)
            completed_chunks += 1
            completed_weight += cur_weight
            if on_partial:
                on_partial("".join(pieces), {
                    "tokens": total_tokens,
                    "tok_s": current_tps(),
                    "chunk": chunk.index + 1,
                    "total_chunks": len(chunks),
                    "progress": calc_progress(0.0, 0.0, is_final_done=is_last_chunk or hit_block_limit),
                })
            if hit_block_limit:
                break
            continue

        chunk_terms: list[DailyGlossaryEntry] = []
        if use_glossary and glossary:
            chunk_terms = match_glossary_terms(
                chunk.text,
                glossary,
                source_lang=detected_src,
                target_lang=resolved_tgt,
            )
            for term in chunk_terms:
                if term.id not in seen_term_ids:
                    seen_term_ids.add(term.id)
                    all_matched_terms.append(term)

        protected_chunk, url_mapping = protect_inline_urls(chunk.text)
        prompt_terms = list(chunk_terms)
        prompt_ctx = prev_context_text
        prompt = build_daily_prompt(
            source_text=protected_chunk,
            target_lang=target_lang_desc,
            glossary_matches=[(t.source, t.target) for t in prompt_terms],
            style_key=style_key,
            context_text=prompt_ctx,
            use_english=use_english_prompt,
        )
        prompt_limit = int(ctx_size * 0.55)
        if estimate_token_count(prompt) + 64 > prompt_limit and prompt_ctx:
            prompt_ctx = ""
            prompt = build_daily_prompt(
                source_text=protected_chunk,
                target_lang=target_lang_desc,
                glossary_matches=[(t.source, t.target) for t in prompt_terms],
                style_key=style_key,
                context_text="",
                use_english=use_english_prompt,
            )
        if estimate_token_count(prompt) + 64 > prompt_limit and len(prompt_terms) > 5:
            prompt_terms = prompt_terms[:5]
            prompt = build_daily_prompt(
                source_text=protected_chunk,
                target_lang=target_lang_desc,
                glossary_matches=[(t.source, t.target) for t in prompt_terms],
                style_key=style_key,
                context_text="",
                use_english=use_english_prompt,
            )

        call_params = dict(DAILY_INFERENCE_PARAMS)
        call_params["max_tokens"] = compute_chunk_max_tokens(
            chunk_text=chunk.text,
            prompt_text=prompt,
            source_lang=detected_src,
            target_lang=resolved_tgt,
            context_size=ctx_size,
        )

        expected_chars = _estimate_expected_chars(chunk.text, detected_src, resolved_tgt)
        chunk_raw = ""
        finish_reason: Optional[str] = None
        hit_output_limit = False
        prefix_text = "".join(pieces)
        prefix_len = len(prefix_text)

        if hasattr(runtime, "chat_completion_stream"):
            stream_meta: dict = {}
            stream_fn = runtime.chat_completion_stream
            stream_kwargs = {
                "prompt": prompt,
                "cache_prompt": True,
                "cancelled": cancelled,
                **call_params,
            }
            try:
                sig = inspect.signature(stream_fn)
                accepts_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                if accepts_var_kw or "stream_meta" in sig.parameters:
                    stream_kwargs["stream_meta"] = stream_meta
            except (TypeError, ValueError):
                pass

            for delta in stream_fn(**stream_kwargs):
                if cancelled and cancelled():
                    was_cancelled = True
                    break
                chunk_raw += delta
                total_tokens += 1
                preview_chunk = restore_inline_urls(clean_daily_output(chunk_raw), url_mapping)
                if max_output_chars is not None and prefix_len + len(preview_chunk) >= max_output_chars:
                    remain = max(0, max_output_chars - prefix_len)
                    clipped_preview = preview_chunk[:remain]
                    hit_output_limit = True
                    truncated = True
                    truncation_reason = truncation_reason or "output_limit"
                    if on_partial:
                        on_partial(prefix_text + clipped_preview, {
                            "tokens": total_tokens,
                            "tok_s": current_tps(),
                            "chunk": chunk.index + 1,
                            "total_chunks": len(chunks),
                            "progress": calc_progress(cur_weight, 1.0, is_final_done=True),
                        })
                    break
                if on_partial:
                    sub_ratio = min(0.95, len(chunk_raw) / expected_chars)
                    on_partial(prefix_text + preview_chunk, {
                        "tokens": total_tokens,
                        "tok_s": current_tps(),
                        "chunk": chunk.index + 1,
                        "total_chunks": len(chunks),
                        "progress": calc_progress(cur_weight, sub_ratio),
                    })
            if not was_cancelled and not hit_output_limit:
                if "finish_reason" in stream_meta:
                    finish_reason = stream_meta.get("finish_reason")
                else:
                    raw_fr = getattr(runtime, "last_stream_finish_reason", None)
                    finish_reason = raw_fr if isinstance(raw_fr, str) else None
        else:
            res = runtime.chat_completion(
                prompt=prompt,
                cache_prompt=True,
                **call_params,
            )
            choice0 = (res.get("choices") or [{}])[0]
            chunk_raw = choice0["message"]["content"]
            finish_reason = choice0.get("finish_reason")
            total_tokens += max(1, len(chunk_raw) // 2)

        if was_cancelled:
            if chunk_raw:
                cancelled_chunk = restore_inline_urls(clean_daily_output(chunk_raw), url_mapping)
                if max_output_chars is not None:
                    cancelled_chunk = cancelled_chunk[: max(0, max_output_chars - prefix_len)]
                pieces.append(cancelled_chunk)
            break

        cleaned_chunk = restore_inline_urls(clean_daily_output(chunk_raw), url_mapping)
        if max_output_chars is not None and prefix_len + len(cleaned_chunk) > max_output_chars:
            cleaned_chunk = cleaned_chunk[: max(0, max_output_chars - prefix_len)]
            hit_output_limit = True
            truncated = True
            truncation_reason = truncation_reason or "output_limit"

        if finish_reason == "length":
            truncated = True
            truncated_chunks += 1
            truncation_reason = truncation_reason or "length"

        pieces.append(cleaned_chunk)
        completed_chunks += 1
        prev_context_text = extract_trailing_context(chunk.text, max_chars=180)
        completed_weight += cur_weight
        if on_partial and not hit_output_limit:
            on_partial("".join(pieces), {
                "tokens": total_tokens,
                "tok_s": current_tps(),
                "chunk": chunk.index + 1,
                "total_chunks": len(chunks),
                "progress": calc_progress(0.0, 0.0, is_final_done=is_last_chunk),
            })
        if hit_output_limit:
            break

    final_text = "".join(pieces)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return DailyTranslationResult(
        translated_text=final_text,
        detected_source_lang=detected_src,
        resolved_target_lang=resolved_tgt,
        matched_terms=all_matched_terms,
        chunks_count=len(chunks),
        elapsed_ms=elapsed_ms,
        tokens_generated=total_tokens,
        tokens_per_sec=current_tps(),
        cancelled=was_cancelled,
        truncated=truncated,
        truncated_chunks=truncated_chunks,
        completed_chunks=completed_chunks,
        truncation_reason=truncation_reason,
    )


class DailyRuntimeManager:
    """为日常翻译维护可复用的本地常驻 llama-server 实例，避免每句重复冷启动。"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self._lock = threading.Lock()
        self._runtime: Optional[LlamaServerRuntime] = None
        self._signature: Optional[tuple] = None

    def is_running(self) -> bool:
        with self._lock:
            return bool(
                self._runtime is not None
                and self._runtime.process is not None
                and self._runtime.process.poll() is None
            )

    def ensure_started(
        self,
        threads: Optional[int] = None,
        context_size: Optional[int] = None,
        n_gpu_layers: Optional[int] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> LlamaServerRuntime:
        restrict_network_to_loopback()
        prof = detect_hardware_profile(
            override_threads=threads,
            override_parallel=1,
            override_gpu_layers=n_gpu_layers,
            override_ctx_size=context_size,
        )
        sig = (prof.threads, prof.context_size, prof.n_gpu_layers)

        with self._lock:
            if (
                self._runtime is not None
                and self._runtime.process is not None
                and self._runtime.process.poll() is None
                and self._signature == sig
            ):
                return self._runtime

            if self._runtime is not None:
                self._runtime.stop()
                self._runtime = None

            model_path = resolve_translation_model(self.root)
            runtime = LlamaServerRuntime(
                model_path=model_path,
                threads=prof.threads,
                context_size=prof.context_size,
                parallel=1,
                n_gpu_layers=prof.n_gpu_layers,
            )
            self._runtime = runtime
            self._signature = sig

        try:
            runtime.start(cancelled=cancelled)
        except BaseException:
            self.stop()
            raise
        return runtime

    def stop(self) -> None:
        with self._lock:
            rt = self._runtime
            self._runtime = None
            self._signature = None
        if rt is not None:
            rt.stop()
