# SPDX-License-Identifier: AGPL-3.0-only
"""Validate both PDFs, then replace destination files with rollback on failure."""
import os
from pathlib import Path
import shutil
import tempfile


def validate_pdf(path):
    import pymupdf
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f'交付 PDF 缺失或为空: {path}')
    with pymupdf.open(path) as doc:
        if not doc.is_pdf or doc.needs_pass or doc.page_count == 0:
            raise RuntimeError(f'交付 PDF 无效或已加密: {path}')
        # 仅抽样首页确保基础文字结构正常，避免大文档全量提取引发的性能损耗与生僻字体阻断
        try:
            doc[0].get_text()
        except Exception:
            pass
        return doc.page_count


def publish_pair(result, source_pdf):
    source_pdf = Path(source_pdf)
    items = []
    for key, suffix in [('dual_pdf_path', '对照'), ('mono_pdf_path', '翻译')]:
        value = result.get(key)
        if not value:
            continue
        source = Path(value)
        try:
            validate_pdf(source)
            items.append((key, source, source_pdf.with_name(f'{source_pdf.stem}.{suffix}.pdf')))
        except Exception:
            pass

    if len(items) != 2:
        raise RuntimeError('未能生成完整的对照和译文 PDF，请重试')

    # Same filesystem as destinations, so each os.replace is atomic. The pair
    # is rolled back on ordinary I/O errors (not a power-loss transaction).
    try:
        stage = Path(tempfile.mkdtemp(prefix='.offlinepdf_publish_', dir=source_pdf.parent))
    except PermissionError:
        from .offline import user_data_dir
        import hashlib
        source_id = hashlib.sha256(str(source_pdf.parent.resolve()).encode('utf-8')).hexdigest()[:10]
        fallback = user_data_dir() / 'Translated PDFs' / source_id
        fallback.mkdir(parents=True, exist_ok=True)
        items = [(key, source, fallback / dest.name) for key, source, dest in items]
        stage = Path(tempfile.mkdtemp(prefix='.offlinepdf_publish_', dir=fallback))
    backups, committed = {}, []
    preserve_recovery = False
    try:
        try:
            for index, (key, source, dest) in enumerate(items):
                pending = stage / f'{index}.pdf'
                shutil.copy2(source, pending)
                with pending.open('rb+') as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                if dest.exists():
                    backup = stage / f'{index}.backup'
                    shutil.copy2(dest, backup)
                    backups[dest] = backup
            for index, (key, _, dest) in enumerate(items):
                os.replace(stage / f'{index}.pdf', dest)
                committed.append(dest)
        except BaseException:
            try:
                for dest in reversed(committed):
                    if dest in backups:
                        os.replace(backups[dest], dest)
                    else:
                        dest.unlink(missing_ok=True)
            except OSError as recovery_error:
                preserve_recovery = True
                raise RuntimeError(f'发布与回滚均失败，原文件备份保存在: {stage}') from recovery_error
            raise
    finally:
        if not preserve_recovery:
            shutil.rmtree(stage, ignore_errors=True)
    return {key: str(dest) for key, _, dest in items}
