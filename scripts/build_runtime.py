# SPDX-License-Identifier: AGPL-3.0-only
"""Stage a standalone Python 3.13 runtime under runtime/python for Windows, Linux, or macOS."""
from pathlib import Path
import shutil
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError('请使用 Python 3.13 构建运行时')
    dest = ROOT / 'runtime/python'
    if sys.platform == 'win32':
        base = Path(sys.base_prefix)
        dest.mkdir(parents=True, exist_ok=True)
        for name in ('python.exe', 'pythonw.exe', 'python3.dll', 'python313.dll',
                     'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt'):
            shutil.copy2(base / name, dest / name)
        for name in ('DLLs', 'Lib'):
            shutil.copytree(base / name, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
        (dest / 'python313._pth').write_text('python313.zip\n.\nDLLs\nLib\nLib\\site-packages\n..\\..\\\n', encoding='utf-8')
    else:
        # On Linux/macOS, stage an isolated virtual environment (use standalone Python for multi-machine bundles).
        venv.create(dest, system_site_packages=False, clear=False, symlinks=False, with_pip=False)
    print(dest)


if __name__ == '__main__':
    main()
