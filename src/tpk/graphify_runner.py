"""Run graphify on a repo checkout and parse its graph.json output.

graphify (PyPI package `graphifyy`, CLI `graphify`) writes a networkx
node-link-format graph. Verified against real output (graphify 0.9.38,
`graphify extract <path> --code-only --out <dir>`, written to
`<dir>/graphify-out/graph.json`):

    {
      "directed": false, "multigraph": false, "graph": {},
      "nodes": [
        {"label": "app.py", "file_type": "code", "source_file": "app.py",
         "source_location": "L1", "id": "app", "community": 0,
         "norm_label": "app.py", "_origin": "ast"},
        {"label": "main()", "file_type": "code", "source_file": "app.py",
         "source_location": "L6", "_callable": true, "id": "app_main",
         "community": 1, "norm_label": "main()", "_origin": "ast"},
        {"label": "Tiny app used as a graphify fixture.",
         "file_type": "rationale", "source_file": "app.py",
         "source_location": "L1", "id": "app_rationale_1", ...}
      ],
      "links": [
        {"relation": "calls", "confidence": "EXTRACTED", "source": "app_main",
         "target": "helper_add", "source_file": "app.py",
         "source_location": "L8", "weight": 1.0, "confidence_score": 1.0}
      ],
      "hyperedges": [], "built_at_commit": "..."
    }

Notable differences from a naive "kind"/"name"/"edges" guess:
- Edges live under "links", not "edges" (bare networkx node_link_data shape).
- There is no "kind"/"type" field on nodes. Node category is derived from
  "file_type" ("code" | "rationale") plus the "_callable" flag: a callable
  code node is a `function`, a non-callable code node is the `file` itself,
  and a "rationale" node (an extracted docstring/comment) is a `comment`.
- There is no "name" field; the human name is derived from "label"
  (stripping a trailing "()" for callables).
- There is no "qualified_name"/"fqn" field; one is synthesized from
  source_file + name (or graphify's own id, for rationale nodes) so it is
  stable and unique within a file.
- File location is "source_file" (not "file"/"file_path"), and there is a
  single "source_location" like "L6" rather than separate line_start/
  line_end fields.
- Edge relation is "relation" (not "type"/"rel"), and endpoints are
  "source"/"target" (not "src"/"dst").

The `_first()` helper and its fallback key lists are kept so the parser
degrades gracefully if a future graphify version (or a different extractor
mode) renames or adds fields, but the *primary* keys above reflect what
graphify 0.9.38 actually emits, not a guess.
"""

import json
import subprocess
from pathlib import Path

from tpk.model import Edge, Node, node_id


class GraphifyError(RuntimeError):
    pass


def run_graphify(repo_path: Path, out_dir: Path) -> Path:
    """Run `graphify extract` on repo_path and return the path to graph.json.

    Uses --code-only: graphify's default `extract` attempts LLM-backed
    semantic extraction for non-code files (docs/papers/images) and errors
    out if no LLM API key is configured. Code parsing itself is always local
    tree-sitter AST with no network calls; --code-only guarantees run_graphify
    never requires an API key, at the cost of skipping non-code files
    (e.g. README.md) in the emitted graph.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["graphify", "extract", str(repo_path), "--code-only", "--out", str(out_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GraphifyError(f"graphify failed on {repo_path}: {proc.stderr[-2000:]}")
    graph_json = out_dir / "graphify-out" / "graph.json"
    if not graph_json.exists():
        raise GraphifyError(f"graphify produced no graph.json in {out_dir}")
    return graph_json


def _first(d: dict, keys: list[str], default=""):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


DOC_KINDS = {"doc", "doc_section", "document", "markdown", "concept"}


def _node_kind_and_name(rn: dict) -> tuple[str, str]:
    """Derive (kind, name) from graphify's file_type/_callable/label fields."""
    label = str(_first(rn, ["label", "name", "title"], _first(rn, ["id"], "")))
    file_type = str(_first(rn, ["file_type", "kind", "type"], "code")).lower()
    callable_ = bool(rn.get("_callable", False))

    if callable_:
        name = label[:-2] if label.endswith("()") else label
        return "function", name
    if file_type == "code":
        return "file", label
    if file_type in DOC_KINDS or file_type == "rationale":
        return ("comment" if file_type == "rationale" else file_type), label
    return file_type or "entity", label


def _parse_line(rn: dict) -> int:
    loc = _first(rn, ["source_location", "line_start", "lineStart", "line"], 0)
    if isinstance(loc, int):
        return loc
    s = str(loc)
    if s.startswith("L") and s[1:].isdigit():
        return int(s[1:])
    try:
        return int(s)
    except ValueError:
        return 0


def parse_graph_json(
    path: Path, repo: str, default_visibility: str
) -> tuple[list[Node], list[Edge]]:
    data = json.loads(path.read_text())
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("edges") or data.get("links") or []

    nodes: list[Node] = []
    id_map: dict[str, str] = {}  # graphify id -> stable tpk id
    for rn in raw_nodes:
        raw_id = str(_first(rn, ["id", "name"]))
        kind, name = _node_kind_and_name(rn)
        file_path = str(_first(rn, ["source_file", "file_path", "file", "path"]))

        if kind == "file":
            qualified = str(_first(rn, ["qualified_name", "qualifiedName", "fqn"], file_path or name))
        elif kind == "function":
            base = f"{file_path}::{name}" if file_path else name
            qualified = str(_first(rn, ["qualified_name", "qualifiedName", "fqn"], base))
        else:
            base = f"{file_path}::{raw_id}" if file_path else raw_id
            qualified = str(_first(rn, ["qualified_name", "qualifiedName", "fqn"], base))

        line = _parse_line(rn)
        stable = node_id(repo, kind, qualified)
        id_map[raw_id] = stable
        visibility = "public" if kind in DOC_KINDS else default_visibility
        nodes.append(
            Node(
                id=stable,
                repo=repo,
                kind=kind,
                name=name,
                qualified_name=qualified,
                file_path=file_path,
                line_start=line,
                line_end=int(_first(rn, ["line_end", "lineEnd"], line) or line),
                summary=str(_first(rn, ["summary", "description", "docstring"], "")),
                community=str(_first(rn, ["community", "cluster"], "")),
                visibility=visibility,
            )
        )

    edges: list[Edge] = []
    for re_ in raw_edges:
        src = id_map.get(str(_first(re_, ["source", "src", "from"])))
        dst = id_map.get(str(_first(re_, ["target", "dst", "to"])))
        if not src or not dst:
            continue  # endpoint outside this repo's parsed nodes
        confidence = str(_first(re_, ["confidence", "tag"], "EXTRACTED")).upper()
        if confidence not in ("EXTRACTED", "INFERRED"):
            confidence = "EXTRACTED"
        edges.append(
            Edge(
                src=src,
                dst=dst,
                rel=str(_first(re_, ["relation", "rel", "type", "label"], "related")).lower(),
                confidence=confidence,
                repo=repo,
            )
        )
    return nodes, edges
