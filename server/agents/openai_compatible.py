"""OpenAICompatibleAgent — calls any OpenAI-style /chat/completions endpoint.

This is the BYOM adapter: OpenAI, OpenRouter, a local Ollama/vLLM instance,
an OpenCode-style gateway, etc. all speak this same request/response shape.

The base URL is player-supplied, so it's validated against a fixed host
allowlist before any request is made — the server, not the browser, makes
this outbound call, so an unrestricted base URL would be an SSRF vector.
"""

import ipaddress
from urllib.parse import urlparse

import aiohttp

from server.agents.interface import AgentBackend
from server.agents.parsing import extract_code_block

# Public providers explicitly allowed, plus localhost/private-IP for
# self-hosted backends (Ollama, vLLM, OpenCode gateway, ...) run alongside
# the server. Extending this list is a one-line change.
ALLOWED_HOSTS = {
    "api.openai.com",
    "openrouter.ai",
}

MAX_TOKENS = 4096


def _is_allowed_host(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in ALLOWED_HOSTS or hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


class OpenAICompatibleAgent(AgentBackend):
    """Adapter for any OpenAI-compatible chat-completions endpoint.

    No server-side API key fallback: config["api_key"] must be supplied by
    the player, except for no-auth local endpoints where it may be omitted.
    """

    def __init__(self, config):
        base_url = (config.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("OpenAI-compatible agent requires a base URL")

        hostname = urlparse(base_url).hostname
        if not _is_allowed_host(hostname):
            raise ValueError(
                f"Host '{hostname}' is not on the allowed list for custom agent endpoints"
            )

        model = config.get("model")
        if not model:
            raise ValueError("OpenAI-compatible agent requires a model name")

        self.base_url = base_url
        self.model = model
        self.api_key = config.get("api_key")
        self.language = config.get("language", "python")

    async def run(self, prompt: str) -> dict:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=55),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    message = data.get("error", {}).get("message", str(data))
                    raise RuntimeError(f"Agent endpoint error ({resp.status}): {message}")

        choices = data.get("choices", [])
        text = choices[0]["message"]["content"] if choices else ""

        return {
            "code": extract_code_block(text, self.language),
            "log": text,
        }
