# Proposal validation record

Date: 2026-10-05. Validation applies to this documentation proposal against unmodified SBX baseline `83317cd8b90b487a01a534ac7c440154efac2d03`; it does not certify a target implementation.

| Check | Result and method |
| --- | --- |
| Scope | Only original Markdown under `docs/architecture/amp-inspired/` is staged. Production source, tests, config, dependencies, schema migrations, frozen contracts and workflows remain unchanged. |
| Source SHAs | All nine recorded Amp commits resolved through GitHub's public commit API and matched local shallow clone HEADs. |
| Pinned source paths | 86 unique pinned GitHub commit/blob/tree links resolved against the corresponding Git object. This verifies exact path/commit contents without treating mutable main links as provenance. |
| Public docs | All 17 listed Amp doc URLs returned HTTP 200 on retrieval. Resolved URLs and SHA-256 HTML hashes are in [research.md](research.md). Historical `/manual/sdk` redirected to `/docs/sdk`. |
| Internal links | All proposal relative Markdown links resolve. Read-only Linear documents and PR #164 metadata were fetched through their authenticated tools; neither was modified. |
| License/provenance | Root license files, package declarations and README declarations distinguished from absent grants. No upstream source/assets/vendor content copied into SBX. |
| Diagrams | All **8 Mermaid diagrams parsed successfully** with Mermaid **11.15.0** in an external Node/jsdom environment: 4 flowcharts, 2 sequence diagrams, 1 state diagram, 1 ER diagram. No claim of pixel-level GitHub rendering verification. |
| `make lint` | Exit 0: Ruff checks passed; 553 Python files already formatted. |
| `make test` | Exit 0: **3,511 passed, 11 skipped, 3 warnings** in 524.50 seconds. |
| Test isolation | Fresh HOME/XDG directories and dependency environment under the external research workspace; subprocess env allowlist excludes real provider/cloud/application credentials. No real Modal/CLI operation was requested. |
| Warning limits | Unmodified baseline suite reported two dependency deprecations and a thread warning from corrupt SessionRecord data (`garbage` field). Checks passed; this docs task does not change or diagnose production behavior. |
| Whitespace / secrets | `git diff --check` and staged checks run before delivery; added docs checked for credential/private-key signatures. Identifiers/hashes are source references, not credentials. |

External repositories, retrieved HTML, research inventories, validation dependencies, diagram sources and test logs stay under `/home/zheng/ai-work/amp-research/`. The final PR/head/file/source manifest is written outside the repository at `/home/zheng/.local/state/sbx-amp-architecture/RESULT.md` after publishing. This PR is a review artifact and is not merged.
