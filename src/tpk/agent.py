"""Chat agent over the knowledge graph: model factory (Task 1), tools +
graph assembly (this task)."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from tpk.agent_tools import build_agent_tools
from tpk.config import AgentConfig, RepoConfig

_PLACEHOLDER_KEY = "placeholder-gateway-key"


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
    # like qwen reject or ignore it).
    effort = os.environ.get("TPK_AGENT_REASONING_EFFORT")
    if effort:
        kwargs["reasoning_effort"] = effort
    return ChatOpenAI(
        model=cfg.model,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        api_key=os.environ.get("OPENAI_API_KEY") or _PLACEHOLDER_KEY,
        **kwargs,
    )


RECURSION_LIMIT = 40


def system_prompt(repos: dict[str, "RepoConfig"]) -> str:
    corpus = "\n".join(
        f"- {r.name} ({r.visibility}): {r.description or 'no description'}"
        for r in repos.values()
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
2. search_entities' `kinds` filter only accepts these exact values: file,
   function, document, concept, rationale. There is no "doc", "code", or
   "repo" kind — omit `kinds` if unsure rather than guessing a value, and
   use `repos` (repo names from the corpus list above) to narrow scope.
   search_entities requires EVERY word in `query` to match — multi-word
   queries fail fast if you guess the wrong phrasing. Prefer short, one-
   or two-word queries (a single distinctive term is often best) and try
   several different single terms before concluding nothing exists.
3. Every factual claim MUST carry a citation in the form
   repo/file_path:line (use the entity's repo, file_path, line_start) —
   always a SPECIFIC line number, e.g.
   `proton/src/Storages/MatView/StorageMaterializedView.h:25`, never a
   bare filename, a line range description like "all sections", or a
   vague "see the documentation". Never state a command, URL, version
   number, or default value unless it came from a tool result — if you
   cannot ground a detail, omit it rather than filling the gap with
   plausible-sounding or "typical" content. End every answer that used
   tool results with a "Citations:" section listing every
   repo/file_path:line you relied on, one per line.
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
   concise and structured.
6. Your last message MUST be a normal assistant reply containing the
   answer text itself — never end the conversation on a tool call or with
   an empty message, and never leave the answer only in your private
   reasoning."""


def build_agent(kg, cfg, repos: dict[str, "RepoConfig"], model=None):
    return create_react_agent(
        model if model is not None else build_chat_model(cfg),
        build_agent_tools(kg),
        prompt=system_prompt(repos),
    )
