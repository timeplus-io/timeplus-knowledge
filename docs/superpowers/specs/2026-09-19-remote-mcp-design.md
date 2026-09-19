# Remote MCP: streamable-HTTP transport + per-user API tokens

Issue: #74. Date: 2026-09-19. Status: approved design.

## Goal

After tpk is deployed on Kubernetes, a user's coding agent connects to the
knowledge graph over MCP with one URL and one token:

    claude mcp add --transport http timeplus-knowledge https://<host>/mcp \
      --header "Authorization: Bearer tpk_…"

The MCP caller **is a tpk user**. It gets exactly that user's access: same
capabilities, same role corpus scope. There is no separate permission model
for MCP.

## Current state

- `src/tpk/mcp_server.py` serves six `KnowledgeGraph` tools over **stdio
  only**; on k8s it is reachable only via `kubectl exec`.
- It is unauthenticated and unscoped: it bypasses `ROLE_SCOPE` and the
  `source:view` gate on `read_source`. Acceptable for local stdio, not for a
  network endpoint.
- Login sessions (`kg_sessions`) are short-lived and minted by
  `/auth/login`; unsuitable for an agent's config file.

## Decisions

| Topic | Decision |
|---|---|
| Transport | Streamable HTTP, mounted in the existing `tpk serve` FastAPI app at `/mcp`. Stateless, JSON responses. |
| Auth integration | Our own ASGI wrapper around the mounted MCP app (not the SDK's OAuth `AuthSettings`, which advertises OAuth metadata we do not implement; not a separate process/port). |
| Credential | Long-lived per-user API tokens, new store. Valid **only** at `/mcp`; the REST API stays session-only. |
| Access gate | Existing `explore` capability. No new capability. |
| Expiry | Optional: never (default) / 30 / 90 / 365 days. |
| UI | New sidebar page "API tokens" for users with `explore`; admin bulk-revoke on the Users page. |
| stdio MCP | Unchanged, still unrestricted (local/dev use). |

## 1. Transport — `src/tpk/mcp_http.py` (new)

- `mcp_server.build_server(kg, guard=None)` gains an optional `guard`
  (section 3). With `guard=None` (stdio) behaviour is exactly as today.
- `mcp_http.create_mcp_asgi(kg, auth, prefix)` builds the server with a guard,
  takes `server.streamable_http_app(streamable_http_path="/mcp",
  stateless_http=True, json_response=True, transport_security=…)`, and wraps
  it in the auth wrapper (section 3). It returns the ASGI app plus the
  session manager's lifespan context.
- `create_app()` mounts it so the public path is exactly `/mcp`, and runs the
  SDK session manager inside the FastAPI lifespan (`session_manager.run()`);
  without that the transport has no task group.
- Stateless mode: no server-side MCP session, so it works behind the NLB and
  with more than one replica, no sticky sessions.
- The KG passed in is the app's existing `kg`; `KnowledgeGraph._query_rows`
  already serializes access to its client, so no second unguarded client.
- DNS-rebinding protection: the SDK's default allow-list is localhost-only and
  would reject `knowledge.timeplus.com`. The endpoint is bearer-authenticated
  (no cookies/ambient credentials), so protection is **off by default**;
  `TPK_MCP_ALLOWED_HOSTS` / `[server].mcp_allowed_hosts` (comma-separated)
  turns it on with that allow-list.
- Switch: `TPK_MCP_HTTP_ENABLED` / `[server].mcp_http`, default `true`, via
  `config.setting()` (env > file > default). When off, `/mcp` is not mounted
  (404).
- When `kg` is not available (test path that injects only `agent`), `/mcp` is
  not mounted — same rule as the graph router.

## 2. Token store — `kg_api_tokens`

Mutable stream, created in `db.ensure_schema`, helpers in `auth.py` next to
the session helpers.

| Column | Notes |
|---|---|
| `token_hash` | sha256 hex of the plaintext; primary key. |
| `token_id` | short public id (`secrets.token_hex(6)`), used to list/revoke. |
| `username` | owner. |
| `name` | user-chosen label, 1–64 chars. |
| `hint` | last 4 chars of the plaintext, display only. |
| `created_at` | |
| `expires_at` | nullable; null = never. |
| `last_used_at` | nullable. |

- Plaintext format: `tpk_` + `secrets.token_urlsafe(32)`. Returned once by the
  create call; only the hash is stored.
- Helpers: `create_api_token`, `list_api_tokens(username)`,
  `resolve_api_token(token) -> ApiToken | None` (expired → treated as absent
  and deleted, like `get_session`), `revoke_api_token(username, token_id)`,
  `delete_user_api_tokens(username)`, `touch_api_token`.
- `last_used_at` is written at most once per 5 minutes per token (skip the
  write when the stored value is fresher) so a busy agent does not hammer the
  stream.
- Per-user cap: 20 live tokens (create → 409 beyond that).
- Lifecycle: deleting or disabling a user deletes their tokens (same call
  sites as `delete_user_sessions`). A password change does **not** revoke
  tokens — they are independent credentials; revocation is explicit.

## 3. Authentication & authorization

### Wrapper (per HTTP request to `/mcp`)

Order, fail-closed, status codes matching the REST API:

1. No / non-`Bearer` header, or token not starting with `tpk_` → **401**.
2. Token unknown, expired, or revoked → **401**.
3. User missing or disabled → **401**.
4. `must_change_password` → **403** `password_change_required`.
5. `explore` not in effective capabilities → **403** `missing capability: explore`.
6. Auth store unreachable → **503**.

401 responses carry a plain `WWW-Authenticate: Bearer` (no OAuth
`resource_metadata`). Blocking store calls run in the threadpool. On success
the resolved `User` and `token_id` are placed on the ASGI `scope["state"]`,
and `touch_api_token` runs (throttled).

### Guard (per tool call)

`guard` is a callable the tool handlers use to run the blocking KG call:
`guard(ctx, fn, *, needs_source=False)`.

- Reads the `User` from the MCP `Context`'s underlying Starlette request
  (`request.state`), not from a ContextVar set in the wrapper — the transport
  may run the handler in a different task.
- Non-admin: loads the role; scope = `frozenset(role.entry_keys)`, or the
  empty set when the role is missing/unreadable (fail closed). Admin: no
  scope.
- `needs_source=True` (`read_source`): requires `source:view` in the user's
  effective capabilities, else raises a tool error
  `missing capability: source:view`.
- Sets `ROLE_SCOPE` **inside** the function submitted to the threadpool and
  resets it in `finally`, so the ContextVar is set in the thread that runs the
  query.
- Logs one line per call: username, token_id, tool name.

Role, scope, and capabilities are resolved on every call, so an admin's
change takes effect on the user's next MCP call with no token reissue.

`graph_api._scoped` and this guard share the scope-resolution logic; extract
it to one helper (`auth.resolve_scope(client, user, prefix)`) used by both.

## 4. REST API (`api.py`)

| Endpoint | Gate | Behaviour |
|---|---|---|
| `GET /api/tokens[?username=]` | `explore`; `username` ≠ self needs `users:manage` | List tokens (never the hash or plaintext). |
| `POST /api/tokens` `{name, expires_days?}` | `explore` | Create for self. `expires_days` ∈ {30, 90, 365} or omitted. Returns the record plus `token` (plaintext, once). 409 at the per-user cap. |
| `POST /api/tokens/revoke` `{token_id, username?}` | `explore`; other user needs `users:manage` | Revoke one token. 404 if not found for that user. |
| `POST /api/tokens/revoke-all` `{username}` | `users:manage` | Revoke all of a user's tokens. |

Follows the existing POST-action style (`/repos/delete`, `/users/delete`).

## 5. Web UI

- `web/src/Tokens.tsx`, sidebar entry "API tokens", shown when the user has
  `explore` (`capabilities.ts`).
- Table: name, `…hint`, created, expires, last used, Revoke.
- "New token" modal: name + expiry select (Never / 30 / 90 / 365 days).
- After create: copy-once panel with the plaintext and a ready-to-paste
  command built from `window.location.origin`:
  `claude mcp add --transport http timeplus-knowledge <origin>/mcp --header "Authorization: Bearer <token>"`.
  Closing the panel discards the plaintext.
- `Users.tsx`: per-user "Revoke API tokens" action for `users:manage`.
- Styling per the existing Timeplus Console tokens in `app.css`.

## 6. Audit

Per tool call: a log line (username, token_id, tool) and the throttled
`last_used_at`. `audit.py` is a chat-turn stream; a queryable MCP audit
stream is a follow-up, not part of this work. MCP tools make no LLM calls, so
the daily token budget does not apply.

## 7. Deploy & docs

- No new port, Service, or ingress: `/mcp` rides the existing app listener.
  Confirm the manifests need no change; document the NLB idle timeout only if
  testing shows it matters (JSON responses, no long-lived stream).
- README "Use from Claude Code (MCP)", `docs/FEATURES.md` §5, and
  `deploy/k8s/README.md`: add remote setup (create token → `claude mcp add`),
  new config keys, and replace "MCP is unrestricted" with "stdio MCP is
  unrestricted; remote MCP runs as the token's user". `.env.example` and the
  `[server]` section of `repos.toml` / `repos.container.toml` gain the two
  new keys.

## 8. Testing

- Token store: create / list / resolve / expiry / revoke / cap /
  delete-with-user / touch throttle.
- REST: own vs other-user access matrix, plaintext returned once, hash never
  returned.
- `/mcp` over the mounted app (in-process HTTP client, real MCP
  initialize + tools/list + tools/call): 401 / 403 / 503 matrix; session
  token rejected; disabled flag → 404.
- Scope isolation for **each** of the six tools with a scoped user vs admin,
  including `path_between` and `read_source`.
- `source:view` gate on `read_source`.
- Role change takes effect on the next call with the same token.
- stdio `build_server(kg)` without a guard behaves as before
  (`tests/test_mcp_server.py` stays green).
- Web: `npm run build` + existing check gates.
- Manual: `claude mcp add --transport http …` against a local `tpk serve`,
  `claude mcp list` ✔, exercise a tool.

## Out of scope

- OAuth 2.1 / dynamic client registration.
- API tokens for the REST API.
- Write/management operations or the chat agent as MCP tools.
- Queryable MCP audit stream; per-token rate limiting.
