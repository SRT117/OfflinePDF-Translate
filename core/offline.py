# SPDX-License-Identifier: AGPL-3.0-only
"""Verified bundle assets and a loopback-only network policy for runtime use."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
_OFFLINE_VERIFIED = False


def user_data_dir():
    """Writable application data, independent of the installation directory."""
    override = os.environ.get('OFFLINEPDF_DATA_DIR')
    if override:
        base = Path(override)
    else:
        if sys.platform == 'win32':
            parent = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
        elif sys.platform == 'darwin':
            parent = Path.home() / 'Library' / 'Application Support'
        else:
            parent = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
        base = parent / 'OfflinePDF Translate'
    base.mkdir(parents=True, exist_ok=True)
    return base



class OfflineAssetError(RuntimeError):
    pass


def configure_cache(cache_base=None):
    import babeldoc.const as const
    cache_base = Path(cache_base or user_data_dir() / 'cache')
    cache_base.mkdir(parents=True, exist_ok=True)
    const.CACHE_FOLDER = cache_base
    const.TIKTOKEN_CACHE_FOLDER = cache_base / 'tiktoken'
    os.environ['TIKTOKEN_CACHE_DIR'] = str(const.TIKTOKEN_CACHE_FOLDER)
    return cache_base


def is_valid_asset(path: Path) -> bool:
    """快速检查资产是否存在且非空（无需花费大量时间重复计算哈希）。"""
    return path.is_file() and path.stat().st_size > 0


def _cache_needs_refresh(source: Path, target: Path) -> bool:
    """Return True when cached asset is missing, empty, or differs in size/mtime from source."""
    if not is_valid_asset(target):
        return True
    try:
        src_stat = source.stat()
        tgt_stat = target.stat()
    except OSError:
        return True
    return (
        tgt_stat.st_size != src_stat.st_size
        or abs(tgt_stat.st_mtime_ns - src_stat.st_mtime_ns) > 1_000_000
    )


def matches(path, digest):
    """历史兼容哈希核验函数（仅在调试或显式需要时调用）。"""
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha3_256').hexdigest() == digest


def resolve_translation_model(root=None) -> Path:
    """Resolve the bundled GGUF translation model without hardcoding a quantization filename."""
    root = Path(root or ROOT)
    models_dir = root / 'models'
    manifest_file = root / 'assets/model_manifest.json'
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            candidate = models_dir / manifest['name']
            if candidate.is_file() and candidate.stat().st_size >= 1024 * 1024:
                return candidate
        except (OSError, ValueError, KeyError):
            pass

    candidates = sorted(
        f for f in models_dir.glob('*.gguf')
        if f.is_file() and f.stat().st_size >= 1024 * 1024
    )
    if candidates:
        return candidates[0]
    raise OfflineAssetError(f'models 目录下未找到有效的 *.gguf 翻译模型: {models_dir}')


def validate_gguf_eos(model_path: Path) -> None:
    """Validate GGUF header and guard against misconfigured EOS tokens (e.g. eos_token_id=3)."""
    model_path = Path(model_path)
    if not model_path.is_file():
        raise OfflineAssetError(f'翻译模型不存在: {model_path}')

    with model_path.open('rb') as stream:
        if stream.read(4) != b'GGUF':
            raise OfflineAssetError(f'翻译模型不是有效的 GGUF 格式: {model_path}')
        _ver, _n_tensors, n_kv = struct.unpack('<IQQ', stream.read(20))

        def read_str() -> str:
            length = struct.unpack('<Q', stream.read(8))[0]
            return stream.read(length).decode('utf-8', 'replace')

        def skip_or_read(vtype: int, keep: bool = False):
            scalars = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i', 6: '<f', 7: '<?', 10: '<Q', 11: '<q', 12: '<d'}
            if vtype == 8:
                return read_str() if keep else stream.seek(struct.unpack('<Q', stream.read(8))[0], 1)
            if vtype in scalars:
                fmt = scalars[vtype]
                size = struct.calcsize(fmt)
                return struct.unpack(fmt, stream.read(size))[0] if keep else stream.seek(size, 1)
            if vtype == 9:
                atype, alen = struct.unpack('<IQ', stream.read(12))
                if keep:
                    return [skip_or_read(atype, keep=True) for _ in range(alen)]
                if atype in scalars:
                    stream.seek(struct.calcsize(scalars[atype]) * alen, 1)
                else:
                    for _ in range(alen):
                        skip_or_read(atype, keep=False)
                return None
            raise OfflineAssetError(f'无法解析 GGUF 元数据类型 {vtype}: {model_path}')

        arch = None
        eos_id = None
        tokens = None
        for _ in range(n_kv):
            key = read_str()
            vtype = struct.unpack('<I', stream.read(4))[0]
            if key in ('general.architecture', 'tokenizer.ggml.eos_token_id', 'tokenizer.ggml.tokens'):
                val = skip_or_read(vtype, keep=True)
                if key == 'general.architecture':
                    arch = val
                elif key == 'tokenizer.ggml.eos_token_id':
                    eos_id = val
                elif key == 'tokenizer.ggml.tokens':
                    tokens = val
            else:
                skip_or_read(vtype, keep=False)
            if arch is not None and eos_id is not None and tokens is not None:
                break

    if arch == 'hunyuan-dense' and isinstance(tokens, list) and len(tokens) > 120020:
        if tokens[120020] == '<｜hy_place▁holder▁no▁2｜>' and eos_id != 120020:
            raise OfflineAssetError(
                f'GGUF 结束符配置异常: {model_path.name} 的 tokenizer.ggml.eos_token_id={eos_id}（应为 120020），会导致推理无限生成'
            )


def ensure_offline_assets_ready(root=None, cache_base=None, force_verify=False):
    global _OFFLINE_VERIFIED
    root = Path(root or ROOT)
    cache_base = Path(cache_base or (user_data_dir() / 'cache' if root == ROOT else root / 'var' / 'cache'))
    if _OFFLINE_VERIFIED and not force_verify and root == ROOT:
        configure_cache(cache_base)
        return cache_base

    from babeldoc.assets.assets import generate_all_assets_file_list
    required = generate_all_assets_file_list()
    missing = []
    try:
        manifest_file = root / 'assets/model_manifest.json'
        if manifest_file.is_file():
            model = json.loads(manifest_file.read_text(encoding='utf-8'))
            model_path = root / 'models' / model['name']
            # 真实 GGUF 模型通常在数百 MB 以上，至少大于 1MB；若只有几个字节说明文件损坏或下载中断
            if not model_path.is_file() or model_path.stat().st_size < 1024 * 1024:
                missing.append('翻译 GGUF 模型损坏或未完整放置')
        else:
            # 兼容用户自定义放置的 *.gguf 模型
            ggufs = [f for f in (root / 'models').glob('*.gguf') if f.stat().st_size > 1024 * 1024]
            if not ggufs:
                missing.append('models 目录下未找到有效的 *.gguf 翻译模型')
    except (OSError, ValueError, KeyError):
        missing.append('assets/model_manifest.json 缺失或损坏')

    for group, entries in required.items():
        source_dir = root / ('models' if group == 'models' else f'assets/{group}')
        for entry in entries:
            source = source_dir / entry['name']
            if not is_valid_asset(source):
                missing.append(str(source.relative_to(root)))

    if missing:
        raise OfflineAssetError('离线资产缺失或损坏，请运行 scripts/prepare_offline_assets.py --prepare：\n'
                                + '\n'.join(missing))

    for group, entries in required.items():
        source_dir = root / ('models' if group == 'models' else f'assets/{group}')
        for entry in entries:
            source = source_dir / entry['name']
            target = cache_base / group / entry['name']
            if _cache_needs_refresh(source, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                pending = target.with_suffix(target.suffix + '.pending')
                shutil.copy2(source, pending)
                os.replace(pending, target)

    # BabelDOC's cache location is a module constant, not an environment setting.
    configure_cache(cache_base)
    _OFFLINE_VERIFIED = True
    return cache_base


def restrict_network_to_loopback():
    """Prevent external sockets/DNS, while allowing the local inference server."""
    if getattr(socket, '_offlinepdf_offline', False):
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_lookup = socket.getaddrinfo

    def check(host):
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise OSError(f'离线模式禁止外部网络连接: {host}')

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return original_connect_ex(sock, address)

    def lookup(host, *args, **kwargs):
        if host is not None:
            check(host)
        return original_lookup(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = lookup
    socket._offlinepdf_offline = True
