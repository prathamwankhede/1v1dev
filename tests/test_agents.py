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
from server.agents.parsing import extract_code_block


class FakeAgent(AgentBackend):
    """Test double: records every prompt it was given, returns canned code."""

    calls = []

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        FakeAgent.calls.append(prompt)
        return {"code": f"# response {len(FakeAgent.calls)}", "log": "fake log"}


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

        self.app = create_app()
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

        # An explicit submit still resolves the race normally.
        await ws1.send_json({"type": "submit", "code": "print('hi')", "language": "python"})
        await ws2.send_json({"type": "submit", "code": "print('bye')", "language": "python"})

        result1 = await self.recv_type(ws1, "result")
        self.assertIn("winner", result1)

        await ws1.close()
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
