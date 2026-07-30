"""Shared response-parsing helpers for agent backends.

Extracts a single code block from an LLM's free-form text response so
adapters (Anthropic, OpenAI-compatible, ...) all behave identically.
"""

import re

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)

# Accepted language tags per user-facing language, for matching fenced blocks.
_LANGUAGE_TAGS = {
    "python": {"python", "py"},
    "javascript": {"javascript", "js"},
}


def extract_code_block(text: str, language: str) -> str:
    """Extract the code the agent intends as its solution.

    Rules, in order:
        1. The first fenced block tagged with the requested language.
        2. If none, the first fenced block regardless of tag.
        3. If no fenced block exists at all, the full response, stripped.
    """
    matches = _FENCE_RE.findall(text or "")
    if not matches:
        return (text or "").strip()

    wanted_tags = _LANGUAGE_TAGS.get(language, set())
    for tag, body in matches:
        if tag.lower() in wanted_tags:
            return body.strip()

    return matches[0][1].strip()
