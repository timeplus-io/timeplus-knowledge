"""Read-only graph query endpoints for the Explorer UI. Role-scoped like chat;
requires a valid user, not admin."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

import tpk.auth as auth_mod
from tpk.tools import ROLE_SCOPE


def create_graph_router(kg, auth, prefix: str = "") -> APIRouter:
    router = APIRouter(prefix="/api/graph")

    async def _scoped(user, fn):
        """Resolve the caller's role scope (fail-closed to an empty scope
        when the role is missing/unreadable) and run the blocking KG call
        under it, off the event loop -- mirrors server.py's /chat handler."""
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
                     limit: int = Query(20, ge=1, le=200), user=Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        kinds = [kind] if kind and kind != "any" else None
        repos = [repo] if repo and repo != "any" else None
        rows = await _scoped(user, lambda: kg.search_entities(q, kinds=kinds, repos=repos, limit=limit))
        return {"results": rows}

    @router.get("/entity")
    async def entity(id: str, user=Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        row = await _scoped(user, lambda: kg.get_entity(id))
        if not row:
            raise HTTPException(404, "no such entity")
        return row

    @router.get("/neighbors")
    async def neighbors(id: str, direction: str = "both", depth: int = 1,
                        limit: int = Query(50, ge=1, le=200), user=Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        result = await _scoped(user, lambda: kg.neighbors(id, direction=direction, depth=depth))
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        center = nodes_by_id.get(id)
        if center is None:
            # Either the id doesn't exist, or it does but sits outside the
            # caller's role scope -- same 404 contract as /entity so a
            # scoped user can't distinguish the two.
            raise HTTPException(404, "no such entity")

        edges_out = []
        for e in result["edges"][:limit]:
            if e["src"] == id:
                edge_dir, other_id = "out", e["dst"]
            elif e["dst"] == id:
                edge_dir, other_id = "in", e["src"]
            else:
                # A further-hop edge (depth > 1) that doesn't touch the
                # center directly -- neither endpoint is "the other side of
                # center", so there's no meaningful in/out; report it as
                # such rather than mislabeling a direction.
                edge_dir, other_id = "indirect", e["dst"]
            other_node = nodes_by_id.get(other_id)
            other = (
                {"id": other_node["id"], "name": other_node["name"], "kind": other_node["kind"]}
                if other_node else {"id": other_id, "name": None, "kind": None}
            )
            edges_out.append({
                "rel": e["rel"],
                "confidence": e["confidence"],
                "direction": edge_dir,
                "other": other,
            })
        return {"center": center, "edges": edges_out}

    @router.get("/source")
    async def source(repo: str, file_path: str,
                     line_start: int = Query(..., ge=1), line_end: int = Query(..., ge=1),
                     user=Depends(auth.require_cap(auth_mod.CAP_EXPLORE))):
        try:
            lines = await _scoped(user, lambda: kg.read_source(repo, file_path, line_start, line_end))
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {"repo": repo, "file_path": file_path, "line_start": line_start,
                "line_end": line_end, "lines": lines}

    return router
