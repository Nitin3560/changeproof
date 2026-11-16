# ChangeProof

ChangeProof will detect and reproduce idempotency and duplicate-side-effect
regressions in Python backend services. A suspicion is a candidate; a reproducible
failing test is evidence.

## Current status: Phase 1, Part 2

The package, invariant configuration, and PostgreSQL-backed benchmark services are
implemented. The CLI validates configuration only. Automated campaigns and bug
verification arrive in later parts.
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
but requiring recovery that is outside Phase 1. Part 3 will force the vulnerable
interleaving with barriers; ordinary concurrent requests alone are not deterministic.
