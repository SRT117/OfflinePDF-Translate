# SPDX-License-Identifier: AGPL-3.0-only
"""硬件环境与动态资源配置自适应引擎。

智能探测 CPU 核心、真实可用内存余量（Available RAM）、GPU 显存与推理后端能力，
在 Windows / Linux / macOS 上生成兼顾整机响应与推理吞吐的经验默认配置。
"""

import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional

logger = logging.getLogger("offlinepdf-translate.hardware")
_DETECTED_RAW_GPU = None
_DETECTED_GPU = None  # 向后兼容旧测试或外部引用


class HardwareProfile(NamedTuple):
    total_cpu: int
    threads: int
    total_ram_gb: float
    available_ram_gb: float
    has_gpu: bool
    gpu_name: str
    vram_gb: float
    n_gpu_layers: int
    parallel: int
    context_size: int
    device_summary: str


def _no_window_flags() -> int:
    """Windows 下隐藏子进程控制台窗口，防止 pythonw 启动 GUI 时黑框闪烁。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _detect_cpu_count() -> int:
    """获取当前进程可用的 CPU 逻辑线程数（在 Linux 上尊重 cpuset / cgroup 亲和性配额）。"""
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity = os.sched_getaffinity(0)
            if affinity:
                return len(affinity)
        except OSError:
            pass
    return os.cpu_count() or 4


def _detect_darwin_memory_via_sysctl() -> Optional[tuple[float, float]]:
    """macOS 原生内存探测回退：结合 sysctl hw.memsize 与 vm_stat。"""
    try:
        mem_res = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if mem_res.returncode != 0 or not mem_res.stdout.strip():
            return None
        total_bytes = int(mem_res.stdout.strip())
        total_gb = round(total_bytes / (1024**3), 1)

        vm_res = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        if vm_res.returncode != 0 or not vm_res.stdout:
            return total_gb, round(total_gb * 0.5, 1)

        page_match = re.search(r"page size of (\d+) bytes", vm_res.stdout)
        page_size = int(page_match.group(1)) if page_match else 4096
        pages = {}
        for line in vm_res.stdout.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            num_str = v.strip().rstrip(".")
            if num_str.isdigit():
                pages[k.strip()] = int(num_str)

        avail_pages = (
            pages.get("Pages free", 0)
            + pages.get("Pages inactive", 0)
            + pages.get("Pages purgeable", 0)
        )
        avail_gb = round((avail_pages * page_size) / (1024**3), 1)
        return total_gb, min(total_gb, max(0.5, avail_gb))
    except Exception:
        return None


def _detect_memory_gb() -> tuple[float, float]:
    """跨平台精准获取物理总内存与真实可用内存（GB）。

    1. Windows：原生 kernel32.GlobalMemoryStatusEx；
    2. Linux：/proc/meminfo；
    3. macOS / 其他 POSIX：优先 psutil.virtual_memory()，macOS 再回退 sysctl + vm_stat。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024**3), 1), round(stat.ullAvailPhys / (1024**3), 1)
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            mem_info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_kb = float(parts[1].strip().split()[0])
                        mem_info[key] = val_kb
            total_gb = round(mem_info.get("MemTotal", 8 * 1024 * 1024) / (1024**2), 1)
            avail_gb = round(mem_info.get("MemAvailable", mem_info.get("MemFree", 3 * 1024 * 1024)) / (1024**2), 1)
            return total_gb, avail_gb
        except Exception:
            pass

    try:
        import psutil

        vm = psutil.virtual_memory()
        return round(vm.total / (1024**3), 1), round(vm.available / (1024**3), 1)
    except Exception:
        pass

    if sys.platform == "darwin":
        mac_mem = _detect_darwin_memory_via_sysctl()
        if mac_mem is not None:
            return mac_mem

    return 8.0, 3.0


def _detect_nvidia_smi_gpu() -> Optional[tuple[bool, str, float, str]]:
    """通过 nvidia-smi 探测所有 NVIDIA GPU 并选择显存最大的一张。"""
    smi_candidates = [shutil.which("nvidia-smi")]
    if sys.platform == "win32":
        smi_candidates.extend([
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ])
    else:
        smi_candidates.extend(["/usr/bin/nvidia-smi", "/usr/local/nvidia/bin/nvidia-smi"])

    for smi in smi_candidates:
        if not smi or not os.path.isfile(smi):
            continue
        try:
            res = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=_no_window_flags(),
            )
            if res.returncode != 0 or not res.stdout.strip():
                continue
            best: Optional[tuple[bool, str, float, str]] = None
            for line in res.stdout.strip().splitlines():
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                gpu_name = parts[0].strip()
                try:
                    vram_gb = round(float(parts[1].strip()) / 1024, 1)
                except ValueError:
                    continue
                if best is None or vram_gb > best[2]:
                    best = (True, gpu_name, vram_gb, "nvidia")
            if best is not None:
                return best
        except Exception:
            pass
    return None


def _detect_linux_drm_gpu() -> Optional[tuple[bool, str, float, str]]:
    """读取 Linux /sys/class/drm 获取非 NVIDIA 显卡与显存（无需外部命令）。"""
    drm_root = Path("/sys/class/drm")
    if not drm_root.is_dir():
        return None
    for card in sorted(drm_root.glob("card[0-9]*")):
        vendor_file = card / "device" / "vendor"
        if not vendor_file.is_file():
            continue
        try:
            vendor = vendor_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if vendor == "0x1002":  # AMD
            vram_file = card / "device" / "mem_info_vram_total"
            vram_gb = 0.0
            if vram_file.is_file():
                try:
                    vram_gb = round(int(vram_file.read_text(encoding="utf-8").strip()) / (1024**3), 1)
                except (OSError, ValueError):
                    pass
            return (vram_gb >= 2.0, "AMD Radeon GPU", vram_gb, "amd")
        if vendor == "0x8086":  # Intel
            return (False, "Intel Graphics (核显，CPU 推理)", 0.0, "intel")
    return None


def _detect_raw_gpu(force_refresh: bool = False) -> tuple[bool, str, float, str]:
    """跨平台探测物理 GPU 硬件与显存容量，返回 (has_hw_gpu, gpu_name, vram_gb, vendor)。"""
    global _DETECTED_RAW_GPU
    if _DETECTED_RAW_GPU is not None and not force_refresh:
        return _DETECTED_RAW_GPU

    # 1. NVIDIA 独立显卡（Windows / Linux）
    nv = _detect_nvidia_smi_gpu()
    if nv is not None:
        _DETECTED_RAW_GPU = nv
        return _DETECTED_RAW_GPU

    # 2. macOS Apple Silicon (Metal 统一内存)
    if sys.platform == "darwin":
        arch = platform.machine().lower()
        if arch in ("arm64", "aarch64"):
            total_ram_gb, avail_ram_gb = _detect_memory_gb()
            brand = "Apple Silicon"
            try:
                res = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if res.returncode == 0 and res.stdout.strip():
                    brand = res.stdout.strip()
            except Exception:
                pass
            metal_budget = round(max(0.0, min(avail_ram_gb, total_ram_gb * 0.75) - 1.0), 1)
            _DETECTED_RAW_GPU = (True, f"{brand} (Metal 统一内存)", metal_budget, "apple_metal")
            return _DETECTED_RAW_GPU

    # 3. Windows 平台探测其他独显或核显
    if sys.platform == "win32":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -Property Name, AdapterRAM | ConvertTo-Json",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=_no_window_flags(),
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    name = (item.get("Name") or "").strip()
                    low = name.lower()
                    if not name or any(v in low for v in ("oray", "rdp", "virtual", "remote", "basic display", "vga")):
                        continue
                    is_discrete_hint = any(k in low for k in ("radeon rx", "radeon pro", "rx ", "arc", "firepro", "instinct"))
                    is_integrated = (not is_discrete_hint) and any(
                        b in low for b in ("intel", "amd", "radeon", "uhd", "iris", "vega")
                    )
                    if is_discrete_hint:
                        raw_ram = item.get("AdapterRAM") or 0
                        vram_gb = round(float(raw_ram) / (1024**3), 1) if isinstance(raw_ram, (int, float)) and raw_ram > 0 else 0.0
                        vendor = "amd" if ("amd" in low or "radeon" in low) else "intel"
                        _DETECTED_RAW_GPU = (True, name, vram_gb, vendor)
                        return _DETECTED_RAW_GPU
                    summary = f"{name} (核显，CPU 推理)" if is_integrated else name
                    _DETECTED_RAW_GPU = (False, summary, 0.0, "integrated")
                    return _DETECTED_RAW_GPU
        except Exception:
            pass

    # 4. Linux 非 NVIDIA GPU
    if sys.platform.startswith("linux"):
        drm_gpu = _detect_linux_drm_gpu()
        if drm_gpu is not None:
            _DETECTED_RAW_GPU = drm_gpu
            return _DETECTED_RAW_GPU

    _DETECTED_RAW_GPU = (False, "None", 0.0, "none")
    return _DETECTED_RAW_GPU


def _detect_gpu(force_refresh: bool = False) -> tuple[bool, str, float]:
    """探测物理 GPU 与显存（保持三元组接口兼容）。"""
    global _DETECTED_GPU
    if _DETECTED_GPU is not None and not force_refresh:
        return _DETECTED_GPU
    has_gpu, name, vram_gb, _vendor = _detect_raw_gpu(force_refresh=force_refresh)
    _DETECTED_GPU = (has_gpu, name, vram_gb)
    return _DETECTED_GPU


def detect_runtime_gpu_backends(runtime_bin: Optional[Path] = None) -> Optional[set[str]]:
    """检查当前 llama-server 运行时实际包含的 GPU 加速后端。

    返回支持的后端集合（如 {'cuda'} / {'metal'} / set() 表示纯 CPU）；
    若运行时目录不存在（例如源码态纯测试环境），返回 None 表示未知。
    """
    if runtime_bin is not None:
        rt_path = Path(runtime_bin).resolve()
        runtime_dir = rt_path.parent
    else:
        runtime_dir = Path(__file__).resolve().parents[1] / "runtime" / "llama.cpp"
        bin_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
        rt_path = runtime_dir / bin_name

    if not runtime_dir.is_dir():
        return None

    names = [p.name.lower() for p in runtime_dir.iterdir() if p.is_file()]
    backends: set[str] = set()
    for fname in names:
        if "ggml-cuda" in fname:
            backends.add("cuda")
        elif "ggml-vulkan" in fname:
            backends.add("vulkan")
        elif "ggml-hip" in fname or "ggml-rocm" in fname:
            backends.add("hip")
        elif "ggml-sycl" in fname:
            backends.add("sycl")
        elif "ggml-metal" in fname:
            backends.add("metal")

    has_modular_cpu = any("ggml-cpu" in fname for fname in names)
    if backends or has_modular_cpu:
        return backends

    # 非模块化单一可执行文件（如 macOS Homebrew 或静态编译 llama-server）：探测 --list-devices
    if rt_path.is_file():
        try:
            res = subprocess.run(
                [str(rt_path), "--list-devices"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=_no_window_flags(),
            )
            out = (res.stdout or "") + "\n" + (res.stderr or "")
            low = out.lower()
            if "cuda" in low:
                backends.add("cuda")
            if "vulkan" in low:
                backends.add("vulkan")
            if "metal" in low:
                backends.add("metal")
            if "rocm" in low or "hip" in low:
                backends.add("hip")
            if "sycl" in low:
                backends.add("sycl")
            return backends
        except Exception:
            pass

    if sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64"):
        return {"metal"}
    return set()


def _resolve_model_size_gb(model_path: Optional[Path] = None) -> float:
    """估算待加载 GGUF 模型体积（GB），无法定位时采用保守经验值 1.5GB。"""
    try:
        target = Path(model_path) if model_path else None
        if target is None or not target.is_file():
            from core.offline import resolve_translation_model

            target = resolve_translation_model()
        if target.is_file():
            return round(target.stat().st_size / (1024**3), 2)
    except Exception:
        pass
    return 1.5


def detect_hardware_profile(
    override_threads: Optional[int] = None,
    override_parallel: Optional[int] = None,
    override_gpu_layers: Optional[int] = None,
    override_ctx_size: Optional[int] = None,
    runtime_bin: Optional[Path] = None,
    model_path: Optional[Path] = None,
) -> HardwareProfile:
    # 1. CPU 核心探测：尊重亲和性/容器配额，默认采用“线程数 - 2”兼顾桌面流畅度
    total_cpu = _detect_cpu_count()
    if override_threads is not None:
        threads = max(1, override_threads)
    else:
        threads = max(1, total_cpu - 2) if total_cpu > 2 else max(1, total_cpu)

    # 2. 真实可用内存余量探测
    total_ram_gb, available_ram_gb = _detect_memory_gb()
    model_size_gb = _resolve_model_size_gb(model_path)

    # 3. 物理 GPU 探测 + 运行时后端可用性交叉校验
    has_hw_gpu, hw_gpu_name, vram_gb, vendor = _detect_raw_gpu()
    if not has_hw_gpu and _DETECTED_GPU is not None:
        # 兼容外部测试直接 mock _detect_gpu() 的场景
        mocked_has, mocked_name, mocked_vram = _detect_gpu()
        if mocked_has:
            has_hw_gpu, hw_gpu_name, vram_gb, vendor = mocked_has, mocked_name, mocked_vram, "nvidia"

    backends = detect_runtime_gpu_backends(runtime_bin)
    if has_hw_gpu and backends is not None:
        vendor_backend_map = {
            "nvidia": {"cuda", "vulkan"},
            "apple_metal": {"metal"},
            "amd": {"hip", "vulkan"},
            "intel": {"sycl", "vulkan"},
        }
        required_backends = vendor_backend_map.get(vendor, {"cuda", "vulkan", "hip", "sycl", "metal"})
        backend_ready = bool(backends & required_backends)
    else:
        backend_ready = has_hw_gpu

    has_gpu = bool(has_hw_gpu and backend_ready)
    if has_hw_gpu and not backend_ready:
        gpu_name = f"{hw_gpu_name} (当前运行时无对应 GPU 后端，CPU 推理)"
        effective_vram_gb = 0.0
    else:
        gpu_name = hw_gpu_name
        effective_vram_gb = vram_gb

    # 4. 联合约束推算 GPU 层数、上下文窗口与并发 slot 数
    if override_gpu_layers is not None:
        n_gpu_layers = max(0, override_gpu_layers)
    elif has_gpu and (effective_vram_gb >= 2.5 or vendor == "apple_metal" and available_ram_gb >= 6.0):
        n_gpu_layers = 99
    else:
        n_gpu_layers = 0

    cpu_headroom_gb = available_ram_gb - model_size_gb
    using_gpu_offload = has_gpu and n_gpu_layers > 0

    if override_ctx_size is not None:
        context_size = override_ctx_size
    elif using_gpu_offload:
        if (effective_vram_gb >= 6.0 or vendor == "apple_metal" and available_ram_gb >= 8.0) and available_ram_gb >= 4.5:
            context_size = 8192
        else:
            context_size = 4096
    elif available_ram_gb < 4.5 or cpu_headroom_gb < 2.0:
        context_size = 4096
    else:
        context_size = 8192

    if override_parallel is not None:
        parallel = max(1, override_parallel)
    elif using_gpu_offload:
        if effective_vram_gb >= 12.0 and available_ram_gb >= 8.0:
            parallel = 4
        elif effective_vram_gb >= 6.0 and available_ram_gb >= 4.5:
            parallel = 2
        else:
            parallel = 1
    else:
        if available_ram_gb >= 24.0 and cpu_headroom_gb >= 16.0 and total_cpu >= 16:
            parallel = 4
        elif available_ram_gb >= 12.0 and cpu_headroom_gb >= 6.0 and total_cpu >= 6:
            parallel = 2
        else:
            parallel = 1

    device_summary = (
        f"CPU: {threads}/{total_cpu} 线程 | 可用内存: {available_ram_gb}GB/{total_ram_gb}GB | "
        f"GPU: {gpu_name} (显存 {vram_gb}GB, 层数 {n_gpu_layers}) | "
        f"槽位: parallel={parallel}, 窗口: {context_size}"
    )

    return HardwareProfile(
        total_cpu=total_cpu,
        threads=threads,
        total_ram_gb=total_ram_gb,
        available_ram_gb=available_ram_gb,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        n_gpu_layers=n_gpu_layers,
        parallel=parallel,
        context_size=context_size,
        device_summary=device_summary,
    )
