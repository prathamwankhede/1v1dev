"""Phase 5 tests: multi-file problems — bundle normalization, assembly,
validation, and the wire protocol built on top of them.

Follows the integration-test pattern from test_smoke.py/test_agents.py: a
real aiohttp server on an ephemeral port, driven over real WebSocket
connections, pinned to "cart-pricing" (problems/cart-pricing.json) so
assertions don't depend on which problem get_random() happens to pick.

Judging tests that assert on an exact pass count need Piston up — see
CLAUDE.md. Tests that only assert on rejection/wire-shape do not, since
Judge.evaluate's sandbox-error fallback still produces a definitive
(all-fail) verdict without Piston.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from server.main import create_app
from server.problems import ProblemBank, normalize_bundle
from server.room import Room

PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems"


def _fixture_file_content(problem_id, language, path):
    """Read one declared file's content straight out of the real fixture
    JSON, so test expectations can never drift from the file on disk."""
    with open(PROBLEMS_DIR / f"{problem_id}.json") as f:
        problem = json.load(f)
    for entry in problem["files"][language]["files"]:
        if entry["path"] == path:
            return entry["content"]
    raise KeyError(path)


RULES_PY = _fixture_file_content("cart-pricing", "python", "rules.py")
CART_BUGGY = _fixture_file_content("cart-pricing", "python", "cart.py")
CART_FIXED = CART_BUGGY.replace("quantity > BULK_THRESHOLD", "quantity >= BULK_THRESHOLD")
CART_IMPORT_CRASH = "import this_module_does_not_exist\n" + CART_FIXED


class TestNormalizeBundle(unittest.TestCase):
    """Unit tests for the pure bundle-normalization helper."""

    def setUp(self):
        self.bank = ProblemBank(PROBLEMS_DIR)

    def test_legacy_problem_normalizes_to_one_writable_file(self):
        problem = self.bank.get_by_id("two-sum")
        bundle = normalize_bundle(problem, "python")
        self.assertEqual(bundle["entrypoint"], "solution.py")
        self.assertEqual(len(bundle["files"]), 1)
        self.assertTrue(bundle["files"][0]["writable"])

    def test_multifile_problem_normalizes_declared_files(self):
        problem = self.bank.get_by_id("cart-pricing")
        bundle = normalize_bundle(problem, "python")
        paths = {f["path"]: f["writable"] for f in bundle["files"]}
        self.assertEqual(paths, {"rules.py": False, "cart.py": True})
        # testFiles supplies the real entrypoint at judge time — a harness
        # problem omits entrypoint from files entirely.
        self.assertIsNone(bundle["entrypoint"])


class TestProblemBankValidation(unittest.TestCase):
    """The bank loads real problems fine; malformed multi-file schemas
    should fail loudly at load time rather than at first submit."""

    def _bank_with(self, problem):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "p.json").write_text(json.dumps(problem))
        return d

    def test_real_problem_bank_loads(self):
        bank = ProblemBank(PROBLEMS_DIR)
        self.assertIsNotNone(bank.get_by_id("cart-pricing"))

    def test_neither_starter_code_nor_files_rejected(self):
        d = self._bank_with({
            "id": "x", "title": "X", "description": "d", "testCases": [],
        })
        with self.assertRaises(ValueError):
            ProblemBank(d)

    def test_no_writable_file_rejected(self):
        d = self._bank_with({
            "id": "x", "title": "X", "description": "d", "testCases": [],
            "files": {"python": {"entrypoint": "a.py", "files": [
                {"path": "a.py", "content": "x", "writable": False},
            ]}},
        })
        with self.assertRaises(ValueError):
            ProblemBank(d)

    def test_nested_path_rejected(self):
        d = self._bank_with({
            "id": "x", "title": "X", "description": "d", "testCases": [],
            "files": {"python": {"entrypoint": "sub/a.py", "files": [
                {"path": "sub/a.py", "content": "x", "writable": True},
            ]}},
        })
        with self.assertRaises(ValueError):
            ProblemBank(d)

    def test_missing_entrypoint_without_test_files_rejected(self):
        d = self._bank_with({
            "id": "x", "title": "X", "description": "d", "testCases": [],
            "files": {"python": {"files": [
                {"path": "a.py", "content": "x", "writable": True},
            ]}},
        })
        with self.assertRaises(ValueError):
            ProblemBank(d)

    def test_overlap_between_files_and_test_files_rejected(self):
        d = self._bank_with({
            "id": "x", "title": "X", "description": "d", "testCases": [],
            "files": {"python": {"files": [
                {"path": "a.py", "content": "x", "writable": True},
            ]}},
            "testFiles": {"python": [{"path": "a.py", "content": "y"}]},
        })
        with self.assertRaises(ValueError):
            ProblemBank(d)


class TestAssembleBundle(unittest.TestCase):
    """Unit tests for Room._assemble_bundle against the real cart-pricing
    fixture — no server, no judge, no Piston needed."""

    def setUp(self):
        bank = ProblemBank(PROBLEMS_DIR)
        problem = bank.get_by_id("cart-pricing")
        self.room = Room(
            "r1",
            {"ws": None, "name": "Alice", "token": "t1"},
            {"ws": None, "name": "Bob", "token": "t2"},
            problem,
            judge=None,
        )

    def test_valid_submission_prepends_harness_and_keeps_locked_file(self):
        files, entrypoint = self.room._assemble_bundle("python", {"cart.py": CART_FIXED})
        self.assertEqual(entrypoint, "main.py")
        by_path = {f["path"]: f["content"] for f in files}
        self.assertEqual(set(by_path), {"main.py", "rules.py", "cart.py"})
        self.assertEqual(by_path["rules.py"], RULES_PY)
        self.assertEqual(by_path["cart.py"], CART_FIXED)
        # The harness must be first for Piston to treat it as the entrypoint.
        self.assertEqual(files[0]["path"], "main.py")

    def test_locked_file_content_is_ignored_not_rejected(self):
        """readOnly is a UX constraint, not a security boundary — a client
        sending modified locked content doesn't reject the attempt, it's
        just never consulted."""
        result = self.room._assemble_bundle("python", {
            "rules.py": "BULK_THRESHOLD = 0  # hacked",
            "cart.py": CART_FIXED,
        })
        self.assertIsNotNone(result)
        files, _ = result
        by_path = {f["path"]: f["content"] for f in files}
        self.assertEqual(by_path["rules.py"], RULES_PY)

    def test_undeclared_path_rejected(self):
        """A path that isn't declared anywhere (writable, locked, or the
        harness) is a broken or hostile client — reject the whole attempt."""
        self.assertIsNone(self.room._assemble_bundle("python", {"hack.py": "x"}))

    def test_harness_path_from_client_rejected(self):
        self.assertIsNone(self.room._assemble_bundle("python", {"main.py": "print(1)"}))

    def test_omitted_writable_file_falls_back_to_starter(self):
        files, _ = self.room._assemble_bundle("python", {})
        by_path = {f["path"]: f["content"] for f in files}
        self.assertEqual(by_path["cart.py"], CART_BUGGY)


class TestMultifileWire(unittest.IsolatedAsyncioTestCase):
    """Integration tests over the real WebSocket protocol, pinned to
    cart-pricing so they don't depend on which problem get_random() picks."""

    PROBLEM_ID = "cart-pricing"

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
        await ws2.send_json({"type": "join", "playerName": "Bob"})
        race1 = await self.recv_type(ws1, "raceStart")
        race2 = await self.recv_type(ws2, "raceStart")
        return ws1, ws2, race1, race2

    async def test_raw_frame_never_leaks_test_files(self):
        """testFiles content appears in no outbound message — checked
        against the raw raceStart frame, not just a parsed field."""
        ws1, ws2, race1, _ = await self._match_two_players()

        self.assertEqual(race1["problem"]["id"], self.PROBLEM_ID)
        py_files = race1["problem"]["files"]["python"]["files"]
        paths = {f["path"] for f in py_files}
        self.assertEqual(paths, {"rules.py", "cart.py"})
        self.assertNotIn("main.py", paths)
        self.assertNotIn("starterCode", race1["problem"])

        raw = json.dumps(race1)
        self.assertNotIn("import cart", raw)  # main.py's harness body
        self.assertNotIn("sys.stdin", raw)

        await ws1.close()
        await ws2.close()

    async def test_writable_path_outside_declared_set_is_rejected(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({
            "type": "submit", "language": "python",
            "files": {"hack.py": "print(1)"},
        })
        err = await self.recv_type(ws1, "error")
        self.assertIn("doesn't allow editing", err["message"])

        # It must never have reached judging.
        with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
            await self.recv_type(ws1, "judging", timeout=0.5)

        await ws1.close()
        await ws2.close()

    async def test_locked_file_submission_still_gets_judged(self):
        """Submitting (ignored) locked-file content alongside a real
        writable file must not trip the rejection path."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({
            "type": "submit", "language": "python",
            "files": {"rules.py": "HACKED = 1", "cart.py": CART_FIXED},
        })
        ack = await self.recv_type(ws1, "judging")
        self.assertEqual(ack["attempt"], 1)

        await ws1.close()
        await ws2.close()

    async def test_omitted_writable_file_uses_starter_and_is_rejected(self):
        """The shipped fixture is buggy — submitting no files at all falls
        back to the buggy starter and must not be accepted."""
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "language": "python", "files": {}})
        verdict = await self.recv_type(ws1, "submissionResult")
        self.assertFalse(verdict["accepted"])

        await ws1.close()
        await ws2.close()

    async def test_legacy_bare_code_still_works_for_single_file_problem(self):
        """A pre-Phase-5 client sending bare `code` (no `files`) must still
        work for an ordinary single-file problem."""
        app = create_app(forced_problem_id="two-sum")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            ws1 = await self.session.ws_connect(f"http://localhost:{port}/ws")
            ws2 = await self.session.ws_connect(f"http://localhost:{port}/ws")
            await ws1.send_json({"type": "join", "playerName": "Alice"})
            await ws2.send_json({"type": "join", "playerName": "Bob"})
            await self.recv_type(ws1, "raceStart")
            await self.recv_type(ws2, "raceStart")

            await ws1.send_json({"type": "submit", "code": "print('nope')", "language": "python"})
            verdict = await self.recv_type(ws1, "submissionResult")
            self.assertFalse(verdict["accepted"])

            await ws1.close()
            await ws2.close()
        finally:
            await runner.cleanup()

    # ── The following need real Piston execution (see CLAUDE.md) ────────

    async def test_buggy_fixture_rejected_with_samples_visible_hidden_stripped(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({"type": "submit", "language": "python", "files": {}})
        verdict = await self.recv_type(ws1, "submissionResult")

        self.assertFalse(verdict["accepted"])
        hidden = [r for r in verdict["results"] if r["hidden"]]
        self.assertEqual(len(hidden), 5)
        for r in hidden:
            self.assertNotIn("input", r)
            self.assertNotIn("expected", r)

        await ws1.close()
        await ws2.close()

    async def test_corrected_fixture_is_accepted(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({
            "type": "submit", "language": "python",
            "files": {"cart.py": CART_FIXED},
        })
        verdict = await self.recv_type(ws1, "submissionResult")
        self.assertTrue(verdict["accepted"])
        self.assertEqual(verdict["passCount"], 8)

        await ws1.close()
        await ws2.close()

    async def test_import_crash_reported_as_distinct_message(self):
        ws1, ws2, _, _ = await self._match_two_players()

        await ws1.send_json({
            "type": "submit", "language": "python",
            "files": {"cart.py": CART_IMPORT_CRASH},
        })
        verdict = await self.recv_type(ws1, "submissionResult")

        self.assertFalse(verdict["accepted"])
        self.assertIsNotNone(verdict.get("importError"))
        self.assertIn("this_module_does_not_exist", verdict["importError"])

        await ws1.close()
        await ws2.close()


if __name__ == "__main__":
    unittest.main()
