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
- There is no "kind"/"type" field on nodes. tpk derives one (CODE_KINDS) from
  the node's flags AND its edges -- flags alone misfile a third of a C++
  codebase (#17, measured on proton-enterprise: 102k "file" nodes, 13k real):
    * `class`    -- `_callable_class`; or a node known only as one end of an
                    `inherits` edge.
    * `function` -- `_callable` ("name()" free function, ".name()" method
                    written inside its class); OR a non-callable node a class
                    `defines` whose body is in the graph (a source file
                    `contains` it, or it makes calls). That second shape is an
                    OUT-OF-CLASS definition (`BlockIO Foo::execute() {...}`, the
                    dominant C++ form): graphify merges it into the header
                    declaration, leaving a bare `execute` label and no
                    `_callable` flag -- but its `calls` edges are intact.
    * `member`   -- a class `defines` it and no body is known: a field, or a
                    method that is only declared (pure virtual / defined in a
                    file outside the extraction).
    * `file`     -- the one node whose label is its source file's basename.
    * `symbol`   -- anything else: bare type / alias / variable references
                    (`String`, `ContextPtr`, ...). Often very high degree.
  Methods and members are named `Class::name` from the owning class's label.
- There is no "name" field; the human name is derived from "label"
  (stripping a trailing "()" for callables).
- There is no "qualified_name"/"fqn" field; one is synthesized so it is
  stable *and unique* across the graph: `source_file::name` for `function`
  nodes (falling back to `...#<graphify id>` when two share it, e.g.
  overloads); `source_file::<graphify's own node id>` for everything else.
  The node's own "id" is used -- rather than, say, its label or line number -- because it is the one field
  graphify guarantees is unique per node (it is the dict key in the
  node-link graph); label and line number are not (two nodes can share a
  source_file, label and line -- see the kind note above).
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
from collections import deque
from collections.abc import Callable
from pathlib import Path

from tpk.model import Edge, Node, node_id


class GraphifyError(RuntimeError):
    pass


# A backend is usable with either an API key or a custom endpoint URL
# (self-hosted server or gateway, e.g. AWS Bedrock behind LiteLLM /
# Bedrock Access Gateway, which may not need a real key). graphify itself
# honors the *_BASE_URL / *_MODEL env vars.
_BACKEND_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
}


def _require_llm_key(backend: str | None) -> None:
    if backend in _BACKEND_ENV:
        if not any(os.environ.get(v) for v in _BACKEND_ENV[backend]):
            key, url = _BACKEND_ENV[backend]
            raise GraphifyError(
                f"semantic extraction with backend {backend!r} requires {key} "
                f"(or {url} for a self-hosted/gateway endpoint) to be set"
            )
    elif not any(
        os.environ.get(v) for pair in _BACKEND_ENV.values() for v in pair
    ):
        raise GraphifyError(
            "semantic extraction requires ANTHROPIC_API_KEY or OPENAI_API_KEY "
            "(or ANTHROPIC_BASE_URL/OPENAI_BASE_URL for a gateway endpoint) to be set"
        )


def _infer_backend_from_env() -> str | None:
    """Resolve an explicit backend when auto mode can't rely on graphify.

    graphify's own auto-detection keys off API keys only. If no key is set
    but a *_BASE_URL is (gateway/self-hosted, e.g. Bedrock), we must pass
    --backend explicitly — inferred from which base URL is present.
    """
    if any(os.environ.get(pair[0]) for pair in _BACKEND_ENV.values()):
        return None  # a real key exists; graphify's own auto-detect works
    with_url = [b for b, (_, url) in _BACKEND_ENV.items() if os.environ.get(url)]
    if len(with_url) > 1:
        raise GraphifyError(
            "both ANTHROPIC_BASE_URL and OPENAI_BASE_URL are set with no API "
            "keys; set [llm].backend in repos.toml to disambiguate"
        )
    return with_url[0] if with_url else None


def _child_env_with_placeholder_key() -> dict[str, str]:
    """graphify requires the backend's *_API_KEY env even when *_BASE_URL
    points at a gateway that does its own auth. When a base URL is set with
    no key, hand the child a placeholder so the gateway path works; the key
    is never sent anywhere except that gateway."""
    env = os.environ.copy()
    for key_var, url_var in _BACKEND_ENV.values():
        if env.get(url_var) and not env.get(key_var):
            print(f"[tpk] {url_var} set without {key_var}; passing placeholder key to graphify")
            env[key_var] = "placeholder-gateway-key"
    return env


def run_graphify(
    repo_path: Path,
    out_dir: Path,
    extraction: str = "code-only",
    backend: str | None = None,
    model: str | None = None,
    token_budget: int = 0,
    stream: bool = False,
    on_line: Callable[[str], None] | None = None,
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
        if backend in (None, "auto"):
            backend = _infer_backend_from_env()
        if backend and backend != "auto":
            cmd += ["--backend", backend]
        if model:
            cmd += ["--model", model]
        if token_budget:
            cmd += ["--token-budget", str(token_budget)]
    cmd += ["--out", str(out_dir)]
    child_env = _child_env_with_placeholder_key()
    # Force graphify (a Python CLI) to line-flush stdout so the tee below is a
    # live heartbeat over a pipe, not a single end-of-run batch.
    child_env["PYTHONUNBUFFERED"] = "1"
    recent: deque[str] = deque(maxlen=50)
    try:
        # Merge stderr into stdout so graphify's progress (on either stream) is
        # captured in order. Read line-by-line: each line is forwarded to
        # `on_line` (the extract-phase heartbeat) and, when stream=True, echoed
        # to the terminal (preserving `tpk ingest --verbose`).
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_env,
        )
    except FileNotFoundError as exc:
        raise GraphifyError(
            "graphify executable not found on PATH; ensure the `graphifyy` package "
            "(providing the `graphify` CLI) is installed in this environment"
        ) from exc
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        recent.append(line)
        if on_line is not None:
            on_line(line)
        if stream:
            print(line)
    proc.wait()
    if proc.returncode != 0:
        detail = ("\n".join(recent))[-2000:] or "(no output captured)"
        raise GraphifyError(f"graphify failed on {repo_path}: {detail}")
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


# Kinds for source-derived ("code") nodes. graphify itself has no such field:
# they are derived from the node's flags AND its edges (see _classify_code_node).
CODE_KINDS = ("file", "class", "function", "member", "symbol")


def _label(rn: dict) -> str:
    return str(_first(rn, ["label", "name", "title"], _first(rn, ["id"], "")))


class _Structure:
    """What the EDGES say about each raw node -- needed because graphify's node
    flags alone misfile a third of a C++ codebase (#17): an out-of-class method
    definition (`BlockIO Foo::execute() {...}`, the dominant C++ form) comes out
    NON-callable with a bare label, merged into its header declaration."""

    def __init__(self, raw_nodes: list[dict], raw_edges: list[dict]):
        self.owner: dict[str, str] = {}      # member id -> id of the class that defines it
        self.contained: set[str] = set()     # a source file `contains` it (it has a body there)
        self.calls_out: set[str] = set()
        self.in_hierarchy: set[str] = set()  # either end of an `inherits` edge
        self.label = {str(_first(rn, ["id", "name"])): _label(rn) for rn in raw_nodes}
        for re_ in raw_edges:
            src = str(_first(re_, ["source", "src", "from"]))
            dst = str(_first(re_, ["target", "dst", "to"]))
            rel = str(_first(re_, ["relation", "rel", "type", "label"], "")).lower()
            if rel in ("defines", "method"):
                self.owner.setdefault(dst, src)
            elif rel == "contains":
                self.contained.add(dst)
            elif rel in ("calls", "indirect_call"):
                self.calls_out.add(src)
            elif rel in ("inherits", "extends"):
                self.in_hierarchy.update((src, dst))

    def qualify(self, raw_id: str, name: str) -> str:
        """`execute` -> `InterpreterInsertQuery::execute` when a class owns it."""
        owner = self.label.get(self.owner.get(raw_id, ""), "")
        if owner and "::" not in name:
            return f"{owner}::{name}"
        return name


def _node_kind_and_name(rn: dict, structure: "_Structure | None" = None) -> tuple[str, str]:
    """Derive (kind, name) from graphify's flags plus the graph structure."""
    label = _label(rn)
    file_type = str(_first(rn, ["file_type", "kind", "type"], "code")).lower()
    if file_type != "code" and not rn.get("_callable"):
        # document/paper/image/rationale/concept (or any other non-"code" value):
        # pass the documented file_type through as the node kind unchanged.
        return file_type or "entity", label
    return _classify_code_node(rn, label, structure or _Structure([], []))


def _classify_code_node(rn: dict, label: str, st: "_Structure") -> tuple[str, str]:
    raw_id = str(_first(rn, ["id", "name"]))
    if rn.get("_callable_class"):
        return "class", label
    if rn.get("_callable"):
        # "name()" free function, or ".name()" for a method written inside its class.
        name = label[:-2] if label.endswith("()") else label
        return "function", st.qualify(raw_id, name.lstrip("."))
    source_file = str(_first(rn, ["source_file", "file_path", "file", "path"], ""))
    if source_file and label == source_file.rsplit("/", 1)[-1]:
        return "file", label                       # the node that IS the file
    if raw_id in st.owner:
        # A class member. It is a method when its body is in the graph (a .cpp
        # `contains` it, or it makes calls); otherwise a field -- or a method
        # that is only declared here (pure virtual / defined elsewhere).
        has_body = raw_id in st.contained or raw_id in st.calls_out
        return ("function" if has_body else "member"), st.qualify(raw_id, label)
    if raw_id in st.in_hierarchy:
        return "class", label                      # known only as a base/derived class
    if raw_id in st.calls_out:
        return "function", label
    return "symbol", label                         # bare type / alias / variable reference


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

    structure = _Structure(raw_nodes, raw_edges)
    seen_function_names: set[str] = set()
    nodes: list[Node] = []
    id_map: dict[str, str] = {}  # graphify id -> stable tpk id
    for rn in raw_nodes:
        raw_id = str(_first(rn, ["id", "name"]))
        kind, name = _node_kind_and_name(rn, structure)
        file_path = str(_first(rn, ["source_file", "file_path", "file", "path"]))

        if kind == "function":
            # `file::Class::method` -- stable across runs and human-meaningful.
            # Collisions (overloads, or a graphify node split across decl/def)
            # fall back to the raw graphify id, which is unique per graph.json.
            base = f"{file_path}::{name}" if file_path else name
            if base in seen_function_names:
                base = f"{base}#{raw_id}"
            seen_function_names.add(base)
        else:
            # Everything else is keyed by graphify's own node id: many such
            # nodes share a file AND a label (fields, symbols, doc fragments),
            # and a bare `file::label` collapsed them onto one row (verified on
            # real timeplus-cli output: 473 parsed nodes -> 99 stored rows).
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
