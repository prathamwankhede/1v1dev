"""Phase 3 tests: agent prompting is player-directed and never auto-submits.

Uses a FakeAgent test double (no real network calls) patched in place of
the registry's build_agent, so these tests exercise the full WebSocket
protocol without hitting Anthropic/OpenAI-compatible endpoints.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from server.main import create_app
from server.agents.interface import AgentBackend
from server.agents.parsing import extract_code_block, find_code_block
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


class ConversationFakeAgent(AgentBackend):
    """Test double for the structured multi-turn path (supports_conversation
    adapters — Anthropic, OpenAI-compatible)."""

    supports_conversation = True
    calls = []  # list of (system, messages)

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        raise AssertionError("run() should not be called when supports_conversation is True")

    async def run_conversation(self, system, messages):
        ConversationFakeAgent.calls.append((system, messages))
        n = len(ConversationFakeAgent.calls)
        return {
            "code": f"# conv response {n}",
            "log": f"conversation log {n}",
            "hasCode": True,
        }


class ProseOnlyFakeAgent(AgentBackend):
    """Test double whose reply has no fenced code block, to exercise the
    hasCode=False path."""

    calls = []

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        ProseOnlyFakeAgent.calls.append(prompt)
        text = "I think a dict works well here, no code needed yet."
        return {"code": text, "log": text, "hasCode": False}


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


class TestFindCodeBlock(unittest.TestCase):
    """find_code_block is extract_code_block's superset: same extraction
    rules, plus whether a real fence was found — a prose-only reply should
    not be silently treated as if it were code."""

    def test_fenced_block_reports_found_true(self):
        text = "Here you go:\n```python\nprint(1)\n```\nDone."
        code, found = find_code_block(text, "python")
        self.assertEqual(code, "print(1)")
        self.assertTrue(found)

    def test_wrong_tag_fence_still_reports_found_true(self):
        text = "```javascript\nconsole.log(1)\n```"
        code, found = find_code_block(text, "python")
        self.assertEqual(code, "console.log(1)")
        self.assertTrue(found)

    def test_prose_only_reports_found_false(self):
        text = "  just print(1) inline, no fence at all  "
        code, found = find_code_block(text, "python")
        self.assertEqual(code, "just print(1) inline, no fence at all")
        self.assertFalse(found)

    def test_empty_text_reports_found_false(self):
        code, found = find_code_block("", "python")
        self.assertEqual(code, "")
        self.assertFalse(found)


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


class TestAgentConversationHistory(unittest.IsolatedAsyncioTestCase):
    """Covers the conversation-history redesign: structured messages for
    supports_conversation adapters, raw-reply storage (not just extracted
    code), and hasCode reporting on the wire."""

    async def asyncSetUp(self):
        FakeAgent.calls = []
        ConversationFakeAgent.calls = []
        ProseOnlyFakeAgent.calls = []

        def _build_agent(agent_type, config):
            if agent_type == "conversation":
                return ConversationFakeAgent(config)
            if agent_type == "prose-only":
                return ProseOnlyFakeAgent(config)
            return FakeAgent(config)

        self.patcher = patch("server.room.build_agent", _build_agent)
        self.patcher.start()

        # Pin the problem for the same reason TestAgentPrompting does: a
        # problem-specific timeLimitSeconds would override a RACE_TIMEOUT
        # patch, and none of these tests are about problem content.
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

    async def _match_two_players(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)
        await ws1.send_json({"type": "join", "playerName": "Alice"})
        await ws2.send_json({"type": "join", "playerName": "Bob"})
        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")
        return ws1, ws2, race1, race2

    def _agent_prompt(self, agent_type, instruction, code=""):
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

    async def test_conversation_agent_receives_structured_messages(self):
        """A supports_conversation adapter gets a system string plus a real
        alternating messages array, with the live editor buffer riding on
        the final (user) message."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("conversation", "write a solution", code="x = 1"))
        await self.recv_type(ws1, "agentResponse")

        self.assertEqual(len(ConversationFakeAgent.calls), 1)
        system, messages = ConversationFakeAgent.calls[0]
        self.assertIn("Problem:", system)
        # First turn: no history yet, so just one user message carrying the
        # current code + instruction.
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("x = 1", messages[0]["content"])
        self.assertIn("write a solution", messages[0]["content"])

        await ws1.send_json(self._agent_prompt("conversation", "now handle negatives", code="x = 2"))
        await self.recv_type(ws1, "agentResponse")

        self.assertEqual(len(ConversationFakeAgent.calls), 2)
        _, messages2 = ConversationFakeAgent.calls[1]
        # The prior turn folds in as an alternating user/assistant pair,
        # then the new user message carries the *latest* editor buffer —
        # not what was current when the prior turn was sent.
        roles = [m["role"] for m in messages2]
        self.assertEqual(roles, ["user", "assistant", "user"])
        self.assertIn("write a solution", messages2[0]["content"])
        self.assertEqual(messages2[1], {"role": "assistant", "content": "conversation log 1"})
        self.assertIn("x = 2", messages2[-1]["content"])
        self.assertIn("now handle negatives", messages2[-1]["content"])
        self.assertNotIn("x = 1", messages2[-1]["content"])

        await ws1.close()
        await ws2.close()

    async def test_flat_agent_unaffected_by_conversation_path(self):
        """An adapter without supports_conversation still gets the single
        flat prompt string."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("fake", "write a solution"))
        response = await self.recv_type(ws1, "agentResponse")

        self.assertEqual(len(FakeAgent.calls), 1)
        self.assertIsInstance(FakeAgent.calls[0], str)
        self.assertIn("write a solution", FakeAgent.calls[0])
        self.assertEqual(response["code"], "# response 1")

        await ws1.close()
        await ws2.close()

    async def test_history_replays_raw_log_not_just_extracted_code(self):
        """The next turn's context should carry the agent's raw reply
        forward, not just the extracted code — otherwise a follow-up like
        'why did you use a dict there?' has nothing to refer back to."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("fake", "write a solution"))
        resp1 = await self.recv_type(ws1, "agentResponse")
        self.assertEqual(resp1["log"], "fake log")

        await ws1.send_json(self._agent_prompt("fake", "second instruction"))
        await self.recv_type(ws1, "agentResponse")

        # FakeAgent's flat path: the prior turn's raw log ("fake log"), not
        # just the extracted code ("# response 1"), must appear in context.
        self.assertIn("fake log", FakeAgent.calls[1])

        await ws1.close()
        await ws2.close()

    async def test_agent_response_includes_full_log(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("fake", "write a solution"))
        response = await self.recv_type(ws1, "agentResponse")

        self.assertEqual(response["log"], "fake log")
        self.assertIn("hasCode", response)

        await ws1.close()
        await ws2.close()

    async def test_prose_only_reply_reports_has_code_false(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json(self._agent_prompt("prose-only", "explain your approach"))
        response = await self.recv_type(ws1, "agentResponse")

        self.assertFalse(response["hasCode"])
        self.assertTrue(response["log"])
        self.assertEqual(response["code"], response["log"])

        await ws1.close()
        await ws2.close()

    async def test_agent_prompt_without_room_returns_error(self):
        """A late/stale agentPrompt with no active room must not just be
        dropped — the client would be left with a permanently disabled
        button and nothing to recover from."""
        ws1 = await self.session.ws_connect(self.ws_url)

        await ws1.send_json(self._agent_prompt("fake", "write a solution"))
        status = await self.recv_type(ws1, "agentStatus")
        self.assertEqual(status["status"], "error")

        await ws1.close()


class TestAgentTaskLifecycle(unittest.IsolatedAsyncioTestCase):
    """The agent call must run as a background task, never block the socket
    read loop, and never outlive the room. Uses SlowFakeAgent so these run
    fast and deterministically."""

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
