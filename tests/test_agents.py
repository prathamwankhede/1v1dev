"""Phase 3 tests: agent prompting is player-directed and never auto-submits.

Uses a FakeAgent test double (no real network calls) patched in place of
the registry's build_agent, so these tests exercise the full WebSocket
protocol without hitting Anthropic/OpenAI-compatible endpoints.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from server.main import create_app
from server.agents.interface import AgentBackend
from server.agents.parsing import extract_code_block
from server.agents.claude_code import ClaudeCodeAgent
from server.agents import claude_code as claude_code_module
from server.room import Room


class FakeAgent(AgentBackend):
    """Test double: records every prompt it was given, returns canned code."""

    calls = []

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        FakeAgent.calls.append(prompt)
        return {"code": f"# response {len(FakeAgent.calls)}", "log": "fake log"}


class SlowFakeAgent(AgentBackend):
    """Test double that sleeps before responding, so tests can observe
    in-flight state (non-blocking dispatch, cancellation on room exit)."""

    calls = []
    cancelled = []
    SLEEP_SECONDS = 1.5

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        SlowFakeAgent.calls.append(prompt)
        try:
            await asyncio.sleep(self.SLEEP_SECONDS)
        except asyncio.CancelledError:
            SlowFakeAgent.cancelled.append(prompt)
            raise
        return {"code": "# slow response", "log": "slow log"}


class FakeProc:
    """Stand-in for asyncio.subprocess.Process, for ClaudeCodeAgent tests."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False, delay=0):
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._hang = hang
        self._delay = delay
        self.returncode = None
        self.killed = False

    async def communicate(self, data=None):
        if self._hang:
            await asyncio.sleep(3600)
        if self._delay:
            await asyncio.sleep(self._delay)
        self.returncode = self._returncode
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class RecordingExec:
    """Replaces asyncio.create_subprocess_exec; records argv/kwargs and
    hands back a FakeProc built by proc_factory (called with no args)."""

    def __init__(self, proc_factory):
        self.calls = []
        self._proc_factory = proc_factory

    async def __call__(self, *argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return self._proc_factory()


def _canned_json(result="```python\nprint('hi')\n```", is_error=False):
    return json.dumps({"result": result, "is_error": is_error}).encode()


class TestExtractCodeBlock(unittest.TestCase):
    """Unit tests for the shared code-block extraction rule."""

    def test_language_tagged_fence(self):
        text = "Here you go:\n```python\nprint(1)\n```\nDone."
        self.assertEqual(extract_code_block(text, "python"), "print(1)")

    def test_prefers_tagged_language_over_first_fence(self):
        text = "```javascript\nconsole.log(1)\n```\n```python\nprint(1)\n```"
        self.assertEqual(extract_code_block(text, "python"), "print(1)")

    def test_falls_back_to_first_fence_if_no_tag_match(self):
        text = "```javascript\nconsole.log(1)\n```"
        self.assertEqual(extract_code_block(text, "python"), "console.log(1)")

    def test_falls_back_to_whole_response_if_no_fence(self):
        text = "  just print(1) inline  "
        self.assertEqual(extract_code_block(text, "python"), "just print(1) inline")


class TestAgentPrompting(unittest.IsolatedAsyncioTestCase):
    """The agent is a copilot the player directs — it never submits on its
    own. A prompt only ever returns code to the requesting player."""

    async def asyncSetUp(self):
        FakeAgent.calls = []
        self.patcher = patch("server.room.build_agent", lambda agent_type, config: FakeAgent(config))
        self.patcher.start()

        # Pin the problem: these tests are about agent plumbing, and a
        # problem carrying its own timeLimitSeconds would override the
        # RACE_TIMEOUT_SECONDS patching some of them rely on.
        self.app = create_app(forced_problem_id="two-sum")
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.session.close()
        await self.runner.cleanup()

    async def recv_type(self, ws, msg_type, timeout=15):
        """Receive messages until we get one of the expected type."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
            if msg.get("type") == msg_type:
                return msg
        raise TimeoutError(f"Did not receive message of type '{msg_type}'")

    async def _match_two_players(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)

        await ws1.send_json({"type": "join", "playerName": "Alice"})
        await ws2.send_json({"type": "join", "playerName": "Bob"})

        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")

        return ws1, ws2, race1, race2

    def _agent_prompt(self, instruction, code=""):
        return {
            "type": "agentPrompt",
            "agentType": "fake",
            "model": "fake-model",
            "baseUrl": "",
            "apiKey": "key",
            "language": "python",
            "instruction": instruction,
            "code": code,
        }

    async def test_agent_response_goes_to_requester_only(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("write a solution"))

        response = await self.recv_type(ws1, "agentResponse")
        self.assertEqual(response["code"], "# response 1")

        # Opponent only sees a status change, never prompt/code/log content.
        opp_status = await self.recv_type(ws2, "opponentStatus")
        self.assertIn(opp_status["status"], ("agent-thinking", "using-agent"))
        for key in ("instruction", "code", "log"):
            self.assertNotIn(key, opp_status)

        await ws1.close()
        await ws2.close()

    async def test_history_accumulates_across_prompts(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("first instruction"))
        await self.recv_type(ws1, "agentResponse")

        await ws1.send_json(self._agent_prompt("second instruction", code="# response 1"))
        await self.recv_type(ws1, "agentResponse")

        self.assertEqual(len(FakeAgent.calls), 2)
        # The second call's context should carry the first turn forward.
        self.assertIn("first instruction", FakeAgent.calls[1])

        await ws1.close()
        await ws2.close()

    async def test_agent_prompt_alone_never_resolves_race(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("write a solution"))
        await self.recv_type(ws1, "agentResponse")

        # No result should arrive from an agent prompt alone.
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv_type(ws1, "result", timeout=1)

        # An explicit submit still goes through to the judge. It is graded on
        # its own merits — incorrect code is rejected rather than winning —
        # so the assertion here is that a verdict comes back at all.
        await ws1.send_json({"type": "submit", "code": "print('hi')", "language": "python"})

        verdict = await self.recv_type(ws1, "submissionResult")
        self.assertFalse(verdict["accepted"])

        await ws1.close()
        await ws2.close()


class TestClaudeCodeAgent(unittest.IsolatedAsyncioTestCase):
    """Direct adapter tests — no real CLI invocation, no Room involved.

    tests/test_agents.py patches server.room.build_agent for the room-level
    tests above, so the room tests never touch a real adapter. These target
    ClaudeCodeAgent directly by patching asyncio.create_subprocess_exec.
    """

    def setUp(self):
        self._env_patcher = patch.dict(os.environ, {"ENABLE_LOCAL_CLAUDE_CODE": "1"})
        self._env_patcher.start()
        # Force a fresh semaphore bound to *this* test's event loop rather
        # than reusing one built (and loop-bound) by a previous test.
        claude_code_module._semaphore = None

    def tearDown(self):
        self._env_patcher.stop()
        claude_code_module._semaphore = None

    async def test_gate_raises_when_disabled(self):
        self._env_patcher.stop()
        os.environ.pop("ENABLE_LOCAL_CLAUDE_CODE", None)
        with self.assertRaises(ValueError):
            ClaudeCodeAgent({})
        self._env_patcher.start()

    async def test_argv_omits_model_flag_when_blank(self):
        recorder = RecordingExec(lambda: FakeProc(stdout=_canned_json()))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agent = ClaudeCodeAgent({"language": "python"})
            result = await agent.run("solve this")

        argv = recorder.calls[0]["argv"]
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("manual", argv)
        self.assertIn(claude_code_module.DISALLOWED_TOOLS, argv)
        self.assertNotIn("--model", argv)
        self.assertEqual(result["code"], "print('hi')")

    async def test_argv_includes_model_when_set(self):
        recorder = RecordingExec(lambda: FakeProc(stdout=_canned_json()))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agent = ClaudeCodeAgent({"model": "sonnet", "language": "python"})
            await agent.run("solve this")

        argv = recorder.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    async def test_rejects_flag_injection_via_model(self):
        with self.assertRaises(ValueError):
            ClaudeCodeAgent({"model": "--dangerously-skip-permissions"})

    async def test_rejects_shell_metacharacters_via_model(self):
        with self.assertRaises(ValueError):
            ClaudeCodeAgent({"model": "; rm -rf /"})

    async def test_is_error_raises_runtime_error(self):
        recorder = RecordingExec(lambda: FakeProc(stdout=_canned_json(result="boom", is_error=True)))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agent = ClaudeCodeAgent({})
            with self.assertRaises(RuntimeError):
                await agent.run("solve this")

    async def test_nonzero_exit_raises_runtime_error(self):
        recorder = RecordingExec(lambda: FakeProc(stdout=b"", stderr=b"auth error", returncode=1))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agent = ClaudeCodeAgent({})
            with self.assertRaises(RuntimeError):
                await agent.run("solve this")

    async def test_unparseable_stdout_raises_runtime_error(self):
        recorder = RecordingExec(lambda: FakeProc(stdout=b"not json"))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agent = ClaudeCodeAgent({})
            with self.assertRaises(RuntimeError):
                await agent.run("solve this")

    async def test_timeout_kills_hung_process(self):
        proc = FakeProc(hang=True)
        recorder = RecordingExec(lambda: proc)
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder), \
             patch("server.agents.claude_code.PROC_TIMEOUT", 0.05):
            agent = ClaudeCodeAgent({})
            with self.assertRaises(asyncio.TimeoutError):
                await agent.run("solve this")
        self.assertTrue(proc.killed)

    async def test_contended_semaphore_does_not_bind_to_stale_loop(self):
        """Regression test for the Python-3.9 asyncio.Semaphore() eager-loop-
        binding bug: three concurrent calls against MAX_CONCURRENT=2 force
        one to actually wait on the semaphore (the contended path), which is
        exactly the case a naive module-level Semaphore() breaks."""
        recorder = RecordingExec(lambda: FakeProc(stdout=_canned_json(), delay=0.05))
        with patch("server.agents.claude_code.asyncio.create_subprocess_exec", recorder):
            agents = [ClaudeCodeAgent({}) for _ in range(3)]
            results = await asyncio.gather(*(a.run("solve this") for a in agents))

        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result["code"], "print('hi')")


class TestAgentTaskLifecycle(unittest.IsolatedAsyncioTestCase):
    """Covers phase3_claude_code_plan.md §2b: the agent call must run as a
    background task, never block the socket read loop, and never outlive
    the room. Uses SlowFakeAgent (no CLI involved) so these run fast and
    deterministically."""

    async def asyncSetUp(self):
        FakeAgent.calls = []
        SlowFakeAgent.calls = []
        SlowFakeAgent.cancelled = []

        def _build_agent(agent_type, config):
            if agent_type == "slow":
                return SlowFakeAgent(config)
            return FakeAgent(config)

        self.patcher = patch("server.room.build_agent", _build_agent)
        self.patcher.start()

        # Pin the problem: test_agent_task_cancelled_on_race_timeout patches
        # RACE_TIMEOUT_SECONDS, which a problem's own timeLimitSeconds wins
        # over. two-sum sets no limit, so the patch takes effect.
        self.app = create_app(forced_problem_id="two-sum")
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.session.close()
        await self.runner.cleanup()

    async def recv_type(self, ws, msg_type, timeout=15):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
            if msg.get("type") == msg_type:
                return msg
        raise TimeoutError(f"Did not receive message of type '{msg_type}'")

    async def _wait_until(self, predicate, timeout=2):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("Condition not met before timeout")

    async def _match_two_players(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)

        await ws1.send_json({"type": "join", "playerName": "Alice"})
        await ws2.send_json({"type": "join", "playerName": "Bob"})

        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")

        return ws1, ws2, race1, race2

    def _agent_prompt(self, instruction, agent_type="slow", code=""):
        return {
            "type": "agentPrompt",
            "agentType": agent_type,
            "model": "fake-model",
            "baseUrl": "",
            "apiKey": "key",
            "language": "python",
            "instruction": instruction,
            "code": code,
        }

    async def test_submit_not_blocked_by_in_flight_agent_call(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("write something"))
        await self._wait_until(lambda: len(SlowFakeAgent.calls) >= 1)

        await ws1.send_json({"type": "submit", "code": "print('hi')", "language": "python"})

        # If the agent call blocked the read loop, this would only arrive
        # after SlowFakeAgent.SLEEP_SECONDS — well past this short timeout.
        # `judging` is the submit acknowledgement: it is sent before the
        # attempt is handed off to the judge.
        ack = await self.recv_type(ws1, "judging", timeout=SlowFakeAgent.SLEEP_SECONDS - 0.5)
        self.assertIsNotNone(ack)

        await ws1.close()
        await ws2.close()

    async def test_overlapping_prompt_rejected(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("first"))
        await self._wait_until(lambda: len(SlowFakeAgent.calls) >= 1)

        await ws1.send_json(self._agent_prompt("second"))
        status = await self.recv_type(ws1, "agentStatus", timeout=2)

        self.assertEqual(status["status"], "error")
        self.assertEqual(len(SlowFakeAgent.calls), 1)  # second never ran

        await ws1.close()
        await ws2.close()

    async def test_agent_task_cancelled_on_race_timeout(self):
        with patch.object(Room, "RACE_TIMEOUT_SECONDS", 1):
            ws1, ws2, _, _ = await self._match_two_players()

            await ws1.send_json(self._agent_prompt("write something"))
            await self._wait_until(lambda: len(SlowFakeAgent.calls) >= 1)

            await self.recv_type(ws1, "timeout", timeout=5)
            result = await self.recv_type(ws1, "result", timeout=5)
            self.assertIn("winner", result)

        await self._wait_until(lambda: len(SlowFakeAgent.cancelled) >= 1)

        await ws1.close()
        await ws2.close()

    async def test_agent_task_cancelled_on_disconnect(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("write something"))
        await self._wait_until(lambda: len(SlowFakeAgent.calls) >= 1)

        await ws1.close()

        await self._wait_until(lambda: len(SlowFakeAgent.cancelled) >= 1)

        await ws2.close()


if __name__ == "__main__":
    unittest.main()
