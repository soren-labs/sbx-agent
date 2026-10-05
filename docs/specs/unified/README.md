# Unified replacement specs

Executable replacement specs for the [unified architecture RFC](../../architecture/unified/README.md).
They describe what this implementation actually does; the RFC remains the normative target.
Legacy `docs/contracts/**` stay untouched until the R7 contract retirement.

| Spec | Subject |
| --- | --- |
| [authority.md](authority.md) | Single-writer table, transactions, dedupe windows, Job claim protocol |
| [persistence.md](persistence.md) | Schema/migration conventions and invariants enforced by PostgreSQL |

Later phases add runtime protocol, Harness manifest, manifest digest vectors, OpenAPI and result
contract schemas to this directory.
