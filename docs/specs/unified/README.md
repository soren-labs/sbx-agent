# Executable unified replacement specifications

These specifications implement the frozen RFC in `docs/architecture/unified/`.
They describe the new benchmark cohort; legacy contracts remain historical
until phase 6's deliberate retirement. No production state is migrated.

- `persistence.md`: PostgreSQL authority, transactions, claims and retention.
- `events.schema.json`: committed Session journal envelope v1.

Runtime, Harness, manifest, result and OpenAPI contracts are added by their
implementing stacked phases. A contract is evidence of a tested implementation,
not evidence that every RFC release gate is already passed.
