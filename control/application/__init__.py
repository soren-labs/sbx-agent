"""Application services — the single writer/authority model (RFC 167 §04).

Every mutation runs inside one Unit of Work: authorize scoped rows → enforce
dedupe → lock resource rows in defined order → verify lifecycle/versions/
fences → mutate typed projections → allocate sequence → append committed
events → insert deduped follow-up Jobs/outbox → commit. Reads never settle
or launch work; all slow/recoverable effects are durable Jobs.
"""
