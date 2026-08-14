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

    async def test_incorrect_submission_is_rejected(self):
        """Submitting fast no longer wins — the code has to actually pass.

        Speed only breaks ties between correct solutions, so an incorrect
        submission comes back rejected and the race stays live.
        """
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "code": "print('hello')", "language": "python"})

        verdict = await self.recv_type(ws1, "submissionResult")
        self.assertFalse(verdict["accepted"])
        self.assertLess(verdict["passCount"], verdict["totalTests"])

        # The race is still running: no result has been broadcast, and the
        # player is free to submit again.
        with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
            await self.recv_type(ws1, "result", timeout=2)

        await ws1.close()
        await ws2.close()

    async def test_single_submitter_wins(self):
        """If only one player attempts, they win when the other disconnects."""
        ws1, ws2, _, _ = await self._match_two_players()

        # Alice makes an attempt (it fails, but it is still an attempt)
        await ws1.send_json({"type": "submit", "code": "x = 1", "language": "python"})
        await self.recv_type(ws1, "submissionResult")

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


# Solves the kv-store-transactions problem completely.
KV_CORRECT = '''import sys

store = {}
undo = []

def record(key):
    if undo:
        undo[-1].append((key, key in store, store.get(key)))

out = []
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    cmd = parts[0]
    if cmd == "END":
        break
    elif cmd == "SET":
        record(parts[1])
        store[parts[1]] = parts[2]
    elif cmd == "GET":
        out.append(store.get(parts[1], "NULL"))
    elif cmd == "DELETE":
        record(parts[1])
        store.pop(parts[1], None)
    elif cmd == "COUNT":
        out.append(str(sum(1 for v in store.values() if v == parts[1])))
    elif cmd == "BEGIN":
        undo.append([])
    elif cmd == "ROLLBACK":
        if not undo:
            out.append("NO TRANSACTION")
        else:
            for key, had, old in reversed(undo.pop()):
                if had:
                    store[key] = old
                else:
                    store.pop(key, None)
    elif cmd == "COMMIT":
        if not undo:
            out.append("NO TRANSACTION")
        else:
            undo.clear()

print("\\n".join(out))
'''

# Handles the data commands but ignores transactions — passes the early
# tiers only, so it exercises partial credit.
KV_PARTIAL = '''import sys

store = {}
out = []
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    cmd = parts[0]
    if cmd == "END":
        break
    elif cmd == "SET":
        store[parts[1]] = parts[2]
    elif cmd == "GET":
        out.append(store.get(parts[1], "NULL"))
    elif cmd == "DELETE":
        store.pop(parts[1], None)
    elif cmd == "COUNT":
        out.append(str(sum(1 for v in store.values() if v == parts[1])))

print("\\n".join(out))
'''


class TestRetryLoop(unittest.IsolatedAsyncioTestCase):
    """Judge-on-submit: a rejected attempt can be fixed and resubmitted.

    Pins every match to the implementation problem so the assertions about
    hidden tests and partial credit are deterministic.
    """

    PROBLEM_ID = "kv-store-transactions"

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

    async def test_forced_problem_and_time_limit(self):
        """The lobby honours the pinned id, and the problem sets its clock."""
        ws1, ws2, race1, race2 = await self._match_two_players()

        self.assertEqual(race1["problem"]["id"], self.PROBLEM_ID)
        self.assertEqual(race2["problem"]["id"], self.PROBLEM_ID)
        self.assertEqual(race1["problem"]["timeLimitSeconds"], 480)
        self.assertEqual(race1["problem"]["kind"], "implementation")

        await ws1.close()
        await ws2.close()

    async def test_hidden_test_cases_are_not_sent_to_players(self):
        """Only sample cases ship in raceStart; the rest stay server-side."""
        ws1, ws2, race1, _ = await self._match_two_players()

        problem = race1["problem"]
        self.assertEqual(len(problem["testCases"]), 2)
        self.assertEqual(problem["totalTests"], 8)
        # The transaction tiers are the hidden ones — none may leak.
        for tc in problem["testCases"]:
            self.assertNotIn("BEGIN", tc["input"])

        await ws1.close()
        await ws2.close()

    async def test_partial_solution_is_rejected_with_partial_credit(self):
        """A solution that misses the transaction tiers scores 3/8, rejected."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "code": KV_PARTIAL, "language": "python"})
        verdict = await self.recv_type(ws1, "submissionResult")

        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["passCount"], 3)
        self.assertEqual(verdict["totalTests"], 8)
        self.assertEqual(verdict["attempt"], 1)

        # Hidden results report pass/fail but never their input or expected
        # output, or a player could dump the suite by resubmitting.
        hidden = [r for r in verdict["results"] if r["hidden"]]
        self.assertEqual(len(hidden), 6)
        for r in hidden:
            self.assertNotIn("input", r)
            self.assertNotIn("expected", r)

        await ws1.close()
        await ws2.close()

    async def test_resubmit_cooldown_is_enforced(self):
        """Back-to-back attempts are rate limited, to protect the sandbox."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "code": KV_PARTIAL, "language": "python"})
        await self.recv_type(ws1, "submissionResult")

        await ws1.send_json({"type": "submit", "code": KV_CORRECT, "language": "python"})
        err = await self.recv_type(ws1, "error")
        self.assertIn("resubmitting", err["message"])

        await ws1.close()
        await ws2.close()

    async def test_rejected_then_corrected_submission_wins(self):
        """The whole point: fail, fix, resubmit, win."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "code": KV_PARTIAL, "language": "python"})
        first = await self.recv_type(ws1, "submissionResult")
        self.assertFalse(first["accepted"])

        await asyncio.sleep(Room.RESUBMIT_COOLDOWN_SECONDS + 0.3)

        await ws1.send_json({"type": "submit", "code": KV_CORRECT, "language": "python"})
        second = await self.recv_type(ws1, "submissionResult")
        self.assertTrue(second["accepted"])
        self.assertEqual(second["passCount"], 8)
        self.assertEqual(second["attempt"], 2)

        result = await self.recv_type(ws1, "result")
        self.assertEqual(result["winner"], "Alice")

        alice = next(s for s in result["submissions"] if s["player"] == "Alice")
        self.assertTrue(alice["passed"])
        self.assertEqual(alice["attempts"], 2)

        bob = next(s for s in result["submissions"] if s["player"] == "Bob")
        self.assertFalse(bob["submitted"])

        await ws1.close()
        await ws2.close()

    async def test_earlier_submission_wins_even_when_judged_slower(self):
        """The race is decided on submission time, not judging latency.

        Alice submits first but her code is padded so her verdict lands
        second. She must still win. This also covers the deadlock that an
        earlier design hit, where the two judging tasks awaited each other.
        """
        ws1, ws2, _, _ = await self._match_two_players()

        slow_but_correct = "import time\ntime.sleep(0.8)\n" + KV_CORRECT

        await ws1.send_json({"type": "submit", "code": slow_but_correct, "language": "python"})
        await asyncio.sleep(0.15)
        await ws2.send_json({"type": "submit", "code": KV_CORRECT, "language": "python"})

        result = await self.recv_type(ws1, "result")
        self.assertEqual(result["winner"], "Alice")

        times = {s["player"]: s["timeMs"] for s in result["submissions"]}
        self.assertLess(times["Alice"], times["Bob"])

        await ws1.close()
        await ws2.close()

    async def test_accepted_attempt_resolves_when_earlier_attempt_fails(self):
        """A later correct attempt still wins once the earlier one is judged.

        Alice submits first but incorrectly, and slowly enough that she is
        still being judged when Bob's correct attempt lands. Bob can only be
        declared once Alice's earlier attempt is known to have failed, so
        this is the case where the race must resolve off Alice's rejection.
        """
        ws1, ws2, _, _ = await self._match_two_players()

        slow_but_wrong = "import time\ntime.sleep(0.8)\n" + KV_PARTIAL

        await ws1.send_json({"type": "submit", "code": slow_but_wrong, "language": "python"})
        await asyncio.sleep(0.15)
        await ws2.send_json({"type": "submit", "code": KV_CORRECT, "language": "python"})

        result = await self.recv_type(ws2, "result", timeout=30)
        self.assertEqual(result["winner"], "Bob")

        alice = next(s for s in result["submissions"] if s["player"] == "Alice")
        self.assertFalse(alice["passed"])

        await ws1.close()
        await ws2.close()

    async def test_correct_solution_beats_a_failing_opponent(self):
        """Correctness outranks speed: Bob submits first but fails."""
        ws1, ws2, _, _ = await self._match_two_players()

        # Bob submits an incorrect solution first...
        await ws2.send_json({"type": "submit", "code": KV_PARTIAL, "language": "python"})
        bob_verdict = await self.recv_type(ws2, "submissionResult")
        self.assertFalse(bob_verdict["accepted"])

        # ...then Alice submits a correct one and takes the race.
        await ws1.send_json({"type": "submit", "code": KV_CORRECT, "language": "python"})
        alice_verdict = await self.recv_type(ws1, "submissionResult")
        self.assertTrue(alice_verdict["accepted"])

        result = await self.recv_type(ws2, "result")
        self.assertEqual(result["winner"], "Alice")

        await ws1.close()
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
