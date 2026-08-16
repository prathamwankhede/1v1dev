"""ClaudeCodeAgent — shells out to a local, already-authenticated `claude` CLI.

Unlike the other two adapters (which make an outbound HTTPS call with a
player-supplied key), this one spawns a local process using the *operator's*
own Claude Code login. No API key, no base URL — auth comes entirely from
the CLI session already on this machine. That's a different threat model:
the player's instruction is untrusted text handed to a subprocess that could,
if left unconstrained, read the host filesystem or run shell commands. Every
flag below exists to close that off (see phase3_claude_code_plan.md §5).
"""

import asyncio
import json
import os
import re
import shutil
import tempfile

from server.agents.interface import AgentBackend
from server.agents.parsing import find_code_block

CLI_BINARY = os.environ.get("CLAUDE_CLI_PATH", "claude")

# Rejects anything starting with "-" (so a player can't smuggle a flag like
# --dangerously-skip-permissions through the model field) and anything
# outside a conservative charset.
MODEL_RE = re.compile(r"^(?!-)[A-Za-z0-9._-]{1,64}$")

MAX_CONCURRENT = int(os.environ.get("CLAUDE_CLI_CONCURRENCY", "2"))
PROC_TIMEOUT = 90         # seconds — bounds the subprocess itself
QUEUE_WAIT_TIMEOUT = 10   # seconds — bounds waiting for a concurrency slot,
                          # kept separate so a busy queue reports "busy"
                          # rather than eating the model-call timeout budget.

# Defense in depth, not the guard — the load-bearing control is
# --permission-mode manual, which denies any tool call headless can't
# answer. This list is the second layer.
DISALLOWED_TOOLS = (
    "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,"
    "TodoWrite,SlashCommand,Skill,BashOutput,KillShell,ExitPlanMode"
)

SYSTEM_PROMPT = (
    "You are a pure code-generation assistant with no tools available. "
    "Given a problem and an instruction, respond with the complete solution "
    "in a single fenced code block. Do not explain, do not ask questions, "
    "do not attempt to use any tool."
)

_semaphore = None


def _get_semaphore():
    """Lazily construct the module-level concurrency semaphore.

    Must not be built at import time: on Python 3.9 (what this repo runs),
    asyncio.Semaphore() binds to whatever event loop is active when it's
    constructed, and tests run each case on a fresh
    IsolatedAsyncioTestCase loop. Building it on first use inside a running
    coroutine ties it to a loop that's actually live.
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


class ClaudeCodeAgent(AgentBackend):
    """Adapter for a local `claude` CLI, using the operator's own login.

    Off by default — requires ENABLE_LOCAL_CLAUDE_CODE=1 on the server.
    Intended for local dev / LAN play, not a shared deployment: anyone who
    can reach /ws spends the host's Claude quota, and this codebase has no
    per-player auth.
    """

    # PROC_TIMEOUT (execution) + QUEUE_WAIT_TIMEOUT (waiting for a
    # concurrency slot), read by Room.AGENT_TIMEOUT_SECONDS fallback logic
    # so the Room's outer timeout stays >= the adapter's own.
    timeout_seconds = PROC_TIMEOUT + QUEUE_WAIT_TIMEOUT

    def __init__(self, config):
        if os.environ.get("ENABLE_LOCAL_CLAUDE_CODE") != "1":
            raise ValueError("Local Claude Code backend is disabled on this server")
        model = (config.get("model") or "").strip()
        if model and not MODEL_RE.match(model):
            raise ValueError("Invalid model name")
        self.model = model or None  # omit --model → CLI default
        self.language = config.get("language", "python")

    async def run(self, prompt: str) -> dict:
        try:
            await asyncio.wait_for(_get_semaphore().acquire(), timeout=QUEUE_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError("Local agent is busy with another request — try again shortly.")

        try:
            return await self._run_once(prompt)
        finally:
            _get_semaphore().release()

    async def _run_once(self, prompt: str) -> dict:
        # A fresh empty cwd per call: running in the repo root would pull
        # this project's own CLAUDE.md, git status, and file tree into the
        # racing agent's context — a leak and a distraction either way.
        workdir = tempfile.mkdtemp(prefix="1v1dev-agent-")

        # Inherit env (the CLI needs HOME/keychain for the local login), but
        # strip ANTHROPIC_API_KEY so auth mode doesn't silently change
        # between machines.
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)

        argv = [
            CLI_BINARY, "-p",
            "--output-format", "json",
            "--disallowed-tools", DISALLOWED_TOOLS,
            "--permission-mode", "manual",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--setting-sources", "",
            "--system-prompt", SYSTEM_PROMPT,
        ]
        if self.model:
            argv += ["--model", self.model]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=PROC_TIMEOUT
            )
        finally:
            # Cancellation from the caller does not kill the child on its
            # own — without this, every timed-out or cancelled race leaves
            # a live `claude` process behind.
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            shutil.rmtree(workdir, ignore_errors=True)

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {stderr.decode(errors='replace')[:300]}"
            )

        try:
            data = json.loads(stdout.decode())
        except json.JSONDecodeError:
            raise RuntimeError(
                f"claude CLI returned unparseable output: {stdout.decode(errors='replace')[:300]}"
            )

        if data.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(data.get('result', ''))[:300]}")

        text = data.get("result", "")
        code, has_code = find_code_block(text, self.language)
        return {"code": code, "log": text, "hasCode": has_code}
