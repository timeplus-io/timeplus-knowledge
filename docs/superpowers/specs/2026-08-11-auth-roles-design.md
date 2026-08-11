# Authentication & role-based repo access — design (issue #8)

Approved 2026-08-11. Adds login-based authentication to the knowledge
stack, roles that scope which corpus entries a user can query, seeded
admin bootstrap with forced password change, and a dedicated timeplusd
DB user for the compose stack.

## Goals

- Users log in; every `/chat` and `/api/*` request is authenticated.
- A **role** is a named definition listing the exact corpus entry keys
  (`name@ref`) its members may query in chat.
- `admin` is a built-in role: all repos, corpus management, user/role
  management. Custom roles are chat-only.
- Fresh deployments seed `admin`/`changeme`; the admin must change the
  password before doing anything else.
- Credentials stored hashed (argon2id); session tokens stored hashed.
- The compose stack stops using timeplusd's open `default` user.

## Non-goals (v1)

- Per-node `visibility` (`internal`/`public`) plays no part in
  authorization; the field stays in the data model, unused.
- No rate limiting / lockout on login (future work).
- MCP server (`tpk-mcp`) and CLI stay ungated: they run via
  `docker compose exec` or locally — host access is already privileged.
  They see all enabled entries, unrestricted by roles.
- No external identity providers, no password reset by email.

## Data model

Three mutable streams beside `kg_repos`, managed by a new
`src/tpk/auth.py` following the `corpus.py` store pattern
(`ensure_schema` creates them; `TPK_STREAM_PREFIX` respected):

- `kg_users` — key `username`; columns: `password_hash` (argon2id
  string), `role` (role name or `admin`), `must_change_password` (bool),
  `disabled` (bool), `created_at`, `updated_at`.
- `kg_roles` — key `name`; columns: `entry_keys` (JSON array of exact
  `name@ref` strings), `description`, `updated_at`. **`admin` is a
  reserved name, never a row**: `user.role == "admin"` short-circuits
  to full access, and role CRUD rejects the name `admin`.
- `kg_sessions` — key `token_hash` (SHA-256 hex of the opaque token);
  columns: `username`, `expires_at`, `created_at`. Raw tokens are never
  stored or logged.

Seeding: on `tpk serve` start (same place the corpus seed runs), if
`kg_users` has no rows, insert `admin` with the argon2id hash of
`changeme`, `role="admin"`, `must_change_password=true`. Never re-seed
once any user exists (deleting the last user then restarting re-seeds —
acceptable break-glass for a locked-out admin, documented in README).

## Auth API (new router in `auth.py`, mounted like `create_api_router`)

- `POST /auth/login` `{username, password}` → verify against argon2id
  hash; on success mint `secrets.token_urlsafe(32)`, store its SHA-256
  with expiry `now + TPK_SESSION_TTL` (env, seconds, default 86400),
  return `{token, role, must_change_password}`. Failures (unknown user,
  wrong password, disabled) all return the same 401 "invalid
  credentials" — no user enumeration.
- `POST /auth/logout` → delete the caller's session row; 204.
- `GET /auth/me` → `{username, role, must_change_password}`.
- `POST /auth/change-password` `{old_password, new_password}` → verify
  old; policy: ≥ 8 chars, ≠ `changeme`, ≠ old. On success re-hash,
  clear `must_change_password`; the current session stays valid, all
  *other* sessions for the user are deleted.

Requests authenticate with `Authorization: Bearer <token>`. Session
lookup hashes the presented token and reads `kg_sessions`; expired rows
are deleted lazily on lookup. The web UI streams `/chat` over `fetch()`
POST (not EventSource), so the header works everywhere.

While `must_change_password` is true, every authenticated endpoint
except `/auth/change-password`, `/auth/me`, `/auth/logout` returns 403
with `{"code": "password_change_required"}` so the UI can force the
flow.

## Authorization enforcement

FastAPI dependencies in `auth.py`:

- `require_user` — valid unexpired session, user exists and not
  disabled. 401 otherwise. Applies the must-change 403 gate.
- `require_admin` — `require_user` + `role == "admin"`; 403 otherwise.

Route coverage:

- `/chat` → `require_user`.
- `/api/repos*`, `/api/jobs*` → `require_admin` (replaces the
  `TPK_ADMIN_TOKEN` / `X-Admin-Token` mechanism, which is **removed**,
  including the path-entries-require-token rule — path-type entries now
  simply require admin like everything else).
- New `/api/users` and `/api/roles` CRUD → `require_admin`.
  - Users: create (initial password + `must_change_password` flag),
    assign role, disable/enable, reset password (sets
    `must_change_password=true`), delete. Disabling or deleting a user
    deletes their sessions. The last admin user cannot be deleted,
    disabled, or demoted (400).
  - Roles: create/update (name + `entry_keys` + description), delete.
    Creating a user with a nonexistent role → 400. Deleting a role
    still assigned to any user → 409. `entry_keys` are validated for
    shape only (non-empty strings) — they may reference entries that
    don't exist yet; unknown keys simply match nothing.
- `/healthz` and static UI assets stay unauthenticated.

Chat repo filtering: for a non-admin user, the chat handler resolves
the role's `entry_keys` and sets a request-scoped `ContextVar`. The
`KnowledgeGraph` corpus filter (already applied in `search_entities`,
`list_communities`, `_nodes_by_ids`, `_edges_touching`, plus
`read_source` path resolution) intersects the enabled-entries set with
the ContextVar set when one is present. Admin/MCP/CLI set nothing and
behave exactly as today. Empty intersection → tools return no results
(the agent answers that it has no accessible knowledge); not an HTTP
error. The ContextVar is set per request and reset in a `finally`, so
worker reuse across requests cannot leak a previous user's scope.

## Web UI

- Login screen gates the app; on success stores the token in
  `sessionStorage` and loads `/auth/me`. Any 401 from any fetch clears
  the token and returns to login.
- Forced password-change screen when `must_change_password` (login
  response or any `password_change_required` 403).
- Header: current username, role, logout button.
- Manage tab renders only for admin; its admin-token input is removed;
  all fetches send the bearer header.
- New admin-only **Users** tab: users table (create, assign role,
  disable, reset password, delete) and roles table (create/edit with a
  checkbox picker of current corpus entry keys from `/api/repos`,
  delete). Timeplus Console tokens, same patterns as Manage.

## timeplusd DB user (compose stack only)

The all-in-one image's entrypoint renders
`/etc/timeplusd-server/users.d/tpk-users.yaml` from env before starting
timeplusd:

- user `tpk` with `TIMEPLUS_PASSWORD` from `.env` (required in compose;
  entrypoint fails fast with a clear message if unset), full rights on
  the knowledge streams.
- `default` user restricted to loopback (`::1`/`127.0.0.1`) so it stops
  being reachable through the published 8123 port or the docker
  network; in-container CLI (`docker compose exec tpk tpk ...`) may
  still use it.
- `tpk` and `agent` services get `TIMEPLUS_USER=tpk` /
  `TIMEPLUS_PASSWORD` via compose environment (config already reads
  these envs — `src/tpk/config.py:24-25`).

Dev outside compose (bare timeplusd container, pytest with
`TIMEPLUS_HOST=localhost`, README manual queries) keeps the open
`default` user — no test or contributor friction.

## Error handling

- 401: missing/invalid/expired token, bad login.
- 403: non-admin on admin routes; `password_change_required` gate.
- 400: password policy violations, reserved role name, nonexistent
  role on user create, last-admin protection.
- 409: deleting a role in use.
- Provider/DB errors surface as 503 in auth dependencies (fail closed —
  never grant access when the user store is unreadable).

## Testing

Existing integration style (`TIMEPLUS_HOST=localhost`, `_eventually`
polling, per-test stream prefixes):

- auth store: hash round-trip (argon2id verify), user/role/session
  CRUD, seed idempotence.
- login/logout/expiry/disabled/uniform-401; change-password policy and
  other-session invalidation; must-change 403 gate.
- route coverage: every `/api` route 401 without token, 403 with
  non-admin token, 200 with admin; `/chat` requires any user.
- role filtering at the `KnowledgeGraph` level: allowed ∩ enabled,
  empty intersection, ContextVar reset between requests.
- role CRUD: reserved `admin`, delete-in-use 409, last-admin
  protection.
- UI: `npm run build` + `npm run check:sanitize`.
- Live E2E gate before PR: fresh stack, seeded login, forced change,
  create role+user, verify chat scoping, verify `default` DB user is
  unreachable from host.

## Migration

- `TPK_ADMIN_TOKEN` removed from compose, `.env.example`, README, UI.
- `.env.example` gains `TIMEPLUS_PASSWORD` (required for compose).
- README: "Users & roles" subsection (model, bootstrap, break-glass:
  a locked-out operator deletes all `kg_users` rows with direct SQL
  against timeplusd — the API protects the last admin — and restarts
  to re-seed), updated Manage docs, DB-user note.
- Existing deployments: first start after upgrade creates the streams
  and seeds `admin`/`changeme`; operators must set `TIMEPLUS_PASSWORD`
  in `.env` before `docker compose up`.

## New dependency

- `argon2-cffi` (password hashing).
