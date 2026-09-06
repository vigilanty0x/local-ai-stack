"""Read-only bounded OS resource observations, never a model attribution."""
import csv
import io
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import subprocess
import threading
import time

from .runtime import RuntimeErrorDetail, remaining

GPU_ARGS = ("--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits")
GPU_BYTES = 65536


def _proc(name):
    if name not in {"stat", "meminfo"}:
        raise RuntimeErrorDetail("unsupported proc observation")
    descriptor = os.open("/proc/" + name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeErrorDetail("proc observation is not regular")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.readline(4097) if name == "stat" else source.read(65537)
        if len(data) > (4096 if name == "stat" else 65536):
            raise RuntimeErrorDetail("proc observation exceeds limit")
        return data.decode("ascii")
    finally:
        os.close(descriptor)


def linux_cpu(text):
    parts = text.strip().split()
    if len(parts) != 11 or parts[0] != "cpu":
        raise RuntimeErrorDetail("aggregate CPU counters missing")
    if any(not p.isascii() or not p.isdigit() or len(p) > 20 for p in parts[1:]):
        raise RuntimeErrorDetail("CPU counters invalid")
    values = tuple(int(p) for p in parts[1:9])
    if any(p > 2**64 - 1 for p in values):
        raise RuntimeErrorDetail("CPU counter overflow")
    return values


def linux_memory(text):
    found = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in {"MemTotal:", "MemAvailable:"}:
            continue
        if (fields[0] in found or len(fields) != 3 or fields[2] != "kB"
                or not fields[1].isascii() or not fields[1].isdigit() or len(fields[1]) > 16):
            raise RuntimeErrorDetail("memory counter invalid")
        found[fields[0]] = int(fields[1]) * 1024
    total, available = found.get("MemTotal:"), found.get("MemAvailable:")
    if total is None or available is None:
        raise RuntimeErrorDetail("memory availability not measured")
    return memory_values(total, available)


def memory_values(total, available):
    if type(total) is not int or type(available) is not int or not 0 <= available <= total <= 2**63 - 1 or total == 0:
        raise RuntimeErrorDetail("memory counters inconsistent")
    return {"total_bytes": total, "available_bytes": available,
            "unavailable_bytes": total - available}




_WINDOWS_TYPES_LOADED = False
_WINDOWS_TYPES_LOCK = threading.Lock()


def _load_windows_types():
    """Load Windows FFI only for an explicit Windows observation/type access."""
    global _WINDOWS_TYPES_LOADED
    with _WINDOWS_TYPES_LOCK:
        if _WINDOWS_TYPES_LOADED:
            return
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                        ("total_phys", ctypes.c_uint64), ("available_phys", ctypes.c_uint64),
                        ("total_page", ctypes.c_uint64), ("available_page", ctypes.c_uint64),
                        ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                        ("available_extended", ctypes.c_uint64)]

        class _Guid(ctypes.Structure):
            _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                        ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]
        globals().update(ctypes=ctypes, FileTime=FileTime, MemoryStatus=MemoryStatus, _Guid=_Guid)
        _WINDOWS_TYPES_LOADED = True


def __getattr__(name):
    # Preserve historical type attributes without importing FFI for Linux calls.
    if name in {"ctypes", "FileTime", "MemoryStatus", "_Guid"}:
        _load_windows_types()
        return globals()[name]
    raise AttributeError(name)


def _kernel():
    _load_windows_types()
    # Restricted system-library search; no caller-selected DLL or PATH lookup.
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
    kernel.GetSystemTimes.argtypes = [ctypes.POINTER(FileTime)] * 3
    kernel.GetSystemTimes.restype = ctypes.c_int
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = ctypes.c_int
    return kernel


def windows_cpu():
    _load_windows_types()
    idle, kernel, user = FileTime(), FileTime(), FileTime()
    if not _kernel().GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("CPU API failed")
    return tuple((v.high << 32) + v.low for v in (idle, kernel, user))


def windows_memory():
    _load_windows_types()
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not _kernel().GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError("memory API failed")
    return memory_values(memory.total_phys, memory.available_phys)


def cpu_delta(first, second, platform):
    count = 3 if platform == "win32" else 8
    if len(first) != count or len(second) != count:
        raise RuntimeErrorDetail("CPU samples incomparable")
    if any(type(v) is not int or not 0 <= v <= 2**64 - 1 for v in (*first, *second)):
        raise RuntimeErrorDetail("CPU counter invalid")
    deltas = [b - a for a, b in zip(first, second)]
    if any(v < 0 for v in deltas):
        raise RuntimeErrorDetail("CPU counter regressed")
    if platform == "win32":
        idle, kernel, user = deltas
        if idle > kernel:
            raise RuntimeErrorDetail("CPU idle exceeds kernel")
        total = kernel + user
    else:
        idle, total = deltas[3] + deltas[4], sum(deltas)
    if total <= 0 or idle > total:
        raise RuntimeErrorDetail("CPU interval has no usable ticks")
    return dict(busy_percent=100.0 * (total - idle) / total, total_ticks=total,
                idle_ticks=idle)


def sample_cpu(platform, sample_ms, deadline):
    reader = windows_cpu if platform == "win32" else lambda: linux_cpu(_proc("stat"))
    remaining(deadline)
    # Measure short sample intervals with the high-resolution monotonic
    # counter; remaining() continues to use the shared deadline clock.
    first_start = time.perf_counter()
    first = reader()
    remaining(deadline)
    if remaining(deadline) < sample_ms / 1000:
        raise TimeoutError("insufficient CPU sample budget")
    time.sleep(sample_ms / 1000)
    remaining(deadline)
    second_start = time.perf_counter()
    second = reader()
    remaining(deadline)
    interval = second_start - first_start
    if interval <= 0:
        raise RuntimeErrorDetail("CPU interval invalid")
    return dict(status="measured", values=dict(cpu_delta(first, second, platform),
                interval_s=interval, requested_interval_ms=sample_ms,
                model_attribution="not_measured", container_quota="not_measured"))


def _executable(path):
    target = Path(path)
    if not target.is_absolute():
        raise RuntimeErrorDetail("absolute trusted GPU executable required")
    for item in (target, *target.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeErrorDetail("GPU executable path must not redirect")
    info = target.stat()
    if not stat.S_ISREG(info.st_mode) or (os.name == "nt" and target.suffix.lower() != ".exe"):
        raise RuntimeErrorDetail("native GPU executable required")
    return target, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)



def _program_files_path(value):
    # Refuse network/device paths before any filesystem access, then redirects.
    path = PureWindowsPath(value)
    if (not value or len(value) > 32767 or any(ord(c) < 32 for c in value)
            or not path.is_absolute() or len(path.drive) != 2
            or not path.drive[0].isascii() or not path.drive[0].isalpha()
            or path.drive[1] != ":" or ".." in path.parts):
        raise RuntimeErrorDetail("Windows Program Files path invalid")
    target = Path(value)
    for item in (target, *target.parents):
        info = item.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeErrorDetail("Windows Program Files path must not redirect")
    return str(target)


def _windows_program_files():
    _load_windows_types()
    # FOLDERID_ProgramFiles, Microsoft Knownfolders.h. Never inherit an app path.
    folder = _Guid(0x905e63b6, 0xc1bf, 0x494e,
                   (ctypes.c_ubyte * 8)(0xb2, 0x9c, 0x65, 0xb7, 0x32, 0xd3, 0xd2, 0x1a))
    shell = ctypes.WinDLL("shell32.dll", use_last_error=True, winmode=0x800)
    ole = ctypes.WinDLL("ole32.dll", use_last_error=True, winmode=0x800)
    query = shell.SHGetKnownFolderPath
    query.argtypes = [ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p)]
    query.restype = ctypes.c_int32
    free = ole.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    pointer = ctypes.c_void_p()
    try:
        # DEFAULT_PATH | DONT_VERIFY: no creation/initialization or remote stat.
        # Validate the returned local directory ourselves before using it.
        if query(ctypes.byref(folder), 0x4400, None, ctypes.byref(pointer)) != 0 or not pointer.value:
            raise OSError("Windows Program Files API failed")
        return _program_files_path(ctypes.wstring_at(pointer))
    finally:
        free(pointer)  # Required even when the API fails; NULL is supported.


def gpu_process(path, deadline):
    target, identity = _executable(path)
    # Reserve cleanup time inside the caller's total budget.
    allowance = min(2.0, remaining(deadline) - 0.5)
    if allowance <= 0:
        raise TimeoutError("insufficient GPU observation budget")
    stop = time.monotonic() + allowance
    environment = {"LC_ALL": "C", "LANG": "C"}
    if os.name == "nt":
        for key in ("SystemRoot", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
        environment["ProgramFiles"] = _windows_program_files()
        if time.monotonic() >= stop:
            raise TimeoutError("insufficient GPU observation budget")
    process = subprocess.Popen([str(target), *GPU_ARGS], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, bufsize=0,
        close_fds=True, env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    chunks = [[], []]
    sizes = [0, 0]
    finished = [False, False]
    fault = threading.Event()
    mutex = threading.Lock()
    def read(index, stream):
        try:
            while True:
                part = stream.read(4096)
                if not part:
                    finished[index] = True
                    return
                with mutex:
                    if sum(sizes) + len(part) > GPU_BYTES:
                        fault.set()
                        return
                    sizes[index] += len(part)
                    chunks[index].append(part)
        except (OSError, ValueError):
            fault.set()
    threads = [threading.Thread(target=read, args=(i, stream), daemon=True,
               name="local-ai-gpu-output") for i, stream in enumerate((process.stdout, process.stderr))]
    started_threads = []
    reason = None
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)
        while process.poll() is None or not all(finished):
            if fault.is_set():
                reason = "GPU_OUTPUT_LIMIT_OR_READ_ERROR"
                break
            if time.monotonic() >= stop:
                reason = "GPU_TIMEOUT"
                break
            time.sleep(min(.01, max(0, stop - time.monotonic())))
    except Exception:
        reason = "GPU_OBSERVATION_FAILED"
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                reason = "GPU_TERMINATION_UNCONFIRMED"
        try:
            process.wait(timeout=max(.001, min(.5, deadline - time.monotonic())))
        except (subprocess.TimeoutExpired, OSError):
            reason = "GPU_TERMINATION_UNCONFIRMED"
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                reason = reason or "GPU_STREAM_CLOSE_FAILED"
        for thread in started_threads:
            thread.join(timeout=max(0, min(.1, deadline - time.monotonic())))
    confirmed = process.poll() is not None and not any(t.is_alive() for t in started_threads)
    if not confirmed:
        reason = "GPU_TERMINATION_UNCONFIRMED"
    try:
        if _executable(path)[1] != identity:
            reason = "GPU_EXECUTABLE_CHANGED"
    except (OSError, ValueError):
        reason = "GPU_EXECUTABLE_CHANGED"
    evidence = dict(process_exit=process.returncode, process_termination_confirmed=confirmed,
                    captured_bytes=sum(sizes), output_limit_bytes=GPU_BYTES)
    if reason or process.returncode != 0:
        return dict(status="timeout" if reason == "GPU_TIMEOUT" else "error", values=None,
                    error_code=reason or "GPU_PROCESS_NONZERO", process=evidence)
    try:
        values = parse_gpu(b"".join(chunks[0]).decode("utf-8"))
    except (UnicodeError, ValueError, csv.Error):
        return dict(status="error", values=None, error_code="GPU_OUTPUT_INVALID", process=evidence)
    return dict(status="measured" if all(v["complete"] for v in values) else "partial",
                values=values, error_code=None, process=evidence)


def parse_gpu(text):
    rows = list(csv.reader(io.StringIO(text), strict=True))
    if not 1 <= len(rows) <= 16:
        raise RuntimeErrorDetail("GPU row count invalid")
    result = []
    def number(raw, maximum):
        raw = raw.strip()
        if raw in {"N/A", "[N/A]", "[Not Supported]"}:
            return None
        if not raw or len(raw) > 32:
            raise RuntimeErrorDetail("GPU number invalid")
        value = float(raw)
        if not math.isfinite(value) or not 0 <= value <= maximum:
            raise RuntimeErrorDetail("GPU number invalid")
        return value
    for index, row in enumerate(rows):
        if len(row) != 4:
            raise RuntimeErrorDetail("GPU columns invalid")
        name = row[0].strip()
        if not name or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise RuntimeErrorDetail("GPU name invalid")
        total, used, utilization = number(row[1], 2**40), number(row[2], 2**40), number(row[3], 100)
        if total is not None and (total == 0 or (used is not None and used > total)):
            raise RuntimeErrorDetail("GPU memory counters inconsistent")
        result.append(dict(index=index, name=name, total_bytes=None if total is None else int(total * 1048576),
            used_bytes=None if used is None else int(used * 1048576), utilization_percent=utilization,
            complete=all(v is not None for v in (total, used, utilization))))
    return result
