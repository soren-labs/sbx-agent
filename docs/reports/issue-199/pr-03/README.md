# PR 3 evidence: Sessions on Machine Slots, real catalog and reasoning effort

Real API, real PostgreSQL, real Modal VMs (nonproduction workspace, Modal SDK 1.6.1) and the
official CLIs, 2026-10-10. Nothing read a profile Volume.

| File | What it shows |
| --- | --- |
| `pr3-parallel.mp4` / `.png` | Live terminal recording of `tests/e2e_modal/slots_parallel_acceptance.py`. |
| `parallel-acceptance.json` | Sanitized report of that run: 30/30 gates, catalogs, per-Turn model/effort, boot IDs, timings. |
| `deepseek-validation.json` | Real `inference_api` validation of a DeepSeek key: per model and protocol the reasoning-off switch is measured as `toggle`. |
| `deepseek-thinking-through-official-clis.json` | The official Codex and OpenCode CLIs run through the SBX Harness adapters against DeepSeek with and without `effort="none"`. |

Parallel Turns (each a real Codex task that runs a shell command in its own VM):

| Round | Slots | Overlap |
| --- | --- | --- |
| 1 | A1 + A2 (one account, two independent logins) | 15.0 s |
| 2 | A1 + B1 (two accounts) | 12.0 s |
| 3 | A1 + A2 + B1 | 7.8 s |

Seven Worker VMs, seven distinct boot IDs, each bound in 5.4-8.4 s, each mounting only its own
Slot Volume, no API key attached, Slots freed afterwards, no VM left running.

Reasoning effort: the same prompt on one model spent 162 reasoning tokens at `low` and 295 at
`ultra`; a value that is not an effort made the CLI exit 1. The provider itself does not refuse an
effort a model does not list, so the server-side catalog check is what enforces it.

DeepSeek through the official CLIs: OpenCode on the chat endpoint 463 reasoning tokens by default
and 0 with `none`; OpenCode on the Anthropic-style endpoint 534 output tokens by default and 2 with
`none`; Codex on the Responses endpoint is unchanged by the setting (416 vs 582), so no control is
offered for Codex with a custom API.
