# ChangeProof

ChangeProof will detect and reproduce idempotency and duplicate-side-effect
regressions in Python backend services. A suspicion is a candidate; a reproducible
failing test is evidence.

## Current status: Phase 1, Part 1

The package and explicit invariant configuration are implemented. The CLI validates
configuration only. Service execution and bug verification arrive in later parts.
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
