# SPDX-License-Identifier: AGPL-3.0-only
"""BabelDOC 底层文档翻译管线极简封装。

接入 BabelDOC 原生专有机翻管道 (ILTranslator)，严格遵守 100% 官方模板规范。
使用经过校验的本地资产；不包含扫描件 OCR 识别引擎。
"""

import asyncio
import logging
from pathlib import Path
import re
from typing import Callable, Optional

from .offline import configure_cache, ensure_offline_assets_ready, restrict_network_to_loopback
configure_cache()

import pymupdf
import babeldoc.format.pdf.high_level
from babeldoc.main import TranslationConfig, WatermarkOutputMode

from .typesetting_patch import install_typesetting_patch
install_typesetting_patch()

# 契合本地大模型翻译耗时特征校准各阶段权重：
# 1. 版面解析等翻译前阶段（YOLO/IR/切分）仅耗时数秒，总权重定为 10%；
# 2. 本地大模型段落翻译是核心瓶颈耗时环节，总权重定为 85%；
# 3. 翻译后排版渲染与 PDF 保存输出总权重定为 5%。
if hasattr(babeldoc.format.pdf.high_level, "TRANSLATE_STAGES"):
    _hl = babeldoc.format.pdf.high_level
    _hl.TRANSLATE_STAGES = [
        ("Parse PDF and Create Intermediate Representation", 3.5),
        (_hl.DetectScannedFile.stage_name, 0.5),
        (_hl.LayoutParser.stage_name, 3.5),
        (_hl.TableParser.stage_name, 0.5),
        (_hl.ParagraphFinder.stage_name, 1.5),
        (_hl.StylesAndFormulas.stage_name, 0.5),
        (_hl.AutomaticTermExtractor.stage_name, 0.0),
        (_hl.ILTranslator.stage_name, 85.0),
        (_hl.Typesetting.stage_name, 1.5),
        (_hl.FontMapper.stage_name, 0.5),
        (_hl.PDFCreater.stage_name, 0.5),
        (_hl.SUBSET_FONT_STAGE_NAME, 0.5),
        (_hl.SAVE_PDF_STAGE_NAME, 2.0),
    ]

from .llama_runtime import LlamaServerRuntime
from .translator import HunyuanLocalTranslator

logger = logging.getLogger("offlinepdf-translate.engine")


from .delivery import validate_pdf


def truncate_to_complete_sentences(text: str, max_words: int = 400) -> str:
    """多语言智能断句截断：优先在标点符号完整句子边界截断；无法判定时保底按单词硬截断。"""
    words = text.split()
    if len(words) <= max_words:
        return text

    # 多语言句末标点体系：
    # - 西文/西里尔/西班牙语等拉丁文：. ! ?
    # - 汉语/日语/韩语等 CJK：。 ！？
    # - 阿拉伯语/波斯语/乌尔都语：؟（镜像问号） .(句点) ۔（乌尔都语句号）
    # - 印地语/梵文/孟加拉等天城体：।（单竖线 Danda） ॥（双竖线）
    # - 希腊语：;（希腊语问号，分号字形）
    # 搭配常见外层闭合引号与括号："'’”」』）\)
    abbr_filter = r"(?<!\bet al)(?<!\be\.g)(?<!\bi\.e)(?<!\bFig)(?<!\bEq)(?<!\bDr)(?<!\bvs)(?<!\bRef)(?<!\bz\. B)(?<!\bd\. h)(?<!\bp\. ex)"
    pattern = abbr_filter + r"(?<=[.!?。！？؟۔।॥;])[\"'’”」』）\)]*(?:\s+|$)"

    try:
        split_points = [m.end() for m in re.finditer(pattern, text)]
    except Exception:
        split_points = []

    # 若无法识别出任何句末标点，直接按单词硬截断
    if not split_points:
        return " ".join(words[:max_words])

    # 寻找在 max_words 约束范围内的最后一个完整句子切分点
    best_cut = None
    for pt in split_points:
        candidate = text[:pt].strip()
        c_words = len(candidate.split())
        if c_words <= max_words:
            best_cut = candidate
        else:
            break

    if best_cut:
        return best_cut

    # 若首句本身就已经超出 max_words，若稍微超出一截 (<= 1.25x) 则保全首句完整性，否则按单词硬截断
    first_pt = split_points[0]
    first_sent = text[:first_pt].strip()
    if len(first_sent.split()) <= max_words * 1.25:
        return first_sent

    return " ".join(words[:max_words])


def extract_document_background(pdf_path: Path, max_words: int = 400) -> str:
    """精准提取文献/书籍/幻灯片的标题及显式摘要作为自适应背景，约束在 400 词以内。

    1. 跳过期刊出版商声明页、空白/纯图封面页与书籍版权/ISBN/目录前置页；
    2. 仅当文档存在显式 Abstract 标题时才附加 Abstract 字段，避免将书籍前言、版权页或 PPT 要点误标为摘要；
    3. 严格基于标点符号在完整句子边界截断，绝不切断句子内部。
    """
    doc = None
    try:
        doc = pymupdf.open(str(pdf_path))
        if len(doc) == 0 or doc.needs_pass:
            return ""

        target_page = None
        abstract_text = ""
        keywords_text = ""
        has_explicit_abstract = False

        # 0. 探测并跳过出版商声明页、纯图封面页、版权/CIP 页或目录页
        start_pno = 0
        cover_disclaimers = [
            "for your personal, non-commercial use only",
            "permission to republish or repurpose",
            "order high-quality copies for your colleagues",
            "see discussions, stats, and author profiles",
            "this content downloaded from",
            "electronic copy available at:",
            "downloaded from https://academic.oup.com",
            "terms & conditions of access and use",
            "articles you may be interested in",
            "view table of contents",
            "published by the aip publishing",
        ]
        book_frontmatter_markers = [
            "cataloging-in-publication",
            "library of congress control number",
            "british library cataloguing",
            "printed in the united states",
            "table of contents",
        ]
        while start_pno < min(3, len(doc) - 1):
            p_txt = doc[start_pno].get_text().strip()
            p_lower = p_txt.lower()
            is_cover_disclaimer = any(d in p_lower for d in cover_disclaimers)
            is_image_cover = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", p_txt)) < 2
            is_copyright_or_toc = (
                ("isbn" in p_lower and ("copyright" in p_lower or "all rights reserved" in p_lower))
                or any(m in p_lower for m in book_frontmatter_markers)
            )
            if is_cover_disclaimer or is_image_cover or is_copyright_or_toc:
                logger.info("探测到第 %d 页为封面/声明/版权前置页，学术背景提取跳至下一页", start_pno + 1)
                start_pno += 1
            else:
                break

        # 1. 查找含有显式 Abstract 标题的页面（最多探测正文前 4 页）
        for pno in range(start_pno, min(start_pno + 4, len(doc))):
            page = doc[pno]
            blocks = page.get_text("blocks")
            for i, b in enumerate(blocks):
                btext = b[4].strip()
                # 兼容普通 "Abstract" 及 Elsevier/Springer 等期刊的空格式 "A B S T R A C T"
                is_abs_header = bool(re.search(r"^a\s*b\s*s\s*t\s*r\s*a\s*c\s*t\b", btext, re.IGNORECASE))
                if is_abs_header:
                    after_abs = re.sub(r"^a\s*b\s*s\s*t\s*r\s*a\s*c\s*t[:\s—\-\.]*", "", btext, flags=re.IGNORECASE).strip()
                    if len(after_abs.split()) > 10:
                        abstract_text = after_abs
                    elif i + 1 < len(blocks):
                        abstract_text = blocks[i + 1][4].strip()
                    target_page = page
                    has_explicit_abstract = bool(abstract_text)
                    break
                elif re.search(r"\ba\s*b\s*s\s*t\s*r\s*a\s*c\s*t[:\s—\-\.]", btext, re.IGNORECASE):
                    m = re.search(r"\ba\s*b\s*s\s*t\s*r\s*a\s*c\s*t[:\s—\-\.](.+)", btext, re.IGNORECASE | re.DOTALL)
                    if m and len(m.group(1).split()) > 10:
                        abstract_text = m.group(1).strip()
                        target_page = page
                        has_explicit_abstract = True
                        break
            if target_page:
                break

        if not target_page:
            target_page = doc[start_pno]

        # 2. 定位标题：基于 target_page 字号与文本特征加权，剔除作者、机构、版权与目录杂讯
        d = target_page.get_text("dict")
        # 探测期刊顶部横幅边界 (如 Elsevier 的 journal homepage, contents lists, full length article 等)
        header_bottom_y = 0.0
        for b in d.get("blocks", []):
            if "lines" not in b:
                continue
            btxt = " ".join("".join(s["text"] for s in l["spans"]) for l in b["lines"]).lower()
            if any(k in btxt for k in ["journal homepage", "contents lists", "full length article", "review article", "sciencedirect"]):
                header_bottom_y = max(header_bottom_y, b.get("bbox", [0, 0, 0, 0])[3])

        candidate_titles = []
        for block in d.get("blocks", []):
            if "lines" not in block:
                continue
            # 若落在期刊元数据横幅区域内则跳过
            if header_bottom_y > 0 and block.get("bbox", [0, 0, 0, 0])[1] < header_bottom_y:
                continue
            txt = ""
            max_size = 0.0
            for l in block["lines"]:
                for s in l["spans"]:
                    txt += s["text"] + " "
                    if s["size"] > max_size:
                        max_size = s["size"]
            txt = re.sub(r"\s+", " ", txt).strip()
            if re.search(r"\ba\s*b\s*s\s*t\s*r\s*a\s*c\s*t\b", txt, re.IGNORECASE):
                break
            if re.search(r"(@|arxiv|permission|licen[sc]e|copyright|all rights|isbn\b|library of congress|grant|conference|proceedings|affiliation|school of|department of|university|cite as:|published|http|doi\.org|articles|research\b|sciencedirect|journal homepage|contents lists|full length article|elsevier|article in press|table of contents|\.{4,})", txt, re.IGNORECASE):
                continue
            if re.match(r"^(?:chapter|part|section|preface|contents)\s*\d*$", txt, re.IGNORECASE):
                continue
            # 论文/书籍/幻灯片主标题通常为 2 至 30 个词，或无空格 CJK 标题（4~80 字符）
            words = txt.split()
            cjk_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", txt))
            if (2 <= len(words) <= 30) or (cjk_chars >= 4 and len(txt) <= 80):
                candidate_titles.append((max_size, len(words) >= 4 or cjk_chars >= 8, len(txt), txt))

        candidate_titles.sort(key=lambda x: (round(x[0]), x[1], x[2]), reverse=True)
        title_text = candidate_titles[0][3] if candidate_titles else ""

        # 3. 仅在有显式 Abstract 的论文中查找关键词 (Keywords / Index Terms)
        if has_explicit_abstract:
            for b in target_page.get_text("blocks"):
                btext = b[4].strip()
                m = re.search(r"(?:keywords|index terms)[:\s—\-]+(.+)", btext, re.IGNORECASE)
                if m:
                    keywords_text = m.group(1).replace("\n", " ").strip()
                    break

        lines = []
        if title_text:
            lines.append(f"Title: {title_text}")

        # 计算剩余可用词数预算
        title_words = len(title_text.split()) if title_text else 0
        kw_words = len(keywords_text.split()) if keywords_text else 0
        avail_for_abs = max(50, max_words - title_words - (kw_words + 2 if keywords_text else 0) - 4)

        if has_explicit_abstract and abstract_text:
            abs_clean = " ".join(abstract_text.replace("\n", " ").split())
            if len(abs_clean.split()) > avail_for_abs:
                abs_clean = truncate_to_complete_sentences(abs_clean, max_words=avail_for_abs)
            lines.append(f"Abstract: {abs_clean}")

        if keywords_text:
            kw_clean = " ".join(keywords_text.replace("\n", " ").split())
            lines.append(f"Keywords: {kw_clean}")

        if not lines:
            return ""

        combined = "\n\n".join(lines)
        if len(combined.split()) > max_words:
            combined = truncate_to_complete_sentences(combined, max_words=max_words)
        return combined
    except Exception as e:
        logger.warning("提取文献背景失败: %s", e)
        return ""
    finally:
        if doc is not None:
            doc.close()


def preflight_check_pdf(input_pdf: Path) -> None:
    """在启动本地推理引擎前快速校验输入 PDF 是否存在、未加密且含可提取文字。"""
    input_pdf = Path(input_pdf)
    if not input_pdf.is_file():
        raise RuntimeError(f"文件不存在: {input_pdf}")
    with pymupdf.open(str(input_pdf)) as doc:
        if doc.needs_pass or doc.page_count == 0:
            raise RuntimeError(f"无法读取此 PDF，文件已加密或页面为空: {input_pdf.name}")
        if not any(page.get_text("text").strip() for page in doc):
            raise RuntimeError(f"不支持扫描版 PDF（未检测到可提取文字，请先进行 OCR 识别后重试）: {input_pdf.name}")


async def translate_document_async(
    input_pdf: Path,
    output_dir: Path,
    runtime: Optional[LlamaServerRuntime] = None,
    lang_in: str = "en",
    lang_out: str = "zh",
    pages: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    enable_background: bool = True,
) -> dict:
    input_pdf = Path(input_pdf).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 0. 确保本地离线字体、模型与分词资产完整挂载（断网环境即开即用），并校验输入 PDF 可读未加密
    restrict_network_to_loopback()
    ensure_offline_assets_ready()
    if input_pdf.is_file():
        preflight_check_pdf(input_pdf)

    # 1. 自动提取完整文献学术背景 (Full Title + Full Abstract + Keywords)
    doc_bg = extract_document_background(input_pdf) if enable_background else ""
    if doc_bg:
        logger.info("文献学术背景探测成功 (%d 词):\n%s", len(doc_bg.split()), doc_bg[:150] + "...")
    else:
        logger.info("未识别到可靠文献标题/摘要背景或未启用背景辅助，使用默认翻译策略")

    # 2. 载入版面分析 ONNX 模型（优先使用项目内置独立模型）
    local_onnx = Path(__file__).resolve().parents[1] / "models" / "doclayout_yolo_docstructbench_imgsz1024.onnx"
    if local_onnx.exists():
        from babeldoc.docvision.doclayout import OnnxModel
        logger.info("载入项目内置独立版面分析模型: %s", local_onnx)
        doc_layout_model = OnnxModel(local_onnx)
    else:
        raise FileNotFoundError(f"缺少离线版面模型: {local_onnx}")

    # 3. 组装翻译器
    if runtime is None:
        raise ValueError("hymt2 引擎需要传入有效的 LlamaServerRuntime 实例")
    def handle_block_translated(block_event: dict):
        if progress_callback:
            progress_callback(block_event)

    translator = HunyuanLocalTranslator(
        runtime=runtime,
        lang_in=lang_in,
        lang_out=lang_out,
        ignore_cache=True,
        on_block_translated=handle_block_translated,
    )
    if doc_bg:
        translator.set_background(doc_bg)
    workers = max(1, runtime.parallel)

    # 4. 构造 TranslationConfig 配置（动态对齐 runtime 并发数与启用双层扫描件兼容）
    config = TranslationConfig(
        translator=translator,
        input_file=input_pdf,
        lang_in=lang_in,
        lang_out=lang_out,
        doc_layout_model=doc_layout_model,
        output_dir=output_dir,
        working_dir=output_dir / "work",
        pages=pages,
        no_dual=False,
        no_mono=False,
        use_side_by_side_dual=True,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        auto_extract_glossary=False,
        save_auto_extracted_glossary=False,
        auto_enable_ocr_workaround=True,  # 允许双层扫描文献利用底层隐式文本层翻译，杜绝 ScannedPDFError 闪退
        pool_max_workers=workers,
        qps=workers * 2,
        report_interval=0.2,
    )

    result_info = None
    async for event in babeldoc.format.pdf.high_level.async_translate(config):
        if progress_callback:
            progress_callback(event)
        if event["type"] == "finish":
            result_info = event.get("translate_result", {})
            break
        elif event["type"] == "error":
            err_msg = str(event.get("error") or "")
            err_lower = err_msg.lower()
            if "no paragraphs" in err_lower or "scanned" in err_lower:
                raise RuntimeError(f"不支持扫描版 PDF（未检测到可翻译文本段落，请先进行 OCR 识别后重试）: {input_pdf.name}")
            raise RuntimeError(f"BabelDOC 翻译失败: {err_msg}")

    if result_info is None:
        raise RuntimeError("翻译管线未正常完成，未收到 finish 事件")
    paths = {}
    for key in ("mono_pdf_path", "dual_pdf_path"):
        value = result_info.get(key) if isinstance(result_info, dict) else getattr(result_info, key, None)
        if value:
            try:
                validate_pdf(value)
                paths[key] = str(value)
            except Exception as e:
                logger.warning("交付物 %s 校验未通过: %s", key, e)
    if not paths:
        raise RuntimeError("翻译管线未产生任何有效交付物 (双语对照与纯译文均生成失败)")

    return {
        "status": "success",
        "result": paths,
        "translator_stats": getattr(translator, "stats", {}),
    }


def translate_document(
    input_pdf: Path,
    output_dir: Path,
    runtime: Optional[LlamaServerRuntime] = None,
    lang_in: str = "en",
    lang_out: str = "zh",
    pages: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    enable_background: bool = True,
) -> dict:
    return asyncio.run(
        translate_document_async(
            input_pdf=input_pdf,
            output_dir=output_dir,
            runtime=runtime,
            lang_in=lang_in,
            lang_out=lang_out,
            pages=pages,
            progress_callback=progress_callback,
            enable_background=enable_background,
        )
    )
