# Contributing

Thanks for helping. A few project-specific rules matter more than style —
read them before your first PR.

## Dev environment

```bash
uv sync            # python >=3.12, hatchling build, dev deps in [dependency-groups]
make lint          # ruff check + ruff format --check (spike/ is excluded)
make test          # pytest tests/unit tests/integration — MUST pass with no
                   # cloud credentials and no Modal connection
make test-e2e      # Playwright against the local mock API (needs Node 22)
```

Local control plane without Modal:

```bash
SBX_BACKEND=local uv run python -m control.app
# or the e2e harness: uv run python tests/e2e/serve_local.py
```

`make test` must never open a real Modal connection — `modal` is only
imported in Modal-specific paths (the backend, Modal-backed stores, image
build, deploy app, host-run e2e gates) and tests must not trigger them.

## Test isolation (hard rules)

- `tests/conftest.py` strips host credentials and isolates `HOME`/XDG. Tests
  must not read, print, or inherit real credentials.
- `LocalProcessBackend.exec` inherits only a whitelist (`PATH`, `HOME`,
  `LANG`) plus `SandboxSpec.env` / explicit `env=` — tests pass every needed
  variable explicitly.
- Provider fakes live in `tests/fakes/` with scenario env vars
  (`FAKE_CODEX_SCENARIO`, `FAKE_AGY_SCENARIO`, …: `success` / `resume` /
  `nonzero` / `hang` / `badjson` / `slow` / `auth_invalid`). Extend
  **scenarios**, never rename events, exit codes, or path semantics.
- Token/password/secret fields in fixtures are always the literal
  `REDACTED`.

## Provider adapters

Each provider implements the frozen `AgentAdapter` Protocol
(`runtime/runner/adapter.py`):

```python
provider: str
credential_files: tuple[str, ...]        # relative to $HOME, restored @0600
def prepare_home(home, model): ...       # config/instructions before turn 1
def first_turn_argv(prompt, model): ...  # argv for turn 1
def resume_argv(prompt, session_id): ... # argv for follow-ups
def translate(raw_line): ...             # native line -> 0..n canonical events
def extract_session_id(events): ...      # native session id, if seen
def health_from(exit_code, stderr_tail): ...  # ok|auth_invalid|rate_limited|unknown
```

Adapter expectations (learned from real-CLI spikes — keep them honest):

- One process per turn, **stdin closed**; prompt as positional arg, never `-`.
- Unknown-but-parseable JSON lines return the NOOP event (forward
  compatibility); only truly unparseable lines count as bad JSON.
- A stale resume id must fail the turn — never silently fork the session
  onto a new native id.
- Strip alternate auth channels from the child env (`ACP_BACKEND`,
  `DEVIN_*`, `WINDSURF_*`, `GROK_*`, `XAI_*` as applicable).
- Fixtures for new providers follow `tests/fixtures/events/<provider>/*.jsonl`.

## Real-credential tests

- Real-account gates (`tests/e2e_modal/*_gate.py`, `spike/`) run on the host
  with your own credentials. They must: check prerequisites up front and exit
  `SKIP` (code 2) rather than fake a PASS; never print credential material
  (sha256-16 fingerprints and booleans only); capture `export-credentials`
  output without echoing; and leave zero sandboxes behind.
- A missing credential is `CREDENTIAL_DEFERRED`, never a PASS — and never a
  failure for unrelated lanes.

## Contracts

`docs/contracts/*`, `control/backend.py`, `control/ports.py` and
`runtime/runner/adapter.py` are frozen per release. Behaviour that crosses
packages must match the canonical blocks (`canonical-yaml` / `x-canonical`);
`tests/unit/test_contract_consistency.py` enforces it. Contract changes are
proposed in an issue, not edited in a feature PR.

## PR checklist

- `make lint` and `make test` green, no cloud credentials required.
- No credentials, tokens, or real account data anywhere in the diff.
- Docs updated if behaviour, env vars, commands, or the provider matrix moved.
