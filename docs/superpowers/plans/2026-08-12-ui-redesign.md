# Timeplus Knowledge UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved mockup `docs/design/timeplus-knowledge-mockups.html` — a Timeplus Console redesign of the web UI: sidebar-nav app shell, restyled Chat / Manage / Users / Login, a new **Explorer** screen, and chat **citations + Sources panel**. Includes the backend needed for Explorer (graph query REST endpoints) and citations (surfacing read sources from the agent stream).

**Architecture:** Frontend (React + Vite in `web/`) moves from a single-column top-tab layout to a 220px sidebar shell with a content area (and a right Sources panel on Chat). Two new backend surfaces: read-only `/api/graph/*` endpoints wrapping the existing `KnowledgeGraph` tools (role-scoped) for Explorer, and `source` SSE events from `/chat` (captured from LangGraph `on_tool_end`) for citations. All design tokens already exist in `web/src/app.css` and match the mockup exactly.

**Tech Stack:** Python 3.11 / FastAPI / timeplus_connect / LangGraph; React 18 + TypeScript + Vite; Timeplus Console design tokens.

## Global Constraints

- **The mockup is the pixel spec.** `docs/design/timeplus-knowledge-mockups.html` holds all seven screens as inline-styled HTML: `1a` Chat answered (tool trace + citations + Sources panel), `1b` Chat empty, `1c` Chat streaming, `1d` Explorer, `1e` Manage, `1f` Users & roles, `1g` Login + forced change. Match its layout, spacing, and structure. Translate its inline styles into shared CSS classes in `web/src/app.css` — do not ship inline styles.
- **Design tokens** are already defined in `web/src/app.css` (`--pink-500:#d53f8c`, `--gray-900:#f7f6f6`, `--gray-700:#dad9db`, etc.) and match the mockup's hex values. Use the CSS variables, never raw hex, in new rules.
- **Shape/spacing:** 4px radius everywhere (pill only for toggles/avatars); no shadows (borders separate surfaces); buttons 32px tall, inputs 40px tall; 2px pink focus ring on focused inputs; Inter font, never below 12px.
- **Sidebar shell** (identical across Chat/Explorer/Manage/Users): 220px, white, right border `--gray-700`; T-logo block; nav items Chat / Explorer / Manage / Users (active = `--gray-800` bg + `inset 3px 0 0 --pink-500` + semibold); spacer; account row (avatar initial + username + Logout link). Manage and Users nav items render only for `role === "admin"`; Explorer is visible to all authenticated users.
- **Auth unchanged:** all data calls go through `apiFetch` (bearer). New `/api/graph/*` endpoints require a valid user (`require_user`); they are NOT admin-only. Non-admin graph queries are role-scoped exactly like chat (set `ROLE_SCOPE` per request).
- **No behavior regressions:** existing endpoints, auth flows, and the 134-test backend suite stay green. Backend tests: `TIMEPLUS_HOST=localhost uv run pytest -q` (dev `default` user has no password on the bare timeplusd; if the hardened compose stack is what serves 8123, pass `TIMEPLUS_USER=tpk TIMEPLUS_PASSWORD=…`).
- **Frontend gate** (every frontend task): `cd web && npm run build && npm run check:sanitize` must pass. There is no JS test runner.
- Conventional commits; full backend suite green before each backend commit; PR at the end — do NOT merge.

## File Structure

```
src/tpk/graph_api.py     # NEW: read-only /api/graph/* router over KnowledgeGraph (role-scoped)
src/tpk/server.py        # share a KnowledgeGraph with the graph router; emit `source` SSE events
src/tpk/api.py           # (unchanged unless graph router mounts here)
web/src/app.css          # sidebar shell + all component classes (replaces the single-column shell)
web/src/Shell.tsx        # NEW: sidebar layout wrapper (nav + account + content slot)
web/src/Chat.tsx         # NEW: extracted chat pane (empty/streaming/answered, tool trace, citations, Sources)
web/src/Explorer.tsx     # NEW: entity search + detail + neighbors + subgraph
web/src/Manage.tsx       # restyled: toggle switches, jobs panel, add-entry form
web/src/Users.tsx        # restyled: users table + role cards with corpus-access checkboxes
web/src/Login.tsx        # restyled login + forced-change
web/src/App.tsx          # routing between Chat/Explorer/Manage/Users inside Shell
web/src/graph.ts         # NEW: typed fetch helpers for /api/graph/*
tests/test_graph_api.py  # NEW
tests/test_server.py     # + citation `source` event assertions
```

---

### Task 1: Backend — read-only graph query endpoints for Explorer

**Files:**
- Create: `src/tpk/graph_api.py`
- Modify: `src/tpk/server.py` (build one shared `KnowledgeGraph`, mount the graph router with it and the auth layer)
- Test: `tests/test_graph_api.py`

**Interfaces:**
- Consumes: `KnowledgeGraph.search_entities(query, kinds=None, repos=None, limit=20)`, `.get_entity(id)`, `.neighbors(entity_id, rels=None, direction="both", depth=1, confidence=None)`, `.read_source(repo, file_path, line_start, line_end)`; `auth.AuthLayer.require_user`; `auth.get_role`; `tools.ROLE_SCOPE`.
- Produces: `create_graph_router(kg, auth, prefix="") -> APIRouter` mounting under `/api/graph`:
  - `GET /api/graph/search?q=&kind=&repo=&limit=` → `{results: [{id, kind, name, qualified_name, file_path, repo, summary}]}`
  - `GET /api/graph/entity?id=` → the entity record (404 if absent/out of scope)
  - `GET /api/graph/neighbors?id=&direction=&depth=&limit=` → `{center, edges: [{rel, confidence, direction, other: {id, name, kind}}]}`
  - `GET /api/graph/source?repo=&file_path=&line_start=&line_end=` → `{repo, file_path, line_start, line_end, lines: str}`
  Every handler depends on `require_user` and, for a non-admin, sets `ROLE_SCOPE` to the user's role entry-keys (fail-closed `frozenset()` when the role is missing) for the duration of the call, resetting in `finally` — mirror `server.py`'s chat scoping (run the blocking KG calls via `fastapi.concurrency.run_in_threadpool`).

- [ ] **Step 1: Write failing tests** — `tests/test_graph_api.py`, patterned on `tests/test_api.py`'s app fixture (`create_app(agent=_NoAgent(), stream_prefix=prefix)`) and `tests/test_tools.py`'s node/edge seeding + corpus-entry seeding. Seed two enabled entries with a couple of nodes/edges, an admin user, and a scoped non-admin role. Assert:
  - unauthenticated → 401 on all four routes;
  - admin `GET /api/graph/search?q=<name>` returns the seeded entity (shape check: `id`,`kind`,`name`,`repo`);
  - `GET /api/graph/entity?id=<id>` returns it; a bogus id → 404;
  - `GET /api/graph/neighbors?id=<id>` returns its edges with `direction`;
  - `GET /api/graph/source?...` returns lines (seed a repo path via the `tools.py` test helper, or assert 404/400 cleanly when no checkout — match how `test_tools.py` exercises `read_source`);
  - a non-admin whose role lists only entry A cannot see entity B: `search` excludes B, `entity?id=<B>` → 404.

  Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_graph_api.py` → FAIL (module missing).

- [ ] **Step 2: Implement `graph_api.py`.** Build the router; each route resolves the scope once:

```python
"""Read-only graph query endpoints for the Explorer UI. Role-scoped like chat;
requires a valid user, not admin."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

import tpk.auth as auth_mod
from tpk.tools import ROLE_SCOPE


def create_graph_router(kg, auth, prefix: str = "") -> APIRouter:
    router = APIRouter(prefix="/api/graph")

    async def _scoped(user, fn):
        scope = None
        if user.role != auth_mod.ROLE_ADMIN:
            try:
                role = await run_in_threadpool(
                    lambda: auth_mod.get_role(auth._client(), user.role, prefix=prefix))
            except Exception:
                role = None
            scope = frozenset(role.entry_keys) if role else frozenset()
        token = ROLE_SCOPE.set(scope) if scope is not None else None
        try:
            return await run_in_threadpool(fn)
        finally:
            if token is not None:
                ROLE_SCOPE.reset(token)

    @router.get("/search")
    async def search(q: str = Query(""), kind: str = Query(""), repo: str = Query(""),
                     limit: int = Query(20), user=Depends(auth.require_user)):
        kinds = [kind] if kind and kind != "any" else None
        repos = [repo] if repo and repo != "any" else None
        rows = await _scoped(user, lambda: kg.search_entities(q, kinds=kinds, repos=repos, limit=limit))
        return {"results": rows}

    @router.get("/entity")
    async def entity(id: str, user=Depends(auth.require_user)):
        row = await _scoped(user, lambda: kg.get_entity(id))
        if not row:
            raise HTTPException(404, "no such entity")
        return row

    @router.get("/neighbors")
    async def neighbors(id: str, direction: str = "both", depth: int = 1,
                        user=Depends(auth.require_user)):
        return await _scoped(user, lambda: kg.neighbors(id, direction=direction, depth=depth))

    @router.get("/source")
    async def source(repo: str, file_path: str, line_start: int, line_end: int,
                     user=Depends(auth.require_user)):
        try:
            lines = await _scoped(user, lambda: kg.read_source(repo, file_path, line_start, line_end))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {"repo": repo, "file_path": file_path, "line_start": line_start,
                "line_end": line_end, "lines": lines}

    return router
```

  (Match the actual return shapes of the KG methods — read `src/tpk/tools.py` and adapt keys so the tests pass. `get_entity`/`neighbors` return whatever `tools.py` returns; wrap or pass through consistently with what the tests assert.)

- [ ] **Step 3: Wire into `server.py`.** In `create_app`, the production agent path already builds a `KnowledgeGraph` inside `_build_production_agent`; refactor so the app can access one shared `KnowledgeGraph` instance (build it in `create_app` when `agent is None`, pass it to both `build_agent` and `create_graph_router`). For the test path (`agent` injected), accept an optional `kg=None` param on `create_app` and mount the graph router only when `kg` is provided, OR build a lightweight KG from `Settings.from_env()` — choose whichever keeps `tests/test_graph_api.py` able to construct the app against the seeded streams (simplest: `create_app(agent=..., stream_prefix=prefix, kg=<KnowledgeGraph over the test prefix>)`). Mount: `app.include_router(create_graph_router(kg, auth, prefix=stream_prefix))` when `kg` is set.

- [ ] **Step 4: Run tests, full suite, commit.**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q tests/test_graph_api.py && TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/graph_api.py src/tpk/server.py tests/test_graph_api.py
git commit -m "feat: read-only role-scoped graph query endpoints for Explorer"
```

---

### Task 2: Backend — surface cited sources in the chat stream

**Files:**
- Modify: `src/tpk/server.py` (`chat` stream: capture `on_tool_end` for `read_source`/`get_entity` and emit `source` events; include a `sources` array on the `done` event)
- Test: `tests/test_server.py`

**Interfaces:**
- Produces: two SSE additions from `/chat`:
  - `{"type":"source", "n":<int>, "repo":str, "file_path":str, "line_start":int, "line_end":int, "kind":"file"|"document"}` emitted when a `read_source` tool call completes (dedup by (repo,file_path,line_start); `n` is a 1-based citation index).
  - the existing `{"type":"done", ...}` gains `"sources": [ …the same source objects… ]`.
- The current `on_tool_start` handling (the `tool` event) is unchanged.

- [ ] **Step 1: Write failing test** in `tests/test_server.py` using a `FakeAgent` whose `astream_events` yields an `on_tool_end` event for `read_source` (inspect how the existing `FakeAgent` yields events and extend it, or add a new fake). Assert a `source` event is emitted with `n==1` and the path, and that the final `done` event carries a `sources` list containing it. Keep it infra-free (stub auth, no DB), like the existing server tests.

  Run: `TIMEPLUS_HOST=localhost uv run pytest -q tests/test_server.py -k source` → FAIL.

- [ ] **Step 2: Implement.** In the `chat` `stream()` generator, add an `on_tool_end` branch: read `event["name"]` and `event["data"]["output"]`; when the tool is `read_source`, parse the originating call's args (from `event["data"]["input"]`) for `repo`/`file_path`/`line_start`/`line_end`, assign the next citation index if that (repo,file_path,line_start) is new, append to a local `sources` list, and `yield _sse({"type":"source", ...})`. On completion, include `"sources": sources` in the `done` payload. Guard everything so a malformed event never breaks the stream (wrap in try/except, log, continue). Do not block the token stream.

- [ ] **Step 3: Run tests, full suite, commit.**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q tests/test_server.py && TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/server.py tests/test_server.py
git commit -m "feat: emit cited source events from the chat stream"
```

---

### Task 3: Frontend — sidebar app shell + design refresh

**Files:**
- Create: `web/src/Shell.tsx`
- Modify: `web/src/app.css` (add sidebar shell + shared component classes; keep the token block), `web/src/App.tsx` (render Shell with routing), `web/src/graph.ts` (NEW — typed helpers, may be a stub filled in Task 5)

**Interfaces:**
- Produces: `Shell` component — props `{ me: {username, role}, view, onNavigate(view), onLogout, children }`. Renders the 220px sidebar (logo, nav items with active state, account row) + a content region that holds `children`. Nav items: `chat`, `explorer`, `manage` (admin only), `users` (admin only). Matches mockup screens' sidebar exactly (translate inline styles to classes: `.tk-shell`, `.tk-sidebar`, `.tk-nav-item`, `.tk-nav-item.active`, `.tk-account`, etc.).
- `App.tsx` state adds `"explorer"` to the `view` union; renders `<Shell …><Chat/|Explorer/|Manage/|Users/></Shell>` for the active view; Login gate stays outside the Shell.

- [ ] **Step 1** — Add sidebar + shared classes to `app.css` from mockup screen `1a`'s sidebar and header (`.tk-shell{display:flex;height:100vh}`, `.tk-sidebar{width:220px;…}`, nav item, active `box-shadow:inset 3px 0 0 var(--pink-500)`, account row). Add shared button/input/card/table/toggle classes used across screens (primary/secondary/danger buttons at 32px, 40px inputs with pink focus ring, `.tk-card`, `.tk-toggle`). Reference the mockup for exact values.
- [ ] **Step 2** — Write `Shell.tsx` rendering the sidebar + content slot; admin-only nav items gated on `me.role === "admin"`; active item from `view`; Logout calls `onLogout`.
- [ ] **Step 3** — Rework `App.tsx`: extend the `view` union with `"explorer"`; move the existing chat markup into a temporary `Chat` usage (Task 4 builds the real `Chat.tsx` — for this task, render the current chat inline or a placeholder inside Shell so the app compiles and Chat still works); wire `onNavigate`/`onLogout`; keep the Login gate and all auth effects intact.
- [ ] **Step 4** — `cd web && npm run build && npm run check:sanitize`; commit `feat: sidebar app shell and shared Timeplus Console component styles`.

---

### Task 4: Frontend — Chat redesign (empty / streaming / answered, tool trace, citations, Sources panel)

**Files:**
- Create: `web/src/Chat.tsx`
- Modify: `web/src/App.tsx` (render `<Chat/>` in the chat view), `web/src/app.css` (chat classes)

**Interfaces:**
- Consumes: the `/chat` SSE (`token`, `tool`, `source`, `done` with `sources`, `error`) via `apiFetch`; `GET /api/repos` (for the empty-state corpus tags — reuse existing shape); `me`.
- Produces: `Chat` component owning its own turns state (moved out of App). Implements mockup screens `1a`/`1b`/`1c`:
  - **Empty (`1b`):** centered "Ask anything about Timeplus", corpus entry tags from `/api/repos` (enabled entries), 4 suggested-question cards (static list is fine), input + Send.
  - **Streaming (`1c`):** live tool-trace card ("Working" + elapsed, each tool row with a filled/hollow dot; running call shows "running…"), streaming answer text with a blinking caret, disabled input showing "Thinking…".
  - **Answered (`1a`):** collapsed tool-trace card ("N tool calls · Ns"), answer with `[n]` citation superscripts (render markdown as today, then superscript links per emitted source), and a right **Sources panel** listing each cited source (index, path, a source preview via `GET /api/graph/source` for file sources or the entity summary, repo·lines footer). The panel is toggpable/closable; absent when no sources.
- The header "answering from" corpus tags (mockup `1a` top bar) list enabled corpus entries.

- [ ] **Step 1** — Add chat classes to `app.css` from screens `1a`/`1b`/`1c` (message bubbles, tool-trace card + rows + dots, citation superscript, Sources panel + source cards, empty-state cards, streaming caret).
- [ ] **Step 2** — Build `Chat.tsx`: port the existing SSE read loop from `App.tsx` (`apiFetch("/chat")`, parse `data:` lines), extend it to collect `tool` events into a trace and `source` events into a sources list; render the three states; keep the existing markdown rendering + sanitize pipeline for answer text (do not introduce `dangerouslySetInnerHTML` beyond what exists). Fetch `/api/graph/source` lazily to fill a source card's preview.
- [ ] **Step 3** — Wire into `App.tsx` (replace the placeholder from Task 3). Remove the now-dead chat state from App.
- [ ] **Step 4** — build + check:sanitize; commit `feat: chat redesign with tool trace, citations, and sources panel`.

---

### Task 5: Frontend — Explorer screen

**Files:**
- Create: `web/src/Explorer.tsx`, `web/src/graph.ts` (typed helpers over `/api/graph/*`)
- Modify: `web/src/App.tsx` (render `<Explorer/>`), `web/src/app.css` (explorer classes)

**Interfaces:**
- Consumes: `/api/graph/search`, `/api/graph/entity`, `/api/graph/neighbors`, `/api/graph/source` (Task 1) via `graph.ts` + `apiFetch`; `/api/repos` for the repo filter dropdown.
- Produces: `Explorer` component implementing mockup screen `1d`: top bar with search input (2px pink focus ring), `kind` and `repo` filter dropdowns, entity count; a left results list (kind badge + name + file path, selected row highlighted); a detail pane (name + kind/repo badges + Read source / "Ask about this" buttons + summary), a Neighbors list (grouped by relation with direction labels + extracted/inferred tags), and a Subgraph SVG (center node + neighbor nodes, lines) rendered from the neighbors response. "Ask about this" navigates to Chat with a prefilled question (via a callback prop `onAsk(text)` App passes down). "Read source" opens the source lines (reuse a source-preview block).

- [ ] **Step 1** — `graph.ts`: typed `searchEntities`, `getEntity`, `getNeighbors`, `readSource` calling `apiFetch` and returning parsed JSON (throw on non-ok).
- [ ] **Step 2** — Explorer classes in `app.css` from screen `1d` (top filter bar, results list rows + kind badge, detail card, neighbors rows, subgraph nodes/lines).
- [ ] **Step 3** — Build `Explorer.tsx`: search-on-submit → results list → select → fetch entity + neighbors → render detail + neighbors + a simple radial subgraph layout (center + up to N neighbors positioned on a circle, SVG lines). Empty/loading/no-results states. `onAsk` prop navigates to chat.
- [ ] **Step 4** — Wire into `App.tsx` (`explorer` view; `onAsk` sets chat input + switches view). build + check:sanitize; commit `feat: Explorer screen — entity search, detail, neighbors, subgraph`.

---

### Task 6: Frontend — Manage redesign

**Files:**
- Modify: `web/src/Manage.tsx`, `web/src/app.css`

**Interfaces:**
- Consumes: existing `/api/repos`, `/api/repos/toggle`, `/api/repos/reindex`, `/api/repos/delete`, `/api/repos` (add), `/api/jobs` (all via `apiFetch`, already wired in the current Manage.tsx).
- Produces: Manage restyled to mockup `1e`: header ("Corpus" + subtitle + "Add corpus entry"); corpus table with columns Entry / Source / Mode(+visibility) / Nodes / Last ingest(+sha) / Enabled(**toggle switch**) / Actions(Reindex, Delete); a two-column lower region — **Ingest jobs** panel (running job with a progress bar + status line; recent finished jobs with node/edge counts or failure text + "view log") and the **Add corpus entry** form (Name*, GitHub org/repo, Ref, Visibility, Extraction, helper text, "Add only" / "Add & index"). Preserve all existing behavior and the existing error handling; only the markup/《classes》 and the toggle/jobs/add-form presentation change.

- [ ] **Step 1** — Manage classes in `app.css` from screen `1e` (table grid, toggle switch on/off, jobs panel + progress bar, add-entry form).
- [ ] **Step 2** — Rework `Manage.tsx` markup to the mockup; wire the toggle switch to the existing toggle call; render `/api/jobs` into the jobs panel (progress % if the job exposes it, else running/ok/failed states — match what `/api/jobs` actually returns); keep the add-entry form posting to the existing endpoint.
- [ ] **Step 3** — build + check:sanitize; commit `feat: Manage redesign — toggle switches, jobs panel, add-entry form`.

---

### Task 7: Frontend — Users & roles redesign

**Files:**
- Modify: `web/src/Users.tsx`, `web/src/app.css`

**Interfaces:**
- Consumes: existing `/api/users*`, `/api/roles*`, `/api/repos` (for the role corpus-access checkboxes) — all already wired.
- Produces: Users restyled to mockup `1f`: header ("Users & roles" + subtitle + "Add role" / "Add user"); users table (avatar + username, Role select, Status, Actions: Reset password / Disable-Enable / Delete); role cards row — each card shows name + member count + Save/Delete, a **Corpus access** checkbox grid over the corpus entry keys (from `/api/repos`), and a Description field. Keep the least-privilege add-user default and all existing guards/behavior; only presentation changes.

- [ ] **Step 1** — Users classes in `app.css` from screen `1f` (users table, role cards, corpus-access checkbox chips).
- [ ] **Step 2** — Rework `Users.tsx` to the mockup markup; role cards render the corpus-access checkboxes bound to each role's `entry_keys`; wire to the existing endpoints.
- [ ] **Step 3** — build + check:sanitize; commit `feat: Users & roles redesign — role cards with corpus-access grid`.

---

### Task 8: Frontend — Login redesign + final gate + PR

**Files:**
- Modify: `web/src/Login.tsx`, `web/src/app.css`, `README.md` (screenshot/section note optional)

- [ ] **Step 1** — Login classes in `app.css` from screen `1g`; restyle `Login.tsx` (sign-in card; forced-change card with Current / New / Confirm-new fields, mismatch error state on Confirm). Keep the existing auth logic (skip401Handling on change-password, forced-change routing) intact — presentation only.
- [ ] **Step 2** — Full frontend gate: `cd web && npm run build && npm run check:sanitize`. Full backend suite once more: `TIMEPLUS_HOST=localhost uv run pytest -q`.
- [ ] **Step 3: Live E2E** — build the stack from this branch (`docker compose build && docker compose up -d`, `.env` needs `TIMEPLUS_PASSWORD`), log in (admin/changeme → forced change), and click through with Playwright MCP: sidebar nav; Chat empty→ask→answered with a tool trace and (if a read_source fired) a Sources panel + citation; Explorer search → select → neighbors/subgraph; Manage toggle + jobs; Users role card. Screenshot each. Record results.
- [ ] **Step 4: Commit, push, PR (do NOT merge).**

```bash
git add -A && git commit -m "feat: Login redesign; UI redesign polish"
git push -u origin worktree-ui-redesign
gh pr create --repo timeplus-io/timeplus-knowledge --base main \
  --title "feat: Timeplus Console UI redesign — sidebar shell, Explorer, citations" \
  --body "Implements docs/design/timeplus-knowledge-mockups.html … (summarize screens; note new backend: /api/graph/* + chat source events; include E2E screenshots)"
```

## After this plan
- Dark-mode pass (mockup "try next").
- Per-user chat history (#10) integrates with the new Chat shell.
- Subgraph could grow into an interactive graph view.
