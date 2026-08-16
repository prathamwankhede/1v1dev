"""AgentBackend interface — the contract every agent must satisfy.

Phase 1 ships only the ManualAgent (human pass-through).
Phase 3 will add ClaudeAgent, OpenAIAgent, etc.
"""

from abc import ABC, abstractmethod


class AgentBackend(ABC):
    """Base class for all agent backends.

    An agent receives the problem prompt and returns generated code.
    """

    # Adapters that can take a real multi-turn conversation (a system prompt
    # plus alternating user/assistant messages) set this True and implement
    # run_conversation(). Adapters that own their flat-prompt handling
    # themselves (the local CLI adapter sets its own --system-prompt) leave
    # this False and only need run().
    supports_conversation = False

    @abstractmethod
    async def run(self, prompt: str) -> dict:
        """Generate a solution for the given problem prompt.

        Args:
            prompt: The full problem description text.

        Returns:
            dict with keys:
                "code" (str): The generated source code.
                "log"  (str): The agent's full raw reply text.
                "hasCode" (bool): Whether a fenced code block was found —
                    False means "code" is the whole prose reply.
        """
        raise NotImplementedError

    async def run_conversation(self, system: str, messages: list) -> dict:
        """Multi-turn variant for adapters with supports_conversation = True.

        Args:
            system: System prompt, static for the whole race.
            messages: [{"role": "user"|"assistant", "content": str}, ...],
                ending with a user message carrying the player's current
                editor buffer plus their new instruction.

        Returns: the same dict shape as run().
        """
        raise NotImplementedError
