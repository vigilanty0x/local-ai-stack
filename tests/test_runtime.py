"""Runtime workflow counterproofs with synthetic transport and isolated state."""
import json

import threading
import types

import pytest
from local_ai_stack import runtime as rt
from local_ai_stack.cli import main


def fake(calls, *, output=None, models=None):
    def request(base, method, path, data, deadline):
        calls.append((method, path, data, deadline))
        if path == "/api/version": return {"version": "synthetic-1"}
        if path == "/api/tags": return {"models": [{"name": m} for m in (models if models is not None else ["test:1"])]}
        return output if output is not None else {"model": "test:1", "done": True, "response": "Synthetic answer", "eval_count": 4}
    return request


def test_end_to_end_exact_model_and_one_deadline(tmp_path):
    calls = []
    result = rt.run(base="http://localhost:11434", model="test:1", prompt="synthetic",
                    lock_path=tmp_path / "ollama.lock", transport=fake(calls))
    assert result["status"] == "completed" and result["response"] == "Synthetic answer"
    assert result["inference_completed"] is True and result["model_observed"] == "test:1"
    assert [c[1] for c in calls] == ["/api/version", "/api/tags", "/api/generate"]
    assert len({c[3] for c in calls}) == 1
    assert calls[-1][2]["stream"] is False
    assert result["response_bytes"] == 16 and result["output_tokens"] == 4


def test_doctor_never_claims_inference_readiness():
    calls = []
    result = rt.run(base="http://127.0.0.1:11434", model="test:1", transport=fake(calls))
    assert result["status"] == "available" and result["inference_readiness"] == "not_measured"
    assert result["inference_completed"] is False and len(calls) == 2


def test_absent_exact_tag_never_calls_generation(tmp_path):
    calls = []
    result = rt.run(base="http://localhost:11434", model="test:1", prompt="synthetic",
        lock_path=tmp_path / "lock", transport=fake(calls, models=["test:latest"]))
    assert result["status"] == "blocked" and result["response"] is None
    assert result["model_installed"] is False and len(calls) == 2


@pytest.mark.parametrize("change", [{"done": False}, {"response": ""}, {"response": {}},
    {"model": "test:other"}, {"eval_count": True}, {"eval_count": 10000}, {"error": "failed"}])
def test_incomplete_or_different_generation_cannot_pass(tmp_path, change):
    output = {"model": "test:1", "done": True, "response": "ok", "eval_count": 2, **change}
    result = rt.run(base="http://localhost:11434", model="test:1", prompt="synthetic",
        lock_path=tmp_path / "lock", transport=fake([], output=output))
    assert result["status"] == "blocked" and result["inference_completed"] is False
    assert result["response"] is None


@pytest.mark.parametrize("base", ["https://example.invalid", "http://169.254.169.254:11434",
    "http://localhost:1234", "http://user:pass@localhost:11434", "http://localhost:11434/private",
    "http://localhost:11434?secret=x", "http://localhost:11434#fragment"])
def test_nonlocal_endpoint_refused_before_transport(base):
    with pytest.raises(ValueError):
        rt.run(base=base, model="test:1", transport=lambda *a: pytest.fail("transport called"))


def test_global_deadline_is_not_reset_between_stages(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock[0])
    calls = []
    original = fake(calls)
    def delayed(*args):
        value = original(*args)
        clock[0] += 2
        return value
    result = rt.run(base="http://localhost:11434", model="test:1", prompt="synthetic",
        timeout=3, transport=delayed, lock_path=tmp_path / "lock")
    assert result["status"] == "timeout" and len(calls) == 2
    assert result["response"] is None


def test_busy_lock_refuses_inference_without_a_post(monkeypatch, tmp_path):
    from local_ai_stack.coordination import OllamaBusy
    calls = []
    def busy(**kwargs): raise OllamaBusy("synthetic contention")
    monkeypatch.setattr(rt, "ollama_inference_lock", busy)
    result = rt.run(base="http://localhost:11434", model="test:1", prompt="synthetic",
        lock_path=tmp_path / "lock", transport=fake(calls))
    assert result["status"] == "blocked" and len(calls) == 2


@pytest.mark.parametrize("status,raw", [(302,b"{}"),(200,b'{"x":NaN}'),
    (200,b'{"x":1,"x":2}'),(200,b'[]'),(200,b'{"x":1e999}')])
def test_real_transport_rejects_ambiguous_http_and_json(monkeypatch,status,raw):
    class Connection:
        sock=None
        def __init__(self,*args,**kwargs): pass
        def request(self,*args,**kwargs): pass
        def close(self): pass
        def getresponse(self):
            data=[raw,b""]
            return types.SimpleNamespace(status=status,getheader=lambda k:str(len(raw)),read=lambda n:data.pop(0))
    monkeypatch.setattr(rt.http.client,"HTTPConnection",Connection)
    monkeypatch.setattr(rt.socket,"getaddrinfo",lambda *a,**kw:[(2,1,6,"",("127.0.0.1",11434))])
    with pytest.raises(ValueError):
        rt.request_json("http://localhost:11434","GET","/api/version",None,rt.time.monotonic()+5)


def test_dns_public_address_never_opens_connection(monkeypatch):
    monkeypatch.setattr(rt.socket,"getaddrinfo",lambda *a,**kw:[(2,1,6,"",("8.8.8.8",11434))])
    monkeypatch.setattr(rt.http.client,"HTTPConnection",lambda *a,**kw:pytest.fail("connection made"))
    with pytest.raises(ValueError):
        rt.request_json("http://ollama:11434","GET","/api/version",None,rt.time.monotonic()+5)


def test_slow_dns_cannot_extend_deadline(monkeypatch):
    release=threading.Event()
    def slow(*args,**kwargs):
        release.wait(1)
        return [(2,1,6,"",("127.0.0.1",11434))]
    monkeypatch.setattr(rt.socket,"getaddrinfo",slow)
    started=rt.time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            rt.request_json("http://ollama:11434","GET","/api/version",None,started+.03)
        assert rt.time.monotonic()-started < .3
    finally: release.set()


def test_cli_invokes_the_integrated_runtime(monkeypatch, capsys, tmp_path):
    actual=rt.run
    monkeypatch.setattr(rt,"run",lambda **kwargs:actual(**kwargs,transport=fake([])))
    assert main(["infer","--model","test:1","--prompt","synthetic","--lock",str(tmp_path/"lock")])==0
    result=json.loads(capsys.readouterr().out)
    assert result["status"]=="completed" and len(result["stages"])==3
