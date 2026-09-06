"""Explicit CPU/RAM/GPU and same-origin Ollama snapshots."""
import sys
import time
import math

from . import resources
from .measurements import integer, observed_call, seal, utc_now, validate
from .runtime import RuntimeErrorDetail, remaining, request_json_terminal


def _inventory(value):
    entries = value.get("models")
    if not isinstance(entries, list) or len(entries) > 1000:
        raise RuntimeErrorDetail("model inventory invalid")
    names = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeErrorDetail("model inventory row invalid")
        name = entry.get("name")
        if not isinstance(name, str) or not name or len(name) > 200 or name in names:
            raise RuntimeErrorDetail("model inventory identity invalid")
        names[name] = entry
    return names


def ollama_observation(base, model, deadline, transport):
    started = time.monotonic()
    version = transport(base, "GET", "/api/version", None, deadline)
    remaining(deadline)
    label = version.get("version")
    if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
        raise RuntimeErrorDetail("runtime version invalid")
    tags = _inventory(transport(base, "GET", "/api/tags", None, deadline))
    remaining(deadline)
    values = dict(version=label, model_requested=model, installed=model in tags,
                  resident=None, resident_vram_bytes=None, declared_model_digest=None,
                  inference_readiness="not_measured", producer="contacted Ollama server",
                  model_weights_verified=False)
    # Retain the two successful stages if ps fails or is unavailable on an older server.
    try:
        active = _inventory(transport(base, "GET", "/api/ps", None, deadline))
        remaining(deadline)
        row = active.get(model)
        values["resident"] = row is not None
        if row is not None:
            if row.get("model", model) != model or model not in tags:
                raise RuntimeErrorDetail("resident model identity inconsistent")
            vram = row.get("size_vram")
            values["resident_vram_bytes"] = integer(vram) if vram is not None else None
            digest = row.get("digest")
            if digest is not None:
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise RuntimeErrorDetail("model digest invalid")
                values["declared_model_digest"] = digest
        status = "partial" if row is not None and values["resident_vram_bytes"] is None else "measured"
        return dict(status=status, values=values, error_code=None,
                    service_diagnostic=service_diagnostic(True, (time.monotonic() - started) * 1000))
    except Exception as exc:
        values.update(resident=None, resident_vram_bytes=None, declared_model_digest=None)
        return dict(status="partial", values=values, error_code="OLLAMA_PS_" + type(exc).__name__,
                    service_diagnostic={"status": "not_measured", "reason": "incomplete observations"})


def service_diagnostic(observed, latency_ms):
    # Port of service-doctor core's healthy + [0,5000]ms rule, for observed inputs only.
    valid = type(latency_ms) in (int, float) and math.isfinite(latency_ms) and 0 <= latency_ms <= 5000
    return dict(status="passed" if observed is True and valid else "failed",
                latency_ms=latency_ms, threshold_ms=5000, source="service-doctor")


def observe(*, base, model, timeout=10.0, sample_ms=250, gpu_executable=None,
            transport=request_json_terminal):
    validate(base, model, timeout, 10)
    if type(sample_ms) is not int or not 50 <= sample_ms <= 1000:
        raise RuntimeErrorDetail("sample_ms must be within [50, 1000]")
    started = time.monotonic()
    deadline = started + timeout
    result = dict(schema_version="local-ai-stack/observations-v1", operation="observe",
                  started_at=utc_now(), budget_s=timeout, provenance="local_runtime_observation",
                  requested_sections=["cpu", "ram", "gpu", "ollama"], sections={}, publication=False,
                  inference_completed=False, unmeasured_scopes=["container_quotas", "docker_health",
                      "port_owners", "configuration_drift", "tool_versions", "model_resource_attribution"])
    sections = result["sections"]
    platform = sys.platform
    supported = platform == "win32" or platform.startswith("linux")
    scope = "calling_thread_primary_processor_group" if platform == "win32" else "current_kernel_view"
    def cpu():
        if not supported:
            return dict(status="unsupported", values=None, error_code="OS_UNSUPPORTED")
        return resources.sample_cpu(platform, sample_ms, deadline)
    sections["cpu"] = observed_call("GetSystemTimes" if platform == "win32" else "proc/stat", scope, cpu)
    def memory():
        remaining(deadline)
        if not supported:
            return dict(status="unsupported", values=None, error_code="OS_UNSUPPORTED")
        values = resources.windows_memory() if platform == "win32" else resources.linux_memory(resources._proc("meminfo"))
        remaining(deadline)
        return dict(status="measured", values=values)
    sections["ram"] = observed_call("GlobalMemoryStatusEx" if platform == "win32" else "proc/meminfo",
                                    "current_OS_physical_memory_view", memory)
    sections["gpu"] = observed_call("operator-configured nvidia-smi fixed query", "driver_visible_gpus",
        lambda: resources.gpu_process(gpu_executable, deadline) if gpu_executable is not None else
        dict(status="unavailable", values=None, error_code="GPU_EXECUTABLE_NOT_CONFIGURED"))
    sections["ollama"] = observed_call("GET version/tags/ps", "same_local_origin_requested_model",
        lambda: ollama_observation(base, model, deadline, transport))
    measured = sum(s["status"] == "measured" for s in sections.values())
    known = sum(s["status"] in {"measured", "partial"} for s in sections.values())
    result.update(status="measured" if measured == len(sections) else "partial" if known else "not_measured",
                  measured_sections=measured, requested_count=len(sections), health="not_inferred")
    return seal(result, started)
