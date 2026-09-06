import ctypes
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from local_ai_stack import observations as ob, resources as res, runtime as rt

BASE = "http://localhost:11434"
MODEL = "first:1"


def fixture_transport(calls, *, ps=None):
    def request(base, method, path, payload, deadline):
        calls.append(path)
        if path == "/api/version":
            return {"version": "synthetic"}
        if path == "/api/tags":
            return {"models": [{"name": MODEL}, {"name": "private-model-not-returned"}]}
        assert path == "/api/ps" and method == "GET"
        if isinstance(ps, Exception):
            raise ps
        return ps if ps is not None else {"models": [{"name": MODEL, "model": MODEL, "size_vram": 1024}]}
    return request


def fixture_system(monkeypatch):
    monkeypatch.setattr(ob.sys, "platform", "linux")
    counters = iter(["cpu 100 0 50 1000 0 0 0 0 0 0\n", "cpu 130 0 70 1050 0 0 0 0 0 0\n"])
    monkeypatch.setattr(res, "_proc", lambda name: next(counters) if name == "stat" else
                        "MemTotal: 4096 kB\nMemAvailable: 1024 kB\n")


def test_cpu_sample_counter_keeps_exact_interval_with_coarse_deadline_clock(monkeypatch):
    from types import SimpleNamespace
    fixture_system(monkeypatch)
    counter = iter((200.0,200.05))
    sleeps = []
    clock = SimpleNamespace(monotonic=lambda:100.0,perf_counter=lambda:next(counter),sleep=sleeps.append)
    monkeypatch.setattr(res,"time",clock)
    monkeypatch.setattr(rt,"time",clock)
    result = res.sample_cpu("linux",50,105.0)
    assert sleeps == [.05] and result["status"] == "measured"
    assert result["values"]["interval_s"] == pytest.approx(.05)
    assert result["values"]["busy_percent"] == 50


@pytest.mark.parametrize("counter", [(200.0,200.0),(200.0,199.0)])
def test_cpu_sample_invalid_counter_interval_is_refused(monkeypatch,counter):
    from types import SimpleNamespace
    fixture_system(monkeypatch)
    readings = iter(counter)
    clock = SimpleNamespace(monotonic=lambda:100.0,perf_counter=lambda:next(readings),sleep=lambda seconds:None)
    monkeypatch.setattr(res,"time",clock)
    monkeypatch.setattr(rt,"time",clock)
    with pytest.raises(rt.RuntimeErrorDetail,match="CPU interval invalid"):
        res.sample_cpu("linux",50,105.0)


def test_cpu_sample_insufficient_budget_does_not_sleep(monkeypatch):
    from types import SimpleNamespace
    fixture_system(monkeypatch)
    clock = SimpleNamespace(monotonic=lambda:100.0,perf_counter=lambda:200.0,
                            sleep=lambda seconds:pytest.fail("no sample after budget refusal"))
    monkeypatch.setattr(res,"time",clock)
    monkeypatch.setattr(rt,"time",clock)
    with pytest.raises(TimeoutError,match="insufficient CPU sample budget"):
        res.sample_cpu("linux",50,100.02)


def test_observe_public_pipeline_partial_without_gpu(monkeypatch):
    fixture_system(monkeypatch)
    calls = []
    result = ob.observe(base=BASE, model=MODEL, sample_ms=50, transport=fixture_transport(calls))
    assert result["status"] == "partial" and result["measured_sections"] == 3
    assert calls == ["/api/version", "/api/tags", "/api/ps"]
    assert result["sections"]["cpu"]["values"]["busy_percent"] == 50
    assert result["sections"]["cpu"]["values"]["interval_s"] >= .05
    assert result["sections"]["ram"]["values"]["total_bytes"] == 4096 * 1024
    assert result["sections"]["gpu"]["values"] is None
    assert result["sections"]["ollama"]["values"]["resident_vram_bytes"] == 1024
    assert "private-model-not-returned" not in json.dumps(result)
    digest = result.pop("evidence_sha256")
    assert hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest() == digest


@pytest.mark.parametrize("first,second,platform", [
    ((1, 3, 4), (1, 2, 8), "win32"), ((1, 3, 4), (1, 3, 4), "win32"),
    ((1, 3, 4), (8, 4, 5), "win32"), ((1, 2), (2, 3), "win32"),
    ((0,) * 8, (0,) * 8, "linux"), ((0,0,0,0,2,0,0,0), (1,1,1,1,1,1,1,1), "linux"),
    ((False, 1, 2), (1,2,3), "win32"), ((0,1,2), (1,2,2**65), "win32")])
def test_bad_cpu_samples_refused(first, second, platform):
    with pytest.raises(ValueError):
        res.cpu_delta(first, second, platform)


def test_cpu_windows_idle_included_and_linux_guest_not_double_counted():
    assert res.cpu_delta((100,300,200), (150,400,300), "win32")["busy_percent"] == 75
    first = res.linux_cpu("cpu 100 20 50 1000 10 5 5 10 90 20")
    second = res.linux_cpu("cpu 120 30 60 1060 10 5 5 10 110 30")
    assert res.cpu_delta(first, second, "linux")["busy_percent"] == 40


@pytest.mark.parametrize("text", ["cpu0 1 2 3", "cpu 1 2 3", "cpu 1 2 -1 4 5 6 7 8 9 10",
    "cpu 1 2 nan 4 5 6 7 8 9 10", "cpu 1 2 3 4 5 6 7 8 9 10 junk"])
def test_cpu_parser_refuses_incomplete_or_invalid(text):
    with pytest.raises(ValueError): res.linux_cpu(text)


@pytest.mark.parametrize("text", ["MemTotal: 1 kB", "MemTotal: 1 kB\nMemAvailable: 2 kB",
    "MemTotal: 1 bytes\nMemAvailable: 0 kB", "MemTotal: 1 kB\nMemTotal: 2 kB\nMemAvailable: 0 kB",
    "MemTotal: 0 kB\nMemAvailable: 0 kB", "MemTotal: 1 kB\nMemAvailable: NaN kB"])
def test_memory_refusals(text):
    with pytest.raises(ValueError): res.linux_memory(text)


def test_windows_ffi_layout_and_success_failure_with_synthetic_api(monkeypatch):
    assert ctypes.sizeof(res.FileTime) == 8 and ctypes.sizeof(res.MemoryStatus) == 64
    class Kernel:
        def GetSystemTimes(self, idle, kernel, user):
            for pointer, value in ((idle,2**32+7),(kernel,2**32+19),(user,40)):
                item = ctypes.cast(pointer, ctypes.POINTER(res.FileTime)).contents
                item.low, item.high = value & 0xffffffff, value >> 32
            return 1
        def GlobalMemoryStatusEx(self, pointer):
            item = ctypes.cast(pointer, ctypes.POINTER(res.MemoryStatus)).contents
            assert item.length == 64
            item.total_phys, item.available_phys = 8192, 2048
            return 1
    monkeypatch.setattr(res, "_kernel", Kernel)
    assert res.windows_cpu() == (2**32+7, 2**32+19, 40)
    assert res.windows_memory()["available_bytes"] == 2048
    monkeypatch.setattr(Kernel, "GetSystemTimes", lambda *a: 0)
    monkeypatch.setattr(Kernel, "GlobalMemoryStatusEx", lambda *a: 0)
    with pytest.raises(OSError): res.windows_cpu()
    with pytest.raises(OSError): res.windows_memory()


@pytest.mark.parametrize("text", ["", "x, 1, 2, 1", "x, 0, 0, 0", "x, NaN, 0, 0", "x, Inf, 0, 0",
    "x, 1, 0, -1", "x, 1, 0, 101", "x, 1, 0", "x, 1, 0, 1\n"*17])
def test_gpu_invalid_values(text):
    with pytest.raises(ValueError): res.parse_gpu(text)


def test_gpu_nullable_and_units():
    values = res.parse_gpu('"Synthetic GPU, quoted", 4096, 1024, 0\nSynthetic 2, 2048, N/A, [Not Supported]\n')
    assert values[0]["total_bytes"] == 4294967296 and values[0]["utilization_percent"] == 0
    assert values[0]["complete"] is True
    assert values[1]["used_bytes"] is None and values[1]["complete"] is False


@pytest.mark.parametrize("mode,status", [("ok","measured"),("partial","partial"),("huge","error"),
    ("stderr","error"),("slow","timeout"),("nonzero","error"),("badutf8","error"),("empty","error")])
def test_real_process_stream_cap_deadline_and_completion(monkeypatch, tmp_path, mode, status):
    executable = tmp_path / "nvidia-smi.exe"
    executable.write_bytes(b"synthetic executable identity; execution is translated at test boundary")
    native = subprocess.Popen
    observed = []
    class FixturePopen(native):
        def __init__(self, argv, **kwargs):
            assert argv == [str(executable), *res.GPU_ARGS]
            assert kwargs["shell"] is False and kwargs["stdin"] == subprocess.DEVNULL
            observed.append(argv)
            super().__init__([sys.executable,"-I","-S","-B",str(Path(__file__).with_name("gpu_fixture_child.py")),
                              mode,str(tmp_path)], **kwargs)
    monkeypatch.setattr(res.subprocess, "Popen", FixturePopen)
    started = time.monotonic()
    result = res.gpu_process(executable, started + (1.5 if mode == "slow" else 5))
    assert result["status"] == status
    assert result["process"]["process_termination_confirmed"] is True
    assert result["process"]["captured_bytes"] <= 65536
    assert len(observed) == 1 and time.monotonic() - started < 5
    assert "synthetic diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("path", ["relative.exe", "../escape.exe"])
def test_gpu_relative_executable_refused(path):
    with pytest.raises(ValueError): res._executable(path)


@pytest.mark.parametrize("ps", [{"models": []}, {"models": [{"name": MODEL}]},
    {"models": [{"name": MODEL, "size_vram": True}]}, {"models": [{"name": MODEL}, {"name": MODEL}]},
    OSError("synthetic private details")])
def test_ps_absence_and_partial_do_not_become_false_gpu_health(monkeypatch, ps):
    fixture_system(monkeypatch)
    result = ob.observe(base=BASE, model=MODEL, sample_ms=50, transport=fixture_transport([], ps=ps))
    section = result["sections"]["ollama"]
    assert section["values"]["resident_vram_bytes"] is None
    assert section["values"]["installed"] is True
    assert result["status"] == "partial" and result["health"] == "not_inferred"
    if ps == {"models": []}:
        assert section["values"]["resident"] is False
    else:
        assert section["status"] == "partial"
    assert "synthetic private details" not in json.dumps(result)


def test_deadline_before_second_cpu_sample_preserves_unknown(monkeypatch):
    fixture_system(monkeypatch)
    result = ob.observe(base=BASE, model=MODEL, timeout=.01, sample_ms=50,
        transport=fixture_transport([]))
    assert result["sections"]["cpu"]["status"] == "timeout"
    assert result["sections"]["cpu"]["values"] is None


@pytest.mark.parametrize("changes", [{"timeout":True},{"timeout":11},{"timeout":float('nan')},
    {"sample_ms":0},{"sample_ms":True},{"model":""},{"base":"https://example.org"}])
def test_invalid_input_before_measurement(monkeypatch, changes):
    monkeypatch.setattr(res,"_proc",lambda *a:pytest.fail("must not read OS"))
    args = dict(base=BASE,model=MODEL); args.update(changes)
    with pytest.raises(ValueError): ob.observe(**args)


def test_actual_historical_service_rule_parity():
    path = Path(__file__).resolve().parent / "fixtures/service_doctor_core.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "9a12b6a57528d5a78ed55c176aeed2a60b1fb8810a3cf1997620e465932721f5"
    spec = importlib.util.spec_from_file_location("historical_service", path)
    historical = importlib.util.module_from_spec(spec); spec.loader.exec_module(historical)
    for status in ("healthy","unhealthy"):
        for latency in (0,1,5000,5001,60000):
            original = historical.evaluate(dict(service="synthetic",status=status,latency_ms=latency))
            result = ob.service_diagnostic(status == "healthy", latency)
            assert original["status"] == result["status"]


@pytest.mark.parametrize("name,content,valid", [("stat", b"cpu 1 2 3 4 5 6 7 8 9 10\n"+b"private later line\n", True),
    ("stat", b"x"*4097, False), ("meminfo", b"MemTotal: 1 kB\nMemAvailable: 0 kB\n", True),
    ("meminfo", b"x"*65537, False)], ids=["cpu-first-line", "cpu-over-limit", "ram-normal", "ram-over-limit"])
def test_proc_io_is_bounded_to_exact_allowlisted_file(monkeypatch,tmp_path,name,content,valid):
    path=tmp_path/"fixture";path.write_bytes(content)
    native=res.os.open
    def redirect(target,flags,*args,**kwargs):
        if str(target).startswith("/proc/"):
            assert target=="/proc/"+name
            assert not flags & (res.os.O_WRONLY|res.os.O_RDWR|res.os.O_CREAT)
            return native(path,flags,*args,**kwargs)
        return native(target,flags,*args,**kwargs)
    monkeypatch.setattr(res.os,"open",redirect)
    if valid:
        value=res._proc(name)
        assert "private later line" not in value
    else:
        with pytest.raises(ValueError):res._proc(name)
    with pytest.raises(ValueError):res._proc("self/environ")


def test_complete_measurement_is_not_automatic_health(monkeypatch):
    fixture_system(monkeypatch)
    monkeypatch.setattr(res,"gpu_process",lambda *a:dict(status="measured",values=[{"complete":True}]))
    result=ob.observe(base=BASE,model=MODEL,sample_ms=50,gpu_executable="trusted-boundary-fixture",
                       transport=fixture_transport([]))
    assert result["status"]=="measured" and result["health"]=="not_inferred"
    assert result["inference_completed"] is False


def test_os_unsupported_explicit(monkeypatch):
    monkeypatch.setattr(ob.sys,"platform","unknown-synthetic")
    monkeypatch.setattr(res,"_proc",lambda *a:pytest.fail("unsupported OS must not read proc"))
    result=ob.observe(base=BASE,model=MODEL,transport=fixture_transport([]))
    assert result["sections"]["cpu"]["status"]=="unsupported"
    assert result["sections"]["ram"]["status"]=="unsupported"


def test_untrusted_gpu_path_components_rejected(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import stat
    target=tmp_path/"nvidia-smi.exe";target.write_bytes(b"fixture")
    original=Path.lstat
    def redirected(path,*args,**kwargs):
        if path==target:return SimpleNamespace(st_mode=stat.S_IFREG,st_file_attributes=0x400)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"lstat",redirected)
    with pytest.raises(ValueError,match="redirect"):res._executable(target)


def test_gpu_uncertain_cleanup_never_claims_process_ended(monkeypatch,tmp_path):
    import io
    from types import SimpleNamespace

    # This fixture exercises failed cleanup, not elapsed OS setup time. Keep
    # the real shared-budget calculation, driven by one controlled clock.
    now = [100.0]
    sleeps = []
    def advance(seconds):
        assert 0 < seconds <= .01
        sleeps.append(seconds)
        assert len(sleeps) <= 300
        now[0] += seconds
    clock = SimpleNamespace(monotonic=lambda:now[0], sleep=advance)
    monkeypatch.setattr(res,"time",clock)
    monkeypatch.setattr(rt,"time",clock)
    monkeypatch.setattr(res,"_windows_program_files",lambda:str(tmp_path))
    executable=tmp_path/"nvidia-smi.exe";executable.write_bytes(b"synthetic")
    calls = []
    class Process:
        stdout=io.BytesIO(b"")
        stderr=io.BytesIO(b"")
        returncode=None
        def poll(self):return None
        def kill(self):
            calls.append("kill")
            raise PermissionError("synthetic process cannot terminate")
        def wait(self,timeout):
            assert 0 < timeout <= .5
            calls.append("wait")
            raise subprocess.TimeoutExpired("synthetic",timeout)
    process=Process()
    def popen(argv,**kwargs):
        assert argv == [str(executable), *res.GPU_ARGS]
        assert kwargs["shell"] is False and kwargs["stdin"] == subprocess.DEVNULL
        calls.append("start")
        return process
    monkeypatch.setattr(res.subprocess,"Popen",popen)
    result=res.gpu_process(executable,clock.monotonic()+1)
    assert calls == ["start", "kill", "wait"] and sleeps
    assert result["error_code"]=="GPU_TERMINATION_UNCONFIRMED"
    assert result["status"]=="error" and result["process"]["process_termination_confirmed"] is False
    assert result["values"] is None and result["process"]["captured_bytes"] == 0
    assert process.stdout.closed and process.stderr.closed
    assert "synthetic process cannot terminate" not in json.dumps(result)


def test_gpu_reader_start_fault_still_reaps_real_process(monkeypatch,tmp_path):
    executable=tmp_path/"nvidia-smi.exe";executable.write_bytes(b"synthetic")
    native=subprocess.Popen;children=[]
    class FixturePopen(native):
        def __init__(self,argv,**kwargs):
            assert argv == [str(executable),*res.GPU_ARGS]
            super().__init__([sys.executable,"-I","-S","-B",str(Path(__file__).with_name("gpu_fixture_child.py")),
                              "slow",str(tmp_path)],**kwargs)
            children.append(self)
    monkeypatch.setattr(res.subprocess,"Popen",FixturePopen)
    native_start=res.threading.Thread.start
    def broken_start(thread):
        if thread.name=="local-ai-gpu-output":raise RuntimeError("synthetic exhausted reader budget")
        return native_start(thread)
    monkeypatch.setattr(res.threading.Thread,"start",broken_start)
    try:
        result=res.gpu_process(executable,time.monotonic()+5)
        assert result["status"]=="error" and result["error_code"]=="GPU_OBSERVATION_FAILED"
        assert result["process"]["process_termination_confirmed"] is True
        assert len(children)==1 and children[0].poll() is not None
    finally:
        # Also clean up when this counterexample runs against the earlier implementation.
        for child in children:
            if child.poll() is None:child.kill()
            child.wait(timeout=2)
            child.stdout.close();child.stderr.close()
