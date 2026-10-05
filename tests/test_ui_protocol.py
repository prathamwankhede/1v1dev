"""Race-view redesign tests: the wire fields the new UI renders.

Stage 3 covers the optional Problem-tab fields (difficulty, category,
summary, inputSpec) and the race-relative submission time
(submittedAtMs) on live and resumed attempts.

Follows the integration-test pattern from test_multifile.py: a real
aiohttp server on an ephemeral port, driven over real WebSocket
connections, pinned to a problem via create_app(forced_problem_id=...).
None of these assert on a pass count, so they don't need Piston:
without it, Judge still returns a definitive all-fail verdict.
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from server.main import create_app
from server.problems import ProblemBank
from server.room import Room

PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems"
DISPLAY_FIELDS = ("difficulty", "category", "summary", "inputSpec")


class TestDisplayFieldValidation(unittest.TestCase):
    """A typo in an optional display field fails at startup, not in a
    player's browser."""

    BASE = {
        "id": "x", "title": "X", "description": "d", "testCases": [],
        "starterCode": {"python": "", "javascript": ""},
    }

    def _load(self, **extra):
        d = Path(tempfile.mkdtemp())
        (d / "p.json").write_text(json.dumps({**self.BASE, **extra}))
        return ProblemBank(d)

    def test_valid_fields_load(self):
        bank = self._load(
            difficulty="hard", category="Graphs", summary="Do `x`.",
            inputSpec={"summary": "s", "fields": ["a", "b"], "note": "n"},
        )
        self.assertEqual(bank.get_by_id("x")["difficulty"], "hard")

    def test_unknown_difficulty_rejected(self):
        with self.assertRaises(ValueError):
            self._load(difficulty="meduim")

    def test_input_spec_fields_must_be_strings(self):
        with self.assertRaises(ValueError):
            self._load(inputSpec={"fields": "a b c"})
        with self.assertRaises(ValueError):
            self._load(inputSpec={"fields": ["a", 2]})

    def test_summary_must_be_string(self):
        with self.assertRaises(ValueError):
            self._load(summary=["not", "a", "string"])


class TestAttemptSummaryTime(unittest.TestCase):
    """_attempt_summary carries submittedAtMs for judged and unjudged
    attempts, measured from race start."""

    def setUp(self):
        problem = ProblemBank(PROBLEMS_DIR).get_by_id("cart-pricing")
        self.room = Room(
            "r1",
            {"ws": None, "name": "Alice", "token": "t1"},
            {"ws": None, "name": "Bob", "token": "t2"},
            problem,
            judge=None,
        )
        self.room.race_start_time = time.time() - 10

    def test_unjudged_attempt(self):
        attempt = {"timestamp": self.room.race_start_time + 4.25, "verdict": None}
        summary = self.room._attempt_summary(attempt, 0)
        self.assertFalse(summary["judged"])
        self.assertEqual(summary["submittedAtMs"], 4250)

    def test_judged_attempt(self):
        attempt = {
            "timestamp": self.room.race_start_time + 1.5,
            "verdict": {"passed": False, "pass_count": 0, "total": 8, "results": []},
        }
        summary = self.room._attempt_summary(attempt, 1)
        self.assertEqual(summary["attempt"], 2)
        self.assertEqual(summary["submittedAtMs"], 1500)


class _WireTest(unittest.IsolatedAsyncioTestCase):
    PROBLEM_ID = None

    async def asyncSetUp(self):
        self.app = create_app(forced_problem_id=self.PROBLEM_ID)
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

    async def recv_type(self, ws, msg_type, timeout=30):
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
        token1 = (await self.recv_type(ws1, "session"))["token"]
        await ws2.send_json({"type": "join", "playerName": "Bob"})
        race1 = await self.recv_type(ws1, "raceStart")
        await self.recv_type(ws2, "raceStart")
        return ws1, ws2, token1, race1


class TestDisplayFieldsOnWire(_WireTest):
    PROBLEM_ID = "cart-pricing"

    async def test_race_start_and_resume_carry_display_fields(self):
        ws1, ws2, token1, race1 = await self._match_two_players()

        problem = race1["problem"]
        self.assertEqual(problem["difficulty"], "medium")
        self.assertEqual(problem["category"], "Pricing Engine")
        self.assertIn("`cart.py`", problem["summary"])
        self.assertEqual(problem["inputSpec"]["fields"], ["unitCents", "bulkCents", "quantity"])
        # The agent still gets the full statement, so it must stay intact.
        self.assertIn("Output Format:", problem["description"])

        # Resume on a fresh socket: resumeState shares the same whitelist.
        await ws1.close()
        ws1b = await self.session.ws_connect(self.ws_url)
        await ws1b.send_json({"type": "resume", "token": token1})
        resumed = await self.recv_type(ws1b, "resumeState")
        for key in DISPLAY_FIELDS:
            self.assertEqual(resumed["problem"][key], problem[key], key)

        await ws1b.close()
        await ws2.close()

    async def test_submission_time_on_live_and_resumed_attempts(self):
        ws1, ws2, token1, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "language": "python", "files": {}})
        await self.recv_type(ws1, "judging")
        result = await self.recv_type(ws1, "submissionResult")
        self.assertIsInstance(result["submittedAtMs"], int)
        self.assertGreaterEqual(result["submittedAtMs"], 0)
        # Submitted right after raceStart, well inside the first minute.
        self.assertLess(result["submittedAtMs"], 60_000)

        await ws1.close()
        ws1b = await self.session.ws_connect(self.ws_url)
        await ws1b.send_json({"type": "resume", "token": token1})
        resumed = await self.recv_type(ws1b, "resumeState")
        self.assertEqual(len(resumed["attempts"]), 1)
        self.assertEqual(resumed["attempts"][0]["submittedAtMs"], result["submittedAtMs"])

        await ws1b.close()
        await ws2.close()


class TestLegacyProblemUnchanged(_WireTest):
    PROBLEM_ID = "two-sum"

    async def test_legacy_problem_has_no_display_fields(self):
        ws1, ws2, _, race1 = await self._match_two_players()
        for key in DISPLAY_FIELDS:
            self.assertNotIn(key, race1["problem"])
        self.assertTrue(race1["problem"]["description"])
        await ws1.close()
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
