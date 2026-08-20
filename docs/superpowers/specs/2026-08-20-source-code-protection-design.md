# Source-code protection design (#66)

## Problem

The knowledge agent ingests private source repos and reads raw source via the
`read_source` tool to answer questions. Reading is fine and must stay
unrestricted — the agent needs the code to answer well. The problem is at the
**user-facing output**: today the raw source can reach the user verbatim,
turning the Q&A surface into a source-download channel.

Requirement: the agent may freely read code, but must not hand the user
complete source.

## Source channels to the user

There are exactly three ways source content reaches a user today:

1. **The answer** — `token`/`done` SSE events (`server.py:345`, `:431`). The
   model can paste source into its free-form answer text.
2. **The thinking trace** — `thinking` SSE event (`server.py:341`). Reasoning
   text (Anthropic extended thinking; OpenAI-compatible reasoning models) can
   quote source.
3. **Citation fragments** — the `source` SSE event (`server.py:410`) carries
   only `repo/file_path/line_start/line_end` **references, not the body**. The
   body reaches the user only when they click a citation, which calls
   `GET /api/graph/source` (`graph_api.py:85`) — the only endpoint that returns
   raw `lines`. Gated today by `CAP_EXPLORE` alone.

The `read_source` tool's raw return value is **not** streamed to the client
(only `search_entities` emits a `tool_result` with a count; `read_source` emits
just the reference `source` event). So the tool output body never reaches the
user except through channel 1 or 3.

## Decisions (locked)

- **Answer channel: prompt only, always on.** No code enforcement, no
  post-hoc redaction, no streaming line-cap. A system-prompt rule instructs the
  model to explain and quote minimally and to decline full-file dumps. Applies
  to every role, including admin (it is guidance, not a gate).
- **Thinking + citations: one role capability**, default-deny.
- **Dropped from the original issue (YAGNI):** aggregate per-conversation
  output budget, streaming line-cap, post-hoc verbatim-diff redaction, per-repo
  strictness knobs. Prompt-only + a single role gate meets the requirement.

## Design

### New capability: `source:view`

Grants visibility of raw source detail — the thinking trace **and**
citation-fragment bodies.

- Added to `auth.py`: new constant `CAP_SOURCE_VIEW = "source:view"`, appended
  to `ALL_CAPABILITIES` (canonical UI/display order). No `:manage` sibling (it
  is a bare grant like `chat`/`explore`), so `_MANAGE_IMPLIES_VIEW` is
  untouched.
- **Not** added to `DEFAULT_CAPABILITIES` → default-deny. `admin` bypasses all
  capability checks as usual.
- Added to `web/src/capabilities.ts` (`CAP`, `CAPABILITY_OPTIONS`) so it renders
  in the role editor.

> **Amended after review of the running build.** The original design kept the
> `file:line` reference rows visible to all chat users and gated only the body.
> In practice a restricted user still saw the reasoning trace and clickable
> citations, and — critically — the reference metadata was only hidden in the
> UI, still crossing the wire in the SSE stream. Decision revised to
> **answer-only** for users without `source:view`: the `/chat` stream carries
> only the answer (`token`/`done`/`error`), and the reasoning trace + all
> source/citation references are withheld **server-side** (not just hidden in
> the client). The server still records the full trace/sources to the audit
> log. See "Enforcement point 2" below.

### Enforcement point 1 — answer (prompt)

Extend the agent system prompt in `agent.py` (the numbered guidance block) with
a source-protection rule: explain behavior and structure, quote only short
illustrative snippets, never reproduce complete or near-complete files; if the
user asks to print/dump a whole file, decline and offer to explain instead. The
existing citation guidance (use `read_source` so files appear in the Sources
panel) is unchanged — reading and citing still happen; only bulk reproduction in
the answer is discouraged.

### Enforcement point 2 — thinking trace (`server.py`)

The chat stream handler already resolves the caller's `role` (see
`server.py:236`, `:274`). Compute a per-request flag:

```
can_view_source = (user.role == ROLE_ADMIN) or (CAP_SOURCE_VIEW in caps)
```

where `caps` is the expanded capability set for the role. When
`can_view_source` is false, the stream is **answer-only**: withhold every
non-answer event so the reasoning trace and all source references never cross
the wire. Concretely, gate these `yield _sse(...)` calls on `can_view_source`:

- `thinking` — the extended-thinking deltas.
- `tool` (`on_tool_start`) and `tool_result` — tool names/inputs (a
  `read_source` input *is* a file/line reference).
- `source` — the clickable citation reference.
- `done` — send `"sources": []` instead of the accumulated list.

Only `token`, `done` (answer text), and `error` reach a restricted user. The
audit accumulators (`tool_calls`, `sources`) are still built and written to the
audit log — only the client stream is gated, not the server-side record.

Why server-side and not just client hiding: the client re-labels the model's
between-tool narration (`token` text before a `tool` event) as a "thinking"
step, so hiding only `kind==="thinking"` rows still showed a trace; and any
client-only gate leaves the references visible to anyone reading the raw SSE
stream. Gating at emission is the actual boundary.

### Enforcement point 3 — citation fragment (`graph_api.py`)

`GET /api/graph/source` is the only endpoint returning raw `lines`. Require the
new capability in addition to `CAP_EXPLORE`. Concretely, its dependency becomes
`require_cap(CAP_SOURCE_VIEW)`. Without the cap the endpoint returns 403; the
`search`/`entity`/`neighbors` endpoints keep `CAP_EXPLORE` and are unaffected.

Admin keeps working automatically: `require_cap` checks `effective_caps`
(`auth.py:390`), and `effective_caps` returns `set(ALL_CAPABILITIES)` for admin
(`auth.py:377`) — so adding `source:view` to `ALL_CAPABILITIES` grants it to
admin with no special-casing.

### Frontend (`web/src/`)

The app already fetches `capabilities` from `/auth/me` and gates UI with
`hasCap`. Thread the flag `hasCap(caps, CAP.sourceView)` into `Chat.tsx` as
`canViewSource`. These are defense-in-depth — the server now withholds the
events, so they matter only if a future stream change regresses:

- **Reasoning trace:** `renderTrace` returns `null` when `canViewSource` is
  false — no thinking steps, no tool-call rows.
- **Sources panel:** the citation side-panel (`activeSource`) is not rendered
  when `canViewSource` is false. `SourceCard` also keeps its own guard, skipping
  the `readSource` fetch and showing a restricted note.
- **Role editor (`Users.tsx`):** `CAPABILITY_OPTIONS` gains `source:view` so
  admins can grant it.

## Migration / behavior change

Default-deny means existing non-admin roles lose the thinking panel and fragment
previews on upgrade until an admin grants `source:view`. This is the intended
protection posture (confirmed). No data migration is required — roles that
predate this cap simply don't hold it. Admin is unaffected.

## Testing (TDD)

Backend, mirroring existing auth/graph/server tests:

- `expand_capabilities` includes `source:view` when granted; unknown/absent
  behaves as before.
- `GET /api/graph/source` → 403 for a role without `source:view`; 200 for a role
  with it; 200 for admin.
- Chat stream: `thinking` events are suppressed for a role without the cap and
  present for a role with it (and for admin). Assert other event types
  (`token`, `source`, `done`) are unaffected in both cases.
- Sanity: a role with `source:view` but the endpoint otherwise scoped still
  respects role `entry_keys` scope (no scope bypass introduced).

Frontend: follow existing `web/src` test conventions if present; otherwise a
manual check that thinking rows and fragment previews are hidden without the cap
and shown with it.

## Out of scope

- Any change to `read_source` tool access or what the model reads into context.
- Aggregate output budgets, streaming caps, redaction, per-repo strictness.
- Reworking how citations are represented for privileged users (unchanged);
  restricted users simply receive no citation events.

## Touched files

- `src/tpk/auth.py` — new capability constant + `ALL_CAPABILITIES`.
- `src/tpk/agent.py` — system-prompt source-protection rule.
- `src/tpk/server.py` — compute `can_view_source`, gate `thinking` events.
- `src/tpk/graph_api.py` — require `source:view` on `/api/graph/source`.
- `web/src/capabilities.ts` — new cap + `CAPABILITY_OPTIONS`.
- `web/src/Chat.tsx` — gate thinking rows + `SourceCard` preview/click.
- `web/src/Users.tsx` — cap appears in role editor (via `CAPABILITY_OPTIONS`).
- Tests alongside the backend modules above.
