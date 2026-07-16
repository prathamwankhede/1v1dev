"""Phase 0 + Phase 1 tests: HTTP, WebSocket, matchmaking, room lifecycle, judging."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

# Allow `from server.main import ...` without packaging
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web
from server.main import create_app
from server.problems import ProblemBank
from server.room import Room, RoomState


PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems"


class TestSmoke(unittest.IsolatedAsyncioTestCase):
    """Phase 0: Basic HTTP serving and WebSocket connectivity."""

    async def asyncSetUp(self):
        self.app = create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://localhost:{self.port}"
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def recv(self, ws, timeout=5):
        """Receive one JSON message with a timeout."""
        return await asyncio.wait_for(ws.receive_json(), timeout=timeout)

    async def test_serves_index_html(self):
        async with self.session.get(self.base_url) as resp:
            self.assertEqual(resp.status, 200)
            text = await resp.text()
            self.assertIn("<title>1v1dev", text)

    async def test_player_count_tracking(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 1})

        ws2 = await self.session.ws_connect(self.ws_url)
        msg = await self.recv(ws2)
        self.assertEqual(msg, {"type": "playerCount", "count": 2})

        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 2})

        await ws2.close()
        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 1})

        await ws1.close()


class TestProblemBank(unittest.TestCase):
    """Problem bank loading and validation."""

    def test_loads_problems(self):
        bank = ProblemBank(PROBLEMS_DIR)
        self.assertGreaterEqual(len(bank), 3)

    def test_required_fields(self):
        bank = ProblemBank(PROBLEMS_DIR)
        for p in bank.problems:
            for field in ("id", "title", "description", "starterCode", "testCases"):
                self.assertIn(field, p, f"Problem {p.get('id', '?')} missing '{field}'")

    def test_get_random(self):
        bank = ProblemBank(PROBLEMS_DIR)
        p = bank.get_random()
        self.assertIn("id", p)

    def test_get_by_id(self):
        bank = ProblemBank(PROBLEMS_DIR)
        p = bank.get_by_id("two-sum")
        self.assertIsNotNone(p)
        self.assertEqual(p["title"], "Two Sum")

    def test_get_by_id_not_found(self):
        bank = ProblemBank(PROBLEMS_DIR)
        self.assertIsNone(bank.get_by_id("nonexistent"))


class TestMatchmaking(unittest.IsolatedAsyncioTestCase):
    """Lobby matchmaking: two players join → matched into a room."""

    async def asyncSetUp(self):
        self.app = create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def recv(self, ws, timeout=5):
        return await asyncio.wait_for(ws.receive_json(), timeout=timeout)

    async def recv_type(self, ws, msg_type, timeout=10):
        """Receive messages until we get one of the expected type."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
            if msg.get("type") == msg_type:
                return msg
        raise TimeoutError(f"Did not receive message of type '{msg_type}'")

    async def test_two_players_get_matched(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)

        # Drain playerCount messages
        await self.recv_type(ws1, "playerCount")
        await self.recv_type(ws2, "playerCount")

        # Both join
        await ws1.send_json({"type": "join", "playerName": "Alice"})
        await ws2.send_json({"type": "join", "playerName": "Bob"})

        # Both should receive a 'matched' message
        matched1 = await self.recv_type(ws1, "matched")
        matched2 = await self.recv_type(ws2, "matched")

        self.assertEqual(matched1["opponent"], "Bob")
        self.assertEqual(matched2["opponent"], "Alice")
        self.assertEqual(matched1["roomId"], matched2["roomId"])

        await ws1.close()
        await ws2.close()


class TestRaceLifecycle(unittest.IsolatedAsyncioTestCase):
    """Full race lifecycle: match → countdown → race → submit → result."""

    async def asyncSetUp(self):
        self.app = create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def recv(self, ws, timeout=5):
        return await asyncio.wait_for(ws.receive_json(), timeout=timeout)

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
        """Connect two players, join lobby, and wait for match + raceStart."""
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)

        await ws1.send_json({"type": "join", "playerName": "Alice"})
        await ws2.send_json({"type": "join", "playerName": "Bob"})

        # Wait for raceStart (which comes after matched + countdown)
        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")

        return ws1, ws2, race1, race2

    async def test_countdown_then_race_start(self):
        """After matching, both players should receive countdown and raceStart."""
        ws1, ws2, race1, race2 = await self._match_two_players()

        # Both should have received a problem in raceStart
        self.assertIn("problem", race1)
        self.assertIn("problem", race2)
        self.assertEqual(race1["problem"]["id"], race2["problem"]["id"])

        await ws1.close()
        await ws2.close()

    async def test_first_submitter_wins(self):
        """The first player to submit should win."""
        ws1, ws2, _, _ = await self._match_two_players()

        # Alice submits first
        await ws1.send_json({"type": "submit", "code": "print('hello')", "language": "python"})
        # Small delay so timestamps differ
        await asyncio.sleep(0.05)
        # Bob submits second
        await ws2.send_json({"type": "submit", "code": "print('world')", "language": "python"})

        # Both should receive a result
        result1 = await self.recv_type(ws1, "result")
        result2 = await self.recv_type(ws2, "result")

        self.assertEqual(result1["winner"], "Alice")
        self.assertEqual(result2["winner"], "Alice")

        # Verify submission details
        self.assertEqual(len(result1["submissions"]), 2)
        for sub in result1["submissions"]:
            self.assertTrue(sub["submitted"])

        await ws1.close()
        await ws2.close()

    async def test_single_submitter_wins(self):
        """If only one player submits, they win when the other disconnects."""
        ws1, ws2, _, _ = await self._match_two_players()

        # Alice submits
        await ws1.send_json({"type": "submit", "code": "x = 1", "language": "python"})
        await self.recv_type(ws1, "submitted")

        # Bob disconnects without submitting
        await ws2.close()

        # Alice should receive the result (winner)
        result = await self.recv_type(ws1, "result")
        self.assertEqual(result["winner"], "Alice")

        await ws1.close()

    async def test_no_submissions_tie(self):
        """If neither player submits and both disconnect, no crash occurs."""
        ws1, ws2, _, _ = await self._match_two_players()

        # Both disconnect without submitting
        await ws1.close()
        # Give server time to process
        await asyncio.sleep(0.1)
        await ws2.close()
        # No assertions — just verify no crash


if __name__ == "__main__":
    unittest.main()
