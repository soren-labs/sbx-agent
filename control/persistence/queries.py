"""Named specialized SQL statements (kept in persistence, invoked by name)."""

from __future__ import annotations

NAMED: dict[str, str] = {
    # Jobs -------------------------------------------------------------------------
    "jobs.claimable": """
        SELECT * FROM jobs
        WHERE ((state IN ('queued', 'retry_wait') AND due_at <= now())
               OR (state = 'claimed' AND claim_expires_at < now()))
          AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))
        ORDER BY priority DESC, due_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    """,
    "jobs.lock_claim": """
        SELECT * FROM jobs WHERE id = %(job_id)s FOR UPDATE
    """,
    # Sessions ---------------------------------------------------------------------
    "sessions.page": """
        SELECT * FROM sessions
        WHERE workspace_id = ANY(%(workspace_ids)s)
          AND (%(lifecycle)s::text IS NULL OR lifecycle = %(lifecycle)s)
          AND (%(role)s::text IS NULL OR role = %(role)s)
          AND (%(project_id)s::text IS NULL OR project_id = %(project_id)s)
          AND (%(parent_session_id)s::text IS NULL OR parent_session_id = %(parent_session_id)s)
          AND (%(cursor_at)s::timestamptz IS NULL
               OR (updated_at, id) < (%(cursor_at)s::timestamptz, %(cursor_id)s::text))
        ORDER BY updated_at DESC, id DESC
        LIMIT %(limit)s
    """,
    "turns.activity": """
        SELECT session_id,
               max(state) FILTER (WHERE state IN ('preparing', 'running', 'cancelling')) AS active,
               count(*) FILTER (WHERE state = 'queued') AS queued,
               (array_agg(state ORDER BY ordinal DESC)
                  FILTER (WHERE state IN ('succeeded', 'failed', 'cancelled', 'interrupted')))[1]
                 AS last_terminal
        FROM turns WHERE session_id = ANY(%(session_ids)s) GROUP BY session_id
    """,
    "turns.next_queued": """
        SELECT * FROM turns WHERE session_id = %(session_id)s AND state = 'queued'
        ORDER BY ordinal LIMIT 1
    """,
    "events.after": """
        SELECT * FROM session_events
        WHERE session_id = %(session_id)s AND seq > %(after)s
          AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s::text[]))
          AND (%(turn_id)s::text IS NULL OR turn_id = %(turn_id)s)
          AND seq <= %(upto)s
        ORDER BY seq LIMIT %(limit)s
    """,
    "events.max_scanned": """
        SELECT coalesce(max(seq), %(after)s) AS seq FROM (
          SELECT seq FROM session_events
          WHERE session_id = %(session_id)s AND seq > %(after)s AND seq <= %(upto)s
          ORDER BY seq LIMIT %(limit)s) s
    """,
    "parts.for_session": """
        SELECT p.* FROM message_parts p WHERE p.session_id = %(session_id)s
        ORDER BY p.message_id, p.ordinal
    """,
    "memberships.for_user": """
        SELECT w.* FROM workspaces w JOIN workspace_memberships m ON m.workspace_id = w.id
        WHERE m.user_id = %(user_id)s AND m.status = 'active' ORDER BY w.created_at
    """,
    "login.failures_recent": """
        SELECT count(*) AS n FROM login_attempts
        WHERE email = %(email)s AND NOT succeeded AND created_at > now() - interval '15 minutes'
    """,
    "capacity.active_slots": """
        SELECT slot_ordinal FROM capacity_reservations
        WHERE connection_id = %(connection_id)s AND state IN ('active', 'quarantined')
    """,
    "connections.lock_many": """
        SELECT * FROM connections WHERE id = ANY(%(ids)s) ORDER BY id FOR UPDATE
    """,
    "observations.latest": """
        SELECT DISTINCT ON (kind) * FROM connection_observations
        WHERE connection_id = %(connection_id)s
          AND credential_version_id = %(credential_version_id)s
        ORDER BY kind, observed_at DESC
    """,
    "leases.live_for_connection": """
        SELECT * FROM executor_leases
        WHERE compute_connection_id = %(connection_id)s
          AND (state IN ('allocating', 'ready', 'quiescing') OR quarantined)
    """,
}
