"""Agent registry — maps a client-supplied agent type to its adapter."""

from server.agents.anthropic import AnthropicAgent
from server.agents.openai_compatible import OpenAICompatibleAgent

AGENT_TYPES = {
    "anthropic": AnthropicAgent,
    "openai-compatible": OpenAICompatibleAgent,
}


def build_agent(agent_type, config):
    """Instantiate the adapter for agent_type with the given config dict.

    Raises ValueError if agent_type is unknown or config is invalid
    (missing key, disallowed host, etc. — validated in each adapter's
    __init__).
    """
    agent_cls = AGENT_TYPES.get(agent_type)
    if agent_cls is None:
        raise ValueError(f"Unknown agent type: '{agent_type}'")
    return agent_cls(config)
