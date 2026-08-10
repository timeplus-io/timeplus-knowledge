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
  "file_type" plus the "_callable" flag: a callable code node is a
  `function`, a non-callable "code" node is mapped to `file`, and any other
  file_type passes through as-is (see the documented vocabulary below).
  NOTE: despite the name, `file` is *not* one node per source file in real
  graphify output -- it also catches top-level structs/types/vars and
  functions the `_callable` heuristic misses (observed on timeplus-cli:
  e.g. a shell function labeled "handle_signal()" with `_callable` unset).
  Do not assume `file`-kind qualified_name/id is a stable per-file handle.
- There is no "name" field; the human name is derived from "label"
  (stripping a trailing "()" for callables).
- There is no "qualified_name"/"fqn" field; one is synthesized so it is
  stable *and unique* across the graph: `source_file::name` for callables
  (`function` kind); `source_file::<graphify's own node id>` for everything
  else (`file` kind and all DOC_KINDS). The node's own "id" is used --
  rather than, say, its label or line number -- because it is the one field
  graphify guarantees is unique per node (it is the dict key in the
  node-link graph); label and line number are not (a "file" node can share
  a source_file and line with an unrelated node -- see the `file` kind note
  below).
- File location is "source_file" (not "file"/"file_path"), and there is a
  single "source_location" like "L6" rather than separate line_start/
  line_end fields.
- Edge relation is "relation" (not "type"/"rel"), and endpoints are
  "source"/"target" (not "src"/"dst").

Documented file_type / confidence vocabulary (from graphify's own extraction
subagent prompt, shipped in the installed package at
`graphify/skills/claude/references/extraction-spec.md`, loaded whenever a
corpus has doc/paper/image content):

- `file_type` MUST be exactly one of: `code`, `document`, `paper`, `image`,
  `rationale`, `concept`. Only `code` is source-derived; the other five are
  always non-code entities (extracted from docs/papers/images, or
  concept-like nodes such as "ideas, principles, mechanisms, design
  patterns") and are treated as doc-kind (`DOC_KINDS`) regardless of the
  repo's default visibility. `--code-only` runs only ever emit `code` (for
  files/functions) in practice, but the parser honors the full six-value
  enum so a future non-`--code-only` run parses correctly too.
- `confidence` is one of `EXTRACTED` (explicit in source: import, call,
  citation), `INFERRED` (reasonable inference), or `AMBIGUOUS` (uncertain,
  flagged for review — the spec says "flag for review, do not omit"). Our
  Edge model only has two confidence buckets (EXTRACTED, INFERRED), so
  `AMBIGUOUS` (and any other unrecognized value) maps to `INFERRED`, never
  `EXTRACTED` — mislabeling an uncertain edge as EXTRACTED (high confidence)
  is the dangerous direction; collapsing it into INFERRED is conservative.

The `_first()` helper and its fallback key lists are kept so the parser
degrades gracefully if a future graphify version (or a different extractor
mode) renames or adds fields, but the *primary* keys above reflect what
graphify 0.9.38 actually emits and documents, not a guess.
"""

import json
import os
import subprocess
from pathlib import Path

from tpk.model import Edge, Node, node_id


class GraphifyError(RuntimeError):
    pass


_BACKEND_KEYS = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _require_llm_key(backend: str | None) -> None:
    if backend in _BACKEND_KEYS:
        var = _BACKEND_KEYS[backend]
        if not os.environ.get(var):
            raise GraphifyError(
                f"semantic extraction with backend {backend!r} requires {var} to be set"
            )
    elif not any(os.environ.get(v) for v in _BACKEND_KEYS.values()):
        raise GraphifyError(
            "semantic extraction requires ANTHROPIC_API_KEY or OPENAI_API_KEY to be set"
        )


def run_graphify(
    repo_path: Path,
    out_dir: Path,
    extraction: str = "code-only",
    backend: str | None = None,
) -> Path:
    """Run `graphify extract` on repo_path and return the path to graph.json.

    extraction="code-only" (default): local tree-sitter AST only, no API key
    needed, non-code files (YAML/Markdown/etc.) are skipped.
    extraction="semantic": graphify's LLM extraction also processes doc/config
    files; requires ANTHROPIC_API_KEY (backend "claude") or OPENAI_API_KEY
    (backend "openai"). backend None/"auto" lets graphify pick from whichever
    key is exported; we fail fast here if none is.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["graphify", "extract", str(repo_path)]
    if extraction == "code-only":
        cmd.append("--code-only")
    else:
        _require_llm_key(backend)
        if backend and backend != "auto":
            cmd += ["--backend", backend]
    cmd += ["--out", str(out_dir)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GraphifyError(
            "graphify executable not found on PATH; ensure the `graphifyy` package "
            "(providing the `graphify` CLI) is installed in this environment"
        ) from exc
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


# graphify's documented file_type enum (extraction-spec.md) is exactly
# {code, document, paper, image, rationale, concept}. `code` is the only
# source-derived value; the rest are always non-code / doc-ish entities.
DOC_KINDS = {"document", "paper", "image", "rationale", "concept"}


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
    # document/paper/image/rationale/concept (or any other non-"code" value):
    # pass the documented file_type through as the node kind unchanged.
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
            # "file" is not actually one node per file: graphify emits many
            # non-callable "code" nodes per file (structs, top-level vars,
            # shell functions missed by the `_callable` heuristic, ...), all
            # mapped to kind="file" by _node_kind_and_name above. A bare
            # `file_path` qualified_name collapsed all of them onto one node
            # id (mutable-stream upsert silently dropped the rest -- verified
            # against real timeplus-cli output: 473 parsed nodes down to 99
            # stored rows). Disambiguate with the node's own graphify id,
            # which is guaranteed unique within one graph.json (it is the
            # node-link graph's own dict key) -- same pattern as the
            # DOC_KINDS branch below, which never collided.
            base = f"{file_path}::{raw_id}" if file_path else raw_id
            qualified = str(_first(rn, ["qualified_name", "qualifiedName", "fqn"], base))
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
        if confidence != "EXTRACTED":
            # graphify's documented confidence enum is EXTRACTED, INFERRED,
            # AMBIGUOUS. Our model has only two buckets: keep EXTRACTED as-is,
            # collapse everything else (INFERRED, AMBIGUOUS, or an unknown
            # future value) into INFERRED. Mislabeling an uncertain/AMBIGUOUS
            # edge as high-confidence EXTRACTED would be the dangerous
            # direction, so unknown values fall to the conservative bucket.
            confidence = "INFERRED"
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
