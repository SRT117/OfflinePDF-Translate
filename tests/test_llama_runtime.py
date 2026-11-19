import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import llama_runtime


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeProcess:
    def __init__(self, alive: bool, returncode: int = 1):
        self._alive = alive
        self.returncode = None if alive else returncode

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self._alive = False


class LlamaRuntimePortRetryTests(unittest.TestCase):
    def _make_runtime(self, td: str, port=None):
        root = Path(td)
        model = root / "model.gguf"
        model.write_bytes(b"GGUF")
        bin_path = root / "llama-server.exe"
        bin_path.write_bytes(b"")
        log_path = root / "llama-server.log"
        return llama_runtime.LlamaServerRuntime(
            model_path=model,
            runtime_bin=bin_path,
            port=port,
            log_path=log_path,
        )

    def test_silent_retry_on_port_bind_failure(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(llama_runtime, "find_free_port", side_effect=[18001, 18002, 18003]):
                rt = self._make_runtime(td)
                self.assertEqual(rt.port, 18001)

                attempts = []

                def fake_popen(cmd, stdout=None, **kwargs):
                    port_idx = cmd.index("--port") + 1
                    chosen_port = int(cmd[port_idx])
                    attempts.append(chosen_port)
                    if len(attempts) == 1:
                        stdout.write("error: failed to bind to 127.0.0.1:18002 (Address already in use)\n")
                        stdout.flush()
                        return _FakeProcess(alive=False, returncode=1)
                    return _FakeProcess(alive=True)

                try:
                    with patch.object(llama_runtime.subprocess, "Popen", side_effect=fake_popen), \
                         patch.object(rt.opener, "open", return_value=_FakeResponse()):
                        rt.start(timeout_seconds=5)

                    self.assertEqual(attempts, [18002, 18003])
                    self.assertEqual(rt.port, 18003)
                    self.assertEqual(rt.base_url, "http://127.0.0.1:18003")
                finally:
                    rt.stop()

    def test_explicit_port_also_falls_back_silently_when_occupied(self):
        with tempfile.TemporaryDirectory() as td:
            rt = self._make_runtime(td, port=19000)
            self.assertEqual(rt.port, 19000)
            attempts = []

            def fake_popen(cmd, stdout=None, **kwargs):
                port_idx = cmd.index("--port") + 1
                chosen_port = int(cmd[port_idx])
                attempts.append(chosen_port)
                if len(attempts) == 1:
                    stdout.write("HTTP server could not bind to port 19000: WSAEADDRINUSE (10048)\n")
                    stdout.flush()
                    return _FakeProcess(alive=False, returncode=1)
                return _FakeProcess(alive=True)

            try:
                with patch.object(llama_runtime, "find_free_port", return_value=19001), \
                     patch.object(llama_runtime.subprocess, "Popen", side_effect=fake_popen), \
                     patch.object(rt.opener, "open", return_value=_FakeResponse()):
                    rt.start(timeout_seconds=5)

                self.assertEqual(attempts, [19000, 19001])
                self.assertEqual(rt.port, 19001)
            finally:
                rt.stop()

    def test_non_port_error_fails_immediately_without_retry(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(llama_runtime, "find_free_port", side_effect=[18101, 18102]):
                rt = self._make_runtime(td)
                attempts = []

                def fake_popen(cmd, stdout=None, **kwargs):
                    attempts.append(cmd)
                    stdout.write("error: failed to load model: invalid GGUF magic\n")
                    stdout.flush()
                    return _FakeProcess(alive=False, returncode=1)

                with patch.object(llama_runtime.subprocess, "Popen", side_effect=fake_popen):
                    with self.assertRaises(RuntimeError):
                        rt.start(timeout_seconds=5)

                self.assertEqual(len(attempts), 1)

    def test_exhausted_port_retries_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(llama_runtime, "find_free_port", side_effect=[18200, 18201, 18202]):
                rt = self._make_runtime(td)

                def fake_popen(cmd, stdout=None, **kwargs):
                    stdout.write("failed to listen on 127.0.0.1\n")
                    stdout.flush()
                    return _FakeProcess(alive=False, returncode=1)

                with patch.object(llama_runtime.subprocess, "Popen", side_effect=fake_popen):
                    with self.assertRaises(RuntimeError):
                        rt.start(timeout_seconds=5, max_port_retries=1)


if __name__ == "__main__":
    unittest.main()
