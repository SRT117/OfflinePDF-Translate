# SPDX-License-Identifier: AGPL-3.0-only
"""OfflinePDF-Translate: Local offline academic PDF translation CLI."""

import argparse
import logging
from pathlib import Path
import sys
import time
import tempfile

from core.jobs import run_document_job
from core.delivery import publish_pair
from core.hardware import detect_hardware_profile
from core.llama_runtime import LlamaServerRuntime
from core.offline import resolve_translation_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("offlinepdf-translate")

ROOT = Path(__file__).resolve().parent


def cmd_translate(args: argparse.Namespace) -> None:
    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        logger.error("Input PDF file not found: %s", pdf_path)
        sys.exit(1)

    out_dir = Path(args.output).resolve() if args.output else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = Path(args.model).resolve() if args.model else resolve_translation_model(ROOT)
    runtime_bin = Path(args.runtime).resolve() if args.runtime else None

    def on_progress(event: dict) -> None:
        stage = event.get("type")
        if stage == "parse":
            logger.info("Parsing layout and extracting formulas...")
        elif stage == "translate":
            logger.info("Translating paragraphs and formulas...")
        elif stage == "generate":
            logger.info("Rendering translated and bilingual PDFs...")

    # Auto-detect hardware profile and available memory
    prof = detect_hardware_profile(
        override_threads=args.threads,
        override_parallel=args.parallel,
        override_gpu_layers=args.gpu_layers,
        override_ctx_size=args.ctx_size,
        runtime_bin=runtime_bin,
        model_path=model_path,
    )

    logger.info("==================================================")
    logger.info("Starting OfflinePDF-Translate local adaptive pipeline")
    logger.info("PDF file: %s", pdf_path)
    logger.info("Output dir: %s", out_dir)
    logger.info("Model: %s", model_path.name)
    logger.info("Hardware: %s", prof.device_summary)
    if args.pages:
        logger.info("Pages: %s", args.pages)
    logger.info("==================================================")

    started = time.monotonic()
    runtime = LlamaServerRuntime(
        model_path=model_path,
        runtime_bin=runtime_bin,
        threads=prof.threads,
        context_size=prof.context_size,
        parallel=prof.parallel,
        n_gpu_layers=prof.n_gpu_layers,
    )

    with runtime:
        with tempfile.TemporaryDirectory(prefix='offlinepdf_cli_') as td:
            summary = run_document_job(
                input_pdf=pdf_path, output_dir=Path(td), runtime=runtime,
                lang_in=args.lang_in, lang_out=args.lang_out,
                pages=args.pages, progress_callback=on_progress,
                enable_background=not args.no_background)
            publish_pair(summary['result'], out_dir / pdf_path.name)

        elapsed = time.monotonic() - started

        logger.info("==================================================")
        logger.info("Translation completed in %.2fs", elapsed)
        logger.info("Stats: %s", summary.get("translator_stats") if summary else {})
        logger.info("Output directory: %s", out_dir)
        logger.info("==================================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="OfflinePDF-Translate: Offline local academic PDF translation tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # translate subcommand
    p_trans = subparsers.add_parser("translate", help="Translate a PDF document")
    p_trans.add_argument("pdf", help="Path to the input PDF file")
    p_trans.add_argument("-o", "--output", help="Output directory path")
    p_trans.add_argument("-p", "--pages", help="Page range, e.g. '1-3' or '1,3,5'")
    p_trans.add_argument("-t", "--threads", type=int, default=None, help="CPU inference threads (default: auto-detected)")
    p_trans.add_argument("--parallel", type=int, default=None, help="Parallel inference slots (default: auto-detected)")
    p_trans.add_argument("--ctx-size", type=int, default=None, help="Model context window size (default: auto 4096 or 8192)")
    p_trans.add_argument("--gpu-layers", "--ngl", dest="gpu_layers", type=int, default=None, help="GPU offload layers (default: auto-detected)")
    p_trans.add_argument("--model", help="Path to GGUF model file")
    p_trans.add_argument("--runtime", help="Path to llama-server executable")
    p_trans.add_argument("--lang-in", default="auto", help="Source language (default: auto)")
    p_trans.add_argument("--lang-out", default="en", help="Target language (default: en)")
    p_trans.add_argument("--no-background", action="store_true", default=False, help="Disable academic context injection")

    args = parser.parse_args()
    if args.command == "translate":
        cmd_translate(args)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logger.error('%s', exc)
        sys.exit(1)
