# Explicit local observations and benchmark

These operations use Local AI Stack's existing transport and inference lock.
They do not install tools, pull models, change OS settings, scan private
configuration or add a queue. They are a partial consolidation of local
diagnostics, not proof that all historical tools are functionally integrated.

## Entry points

```python
from local_ai_stack.observations import observe
from local_ai_stack.benchmark import benchmark

snapshot = observe(base="http://127.0.0.1:11434", model="EXACT_INSTALLED_TAG",
                   timeout=10, sample_ms=250, gpu_executable=None)
receipt = benchmark(base="http://127.0.0.1:11434", model="EXACT_INSTALLED_TAG",
                    repetitions=1, timeout=60, max_tokens=64,
                    lock_path="/operator/configured/shared-inference.lock")
```

The examples contain placeholders, not claims that a model or lock exists.
All participating processes must use the same physical inference lock as
`infer`. A benchmark is real requested inference, so it can load an installed
model into RAM/VRAM and consume CPU/GPU. `observe` does not generate or load a
model and does not acquire a write lock. It makes no claim that observations
are an atomic snapshot or that resource use belongs to a particular model.

```text
local-ai-stack observe --model EXACT_INSTALLED_TAG --sample-ms 250 --timeout 10
local-ai-stack benchmark --model EXACT_INSTALLED_TAG --repetitions 1 --max-tokens 64 --timeout 60 --lock /operator/configured/shared-inference.lock
```

CLI exit 0 means all requested observation sections were measured, or all
benchmark attempts completed with usable metrics. Exit 2 includes partial,
unavailable, blocked, timeout and invalid input. No supplied JSON measurement
can turn a failed collector into a success. The historical `record.json`
evaluator and `doctor`/`infer` receipt schemas are unchanged.

## Observation contract

`observe(*, base, model, timeout=10.0, sample_ms=250, gpu_executable=None,
transport=request_json_terminal)` returns schema `local-ai-stack/observations-v1`.
`transport` is the existing trusted Python test/embedding seam, never JSON from
an untrusted caller. Endpoint/model validation is shared with the runtime.
Total requested budget is `(0,10]` seconds; CPU interval `[50,1000]` ms.

All four sections are requested: `cpu`, `ram`, `gpu`, `ollama`. Each has
`status`, `method`, `scope`, `started_at`, `ended_at`, `elapsed_s`, `values` and
`error_code`. Section statuses: `measured`, `partial`, `unsupported`,
`unavailable`, `timeout`, `error`. Missing values are null, never guessed zero.
The overall status is `measured` only when all four sections were measured,
otherwise `partial` or `not_measured`. Overall `health=not_inferred`: a complete
measurement is not automatically a healthy machine. Date fields are UTC
observations of this process; elapsed intervals use monotonic clocks. Short CPU
sample and generation intervals use `time.perf_counter()` for its higher
resolution on older Windows Python versions. Deadlines retain the shared
`time.monotonic()` clock; these two reference points are never subtracted from
each other. A zero or regressing measured interval is refused, never replaced
with an invented positive duration.

| Section | Method and values | Scope and limits |
| --- | --- | --- |
| CPU Windows | Two GetSystemTimes calls; delta kernel+user minus idle already included in kernel | Calling thread's primary processor group; >64 processors may not cover all groups |
| CPU Linux | First aggregate /proc/stat line, eight counters, idle+iowait excluded | Current kernel view; container quota and model attribution not measured |
| RAM Windows | GlobalMemoryStatusEx; total/available/unavailable bytes | Current OS physical-memory view |
| RAM Linux | MemTotal/MemAvailable kB converted to bytes | MemAvailable is the kernel's estimate; never replaced with MemFree |
| GPU NVIDIA | Fixed read-only nvidia-smi query; name, total/used bytes, utilization percent | Driver-visible GPU snapshot, no process list/UUID/serial; unsupported vendors remain unknown |
| Ollama | GET version/tags/ps; requested model installed/resident and reported resident VRAM | Same allowed local origin; no independent weights verification or inference-readiness claim |

CPU retains actual `interval_s`, requested interval and aggregate deltas. Zero
denominators, changed field counts, impossible idle or regressive counters
(including Linux iowait) refuse the calculation. RAM missing fields and
available > total are errors. Data is read with fixed size limits (CPU 4 KiB,
RAM 64 KiB); no other /proc paths are traversed.

GPU requires `--gpu-executable ABSOLUTE_PATH` / `gpu_executable`. Only the
operator may select this trusted native executable; an HTTP consumer must not
expose the path as arbitrary client input. Path components must not redirect
through symlinks/reparse points. Identity is checked before and after running,
not authenticated by a software signature. There is no PATH discovery or
automatic installation. Exact arguments are
`--query-gpu=name,memory.total,memory.used,utilization.gpu` and
`--format=csv,noheader,nounits`; no shell or user arguments. Both pipes are
drained concurrently with a combined 64 KiB retained-output limit. A ≤2 s
execution budget reserves cleanup time in the caller budget. The receipt
includes exit status and `process_termination_confirmed`; unknown termination
is an error. N/A values remain null. No configured executable means
unavailable, not “no GPU”. At most 16 records are accepted.

On Windows the GPU child receives only `LC_ALL`, `LANG`, the existing
`SystemRoot`/`WINDIR` entries when present, and `ProgramFiles`. The latter is
resolved by `SHGetKnownFolderPath(FOLDERID_ProgramFiles)` from system DLLs;
the inherited `ProgramFiles`, `ProgramW6432`, `PATH`, profile and application
variables are not copied. Known-folder lookup uses the default path without
creating a directory, then refuses nonlocal, missing or redirected paths.
Failure stops the observation before launching the child. This supplies the
Windows NVIDIA initialization prerequisite without broadening executable
lookup, arguments, output limits or the deadline. It is not a driver health
attestation. POSIX child environments are unchanged.

API references: [SHGetKnownFolderPath](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath),
[FOLDERID_ProgramFiles](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid),
[known-folder flags](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/ne-shlobj_core-known_folder_flag).

Ollama inventory replies are bounded to 1 MiB and 1000 unique entries. The
output contains only the requested tag, never other installed tag names. A
successful version/tags stage remains visible if `/api/ps` fails. A missing
resident model is a measured absence, not a runtime failure. Optional model
digest and VRAM values are server declarations, not hardware measurements.
The small service diagnostic ports Service Doctor's healthy observation and
≤5000 ms condition; it does not change the global measured/unknown semantics.

## Benchmark contract

`benchmark(*, base, model, repetitions=1, timeout=60.0, max_tokens=64,
lock_path=None, transport=request_json_terminal)` returns schema
`local-ai-stack/benchmark-v1`. Repetitions `[1,3]`, tokens `[1,128]`, global
budget `(0,180]` seconds. No fallback policy is accepted. One version and
inventory lookup precede one acquisition of the existing lock held across
all requested attempts. Each POST uses the fixed versioned synthetic prompt,
non-streaming native generation and temperature zero. No hidden warmup or
automatic retry. A timeout does not prove cancellation of work inside Ollama.

Each attempt remains `not_attempted`, `started`, `completed`, `blocked` or
`timeout`, with its own dates and elapsed interval. A complete generation
requires the native exact-model/done/nonempty-output/bounded-token contract.
Only a hash and byte count are returned for benchmark text. Overall `completed`
requires every attempt and successful lock exit. `measurement_status` is a
separate `measured`/`partial`/`not_measured` decision. Missing/zero timing can
leave all generations complete while their benchmark remains partial.
Malformed timing stops the benchmark after retaining any already established
generation completion; future attempts remain unattempted.

Metrics preserve client monotonic latency separately from `eval_count` and
`eval_duration`, `total_duration`, `load_duration`, `prompt_eval_duration`
reported by the server. Durations must be integers in `[0,86400e9]` ns and
components must not exceed total. No independent token counting is implied.
`server_tokens_per_second` uses reported eval duration; the client variant
uses measured request latency but still uses the server's token count.
Both carry that provenance. Missing timing or zero output never passes the
benchmark rule. Aggregates show valid sample count and requested count,
including after an interrupted series. A partial series is never globally
green. Local Model Benchmark's positive throughput / ≤60000 ms latency rule
is retained only as a diagnostic of usable measured inputs, not model quality.

Receipt SHA-256 binds its fields but is not a signature or proof of the
producer's identity. `quality=not_measured`, no external publication and no
installed-model weight attestation. The module does not persist reports;
the caller decides where to retain them without altering their provenance.

## Scope and sources

CONS‑10's Docker health/resource attribution, port owners, configuration drift
and all eight historical version-CLI diagnostics remain unmeasured. The
container-resource-profiler source requires I/O/network/startup measurements;
none is filled with fabricated zero. Its declared-data evaluator is not a
collector. AI Setup Doctor already executes version commands, but its full
captured output precedes truncation; that executor is not reused here.

Historical rule provenance: service-doctor commit
`05be8b5b3d4e74267543bbcd156412212157d982` and local-model-benchmark commit
`1e5ab13d37a794f8f756eb5760871ff8f2f3806d` (Apache-2.0). Tests compare the
actual retained source functions on positive and negative inputs. They do not
claim the two projects' unrelated branches or all historical APIs are ported.

Primary API references: [Windows CPU](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes),
[Windows RAM](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-globalmemorystatusex),
[Linux proc](https://docs.kernel.org/filesystems/proc.html),
[NVIDIA query](https://docs.nvidia.com/deploy/nvidia-smi/index.html),
[Ollama residency](https://docs.ollama.com/api/ps),
[Ollama timing](https://docs.ollama.com/api/generate).
