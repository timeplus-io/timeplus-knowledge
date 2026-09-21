"""Chat agent over the knowledge graph: model factory (Task 1), tools +
graph assembly (this task)."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from tpk.agent_tools import build_agent_tools
from tpk.config import AgentConfig, RepoConfig, entry_key

_PLACEHOLDER_KEY = "placeholder-gateway-key"


class _ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that preserves the non-standard `reasoning` /
    `reasoning_content` streaming delta field.

    Base ChatOpenAI targets the official OpenAI spec and deliberately drops
    that field, but OpenAI-compatible reasoning models (gpt-oss, DeepSeek,
    Qwen, Kimi, ...) stream their chain-of-thought there. We re-attach it to
    the chunk's `additional_kwargs["reasoning"]` so the server can surface it
    as a thinking event (see server._chunk_thinking). Verified against the
    Bedrock gateway: gpt-oss-120b streams reasoning as `delta.reasoning`.
    """

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        gen = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info)
        if gen is not None:
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                if reasoning:
                    gen.message.additional_kwargs["reasoning"] = reasoning
        return gen


def build_chat_model(cfg: AgentConfig):
    if cfg.provider == "anthropic":
        return ChatAnthropic(
            model=cfg.model,
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            api_key=os.environ.get("ANTHROPIC_API_KEY") or _PLACEHOLDER_KEY,
        )
    kwargs = {}
    # For reasoning models (gpt-oss et al): "low" keeps output on the content
    # channel instead of flooding the reasoning channel and ending with an
    # empty final message. Unset -> parameter not sent (non-reasoning models
    # like qwen reject or ignore it). NOTE: with reasoning surfaced to the
    # thinking panel, a non-"low" effort now feeds that panel — the empty-final
    # -message risk is separately handled by the on_chat_model_end fallback.
    effort = AgentConfig.reasoning_effort()
    if effort:
        kwargs["reasoning_effort"] = effort
    return _ReasoningChatOpenAI(
        model=cfg.model,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        api_key=os.environ.get("OPENAI_API_KEY") or _PLACEHOLDER_KEY,
        **kwargs,
    )


RECURSION_LIMIT = 40


def system_prompt(repos) -> str:
    """`repos` may be a dict[str, RepoConfig] (legacy) or an iterable of
    RepoConfig (e.g. live corpus entries from corpus.list_entries)."""
    entries = list(repos.values()) if isinstance(repos, dict) else list(repos)
    corpus = "\n".join(
        f"- {entry_key(r)} ({r.visibility}): {r.description or 'no description'}"
        for r in entries
    )
    return f"""You are the Timeplus knowledge agent. You answer questions about
Timeplus — its code, design, architecture, and devops — using ONLY the
knowledge graph tools available to you.

The knowledge graph covers these repositories:
{corpus}

Rules:
1. Ground every answer in tool results. Start with search_entities, then
   use neighbors / path_between / get_entity to explore, and read_source
   to quote real code or docs. Prefer one tool call at a time, reading
   each result before deciding the next step — this keeps your reasoning
   clearer even though the backend now supports concurrent tool calls.
   LEAN ON THE GRAPH, not just keyword search. Once you have a central
   entity's id, a neighbors(id) call is often the fastest way to see how
   it fits together — its callers, callees, containing file/module, and
   related nodes — and surfaces structure that repeated search_entities
   never will. Reach for neighbors/path_between whenever understanding how
   pieces connect would improve the answer (mapping the components of a
   subsystem, finding the key collaborators of a class, confirming what a
   function actually depends on), not only when the user literally asks a
   relationship question. RELATIONSHIP and CALL-PATH questions — "what
   calls X", "what does X call", "who uses X", "how does X reach Y",
   "trace the call path from A to B" — REQUIRE it: search_entities ONCE
   for the endpoint id(s), then neighbors(id) for one-hop callers/callees
   or path_between(id_a, id_b) to connect two endpoints, instead of more
   keyword searches. path_between tells you what it found: mode "calls"
   is a real directed call chain (check `direction`); mode "related" is
   only an association and must NOT be described as a call path. The
   extracted call graph is incomplete (AST-only extraction misses C++
   virtual dispatch, templates, callbacks, and member calls through
   pointer aliases), so a chain usually BREAKS where the code calls
   through an interface. When found is false, do not stop: take
   `callees_of_a` / `callers_of_b`, walk neighbors(id, rels=["calls"],
   direction="out") a hop at a time, and read_source the function body at
   the break to see what it really calls (e.g. `storage->write(...)`),
   then search for the implementations and continue from there. Report
   the chain you reconstructed and mark which hops came from the graph
   and which from reading the code, rather than answering "not found".
2. search_entities' `kinds` filter only accepts these exact values: function
   (functions AND methods, named `Class::method`), class, member (a class
   field or a method that is only declared), symbol (a bare type / alias
   reference -- rarely what you want), file, document, concept, rationale.
   There is no "doc", "code", "method", or "repo" kind — omit `kinds` if unsure rather than guessing a value, and
   use `repos` (repo names from the corpus list above) to narrow scope.
   search_entities requires EVERY word in `query` to match — multi-word
   queries fail fast if you guess the wrong phrasing. Prefer short, one-
   or two-word queries (a single distinctive term is often best) and try
   several different single terms before concluding nothing exists.
3. Ground and cite in prose. When a claim rests on a specific file or
   doc, reference it inline as repo/file_path:line (the entity's repo,
   file_path, and a SPECIFIC line — never a bare filename or a vague "see
   the documentation"), and read_source the key files so they appear in
   the UI's Sources panel. Do NOT number citations as [1], [2] and do NOT
   append a separate "Citations"/"Sources" list — the Sources panel is the
   source list, and hand-numbered citations only drift from it. Never
   state a command, URL, version number, or default value unless it came
   from a tool result — if you cannot ground a detail, omit it rather than
   filling the gap with plausible-sounding or "typical" content.
4. Be efficient: 2-4 well-varied search_entities queries (different
   keywords, not repeats of the same query) are usually enough to know
   whether the corpus has an answer. If several distinct queries and a
   look at list_communities/neighbors turn up nothing relevant, stop and
   say "I could not find this in the knowledge graph" rather than
   continuing to search — never invent an answer. You have a hard budget
   of about 12 tool calls per question — track your count, and once
   you're close to that budget, STOP exploring and write your best
   answer from what you've already gathered (with citations for whatever
   you did confirm), even if some sub-detail stays unconfirmed. A partial,
   cited answer is always better than running out of turns with no answer
   at all.
5. Prefer document/concept entities for conceptual questions and
   file/function entities for implementation questions. Keep answers
   concise and structured. Formatting: standard markdown only — never
   raw HTML except <br> for a line break inside a table cell; put code
   in fenced blocks (```sql ... ```) below the table, never squeezed
   into table cells.
6. Your last message MUST be a normal assistant reply containing the
   answer text itself — never end the conversation on a tool call or with
   an empty message, and never leave the answer only in your private
   reasoning.
7. TRACE RECIPE — for "trace the call path", "how does X reach Y", "what
   happens when ..." questions. The graph's call edges are real but the
   chain BREAKS wherever the code calls through an interface or a pointer
   (`storage->write(...)`, `interpreter->execute()`): those calls are not
   in the graph. Do not give up at a break — get past it:
   a. Find both endpoints with search_entities (kinds=["function"];
      methods are named `Class::method`, so search "InterpreterInsertQuery
      execute", not just "execute").
   b. path_between(id_a, id_b). mode "calls" → you have the chain, go to e.
   c. Otherwise walk it yourself, one hop at a time: neighbors(id,
      rels=["calls"], direction="out") from the start (and
      direction="in" from the end), following the callee that leads
      toward the other endpoint.
   d. At a dead end, read_source the function you are stuck in and look at
      what it actually calls. For a call through an interface such as
      `x->write(...)`, find the implementations by method name —
      search_entities("::write", kinds=["function"]) — pick the one that
      fits the context (the class named in the code, or the subsystem you
      are heading for), and continue from it with step c.
   e. Answer with the chain in order, marking every hop [graph] (an edge
      you saw) or [code] (you read the call in the source), and say where
      the chain is still unconfirmed.
   A trace may use up to 18 tool calls instead of the usual 12.
8. PROTECT SOURCE CODE. Read and search the code freely to ground your
   answer, and quote only the SHORT snippets needed to explain a point —
   but never reproduce complete or near-complete files, and never
   reconstruct a whole file across several quotes. If the user asks you to
   print, dump, export, or output the full contents of a file, decline and
   offer to explain what it does or show the specific lines relevant to
   their question instead."""


def build_agent(
    kg,
    cfg,
    repos: "dict[str, RepoConfig] | list[RepoConfig]",
    model=None,
    corpus_provider=None,
):
    chat_model = model if model is not None else build_chat_model(cfg)
    tools = build_agent_tools(kg)
    if corpus_provider is None:
        return create_react_agent(chat_model, tools, prompt=system_prompt(repos))

    # `repos` seeds the fallback corpus: if `corpus_provider()` raises (e.g.
    # a transient DB failure), the turn must still get a usable prompt
    # instead of the chat dying with an error event. Mirrors the
    # degrade-gracefully pattern in tools.py's KnowledgeGraph._corpus_state.
    last_good_repos = repos

    def _live_prompt(state):
        nonlocal last_good_repos
        try:
            last_good_repos = corpus_provider()
        except Exception:
            pass  # keep serving the last successful (or seed) corpus
        return [
            {"role": "system", "content": system_prompt(last_good_repos)}
        ] + list(state["messages"])

    return create_react_agent(chat_model, tools, prompt=_live_prompt)
