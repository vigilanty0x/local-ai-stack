"""Small shared receipt primitives; observations are not attestations."""
from datetime import datetime, timezone
import hashlib
import json
import math
import time

from .runtime import RuntimeErrorDetail, endpoint


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate(base, model, timeout, maximum):
    endpoint(base)
    if (not isinstance(model, str) or not model or len(model) > 200
            or any(not 33 <= ord(c) <= 126 for c in model)):
        raise RuntimeErrorDetail("an exact printable model tag is required")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= maximum:
        raise RuntimeErrorDetail(f"timeout must be within (0, {maximum}]")


def seal(result, started):
    result["ended_at"] = utc_now()
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    result["evidence_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return result


def observed_call(method, scope, function):
    started = time.monotonic()
    result = dict(status="error", method=method, scope=scope, started_at=utc_now(),
                  values=None, error_code=None)
    try:
        result.update(function())
    except Exception as exc:
        result.update(status="timeout" if isinstance(exc, TimeoutError) else
                      "unavailable" if isinstance(exc, (FileNotFoundError, PermissionError)) else "error",
                      error_code=type(exc).__name__, error_detail=str(exc) if isinstance(exc, RuntimeErrorDetail)
                      else "local observation could not complete")
    result.update(ended_at=utc_now(), elapsed_s=round(time.monotonic() - started, 6))
    return result


def integer(value, maximum=2**63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise RuntimeErrorDetail("measurement integer invalid")
    return value
