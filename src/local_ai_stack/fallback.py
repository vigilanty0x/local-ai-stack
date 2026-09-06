"""Explicit local candidates under one native lock and one total deadline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from . import runtime as rt

MAX_POLICY_BYTES = 8192
SCHEMA = 'local-ai-stack/fallback-v1'


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def validate_policy(value, primary):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'models'}
            or value['schema_version'] != SCHEMA):
        raise rt.RuntimeErrorDetail('fallback policy invalid')
    models = value['models']
    if (not isinstance(models, list) or not 1 <= len(models) <= 4
            or any(not isinstance(m, str) or not m or len(m) > 200
                   or any(ord(c) < 33 or ord(c) > 126 for c in m) for m in models)
            or len(set(models)) != len(models) or models[0] != primary):
        raise rt.RuntimeErrorDetail('fallback requires 1-4 unique exact tags starting with requested model')
    result = {'schema_version': SCHEMA, 'models': list(models)}
    if len(_encoded(result)) > MAX_POLICY_BYTES:
        raise rt.RuntimeErrorDetail('fallback policy exceeds limit')
    return result


def load_policy(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise rt.RuntimeErrorDetail('fallback policy exceeds limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=rt._strict_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise rt.RuntimeErrorDetail('fallback policy JSON invalid') from exc
    if not isinstance(value, dict):
        raise rt.RuntimeErrorDetail('fallback policy object required')
    return value


def _error(exc):
    return {'error_code': type(exc).__name__,
            'error_detail': str(exc) if isinstance(exc, rt.RuntimeErrorDetail) else 'local runtime operation failed'}


def run_fallback(*, base, model, prompt, timeout, max_tokens, lock_path, transport, policy):
    policy = validate_policy(policy, model)
    if prompt is None:
        raise rt.RuntimeErrorDetail('fallback requires explicit inference prompt')
    # The native transport requires a complete bounded error response before
    # it can classify a terminal rejection. Injected transports remain an
    # explicit testing/integration boundary, as in runtime-v1.
    transport = rt.request_json_terminal if transport is rt.request_json else transport
    started = time.monotonic(); deadline = started + timeout
    result = {'schema_version': 'local-ai-stack/runtime-v2', 'operation': 'infer',
              'model_requested': model, 'model_selected': None, 'model_observed': None,
              'status': 'blocked', 'runtime_observed': False, 'model_installed': False,
              'requested_model_installed': False, 'inference_completed': False,
              'observed_at': time.time(), 'stages': [], 'response': None,
              'provenance': 'local_runtime_observation', 'publication': False,
              'policy': policy, 'policy_sha256': hashlib.sha256(_encoded(policy)).hexdigest(),
              'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
              'persistent_circuit': 'not_implemented',
              'attempts': [{'model': m, 'status': 'not_attempted', 'model_installed': None,
                            'generation_attempted': False, 'elapsed_s': 0.0} for m in policy['models']]}
    current = None
    try:
        version = transport(base, 'GET', '/api/version', None, deadline); rt.remaining(deadline)
        if not isinstance(version.get('version'), str) or not version['version'].strip():
            raise rt.RuntimeErrorDetail('runtime version missing')
        result.update(runtime_observed=True, runtime_version=version['version'][:100])
        result['stages'].append({'stage': 'health', 'status': 'passed'})
        tags = transport(base, 'GET', '/api/tags', None, deadline); rt.remaining(deadline)
        models = tags.get('models')
        if not isinstance(models, list) or len(models) > 1000:
            raise rt.RuntimeErrorDetail('model inventory invalid')
        names = [item.get('name') for item in models if isinstance(item, dict)]
        result['requested_model_installed'] = model in names
        for attempt in result['attempts']:
            attempt['model_installed'] = attempt['model'] in names
            if not attempt['model_installed']: attempt['status'] = 'not_installed'
        result['stages'].append({'stage': 'inventory', 'status': 'passed'})
        if not any(a['model_installed'] for a in result['attempts']):
            raise rt.RuntimeErrorDetail('no declared fallback model is installed')
        with rt.ollama_inference_lock(timeout_s=rt.remaining(deadline), lock_path=lock_path):
            result['stages'].append({'stage': 'lock', 'status': 'passed'})
            for attempt in result['attempts']:
                if not attempt['model_installed']: continue
                current = attempt
                attempt_started = time.monotonic()
                try:
                    rt.remaining(deadline)
                    selected = attempt['model']
                    result.update(model_selected=selected, model_installed=True)
                    attempt['generation_attempted'] = True
                    generated = rt._generate(base, selected, prompt, max_tokens, deadline, transport)
                    output = rt._validated_generation(selected, prompt, max_tokens, generated)
                    rt.remaining(deadline)
                    result.update(output, model_observed=selected)
                    attempt.update(status='completed', response_sha256=output['response_sha256'],
                                   response_bytes=output['response_bytes'], output_tokens=output['output_tokens'])
                    result['stages'].append({'stage': 'inference', 'status': 'passed'})
                    break
                except rt.TerminalHTTPRejection as exc:
                    # No new budget, no sleep/backoff and no model retry.
                    attempt.update(status='terminal_rejection', http_status=exc.status, **_error(exc))
                except Exception as exc:
                    attempt.update(status='timeout' if isinstance(exc, TimeoutError) else 'blocked', **_error(exc))
                    raise
                finally:
                    attempt['elapsed_s'] = round(time.monotonic() - attempt_started, 6)
            if not result['inference_completed']:
                rt.remaining(deadline)
                raise rt.RuntimeErrorDetail('all installed fallback candidates were rejected')
        rt.remaining(deadline)
    except Exception as exc:
        result.update(status='timeout' if isinstance(exc, TimeoutError) else 'blocked', response=None,
                      inference_completed=False, **_error(exc))
        result['stages'].append({'stage': 'inference' if current else 'lock' if result['runtime_observed'] and any(a['model_installed'] for a in result['attempts']) else 'inventory' if result['runtime_observed'] else 'health',
                                 'status': result['status']})
    result['elapsed_s'] = round(time.monotonic() - started, 6)
    result['budget_s'] = timeout
    result['evidence_sha256'] = hashlib.sha256(_encoded(result)).hexdigest()
    return result
