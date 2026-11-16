# ChangeProof

ChangeProof will detect and reproduce idempotency and duplicate-side-effect
regressions in Python backend services. A suspicion is a candidate; a reproducible
failing test is evidence.

## Current status: Phase 3 code implemented; live App setup pending

The local instrumented prototype is implemented: configuration, PostgreSQL payment
benchmarks, deterministic campaigns, verification CLI, evidence, and reproduction
tests. Python/Git analysis now feeds bounded generated campaigns, property-based
search and shrinking, and optional validated model suggestions. Automatic
instrumentation and arbitrary repository execution remain future work. GitHub
webhook intake persists allowlisted PR jobs, and an isolated worker can verify
approved snapshots and publish GitHub Checks. Live App registration, installation,
and deployment still require operator configuration.
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
ambiguity, automatic repository instrumentation, and multi-service workflows remain
outside this prototype.

## Two-part Phase 2 plan

1. **Code analysis (implemented):** extract Python function facts, configured route
   matches, direct local call relationships, and Git change locations. Report
   candidate read/branch/charge/write patterns with source locations and scenario
   recommendations. Acceptance: inspect source without executing it, narrow to
   changed functions, and keep every static finding unconfirmed.
2. **Targeted scenario generation (implemented):** connect analysis to the verifier,
   introduce property-based input/action generation, validate LLM-proposed
   hypotheses and scenarios against a bounded schema, and benchmark failures beyond
   the current templates. Acceptance: an independently reproduced defect requiring
   generated inputs or actions, with evidence and a runnable regression test.

Part 1 supplies source facts. Part 2 turns candidates and optional suggestions
into tests; suggestions cannot change the explicit oracle.

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
concurrent requests at `after_key_lookup`; the generated verification mode executes these probes and links the analysis
to reports. The analyzer does not add hooks to target code.

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

## Search generated inputs and shrink counterexamples

Install `python -m pip install -e '.[benchmark,generation]'` for local use.
The Docker test image includes all optional dependencies.

```sh
docker compose --profile test run --build --rm -v ./reports:/app/reports tests changeproof verify --generated --repo ./examples/payment_service/amount_sensitive --analysis-root ./examples/payment_service --config ./configs/payment-idempotency.yaml --max-examples 30 --seed 0 --output ./reports/generated
```

The amount-sensitive benchmark deliberately claims keys only for amounts below
1000. The original templates always send 500, so they miss this defect. Generated
verification discovers and shrinks a duplicate-charge counterexample to two
concurrent requests at amount 1000, then independently replays it with a fresh key.
Run the same command with `--repo ./examples/payment_service/corrected` as a control.
Saved regression tests preserve the exact generated amount, account ID, duplicate
count, and schedule; they need no model to replay.

Static candidates add a two-request concurrent probe when enabled. Hypothesis then
searches amounts 1–2000, alphanumeric account IDs of 1–12 characters, sequential
repeats of 2–10 requests, and concurrent repeats of 2–min(10, configured bound).
Only configured scenario types are explored. `--analysis-root` defaults to the
adapter directory; point it at shared source to include the payment handler.
Optional `--base REV` restricts analysis to Git changes. An empty candidate set
still permits property search; incomplete analysis prevents generated execution.

Generated mode uses `--max-examples` (1–100, default 30), not the template iteration
count. Shrinking and probes add executions beyond this search budget, capped at
min(500, max(50, 5 × max-examples)) total executions. Exceeding that cap is an
execution error and incomplete campaign, never a passing search. Each case keeps
its existing timeout and disposable schema. Search stops after a shrunk
counterexample; otherwise reports say no counterexample was found within budget.
A seed helps repeat the search, but package versions and source changes can alter
Hypothesis exploration. Saved concrete replays do not depend on that exploration.

Reports include analysis, suggestion provenance, seed, budgets, search outcome,
minimized input, and each executed input. There may be several violations from
search and shrinking; the last finding replays the independently checked minimized
case when `search_status` is `counterexample_found`. Passing these synthetic
benchmarks does not establish real-world accuracy or correctness.

## Optional model suggestions

No API call is made by default. To use an existing model-produced JSON file:

```sh
changeproof verify --generated --repo ./examples/payment_service/amount_sensitive --analysis-root ./examples/payment_service --config ./configs/payment-idempotency.yaml --suggestions ./configs/candidate-suggestions.json --output ./reports/suggested
```

The checked-in file is a hand-written format example, not evidence of a model run.
Its schema permits at most ten candidate hypotheses and only supported duplicate
scenarios, bounded inputs, and the known lookup checkpoint. Unknown fields, shell
commands, invariant overrides, unsupported hooks, confirmed labels, and bounds
exceeding configuration are rejected. Accepted hypotheses stay unconfirmed until
actual gateway effects reproduce a violation.

For optional live suggestions, install `.[benchmark,generation,llm]`, set
`OPENAI_API_KEY`, and replace `--suggestions ...` with `--llm-model MODEL_NAME`.
Choose a model supporting structured outputs. This explicit option sends bounded
analysis facts (including conditions and call names) to OpenAI. The client has a
30-second timeout, no automatic retries, no tools, and `store=False`. It uses the
[Responses structured-output API](https://developers.openai.com/api/docs/guides/structured-outputs),
then applies local schema and campaign-policy validation. Refused, incomplete, or
invalid output is an error. Model confidence never confirms a bug, and model text
is never executed. Live calls require your credentials and were not part of the
local benchmark; SDK request/response handling is tested with mock HTTP responses.

## Two-part Phase 3 plan

1. **GitHub App intake (implemented):** authenticate webhook deliveries, validate
   installation/repository scope, normalize pull-request revisions, and persist
   deduplicated jobs. Acceptance: valid events survive restarts, concurrent
   redeliveries enqueue one job, and invalid signatures never queue work.
2. **PR verification and Checks (implemented; live setup pending):** add GitHub App authentication, claim jobs
   in a worker, confirm the PR still targets the queued revision, run bounded
   verification for explicitly approved targets, and publish GitHub Checks with
   evidence. Acceptance: an installed app completes a check on the correct PR
   commit, handles retries/stale work, and never reports an incomplete run as passed.

Part 1 is a local intake service. Part 2 supplies the worker and Checks client.
No live GitHub App has been registered, installed, or deployed by these changes.

## Run GitHub webhook intake

For local Python use, install `python -m pip install -e '.[github]'`. Configure:

- `CHANGEPROOF_GITHUB_WEBHOOK_SECRET`: a random secret of at least 32 characters.
- `CHANGEPROOF_GITHUB_ALLOWLIST`: a JSON list of installation/repository bindings.
  Each entry has `installation_id`, `id` (repository ID), and `full_name`.
- `CHANGEPROOF_GITHUB_QUEUE_PATH`: SQLite file path, default `work/github-jobs.sqlite3`.

For example, after replacing the installation ID with your app installation ID:

```sh
export CHANGEPROOF_GITHUB_WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export CHANGEPROOF_GITHUB_ALLOWLIST='[{"installation_id":123456,"id":1460578755,"full_name":"Nitin3560/changeproof"}]'
uvicorn changeproof.github.app:create_app --factory --host 127.0.0.1 --port 8080
```

Keep the same secret when restarting and configure that value in your GitHub App.
The service will not start with a missing/short secret or empty allowlist. It needs
no GitHub token or private key in Part 1, because it makes no GitHub API calls.
The allowlist IDs must match the authenticated payload, including the base repo;
matching only a repository name is insufficient.

Alternatively, with the environment variables exported:

```sh
docker compose --profile github up --build -d --wait github-intake
docker compose --profile github exec github-intake changeproof github-jobs --database /data/github-jobs.sqlite3
docker compose --profile github down
```

The intake profile requires no PostgreSQL service. It binds port 8080 to loopback
and saves SQLite state in the `github-queue` Docker volume. `down` preserves that
volume; `down -v` removes it. Use a persistent local disk for a non-Docker queue.
The SQLite queue supports concurrent local processes, not multiple hosts sharing
a network filesystem.

`POST /github/webhooks` verifies HMAC-SHA256 over the exact raw bytes using the
`X-Hub-Signature-256` header before parsing JSON. Bodies are capped at 1 MiB,
including streamed requests. `X-GitHub-Delivery` must be a UUID; `X-GitHub-Event`
selects the event type. Duplicate JSON keys and inconsistent PR identities are
rejected. The receiver follows GitHub's
[signature-validation guidance](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
and [quick acknowledgement/queue guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks).

Supported pull-request actions are `opened`, `synchronize`, `reopened`, and
`ready_for_review`. Only open, non-draft PRs from the same allowlisted repository
are queued. Forks, missing head repositories, drafts, other actions, and other
events are acknowledged as ignored. Signed `ping` events are acknowledged too.
GitHub may include additional fields; unused fields such as PR descriptions are
not persisted. Repository names are normalized to lowercase. Commit SHAs must
be full 40-character hexadecimal values.

Successful acknowledgements return 202 with `outcome`, `job_id`, `reason`, and
`duplicate`. Jobs are identified by installation, repository, PR number, and both
base/head SHAs. The same delivery or the same revision pair does not add another
job, even across restarts. A new head or base SHA creates a separate job. Delivery
ID reuse with different content/event returns 409. Invalid signatures return 401,
malformed payloads 422, oversized bodies 413, and unavailable storage 503 with a
retry hint. Receipts and jobs commit in one SQLite transaction before acknowledgement.
Only normalized job metadata and delivery hashes are stored, not raw payloads.

Inspect the queue without starting a server:

```sh
changeproof github-jobs --database ./work/github-jobs.sqlite3 --limit 50
```

This command opens the existing database read-only. It prints JSON and returns 2
if storage cannot be read. `GET /health` reports process liveness and intake-only
mode; it does not promise database availability or successful verification.

For later GitHub App setup, subscribe to pull-request events and use an HTTPS
webhook URL ending in `/github/webhooks` with the configured secret. Pull-request
read access is sufficient for intake; Worker permissions are listed below. Do not treat an acknowledgement as a verification
result. Queued events may arrive out of order, and closed/draft/installation
lifecycle events do not cancel existing jobs in Part 1. The worker rechecks installation access, PR readiness, and current SHAs before
execution and publication. No webhook handler clones repositories, imports adapters, calls an
LLM, runs tests, or sends GitHub comments.

## Process approved PR jobs and publish GitHub Checks

Run the worker on the Docker host, separately from the intake service. It needs
the same SQLite queue file, Docker Engine/Desktop, and local trusted policy. It
does not mount the Docker socket into verification containers.

```sh
python -m pip install -e '.[github]'
docker build -t changeproof-worker:local .
docker pull postgres:16-alpine
```

The worker resolves both image references to immutable local image IDs for each
run and uses `--pull never` for execution. Build the verifier image from a trusted
checkout, not the incoming PR. Custom database images must retain PostgreSQL 16
Alpine's UID/GID 70 layout. The runtime supports the controlled instrumented
payment target contract; it does not install dependencies or run repository scripts.

Copy `configs/github-worker.example.json` to an operator-managed policy file.
Replace the sample installation ID and zero SHA with the reviewed PR's full head
SHA. The repository ID/name, adapter path, and source package must match your
controlled target. `config_path` selects the trusted invariant configuration and
resolves relative to the policy file, not the PR checkout. Keep this policy outside
PR control; approval is a local policy entry, not a label or message in a PR.

Set credentials only in the worker host environment:

```sh
export CHANGEPROOF_GITHUB_APP_ID='YOUR_APP_ID'
export CHANGEPROOF_GITHUB_PRIVATE_KEY_PATH='/absolute/path/to/app-private-key.pem'
changeproof github-worker --policy ./configs/github-worker.local.json --database ./work/github-jobs.sqlite3 --reports ./reports/github
```

One invocation handles at most one job; `--watch` polls continuously. It returns
0 for idle/completed/skipped work and 2 for a retryable or terminal worker failure.
A reproduced violation is a completed job with a failing Check, not a worker crash.
If intake runs in Compose, point the host worker at the same queue using a host
bind-mounted queue directory instead of the default Docker-only named volume.
Never copy a live SQLite file without its WAL; both processes should use the same
local disk file. Keep the private key out of the image and source snapshot.

The worker signs a short-lived RS256 App JWT and requests a repository-scoped
installation token with Contents read, Pull requests read, and Checks write.
It checks repository identity and fetches the PR's current base/head, draft/state,
and same-repository identity. Stale or unapproved jobs never execute. It downloads
the exact approved head SHA, accepts only the GitHub codeload redirect, and never
forwards credentials to that redirect host. Archives are limited to 20 MB, 2,000
entries, 100 MB extracted, and 5 MB per file; traversal, symlinks, hard links, and
special files are rejected.

Verification runs as UID 1000 with a read-only root/source/config, dropped
capabilities, no-new-privileges, 1 CPU, 512 MiB memory, 128 PIDs, and bounded tmpfs.
A separate disposable PostgreSQL container uses an internal Docker network with
no published ports or internet route. No host/GitHub/model credentials are passed
to the verifier. The database/network/containers are removed afterward. The
verifier gets a writable output directory; artifact collection rejects links and
bounds file counts and size. Runtime disk quotas are not enforced on that output
directory, so this remains for reviewed controlled commits, not hostile hosted
repositories. Docker is not a complete security boundary for arbitrary code.

The default verifier deadline is 120 seconds (policy range 10–300), enforced by
a container-local timeout as well as the host wait, with separate
bounded database startup, Docker operations, archive transfer, and API calls.
Jobs use atomic 30-minute leases, fencing expired owners from writes. Forced
host termination can leave a disposable database/network behind; operators must
remove such orphan resources. Crashed jobs
are recoverable; transient failures use up to three attempts with bounded backoff.
An interrupted final attempt is finalized as incomplete without another execution.
Check creation uses a stable external job ID and recovers a remotely created Check
after an interruption. Publication retries reuse the Check and saved evidence;
a changed policy/configuration invalidates cached verification.

Before publishing, the worker rechecks both PR revisions and its lease. A changed
PR cancels the old Check. Complete campaigns with reproduced effects publish
`failure`; complete campaigns without violations publish `success`, with the
bounded-campaign limitation. Incomplete campaigns publish `action_required` and
never success. Exhausted infrastructure failures also attempt an `action_required`
conclusion. If GitHub is unreachable, the local job records failure; remote Check
completion cannot be guaranteed until service recovers and an operator retries.

Checks include counts, the tested base/head, a first-violation ledger/trace excerpt,
and the saved regression test when present. Full JSON/Markdown and regression
artifacts stay under the configured reports directory, grouped by job/attempt.
Each report records GitHub revision metadata, trusted configuration fingerprint,
and actual runtime image IDs. There is no public artifact server or upload service
in this prototype. To replay an artifact, restore the recorded snapshot and mount
it at `/source`, or update the saved script's target path for a local checkout.
The worker currently runs the explicit template campaign; generated verification
remains available through the separate local `verify --generated` command.

Inspect job state, attempts, Check ID, and artifact location with `github-jobs`.
After changing an approval or repairing infrastructure, explicitly requeue failed,
skipped, or action-required work:

```sh
changeproof github-retry --database ./work/github-jobs.sqlite3 --job-id JOB_ID
```

Running or successfully completed jobs cannot be requeued by this command. It
retains the Check ID and any usable evidence, while resetting retry counters.
New PR revisions arrive as separate jobs. Repository/installation access is
revalidated for every attempt; removal or revoked credentials prevent execution.

For a live installation, create/install a GitHub App with pull-request events,
Contents read, Pull requests read, Checks write, the intake HTTPS webhook URL,
and the same webhook secret. Set the real installation binding and approved SHA
in both intake/worker configuration. The implementation follows GitHub's
[App JWT guidance](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app),
[installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation),
and [Checks API](https://docs.github.com/en/rest/checks/runs).

Local tests cover real Docker verification of vulnerable and patched snapshots,
plus mocked GitHub HTTP authentication/Check contracts, stale revisions, retries,
lease recovery, and archive restrictions. A live installed-App PR Check remains
unverified until real App credentials and an installation/deployment are provided.
