"""Chat agent over the knowledge graph: model factory (this task), tools +
graph assembly (later tasks)."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from tpk.config import AgentConfig

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
