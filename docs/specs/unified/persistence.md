# Unified persistence replacement spec

Implementation baseline: RFC #167 at bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3.
The benchmark is a new disposable cohort, with no production import or traffic.

PostgreSQL is the sole business authority. Migration 001 establishes typed IDs,
ownership FKs, ordinals, append-only journal, immutable version/result evidence,
terminal guards, and partial uniqueness for active Turns, Executions and leases.
Application commands commit events, projections, Jobs and notification outbox
together. Runtime source tuples dedupe separately from committed Session seq.
Command receipts are retained for the lifetime of this MVP database; no expiry
can authorize an irreversible effect to repeat. UUID4 suffixes are used until a
Python UUID7 implementation is available (the RFC's UUID7 recommendation is optional).

The SQL repository is an injected transaction port, not a second domain owner.
Jobs have exactly one typed owned FK matching their declared target family.
Reclaim increments claim generation without changing the external effect ID.
All worker commits must compare holder, generation, state and expiry. Lease
fences are independently enforced by the runtime.

Legacy stores remain solely for baseline regression checks during the stack;
new Session records never read or write them. Final cutover removes them.
