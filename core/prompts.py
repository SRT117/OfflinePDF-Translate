# SPDX-License-Identifier: AGPL-3.0-only
"""腾讯 Hy-MT2-1.8B 官方规范提示词模板库。

严格遵循腾讯官方 Hy-MT / Hy-MT2 README 与 IFMTBench 双轨指令规范：
- 含中文语向 (ZH ⇄ XX) 及中国方言/民族语言 (yue/bo/kk/mn/ug)：使用中文指令与中文语言名；
- 非中文语向 (XX ⇄ XX，如英译日、英译俄、英译法等)：使用官方英文指令与英文语言名，避免中文标签诱发误译泄漏。
"""

from typing import Optional

DEFAULT_INFERENCE_PARAMS = {
    "temperature": 0.1,
    "top_p": 0.3,
    "top_k": 10,
    "repetition_penalty": 1.05,
    "dry_multiplier": 0.8,
    "dry_allowed_length": 6,
    "dry_penalty_last_n": 512,
    "max_tokens": 4096,
    "seed": 42,
}

# 含中文及其方言/民族语种集合：该集合参与的翻译方向采用官方中文指令模板
CHINESE_PROMPT_LANGS = {
    "zh", "zh-cn", "zh-hant", "zh-tw", "yue", "bo", "kk", "mn", "ug",
}

# Hy-MT2-1.8B 官方完整支持语言列表（常用国际学术语言在前，官方扩展语言在后）
SUPPORTED_LANGUAGES: list[tuple[str, str]] = [
    # 常用国际学术语言
    ("简体中文", "zh"),
    ("English", "en"),
    ("繁體中文", "zh-Hant"),
    ("日本語", "ja"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Русский", "ru"),
    ("Español", "es"),
    ("Português", "pt"),
    ("Italiano", "it"),
    ("العربية", "ar"),
    # 官方扩展语言
    ("Türkçe", "tr"),
    ("ไทย", "th"),
    ("Tiếng Việt", "vi"),
    ("Polski", "pl"),
    ("Nederlands", "nl"),
    ("Čeština", "cs"),
    ("Українська", "uk"),
    ("فارسی", "fa"),
    ("עברית", "he"),
    ("हिन्दी", "hi"),
    ("Bahasa Indonesia", "id"),
    ("Bahasa Melayu", "ms"),
    ("Filipino", "tl"),
    ("ខ្មែរ", "km"),
    ("မြန်မာ", "my"),
    ("বাংলা", "bn"),
    ("اردو", "ur"),
    ("ગુજરાતી", "gu"),
    ("తెలుగు", "te"),
    ("मराठी", "mr"),
    ("தமிழ்", "ta"),
    ("བོད་སྐད་", "bo"),
    ("Қазақша", "kk"),
    ("Монгол", "mn"),
    ("ئۇيغۇرچە", "ug"),
    ("粵語", "yue"),
]

# PDF 排版中可能存在 RTL 或复杂字形组合风险的目标语言集合
COMPLEX_SCRIPT_LANGS = {
    "ar", "fa", "he", "ur", "ug", "hi", "bn", "gu", "te", "mr", "ta", "th", "km", "my", "bo",
}


def should_use_chinese_prompt(lang_in: str, lang_out: str) -> bool:
    """遵循 Hy-MT2 官方规范：仅在源或目标语言属于中文/民族语系时使用中文指令。"""
    return (
        (lang_in or "").strip().lower() in CHINESE_PROMPT_LANGS
        or (lang_out or "").strip().lower() in CHINESE_PROMPT_LANGS
    )


def prompt_default(
    source_text: str,
    target_lang: str = "简体中文",
    use_english: bool = False,
) -> str:
    """腾讯官方标准默认翻译模板 (ZH⇄XX 中文指令 / XX⇄XX 英文指令)。"""
    if use_english:
        return (
            f"Translate the following segment into {target_lang}, without additional explanation.\n\n"
            f"{source_text}"
        )
    return (
        f"将以下文本翻译为 {target_lang}，注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{source_text}"
    )


def prompt_structural_protection(
    source_text: str,
    target_lang: str = "简体中文",
    use_english: bool = False,
) -> str:
    """占位符与代码标签锁定保护模板。"""
    if use_english:
        return (
            f"Translate the following segment into {target_lang}. "
            f"Keep all code tags and variable placeholders (such as [v1]) unchanged in their corresponding positions, "
            f"without additional explanation:\n\n"
            f"{source_text}"
        )
    return (
        f"将以下文本翻译为 {target_lang}，严禁翻译或更改任何代码标签与变量占位符（如 [v1]），"
        f"必须原样完整保留并注意其相对位置。注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{source_text}"
    )


def prompt_with_background(
    source_text: str,
    background_text: str,
    target_lang: str = "简体中文",
    use_english: bool = False,
) -> str:
    """腾讯官方上下文背景辅助翻译模板（不使用易被误译的中文方括号分段标签）。"""
    ctx = background_text.strip()
    if use_english:
        return (
            f"{ctx}\n"
            f"Refer to the above information and translate the following segment into {target_lang}. "
            f"Do not translate the context above, without additional explanation:\n\n"
            f"{source_text}"
        )
    return (
        f"{ctx}\n"
        f"参考上面的信息，把下面的文本翻译成{target_lang}，注意不需要翻译上文，也不要额外解释：\n\n"
        f"{source_text}"
    )


def prompt_structural_with_background(
    source_text: str,
    background_text: str,
    target_lang: str = "简体中文",
    use_english: bool = False,
) -> str:
    """结合学术背景与结构占位符保护的标准精炼组合模板。"""
    ctx = background_text.strip()
    if use_english:
        return (
            f"{ctx}\n"
            f"Refer to the above information and translate the following segment into {target_lang}. "
            f"Do not translate the context above. "
            f"Keep all code tags and variable placeholders (such as [v1]) unchanged in their corresponding positions, "
            f"without additional explanation:\n\n"
            f"{source_text}"
        )
    return (
        f"{ctx}\n"
        f"参考上面的信息，把下面的文本翻译成{target_lang}，严禁翻译或更改任何代码标签与变量占位符（如 [v1]），"
        f"必须原样完整保留并注意其相对位置。注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{source_text}"
    )


def prompt_structural_retry(
    source_text: str,
    missing_tokens: list = None,
    target_lang: str = "简体中文",
    use_english: bool = False,
) -> str:
    """小模型专用无背景纯净重试模板。"""
    return prompt_structural_protection(
        source_text=source_text,
        target_lang=target_lang,
        use_english=use_english,
    )


# ---------------------------------------------------------------------------
# 日常翻译 Hy-MT2 官方提示词模板（术语干预 / 风格预设 / 上下文滑窗）
# ---------------------------------------------------------------------------

DAILY_INFERENCE_PARAMS = {
    "temperature": 0.3,
    "top_p": 0.6,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "dry_multiplier": 0.8,
    "dry_allowed_length": 6,
    "dry_penalty_last_n": 512,
    "max_tokens": 4096,
    "seed": 42,
}

# key -> (中文风格说明, 英文风格说明)
DAILY_STYLE_PRESETS = {
    "default": ("", ""),
    "academic": (
        "学术论文与技术文档风格，用词严谨规范，准确保留专业术语与代码标识",
        "Academic and technical writing, precise and formal terminology, keeping code identifiers intact",
    ),
    "formal": (
        "正式商务书面语，措辞得体礼貌、条理清晰",
        "Formal business writing, polite, clear, and professional",
    ),
    "casual": (
        "自然流畅的日常口语表达，地道亲切、易于阅读",
        "Natural, conversational everyday tone, fluent and approachable",
    ),
    "concise": (
        "简练地道，直接表达核心语义，避免生硬直译与冗余修饰",
        "Concise and idiomatic, conveying the core meaning directly without wordy literal phrasing",
    ),
}


def build_daily_prompt(
    source_text: str,
    target_lang: str = "简体中文",
    glossary_matches: Optional[list[tuple[str, str]]] = None,
    style_key: str = "default",
    context_text: str = "",
    use_english: bool = False,
) -> str:
    """构造严格遵循腾讯官方 Hy-MT2 规范的日常翻译提示词。

    支持：
    1. 官方标准模板（无冗余前缀，推理最快）；
    2. 官方术语表干预模板（参考下面的翻译 / Reference the following translations）；
    3. 官方风格控制模板（注意翻译的风格要严格符合【...】）；
    4. 长文本分段翻译滑窗上下文参考（参考上面的信息，不重译上文）。
    """
    terms = [(s.strip(), t.strip()) for s, t in (glossary_matches or []) if s and s.strip() and t and t.strip()]
    style_pair = DAILY_STYLE_PRESETS.get(style_key or "default", ("", ""))
    style_desc = style_pair[1] if use_english else style_pair[0]
    ctx = (context_text or "").strip()

    # 最简高频路径：无术语、无风格、无上下文 -> 严格输出官方 Default 模板
    if not terms and not style_desc and not ctx:
        return prompt_default(source_text=source_text, target_lang=target_lang, use_english=use_english)

    # 仅带滑窗上下文且无术语/风格 -> 复用官方 Context 模板
    if ctx and not terms and not style_desc:
        return prompt_with_background(
            source_text=source_text,
            background_text=ctx,
            target_lang=target_lang,
            use_english=use_english,
        )

    sections = []
    if ctx:
        if use_english:
            sections.append(f"[Background Information]\n{ctx}\nDo not translate the context above.")
        else:
            sections.append(f"【背景信息】\n{ctx}\n注意不需要翻译上面的背景信息。")

    if terms:
        if use_english:
            term_lines = "\n".join(f"{src} translates to {tgt}" for src, tgt in terms)
            sections.append(f"Reference the following translations:\n{term_lines}")
        else:
            term_lines = "\n".join(f"{src} 翻译成 {tgt}" for src, tgt in terms)
            sections.append(f"参考下面的翻译：\n{term_lines}")

    if use_english:
        inst = f"Translate the following text into {target_lang}."
        if style_desc:
            inst += f" Note that the translation style must strictly conform to [{style_desc}]."
        inst += " Note that you must ONLY output the translated result without any additional explanation:"
    else:
        inst = f"将以下文本翻译为 {target_lang}。"
        if style_desc:
            inst += f"\n注意翻译的风格要严格符合【{style_desc}】。"
        inst += "注意只需要输出翻译后的结果，不要额外解释："

    sections.append(inst)
    return "\n\n".join(sections) + f"\n\n{source_text}"
