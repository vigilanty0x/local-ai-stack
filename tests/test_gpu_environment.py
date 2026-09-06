"""Synthetic OS/process seams; no installed GPU or private environment required."""
import ctypes
import io
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from local_ai_stack import resources as res


def completed_process():
    return SimpleNamespace(stdout=io.BytesIO(b"Synthetic GPU, 8192, 2048, 0\n"),
                           stderr=io.BytesIO(), returncode=0,
                           poll=lambda: 0, wait=lambda **kw: 0)


def test_gpu_child_requires_os_program_files_and_filters_inherited_apps(monkeypatch, tmp_path):
    executable = tmp_path / "nvidia-smi.exe"
    executable.write_bytes(b"synthetic identity, never executed")
    inherited = {"SystemRoot":r"C:\Windows", "WINDIR":r"C:\Windows",
                 "ProgramFiles":r"Z:\untrusted", "ProgramW6432":r"Z:\untrusted",
                 "PATH":r"Z:\untrusted", "APP_SETTING":"synthetic-private"}
    monkeypatch.setattr(res, "os", SimpleNamespace(name="nt", environ=inherited))
    # raising=False lets this public counterexample exercise the old collector.
    monkeypatch.setattr(res, "_windows_program_files", lambda: r"C:\Program Files", raising=False)
    calls = []
    def process(argv, **kwargs):
        calls.append((argv,kwargs))
        assert kwargs["env"] == {"LC_ALL":"C", "LANG":"C", "SystemRoot":r"C:\Windows",
                                 "WINDIR":r"C:\Windows", "ProgramFiles":r"C:\Program Files"}
        assert argv == [str(executable), *res.GPU_ARGS]
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        return completed_process()
    monkeypatch.setattr(res.subprocess, "Popen", process)
    result = res.gpu_process(executable, time.monotonic() + 5)
    assert len(calls) == 1 and result["status"] == "measured"
    assert result["values"][0]["total_bytes"] == 8192 * 1048576
    assert result["process"]["process_termination_confirmed"] is True


def test_gpu_posix_environment_unchanged(monkeypatch, tmp_path):
    executable = tmp_path / "nvidia-smi"
    executable.write_bytes(b"synthetic")
    monkeypatch.setattr(res, "os", SimpleNamespace(name="posix", environ={"PATH":"synthetic"}))
    monkeypatch.setattr(res, "_windows_program_files", lambda: pytest.fail("not Windows"))
    def process(argv, **kwargs):
        assert kwargs["env"] == {"LC_ALL":"C", "LANG":"C"}
        return completed_process()
    monkeypatch.setattr(res.subprocess, "Popen", process)
    assert res.gpu_process(executable, time.monotonic()+5)["status"] == "measured"


@pytest.mark.parametrize("failure", [OSError("synthetic API error"), ValueError("synthetic path invalid")])
def test_program_files_failure_stops_before_process(monkeypatch, tmp_path, failure):
    executable = tmp_path / "nvidia-smi.exe"; executable.write_bytes(b"synthetic")
    monkeypatch.setattr(res, "os", SimpleNamespace(name="nt", environ={"ProgramFiles":"ignored"}))
    def failed(): raise failure
    monkeypatch.setattr(res, "_windows_program_files", failed)
    monkeypatch.setattr(res.subprocess, "Popen", lambda *a,**kw: pytest.fail("must not launch"))
    with pytest.raises(type(failure)): res.gpu_process(executable, time.monotonic()+5)


def test_slow_known_folder_resolution_does_not_launch_past_budget(monkeypatch, tmp_path):
    executable = tmp_path / "nvidia-smi.exe"; executable.write_bytes(b"synthetic")
    monkeypatch.setattr(res, "os", SimpleNamespace(name="nt", environ={}))
    now = [100.0]
    monkeypatch.setattr(res.time, "monotonic", lambda: now[0])
    def slow(): now[0] = 103.0; return r"C:\Program Files"
    monkeypatch.setattr(res, "_windows_program_files", slow)
    monkeypatch.setattr(res.subprocess, "Popen", lambda *a,**kw: pytest.fail("must not launch"))
    with pytest.raises(TimeoutError): res.gpu_process(executable, 105.0)


def ffi_fixture(monkeypatch, text=r"C:\Program Files", status=0, null=False):
    buffer = ctypes.create_unicode_buffer(text)
    address = None if null else ctypes.addressof(buffer)
    calls, freed = [], []
    def query(folder, flags, token, out):
        calls.append((bytes(ctypes.cast(folder, ctypes.POINTER(res._Guid)).contents), flags, token))
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = address
        return status
    def free(pointer): freed.append(pointer.value)
    query.argtypes = query.restype = free.argtypes = free.restype = None
    def dll(name, **kwargs):
        assert kwargs == {"use_last_error":True, "winmode":0x800}
        if name == "shell32.dll": return SimpleNamespace(SHGetKnownFolderPath=query)
        assert name == "ole32.dll"
        return SimpleNamespace(CoTaskMemFree=free)
    monkeypatch.setattr(res.ctypes, "WinDLL", dll, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda *a,**kw: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0))
    return calls, freed, address, query, free, buffer


def test_known_folder_exact_ffi_guid_and_free(monkeypatch):
    calls, freed, address, query, free, buffer = ffi_fixture(monkeypatch)
    assert res._windows_program_files() == str(Path(r"C:\Program Files"))
    assert calls == [(bytes.fromhex("b6635e90bfc14e49b29c65b732d3d21a"),0x4400,None)]
    assert ctypes.sizeof(res._Guid) == 16
    assert query.argtypes == [ctypes.POINTER(res._Guid),ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
    assert query.restype is ctypes.c_int32
    assert free.argtypes == [ctypes.c_void_p] and free.restype is None
    assert freed == [address]


@pytest.mark.parametrize("status,null", [(-2147467259,False),(-2147467259,True),(0,True)])
def test_known_folder_api_failures_still_free_buffer(monkeypatch,status,null):
    calls, freed, address, *_ = ffi_fixture(monkeypatch,status=status,null=null)
    with pytest.raises(OSError,match="API failed"): res._windows_program_files()
    assert len(calls) == 1 and freed == [address]


@pytest.mark.parametrize("value", ["", "relative", r"C:relative", r"\\server\share\folder",
    r"\\?\C:\Program Files", r"C:\parent\..\other", "C:\\bad\nname", "C:\\"+"a"*32768],
    ids=["empty","relative","drive-relative","network","device","parent","control","overlong"])
def test_invalid_program_files_path_never_probes_filesystem_and_frees(monkeypatch,value):
    calls, freed, address, *_ = ffi_fixture(monkeypatch,text=value)
    monkeypatch.setattr(Path,"lstat",lambda *a,**kw:pytest.fail("invalid path must not stat"))
    with pytest.raises(ValueError): res._windows_program_files()
    assert freed == [address]


@pytest.mark.parametrize("kind", ["link", "reparse", "file", "missing", "denied"])
def test_program_files_redirect_missing_or_unreadable_refused_and_freed(monkeypatch,kind):
    calls, freed, address, *_ = ffi_fixture(monkeypatch)
    def metadata(path,*a,**kw):
        if kind == "missing": raise FileNotFoundError("synthetic")
        if kind == "denied": raise PermissionError("synthetic")
        return SimpleNamespace(st_mode=stat.S_IFLNK if kind=="link" else stat.S_IFREG if kind=="file" else stat.S_IFDIR,
                               st_file_attributes=0x400 if kind=="reparse" else 0)
    monkeypatch.setattr(Path,"lstat",metadata)
    with pytest.raises((ValueError,OSError)): res._windows_program_files()
    assert freed == [address]
