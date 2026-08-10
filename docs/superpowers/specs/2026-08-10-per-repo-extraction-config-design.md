# Per-Repo Extraction Config — Design

**Date:** 2026-08-10
**Status:** Approved (user approved design in session; descriptions added at user request)

## Purpose

Make each corpus repo's graphify extraction mode configurable so YAML- and
docs-heavy repos (helm-charts, timeplus-enterprise-deployment, docs,
timeplus-enterprise) can use graphify's semantic LLM extraction path, while
code repos stay on the free local AST path. Support both **Anthropic** and
**OpenAI** as LLM backends. Give every repo a human-readable description.

Background: graphify parses code via tree-sitter locally, but classifies
YAML/Markdown as document formats that require its semantic LLM extraction
(`--backend`, API key). Our `run_graphify` previously hardcoded `--code-only`,
so all-YAML repos produced zero nodes.

## repos.toml schema

```toml
[llm]
backend = "auto"   # "claude" | "openai" | "auto" (default). auto = graphify
                   # picks from whichever API key is exported.

[repos.helm-charts]
path = "/Users/gangtao/Code/timeplus/helm-charts"
visibility = "internal"
extraction = "semantic"      # "code-only" (default) | "semantic"
description = "Kubernetes Helm charts for deploying Timeplus Enterprise"
```

- `extraction` defaults to `"code-only"`; any other value than the two allowed
  raises at load time.
- `description` defaults to `""`.
- Semantic repos in v1 corpus: helm-charts, timeplus-enterprise-deployment,
  docs, timeplus-enterprise. All others code-only.
- API keys come only from env (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), never
  from the toml.

## Code changes

- `tpk/config.py`: `RepoConfig` gains `extraction: str = "code-only"` and
  `description: str = ""` (validated); new `LLMConfig` dataclass
  (`backend: str = "auto"`, validated) and `load_llm(toml_path) -> LLMConfig`
  reading the optional `[llm]` table. `load_repos` signature unchanged.
- `tpk/graphify_runner.py`: `run_graphify(repo_path, out_dir,
  extraction="code-only", backend=None)`. Code-only mode passes `--code-only`
  as before. Semantic mode omits it and appends `--backend claude|openai` when
  backend is not auto. Fail fast with `GraphifyError` when semantic mode has no
  usable key: claude → `ANTHROPIC_API_KEY` required; openai → `OPENAI_API_KEY`;
  auto → either.
- `tpk/ingest.py`: `ingest_repo` gains `backend: str | None = None` and passes
  `extraction=repo_cfg.extraction, backend=backend` to `run_graphify`.
  Per-repo failure isolation already covers LLM-path failures.
- `tpk/cli.py`: `ingest` loads `LLMConfig` and passes the backend
  (`"auto"` → `None`).
- `repos.toml`: descriptions and extraction modes for all 9 repos; `[llm]`
  section with `backend = "auto"`.
- `README.md`: document the new fields and the required env keys.

## Error handling

- Unknown `extraction`/`backend` values raise `ValueError` at config load.
- Semantic extraction without a usable key raises `GraphifyError` before
  invoking graphify (clear message naming the missing env var); ingest records
  the repo as `failed` and other repos proceed, as today.

## Testing

- Config: extraction/description parsing, defaults, validation errors, `[llm]`
  present/absent.
- Runner: argv assembly for code-only (has `--code-only`), semantic (no
  `--code-only`), semantic+claude / semantic+openai (`--backend` flag), via
  subprocess monkeypatch; fail-fast tests with env keys cleared.
- Ingest: `ingest_repo` passes mode/backend through (monkeypatch
  `run_graphify`, assert call args).
- Live semantic ingest of the 4 repos runs once a key is exported (manual,
  post-merge).

## M4 requirement (recorded, not built here)

The chat agent (M4) must support both Anthropic and OpenAI models via
configuration — carried into the M4 plan.
