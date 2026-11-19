# SPDX-License-Identifier: AGPL-3.0-only
"""Build-time asset preparation; default is verification only, without downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def find_gguf_model(models_dir: Path, requested: str | None = None) -> Path:
    if requested:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = models_dir / candidate
        if not candidate.is_file():
            raise FileNotFoundError(f'指定的翻译模型不存在: {candidate}')
        return candidate
    candidates = sorted(
        f for f in models_dir.glob('*.gguf')
        if f.is_file() and f.stat().st_size >= 1024 * 1024
    )
    if not candidates:
        raise FileNotFoundError(f'models 目录下未找到有效的 *.gguf 翻译模型: {models_dir}')
    if len(candidates) > 1:
        raise ValueError(f'models 目录下存在多个 GGUF 模型，请只保留一个或用 --model 指定: {[c.name for c in candidates]}')
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='从本地缓存收集资产并生成模型清单')
    parser.add_argument('--model', help='指定用于生成清单的 GGUF 模型路径或文件名')
    args = parser.parse_args()
    try:
        from babeldoc.assets import assets
        from babeldoc.const import CACHE_FOLDER
        from core.offline import ensure_offline_assets_ready, matches, resolve_translation_model, validate_gguf_eos
        if args.prepare:
            if sys.platform == 'win32':
                qt = ROOT / 'runtime/python/Lib/site-packages/PySide6'
                native = ROOT / 'runtime/llama.cpp'
                py_root = ROOT / 'runtime/python'
                native.mkdir(parents=True, exist_ok=True)
                for pattern in ('msvcp140*.dll', 'vcruntime140*.dll', 'concrt140.dll'):
                    for dll in qt.glob(pattern):
                        shutil.copy2(dll, native / dll.name)
                        if py_root.is_dir() and not (py_root / dll.name).exists():
                            shutil.copy2(dll, py_root / dll.name)
            for group, entries in assets.generate_all_assets_file_list().items():
                folder = ROOT / ('models' if group == 'models' else f'assets/{group}')
                folder.mkdir(parents=True, exist_ok=True)
                for entry in entries:
                    dest = folder / entry['name']
                    if not matches(dest, entry['sha3_256']):
                        source = CACHE_FOLDER / group / entry['name']
                        if not matches(source, entry['sha3_256']):
                            raise FileNotFoundError(f'本地资产缺失或损坏: {dest}（缓存: {source}）')
                        shutil.copy2(source, dest)
            model = find_gguf_model(ROOT / 'models', args.model)
            validate_gguf_eos(model)
            with model.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha3_256').hexdigest()
            (ROOT / 'assets/model_manifest.json').write_text(json.dumps({
                'name': model.name, 'sha3_256': digest,
            }, indent=2), encoding='utf-8')
        ensure_offline_assets_ready()
        validate_gguf_eos(resolve_translation_model(ROOT))
        runtime = ROOT / 'runtime/llama.cpp' / ('llama-server.exe' if sys.platform == 'win32' else 'llama-server')
        if not runtime.is_file():
            raise FileNotFoundError(runtime)
        if sys.platform == 'win32':
            for name in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
                if not (runtime.parent / name).is_file():
                    raise FileNotFoundError(f'缺少便携 C++ 运行库: {runtime.parent / name}')
        print('离线字体、CMap、分词器、模型和推理程序检查通过。')
        return 0
    except Exception as exc:
        print(f'离线资产检查失败: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
