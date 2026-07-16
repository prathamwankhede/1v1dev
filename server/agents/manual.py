"""ManualAgent — pass-through agent for human players.

The human types code directly in the editor and submits it.
This agent exists to define the contract; the submit flow
bypasses it entirely in Phase 1.
"""

from server.agents.interface import AgentBackend


class ManualAgent(AgentBackend):
    """Pass-through agent: returns whatever code the human typed."""

    async def run(self, prompt: str) -> dict:
        # In practice this is never called for human players.
        # The client submit flow sends code directly to the room.
        return {"code": "", "log": "ManualAgent: human is typing."}
