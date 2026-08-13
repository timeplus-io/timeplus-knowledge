"""Chat support-history audit: one row per chat turn written to the
append-only ``{prefix}chat_audit_log`` stream (see db.ensure_schema).

The sink is deliberately best-effort: auditing must never break or slow the
chat response, so every write is wrapped and any failure is logged and
swallowed. The server calls the sink off the event loop (run_in_threadpool)
after the answer has already streamed to the client.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Insert column order for {prefix}chat_audit_log. Kept next to AuditRecord.to_row
# so the two never drift.
CHAT_AUDIT_COLUMNS = [
    "ts",
    "turn_id",
    "conversation_id",
    "username",
    "role",
    "provider",
    "model",
    "question",
    "answer",
    "history_len",
    "tool_calls",
    "sources",
    "tool_count",
    "source_count",
    "latency_ms",
    "status",
    "error",
]


@dataclass
class AuditRecord:
    """One chat turn's worth of support history. ``tool_calls`` and ``sources``
    are variable-shape, so they serialize to JSON strings in the row."""

    ts: datetime
    turn_id: str
    username: str
    role: str
    provider: str
    model: str
    question: str
    answer: str
    history_len: int
    latency_ms: int
    status: str  # "ok" | "error"
    error: str = ""
    conversation_id: str = ""
    tool_calls: list = field(default_factory=list)
    sources: list = field(default_factory=list)

    def to_row(self) -> list:
        return [
            self.ts,
            self.turn_id,
            self.conversation_id,
            self.username,
            self.role,
            self.provider,
            self.model,
            self.question,
            self.answer,
            self.history_len,
            json.dumps(self.tool_calls),
            json.dumps(self.sources),
            len(self.tool_calls),
            len(self.sources),
            self.latency_ms,
            self.status,
            self.error,
        ]


def make_db_sink(prefix: str, client_factory):
    """Return a ``sink(record: AuditRecord) -> None`` that inserts into
    ``{prefix}chat_audit_log``.

    ``client_factory`` is called per write to obtain a Timeplus client -- a
    fresh, single-use session avoids timeplus_connect's concurrent-query
    restriction without sharing state across the chat request threads.

    Best-effort: any exception (unreachable store, schema drift, bad row) is
    logged and swallowed so the chat turn is never affected.
    """

    def sink(record: AuditRecord) -> None:
        try:
            client = client_factory()
            client.insert(
                f"{prefix}chat_audit_log",
                [record.to_row()],
                column_names=CHAT_AUDIT_COLUMNS,
            )
        except Exception:
            logger.exception("chat audit write failed; skipping")

    return sink
