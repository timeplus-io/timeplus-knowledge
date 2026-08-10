# Per-Repo Extraction Config — Implementation Plan

> Executed inline (small change, user pre-approved). Spec:
> docs/superpowers/specs/2026-08-10-per-repo-extraction-config-design.md

**Goal:** repos.toml gains per-repo `extraction` + `description` and a global
`[llm] backend`; `run_graphify` supports semantic mode with claude/openai
backends and fails fast without keys.

## Tasks

1. **Config** — tests for `extraction`/`description`/`[llm]` parsing +
   validation → implement `RepoConfig` fields, `LLMConfig`, `load_llm`.
2. **Runner** — tests for argv assembly (code-only ↔ semantic ↔ backends) and
   key fail-fast → implement `run_graphify(..., extraction, backend)`.
3. **Ingest/CLI passthrough** — test `ingest_repo` forwards mode/backend →
   implement; CLI loads `LLMConfig`.
4. **Corpus config + docs** — repos.toml (9 descriptions, 4 semantic repos,
   `[llm]`), README section.
5. **Verify** — full suite with TIMEPLUS_HOST=localhost; code-only repos
   unaffected; semantic repos fail fast with clear message until a key is
   exported.
