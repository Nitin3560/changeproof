# ChangeProof

ChangeProof will detect and reproduce idempotency and duplicate-side-effect
regressions in Python backend services. A suspicion is a candidate; a reproducible
failing test is evidence.

## Current status: Phase 2, Part 1

The local instrumented prototype is implemented: configuration, PostgreSQL payment
benchmarks, deterministic campaigns, verification CLI, evidence, and reproduction
tests. Static Python and Git analysis now identifies candidate race windows.
Automatic instrumentation and analysis-driven scenarios are future work.
A valid configuration does not mean a service passed verification.

## Setup

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
changeproof validate --config configs/payment-idempotency.yaml
python -m unittest discover -s tests -v
```

`python -m changeproof` also runs the CLI. Validation returns exit code 0 for valid
configuration and 2 for invalid configuration. Unknown fields, duplicate YAML keys,
duplicate invariant IDs, and unsupported invariant types are rejected.

The oracle counts actual fake-gateway charge attempts for an idempotency key,
with at most one permitted. It must not count only payment rows or HTTP successes.
Campaign bounds are 2–100 concurrent requests and 1–1,000 iterations.

## Four-part Phase 1 plan

Each part is implemented, tested, reviewed, and pushed separately before the next.

1. **Foundation:** package, validated YAML invariant, CLI, and setup documentation.
   Acceptance: valid example loads; invalid or ambiguous inputs fail clearly.
2. **Benchmark services:** FastAPI vulnerable/corrected implementations, PostgreSQL,
   durable fake-gateway charge ledger, Docker environment, and baseline tests.
   Acceptance: both versions run and sequential duplicates cause one charge.
3. **Deterministic campaign:** single payment, sequential duplicates, concurrent
   duplicates, test-only barriers, bounded timeouts, and isolated state per case.
   Acceptance: the same campaign reproduces duplicates in the vulnerable version
   and no duplicate charges in the corrected version; barrier failures are errors.
4. **Evidence and CLI verification:** local repository targeting, saved execution
   traces, reproducible failing test, JSON/Markdown reports, and benchmark checks.
   Acceptance: a local command produces independently inspectable evidence for the
   vulnerable service and reports “No violations reproduced” for the corrected one.

Passing a bounded campaign never proves correctness for all executions. Crash
recovery, uncertain external outcomes, arbitrary repository instrumentation, AST
analysis, LLM integration, and GitHub integration are outside this first milestone.

## Run the benchmark services

Requires Docker Desktop (or Docker Engine with Compose).

```sh
docker compose up --build -d --wait postgres gateway vulnerable corrected
docker compose --profile test run --rm tests
```

The vulnerable service listens on `http://localhost:8000`, the corrected service
on `http://localhost:8001`, and the fake gateway on `http://localhost:8002`.
All published ports bind to loopback. These are disposable local benchmarks.

```sh
curl -X POST http://localhost:8001/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{"account_id":"A100","amount":500}'
curl 'http://localhost:8002/charges?variant=corrected&idempotency_key=demo-1'
```

Repeat the POST with the same key and payload to receive the saved result. A reused
key with a different payload returns 409. Pending or uncertain corrected payments
also return 409. Invalid input returns 422. The corrected service commits an atomic
PostgreSQL claim before contacting the gateway; only its owner can charge.
The vulnerable service deliberately checks, charges, then saves without that claim.

The gateway commits each charge in its own database transaction, independently of
payment state. It deliberately does not deduplicate requests. Charge records are
namespaced by service variant so the two benchmarks do not share idempotency keys.
The database volume persists across restarts; test cases use fresh UUID keys.

```sh
docker compose down
# Remove all benchmark data when you want a fresh database:
docker compose down -v
```

For local Python development, install `python -m pip install -e '.[benchmark]'`.
Set `DATABASE_URL` to a disposable PostgreSQL database before running the unit-test
command to include integration tests. Without it, database tests are skipped.
The Compose test command always sets it and runs the PostgreSQL tests.

The corrected service is a concurrency benchmark, not a production payment system.
An uncertain gateway outcome leaves the claim reserved, preventing another charge
but requiring recovery that is outside Phase 1. The campaign forces the vulnerable
interleaving with barriers; ordinary concurrent requests alone are not deterministic.

## Run a deterministic campaign

```sh
docker compose --profile test run --build --rm tests python -m changeproof.execution.benchmark --variant vulnerable
docker compose --profile test run --rm tests python -m changeproof.execution.benchmark --variant corrected
```

These commands run the same configured campaign against the instrumented sample
apps in the test container, using ASGI HTTP requests and real PostgreSQL. They do
not target the separately running Uvicorn containers or arbitrary repositories.
Only PostgreSQL must be running; Compose starts it automatically.

Each iteration runs one valid payment, two sequential duplicates (when enabled),
and the configured number of concurrent duplicates (when enabled). Every case
gets a fresh UUID key. Concurrent requests pause immediately after lookup until
all requests arrive, before any can reserve a key or charge. The same checkpoint
is used in both variants. Instrumentation is injected through the app factory;
normal service startup exposes no scheduling endpoint and enables no hooks.

The default configuration runs 100 iterations: 300 cases per variant. Expected:
100 reproduced violations for the vulnerable variant and zero for the corrected
variant. Exit codes are 0 for no violation reproduced, 1 for reproduced violations,
and 2 for execution/configuration errors. A campaign stops at its first execution
error. A case has a 15-second deadline covering requests, barriers, and ledger reads;
pending request tasks are cancelled and awaited on failure.

The gateway ledger decides whether duplicate effects occurred. A valid payment
must also complete; zero charges or unexpected HTTP errors are execution errors.
Concurrent corrected requests may return 409 while the owner completes. Multiple
recorded effects remain a violation even when requests return HTTP errors.

The Python `run_benchmark` API returns case results with request responses, charge
records, and an ordered checkpoint trace. Traces capture the enforced lookup
ordering and charge/save checkpoints; they are not a complete database schedule.
The verify command saves these results and produces reproduction artifacts and reports.

## Verify a local target and save evidence

```sh
docker compose --profile test run --build --rm -v ./reports:/app/reports tests changeproof verify --repo ./examples/payment_service/vulnerable --config ./configs/payment-idempotency.yaml --output ./reports/vulnerable
docker compose --profile test run --rm -v ./reports:/app/reports tests changeproof verify --repo ./examples/payment_service/corrected --config ./configs/payment-idempotency.yaml --output ./reports/corrected
```

Each invocation creates a unique `run-*` directory with `report.json` and
`report.md`. Reproduced violations also get `CP-001.json`, `CP-001.md`, and
`CP-001-repro.py` (and subsequent IDs). JSON includes the configuration, adapter
hash, actual charges, HTTP responses, and ordered checkpoint trace. Reports
separate reproduced violations, no violation reproduced, and execution errors;
an incomplete campaign is never reported as passing.

A regression test uses the recorded scenario and request count with a fresh key.
To replay in Docker, replace the path below with your saved run directory:

```sh
docker compose --profile test run --rm -v ./reports:/app/reports tests python /app/reports/vulnerable/run-REPLACE/CP-001-repro.py
```

The test fails with an assertion showing the duplicate charge count. It must not
fail because a dependency or database is unavailable. For local Python execution,
install `.[benchmark]`, set `DATABASE_URL`, and run the saved script. Target paths
in saved scripts are absolute; replay in the same checkout/container layout, or
edit the path after relocating the checkout. Replays use current source code, so
a corrected implementation may make the saved regression test pass.

`--repo` identifies a local directory exporting `changeproof_target.py`, not a
remote Git URL. The adapter must export `VARIANT` (`vulnerable` or `corrected`)
and `create_app(store, gateway_transport, hook)`, using the supplied store,
fake gateway transport, and checkpoint hook. The sample adapters demonstrate
this contract. Adapters are trusted Python code executed in the local process;
this is not a sandbox for untrusted repositories.

Each campaign creates and drops its own PostgreSQL schema, including on ordinary
execution errors. The configured database user needs schema creation permissions.
Evidence is copied into results before schema cleanup; there are no campaign
charges left in the persistent demonstration services' ledger. Forced process
termination may leave an unused `cp_*` schema requiring manual removal.
Case keys are also unique. PostgreSQL connection, statement, and lock waits are
bounded. The benchmark gateway has no real payment integration.

The first milestone is complete for these instrumented targets. Passing results
apply only to the configured bounded schedule. Crash recovery, external provider
ambiguity, automatic repository instrumentation, and LLM-generated scenarios remain
outside this prototype.

## Two-part Phase 2 plan

1. **Code analysis (implemented):** extract Python function facts, configured route
   matches, direct local call relationships, and Git change locations. Report
   candidate read/branch/charge/write patterns with source locations and scenario
   recommendations. Acceptance: inspect source without executing it, narrow to
   changed functions, and keep every static finding unconfirmed.
2. **Targeted scenario generation (next):** connect analysis to the verifier,
   introduce property-based input/action generation, validate LLM-proposed
   hypotheses and scenarios against a bounded schema, and benchmark failures beyond
   the current templates. Acceptance: an independently reproduced defect requiring
   generated inputs or actions, with evidence and a runnable regression test.

Part 1 does not invoke an LLM or generate property-based campaigns. Those belong
to Part 2, where candidate suggestions must never change the explicit oracle.

## Analyze source without running it

The analyzer uses the Python standard library. It needs neither the target's
installed dependencies, a target adapter, nor a running database.

```sh
changeproof analyze --repo ./examples/payment_service --config ./configs/payment-idempotency.yaml --output ./reports/analysis.json
changeproof analyze --repo . --base HEAD~1 --config ./configs/payment-idempotency.yaml --output ./reports/changes.json
```

Without `--base`, all Python files under the selected directory are inspected.
With `--base`, Git compares that revision to the current worktree, including
staged, unstaged, and untracked Python files. Paths in Git mode are relative to
the Git root. Functions intersecting changed lines are selected; unchanged
functions in changed files may appear as endpoint or call-graph context. Removed
functions have no current body to analyze. An invalid base is an error, not an
empty successful report. The Git executable must be installed for this mode.

JSON includes function/decorator locations, literal route mappings, assignments,
ordered calls, conditions, exception handlers, lexical and `self`/`cls` call
links within a file, and candidate race windows. Candidates recommend two
concurrent requests at `after_key_lookup`; execution and report linking are still
separate operations. The analyzer does not add hooks to target code.

A candidate requires a recognized read, a conditional, a charge call, and a later
write in one function. Charge names and literal `post("/charges")` calls are
recognized; database names and simple literal payment SQL supply read/write
heuristics. Observed reservation calls are recorded, but do not prove safety or
suppress candidates. The shared sample handler contains both vulnerable and
corrected branches, so it remains a candidate until a campaign tests a variant.

These are syntactic heuristics: no cross-module propagation, alias/type resolution,
branch feasibility, router-prefix resolution, or atomicity proof is claimed.
Candidates can be false positives; unfamiliar wrappers can be missed. Finding
nothing is not evidence of correctness. Analysis exits 0 when inspection completes,
even with candidates, and 2 for invalid input, Git errors, or incomplete parsing.
Syntax/encoding/read errors are included in `diagnostics` and mark the report
incomplete. Files over 1 MB are reported as incomplete; scans over 1,000 Python
files are rejected. Symlinks and common dependency/build directories are skipped.
