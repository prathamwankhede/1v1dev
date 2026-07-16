"""AgentBackend interface — the contract every agent must satisfy.

Phase 1 ships only the ManualAgent (human pass-through).
Phase 3 will add ClaudeAgent, OpenAIAgent, etc.
"""

from abc import ABC, abstractmethod


class AgentBackend(ABC):
    """Base class for all agent backends.

    An agent receives the problem prompt and returns generated code.
    """

    @abstractmethod
    async def run(self, prompt: str) -> dict:
        """Generate a solution for the given problem prompt.

        Args:
            prompt: The full problem description text.

        Returns:
            dict with keys:
                "code" (str): The generated source code.
                "log"  (str): Any reasoning / debug log from the agent.
        """
        raise NotImplementedError
