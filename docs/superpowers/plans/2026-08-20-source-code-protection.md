# Source-Code Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the agent from handing users complete source, while leaving `read_source` tool access and model reasoning unrestricted.

**Architecture:** One new default-deny capability, `source:view`, gates the two channels that can leak raw source to the user — the thinking trace (suppressed in the chat SSE stream) and citation-fragment bodies (the `GET /api/graph/source` endpoint). The answer channel is protected by a system-prompt rule only (no code enforcement). Admin holds all capabilities and is unaffected.

**Tech Stack:** Python (FastAPI, pytest), TypeScript/React (Vite), Timeplus streams for the auth store.

**Spec:** `docs/superpowers/specs/2026-08-20-source-code-protection-design.md`

## Global Constraints

- Capability string is exactly `source:view` (backend constant `CAP_SOURCE_VIEW`, frontend `CAP.sourceView`).
- `source:view` is **NOT** in `DEFAULT_CAPABILITIES` — default-deny. Only `admin` (via `ALL_CAPABILITIES`) and explicit grants hold it.
- No `:manage` sibling — it is a bare grant like `chat`/`explore`; do not touch `_MANAGE_IMPLIES_VIEW`.
- Reference rows (`file:line`) stay visible to all chat users; only the source **body** is gated.
- Backend tests that touch Timeplus carry `pytestmark = requires_timeplus`; pure-unit tests do not. Follow the existing per-file convention.
- Every task ends with a passing test run and a commit.

---

### Task 1: Add the `source:view` capability (backend model)

**Files:**
- Modify: `src/tpk/auth.py:26-38` (capability constants + `ALL_CAPABILITIES`)
- Test: `tests/test_capabilities.py` (unit section, no store)

**Interfaces:**
- Produces: `auth.CAP_SOURCE_VIEW = "source:view"`, present in `auth.ALL_CAPABILITIES`, absent from `auth.DEFAULT_CAPABILITIES`. Later tasks import `CAP_SOURCE_VIEW` from `tpk.auth`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capabilities.py` (import `CAP_SOURCE_VIEW` and `DEFAULT_CAPABILITIES` in the existing `from tpk.auth import (...)` block):

```python
def test_source_view_capability_registered_and_default_deny():
    assert CAP_SOURCE_VIEW == "source:view"
    assert CAP_SOURCE_VIEW in ALL_CAPABILITIES
    # Default-deny: brand-new roles must not silently hold it.
    assert CAP_SOURCE_VIEW not in DEFAULT_CAPABILITIES
    # It is a bare grant (no :manage sibling), so expand is identity.
    assert expand_capabilities([CAP_SOURCE_VIEW]) == {CAP_SOURCE_VIEW}
    # Admin holds every capability, including this one.
    admin = User("a", "", auth.ROLE_ADMIN)
    assert CAP_SOURCE_VIEW in effective_capabilities(admin, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities.py::test_source_view_capability_registered_and_default_deny -v`
Expected: FAIL with `ImportError` (cannot import `CAP_SOURCE_VIEW`) or `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

In `src/tpk/auth.py`, add the constant after `CAP_USERS_MANAGE` (line 31):

```python
CAP_USERS_MANAGE = "users:manage"
CAP_SOURCE_VIEW = "source:view"
```

Append it to `ALL_CAPABILITIES` (keep it last — canonical display order):

```python
ALL_CAPABILITIES = [
    CAP_CHAT, CAP_EXPLORE,
    CAP_CORPUS_VIEW, CAP_CORPUS_MANAGE,
    CAP_USERS_VIEW, CAP_USERS_MANAGE,
    CAP_SOURCE_VIEW,
]
```

Leave `DEFAULT_CAPABILITIES` and `_MANAGE_IMPLIES_VIEW` unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities.py::test_source_view_capability_registered_and_default_deny -v`
Expected: PASS

- [ ] **Step 5: Run the capability unit tests to confirm no regressions**

Run: `pytest tests/test_capabilities.py -k "expand or effective or parse" -v`
Expected: PASS (existing `test_expand_capabilities_*`, `test_effective_capabilities_*` unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/tpk/auth.py tests/test_capabilities.py
git commit -m "feat(auth): add default-deny source:view capability (#66)"
```

---

### Task 2: Gate `GET /api/graph/source` on `source:view`

**Files:**
- Modify: `src/tpk/graph_api.py:85-94` (the `/source` route dependency)
- Test: `tests/test_graph_api.py` (update `scoped_hdr` fixture; add one test)

**Interfaces:**
- Consumes: `auth.CAP_SOURCE_VIEW` (Task 1).
- Produces: `/api/graph/source` requires `source:view` (admin bypasses via `effective_caps`); `search`/`entity`/`neighbors` keep `CAP_EXPLORE`.

- [ ] **Step 1: Write the failing test**

The `scoped_hdr` fixture builds an `alpha-only` role with `capabilities=[auth.CAP_EXPLORE]`. Because `/source` will now require `source:view`, that fixture must also grant it so the existing scope test (`test_non_admin_source_out_of_scope_404`) still exercises scoping rather than the new cap gate. Update the fixture (`tests/test_graph_api.py:120-121`):

```python
    auth.upsert_role(client, auth.Role("alpha-only", ["alpha@v1"],
                                       capabilities=[auth.CAP_EXPLORE, auth.CAP_SOURCE_VIEW]), prefix=prefix)
```

Then add a new test that a role with `explore` but **not** `source:view` is refused on `/source` yet still allowed on `/search`:

```python
def test_source_requires_source_view_cap(kg_app):
    """`/source` now requires source:view; a role with explore-only can still
    search/entity/neighbors but is refused the raw source body."""
    c, client, prefix = kg_app
    auth.upsert_role(client, auth.Role("explore-only", ["alpha@v1"],
                                       capabilities=[auth.CAP_EXPLORE]), prefix=prefix)
    auth.upsert_user(client, auth.User("exp", auth.hash_password("password-1"),
                                       "explore-only"), prefix=prefix)
    _eventually(lambda: c.post("/auth/login",
                json={"username": "exp", "password": "password-1"}).status_code == 200)
    token = c.post("/auth/login", json={"username": "exp", "password": "password-1"}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    # search still works with explore alone...
    assert c.get("/api/graph/search", params={"q": "AlphaWidget"}, headers=hdr).status_code == 200
    # ...but the source body is gated behind source:view.
    assert c.get("/api/graph/source", params={
        "repo": "alpha@v1", "file_path": "w.py", "line_start": 1, "line_end": 2,
    }, headers=hdr).status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_graph_api.py::test_source_requires_source_view_cap -v`
Expected: FAIL — returns 200 (or a 404 scope/file miss), not 403, because `/source` still only requires `CAP_EXPLORE`.

- [ ] **Step 3: Write minimal implementation**

In `src/tpk/graph_api.py`, change the `/source` route's dependency from `CAP_EXPLORE` to `CAP_SOURCE_VIEW` (line 88). Leave `search`/`entity`/`neighbors` on `CAP_EXPLORE`:

```python
    @router.get("/source")
    async def source(repo: str, file_path: str,
                     line_start: int = Query(..., ge=1), line_end: int = Query(..., ge=1),
                     user=Depends(auth.require_cap(auth_mod.CAP_SOURCE_VIEW))):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_graph_api.py::test_source_requires_source_view_cap -v`
Expected: PASS

- [ ] **Step 5: Run the full graph-api suite to confirm the fixture change is consistent**

Run: `pytest tests/test_graph_api.py -v`
Expected: PASS. In particular `test_admin_source_returns_lines`, `test_admin_source_reads_beta_repo`, and `test_non_admin_source_out_of_scope_404` still pass (admin bypasses the cap; `scoped_hdr` now holds `source:view` so its 404 is a genuine scope block). `test_explore_capability_required` still passes (its `no-explore` role holds neither `explore` nor `source:view`, so `/source` is still 403).

- [ ] **Step 6: Commit**

```bash
git add src/tpk/graph_api.py tests/test_graph_api.py
git commit -m "feat(graph-api): require source:view for /api/graph/source (#66)"
```

---

### Task 3: Suppress `thinking` SSE events without `source:view`

**Files:**
- Modify: `src/tpk/server.py:266-286` (compute the flag) and `:339-341` (gate the emit)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `auth_mod.CAP_SOURCE_VIEW`, `auth_mod.expand_capabilities` (Task 1); the already-resolved `role` in the non-admin branch.
- Produces: the chat SSE stream omits every `{"type": "thinking"}` event when the caller lacks `source:view`; all other event types are unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`. These monkeypatch `tpk.auth.get_role` so a non-admin role resolves without a real store, and give the stub a non-raising `_client` so the handler's role lookup reaches the patched `get_role`:

```python
def _think_agent():
    # One thinking delta, then an answer token.
    return FakeAgent([_think("secret internal reasoning"), _tok("the answer")])


def _nonadmin_auth_with_role(monkeypatch, caps):
    from tpk import auth as auth_mod
    role = auth_mod.Role("r", [], capabilities=list(caps))
    monkeypatch.setattr(auth_mod, "get_role", lambda *a, **k: role)
    a = _StubAuth(User("u", "", "r"))          # non-admin user
    a._client = lambda: None                    # reach patched get_role, don't raise
    return a


def test_chat_suppresses_thinking_without_source_view(monkeypatch):
    from tpk.auth import CAP_CHAT
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT])  # no source:view
    client = TestClient(create_app(agent=_think_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert not any(e["type"] == "thinking" for e in events)
    # The answer itself still streams.
    assert any(e["type"] == "token" for e in events)


def test_chat_emits_thinking_with_source_view(monkeypatch):
    from tpk.auth import CAP_CHAT, CAP_SOURCE_VIEW
    auth = _nonadmin_auth_with_role(monkeypatch, [CAP_CHAT, CAP_SOURCE_VIEW])
    client = TestClient(create_app(agent=_think_agent(), auth=auth))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert [e["text"] for e in events if e["type"] == "thinking"] == ["secret internal reasoning"]


def test_chat_admin_still_emits_thinking():
    # Default _StubAuth user is admin -> holds every capability.
    client = TestClient(create_app(agent=_think_agent(), auth=_StubAuth()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert any(e["type"] == "thinking" for e in events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -k "thinking_without_source_view or thinking_with_source_view" -v`
Expected: FAIL — `test_chat_suppresses_thinking_without_source_view` finds a `thinking` event (gate not implemented yet).

- [ ] **Step 3: Write minimal implementation**

In `src/tpk/server.py`, compute `can_view_source` in the `/chat` handler. Initialize it to the admin default before the non-admin branch (near line 266, alongside `scope = None`):

```python
        scope = None
        can_view_source = (user.role == auth_mod.ROLE_ADMIN)
        turn_limit = 0  # effective daily token budget for this user (0 = unlimited)
        if user.role != auth_mod.ROLE_ADMIN:
```

Inside the non-admin branch, after `scope = frozenset(role.entry_keys) if role else frozenset()` (line 281), set the flag from the same resolved `role` (fails closed to no cap when the role is missing/unreadable):

```python
            caps = auth_mod.expand_capabilities(role.capabilities) if role else set()
            can_view_source = auth_mod.CAP_SOURCE_VIEW in caps
```

Then gate the emit at line 340-341:

```python
                            thinking = _chunk_thinking(chunk)
                            if thinking and can_view_source:
                                yield _sse({"type": "thinking", "text": thinking})
```

(`can_view_source` is captured by the inner `stream()` closure, exactly as `scope` already is.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -k "thinking" -v`
Expected: PASS — including the pre-existing `test_chat_streams_thinking_interleaved_with_tools`, `test_chat_streams_openai_reasoning_as_thinking`, and `test_chat_no_thinking_emits_no_thinking_events` (all use the default admin `_StubAuth`, so `can_view_source` is `True`).

- [ ] **Step 5: Run the full server suite**

Run: `pytest tests/test_server.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tpk/server.py tests/test_server.py
git commit -m "feat(server): suppress thinking trace without source:view (#66)"
```

---

### Task 4: Add the answer-protection rule to the system prompt

**Files:**
- Modify: `src/tpk/agent.py:145-148` (append a rule to the numbered `Rules:` block)
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `system_prompt(repos)` output contains an explicit instruction not to reproduce complete source.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py` (`system_prompt` is already imported):

```python
def test_system_prompt_forbids_dumping_complete_source():
    p = system_prompt(REPOS)
    # The model may explain and quote minimally, but must not reproduce whole files.
    assert "never reproduce complete" in p
    assert "decline" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py::test_system_prompt_forbids_dumping_complete_source -v`
Expected: FAIL — the phrases are absent.

- [ ] **Step 3: Write minimal implementation**

In `src/tpk/agent.py`, add a new rule as item 7, immediately before the closing `"""` of the prompt (after the current rule 6 that ends `...only in your private reasoning.`):

```python
6. Your last message MUST be a normal assistant reply containing the
   answer text itself — never end the conversation on a tool call or with
   an empty message, and never leave the answer only in your private
   reasoning.
7. PROTECT SOURCE CODE. Read and search the code freely to ground your
   answer, and quote only the SHORT snippets needed to explain a point —
   but never reproduce complete or near-complete files, and never
   reconstruct a whole file across several quotes. If the user asks you to
   print, dump, export, or output the full contents of a file, decline and
   offer to explain what it does or show the specific lines relevant to
   their question instead."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py::test_system_prompt_forbids_dumping_complete_source -v`
Expected: PASS

- [ ] **Step 5: Confirm the existing prompt test still passes**

Run: `pytest tests/test_agent.py::test_system_prompt_contains_corpus_and_citation_rules -v`
Expected: PASS (the citation/grounding rules are untouched).

- [ ] **Step 6: Commit**

```bash
git add src/tpk/agent.py tests/test_agent.py
git commit -m "feat(agent): system-prompt rule against dumping complete source (#66)"
```

---

### Task 5: Frontend — expose `source:view` and gate citation previews

**Files:**
- Modify: `web/src/capabilities.ts:5-22` (add `CAP.sourceView` + `CAPABILITY_OPTIONS` entry)
- Modify: `web/src/App.tsx:146` (pass the flag into `Chat`)
- Modify: `web/src/Chat.tsx` (`SourceCard` at `:150-187`, its call site at `:690`, and the `Chat` prop list at `:200-208`)

**Interfaces:**
- Consumes: `me.capabilities` from `/auth/me` (already fetched in `App.tsx`).
- Produces: role editor lists "View source" so admins can grant it; `SourceCard` shows a restricted note instead of fetching the fragment when the user lacks the cap. (Server already withholds `thinking` events and 403s `/api/graph/source`, so this is UX, not the enforcement boundary.)

> No automated frontend tests exist in `web/` (no vitest/jest, no `*.test.*`). Verification is a type-check/build plus manual check. Keep changes minimal.

- [ ] **Step 1: Add the capability to `capabilities.ts`**

In `web/src/capabilities.ts`, add to the `CAP` object:

```javascript
export const CAP = {
  chat: "chat",
  explore: "explore",
  corpusView: "corpus:view",
  corpusManage: "corpus:manage",
  usersView: "users:view",
  usersManage: "users:manage",
  sourceView: "source:view",
} as const;
```

And append to `CAPABILITY_OPTIONS` (keep it last, matching backend order):

```javascript
  { key: CAP.usersManage, label: "Users — manage", implies: CAP.usersView },
  { key: CAP.sourceView, label: "View source (thinking trace & citation code)" },
```

Leave `MANAGE_IMPLIES_VIEW` unchanged (no `:manage` sibling).

- [ ] **Step 2: Thread the flag from `App.tsx` into `Chat`**

In `web/src/App.tsx`, update the `Chat` render (line 146) to pass the capability:

```javascript
        <Chat initialInput={chatPrefill} onConsumeInitial={() => setChatPrefill(undefined)}
              canViewSource={hasCap(me.capabilities, CAP.sourceView)} />
```

(`hasCap` and `CAP` are already imported in `App.tsx`.)

- [ ] **Step 3: Accept the prop and gate `SourceCard` in `Chat.tsx`**

Add `canViewSource` to the `Chat` component's props (destructure at `:200-208`, and its type):

```javascript
export default function Chat({
  initialInput,
  onConsumeInitial,
  canViewSource = false,
}: {
  initialInput?: string;
  onConsumeInitial?: () => void;
  canViewSource?: boolean;
} = {}) {
```

Pass it to the `SourceCard` at line 690:

```javascript
                <SourceCard n={activeSource.n} source={activeSource} canViewSource={canViewSource} />
```

Update `SourceCard` (`:150-161`) to skip the fetch and show a restricted note when the cap is absent:

```javascript
function SourceCard({ n, source, canViewSource }: { n?: number; source: SourceEventPayload; canViewSource: boolean }) {
  const [preview, setPreview] = useState<SourceResponse | "loading" | "error" | "restricted">(
    canViewSource ? "loading" : "restricted",
  );

  useEffect(() => {
    if (!canViewSource) { setPreview("restricted"); return; }
    let cancelled = false;
    setPreview("loading");
    readSource(source.repo, source.file_path, source.line_start, source.line_end)
      .then((r) => { if (!cancelled) setPreview(r); })
      .catch(() => { if (!cancelled) setPreview("error"); });
    return () => { cancelled = true; };
  }, [canViewSource, source.repo, source.file_path, source.line_start, source.line_end]);
```

Add a branch in the render for the `"restricted"` state (alongside the `"loading"`/`"error"` branches at `:168-172`), so the card still shows the path + line range but not the body:

```javascript
      {preview === "loading" ? (
        <div className="tk-source-preview-status">Loading preview…</div>
      ) : preview === "restricted" ? (
        <div className="tk-source-preview-status">Source preview restricted for your role</div>
      ) : preview === "error" ? (
        <div className="tk-source-preview-status">Preview unavailable</div>
      ) : (
```

- [ ] **Step 4: Type-check and build**

Run: `npm --prefix web run build`
Expected: PASS — `tsc -b` reports no type errors and `vite build` completes. (Fix any prop-type mismatch surfaced here.)

- [ ] **Step 5: Manual verification (record result in the commit / PR)**

- As an admin: Chat shows the thinking trace, and clicking a citation renders the code preview. The role editor (Users view) lists "View source (thinking trace & citation code)".
- Create a non-admin role with `chat` + `explore` but **not** `source:view`; log in as a user in it: no thinking trace appears, and a citation card shows "Source preview restricted for your role" with the path/line range still visible.

- [ ] **Step 6: Commit**

```bash
git add web/src/capabilities.ts web/src/App.tsx web/src/Chat.tsx
git commit -m "feat(web): gate thinking trace & citation preview behind source:view (#66)"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the backend test suite**

Run: `pytest tests/test_capabilities.py tests/test_graph_api.py tests/test_server.py tests/test_agent.py -v`
Expected: PASS. (These four files cover every backend change; run the full `pytest` if the environment has a live Timeplus for the `requires_timeplus` integration tests.)

- [ ] **Step 2: Build the frontend**

Run: `npm --prefix web run build`
Expected: PASS.

- [ ] **Step 3: Open the PR**

Push the branch and open a PR referencing #66, summarizing the three enforcement points (prompt, thinking gate, `/api/graph/source` cap) and the default-deny migration note (existing non-admin roles lose the thinking panel and citation previews until an admin grants `source:view`).

---

## Self-Review

**Spec coverage:**
- New `source:view` capability, default-deny, admin-held → Task 1.
- Enforcement point 1 (answer / prompt) → Task 4.
- Enforcement point 2 (thinking trace suppressed in stream) → Task 3.
- Enforcement point 3 (`/api/graph/source` requires the cap) → Task 2.
- Frontend cap exposure + citation/thinking gating → Task 5.
- Migration/behavior-change note surfaced in the PR → Task 6, Step 3.
- Testing list from the spec (expand includes cap; endpoint 403/200/admin; thinking suppressed/present; scope not bypassed) → Tasks 1–3.

**Placeholder scan:** No TODO/TBD; every code and test step is concrete.

**Type/name consistency:** `CAP_SOURCE_VIEW`/`"source:view"`/`CAP.sourceView` used consistently across tasks; `can_view_source` defined before use and captured by the `stream()` closure; `SourceCard`'s new `canViewSource` prop is threaded from `App.tsx` → `Chat` → `SourceCard`; the `"restricted"` preview state is added to both the `useState` union and the render branches.
