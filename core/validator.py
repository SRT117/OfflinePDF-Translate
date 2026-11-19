# SPDX-License-Identifier: AGPL-3.0-only
"""占位符完整性、语法闭合与译文质量底线校验器。

严格支持官方规范的 {{v数字}}、{v数字} 占位符与 <style id='...'> 代码标签校验。
尊重自然语言语序与大模型语义生成能力，解除机械的占位符相对顺序限制；
仅对截断、空输出、代码样式未闭合等不可逆严重错误进行拦截与纠错。
"""

from collections import Counter
import re
from typing import List, NamedTuple, Optional

# 匹配 [v1]、{{v1}}、{v1}、变体括号及代码标签
PLACEHOLDER_PATTERN = re.compile(
    r"[\[\{【（]\s*v\s*\d+\s*[\]\}】）]|(?<!\{)\{\{\s*v\s*\d+\s*\}\}(?!\})|<style\s+id='\d+'>|</style>",
    re.IGNORECASE,
)
STYLE_OPEN_PATTERN = re.compile(r"<style\s+id='(\d+)'>", re.IGNORECASE)
STYLE_CLOSE_PATTERN = re.compile(r"<\/style>", re.IGNORECASE)
# 检测未被还原的 URL 占位符 [u1] 或被大模型错写为西里尔/近似字母/畸变括号的残留占位符 ([в2]、[B4]、[у2]、[v3>、[vn4])
LEAKED_VARIANT_TOKEN_PATTERN = re.compile(
    r"[\[\{【（]\s*(?:u|у|в|b|v[a-z]{1,2})\s*\d+\s*[\]\}】）>]"
    r"|[\[\{【（]\s*v\s*\d+\s*[>》]",
    re.IGNORECASE,
)


def normalize_token(raw_match: str) -> str:
    """标准化占位符形式为紧凑小写格式。"""
    cleaned = re.sub(r"\s+", "", raw_match).lower()
    return cleaned


def extract_placeholders(text: str) -> List[str]:
    """提取文本中所有的占位符，返回标准化列表（按文本中出现的顺序）。"""
    if not text:
        return []
    return [normalize_token(m.group(0)) for m in PLACEHOLDER_PATTERN.finditer(text)]


class ValidationResult(NamedTuple):
    is_valid: bool
    reason: str
    source_tokens: List[str]
    target_tokens: List[str]
    missing_tokens: List[str]
    extra_tokens: List[str]


class PlaceholderValidator:
    @staticmethod
    def is_standalone_placeholder(text: str) -> bool:
        """检查文本是否仅由占位符与标点/空白构成（如单个公式块），无需大模型翻译。"""
        if not text:
            return False
        stripped = PLACEHOLDER_PATTERN.sub("", text)
        return len(re.sub(r"[\s\.,;:!?\(\)\[\]\{\}\'\"^_\-=+*/\\|~`<>]+", "", stripped)) == 0

    @staticmethod
    def validate(
        source_text: str,
        target_text: str,
        finish_reason: Optional[str] = "stop",
    ) -> ValidationResult:
        # 1. 截断校验：检查推理是否正常结束
        if finish_reason != "stop":
            return ValidationResult(
                is_valid=False,
                reason=f"LLM 输出未正常结束，可能被截断 (finish_reason={finish_reason})",
                source_tokens=[],
                target_tokens=[],
                missing_tokens=[],
                extra_tokens=[],
            )

        # 2. 空白校验：原文非空时，译文严禁为空
        if source_text and source_text.strip() and (not target_text or not target_text.strip()):
            return ValidationResult(
                is_valid=False,
                reason="译文输出为空",
                source_tokens=[],
                target_tokens=[],
                missing_tokens=[],
                extra_tokens=[],
            )

        src_tokens = extract_placeholders(source_text)
        tgt_tokens = extract_placeholders(target_text)

        src_counter = Counter(src_tokens)
        tgt_counter = Counter(tgt_tokens)

        missing = []
        for token, count in src_counter.items():
            diff = count - tgt_counter.get(token, 0)
            if diff > 0:
                missing.extend([token] * diff)

        extra = []
        for token, count in tgt_counter.items():
            diff = count - src_counter.get(token, 0)
            if diff > 0:
                extra.extend([token] * diff)

        # 3. 占位符数量与完整性校验
        if missing or extra:
            reasons = []
            if missing:
                reasons.append(f"缺失占位符: {missing}")
            if extra:
                reasons.append(f"多余/幻觉占位符: {extra}")
            return ValidationResult(
                is_valid=False,
                reason="; ".join(reasons),
                source_tokens=src_tokens,
                target_tokens=tgt_tokens,
                missing_tokens=missing,
                extra_tokens=extra,
            )

        # 4. 代码标签闭合校验 (<style id='...'> ... </style>)
        tgt_open_styles = len(STYLE_OPEN_PATTERN.findall(target_text))
        tgt_close_styles = len(STYLE_CLOSE_PATTERN.findall(target_text))
        if tgt_open_styles != tgt_close_styles:
            return ValidationResult(
                is_valid=False,
                reason=f"代码样式标签未配对闭合: 开标签 {tgt_open_styles} 个 vs 闭标签 {tgt_close_styles} 个",
                source_tokens=src_tokens,
                target_tokens=tgt_tokens,
                missing_tokens=[],
                extra_tokens=[],
            )

        depth = 0
        for token in tgt_tokens:
            if token.startswith("<style"):
                depth += 1
            elif token == "</style>":
                depth -= 1
                if depth < 0:
                    break
        if depth != 0:
            return ValidationResult(False, "代码标签嵌套顺序无效", src_tokens, tgt_tokens, [], [])

        # 5. 残留变体/URL 占位符校验（例如未还原的 [u2] 或俄语错写 [в2]/[B4]）
        src_variant_counts = Counter(
            normalize_token(m.group(0)) for m in LEAKED_VARIANT_TOKEN_PATTERN.finditer(source_text or "")
        )
        tgt_variant_counts = Counter(
            normalize_token(m.group(0)) for m in LEAKED_VARIANT_TOKEN_PATTERN.finditer(target_text or "")
        )
        leaked_variants = [
            tok
            for tok, cnt in tgt_variant_counts.items()
            if cnt > src_variant_counts.get(tok, 0)
        ]
        if leaked_variants:
            return ValidationResult(
                is_valid=False,
                reason=f"存在未还原或畸变的占位符残留: {leaked_variants}",
                source_tokens=src_tokens,
                target_tokens=tgt_tokens,
                missing_tokens=[],
                extra_tokens=leaked_variants,
            )

        return ValidationResult(
            is_valid=True,
            reason="校验通过",
            source_tokens=src_tokens,
            target_tokens=tgt_tokens,
            missing_tokens=[],
            extra_tokens=[],
        )
