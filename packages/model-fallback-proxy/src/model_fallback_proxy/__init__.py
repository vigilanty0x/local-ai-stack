"""Offline, fail-closed routing over caller-declared model availability."""

import argparse
import hashlib
import json
import re

NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_MODELS = 100
MAX_VALUE = 10_000_000
STATUSES = {"healthy", "degraded", "down"}


def _integer(value, low=0, high=MAX_VALUE):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _capabilities(value):
    return (isinstance(value, list) and len(value) <= 100
            and all(isinstance(item, str) and NAME.fullmatch(item) for item in value)
            and len(value) == len(set(value)))


def route(data):
    if not isinstance(data, dict) or set(data) != {"request", "models"}:
        return {"ok": False, "decision": "blocked", "errors": ["invalid_input"]}
    request, models = data["request"], data["models"]
    if (not isinstance(request, dict) or set(request) != {"context_tokens", "capabilities"}
            or not _integer(request["context_tokens"]) or not _capabilities(request["capabilities"])
            or not isinstance(models, list) or len(models) > MAX_MODELS):
        return {"ok": False, "decision": "blocked", "errors": ["invalid_request_or_models"]}
    parsed, names = [], set()
    keys = {"name", "order", "status", "remaining_requests", "context_limit", "capabilities"}
    for model in models:
        if (not isinstance(model, dict) or set(model) != keys
                or not isinstance(model["name"], str) or not NAME.fullmatch(model["name"])
                or model["name"] in names or not _integer(model["order"], 0, 10_000)
                or model["status"] not in STATUSES
                or not _integer(model["remaining_requests"])
                or not _integer(model["context_limit"])
                or not _capabilities(model["capabilities"])):
            return {"ok": False, "decision": "blocked", "errors": ["invalid_model"]}
        names.add(model["name"])
        parsed.append(model)
    needed = set(request["capabilities"])
    attempts, selected = [], None
    for model in sorted(parsed, key=lambda item: (item["order"], item["name"])):
        reasons = []
        if model["status"] != "healthy":
            reasons.append("unhealthy")
        if model["remaining_requests"] <= 0:
            reasons.append("quota")
        if model["context_limit"] < request["context_tokens"]:
            reasons.append("context")
        if not needed <= set(model["capabilities"]):
            reasons.append("capability")
        attempts.append({"model": model["name"], "eligible": not reasons, "reasons": reasons})
        if selected is None and not reasons:
            selected = model["name"]
    body = {"selected": selected, "attempts": attempts}
    return {"ok": selected is not None, "decision": "ready" if selected else "blocked", **body,
            "route_sha256": hashlib.sha256(json.dumps(body, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()}


def probe():
    model = {"name": "m", "order": 1, "status": "healthy", "remaining_requests": 1,
             "context_limit": 10, "capabilities": ["text"]}
    good = route({"request": {"context_tokens": 1, "capabilities": ["text"]}, "models": [model]})
    bad = route({"request": {"context_tokens": 1, "capabilities": []}, "models": []})
    return {"ok": good["ok"] and not bad["ok"], "no_route_counter_proof": not bad["ok"]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("route", "probe"))
    parser.add_argument("--input")
    args = parser.parse_args(argv)
    try:
        data = json.load(open(args.input, encoding="utf-8")) if args.input else None
        out = probe() if args.command == "probe" else route(data)
    except (OSError, UnicodeError, json.JSONDecodeError):
        out = {"ok": False, "decision": "blocked", "errors": ["input_unreadable"]}
    print(json.dumps(out, sort_keys=True))
    return 0 if out["ok"] else 2
