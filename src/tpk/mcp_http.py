"""Remote MCP: the knowledge-graph tools over streamable HTTP at /mcp (#74).

The caller authenticates with a per-user API token (auth.create_api_token)
and IS that tpk user: `explore` gates access, every tool call runs under the
user's role corpus scope, and read_source additionally needs `source:view`.
stdio MCP (mcp_server.main) is separate and stays unrestricted.
"""

import logging

from fastapi import HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

import tpk.auth as auth_mod
from tpk.config import as_bool, setting
from tpk.mcp_server import build_server
from tpk.tools import ROLE_SCOPE

log = logging.getLogger("tpk.mcp")

MCP_PATH = "/mcp"


def enabled() -> bool:
    return as_bool(setting("TPK_MCP_HTTP_ENABLED", "server", "mcp_http", True))


def _transport_security() -> TransportSecuritySettings:
    """The SDK's DNS-rebinding allow-list defaults to localhost and would
    reject a real hostname. /mcp is bearer-authenticated (no ambient browser
    credentials), so protection is off unless an allow-list is configured."""
    hosts = [h.strip() for h in
             str(setting("TPK_MCP_ALLOWED_HOSTS", "server", "mcp_allowed_hosts", "")).split(",")
             if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)


class McpAuth:
    """ASGI wrapper: resolve the bearer API token to a tpk user before the
    MCP transport sees the request. Status codes mirror AuthLayer (REST)."""

    def __init__(self, app, auth, prefix: str = ""):
        self.app, self.auth, self.prefix = app, auth, prefix

    def _authenticate(self, authorization: str):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ")
        if not token.startswith(auth_mod.API_TOKEN_PREFIX):
            raise HTTPException(401, "invalid or expired token")
        try:
            client = self.auth._client()
            rec = auth_mod.resolve_api_token(client, token, prefix=self.prefix)
            user = auth_mod.get_user(client, rec.username, prefix=self.prefix) if rec else None
        except Exception:
            # Keep the cause: to an operator a bare 503 on /mcp is undiagnosable.
            log.exception("mcp auth store unavailable")
            raise HTTPException(503, "auth store unavailable")
        if user is None or user.disabled:
            raise HTTPException(401, "invalid or expired token")
        if user.must_change_password:
            raise HTTPException(403, detail={"code": "password_change_required"})
        if auth_mod.CAP_EXPLORE not in self.auth.effective_caps(user):
            raise HTTPException(403, f"missing capability: {auth_mod.CAP_EXPLORE}")
        return user, rec.token_id

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        authorization = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        try:
            user, token_id = await run_in_threadpool(self._authenticate, authorization)
        except HTTPException as e:
            # /mcp is the only network-reachable credential check, so every
            # rejection leaves a trace -- otherwise token probing is invisible.
            # The token itself is never logged, in whole or in part.
            log.warning("mcp auth denied status=%s detail=%s", e.status_code, e.detail)
            headers = {"WWW-Authenticate": "Bearer"} if e.status_code == 401 else None
            return await JSONResponse({"detail": e.detail}, status_code=e.status_code,
                                      headers=headers)(scope, receive, send)
        state = scope.setdefault("state", {})
        state["tpk_user"], state["tpk_token_id"] = user, token_id
        await self.app(scope, receive, send)


def make_guard(auth, prefix: str = ""):
    """The per-tool-call guard handed to build_server. The user comes from the
    request scope set by McpAuth (NOT a ContextVar: the transport may run the
    handler in another task). Role/caps are re-read on every call so an
    admin's change applies immediately. ROLE_SCOPE is set inside the worker
    thread that runs the KG query."""

    async def guard(ctx, fn, *, tool: str, needs_source: bool = False):
        state = ctx.request_context.request.scope.get("state", {})
        user = state.get("tpk_user")
        if user is None:  # unreachable behind McpAuth; fail closed anyway
            raise PermissionError("unauthenticated")
        log.info("mcp tool call user=%s token=%s tool=%s",
                 user.username, state.get("tpk_token_id"), tool)

        def work():
            if needs_source and auth_mod.CAP_SOURCE_VIEW not in auth.effective_caps(user):
                raise PermissionError(f"missing capability: {auth_mod.CAP_SOURCE_VIEW}")
            try:
                scope = auth_mod.resolve_scope(auth._client(), user, prefix=prefix)
            except Exception:
                # An empty scope looks exactly like "your role has no entries"
                # to the caller, so the outage must be visible server-side.
                log.exception("scope resolution failed; failing closed user=%s", user.username)
                scope = None if user.role == auth_mod.ROLE_ADMIN else frozenset()
            token = ROLE_SCOPE.set(scope) if scope is not None else None
            try:
                return fn()
            finally:
                if token is not None:
                    ROLE_SCOPE.reset(token)

        return await run_in_threadpool(work)

    return guard


def create_mcp_route(kg, auth, prefix: str = ""):
    """(route, server): add `route` to the FastAPI app and run
    `server.session_manager.run()` in the app lifespan."""
    server = build_server(kg, guard=make_guard(auth, prefix))
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
        transport_security=_transport_security(),
    )
    route = Route(MCP_PATH, endpoint=McpAuth(inner, auth, prefix),
                  methods=["GET", "POST", "DELETE"])
    return route, server
