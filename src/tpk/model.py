"""Graph domain model: nodes, edges, stable IDs, stream-row conversion."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: str
    repo: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    summary: str
    community: str
    visibility: str


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    rel: str
    confidence: str
    repo: str


NODE_COLUMNS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility", "updated_at",
]

EDGE_COLUMNS = ["src", "dst", "rel", "confidence", "repo", "updated_at"]


def node_id(repo: str, kind: str, qualified_name: str) -> str:
    return hashlib.sha256(f"{repo}:{kind}:{qualified_name}".encode()).hexdigest()[:16]


def node_rows(nodes: list[Node], updated_at) -> list[list]:
    return [
        [n.id, n.repo, n.kind, n.name, n.qualified_name, n.file_path,
         n.line_start, n.line_end, n.summary, n.community, n.visibility, updated_at]
        for n in nodes
    ]


def edge_rows(edges: list[Edge], updated_at) -> list[list]:
    return [[e.src, e.dst, e.rel, e.confidence, e.repo, updated_at] for e in edges]
