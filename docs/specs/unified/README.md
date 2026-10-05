# Unified replacement specs

Executable specs for the [unified architecture RFC](../../architecture/unified/README.md).
They describe what this implementation actually does; the RFC remains the normative target.
They replace the retired pre-unification contracts, which are archived read-only under
[`docs/archive/contracts/`](../../archive/contracts/README.md).

| Spec | Subject |
| --- | --- |
| [authority.md](authority.md) | Single-writer table, transactions, dedupe windows, Job claim protocol |
| [persistence.md](persistence.md) | Schema/migration conventions and invariants enforced by PostgreSQL |
| [runtime.md](runtime.md) | `sbx-runtime` protocol v1 between control plane and daemon |
| [connections.md](connections.md) | Product identity, Connections, CredentialVersions and grants |
| [changes-delivery.md](changes-delivery.md) | ChangeSets, Delivery gates and Delegation |
| [console-sdk.md](console-sdk.md) | One API, Console state model and SDK/CLI |
| [openapi.yaml](openapi.yaml) | Generated `/api` contract (`make openapi`; drift-checked) |
| [harnesses/manifests.json](harnesses/manifests.json) | Pinned Harness manifests |
| [manifests/vectors.json](manifests/vectors.json) | Manifest digest test vectors |
