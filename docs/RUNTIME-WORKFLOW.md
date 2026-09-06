# Local runtime contract

The public workflow is `doctor` or `infer`, implemented by
`local_ai_stack.runtime.run`. Both use the same version → inventory pipeline.
Inference adds lock → generation → completed-response validation. There is no
provider fallback, automatic model pull, installation, shell or subprocess.

The optional same-runtime local model policy is documented in
[Local fallback](LOCAL-FALLBACK.md). Without that explicit input, this v1
workflow and its refusal behavior remain unchanged.
One transport defect is corrected for both versions: a JSON-looking response
whose declared HTTP body was not completely received is refused, even if its
JSON fragment contains `done=true`. Valid v1 receipts retain their schema.

Inputs: exact nonempty printable model tag, HTTP local base, optional nonempty
prompt up to 32768 bytes, timeout in (0,180] seconds and token budget 1–2048.
The default timeout is 30 seconds and output budget 128 tokens. Bodies are
bounded at 1 MiB; duplicate keys, non-finite numbers and non-object responses
are invalid. `/api/version`, `/api/tags`, `/api/generate` are this workflow's
routes. The separate explicit observation operation adds read-only
`GET /api/ps` to the native transport allowlist; it is never called by these
unchanged `doctor`/`infer` flows. See [observations](LOCAL-OBSERVATIONS.md).

`available` records an installed model and a responding runtime; it does not
claim inference readiness. `completed` additionally requires `done=true`,
the exact returned model, nonempty string output and a bounded integer token
count. `blocked`/`timeout` contain no answer. Error bodies and arbitrary
exception messages are not exposed. Version/model labels are observations of
the contacted server, not independent verification of its model weights.

An evidence SHA binds this response, including its measurements. It is an
integrity digest, not a signature or proof of authorship. No prompt journal is
written by this module. Successful inference returns its requested text to the
caller and includes only digests/counts for audit consumers.

The coordination implementation was adapted from the separately reviewed
SKYOM Ollama coordinator. The algorithm and Windows/POSIX checks are retained;
the application data-root import was replaced by `LOCAL_AI_STATE_ROOT` and the
default POSIX group name made generic. No original module was modified.
Sharing an existing deployment requires matching its lock path and any shared
mode/GID configuration. New lock files are not substitutes for an existing
lock owned by another runtime.

Tests use synthetic transport, bounded HTTP counterexamples, deadlines, lock
contention, exact model checks and the public CLI. A real local observation is
a separately dated operational proof; it is never baked into readiness.
