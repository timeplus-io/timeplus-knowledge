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
    return ChatOpenAI(
        model=cfg.model,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        api_key=os.environ.get("OPENAI_API_KEY") or _PLACEHOLDER_KEY,
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
   to quote real code or docs.
2. Every factual claim MUST carry a citation in the form
   repo/file_path:line (use the entity's repo, file_path, line_start).
3. If the tools return nothing relevant, say "I could not find this in the knowledge graph" —
   never invent an answer.
4. Prefer doc/concept entities for conceptual questions and code entities
   for implementation questions. Keep answers concise and structured."""


def build_agent(kg, cfg, repos: dict[str, "RepoConfig"], model=None):
    return create_react_agent(
        model if model is not None else build_chat_model(cfg),
        build_agent_tools(kg),
        prompt=system_prompt(repos),
    )
