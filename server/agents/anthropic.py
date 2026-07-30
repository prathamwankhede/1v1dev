"""AnthropicAgent — calls the native Anthropic Messages API.

For players who want Claude specifically, as opposed to the generic
OpenAI-compatible adapter used for everything else.
"""

import aiohttp

from server.agents.interface import AgentBackend
from server.agents.parsing import extract_code_block

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096


class AnthropicAgent(AgentBackend):
    """Adapter for Anthropic's native Messages API.

    No server-side API key fallback: config["api_key"] is required.
    """

    def __init__(self, config):
        api_key = config.get("api_key")
        if not api_key:
            raise ValueError("Anthropic agent requires an API key")
        self.api_key = api_key
        self.model = config.get("model") or DEFAULT_MODEL
        self.language = config.get("language", "python")

    async def run(self, prompt: str) -> dict:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=55),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    message = data.get("error", {}).get("message", str(data))
                    raise RuntimeError(f"Anthropic API error ({resp.status}): {message}")

        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        return {
            "code": extract_code_block(text, self.language),
            "log": text,
        }
