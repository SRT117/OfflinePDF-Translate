# SPDX-License-Identifier: AGPL-3.0-only
"""与 BabelDOC 引擎无缝对接的腾讯 Hy-MT2 纯血官方模板翻译适配器。"""

import logging
import re
import threading
from typing import Optional, Callable

from .offline import configure_cache
configure_cache()
from babeldoc.translator.translator import BaseTranslator

from .llama_runtime import LlamaServerRuntime
from .prompts import (
    DEFAULT_INFERENCE_PARAMS,
    SUPPORTED_LANGUAGES,
    prompt_default,
    prompt_structural_protection,
    prompt_structural_retry,
    prompt_structural_with_background,
    prompt_with_background,
    should_use_chinese_prompt,
)
from .validator import PLACEHOLDER_PATTERN, PlaceholderValidator, extract_placeholders, normalize_token

logger = logging.getLogger("offlinepdf-translate.translator")


def is_standalone_url_or_code(text: str) -> bool:
    """检测文本是否为独立的 URL、DOI 链接、邮箱或纯路径，避免调用大模型与误报漏译。"""
    if not text:
        return False
    stripped = text.strip()
    if re.match(r"^(?:https?://|www\.|doi\.org/|mailto:)\S+$", stripped, re.IGNORECASE):
        return True
    if re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", stripped):
        return True
    return False


_INLINE_URL_RE = re.compile(
    r"(?:https?://|www\.|doi\.org/|mailto:)[^\s<>\"'\)\]\},;，。；）】》]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    re.IGNORECASE,
)


def protect_inline_urls(text: str) -> tuple[str, list[tuple[str, str]]]:
    """将混排段落中的 URL/DOI/邮箱替换为中性占位符，防止小模型改写链接路径或数字。"""
    if not text:
        return text, []
    mapping: list[tuple[str, str]] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,;:!?")
        suffix = raw[len(trimmed):]
        token = f"[u{len(mapping) + 1}]"
        mapping.append((token, trimmed))
        return f" {token}{suffix}"

    protected = _INLINE_URL_RE.sub(_replace, text)
    return re.sub(r"  +", " ", protected), mapping


def _normalize_placeholder_digits(raw: str) -> str:
    """将全角数字及阿拉伯-印地/波斯数字统一转换为 ASCII 数字，便于占位符修复。"""
    trans = str.maketrans("０１２３４５６７８９٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "012345678901234567890123456789")
    return raw.translate(trans)


def restore_inline_urls(text: str, mapping: list[tuple[str, str]], source_text: str = "") -> str:
    """恢复被临时保护的 URL/DOI/邮箱，兼容西里尔 у、全角字符与空格变体，并清理幻觉 [u数字]。"""
    if not text:
        return text
    res = _normalize_placeholder_digits(text)
    for token, original in mapping or []:
        idx = token[2:-1]
        pat = re.compile(rf"[\[\【\(（\{{「『]\s*[uуｕ]\s*{re.escape(idx)}\s*[\]\】\)）\}}」』]", re.IGNORECASE)
        res = pat.sub(lambda _m, orig=original: orig, res)

    # 清理大模型幻觉多生成的、且原文中不存在的 [u数字]/[у数字] 占位符
    def _strip_hallucinated_u(m: re.Match) -> str:
        raw_tok = m.group(0)
        if source_text and raw_tok in source_text:
            return raw_tok
        return ""

    res = re.sub(r"[\[\【\(（]\s*[uуｕ]\s*\d+\s*[\]\】\)）]", _strip_hallucinated_u, res, flags=re.IGNORECASE)
    return re.sub(r"  +", " ", res).strip()


_NON_EN_LATIN_DIACRITIC_RE = re.compile(r"[\u00c0-\u024f\u1e00-\u1eff]")
_NON_EN_LATIN_STOPWORDS_RE = re.compile(
    r"\b(?:"
    r"der|die|das|und|ist|nicht|mit|für|eine|einer|einem|werden|wurde|durch|auch|sich|auf|"
    r"les|des|une|est|dans|pour|avec|sont|cette|nous|vous|mais|plus|comme|entre|"
    r"los|las|una|del|con|por|para|que|como|pero|entre|sobre|este|esta|"
    r"não|mais|uma|pelo|pela|dos|das|como|também|entre|"
    r"sono|della|degli|nella|questo|questa|anche|come|con|per|tra|"
    r"het|een|van|zijn|voor|niet|ook|wordt|met|deze"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_non_english_latin(text: str) -> bool:
    """判断拉丁字母文本是否明显为非英语（用于 auto->en 漏译复检，避免误判为同语种而跳过）。"""
    if not text:
        return False
    if _NON_EN_LATIN_DIACRITIC_RE.search(text):
        return True
    return len(_NON_EN_LATIN_STOPWORDS_RE.findall(text)) >= 2


def detect_text_language(text: str) -> str:
    """基于字符脚本轻量识别输入文本主语言，用于 lang_in='auto' 时的双轨模板分流与质量核查。"""
    prose = PLACEHOLDER_PATTERN.sub("", text or "")
    if not prose.strip():
        return "en"
    if re.search(r"[\u3040-\u30ff]", prose):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", prose):
        return "ko"
    if re.search(r"[\u3400-\u9fff]", prose):
        return "zh"
    if re.search(r"[\u0f00-\u0fff]", prose):
        return "bo"
    if re.search(r"[\u1800-\u18af]", prose):
        return "mn"
    if re.search(r"[\u0e00-\u0e7f]", prose):
        return "th"
    if re.search(r"[\u1780-\u17ff]", prose):
        return "km"
    if re.search(r"[\u1000-\u109f]", prose):
        return "my"
    if re.search(r"[\u0590-\u05ff]", prose):
        return "he"
    if re.search(r"[\u0980-\u09ff]", prose):
        return "bn"
    if re.search(r"[\u0a80-\u0aff]", prose):
        return "gu"
    if re.search(r"[\u0b80-\u0bff]", prose):
        return "ta"
    if re.search(r"[\u0c00-\u0c7f]", prose):
        return "te"
    if re.search(r"[\u0900-\u097f]", prose):
        return "hi"
    if re.search(r"[\u0400-\u04ff]", prose):
        if re.search(r"[\u04d8\u04d9\u0492\u0493\u049a\u049b\u04a2\u04a3\u04b0\u04b1\u04ba\u04bb]", prose):
            return "kk"
        if re.search(r"[\u0404\u0454\u0407\u0457\u0490\u0491]", prose):
            return "uk"
        if re.search(r"[\u04e8\u04e9\u04ae\u04af]", prose):
            return "mn"
        return "ru"
    if re.search(r"[\u0600-\u06ff]", prose):
        if re.search(r"[\u06d5\u06c6\u06c8\u06c7\u06d0\u06ad]", prose):
            return "ug"
        if re.search(r"[\u0679\u0688\u0691\u06ba\u06d2\u06be]", prose):
            return "ur"
        if re.search(r"[\u067e\u0686\u0698\u06af\u06a9\u06cc]", prose):
            return "fa"
        return "ar"
    # 拉丁语系高区分度字符轻量识别（不误伤纯 ASCII 英文）
    if re.search(r"[\u1ea0-\u1ef9\u0102\u0103\u0110\u0111\u01a0\u01a1\u01af\u01b0]", prose):
        return "vi"
    if re.search(r"[\u011e\u011f\u0130\u0131\u015e\u015f]", prose):
        return "tr"
    if re.search(r"[\u0104\u0105\u0106\u0107\u0118\u0119\u0141\u0142\u0143\u0144\u015a\u015b\u0179\u017a\u017b\u017c]", prose):
        return "pl"
    if re.search(r"[\u010c\u010d\u010e\u010f\u011a\u011b\u0147\u0148\u0158\u0159\u0160\u0161\u0164\u0165\u016e\u016f\u017d\u017e]", prose):
        return "cs"
    if "ß" in prose:
        return "de"
    if re.search(r"[ñÑ¿¡]", prose):
        return "es"
    if re.search(r"[ãõÃÕ]", prose):
        return "pt"
    return "en"


def pad_structural_tokens(text: str) -> str:
    """在输入大模型前对断词、软换行、专有符号、标点与占位符进行正规化处理。

    1. 规范化学术文献专有字体编码（如 AIP 的连字伪影、控制字符）；
    2. 拼接跨行连字断词 (如 dis- tance -> distance) 与换行；
    3. 保留参考文献 [v数字] 与多行列表的独立换行结构；
    4. 标点与文献引用占位符自然倒置绑定；
    5. 添加词边界空格保护。
    """
    if not text:
        return text

    # 0. 规范化学术文献中 AIP/出版社专有数学字体编码的异常控制字符与连字伪影
    # 消除连续连字伪影（如由 U+FB03 展开而来的虚假平方根上划线 ffi...ffi）
    res = re.sub(r"(?:ﬃ|(?:ffi)){2,}\s*", "", text, flags=re.IGNORECASE)
    # 映射常见专有符号编码到标准数学/文本符号
    charmap = {
        "\x01": "(",
        "\x02": ")",
        "\x03": "-",       # 减号 / 负号 (如 -L < x < L, 10^-4)
        "\x04": "×",       # 乘号 (如 256 × 256, 1.66 × 10^4)
        "\x05": "≤",       # 小于等于号 (如 R/R0 ≤ 4)
        "\x06": "·",       # 点乘
        "\x07": "*",       # 上标星号 (如 T*, e*)
        "ð": "(",
        "Þ": ")",
        "¼": "=",          # 等号 (如 R/R0 = 6)
        "þ": "+",          # 加号 (如 x2 + R2)
    }
    for k, v in charmap.items():
        res = res.replace(k, v)

    # 0.5 参考文献连续条目识别：若长段落包含递增的 [v1]...[v2]...[v3]... 且含出版元数据，
    # BabelDOC 底层分词容易将换行压缩为空格，在此主动恢复每条文献的独立换行结构。
    # 严格确保段落为真实参考文献列表（必须包含年份与出版元数据且序号连续递增），严禁误伤普通正文段落
    if len(res) > 150 and "[v1]" in res:
        tokens = re.findall(r"\[v(\d+)\]", res)
        if tokens and tokens[0] == "1" and len(tokens) >= 3:
            has_year = bool(re.search(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", res))
            has_pub_meta = bool(
                re.search(
                    r"(?:press|report|journal|vol\b|no\b|pp\b|university|publisher|proceedings|trans\b|struct\b|phys\b|chem\b|eng\b|doi\b|et al\b)",
                    res,
                    re.IGNORECASE,
                )
            )
            if has_year and has_pub_meta:
                nums = [int(x) for x in tokens]
                if nums == list(range(nums[0], nums[0] + len(nums))):
                    res = re.sub(r"([.\)）”’\d])\s*(\[v\d+\])", r"\1\n\2", res)

    # 1. 断词与软换行拼接，同时保护参考文献与编号列表的条目换行
    res = re.sub(r"([A-Za-z]{2,})-\s*(?:\r?\n)+\s*([A-Za-z]{2,})", r"\1\2", res)
    res = re.sub(r"([A-Za-z]{3,})-\s+([a-z]{3,})", r"\1\2", res)
    res = re.sub(
        r"(?<!\n)\r?\n(?!\r?\n|[ \t]*[-*+]\s|[ \t]*\d+[\.\sA-Za-z]|[ \t]*\[\d+\]|[ \t]*\[v\d+\]|[ \t]*\{\{v\d+\}\}|[ \t]*#)",
        " ",
        res,
    )

    # 2. 标点与占位符同行自然倒置绑定：处理单占位符及连续多个占位符跟随标点的情况
    # 严禁跨越换行符（限定水平空白 [ \t]*），避免误将换行后新条目/新文献首的 [v数字] 卷入上一句末尾标点
    # 特别保护：若标点后紧随的占位符后方紧跟谓语动词或关系词（如 where [v1] is ..., [v2] is ...），该占位符为分句主语/变量，绝对不可倒置
    token_pat = r"(?:\[\s*v\s*\d+\s*\]|(?<!\{)(?:\{\{\s*v\s*\d+\s*\}\}|\{\s*v\s*\d+\s*\})(?!\})|<style\s+id='\d+'>|</style>)"
    def _swap_citation_punctuation(m: re.Match) -> str:
        full = m.string
        end_pos = m.end()
        following = full[end_pos:end_pos + 30].strip()
        # 若后方紧跟系动词、谓语动词、关系连词或数学比较符，说明是下一分句的变量或主语，严禁移位
        if re.match(r"^(?:is|are|was|were|denotes?|represents?|stands?|equals?|means?|refers?|defined|which|where|when|with|if|[=<>+\-*/])\b", following, re.IGNORECASE):
            return m.group(0)
        return " " + re.sub(r"[ \t]*,[ \t]*", " ", m.group(2)) + m.group(1) + " "

    res = re.sub(
        rf"([,\.;:!?]+)[ \t]*({token_pat}(?:[ \t]*,[ \t]*{token_pat}|[ \t]+{token_pat})*)",
        _swap_citation_punctuation,
        res,
    )

    # 3. 词边界空格保护（不破坏换行符边界）
    res = re.sub(rf"(?<![ \t\r\n])({token_pat})", r" \1", res)
    res = re.sub(rf"({token_pat})(?![ \t\r\n])", r"\1 ", res)
    return res


def recover_missing_placeholders(source_text: str, candidate: str, missing_tokens: list) -> str:
    """智能回填缺失占位符到其最接近的语义锚点句，严禁粗暴盲目追加至段落最末尾导致上标乱跑。"""
    if not candidate or not missing_tokens:
        return candidate
    res = candidate
    for token in missing_tokens:
        # 0. 优先检测是否为数学公式、物理变量角标或生物/技术缩写修饰符 (例如 M [v2] -> Ms, R [v1] -> R0, TK [v1] -> TK+, Ki [v2] -> Ki67)
        var_match = re.search(rf"(?<![A-Za-z0-9])([A-Za-z]{{1,5}})\s*{re.escape(token)}", source_text)
        if var_match:
            var_word = var_match.group(1)
            cand_var_pattern = rf"(?<![A-Za-z0-9]){re.escape(var_word)}[A-Za-z0-9]?(?![A-Za-z0-9])(?![ \t]*\[v\d+\])"
            if re.search(cand_var_pattern, res):
                res = re.sub(cand_var_pattern, f"{var_word} {token}", res, count=1)
                continue

        pos = source_text.find(token)
        if pos == -1:
            m = re.search(re.escape(token).replace(r"\[", r"\[\s*").replace(r"\]", r"\s*\]"), source_text)
            pos = m.start() if m else -1

        inserted = False
        if pos != -1 and len(source_text) > 0:
            prefix = source_text[:pos]
            # 0.5 优先在全局译文中精准匹配紧邻该占位符前方的专有名词/锚点词（例如 AUSM [v20] -> AUSM）
            # 杜绝因人名缩写 (如 A. M. Zhang, et al.) 导致句子索引失真，或将公式符号误追加至段落最末尾
            anchor_words = [w for w in re.findall(r"[A-Za-z0-9\-_]{2,}", prefix.strip()) if len(w) >= 3]
            if anchor_words:
                stopwords = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "been"}
                for w in reversed(anchor_words[-4:]):
                    if w.lower() in stopwords:
                        continue
                    w_pat = rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])"
                    if re.search(w_pat, res, re.IGNORECASE):
                        # 在锚点词后插入，且保证不破坏紧随其后的现有占位符
                        res = re.sub(rf"({w_pat})(?![ \t]*\[v\d+\])", rf"\1 {token}", res, count=1, flags=re.IGNORECASE)
                        inserted = True
                        break

            if not inserted:
                src_sent_idx = len(re.findall(r"(?:[\.\?!]\s+|\n+)", prefix))
                sentences = [s for s in re.split(r"(?<=[。！？.!?\n])", res) if s]
                if sentences:
                    target_idx = min(len(sentences) - 1, src_sent_idx)
                    s = sentences[target_idx]

                    # 优先在目标句内前置词汇后插入
                    for w in reversed(anchor_words[-3:] if anchor_words else []):
                        if re.search(rf"\b{re.escape(w)}\b", s, re.IGNORECASE):
                            s = re.sub(rf"(\b{re.escape(w)}\b)", rf"\1 {token}", s, count=1, flags=re.IGNORECASE)
                            inserted = True
                            break
                    if not inserted:
                        # 紧贴该目标句子的末尾标点（句号/分号）前，确保绝不串句或飘到段末
                        if re.search(r"[。！？；.;!?]", s):
                            s = re.sub(r"([。！？；.;!?]+)$", rf" {token}\1", s.rstrip())
                        else:
                            s = s.rstrip() + f" {token}"
                        inserted = True
                    sentences[target_idx] = s
                    res = "".join(sentences)

        if not inserted:
            if re.search(r"[。！？.!?]", res):
                res = re.sub(r"([。！？.!?])(?=[^。！？.!?]*$)", rf" {token}\1", res)
            else:
                res = res + f" {token}"
    return res


_LEAK_HEADER_RE = re.compile(
    r"^【(?:重要注意|背景信息|待翻译文本|译文|注意|翻訳対象テキスト|翻訳対象文本|翻訳済みテキスト|訳文|번역\s*대상\s*텍스트|번역문)[^】\]]*[】\]]"
    r"|^\[(?:Background Information|Source Text|Translated Text|Translation)\]"
)
_INLINE_LEAK_RE = re.compile(
    r"^(?:(?:"
    r"【(?:重要注意|背景信息|待翻译文本|译文|注意|翻訳対象テキスト|翻訳対象文本|翻訳済みテキスト|訳文|번역\s*대상\s*텍스트|번역문)[^】\]]*[】\]]"
    r"|\[(?:Background Information|Source Text|Translated Text|Translation)\]"
    r"|(?:译文|訳文|Translation)\s*[：:]"
    r"|(?:严禁翻译|请将以下文本|将以下文本|请结合背景信息|参考上面的信息).*?(?:(?:只需输出|只需要输出).*?结果[，,。:：\s]*)?(?:不要额外解释|不要解释|(?:只需输出|只需要输出).*?结果|简体中文|繁体中文|日语|英语|韩语|俄语|法语|德语)"
    r"|(?:翻訳やコードタグ|コードタグや変数).*?(?:説明は不要で.*?出力してください|のみを出力してください)"
    r"|(?:Translate the following segment|Refer to the above information|Keep all code tags).*?without additional explanation"
    r")[，,。.:：\s]*)+",
    flags=re.IGNORECASE | re.DOTALL,
)


def clean_translated_candidate(candidate: str) -> str:
    """彻底剥离大模型可能回显或泄漏的提示词、说明性文字、Markdown 标记。

    覆盖中文、官方英文指令以及日语/韩语误译提示词前缀，防止模板文字进入最终 PDF。
    """
    if not candidate:
        return ""
    # 0. 剔除任何残留的连续连字伪影
    candidate = re.sub(r"(?:ﬃ|(?:ffi)){2,}\s*", "", candidate, flags=re.IGNORECASE)

    # 1. 剥离 Markdown 代码块标记 ```markdown ... ```
    candidate = re.sub(r"^```(?:markdown|text|html|xml)?\s*", "", candidate)
    candidate = re.sub(r"\s*```$", "", candidate)

    # 2. 逐行剥离模型可能复读的指令前缀行
    lines = candidate.splitlines()
    clean_lines = []
    skipping_headers = True
    for line in lines:
        stripped = line.strip()
        if skipping_headers:
            if not stripped:
                continue
            is_full_header = (
                bool(_LEAK_HEADER_RE.match(stripped)) and (
                    "遗漏" in stripped or "占位符" in stripped or not _LEAK_HEADER_RE.sub("", stripped).strip()
                )
            ) or (
                stripped in ("译文：", "译文:", "訳文：", "訳文:", "Translation:", "Translation：")
            ) or (
                stripped.startswith("特别注意")
            ) or (
                ("严禁翻译" in stripped or "上一次翻译遗漏" in stripped or "占位符遗漏警告" in stripped)
                and len(stripped) < 200
                and not any(p in stripped for p in ["其中，", "其中 "])
            ) or (
                stripped.endswith(("不要额外解释：", "不要额外解释:", "不要额外解释", "のみを出力してください。", "のみを出力してください", "without additional explanation.", "without additional explanation:"))
                and len(stripped) < 260
                and not any(p in stripped for p in ["其中，", "其中 "])
            )
            if is_full_header:
                continue

            inline_cleaned = _INLINE_LEAK_RE.sub("", stripped).strip()

            if not inline_cleaned:
                continue

            skipping_headers = False
            clean_lines.append(inline_cleaned)
        else:
            clean_lines.append(line)

    res = "\n".join(clean_lines).strip()
    res = _INLINE_LEAK_RE.sub("", res).strip()
    return res


def remove_spurious_newlines(text: str) -> str:
    """清理非预期的换行符：若原文无换行，译文中的硬换行必须彻底平滑合并。

    - 中日韩汉字、日文假名与全角标点之间的换行符消除，不留多余空格；
    - 其它（西文、西里尔文、韩文空格词语、公式占位符等）之间的换行符折叠为单个空格。
    """
    if not text or ("\n" not in text and "\r" not in text):
        return text
    # 1. CJK 汉字/日文假名/全角标点之间的换行彻底去除（紧密相连）
    cjk_pat = r"[\u3040-\u30ff\u3400-\u9fff\u3000-\u303f\uff01-\uffee]"
    text = re.sub(rf"({cjk_pat})\s*[\r\n]+\s*({cjk_pat})", r"\1\2", text)
    # 2. 其它任何残留换行折叠为单个空格
    text = re.sub(r"\s*[\r\n]+\s*", " ", text)
    return text.strip()


def align_reference_newlines(source_text: str, candidate_text: str) -> str:
    """对齐多行条目与换行。

    如果原文没有换行，译文中的换行强制清理平滑合并；
    如果原文是参考文献列表或多行条目，确保译文中对应的条目标记强制独占一行，绝不挤在同一行。
    """
    if not candidate_text or not source_text:
        return candidate_text

    # 0. 换行一致性检查：若之前的原文没有换行，译文中的所有硬换行均属模型幻觉，必须彻底清理
    padded_src = pad_structural_tokens(source_text)
    if "\n" not in source_text.strip() and "\n" not in padded_src.strip():
        return remove_spurious_newlines(candidate_text)

    # 1. 优先检测连续递增参考文献块 [v1]...[v2]...[v3]...
    src_lines = [l.strip() for l in padded_src.splitlines() if l.strip()]
    src_line_tokens = []
    for l in src_lines:
        m = re.match(r"^(\[v\d+\])", l)
        if m:
            src_line_tokens.append(m.group(1))

    if len(src_line_tokens) >= 3 and len(src_line_tokens) == len(src_lines):
        # 将 [v2], [v3] 等前置插入换行符，并剔除其后误附带的中文句号（如 [v2]。 -> \n[v2] ）
        candidate_text = re.sub(r"([。\.\)）”’\d])\s*(\[v\d+\])\s*[。、，,\.]?\s*", r"\1\n\2 ", candidate_text)
        cand_lines = [l.strip() for l in candidate_text.splitlines() if l.strip()]

        # 若译文条目数与原文条目数完全匹配，对齐行首占位符（避免条目内部公式符号缺失导致后续序号整体偏移）
        if len(cand_lines) == len(src_line_tokens):
            new_cand_lines = []
            for i, l in enumerate(cand_lines):
                expected_tok = src_line_tokens[i]
                repaired_line = re.sub(r"^\[v\d+\]\s*", f"{expected_tok} ", l)
                new_cand_lines.append(repaired_line)
            return "\n".join(new_cand_lines).strip()
        return candidate_text.strip()

    # 2. 通用多行条目对齐
    newline_tokens = re.findall(r"(?:^|\n)\s*(\[v\d+\]|\{\{v\d+\}\})", source_text)
    if len(newline_tokens) <= 1:
        return candidate_text
    res = candidate_text
    for token in newline_tokens:
        pat = rf"(?<!\n)\s*{re.escape(token)}\s*[。、，,\.]?\s*"
        res = re.sub(pat, rf"\n{token} ", res)
    return res.strip()


def sanitize_placeholders(source_text: str, candidate_text: str) -> tuple[str, list[str]]:
    """自动标准化占位符形式，并安全剥离大模型幻觉生成的不在原文中的多余占位符。

    1. 自动紧凑化占位符内部空格: [ v 1 ] -> [v1]，{ v 1 } -> {v1}，{{ v 1 }} -> {{v1}}；
    2. 剔除原文中完全不存在的幻觉公式占位符；
    3. 剔除超出原文出现频次的重复公式占位符。
    """
    if not candidate_text:
        return candidate_text, []
    from collections import Counter

    src_tokens = extract_placeholders(source_text)
    src_counter = Counter(src_tokens)

    # 0. 规范化畸变括号、非 ASCII 数字及西里尔/希腊近似字母变体（例如 [в2]、[В4]、[B4]、[ν1]、[v3>、[vn4]、［ｖ１］、【v2】、[R/v3] 等）
    def _repair_bracket_token(m: re.Match) -> str:
        num = _normalize_placeholder_digits(m.group(1))
        target_token = f"[v{num}]"
        target_token_brace = f"{{{{v{num}}}}}"
        if target_token in src_tokens:
            return target_token
        elif target_token_brace in src_tokens:
            return target_token_brace
        return m.group(0)

    res = re.sub(
        r"(?<!\{)[\[\{【（\(「『［][A-Za-z0-9_/\-\.,\s]*[vｖвbν][A-Za-z\s]{0,2}([0-9０-９٠-٩۰-۹]+)[A-Za-z0-9_/\-\.,\s]*[\]\}】）\)」』］>》](?!\})",
        _repair_bracket_token,
        candidate_text,
        flags=re.IGNORECASE,
    )
    res = re.sub(r"\[\s*\[\s*(v\d+)\s*\]\s*\]", r"[\1]", res, flags=re.IGNORECASE)

    # 1. 紧凑化内部空格
    res = re.sub(r"\[\s*v\s*(\d+)\s*\]", r"[v\1]", res, flags=re.IGNORECASE)
    res = re.sub(r"(?<!\{)\{\s*(v\d+)\s*\}(?!\})", r"{\1}", res, flags=re.IGNORECASE)
    res = re.sub(r"\{\{\s*(v\d+)\s*\}\}", r"{{\1}}", res, flags=re.IGNORECASE)

    # 2. 剥离幻觉占位符与超出原文频次的多余重复占位符
    seen_counts = Counter()
    stripped = []
    def _filter_token(m: re.Match) -> str:
        token = normalize_token(m.group(0))
        if token.startswith("[v") or token.startswith("{{v") or token.startswith("{v"):
            max_allowed = src_counter.get(token, 0)
            if seen_counts[token] < max_allowed:
                seen_counts[token] += 1
                return token
            else:
                stripped.append(token)
                return ""
        return m.group(0)

    res = PLACEHOLDER_PATTERN.sub(_filter_token, res)
    return res, stripped


class HunyuanLocalTranslator(BaseTranslator):
    name = "hymt2_local"
    # Keep language codes intact: BaseTranslator applies lang_map during init.
    lang_map = {}
    language_names = {
        "zh": "简体中文",
        "zh-cn": "简体中文",
        "en": "英语",
        "zh-hant": "繁体中文",
        "zh-tw": "繁体中文",
        "ja": "日语",
        "ko": "韩语",
        "fr": "法语",
        "de": "德语",
        "es": "西班牙语",
        "ru": "俄语",
        "pt": "葡萄牙语",
        "it": "意大利语",
        "ar": "阿拉伯语",
        "tr": "土耳其语",
        "th": "泰语",
        "vi": "越南语",
        "ms": "马来语",
        "id": "印尼语",
        "tl": "菲律宾语",
        "hi": "印地语",
        "pl": "波兰语",
        "cs": "捷克语",
        "nl": "荷兰语",
        "km": "高棉语",
        "my": "缅甸语",
        "fa": "波斯语",
        "gu": "古吉拉特语",
        "ur": "乌尔都语",
        "te": "泰卢固语",
        "mr": "马拉地语",
        "he": "希伯来语",
        "bn": "孟加拉语",
        "ta": "泰米尔语",
        "uk": "乌克兰语",
        "bo": "藏语",
        "kk": "哈萨克语",
        "mn": "蒙古语",
        "ug": "维吾尔语",
        "yue": "粤语",
    }
    language_names_en = {
        "zh": "Simplified Chinese",
        "zh-cn": "Simplified Chinese",
        "en": "English",
        "zh-hant": "Traditional Chinese",
        "zh-tw": "Traditional Chinese",
        "ja": "Japanese",
        "ko": "Korean",
        "fr": "French",
        "de": "German",
        "es": "Spanish",
        "ru": "Russian",
        "pt": "Portuguese",
        "it": "Italian",
        "ar": "Arabic",
        "tr": "Turkish",
        "th": "Thai",
        "vi": "Vietnamese",
        "ms": "Malay",
        "id": "Indonesian",
        "tl": "Filipino",
        "hi": "Hindi",
        "pl": "Polish",
        "cs": "Czech",
        "nl": "Dutch",
        "km": "Khmer",
        "my": "Burmese",
        "fa": "Persian",
        "gu": "Gujarati",
        "ur": "Urdu",
        "te": "Telugu",
        "mr": "Marathi",
        "he": "Hebrew",
        "bn": "Bengali",
        "ta": "Tamil",
        "uk": "Ukrainian",
        "bo": "Tibetan",
        "kk": "Kazakh",
        "mn": "Mongolian",
        "ug": "Uyghur",
        "yue": "Cantonese",
    }
    SCRIPT_PATTERNS = {
        "zh": r"[\u3400-\u9fff]",
        "zh-cn": r"[\u3400-\u9fff]",
        "zh-hant": r"[\u3400-\u9fff]",
        "zh-tw": r"[\u3400-\u9fff]",
        "yue": r"[\u3400-\u9fff]",
        "ja": r"[\u3040-\u30ff\u3400-\u9fff]",
        "ko": r"[\uac00-\ud7af]",
        "ru": r"[\u0400-\u04ff]",
        "uk": r"[\u0400-\u04ff]",
        "mn": r"[\u1800-\u18af\u0400-\u04ff]",
        "kk": r"[\u0600-\u06ff\u0400-\u04ff]",
        "ar": r"[\u0600-\u06ff]",
        "fa": r"[\u0600-\u06ff]",
        "ur": r"[\u0600-\u06ff]",
        "ug": r"[\u0600-\u06ff]",
        "he": r"[\u0590-\u05ff]",
        "hi": r"[\u0900-\u097f]",
        "mr": r"[\u0900-\u097f]",
        "bn": r"[\u0980-\u09ff]",
        "gu": r"[\u0a80-\u0aff]",
        "te": r"[\u0c00-\u0c7f]",
        "ta": r"[\u0b80-\u0bff]",
        "th": r"[\u0e00-\u0e7f]",
        "km": r"[\u1780-\u17ff]",
        "my": r"[\u1000-\u109f]",
        "bo": r"[\u0f00-\u0fff]",
    }

    def __init__(
        self,
        runtime: LlamaServerRuntime,
        lang_in: str = "en",
        lang_out: str = "zh",
        ignore_cache: bool = True,
        max_retries: int = 2,
        on_block_translated: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(lang_in, lang_out, ignore_cache)
        if self.lang_out.lower() not in self.language_names:
            raise ValueError(f"不支持的目标语言: {lang_out}")
        self.runtime = runtime
        self.max_retries = max_retries
        self.on_block_translated = on_block_translated
        self.background_text: Optional[str] = None
        self.title_background_text: Optional[str] = None
        self.quality_issues = []
        self._issue_lock = threading.Lock()
        self._translation_cache = {}
        self._cache_lock = threading.Lock()
        self.stats = {
            "total_calls": 0,
            "success_calls": 0,
            "retry_calls": 0,
            "fallback_calls": 0,
            "cache_hits": 0,
            "standalone_calls": 0,
            "structural_protected_calls": 0,
            "background_assisted_calls": 0,
            "default_calls": 0,
        }

    def _store_cache(self, source: str, candidate: str) -> None:
        """记录有效译文到文献级内存缓存，供全篇相同页眉/页脚/重复短语零延时复用。"""
        if (
            source
            and candidate
            and candidate.strip()
            and candidate.strip().casefold() != source.strip().casefold()
        ):
            with self._cache_lock:
                self._translation_cache[source.strip()] = candidate

    def set_background(self, text: str) -> None:
        """设置整篇文献的全局背景，同时提取轻量标题背景供短标题消歧使用。"""
        if text and text.strip():
            self.background_text = text.strip()
            title_lines = [line.strip() for line in self.background_text.splitlines() if line.strip().startswith("Title:")]
            self.title_background_text = title_lines[0] if title_lines else None
            logger.info("已载入文献自适应学术背景 (%d 字/词)", len(self.background_text.split()))

    @staticmethod
    def _is_short_heading(text: str) -> bool:
        """短标题/表头/幻灯片词条检测：保留领域标题消歧，避免带入长摘要诱发内容泄漏。"""
        prose = re.sub(r"\s+", " ", PLACEHOLDER_PATTERN.sub("", text or "")).strip()
        if not prose:
            return False
        if re.search(r"[.!?。！？]$", prose):
            return False
        words = prose.split()
        return len(words) <= 6 and len(prose) <= 60

    def _notify_block(
        self,
        source: str,
        target: str,
        status: str = "success",
        reason: str = "",
        attempt: int = 1,
    ) -> None:
        if self.on_block_translated:
            try:
                self.on_block_translated({
                    "type": "block_translated",
                    "index": self.stats["total_calls"],
                    "source": source,
                    "target": target,
                    "status": status,
                    "reason": reason,
                    "attempt": attempt,
                })
            except Exception:
                pass

    def get_formular_placeholder(self, placeholder_id: int | str):
        """采用学术文献天然兼容的高保留率引用占位符：[v数字]。

        实机基准测试表明：大模型天然将方括号 [v1] 识别为学术文献引文或编号标记，
        保留率高达 100%，彻底消除了双花括号 {{v1}} 造成的模板语法抑制与占位符吞噬问题。
        """
        return f"[v{placeholder_id}]", f"\\[\\s*v\\s*{placeholder_id}\\s*\\]"

    def get_rich_text_left_placeholder(self, placeholder_id: int | str):
        """契合腾讯官方代码标签对齐规范：<style id='...'>。"""
        return (
            f"<style id='{placeholder_id}'>",
            f"<\\s*style\\s*id\\s*=\\s*'\\s*{placeholder_id}\\s*'\\s*>",
        )

    def get_rich_text_right_placeholder(self, placeholder_id: int | str):
        return "</style>", r"<\s*\/\s*style\s*>"

    def do_llm_translate(self, text: str, rate_limit_params: dict = None) -> str:
        # 抛出 NotImplementedError，告知 BabelDOC 走原生专用机器翻译管道 (ILTranslator)
        raise NotImplementedError

    def do_translate(self, text: str, rate_limit_params: dict = None) -> str:
        if not text or not text.strip():
            return text

        self.stats["total_calls"] += 1
        effective_lang_in = (
            detect_text_language(text)
            if (self.lang_in or "").strip().lower() in ("", "auto")
            else self.lang_in
        )
        use_english_prompt = not should_use_chinese_prompt(effective_lang_in, self.lang_out)
        target_lang_desc = (
            self.language_names_en[self.lang_out.lower()]
            if use_english_prompt
            else self.language_names[self.lang_out.lower()]
        )

        # 0. 纯公式/纯占位符/独立 URL/DOI 链接直接原样返回，避免大模型幻觉与不必要的计算开销
        if PlaceholderValidator.is_standalone_placeholder(text) or is_standalone_url_or_code(text):
            self.stats["standalone_calls"] += 1
            return text

        # 0.5 精确匹配段落级内存复用缓存（针对各页重复出现的页眉、页脚、期刊栏头、版权声明等）
        cache_key = text.strip()
        with self._cache_lock:
            cached_candidate = self._translation_cache.get(cache_key)

        if cached_candidate is not None:
            self.stats["cache_hits"] += 1
            self.stats["success_calls"] += 1
            logger.info("段落命中精确复用缓存 (累计命中 %d 次): %r", self.stats["cache_hits"], cache_key[:40])
            self._notify_block(text, cached_candidate, "success", reason="命中完全重复内容缓存，直接复用")
            return cached_candidate

        # 检查是否含有公式占位符 [v数字]、{{v数字}} 或代码标签 <style>，并保护混排段内 URL/DOI
        has_structural_tokens = bool(PLACEHOLDER_PATTERN.search(text) or "<style" in text)
        padded_text = pad_structural_tokens(text)
        input_text, url_mapping = protect_inline_urls(padded_text)
        if url_mapping:
            has_structural_tokens = True

        # 1. 分级背景注入：
        #    - 参考文献列表绝不注入论文摘要背景（防止长文献块注意力漂移重译摘要）；
        #    - 短标题仅注入轻量 Title 消歧背景；长正文段落注入完整背景
        if self._is_reference_block(text):
            active_bg = None
        elif self._is_short_heading(text):
            active_bg = self.title_background_text
        else:
            active_bg = self.background_text
        if has_structural_tokens:
            self.stats["structural_protected_calls"] += 1
            if active_bg:
                self.stats["background_assisted_calls"] += 1
                prompt = prompt_structural_with_background(
                    source_text=input_text,
                    background_text=active_bg,
                    target_lang=target_lang_desc,
                    use_english=use_english_prompt,
                )
            else:
                prompt = prompt_structural_protection(
                    source_text=input_text,
                    target_lang=target_lang_desc,
                    use_english=use_english_prompt,
                )
        elif active_bg:
            # 纯自然语言段落使用背景辅助翻译（提供领域学术上下文）
            self.stats["background_assisted_calls"] += 1
            prompt = prompt_with_background(
                source_text=input_text,
                background_text=active_bg,
                target_lang=target_lang_desc,
                use_english=use_english_prompt,
            )
        else:
            self.stats["default_calls"] += 1
            prompt = prompt_default(
                source_text=input_text,
                target_lang=target_lang_desc,
                use_english=use_english_prompt,
            )

        raw_output = ""
        stripped_tokens = []
        try:
            res = self.runtime.chat_completion(
                prompt=prompt,
                cache_prompt=True,
                **DEFAULT_INFERENCE_PARAMS,
            )
            raw_output = res["choices"][0]["message"]["content"]
            candidate = raw_output.strip()
            finish_reason = res["choices"][0].get("finish_reason")
            candidate = clean_translated_candidate(candidate)
            if "**" not in text:
                candidate = candidate.replace("**", "")
            candidate = restore_inline_urls(candidate, url_mapping, source_text=text)
            candidate = align_reference_newlines(text, candidate)
            candidate, stripped_tokens = sanitize_placeholders(text, candidate)
            if stripped_tokens:
                with self._issue_lock:
                    self.quality_issues.append({"source": text, "candidate": candidate, "reason": f"剥离幻觉占位符: {stripped_tokens}"})
        except TimeoutError as te:
            logger.warning("首轮推理单段超时 (超过 600 秒): %s，尝试纠错降级", te)
            candidate = ""
            finish_reason = "timeout"
        except Exception as e:
            logger.warning("大模型段落推理异常: %s", e)
            candidate = ""
            finish_reason = "error"

        # 2. 校验占位符完整性、截断、异常膨胀、年份保真、跨脚本乱码与漏译
        val = PlaceholderValidator.validate(text, candidate, finish_reason=finish_reason)
        expanded = self._suspicious_expansion(text, candidate)
        year_mutated = self._has_year_hallucination(text, candidate)
        script_glitch = self._has_foreign_script_glitch(text, candidate, self.lang_out)
        suspected = self._suspect_translation(text, candidate, effective_lang_in=effective_lang_in)
        is_ok = val.is_valid and not expanded and not year_mutated and not script_glitch and not suspected

        if is_ok:
            self.stats["success_calls"] += 1
            self._notify_block(text, candidate, "success", reason="校验通过")
            self._store_cache(text, candidate)
            return candidate

        # 3. 校验失败时重试：彻底剥离背景信息，使用聚焦纯净提示词指明缺失占位符低温纠错重试
        if not val.is_valid:
            fail_reason = val.reason
        elif expanded:
            fail_reason = "译文异常膨胀，疑似背景泄漏"
        elif year_mutated:
            fail_reason = "四位年份数字被模型篡改"
        elif script_glitch:
            fail_reason = "译文混入非目标语系乱码字符"
        else:
            fail_reason = "正文疑似漏译或目标语言不符"
        # 实时推送失败状态至前端监视流
        self._notify_block(
            text,
            candidate if candidate.strip() else "[首轮推理输出空缺]",
            status="retry_pending",
            reason=f"首轮未通过: {fail_reason}，正在启动无背景精准重试",
            attempt=1,
        )
        for attempt in range(1, self.max_retries + 1):
            self.stats["retry_calls"] += 1
            logger.warning(
                "占位符或输出校验失败 (%s)，执行精准结构锁定重试 (第 %d/%d 次)",
                fail_reason, attempt, self.max_retries,
            )
            # 重试时严格使用无背景的纯净结构提示词，避免背景上下文分心
            retry_prompt = prompt_structural_retry(
                input_text, val.missing_tokens, target_lang=target_lang_desc,
                use_english=use_english_prompt,
            )

            retry_raw = ""
            stripped_retry = []
            retry_finish = "unknown"
            try:
                retry_res = self.runtime.chat_completion(
                    prompt=retry_prompt,
                    temperature=0.1,
                    top_p=0.3,
                    top_k=10,
                    repetition_penalty=1.1,
                    dry_multiplier=DEFAULT_INFERENCE_PARAMS["dry_multiplier"],
                    dry_allowed_length=DEFAULT_INFERENCE_PARAMS["dry_allowed_length"],
                    dry_penalty_last_n=DEFAULT_INFERENCE_PARAMS["dry_penalty_last_n"],
                    cache_prompt=True,
                )
                retry_raw = retry_res["choices"][0]["message"]["content"]
                candidate = retry_raw.strip()
                candidate = clean_translated_candidate(candidate)
                if "**" not in text:
                    candidate = candidate.replace("**", "")
                candidate = restore_inline_urls(candidate, url_mapping, source_text=text)
                candidate = align_reference_newlines(text, candidate)
                candidate, stripped_retry = sanitize_placeholders(text, candidate)
                if stripped_retry:
                    with self._issue_lock:
                        self.quality_issues.append({"source": text, "candidate": candidate, "reason": f"剥离幻觉占位符: {stripped_retry}"})
                retry_finish = retry_res["choices"][0].get("finish_reason")

                val = PlaceholderValidator.validate(text, candidate, finish_reason=retry_finish)
                retry_is_ok = (
                    val.is_valid
                    and not self._suspicious_expansion(text, candidate)
                    and not self._has_year_hallucination(text, candidate)
                    and not self._has_foreign_script_glitch(text, candidate, self.lang_out)
                    and not self._suspect_translation(text, candidate, effective_lang_in=effective_lang_in)
                )

                if retry_is_ok:
                    self.stats["success_calls"] += 1
                    self._notify_block(text, candidate, "retry_success", reason=f"第 {attempt} 次重试校验通过", attempt=attempt)
                    self._store_cache(text, candidate)
                    return candidate
                else:
                    if attempt < self.max_retries:
                        self._notify_block(
                            text,
                            candidate if candidate.strip() else "[重试输出空缺]",
                            status="retry_pending",
                            reason=f"第 {attempt} 次重试仍未通过: {val.reason}",
                            attempt=attempt + 1,
                        )
            except TimeoutError as te:
                logger.error("重试单段仍然超时: %s，直接终止本段重试", te)
                break
            except Exception as e:
                logger.error("重试发生异常: %s", e)
                break

        # 4. 严谨保全与自动补齐兜底：杜绝任何异常抛出导致 BabelDOC 崩溃或整段漏译
        self.stats["fallback_calls"] += 1
        reason = val.reason if not val.is_valid else "正文疑似漏译或目标语言不符"
        with self._issue_lock:
            self.quality_issues.append({"source": text, "candidate": candidate, "reason": reason})

        # 检查候选译文是否包含有效译文内容（支持多语种脚本校验）
        lang_out_lower = self.lang_out.lower()
        script_pat = self.SCRIPT_PATTERNS.get(lang_out_lower)
        if script_pat:
            has_valid_content = bool(re.search(script_pat, PLACEHOLDER_PATTERN.sub("", candidate)))
        else:
            has_valid_content = bool(candidate.strip() and candidate.strip().casefold() != text.strip().casefold())

        if has_valid_content:
            # 若存在明确单一年份被篡改（如页眉 (2026) -> (2024)），自动还原原文年份；并剥离非目标语系乱码
            candidate = self._repair_year_if_unambiguous(text, candidate)
            # 如果存在缺失的占位符，采用语义锚点智能回填，确保文献上标定位准确
            if val.missing_tokens:
                logger.warning(
                    "重试后仍有占位符缺失 (%s)，采用智能锚点回填保全排版与上标定位: %r",
                    val.missing_tokens, text[:60],
                )
                candidate = recover_missing_placeholders(text, candidate, val.missing_tokens)
            else:
                logger.warning("占位符或格式存在微小瑕疵 (%s)，充分信任大模型并保全译文: %r", val.reason, text[:60])
            candidate = align_reference_newlines(text, candidate)
            self._notify_block(text, candidate, "fallback_repaired", reason=f"重试达上限({self.max_retries}次)，智能锚点保全: {val.reason}")
            if not self._has_year_hallucination(text, candidate):
                self._store_cache(text, candidate)
            return candidate

        # 若候选译文无有效目标语言内容（模型未翻译或返回空白），安全回退原文，绝不抛出致命异常
        logger.warning("大模型未输出有效目标语言译文 (%s)，保全原文段落以确保排版交付: %r", reason, text[:60])
        self._notify_block(text, text, "fallback_raw", reason="译文无效，已安全回退原文")
        return text

    @staticmethod
    def _is_reference_block(text: str) -> bool:
        """检测参考文献/书目列表块，避免注入摘要背景诱发大模型跨页重译摘要。"""
        if not text or len(text) < 120:
            return False
        markers = re.findall(r"(?:\[v\d+\]|\[\d+\])", text)
        if len(markers) < 2:
            return False
        has_year = bool(re.search(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", text))
        has_pub_meta = bool(
            re.search(
                r"(?:press|report|journal|vol\b|no\b|pp\b|university|publisher|proceedings|trans\b|struct\b|phys\b|chem\b|eng\b|doi\b|et al\b)",
                text,
                re.IGNORECASE,
            )
        )
        return has_year and has_pub_meta

    @staticmethod
    def _has_year_hallucination(source: str, candidate: str) -> bool:
        """检测四位年份（19xx/20xx）是否被大模型篡改（如页眉 2026 -> 2024）。"""
        if not source or not candidate:
            return False
        src_years = set(re.findall(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", source))
        if not src_years:
            return False
        cand_years = set(re.findall(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", candidate))
        return bool(cand_years - src_years)

    @staticmethod
    def _repair_year_if_unambiguous(source: str, candidate: str) -> str:
        """若原文与译文各自仅含单个不同四位年份，自动还原为原文年份。"""
        if not source or not candidate:
            return candidate
        src_years = list(dict.fromkeys(re.findall(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", source)))
        cand_years = list(dict.fromkeys(re.findall(r"(?<!\d)(?:19\d\d|20\d\d)(?!\d)", candidate)))
        if len(src_years) == 1 and len(cand_years) == 1 and src_years[0] != cand_years[0]:
            return re.sub(rf"(?<!\d){re.escape(cand_years[0])}(?!\d)", src_years[0], candidate)
        return candidate

    @staticmethod
    def _has_foreign_script_glitch(source: str, candidate: str, lang_out: str) -> bool:
        """检测非目标语系文字异常混入（如俄/英/中译文中出现阿拉伯文 token glitch `ىلع`）。"""
        if not candidate:
            return False
        lang = (lang_out or "").strip().lower()
        prose = PLACEHOLDER_PATTERN.sub("", candidate)
        src_prose = PLACEHOLDER_PATTERN.sub("", source or "")
        checks = [
            (r"[\u0600-\u06ff]", {"ar", "fa", "ur", "ug", "kk"}),
            (r"[\u0590-\u05ff]", {"he"}),
            (r"[\u0900-\u097f]", {"hi", "mr"}),
            (r"[\u0e00-\u0e7f]", {"th"}),
            (r"[\uac00-\ud7af]", {"ko"}),
            (r"[\u3040-\u30ff]", {"ja"}),
        ]
        for pat, allowed_langs in checks:
            if lang not in allowed_langs and re.search(pat, prose) and not re.search(pat, src_prose):
                return True
        return False

    @staticmethod
    def _suspicious_expansion(source: str, candidate: str) -> bool:
        """拦截短标题膨胀及长段落中突发注入摘要/背景的异常扩写。"""
        src_prose = re.sub(r"\s+", " ", PLACEHOLDER_PATTERN.sub("", source or "")).strip()
        cand_prose = re.sub(r"\s+", " ", PLACEHOLDER_PATTERN.sub("", candidate or "")).strip()
        if not src_prose or not cand_prose:
            return False
        if len(src_prose.split()) <= 4 and len(src_prose) <= 40:
            return len(cand_prose) > max(60, len(src_prose) * 6)
        if len(src_prose) > 80 and len(cand_prose) > max(400, len(src_prose) * 2.8):
            return True
        return False

    def _suspect_translation(self, source: str, candidate: str, effective_lang_in: Optional[str] = None) -> bool:
        # Short names, abbreviations, numbers and URLs may legitimately remain unchanged.
        if is_standalone_url_or_code(source):
            return False
        explicit_lang_in = (self.lang_in or "").strip().lower()
        is_auto = explicit_lang_in in ("", "auto")
        src_lang = (effective_lang_in or explicit_lang_in).lower()
        lang_out = self.lang_out.lower()
        prose = PLACEHOLDER_PATTERN.sub("", source).strip()
        if src_lang == lang_out:
            # 仅当用户显式指定同语种，或 auto 模式下确实不是其他拉丁语系时才跳过漏译校验
            if not is_auto or lang_out != "en" or not _looks_like_non_english_latin(prose):
                return False
        if src_lang in ("zh", "zh-cn", "zh-hant", "zh-tw", "yue", "ja", "ko", "th", "km", "my", "bo"):
            if len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0e00-\u0e7f\u1780-\u17ff\u1000-\u109f\u0f00-\u0fff]", prose)) < 6:
                return False
        elif len(re.findall(r"(?:[^\W\d_]|[\u0900-\u0d7f]){2,}", prose)) < 4:
            return False
        if source.strip().casefold() == candidate.strip().casefold():
            return True
        script = self.SCRIPT_PATTERNS.get(lang_out)
        return bool(script and not re.search(script, PLACEHOLDER_PATTERN.sub('', candidate)))


class TranslationQualityError(RuntimeError):
    """No structurally valid translation was obtained; document needs review."""
