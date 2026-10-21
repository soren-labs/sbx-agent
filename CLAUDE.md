# Claude Code entry point

Read `AGENTS.md` first. Its repository boundaries, engineering requirements, testing, review severity and production rules remain in force. This file only routes the current SBX work; it does not replace those requirements.

For the open-source backend work established on 2026-10-21, then read:

1. `docs/handoff/oss-2026-10-21/CODEX_HANDOFF.md`
2. `docs/handoff/oss-2026-10-21/OSS_BACKEND_TASKS.md`
3. The task assigned by the Mac Codex coordinator and the relevant implemented specifications.

Claude Code is the preferred feature developer on Ubuntu. Mac Codex coordinates and independently verifies the work. Perform only the assigned work package in its own workspace/branch; request a scope adjustment through the coordinator when dependencies or shared contracts require it. Parallel subtasks may stay within the assigned package; cross-package scheduling belongs to the coordinator.

First finish environment handoff. Initial product work targets B1, B2 and B4 (self-hosted backend, persistent machines and external MCP), with API consistency checked under B5. A roadmap is not an instruction to start all packages at once.

Client implementation is paused pending the owner's Claude Design or other human-led redesign. Preserve the existing SwiftUI prototype. SaaS is paused; retain the shared persistence, recovery, authentication and credential boundaries. A developer CLI login is not proof that SBX supports that subscription.

Latest user instructions define scope. Preserve existing changes, report verified results and concrete blockers, and return implementation for independent Codex acceptance. Do not change product code merely because the environment setup or this entry file exists.
