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


def find_code_block(text: str, language: str) -> tuple:
    """Extract the code the agent intends as its solution.

    Rules, in order:
        1. The first fenced block tagged with the requested language.
        2. If none, the first fenced block regardless of tag.
        3. If no fenced block exists at all, the full response, stripped.

    Returns (code, found_fence). found_fence is False only for rule 3, so
    callers can tell a real code block from a prose-only reply — the
    transcript uses this to disable Apply on the latter rather than
    offering to paste an essay into the editor.
    """
    matches = _FENCE_RE.findall(text or "")
    if not matches:
        return (text or "").strip(), False

    wanted_tags = _LANGUAGE_TAGS.get(language, set())
    for tag, body in matches:
        if tag.lower() in wanted_tags:
            return body.strip(), True

    return matches[0][1].strip(), True


def extract_code_block(text: str, language: str) -> str:
    """Code-only convenience wrapper around find_code_block."""
    code, _ = find_code_block(text, language)
    return code
