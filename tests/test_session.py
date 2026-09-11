"""Phase 4 tests: session tokens, disconnect grace, resume, and syncTree.

Follows the integration-test pattern from test_smoke.py/test_agents.py: a
real aiohttp server on an ephemeral port, driven over real WebSocket
connections. DISCONNECT_GRACE_SECONDS is patched down on every test class
here so the suite doesn't spend 45s per disconnect case.
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
from server.room import Room


class _FakeAgent:
    """Minimal agent double so agent-transcript tests don't hit a real API."""

    def __init__(self, config):
        self.config = config

    async def run(self, prompt):
        return {"code": "# fake code", "log": "fake log", "hasCode": True}


class TestSessionReconnect(unittest.IsolatedAsyncioTestCase):
    PROBLEM_ID = "two-sum"

    async def asyncSetUp(self):
        self.grace_patcher = patch.object(Room, "DISCONNECT_GRACE_SECONDS", 0.3)
        self.grace_patcher.start()

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
        self.grace_patcher.stop()

    async def recv_type(self, ws, msg_type, timeout=10):
        """Receive messages until we get one of the expected type."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
            if msg.get("type") == msg_type:
                return msg
        raise TimeoutError(f"Did not receive message of type '{msg_type}'")

    async def _join(self, ws, name):
        await ws.send_json({"type": "join", "playerName": name})
        session_msg = await self.recv_type(ws, "session")
        return session_msg["token"]

    async def _match_two_players(self, name1="Alice", name2="Bob"):
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)
        token1 = await self._join(ws1, name1)
        token2 = await self._join(ws2, name2)
        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")
        return ws1, ws2, token1, token2, race1, race2

    # ── 1. Token identity ───────────────────────────────────────

    async def test_join_returns_token_stable_across_resume(self):
        ws1, ws2, token1, _, _, _ = await self._match_two_players()

        # Alice never submitted, so closing her socket starts a disconnect
        # grace timer (patched to 0.3s class-wide) — widen it here so this
        # reconnect isn't racing that timer under load.
        with patch.object(Room, "DISCONNECT_GRACE_SECONDS", 5):
            await ws1.close()
            ws1b = await self.session.ws_connect(self.ws_url)
            await ws1b.send_json({"type": "resume", "token": token1})
            state = await self.recv_type(ws1b, "resumeState")
            self.assertEqual(state["phase"], "racing")

        await ws1b.close()
        await ws2.close()

    # ── 2. Disconnect no longer resolves the race ───────────────

    async def test_disconnect_does_not_resolve_race(self):
        ws1, ws2, _, _, _, _ = await self._match_two_players()

        await ws2.close()
        status = await self.recv_type(ws1, "opponentStatus")
        self.assertEqual(status["status"], "disconnected")

        with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
            await self.recv_type(ws1, "result", timeout=0.15)

        await ws1.close()
        await asyncio.sleep(0.5)  # let the grace timer settle before teardown

    # ── 3. Resume within grace restores full state ──────────────

    async def test_resume_restores_problem_time_history_and_transcript(self):
        with patch("server.room.build_agent", lambda agent_type, config: _FakeAgent(config)):
            ws1, ws2, token1, _, race1, _ = await self._match_two_players()

            await ws1.send_json({"type": "submit", "code": "print('nope')", "language": "python"})
            verdict = await self.recv_type(ws1, "submissionResult")
            self.assertFalse(verdict["accepted"])

            await ws1.send_json({
                "type": "agentPrompt",
                "agentType": "fake",
                "model": "fake-model",
                "baseUrl": "",
                "apiKey": "key",
                "language": "python",
                "instruction": "help me out",
                "code": "",
            })
            await self.recv_type(ws1, "agentResponse")

            await ws1.close()
            ws1b = await self.session.ws_connect(self.ws_url)
            await ws1b.send_json({"type": "resume", "token": token1})
            state = await self.recv_type(ws1b, "resumeState")

            self.assertEqual(state["phase"], "racing")
            self.assertEqual(state["problem"]["id"], race1["problem"]["id"])
            self.assertGreater(state["remainingSeconds"], 0)
            self.assertEqual(len(state["attempts"]), 1)
            self.assertTrue(state["attempts"][0]["judged"])
            self.assertFalse(state["attempts"][0]["accepted"])
            self.assertEqual(len(state["agentTranscript"]), 2)  # user + agent turn
            self.assertEqual(state["opponentName"], "Bob")
            self.assertTrue(state["opponentConnected"])

            await ws1b.close()
            await ws2.close()

    # ── 4. Resume after grace expiry gets the forfeit result ────

    async def test_resume_after_grace_expiry_gets_forfeit_result(self):
        ws1, ws2, _, token2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "code": "x = 1", "language": "python"})
        await self.recv_type(ws1, "submissionResult")

        await ws2.close()
        result = await self.recv_type(ws1, "result", timeout=5)
        self.assertEqual(result["winner"], "Alice")

        # Bob reconnects after the race already resolved.
        ws2b = await self.session.ws_connect(self.ws_url)
        await ws2b.send_json({"type": "resume", "token": token2})
        msg = await self.recv_type(ws2b, "result")
        self.assertEqual(msg["winner"], "Alice")

        await ws1.close()
        await ws2b.close()

    # ── 5. The clock keeps running across a disconnect ──────────

    async def test_clock_keeps_running_during_disconnect(self):
        ws1, ws2, token1, _, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "resume", "token": token1})
        before = await self.recv_type(ws1, "resumeState")

        # Bob never submitted, so his disconnect starts a grace timer for
        # *him* — widen it well past this test's 1s sleep so the race
        # doesn't resolve out from under the assertion below (this test is
        # about the clock, not the grace window — see the dedicated grace
        # tests for that).
        with patch.object(Room, "DISCONNECT_GRACE_SECONDS", 30):
            await ws2.close()
            await asyncio.sleep(1.0)

            await ws1.send_json({"type": "resume", "token": token1})
            after = await self.recv_type(ws1, "resumeState")

        elapsed = before["remainingSeconds"] - after["remainingSeconds"]
        self.assertGreater(elapsed, 0.7)

        await ws1.close()
        await asyncio.sleep(0.5)

    # ── 6. Duplicate socket claim ────────────────────────────────

    async def test_duplicate_socket_claim_closes_old_and_attaches_new(self):
        ws1, ws2, token1, _, _, _ = await self._match_two_players()

        ws1b = await self.session.ws_connect(self.ws_url)
        await ws1b.send_json({"type": "resume", "token": token1})
        state = await self.recv_type(ws1b, "resumeState")
        self.assertEqual(state["phase"], "racing")

        # Drain whatever else was already queued for the old socket (e.g. a
        # playerCount bump from ws1b's own connect) until it closes.
        close_types = (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
        deadline = asyncio.get_event_loop().time() + 5
        saw_close = False
        while asyncio.get_event_loop().time() < deadline:
            msg = await asyncio.wait_for(ws1.receive(), timeout=5)
            if msg.type in close_types:
                saw_close = True
                break
        self.assertTrue(saw_close, "old socket was never closed by the server")

        await ws1b.close()
        await ws2.close()

    # ── 7. Unknown token ─────────────────────────────────────────

    async def test_unknown_token_resume_failed(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        await ws1.send_json({"type": "resume", "token": "not-a-real-token"})
        msg = await self.recv_type(ws1, "resumeFailed")
        self.assertEqual(msg["reason"], "unknown")
        await ws1.close()

    # ── 8. syncTree round-trips, rejects a stale rev ─────────────

    async def test_sync_tree_round_trips_and_rejects_lower_rev(self):
        ws1, ws2, token1, _, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "syncTree", "rev": 1, "files": {"solution": "print(1)"}})
        await ws1.send_json({"type": "syncTree", "rev": 2, "files": {"solution": "print(2)"}})
        # Out-of-order/stale rev must not overwrite the newer copy.
        await ws1.send_json({"type": "syncTree", "rev": 1, "files": {"solution": "stale"}})
        await asyncio.sleep(0.2)  # let the fire-and-forget syncs land

        # Alice never submitted, so closing her socket starts a disconnect
        # grace timer — widen it so this reconnect isn't racing that timer.
        with patch.object(Room, "DISCONNECT_GRACE_SECONDS", 5):
            await ws1.close()
            ws1b = await self.session.ws_connect(self.ws_url)
            await ws1b.send_json({"type": "resume", "token": token1})
            state = await self.recv_type(ws1b, "resumeState")

        self.assertEqual(state["rev"], 2)
        self.assertEqual(state["tree"], {"solution": "print(2)"})

        await ws1b.close()
        await ws2.close()

    # ── 8b. Multi-file syncTree round-trips (Phase 5) ─────────────

    async def test_multifile_sync_tree_round_trips_both_writable_files(self):
        """A multi-file problem's tree carries every writable file's path,
        not just a single placeholder key — extends the Phase 4 syncTree
        test rather than duplicating it in test_multifile.py, per the
        Phase 5 plan's own instruction."""
        ws1 = await self.session.ws_connect(self.ws_url)
        ws2 = await self.session.ws_connect(self.ws_url)
        token1 = await self._join(ws1, "Alice")
        await self._join(ws2, "Bob")
        # This class is pinned to two-sum (single-file); resume/tree
        # handling doesn't care about problem shape, so reuse asyncSetUp's
        # pin rather than standing up a second server pinned to cart-pricing.
        await self.recv_type(ws1, "raceStart")
        await self.recv_type(ws2, "raceStart")

        await ws1.send_json({
            "type": "syncTree", "rev": 1,
            "files": {"cart.py": "def f(): pass", "extra.py": "x = 1"},
        })
        await asyncio.sleep(0.2)

        with patch.object(Room, "DISCONNECT_GRACE_SECONDS", 5):
            await ws1.close()
            ws1b = await self.session.ws_connect(self.ws_url)
            await ws1b.send_json({"type": "resume", "token": token1})
            state = await self.recv_type(ws1b, "resumeState")

        self.assertEqual(state["rev"], 1)
        self.assertEqual(state["tree"], {"cart.py": "def f(): pass", "extra.py": "x = 1"})

        await ws1b.close()
        await ws2.close()

    # ── 9. Name collision no longer shares state ─────────────────

    async def test_anonymous_collision_keeps_separate_state(self):
        ws1, ws2, _, _, _, _ = await self._match_two_players("Anonymous", "Anonymous")

        await ws1.send_json({"type": "submit", "code": "x = 1", "language": "python"})
        v1 = await self.recv_type(ws1, "submissionResult")
        self.assertEqual(v1["attempt"], 1)

        await ws2.send_json({"type": "submit", "code": "y = 2", "language": "python"})
        v2 = await self.recv_type(ws2, "submissionResult")
        # A shared name-keyed dict would make this Bob's *second* attempt.
        self.assertEqual(v2["attempt"], 1)

        await ws1.close()
        await ws2.close()
        await asyncio.sleep(0.5)

    # ── 10. In-flight judging survives a disconnect ──────────────

    async def test_inflight_judging_survives_disconnect(self):
        ws1, ws2, token1, _, _, _ = await self._match_two_players()

        slow_code = "import time\ntime.sleep(0.6)\nprint('nope')"
        await ws1.send_json({"type": "submit", "code": slow_code, "language": "python"})
        await asyncio.sleep(0.1)
        await ws1.close()

        # Let judging finish while the socket is down, then reconnect.
        await asyncio.sleep(1.0)

        ws1b = await self.session.ws_connect(self.ws_url)
        await ws1b.send_json({"type": "resume", "token": token1})
        state = await self.recv_type(ws1b, "resumeState")

        self.assertEqual(len(state["attempts"]), 1)
        self.assertTrue(state["attempts"][0]["judged"])

        await ws1b.close()
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
