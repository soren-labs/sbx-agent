"""Formal typed Python SDK for the sbx-browser public ``/v1`` API (SOR-226).

    from sbx.sdk import SbxClient

    client = SbxClient(base_url="...", api_key="sbx_...")
    created = client.tasks.create("Write hello.txt", source={"repo": "..."})
    task = client.tasks.wait(created.task.id)
    revision = client.tasks.revision(created.task.id)
    review = client.tasks.review(created.task.id, verdict="approve")
    client.tasks.merge(created.task.id)

Errors decode the canonical error body (``code``/``message``/``retryable``/
``action``/``retry_after``/``details``) into :class:`SbxApiError`; bounded
transport failures raise :class:`SbxTransportError`. The public OpenAPI is
runtime-derived — ``client.openapi()`` fetches it.
"""

from sbx.sdk.client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    TIMEOUT_ENV,
    SbxClient,
)
from sbx.sdk.errors import SbxApiError, SbxTransportError
from sbx.sdk.models import (
    RUN_DONE,
    RUN_END_EVENTS,
    RUN_TERMINAL,
    TASK_TERMINAL,
    Agent,
    Delivery,
    Review,
    Revision,
    Run,
    RunError,
    SseEvent,
    Task,
    TaskCreated,
    TaskDetail,
    WorkflowRecovery,
)
from sbx.sdk.unified import UnifiedApiError, UnifiedClient

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "TIMEOUT_ENV",
    "RUN_DONE",
    "RUN_END_EVENTS",
    "RUN_TERMINAL",
    "TASK_TERMINAL",
    "Agent",
    "Delivery",
    "Revision",
    "Review",
    "Run",
    "RunError",
    "SbxApiError",
    "SbxClient",
    "SbxTransportError",
    "SseEvent",
    "Task",
    "TaskCreated",
    "TaskDetail",
    "UnifiedApiError",
    "UnifiedClient",
    "WorkflowRecovery",
]
