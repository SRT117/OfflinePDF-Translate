# SPDX-License-Identifier: AGPL-3.0-only
"""Single-document process boundary shared by GUI and CLI."""
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from .process_tree import ProcessTree
from .offline import configure_cache
configure_cache()

# 系统防睡眠保活（Windows: SetThreadExecutionState; macOS: caffeinate）
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
_KEEP_AWAKE_LOCK = threading.Lock()
_CAFFEINATE_PROC = None


def set_system_keep_awake(enable: bool = True) -> None:
    """跨平台挂机防休眠保活：翻译期间阻止操作系统自动睡眠，完成后恢复。"""
    global _CAFFEINATE_PROC
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
            kernel.SetThreadExecutionState.restype = wintypes.DWORD
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
            kernel.SetThreadExecutionState(flags)
        except Exception:
            pass
    elif sys.platform == 'darwin':
        with _KEEP_AWAKE_LOCK:
            if enable:
                if _CAFFEINATE_PROC is None or _CAFFEINATE_PROC.poll() is not None:
                    caffeinate = shutil.which('caffeinate')
                    if caffeinate:
                        try:
                            _CAFFEINATE_PROC = subprocess.Popen(
                                [caffeinate, '-i', '-w', str(os.getpid())],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        except OSError:
                            _CAFFEINATE_PROC = None
            else:
                if _CAFFEINATE_PROC is not None:
                    try:
                        _CAFFEINATE_PROC.terminate()
                        _CAFFEINATE_PROC.wait(timeout=1)
                    except Exception:
                        pass
                    finally:
                        _CAFFEINATE_PROC = None


def _set_thread_keep_awake(enable: bool = True) -> None:
    set_system_keep_awake(enable)


class JobCancelled(RuntimeError):
    pass


class JobFailed(RuntimeError):
    pass


def document_process(channel, kwargs, runtime_options):
    try:
        from .offline import restrict_network_to_loopback
        restrict_network_to_loopback()
        from .engine import translate_document
        from .llama_runtime import LlamaServerRuntime
        runtime = LlamaServerRuntime(**runtime_options)
        result = translate_document(
            runtime=runtime,
            progress_callback=lambda event: channel.put(('progress', event)),
            **kwargs,
        )
        channel.put(('result', result))
    except BaseException:
        channel.put(('error', traceback.format_exc()))


def job_entry(target, channel, kwargs, options):
    if os.name != 'nt':
        os.setsid()
    target(channel, kwargs, options)


def run_document_job(runtime, input_pdf, output_dir, lang_in='en', lang_out='zh',
                     pages=None, progress_callback=None, cancelled=lambda: False,
                     inactivity_limit=900, target=document_process,
                     enable_background=True):
    """执行单文档翻译隔离任务。
    
    inactivity_limit 默认提升至 900 秒 (15 分钟)，与底层单段 600 秒 (10 分钟) 推理超时深度对齐，
    兼顾低配笔记本纯 CPU 推理耗时，并自动激活系统唤醒锁防止通宵挂机休眠。
    """
    _set_thread_keep_awake(True)
    ctx = multiprocessing.get_context('spawn')
    channel = ctx.Queue()
    options = {name: getattr(runtime, name) for name in (
        'model_path', 'runtime_bin', 'threads', 'context_size', 'parallel',
        'n_gpu_layers', 'port', 'log_path')}
    kwargs = dict(input_pdf=Path(input_pdf), output_dir=Path(output_dir),
                  lang_in=lang_in, lang_out=lang_out, pages=pages,
                  enable_background=enable_background)
    process = ctx.Process(target=job_entry, args=(target, channel, kwargs, options))
    last_progress = time.monotonic()
    result = None
    tree = None
    try:
        process.start()
        tree = ProcessTree(process.pid)
        while True:
            if cancelled():
                raise JobCancelled('用户已停止')
            if time.monotonic() - last_progress > inactivity_limit:
                if runtime and hasattr(runtime, 'stop'):
                    runtime.stop()
                raise JobFailed(f'文档连续 {inactivity_limit} 秒无进展，已终止处理进程')
            try:
                kind, payload = channel.get(timeout=0.1)
            except queue.Empty:
                if not process.is_alive():
                    raise JobFailed(f'文档进程异常退出，退出码 {process.exitcode}')
                continue
            if kind == 'progress':
                last_progress = time.monotonic()
                if progress_callback:
                    progress_callback(payload)
            elif kind == 'result':
                result = payload
                break
            elif kind == 'error':
                raise JobFailed(payload)
        return result
    finally:
        _set_thread_keep_awake(False)
        if tree:
            tree.close()
        if process.pid:
            # Result may be queued just before normal process shutdown.
            process.join(timeout=0.5 if result is not None else 0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()
        channel.close()
        channel.join_thread()
