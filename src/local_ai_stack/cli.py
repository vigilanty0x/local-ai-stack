from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .core import evaluate


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] in {"observe", "benchmark"}:
        return measurement_main(supplied)
    if supplied and supplied[0] in {"doctor", "infer"}:
        return runtime_main(supplied)
    parser = argparse.ArgumentParser(prog="local-ai-stack")
    parser.add_argument("--version", action="version", version=f"local-ai-stack {__version__}")
    parser.add_argument("record", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = evaluate(json.loads(args.record.read_text(encoding="utf-8")))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "passed" else 2


def _emit_json(result):
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


def runtime_main(argv):
    from .runtime import run
    from .fallback import load_policy
    parser = argparse.ArgumentParser(prog="local-ai-stack")
    parser.add_argument("operation", choices=("doctor", "infer"))
    parser.add_argument("--base", default=os.environ.get("CC_OLLAMA_BASE", "http://127.0.0.1:11434"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--fallback", type=Path, help="explicit ordered local model policy; infer only")
    args = parser.parse_args(argv)
    if args.operation == "infer" and args.prompt is None:
        parser.error("infer requires --prompt")
    if args.operation == "doctor" and args.prompt is not None:
        parser.error("doctor does not accept --prompt")
    if args.operation == "doctor" and args.fallback is not None:
        parser.error("doctor does not accept --fallback")
    try:
        optional = {"fallback": load_policy(args.fallback)} if args.fallback is not None else {}
        result = run(base=args.base, model=args.model, prompt=args.prompt,
                     timeout=args.timeout, max_tokens=args.max_tokens, lock_path=args.lock, **optional)
    except (TypeError, ValueError, OSError) as exc:
        result = {"status": "blocked", "error_code": type(exc).__name__, "response": None}
    _emit_json(result)
    return 0 if result["status"] in {"available", "completed"} else 2


def measurement_main(argv):
    from .benchmark import benchmark
    from .observations import observe
    parser = argparse.ArgumentParser(prog="local-ai-stack " + argv[0])
    parser.add_argument("--base", default=os.environ.get("CC_OLLAMA_BASE", "http://127.0.0.1:11434"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=10 if argv[0] == "observe" else 60)
    if argv[0] == "observe":
        parser.add_argument("--sample-ms", type=int, default=250)
        parser.add_argument("--gpu-executable", type=Path,
                            help="operator-selected trusted existing NVIDIA executable; never install")
    else:
        parser.add_argument("--repetitions", type=int, default=1)
        parser.add_argument("--max-tokens", type=int, default=64)
        parser.add_argument("--lock", type=Path)
    args = parser.parse_args(argv[1:])
    try:
        if argv[0] == "observe":
            result = observe(base=args.base, model=args.model, timeout=args.timeout,
                             sample_ms=args.sample_ms, gpu_executable=args.gpu_executable)
            successful = result["status"] == "measured"
        else:
            result = benchmark(base=args.base, model=args.model, timeout=args.timeout,
                repetitions=args.repetitions, max_tokens=args.max_tokens, lock_path=args.lock)
            successful = result["status"] == "completed" and result["measurement_status"] == "measured"
    except (TypeError, ValueError, OSError) as exc:
        result = {"status": "blocked", "error_code": type(exc).__name__, "response": None}
        successful = False
    _emit_json(result)
    return 0 if successful else 2


if __name__ == "__main__":
    raise SystemExit(main())
