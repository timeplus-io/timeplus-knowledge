"""Per-user LLM token metering + daily-budget accounting (issue #62).

One row per chat turn is written to the append-only ``chat_usage`` stream,
independent of the chat-audit toggle (``TPK_CHAT_AUDIT``), so a daily token
budget stays enforceable even when auditing is off. Enforcement reads the
day's running total from the same stream.

Both read and write are best-effort at the call site: a token budget must never
be a single point of failure for chat. Reads fail *open* (an unreachable store
returns 0 used, so nobody is wrongly blocked); writes are swallowed.
"""

import logging
from datetime import datetime, timedelta, timezone

from tpk import db

logger = logging.getLogger(__name__)

# Insert column order for the append-only chat_usage stream (see db.ensure_schema).
USAGE_COLUMNS = ["ts", "username", "tokens"]


def effective_daily_limit(user_limit: int, role, global_default: int) -> int:
    """The daily token budget that applies to a user, by precedence (#62):
    the user's own override, else their role's limit, else the global fallback.
    0 at every level means unlimited. `role` may be a Role or None (an
    unreadable/absent role falls through to the global)."""
    ul = int(user_limit or 0)
    if ul > 0:
        return ul
    role_limit = getattr(role, "daily_token_limit", 0) or 0
    return int(role_limit) if role_limit > 0 else max(int(global_default), 0)


def day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The current budgeting window: [UTC-midnight-today, UTC-midnight-tomorrow).
    A per-UTC-calendar-day window — predictable and simple to explain."""
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def record_usage(client, username: str, tokens: int, prefix: str = "",
                 ts: datetime | None = None) -> None:
    """Append one usage row (a chat turn's total token cost)."""
    client.insert(
        db.qualified("chat_usage", prefix),
        [[ts or datetime.now(timezone.utc), username, int(tokens)]],
        column_names=USAGE_COLUMNS,
    )


def tokens_used_today(client, username: str, prefix: str = "",
                      now: datetime | None = None) -> int:
    """Sum of tokens a user has spent in the current day window."""
    start, _ = day_window(now)
    rows = client.query(
        f"SELECT sum(tokens) FROM table({db.qualified('chat_usage', prefix)})"
        " WHERE username = %(u)s AND ts >= %(start)s",
        parameters={"u": username, "start": start},
    ).result_rows
    return int(rows[0][0]) if rows and rows[0][0] is not None else 0


class DbUsage:
    """DB-backed usage store the /chat handler uses to enforce + record. A
    fresh single-use client per call avoids timeplus_connect's concurrent-query
    restriction (same reasoning as the audit sink)."""

    def __init__(self, prefix: str, client_factory):
        self.prefix = prefix
        self._client_factory = client_factory

    def used_today(self, username: str, now: datetime | None = None) -> int:
        try:
            return tokens_used_today(self._client_factory(), username,
                                     prefix=self.prefix, now=now)
        except Exception:
            # Fail OPEN: never block a user because the usage store is
            # unreachable. Log and treat as zero used.
            logger.exception("usage read failed; treating as 0 used (fail-open)")
            return 0

    def record(self, username: str, tokens: int) -> None:
        if tokens <= 0:
            return
        try:
            record_usage(self._client_factory(), username, tokens, prefix=self.prefix)
        except Exception:
            logger.exception("usage write failed; skipping")
