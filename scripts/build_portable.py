# SPDX-License-Identifier: AGPL-3.0-only
"""Assemble a standalone portable folder across platforms; never follows developer junctions."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _resolve_python() -> Path:
    for candidate in (
        ROOT / 'runtime/python/python.exe',
        ROOT / 'runtime/python/bin/python3',
        ROOT / 'runtime/python/bin/python',
    ):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _copy_runtime(src_runtime: Path, dst_runtime: Path) -> None:
    """Copy Python and llama.cpp runtimes, omitting only unused developer CLI binaries."""
    py_src = src_runtime / 'python'
    if py_src.is_dir():
        def ignore_py(directory: str, names: list[str]) -> set[str]:
            ignored = {n for n in names if n == '__pycache__' or n.endswith(('.pyc', '.pyo'))}
            if Path(directory).name == 'bin' and Path(directory).parent.name == 'site-packages':
                ignored.update(n for n in names if n in ('ruff', 'ruff.exe'))
            return ignored
        shutil.copytree(py_src, dst_runtime / 'python', ignore=ignore_py)

    llama_src = src_runtime / 'llama.cpp'
    if llama_src.is_dir():
        llama_dst = dst_runtime / 'llama.cpp'
        llama_dst.mkdir(parents=True, exist_ok=True)
        unused_dlls = {
            'ggml-rpc.dll',
            'llama-batched-bench-impl.dll',
            'llama-bench-impl.dll',
            'llama-cli-impl.dll',
            'llama-completion-impl.dll',
            'llama-fit-params-impl.dll',
            'llama-perplexity-impl.dll',
            'llama-quantize-impl.dll',
        }
        for item in llama_src.iterdir():
            if not item.is_file():
                continue
            name = item.name
            if name in unused_dlls:
                continue
            is_shared_lib = item.suffix in ('.dll', '.dylib') or '.so' in item.suffixes
            is_server = name in ('llama-server', 'llama-server.exe')
            is_license = name.startswith('LICENSE')
            if is_server or is_shared_lib or is_license:
                shutil.copy2(item, llama_dst / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/OfflinePDF-Translate')
    args = parser.parse_args()
    dest = args.output.resolve()
    if dest.exists():
        raise FileExistsError(f'Output directory already exists, please choose a new path: {dest}')
    subprocess.run([str(_resolve_python()), str(ROOT / 'scripts/prepare_offline_assets.py')], check=True)
    if ROOT == dest or ROOT.is_relative_to(dest):
        raise ValueError('Output directory cannot be the source directory or its parent')
    dest.mkdir(parents=True)
    for folder in ('core', 'models', 'assets', 'scripts', 'tests'):
        shutil.copytree(ROOT / folder, dest / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    _copy_runtime(ROOT / 'runtime', dest / 'runtime')
    for file in ('gui.py', 'cli.py', 'Start.bat', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'requirements.lock'):
        shutil.copy2(ROOT / file, dest / file)
    start_sh = dest / 'start.sh'
    start_sh.write_text(
        '#!/usr/bin/env bash\n'
        'set -euo pipefail\n'
        'cd "$(dirname "$0")"\n'
        'if [ -x "runtime/python/bin/python3" ]; then\n'
        '  exec "runtime/python/bin/python3" -B gui.py "$@"\n'
        'else\n'
        '  exec python3 -B gui.py "$@"\n'
        'fi\n',
        encoding='utf-8',
        newline='\n',
    )
    start_sh.chmod(0o755)
    manifest = {}
    for file in sorted(dest.rglob('*')):
        if file.is_file():
            with file.open('rb') as stream:
                manifest[file.relative_to(dest).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (dest / 'SHA256SUMS.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Portable package created: {dest}')


if __name__ == '__main__':
    main()
