"""Small explicit benchmark through the existing transport and inference lock."""
import hashlib
import math
import statistics
import time

from .coordination import ollama_inference_lock
from .measurements import integer, seal, utc_now, validate
from .observations import _inventory
from .runtime import RuntimeErrorDetail, _generate, _validated_generation, remaining, request_json_terminal

PROMPT_VERSION = "local-ai-stack/synthetic-benchmark-v1"
PROMPT = "Write one short sentence describing a blue triangle. Do not use tools."


def generation_metrics(generated, elapsed, max_tokens):
    count = integer(generated.get("eval_count"), max_tokens)
    values = {"output_tokens_server_reported": count, "client_elapsed_s": elapsed,
              "server_tokens_per_second": None, "client_tokens_per_second": None}
    missing = []
    for field in ("eval_duration", "total_duration", "load_duration", "prompt_eval_duration"):
        value = generated.get(field)
        values[field + "_ns_server_reported"] = integer(value, 86400 * 10**9) if value is not None else None
        if value is None:
            missing.append(field)
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeErrorDetail("benchmark elapsed time invalid")
    total = values["total_duration_ns_server_reported"]
    components = [values[name + "_ns_server_reported"] for name in
                  ("eval_duration", "load_duration", "prompt_eval_duration")]
    if total is not None and any(v is not None and v > total for v in components):
        raise RuntimeErrorDetail("server durations inconsistent")
    if total is not None and all(v is not None for v in components) and sum(components) > total:
        raise RuntimeErrorDetail("server duration components exceed total")
    duration = values["eval_duration_ns_server_reported"]
    if count > 0:
        values["client_tokens_per_second"] = count / elapsed
        if duration is not None and duration > 0:
            values["server_tokens_per_second"] = count * 10**9 / duration
    if any(isinstance(value, float) and not math.isfinite(value) for value in values.values()):
        raise RuntimeErrorDetail("derived measurement overflow")
    return dict(status="measured" if not missing and values["server_tokens_per_second"] is not None else "partial",
                values=values, missing_fields=missing,
                provenance={"client_elapsed_s": "client_monotonic_observation",
                            "durations_and_tokens": "contacted_server_declaration",
                            "throughputs": "derived_using_server_reported_token_count"})


def benchmark(*, base, model, repetitions=1, timeout=60.0, max_tokens=64,
              lock_path=None, transport=request_json_terminal):
    validate(base, model, timeout, 180)
    if type(repetitions) is not int or not 1 <= repetitions <= 3:
        raise RuntimeErrorDetail("repetitions must be within [1, 3]")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 128:
        raise RuntimeErrorDetail("max_tokens must be within [1, 128]")
    started = time.monotonic()
    deadline = started + timeout
    result = dict(schema_version="local-ai-stack/benchmark-v1", operation="benchmark",
        started_at=utc_now(), budget_s=timeout, model_requested=model, model_observed=None,
        runtime_observed=False, model_installed=False, status="blocked", measurement_status="not_measured",
        prompt_version=PROMPT_VERSION, prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        repetitions_requested=repetitions, max_tokens_per_attempt=max_tokens,
        attempts=[dict(index=i + 1, status="not_attempted") for i in range(repetitions)],
        provenance="local_runtime_observation", publication=False, model_weights_verified=False,
        quality="not_measured", persistent_circuit="not_implemented", response=None)
    active = None
    try:
        version = transport(base, "GET", "/api/version", None, deadline)
        remaining(deadline)
        label = version.get("version")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise RuntimeErrorDetail("runtime version missing")
        result.update(runtime_observed=True, runtime_version=label)
        tags = _inventory(transport(base, "GET", "/api/tags", None, deadline))
        remaining(deadline)
        if model not in tags:
            raise RuntimeErrorDetail("requested exact model is not installed")
        result["model_installed"] = True
        with ollama_inference_lock(timeout_s=remaining(deadline), lock_path=lock_path):
            for active in result["attempts"]:
                remaining(deadline)
                active.update(status="started", started_at=utc_now())
                attempt_start = time.monotonic()
                try:
                    generated = _generate(base, model, PROMPT, max_tokens, deadline, transport)
                    elapsed = time.monotonic() - attempt_start
                    response = _validated_generation(model, PROMPT, max_tokens, generated)
                    remaining(deadline)
                    active.update(status="completed", model_observed=model,
                        response_sha256=response["response_sha256"], response_bytes=response["response_bytes"],
                        output_tokens=response["output_tokens"], client_elapsed_s=elapsed)
                    # Bad metrics do not undo the observed completion, but stop this benchmark.
                    active["metrics"] = generation_metrics(generated, elapsed, max_tokens)
                    active["diagnostic"] = dict(source="local-model-benchmark", status="not_measured")
                    if active["metrics"]["status"] == "measured":
                        active["diagnostic"]["status"] = "passed" if elapsed * 1000 <= 60000 else "failed"
                    remaining(deadline)
                except Exception as exc:
                    if active["status"] == "started":
                        active["status"] = "timeout" if isinstance(exc, TimeoutError) else "blocked"
                    active["error_code"] = type(exc).__name__
                    raise
                finally:
                    active.update(ended_at=utc_now(), elapsed_s=time.monotonic() - attempt_start)
        remaining(deadline)
        result.update(status="completed", model_observed=model)
    except Exception as exc:
        result.update(status="timeout" if isinstance(exc, TimeoutError) else "blocked",
                      error_code=type(exc).__name__, error_detail="local benchmark did not fully complete")
    completed = [a for a in result["attempts"] if a["status"] == "completed"]
    usable = [a for a in completed if a.get("metrics", {}).get("status") == "measured"]
    result.update(completed_attempts=len(completed), measured_attempts=len(usable),
                  measurement_status="measured" if len(usable) == repetitions and result["status"] == "completed"
                  else "partial" if completed else "not_measured",
                  aggregates=None)
    if usable:
        result["aggregates"] = dict(sample_count=len(usable), requested_count=repetitions,
            client_latency_ms_median=statistics.median(a["client_elapsed_s"] * 1000 for a in usable),
            server_tokens_per_second_median=statistics.median(a["metrics"]["values"]["server_tokens_per_second"] for a in usable))
    return seal(result, started)
