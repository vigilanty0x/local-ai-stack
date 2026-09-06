"""Observed local Ollama workflow; no provider account or installation.

One deadline covers DNS, health, inventory, lock wait and inference. The
transport pins an allowed resolved address, uses no proxy, follows no redirect,
and never returns raw HTTP error bodies. A ready process is not a model proof.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import socket
import queue
import threading
import time
from urllib.parse import urlsplit

from .coordination import ollama_inference_lock

MAX_RESPONSE = 1024 * 1024
HOSTS = {"127.0.0.1", "::1", "localhost", "ollama"}


class RuntimeErrorDetail(ValueError):
    pass


class TerminalHTTPRejection(RuntimeErrorDetail):
    """A bounded, complete error response; never a disconnected request."""
    def __init__(self, status):
        self.status = status
        super().__init__(f"local runtime terminal HTTP rejection {status}")


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("local runtime deadline expired")
    return value


def endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or parsed.port not in (None, 11434)):
        raise RuntimeErrorDetail("local Ollama endpoint required on port 11434")
    return parsed.hostname, parsed.port or 11434


def _strict_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeErrorDetail("duplicate JSON key")
        result[key] = value
    return result


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeErrorDetail("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values(): _finite(child)
    elif isinstance(value, list):
        for child in value: _finite(child)


def _resolve(host, port, deadline):
    if host in {"127.0.0.1", "::1", "localhost"}:
        return [ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)]
    results = queue.Queue(maxsize=1)
    def lookup():
        try: results.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except Exception as exc: results.put((False, exc))
    # A stuck resolver cannot delay the caller beyond the common deadline.
    # Its daemon performs no HTTP request and holds no inference lock itself.
    threading.Thread(target=lookup, daemon=True, name="local-ai-dns").start()
    try: ok, value = results.get(timeout=remaining(deadline))
    except queue.Empty: raise TimeoutError("local runtime DNS deadline expired") from None
    remaining(deadline)
    if not ok: raise value
    return [ipaddress.ip_address(item[4][0]) for item in value]


def _request_json(base, method, path, payload, deadline, *, terminal_errors=False):
    host, port = endpoint(base)
    if (method, path) not in {("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"),
                              ("POST", "/api/generate")}:
        raise RuntimeErrorDetail("unsupported local runtime operation")
    remaining(deadline)
    ips = _resolve(host, port, deadline)
    if not ips:
        raise RuntimeErrorDetail("local runtime address unavailable")
    private = [ipaddress.ip_network(value) for value in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
    if any(not (ip.is_loopback or (host == "ollama" and any(ip in block for block in private)))
           for ip in ips):
        raise RuntimeErrorDetail("local runtime resolved outside allowed network")
    connection = http.client.HTTPConnection(str(ips[0]), port, timeout=remaining(deadline))
    expired = threading.Event()
    def interrupt():
        expired.set()
        sock = connection.sock
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        connection.close()
    watchdog = threading.Timer(remaining(deadline), interrupt)
    watchdog.daemon = True
    watchdog.start()
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    authority = "[::1]:11434" if host == "::1" else f"{host}:{port}"
    try:
        connection.request(method, path, body=body, headers={
            "Host": authority, "Accept": "application/json", "Content-Type": "application/json"})
        if connection.sock is not None: connection.sock.settimeout(remaining(deadline))
        response = connection.getresponse()
        if response.status != 200 and not terminal_errors:
            raise RuntimeErrorDetail(f"local runtime HTTP status {response.status}")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE):
            raise RuntimeErrorDetail("local runtime response exceeds limit")
        if terminal_errors:
            transfer = response.getheader("Transfer-Encoding")
            if transfer is not None and (length is not None or transfer.strip().lower() != "chunked"):
                raise RuntimeErrorDetail("local runtime response framing invalid")
        chunks = []
        size = 0
        while True:
            if connection.sock is not None: connection.sock.settimeout(remaining(deadline))
            chunk = response.read(min(65536, MAX_RESPONSE + 1 - size))
            remaining(deadline)
            if not chunk: break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise RuntimeErrorDetail("local runtime response exceeds limit")
        if getattr(response, "length", None) not in (None, 0):
            raise RuntimeErrorDetail("local runtime response incomplete")
        value = json.loads(b"".join(chunks), object_pairs_hook=_strict_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               RuntimeErrorDetail("non-finite JSON constant")))
        _finite(value)
        if not isinstance(value, dict):
            raise RuntimeErrorDetail("local runtime JSON object required")
        if response.status != 200:
            if (response.status in {404, 429, 500, 503}
                    and isinstance(value.get("error"), str) and value["error"].strip()):
                raise TerminalHTTPRejection(response.status)
            raise RuntimeErrorDetail(f"local runtime HTTP status {response.status}")
        return value
    except Exception:
        if expired.is_set(): raise TimeoutError("local runtime HTTP deadline expired") from None
        raise
    finally:
        watchdog.cancel()
        connection.close()


def request_json(base, method, path, payload, deadline):
    return _request_json(base, method, path, payload, deadline)


def request_json_terminal(base, method, path, payload, deadline):
    return _request_json(base, method, path, payload, deadline, terminal_errors=True)


def _generate(base, model, prompt, max_tokens, deadline, transport):
    generated = transport(base, "POST", "/api/generate", {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0}}, deadline)
    remaining(deadline)
    return generated


def _validated_generation(model, prompt, max_tokens, generated):
    text = generated.get("response")
    if (generated.get("done") is not True or generated.get("model") != model
            or not isinstance(text, str) or not text.strip()
            or len(text.encode()) > MAX_RESPONSE or generated.get("error")):
        raise RuntimeErrorDetail("inference incomplete or model identity mismatch")
    count = generated.get("eval_count")
    if type(count) is not int or not 0 <= count <= max_tokens:
        raise RuntimeErrorDetail("inference token count invalid")
    return dict(status="completed", inference_completed=True, response=text,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                response_sha256=hashlib.sha256(text.encode()).hexdigest(),
                response_bytes=len(text.encode()), output_tokens=count)


def run(*, base, model, prompt=None, timeout=30.0, max_tokens=128,
        lock_path=None, transport=request_json, fallback=None):
    """Health -> exact inventory -> optional inference -> observed receipt.

    An error returns no fabricated text or ready status. The returned receipt
    contains prompt/output digests and counts; only a successful explicit
    inference returns the requested answer. No journal stores the prompt.
    """
    endpoint(base)
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or any(ord(c) < 33 or ord(c) > 126 for c in model)):
        raise RuntimeErrorDetail("an exact model tag is required")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 180:
        raise RuntimeErrorDetail("timeout must be within (0, 180]")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 2048:
        raise RuntimeErrorDetail("max_tokens must be within [1, 2048]")
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()
                               or len(prompt.encode()) > 32768):
        raise RuntimeErrorDetail("nonempty prompt of at most 32768 bytes required")
    if fallback is not None:
        from .fallback import run_fallback
        return run_fallback(base=base, model=model, prompt=prompt, timeout=timeout,
                            max_tokens=max_tokens, lock_path=lock_path,
                            transport=transport, policy=fallback)
    started = time.monotonic()
    deadline = started + timeout
    result = {"schema_version": "local-ai-stack/runtime-v1", "operation": "infer" if prompt is not None else "doctor",
              "model_requested": model, "model_observed": None, "status": "blocked",
              "runtime_observed": False, "model_installed": False, "inference_completed": False,
              "observed_at": time.time(), "stages": [], "response": None,
              "provenance": "local_runtime_observation", "publication": False}
    try:
        version = transport(base, "GET", "/api/version", None, deadline)
        remaining(deadline)
        if not isinstance(version.get("version"), str) or not version["version"].strip():
            raise RuntimeErrorDetail("runtime version missing")
        result.update(runtime_observed=True, runtime_version=version["version"][:100])
        result["stages"].append({"stage": "health", "status": "passed"})
        tags = transport(base, "GET", "/api/tags", None, deadline)
        remaining(deadline)
        models = tags.get("models")
        if not isinstance(models, list) or len(models) > 1000:
            raise RuntimeErrorDetail("model inventory invalid")
        names = [item.get("name") for item in models if isinstance(item, dict)]
        if model not in names:
            raise RuntimeErrorDetail("requested exact model is not installed")
        result.update(model_installed=True, model_observed=model)
        result["stages"].append({"stage": "inventory", "status": "passed"})
        if prompt is None:
            result.update(status="available", inference_readiness="not_measured")
        else:
            with ollama_inference_lock(timeout_s=remaining(deadline), lock_path=lock_path):
                generated = _generate(base, model, prompt, max_tokens, deadline, transport)
            result.update(_validated_generation(model, prompt, max_tokens, generated))
            result["stages"].append({"stage": "inference", "status": "passed"})
    except Exception as exc:
        result.update(status="timeout" if isinstance(exc, TimeoutError) else "blocked",
                      error_code=type(exc).__name__, response=None,
                      error_detail=str(exc) if isinstance(exc, RuntimeErrorDetail) else "local runtime operation failed")
        result["stages"].append({"stage": "inference" if result["model_installed"] else
                                "inventory" if result["runtime_observed"] else "health", "status": result["status"]})
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    result["budget_s"] = timeout
    result["evidence_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return result
