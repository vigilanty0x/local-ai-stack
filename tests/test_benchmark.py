import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from local_ai_stack import benchmark as bm, cli, coordination, runtime as rt
from test_fallback import http_fixture

BASE = "http://localhost:11434"
MODEL = "first:1"


def generated(**changes):
    value = dict(model=MODEL,done=True,response="A blue triangle has three sides.",eval_count=8,
        eval_duration=200000000,total_duration=400000000,load_duration=100000000,prompt_eval_duration=50000000)
    value.update(changes)
    return value


def request_fixture(calls, generate=None, models=(MODEL,)):
    def request(base, method, path, payload, deadline):
        calls.append(dict(method=method,path=path,payload=payload,deadline=deadline))
        if path == "/api/version": return {"version":"synthetic"}
        if path == "/api/tags": return {"models":[{"name":model} for model in models]}
        return generate(payload) if generate else generated()
    return request


def execute(tmp_path, **kwargs):
    return bm.benchmark(base=BASE,model=MODEL,repetitions=3,lock_path=tmp_path/"lock",**kwargs)


def test_three_real_http_generations_under_one_native_lock(monkeypatch,tmp_path):
    with http_fixture(monkeypatch,lambda server,payload:server.respond(200,generated())) as calls:
        result = execute(tmp_path)
    assert result["status"] == "completed" and result["measurement_status"] == "measured"
    assert [call[1] for call in calls] == ["/api/version","/api/tags"] + ["/api/generate"] * 3
    assert result["completed_attempts"] == result["measured_attempts"] == 3
    assert result["aggregates"]["server_tokens_per_second_median"] == 40
    assert all(a["metrics"]["values"]["server_tokens_per_second"] == 40 for a in result["attempts"])
    assert all(a["client_elapsed_s"] > 0 for a in result["attempts"])
    assert all(c[2]["prompt"] == bm.PROMPT and c[2]["options"]["temperature"] == 0 for c in calls if c[0] == "POST")
    assert "A blue triangle has three sides." not in json.dumps(result)
    assert result["quality"] == "not_measured"


def test_repeated_calls_hold_existing_native_lock_and_same_deadline(tmp_path):
    calls = []
    def generate(payload):
        with pytest.raises(coordination.OllamaBusy):
            with coordination.ollama_inference_lock(blocking=False,lock_path=tmp_path/"lock"):
                pytest.fail("benchmark did not hold native lock")
        return generated()
    result = execute(tmp_path,transport=request_fixture(calls,generate))
    assert result["status"] == "completed"
    assert len({c["deadline"] for c in calls}) == 1
    with coordination.ollama_inference_lock(blocking=False,lock_path=tmp_path/"lock"):
        pass


@pytest.mark.parametrize("case", ["wrong-model","truncated","malformed","terminal","timeout","framing"])
def test_real_http_uncertainty_stops_following_attempts(monkeypatch,tmp_path,case):
    def behavior(server,payload):
        if case == "wrong-model": server.respond(200,generated(model="other:1"))
        elif case == "truncated": server.respond(200,generated(),extra_length=5)
        elif case == "malformed": server.respond(200,None,raw=b'{')
        elif case == "terminal": server.respond(503,{"error":"private synthetic detail"})
        elif case == "timeout": server.respond(200,generated(),delay=.4)
        else: server.respond(200,generated(),extra_headers=[("Transfer-Encoding","chunked")])
    with http_fixture(monkeypatch,behavior) as calls:
        result = execute(tmp_path,timeout=.2 if case == "timeout" else 5)
    assert result["status"] in {"blocked","timeout"}
    assert len([c for c in calls if c[0] == "POST"]) == 1
    assert [a["status"] for a in result["attempts"]][1:] == ["not_attempted","not_attempted"]
    assert "private synthetic detail" not in json.dumps(result)


def test_successful_first_sample_preserved_after_second_unknown(monkeypatch,tmp_path):
    n = [0]
    def behavior(server,payload):
        n[0] += 1
        server.respond(200,generated(),extra_length=10 if n[0] == 2 else 0)
    with http_fixture(monkeypatch,behavior) as calls: result = execute(tmp_path)
    assert [a["status"] for a in result["attempts"]] == ["completed","blocked","not_attempted"]
    assert result["measurement_status"] == "partial" and result["status"] == "blocked"
    assert result["aggregates"]["sample_count"] == 1 and result["aggregates"]["requested_count"] == 3
    assert len(calls) == 4


@pytest.mark.parametrize("changes", [{"eval_duration":None},{"eval_duration":0},{"eval_count":0},
    {"load_duration":None},{"total_duration":None}])
def test_generation_complete_does_not_invent_missing_benchmark(changes,tmp_path):
    result = execute(tmp_path,transport=request_fixture([],lambda _:generated(**changes)))
    assert result["status"] == "completed" and result["measurement_status"] == "partial"
    assert result["completed_attempts"] == 3 and result["measured_attempts"] == 0
    assert result["aggregates"] is None


@pytest.mark.parametrize("changes", [{"eval_duration":-1},{"eval_duration":True},{"eval_duration":float('nan')},
    {"eval_duration":float('inf')},{"eval_duration":10**100},{"eval_duration":500000000},
    {"load_duration":300000000},{"eval_count":True},{"eval_count":129}])
def test_invalid_metrics_or_tokens_stop_without_losing_completed_truth(changes,tmp_path):
    calls = []
    result = execute(tmp_path,transport=request_fixture(calls,lambda _:generated(**changes)))
    assert result["status"] == "blocked" and result["measurement_status"] != "measured"
    assert result["attempts"][1]["status"] == "not_attempted"
    assert len([c for c in calls if c["method"] == "POST"]) == 1
    if "eval_count" not in changes:
        assert result["attempts"][0]["status"] == "completed"


def test_model_absent_and_budget_before_next_attempt(tmp_path):
    calls=[]
    result=execute(tmp_path,transport=request_fixture(calls,models=()))
    assert result["status"] == "blocked" and len(calls) == 2
    calls=[]
    def generate(payload):
        time.sleep(.03)
        return generated()
    result=execute(tmp_path,timeout=.02,transport=request_fixture(calls,generate))
    assert result["status"] == "timeout" and len(calls) == 3
    assert result["attempts"][1]["status"] == "not_attempted"


def test_real_child_native_lock_blocks_all_generation(tmp_path):
    # Reuse the tested interprocess fixture, including Windows msvcrt / Linux flock.
    import local_ai_stack
    source = Path(local_ai_stack.__file__).parent.parent
    child = Path(__file__).with_name("fallback_lock_child.py")
    process = subprocess.Popen([sys.executable,"-I","-S","-B",str(child),str(source),str(tmp_path)],
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        ready_deadline = time.monotonic() + 3
        while not (tmp_path / "ready").exists() and process.poll() is None and time.monotonic() < ready_deadline:
            time.sleep(.01)
        assert (tmp_path / "ready").read_text() == "held"
        calls=[]
        result=bm.benchmark(base=BASE,model=MODEL,timeout=.1,lock_path=tmp_path/"lock",
                            transport=request_fixture(calls))
        assert result["status"] == "blocked" and result["error_code"] == "OllamaBusy"
        assert not any(c["method"] == "POST" for c in calls)
    finally:
        (tmp_path/"release").write_text("release")
        stdout,stderr=process.communicate(timeout=5)
        assert process.returncode == 0,stderr
        assert json.loads((tmp_path / "child-result.json").read_text())["blocked"] == []


@pytest.mark.parametrize("changes", [{"repetitions":0},{"repetitions":4},{"repetitions":True},
    {"max_tokens":0},{"max_tokens":129},{"max_tokens":True},{"timeout":float('nan')},{"timeout":181}])
def test_benchmark_bad_inputs_before_io(tmp_path,changes):
    args=dict(base=BASE,model=MODEL,lock_path=tmp_path/"lock",transport=lambda *a:pytest.fail("no IO"))
    args.update(changes)
    with pytest.raises(ValueError): bm.benchmark(**args)


def test_native_ps_route_is_exactly_read_only(monkeypatch):
    with http_fixture(monkeypatch,lambda *a:pytest.fail("no POST")) as calls:
        value=rt.request_json_terminal(BASE,"GET","/api/ps",None,time.monotonic()+5)
        assert isinstance(value["models"],list)
        with pytest.raises(ValueError): rt.request_json_terminal(BASE,"POST","/api/ps",{},time.monotonic()+5)
    assert [c[1] for c in calls] == ["/api/ps"]


def test_cli_measurements_dispatch_and_partial_exit(monkeypatch,capsys):
    from local_ai_stack import observations
    monkeypatch.setattr(observations,"observe",lambda **kw:dict(status="partial",sections={}))
    assert cli.main(["observe","--model",MODEL]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "partial"
    monkeypatch.setattr(bm,"benchmark",lambda **kw:dict(status="completed",measurement_status="measured"))
    assert cli.main(["benchmark","--model",MODEL]) == 0
    assert json.loads(capsys.readouterr().out)["measurement_status"] == "measured"


def test_real_historical_benchmark_rule_on_observed_derived_values():
    path=Path(__file__).resolve().parents[1]/"packages/local-model-benchmark/src/local_model_benchmark/core.py"
    spec=importlib.util.spec_from_file_location("historical_benchmark",path)
    original=importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
    for elapsed in (.001,1,60,60.001):
        measure=bm.generation_metrics(generated(),elapsed,64)
        result=original.evaluate(dict(model=MODEL,tokens_per_second=measure["values"]["server_tokens_per_second"],latency_ms=elapsed*1000))
        assert result["status"] == ("passed" if elapsed <=60 else "failed")
