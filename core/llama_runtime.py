# SPDX-License-Identifier: AGPL-3.0-only
"""llama.cpp 本地高性能轻量推断运行时封装。"""

import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

logger = logging.getLogger("offlinepdf-translate.runtime")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServerRuntime:
    def __init__(
        self,
        model_path: Path,
        runtime_bin: Optional[Path] = None,
        threads: int = 4,
        context_size: int = 8192,
        parallel: int = 1,
        n_gpu_layers: int = 0,
        port: Optional[int] = None,
        log_path: Optional[Path] = None,
    ):
        self.model_path = Path(model_path).resolve()
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file not found: {self.model_path}")

        base = Path(__file__).resolve().parents[1]
        if runtime_bin is None:
            candidate = base / 'runtime' / 'llama.cpp' / ('llama-server.exe' if sys.platform == 'win32' else 'llama-server')
            if candidate.is_file():
                runtime_bin = candidate
            elif sys.platform != 'win32':
                which_bin = shutil.which('llama-server')
                runtime_bin = Path(which_bin) if which_bin else candidate
            else:
                runtime_bin = candidate
            if sys.platform == 'win32':
                for name in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
                    if not (runtime_bin.parent / name).is_file():
                        raise FileNotFoundError(f'缺少便携 C++ 运行库: {name}，请执行资产准备命令 --prepare')

        if log_path is None:
            from core.offline import user_data_dir
            log_dir = user_data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "llama-server.log"

        self.runtime_bin = Path(runtime_bin).resolve()
        if not self.runtime_bin.is_file():
            raise FileNotFoundError(f'推理运行时不存在: {self.runtime_bin}')
        self.log_path = Path(log_path).resolve()
        self.threads = threads
        self.context_size = context_size
        self.parallel = parallel
        self.n_gpu_layers = n_gpu_layers
        self._explicit_port = port
        self.port = port or find_free_port()
        self.process: Optional[subprocess.Popen] = None
        self.log_file = None
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._stop_lock = threading.Lock()
        self.last_stream_finish_reason: Optional[str] = None

    _PORT_BIND_ERROR_TOKENS = (
        "address already in use",
        "failed to bind",
        "could not bind",
        "couldn't bind",
        "bind failed",
        "failed to listen",
        "wsaeaddrinuse",
        "eaddrinuse",
        "10048",
    )

    def _refresh_port(self, force_free: bool = False) -> None:
        if force_free or self._explicit_port is None:
            self.port = find_free_port()
            self.base_url = f"http://127.0.0.1:{self.port}"

    def _is_port_bind_failure(self, log_offset: int) -> bool:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(log_offset)
                snippet = f.read().lower()
        except OSError:
            return False
        return any(tok in snippet for tok in self._PORT_BIND_ERROR_TOKENS)

    def start(self, timeout_seconds: int = 600, cancelled=None, max_port_retries: int = 3) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        deadline = time.monotonic() + timeout_seconds
        force_free = False
        for attempt in range(max_port_retries + 1):
            self._refresh_port(force_free=force_free)
            if self._start_once(deadline=deadline, timeout_seconds=timeout_seconds, cancelled=cancelled):
                return
            if attempt < max_port_retries:
                logger.warning(
                    "llama-server 端口 %d 绑定失败，正在静默切换空闲端口重试 (%d/%d)",
                    self.port, attempt + 1, max_port_retries,
                )
                force_free = True

        raise RuntimeError(f"llama-server 启动异常退出，详情请查阅: {self.log_path}")

    def _start_once(self, deadline: float, timeout_seconds: int, cancelled=None) -> bool:
        cmd = [
            str(self.runtime_bin),
            "--model", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--threads", str(self.threads),
            "--threads-batch", str(self.threads),
            "--ctx-size", str(self.context_size * self.parallel),
            "--parallel", str(self.parallel),
            "--n-gpu-layers", str(self.n_gpu_layers),
            "--jinja",
        ]

        logger.info(
            "Starting llama-server: %s (ctx-size=%d, parallel=%d, ngl=%d, logging to %s)",
            " ".join(cmd), self.context_size, self.parallel, self.n_gpu_layers, self.log_path,
        )
        try:
            log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        except OSError:
            log_offset = 0
        self.log_file = open(self.log_path, "a", encoding="utf-8")
        proc_env = None
        if sys.platform != 'win32':
            lib_dir = str(self.runtime_bin.parent)
            lib_var = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
            proc_env = os.environ.copy()
            existing = proc_env.get(lib_var, '')
            proc_env[lib_var] = f"{lib_dir}{os.pathsep}{existing}" if existing else lib_dir
        self.process = subprocess.Popen(
            cmd,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=proc_env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        process = self.process

        started = time.monotonic()
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                self.stop()
                raise RuntimeError('启动已取消')
            if process.poll() is not None:
                self.stop()
                if self._is_port_bind_failure(log_offset):
                    return False
                raise RuntimeError(f"llama-server 启动异常退出，详情请查阅: {self.log_path}")
            try:
                req = urllib.request.Request(f"{self.base_url}/health")
                with self.opener.open(req, timeout=1) as resp:
                    if resp.status == 200:
                        logger.info("llama-server ready in %.2fs on port %d", time.monotonic() - started, self.port)
                        return True
            except Exception:
                time.sleep(0.5)

        self.stop()
        raise TimeoutError(f"llama-server 未能在 {timeout_seconds}s 内启动就绪")

    def stop(self) -> None:
        with self._stop_lock:
            self._stop()

    def _stop(self) -> None:
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except Exception:
                    pass
            finally:
                self.process = None

        if self.log_file is not None:
            try:
                self.log_file.close()
            except Exception:
                pass
            finally:
                self.log_file = None

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _build_chat_payload(
        self,
        prompt: str,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        dry_multiplier: Optional[float],
        dry_allowed_length: Optional[int],
        dry_penalty_last_n: Optional[int],
        max_tokens: int,
        seed: int,
        cache_prompt: bool,
        stream: bool,
    ) -> Dict[str, Any]:
        if self.process and self.process.poll() is not None:
            raise RuntimeError(f"llama-server 进程已异常退出，退出码: {self.process.returncode}")

        payload: Dict[str, Any] = {
            "model": self.model_path.stem,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "repeat_penalty": repetition_penalty,
            "max_tokens": max_tokens,
            "seed": seed,
            "stream": stream,
            "cache_prompt": cache_prompt,
        }
        if dry_multiplier is not None:
            payload["dry_multiplier"] = dry_multiplier
        if dry_allowed_length is not None:
            payload["dry_allowed_length"] = dry_allowed_length
        if dry_penalty_last_n is not None:
            payload["dry_penalty_last_n"] = dry_penalty_last_n
        return payload

    def _raise_if_context_http_error(self, exc: urllib.error.HTTPError) -> None:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        low = err_body.lower()
        if exc.code == 400 and any(tok in low for tok in ("context", "n_ctx", "exceed")):
            logger.error("推理请求超出模型上下文长度限制: %s", err_body[:200])
            raise RuntimeError(f"输入超出模型上下文长度限制: {err_body[:200]}") from exc

    def chat_completion(
        self,
        prompt: str,
        temperature: float = 0.7,
        top_p: float = 0.6,
        top_k: int = 20,
        repetition_penalty: float = 1.05,
        dry_multiplier: Optional[float] = None,
        dry_allowed_length: Optional[int] = None,
        dry_penalty_last_n: Optional[int] = None,
        max_tokens: int = 4096,
        seed: int = 42,
        cache_prompt: bool = True,
        timeout: int = 600,
    ) -> Dict[str, Any]:
        """向 llama-server 发送标准 /v1/chat/completions 推理请求。

        开启 cache_prompt=True 复用相同背景信息的 KV Cache 显著降低 prefill 延迟。
        timeout 默认放宽至 600 秒 (10 分钟)，专为弱算力轻薄本纯 CPU 推理与挂机通宵长文献场景设计，
        兼顾 4,000+ 字符超长参考文献与密集公式段落，杜绝因过早超时触发降级。
        """
        payload = self._build_chat_payload(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            repetition_penalty=repetition_penalty,
            dry_multiplier=dry_multiplier,
            dry_allowed_length=dry_allowed_length,
            dry_penalty_last_n=dry_penalty_last_n,
            max_tokens=max_tokens,
            seed=seed,
            cache_prompt=cache_prompt,
            stream=False,
        )
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self._raise_if_context_http_error(e)
            raise
        except (socket.timeout, TimeoutError) as e:
            logger.error("推理单次请求超时 (超过 %d 秒): %s", timeout, e)
            raise TimeoutError(f"llama-server 段落推理超时 (超过 {timeout} 秒)") from e
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                logger.error("推理网络连接超时 (超过 %d 秒): %s", timeout, e)
                raise TimeoutError(f"llama-server 段落推理超时 (超过 {timeout} 秒)") from e
            raise

    def chat_completion_stream(
        self,
        prompt: str,
        temperature: float = 0.3,
        top_p: float = 0.6,
        top_k: int = 20,
        repetition_penalty: float = 1.05,
        dry_multiplier: Optional[float] = None,
        dry_allowed_length: Optional[int] = None,
        dry_penalty_last_n: Optional[int] = None,
        max_tokens: int = 4096,
        seed: int = 42,
        cache_prompt: bool = True,
        timeout: int = 300,
        cancelled=None,
        stream_meta: Optional[Dict[str, Any]] = None,
    ):
        """流式向 llama-server 发送 /v1/chat/completions 请求，逐块产出 delta 文本。"""
        self.last_stream_finish_reason = None
        finish_reason: Optional[str] = None
        payload = self._build_chat_payload(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            repetition_penalty=repetition_penalty,
            dry_multiplier=dry_multiplier,
            dry_allowed_length=dry_allowed_length,
            dry_penalty_last_n=dry_penalty_last_n,
            max_tokens=max_tokens,
            seed=seed,
            cache_prompt=cache_prompt,
            stream=True,
        )
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                for raw_line in resp:
                    if cancelled and cancelled():
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        continue
                    choices = parsed.get("choices") or []
                    if not choices:
                        continue
                    choice0 = choices[0]
                    if choice0.get("finish_reason"):
                        finish_reason = choice0["finish_reason"]
                    delta = (choice0.get("delta") or {}).get("content")
                    if delta:
                        yield delta
        except urllib.error.HTTPError as e:
            self._raise_if_context_http_error(e)
            raise
        except (socket.timeout, TimeoutError) as e:
            logger.error("流式推理请求超时 (超过 %d 秒): %s", timeout, e)
            raise TimeoutError(f"llama-server 推理超时 (超过 {timeout} 秒)") from e
        except urllib.error.URLError as e:
            if "timed out" in str(e).lower():
                logger.error("流式推理网络连接超时 (超过 %d 秒): %s", timeout, e)
                raise TimeoutError(f"llama-server 推理超时 (超过 {timeout} 秒)") from e
            raise
        finally:
            self.last_stream_finish_reason = finish_reason
            if stream_meta is not None:
                stream_meta["finish_reason"] = finish_reason
