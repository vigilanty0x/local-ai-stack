from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

REQUIRED_FIELDS = ["service", "status", "latency_ms"]
PROJECT = "service-doctor"
RULE = "status must be healthy and latency bounded"

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _rule(record: dict[str, Any]) -> tuple[bool, str]:
    kind = "service"
    if kind == "timeout":
        ok = isinstance(record["timeout_ms"], (int, float)) and isinstance(record["elapsed_ms"], (int, float)) and 0 <= record["elapsed_ms"] <= record["timeout_ms"]
    elif kind == "idempotency":
        ok = all(isinstance(record[key], str) and bool(record[key].strip()) for key in ("request_id", "fingerprint"))
    elif kind == "flaky":
        ok = isinstance(record["runs"], int) and isinstance(record["passes"], int) and record["runs"] > 0 and 0 <= record["passes"] <= record["runs"]
    elif kind == "tests":
        ok = isinstance(record["failed"], int) and record["failed"] == 0 and isinstance(record["passed"], int) and record["passed"] >= 0
    elif kind == "coverage":
        ok = isinstance(record["tested"], int) and isinstance(record["total"], int) and record["total"] > 0 and record["tested"] >= record["total"]
    elif kind == "exit":
        ok = record["exit_code"] == 0 and isinstance(record["duration_ms"], (int, float)) and record["duration_ms"] >= 0
    elif kind == "summary":
        ok = all(isinstance(record[key], str) and bool(record[key].strip()) for key in ("job", "failure", "log_excerpt"))
    elif kind == "matrix":
        ok = all(isinstance(record[key], list) and bool(record[key]) for key in ("python_versions", "operating_systems"))
    elif kind == "readiness":
        ok = isinstance(record["checks_total"], int) and record["checks_total"] > 0 and record["checks_passed"] == record["checks_total"]
    elif kind == "rollback":
        ok = isinstance(record["recovery_seconds"], (int, float)) and 0 <= record["recovery_seconds"] <= 300 and record["release"] != record["rollback_target"]
    elif kind == "service":
        ok = record["status"] == "healthy" and isinstance(record["latency_ms"], (int, float)) and 0 <= record["latency_ms"] <= 5000
    else:
        ok = False
    return ok, RULE

def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    if missing:
        status, reason = "blocked", "missing required fields: " + ", ".join(missing)
    else:
        ok, reason = _rule(record)
        status = "passed" if ok else "failed"
    evidence = {"project": PROJECT, "status": status, "reason": reason, "record": record}
    evidence["evidence_sha256"] = sha256(_canonical(evidence).encode()).hexdigest()
    return evidence

