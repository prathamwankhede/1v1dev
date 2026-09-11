"""Room state machine — manages a single two-player race.

States: COUNTDOWN → RACING → RESOLVING → FINISHED

Phase 2 judging: code is executed against test cases via sandbox.
Winner is determined by correctness first, then timestamp as tiebreaker.

Phase 4 identity: `self.players` entries are the same session dicts Lobby
owns (`Lobby.sessions[token]`), not copies — so a reconnect that reassigns
`session["ws"]` is visible here automatically. Every per-player structure
below is keyed by `token`, not by display name, since nothing dedupes
names and two players named "Anonymous" would otherwise share state.
`name` still flows into outward-facing payloads for display.
"""

import asyncio
import json
import time

from langchain_core.messages import AIMessage, HumanMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

from server.agents.registry import build_agent
from server.problems import normalize_bundle


class RoomState:
    COUNTDOWN = "countdown"
    RACING = "racing"
    RESOLVING = "resolving"
    FINISHED = "finished"


class Room:
    COUNTDOWN_SECONDS = 5
    RACE_TIMEOUT_SECONDS = 120  # default when a problem sets no timeLimitSeconds
    AGENT_TIMEOUT_SECONDS = 60
    AGENT_HISTORY_LIMIT = 20  # messages (10 player/agent turn pairs), FIFO
    AGENT_TURN_CHAR_LIMIT = 8000  # cap a stored turn's raw text so 20 turns
                                  # of history can't grow the prompt unbounded
    AGENT_HISTORY_TOKEN_BUDGET = 6000  # trim_messages budget for the history
                                        # sent to structured-message adapters
                                        # (anthropic, openai-compatible) —
                                        # separate from the storage-side caps
                                        # above, which still bound agent_sessions
                                        # itself and any flat-prompt adapter
    RESUBMIT_COOLDOWN_SECONDS = 3  # floor between one player's attempts
    JUDGE_DRAIN_SECONDS = 15  # grace for judging an attempt made before time ran out
    DISCONNECT_GRACE_SECONDS = 45  # window to reconnect before a dropped
                                    # player's non-attempt becomes a forfeit;
                                    # also reused as the room's post-result
                                    # retention window (see Lobby._teardown_room)
    MAX_TREE_BYTES = 256 * 1024  # cap on a syncTree payload, and reused as
                                  # the cap on a submitted files map (Phase 5)

    def __init__(self, room_id, player1, player2, problem, judge=None, on_finished=None):
        """Create a new room.

        Args:
            room_id: Short unique identifier.
            player1: session dict with (at least) "ws", "name", "token".
            player2: session dict with (at least) "ws", "name", "token".
            problem: Problem dict from the problem bank.
            judge: Judge instance for code evaluation (Phase 2+).
            on_finished: Called (no args) once resolve() reaches FINISHED —
                Lobby uses this to schedule room teardown.
        """
        self.room_id = room_id
        self.players = [player1, player2]
        self.problem = problem
        self.judge = judge
        self.on_finished = on_finished
        self.state = RoomState.COUNTDOWN
        # token → [{ code, language, timestamp, verdict }, ...] — one entry
        # per attempt, since a rejected submission can be retried.
        self.submissions = {}
        # token → [{ role, content }, ...] — agent turns also carry "code"
        # and "hasCode", the extracted solution alongside the raw reply text
        # stored in "content".
        self.agent_sessions = {}
        self.agent_tasks = {}  # token → asyncio.Task running _run_agent_prompt
        self.judge_tasks = {}  # token → asyncio.Task running _run_judging
        self.grace_tasks = {}  # token → asyncio.Task running _grace_timeout
        self.last_submit_at = {}  # token → monotonic time of last attempt
        self.accepted = {}  # token → the attempt that passed every test
        self.winner_name = None  # set as soon as an accepted attempt is settled
        self.race_start_time = None
        self._timeout_task = None
        self._countdown_task = None
        self._last_result = None  # the broadcast 'result' payload, for resume

        # A problem may set its own clock; implementation-style problems need
        # far longer than the algorithmic puzzles this default was sized for.
        try:
            self.time_limit = int(problem.get("timeLimitSeconds") or 0)
        except (TypeError, ValueError):
            self.time_limit = 0
        if self.time_limit <= 0:
            self.time_limit = self.RACE_TIMEOUT_SECONDS

        # Hidden tests are opt-in per problem: if no case carries a "sample"
        # key, every case is a sample and the pre-existing problems behave
        # exactly as before.
        cases = problem.get("testCases", []) if problem else []
        self._uses_hidden_tests = any("sample" in tc for tc in cases)

    # ── Messaging ──────────────────────────────────────────────

    async def broadcast(self, msg):
        """Send a JSON message to both players in this room."""
        data = json.dumps(msg)
        for p in self.players:
            ws = p.get("ws")
            if ws is None:
                continue
            try:
                await ws.send_str(data)
            except Exception:
                pass

    async def send_to(self, player, msg):
        """Send a JSON message to a single player."""
        ws = player.get("ws")
        if ws is None:
            return
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            pass

    def get_opponent(self, ws):
        """Return the opponent player dict for a given WebSocket."""
        for p in self.players:
            if p["ws"] is not ws:
                return p
        return None

    def get_player(self, ws):
        """Return the player dict for a given WebSocket."""
        for p in self.players:
            if p["ws"] is ws:
                return p
        return None

    def get_player_by_token(self, token):
        """Return the player dict for a given session token."""
        for p in self.players:
            if p["token"] == token:
                return p
        return None

    def get_opponent_by_token(self, token):
        """Return the opponent player dict for a given session token."""
        for p in self.players:
            if p["token"] != token:
                return p
        return None

    # ── Race Lifecycle ─────────────────────────────────────────

    async def start_countdown(self):
        """Run the pre-race countdown (5 → 1), then start the race."""
        self.state = RoomState.COUNTDOWN
        for i in range(self.COUNTDOWN_SECONDS, 0, -1):
            await self.broadcast({"type": "countdown", "secondsLeft": i})
            await asyncio.sleep(1)
        await self.start_race()

    def _is_sample(self, index):
        """Whether test case `index` may be shown to the players."""
        if not self._uses_hidden_tests:
            return True
        cases = self.problem.get("testCases", [])
        return index < len(cases) and bool(cases[index].get("sample"))

    def _sample_test_cases(self):
        """The test cases the players are allowed to see."""
        cases = self.problem.get("testCases", [])
        return [tc for i, tc in enumerate(cases) if self._is_sample(i)]

    def _public_results(self, results):
        """Strip hidden test cases down to what is safe to send.

        A player retrying against hidden tests would otherwise be able to
        reconstruct the entire hidden suite from the feedback, so hidden
        cases report only whether they passed — never their input or the
        expected output.
        """
        public = []
        for i, r in enumerate(results):
            if self._is_sample(i):
                public.append({**r, "index": i + 1, "hidden": False})
            else:
                public.append({
                    "index": i + 1,
                    "hidden": True,
                    "passed": r["passed"],
                    "timed_out": r["timed_out"],
                    "wall_time_ms": r["wall_time_ms"],
                    # Keep crash text (it is the player's own stderr) but
                    # never the case's input or expected output.
                    "error": r["error"],
                })
        return public

    def _race_field_whitelist(self):
        """The problem fields sent to clients — shared by raceStart and
        resumeState so a new field only needs to be added here (per
        CLAUDE.md: adding a problem field means touching problems.py
        validation and this whitelist, or it silently vanishes).

        `files` is the normalized starter bundle for every language
        (Phase 5) — legacy single-file problems normalize into a
        one-element bundle via normalize_bundle, so the client has one
        shape to render regardless of whether the problem declares real
        `files` or just `starterCode`. The hidden harness (`testFiles`)
        never appears here — only `files` is client-visible, by design.
        """
        return {
            "id": self.problem["id"],
            "title": self.problem["title"],
            "description": self.problem["description"],
            "files": {
                lang: normalize_bundle(self.problem, lang)
                for lang in ("python", "javascript")
            },
            "testCases": self._sample_test_cases(),
            "totalTests": len(self.problem.get("testCases", [])),
            "timeLimitSeconds": self.time_limit,
            "kind": self.problem.get("kind", "algorithmic"),
        }

    async def start_race(self):
        """Broadcast the problem and begin the race timer."""
        self.state = RoomState.RACING
        self.race_start_time = time.time()
        await self.broadcast({
            "type": "raceStart",
            "problem": self._race_field_whitelist(),
        })
        # Start the wall-clock timeout
        self._timeout_task = asyncio.create_task(self._race_timeout())

    async def _race_timeout(self):
        """Auto-resolve once the problem's time limit expires."""
        await asyncio.sleep(self.time_limit)
        if self.state != RoomState.RACING:
            return

        # Notify players that time is up
        await self.broadcast({"type": "timeout"})

        # An attempt submitted just before the buzzer still deserves its
        # verdict — resolving straight away would cancel judging and throw
        # away a submission that may well have been correct.
        await self._drain_judging(self.JUDGE_DRAIN_SECONDS)

        # Draining can itself end the race, if the attempt passed.
        if self.state == RoomState.RACING:
            await self.resolve()

    async def _drain_judging(self, timeout):
        """Give in-flight judging a bounded chance to finish."""
        tasks = [t for t in self.judge_tasks.values() if not t.done()]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            pass  # resolve() cancels whatever is still running

    # ── Submission ─────────────────────────────────────────────

    def _assemble_bundle(self, language, submitted_files):
        """Build the judge-ready bundle for one attempt.

            judge bundle = [hidden harness from testFiles]   ← never sent to the client
                         + [locked files]                    ← rebuilt server-side
                         + [player's writable files]         ← the only thing from the wire

        Returns `(files, entrypoint)` on success, or `None` if
        `submitted_files` names a path the problem doesn't declare at all
        (not even a locked one) — a broken or hostile client, since the
        harness path in particular is never supposed to be client-visible.
        The caller rejects the whole attempt rather than judging it in
        that case. A path that *is* declared but locked is accepted and
        simply ignored (readOnly is a UX constraint, not a security
        boundary — the server rebuilds locked files from the problem
        regardless of what a client sends for them).
        """
        starter = normalize_bundle(self.problem, language)
        declared_paths = {f["path"] for f in starter["files"]}

        for path in submitted_files:
            if path not in declared_paths:
                return None

        files = []
        for f in starter["files"]:
            content = submitted_files.get(f["path"], f["content"]) if f["writable"] else f["content"]
            files.append({"path": f["path"], "content": content})

        test_files = (self.problem.get("testFiles") or {}).get(language)
        if test_files:
            # The harness is prepended and becomes the entrypoint — it is
            # what imports the player's module(s), never sent to the client.
            files = [{"path": tf["path"], "content": tf["content"]} for tf in test_files] + files
            entrypoint = test_files[0]["path"]
        else:
            entrypoint = starter["entrypoint"]

        return files, entrypoint

    async def handle_submit(self, ws, files, language):
        """Accept one attempt and judge it in the background.

        Returns as soon as the attempt is queued. Judging takes seconds and
        this runs inside the player's socket read loop, so blocking here
        would stall everything else that player sends — the same bug fixed
        for agent calls in f245bb7.

        `files` is a {path: content} map of the player's writable files —
        main.py wraps a legacy bare `code` string into a one-entry map
        before this is ever called, so this always sees the new shape.

        An attempt that fails any test is rejected and the player may fix it
        and submit again; the first attempt to pass every test wins the race.
        """
        if self.state != RoomState.RACING:
            # Reject submissions outside the racing state
            await self.send_to(
                self.get_player(ws) or {"ws": ws},
                {"type": "error", "message": "Submissions only accepted during race."},
            )
            return

        player = self.get_player(ws)
        if not player:
            return
        token = player["token"]

        # With no judge there is no verdict to retry against, so keep the
        # Phase 1 one-shot, first-to-submit behaviour.
        if not self.judge:
            await self._handle_submit_timestamp_only(player, files, language)
            return

        in_flight = self.judge_tasks.get(token)
        if in_flight and not in_flight.done():
            await self.send_to(
                player,
                {"type": "error", "message": "Your last submission is still being judged."},
            )
            return

        # Each attempt costs a full sandbox run per test case, for both
        # players — a tight retry loop would otherwise flood Piston.
        waited = time.monotonic() - self.last_submit_at.get(token, float("-inf"))
        if waited < self.RESUBMIT_COOLDOWN_SECONDS:
            remaining = max(1, int(round(self.RESUBMIT_COOLDOWN_SECONDS - waited)))
            await self.send_to(player, {
                "type": "error",
                "message": f"Wait {remaining}s before resubmitting.",
            })
            return

        if not isinstance(files, dict):
            files = {}
        total_bytes = sum(len(k) + len(v) for k, v in files.items() if isinstance(v, str))
        if total_bytes > self.MAX_TREE_BYTES:
            await self.send_to(player, {"type": "error", "message": "Submission too large."})
            return

        assembled = self._assemble_bundle(language, files)
        if assembled is None:
            await self.send_to(player, {
                "type": "error",
                "message": "Submission references a file this problem doesn't allow editing.",
            })
            return
        bundle_files, entrypoint = assembled

        self.last_submit_at[token] = time.monotonic()

        attempt = {
            "files": bundle_files,
            "entrypoint": entrypoint,
            "language": language,
            "timestamp": time.time(),
            "verdict": None,
        }
        self.submissions.setdefault(token, []).append(attempt)

        await self.send_to(player, {
            "type": "judging",
            "attempt": len(self.submissions[token]),
        })
        opponent = self.get_opponent(ws)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "submitted"}
            )

        self.judge_tasks[token] = asyncio.create_task(
            self._run_judging(player, attempt)
        )

    async def _handle_submit_timestamp_only(self, player, files, language):
        """Phase 1 fallback used when no judge is configured. Nothing here
        ever executes the submission, so no bundle assembly is needed —
        this is just evidence that the player submitted at all."""
        token = player["token"]
        if self.submissions.get(token):
            await self.send_to(
                player,
                {"type": "error", "message": "You have already submitted."},
            )
            return

        self.submissions[token] = [{
            "files": files,
            "language": language,
            "timestamp": time.time(),
            "verdict": None,
        }]

        await self.send_to(player, {"type": "submitted"})

        opponent = self.get_opponent(player["ws"])
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "submitted"}
            )

        if sum(1 for a in self.submissions.values() if a) >= 2:
            await self.resolve()

    async def _run_judging(self, player, attempt):
        """Background worker: judge one attempt and act on its verdict."""
        token = player["token"]
        name = player["name"]
        test_cases = self.problem.get("testCases", [])

        try:
            verdict = await self.judge.evaluate(
                {"files": attempt["files"], "entrypoint": attempt["entrypoint"]},
                attempt["language"],
                test_cases,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # If judging fails entirely, treat the attempt as all-fail rather
            # than dropping the player out of the race.
            import traceback
            print(f"[Room {self.room_id}] ⚠ Judge error for {name}: {e}")
            traceback.print_exc()
            verdict = {
                "passed": False,
                "pass_count": 0,
                "total": len(test_cases),
                "results": [],
                "import_error": None,
            }

        attempt["verdict"] = verdict
        print(f"[Room {self.room_id}] {name} attempt "
              f"{len(self.submissions.get(token, []))}: "
              f"{verdict['pass_count']}/{verdict['total']}")

        # The race may have ended (timeout, opponent won, disconnect) while
        # this was running.
        if self.state != RoomState.RACING:
            return

        if verdict["passed"]:
            await self._accept(player, attempt)
            return

        await self.send_to(player, {
            "type": "submissionResult",
            "accepted": False,
            "passCount": verdict["pass_count"],
            "totalTests": verdict["total"],
            "attempt": len(self.submissions.get(token, [])),
            "results": self._public_results(verdict["results"]),
            "importError": verdict.get("import_error"),
        })

        opponent = self.get_opponent_by_token(token)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "attempted"}
            )

        # This attempt was submitted before the opponent's, so an accepted
        # opponent attempt may have been waiting on this verdict.
        await self._maybe_finish()

    async def _accept(self, player, attempt):
        """Record an attempt that passed every test, and maybe end the race."""
        if self.state != RoomState.RACING:
            return

        self.accepted[player["token"]] = attempt

        await self.send_to(player, {
            "type": "submissionResult",
            "accepted": True,
            "passCount": attempt["verdict"]["pass_count"],
            "totalTests": attempt["verdict"]["total"],
            "attempt": len(self.submissions.get(player["token"], [])),
            "results": self._public_results(attempt["verdict"]["results"]),
        })

        await self._maybe_finish()

    async def _maybe_finish(self):
        """End the race once the earliest accepted attempt is settled.

        Test cases run concurrently and attempts differ in cost, so the
        verdict that lands first is not necessarily the attempt that was
        *submitted* first. Rather than have one judging task await another —
        which deadlocks, since resolving cancels the opponent's task — a
        passing attempt is just recorded, and the race ends only once no
        earlier-submitted attempt is still being judged. Every judging task
        calls this when it finishes, so whichever settles last does the
        resolving.
        """
        if self.state != RoomState.RACING or not self.accepted:
            return

        winner_token, best = min(
            self.accepted.items(), key=lambda kv: kv[1]["timestamp"]
        )

        for p in self.players:
            token = p["token"]
            if token == winner_token:
                continue
            pending = self._latest_attempt(token)
            if pending is None or pending["timestamp"] >= best["timestamp"]:
                continue
            if pending["verdict"] is not None:
                continue  # already decided, and it did not win
            task = self.judge_tasks.get(token)
            # `task is current_task()` means we are that judging task calling
            # in after recording our own verdict — settled, not in flight.
            if (task and not task.done()
                    and task is not asyncio.current_task()):
                # That attempt was sent first and could still beat this one.
                return

        winner_player = self.get_player_by_token(winner_token)
        self.winner_name = winner_player["name"] if winner_player else None
        await self.resolve()

    def _latest_attempt(self, token):
        """The most recent attempt from a player, if any."""
        attempts = self.submissions.get(token)
        return attempts[-1] if attempts else None

    def _best_attempt(self, token):
        """The attempt that stands as this player's result.

        A passing attempt always wins; otherwise the highest pass count, with
        the earliest submission breaking ties.
        """
        attempts = [
            a for a in self.submissions.get(token, []) if a["verdict"] is not None
        ]
        if not attempts:
            return None
        return max(
            attempts,
            key=lambda a: (
                a["verdict"]["passed"],
                a["verdict"]["pass_count"],
                -a["timestamp"],
            ),
        )

    # ── Working-tree sync (Phase 4) ────────────────────────────
    # The client is the sole writer; the server just stores whatever the
    # latest-rev sync sent, as a recovery baseline for a reconnecting client.

    async def handle_sync_tree(self, ws, rev, files):
        """Accept a newer copy of the player's working buffer.

        Stored directly on the player's session dict, which Lobby.sessions
        and Room.players both reference — no separate sync path needed.
        Silently ignores a stale or malformed sync rather than erroring,
        since this is a best-effort background save, not a request the
        client is waiting on.
        """
        player = self.get_player(ws)
        if not player:
            return
        try:
            rev = int(rev)
        except (TypeError, ValueError):
            return
        if rev <= player.get("rev", 0):
            return
        if not isinstance(files, dict):
            return
        try:
            total_bytes = sum(len(k) + len(v) for k, v in files.items())
        except TypeError:
            return
        if total_bytes > self.MAX_TREE_BYTES:
            return
        player["tree"] = files
        player["rev"] = rev

    # ── Resume (Phase 4) ────────────────────────────────────────

    def _attempt_summary(self, attempt, index):
        """One submissions[token] entry, shaped like submissionResult but
        safe to replay (hidden test cases stay stripped)."""
        verdict = attempt["verdict"]
        if verdict is None:
            return {"attempt": index + 1, "judged": False}
        return {
            "attempt": index + 1,
            "judged": True,
            "accepted": verdict["passed"],
            "passCount": verdict["pass_count"],
            "totalTests": verdict["total"],
            "results": self._public_results(verdict["results"]),
            "importError": verdict.get("import_error"),
        }

    def build_resume_payload(self, token):
        """What to send a reconnecting client, based on current room state.

        A distinct payload per phase: a finished/resolving room just gets
        its result replayed (nothing left to resume into); a still-racing
        room gets the full resumeState (problem, remaining time, attempt
        history, agent transcript, working tree); anything else (countdown)
        gets a light phase marker — the client just waits for the next
        broadcast, which will now reach it since its ws is reattached.
        """
        player = self.get_player_by_token(token)
        if not player:
            return None

        if self.state == RoomState.FINISHED:
            return self._last_result or {
                "type": "result", "winner": None, "submissions": []
            }

        if self.state != RoomState.RACING:
            return {"type": "resumeState", "phase": self.state}

        opponent = self.get_opponent_by_token(token)
        elapsed = (time.time() - self.race_start_time) if self.race_start_time else 0
        remaining = max(0, self.time_limit - elapsed)

        attempts = self.submissions.get(token, [])

        return {
            "type": "resumeState",
            "phase": "racing",
            "problem": self._race_field_whitelist(),
            "remainingSeconds": remaining,
            "attempts": [self._attempt_summary(a, i) for i, a in enumerate(attempts)],
            "agentTranscript": self.agent_sessions.get(token, []),
            "opponentName": opponent["name"] if opponent else None,
            "opponentConnected": bool(opponent and opponent.get("ws")),
            "tree": player.get("tree") or {},
            "rev": player.get("rev", 0),
        }

    # ── Agent prompting ────────────────────────────────────────
    # The agent is a copilot the player directs — it never submits on its
    # own. A prompt only ever returns code to the requesting player; the
    # player still has to click Submit themselves for the race to resolve.

    def _agent_system_prompt(self, language):
        """The instruction + problem text shared by both prompt shapes."""
        return (
            "You are pair-programming with a player racing to solve this "
            f"problem. Respond with the complete updated solution in a "
            f"single {language} code block.\n\n"
            f"Problem: {self.problem['title']}\n{self.problem['description']}"
        )

    def _agent_code_context(self, language, current_code):
        """Fenced current-editor (or starter) code, or '' if there is none."""
        starter = self.problem.get("starterCode", {}).get(language, "")
        if current_code:
            return f"Player's current code:\n```{language}\n{current_code}\n```"
        if starter:
            return f"Starter code:\n```{language}\n{starter}\n```"
        return ""

    def _build_agent_context(self, language, current_code, history, instruction):
        """Assemble the full flat prompt sent to the agent for one turn.

        Used for adapters that own their own system-prompt handling (the
        local CLI adapter) rather than a real messages array.
        """
        parts = [self._agent_system_prompt(language)]

        code_context = self._agent_code_context(language, current_code)
        if code_context:
            parts.append(code_context)

        for turn in history:
            speaker = "Player" if turn["role"] == "user" else "Agent"
            parts.append(f"{speaker}: {turn['content']}")

        parts.append(f"Player: {instruction}")
        return "\n\n".join(parts)

    def _build_agent_messages(self, language, current_code, history, instruction):
        """Assemble a structured system + messages payload for adapters that
        support a real multi-turn conversation (the two HTTP adapters).

        Returns (system, messages). History turns are stored in pairs (a
        successful turn always appends both a "user" and an "agent" entry
        together — see _run_agent_prompt), so messages already alternates
        correctly and ends on an "assistant" turn before this appends the
        final user message.

        The stored session (agent_sessions) is already FIFO-capped at
        AGENT_HISTORY_LIMIT turns, but a long race can still accumulate more
        history than a single call should carry — trim_messages applies a
        token-aware cut on top of that, keeping the most recent turns and
        always starting on a "human" turn so the alternation stays valid.
        """
        system = self._agent_system_prompt(language)

        lc_history = [
            HumanMessage(turn["content"]) if turn["role"] == "user" else AIMessage(turn["content"])
            for turn in history
        ]
        lc_history = trim_messages(
            lc_history,
            max_tokens=self.AGENT_HISTORY_TOKEN_BUDGET,
            token_counter=count_tokens_approximately,
            strategy="last",
            start_on="human",
        )
        messages = [
            {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": m.content}
            for m in lc_history
        ]

        # Current code rides on the final message, not the system block,
        # since the player may have hand-edited since the previous turn.
        final_parts = []
        code_context = self._agent_code_context(language, current_code)
        if code_context:
            final_parts.append(code_context)
        final_parts.append(instruction)
        messages.append({"role": "user", "content": "\n\n".join(final_parts)})

        return system, messages

    async def handle_agent_prompt(self, ws, agent_type, config, instruction, language, current_code):
        """Dispatch one agent turn for a player as a background task.

        Returns immediately — the caller (the socket read loop) must never
        block on an agent call, or the player can't Submit while their agent
        is still thinking and their submission timestamp gets stamped late.
        Never resolves the race on its own; that still requires an explicit
        submit.
        """
        if self.state != RoomState.RACING:
            await self.send_to(
                self.get_player(ws) or {"ws": ws},
                {"type": "agentStatus", "status": "error", "message": "Agent prompts only accepted during race."},
            )
            return

        player = self.get_player(ws)
        if not player:
            return
        token = player["token"]

        existing = self.agent_tasks.get(token)
        if existing and not existing.done():
            await self.send_to(
                player,
                {"type": "agentStatus", "status": "error", "message": "Agent is still working on your last instruction."},
            )
            return

        self.agent_tasks[token] = asyncio.create_task(
            self._run_agent_prompt(player, agent_type, config, instruction, language, current_code)
        )

    async def _run_agent_prompt(self, player, agent_type, config, instruction, language, current_code):
        """Background worker for one agent turn — the actual agent call."""
        token = player["token"]
        history = self.agent_sessions.setdefault(token, [])

        opponent = self.get_opponent_by_token(token)
        if opponent:
            await self.send_to(opponent, {"type": "opponentStatus", "status": "agent-thinking"})

        try:
            agent = build_agent(agent_type, {**config, "language": language})
            timeout = getattr(agent, "timeout_seconds", self.AGENT_TIMEOUT_SECONDS)

            if getattr(agent, "supports_conversation", False):
                system, messages = self._build_agent_messages(
                    language, current_code, history, instruction
                )
                result = await asyncio.wait_for(
                    agent.run_conversation(system, messages), timeout=timeout
                )
            else:
                context = self._build_agent_context(language, current_code, history, instruction)
                result = await asyncio.wait_for(agent.run(context), timeout=timeout)
        except asyncio.TimeoutError:
            await self.send_to(player, {"type": "agentStatus", "status": "error", "message": "Agent timed out."})
        except Exception as e:
            await self.send_to(player, {"type": "agentStatus", "status": "error", "message": str(e)})
        else:
            log = result.get("log", "")
            has_code = result.get("hasCode", True)
            # Store the full raw reply, not just the extracted code — that's
            # what lets a follow-up like "why did you use a dict there?"
            # refer back to what the agent actually said, and it's what a
            # flat-prompt adapter replays with its original fences intact
            # rather than as bare unlabeled code.
            history.append({"role": "user", "content": instruction})
            history.append({
                "role": "agent",
                "content": log[: self.AGENT_TURN_CHAR_LIMIT],
                "code": result["code"],
                "hasCode": has_code,
            })
            del history[: -self.AGENT_HISTORY_LIMIT]
            await self.send_to(
                player,
                {
                    "type": "agentResponse",
                    "code": result["code"],
                    "log": log,
                    "hasCode": has_code,
                },
            )

        if opponent:
            status = "using-agent" if self.agent_sessions.get(token) else "writing"
            await self.send_to(opponent, {"type": "opponentStatus", "status": status})

    async def _cancel_agent_task(self, token):
        """Cancel one player's in-flight agent task, if any, and await its
        teardown so the adapter's process-kill `finally` actually runs
        before we move on (room exit, disconnect, etc.)."""
        task = self.agent_tasks.get(token)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _cancel_all_agent_tasks(self):
        for token in list(self.agent_tasks.keys()):
            await self._cancel_agent_task(token)

    async def _cancel_judge_task(self, token):
        """Cancel one player's in-flight judging, if any.

        Skips the caller's own task: `resolve()` is reached from inside a
        judging task whenever an attempt is accepted, and a task that
        cancelled and then awaited itself would deadlock.
        """
        task = self.judge_tasks.get(token)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _cancel_all_judge_tasks(self):
        for token in list(self.judge_tasks.keys()):
            await self._cancel_judge_task(token)

    async def _cancel_grace_task(self, token):
        task = self.grace_tasks.get(token)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _cancel_all_grace_tasks(self):
        for token in list(self.grace_tasks.keys()):
            await self._cancel_grace_task(token)

    # ── Resolution ─────────────────────────────────────────────

    async def resolve(self):
        """Determine the winner and broadcast the result.

        Phase 2 judging rules:
        1. Both PASS  → winner = earlier submissionTimestamp
        2. One PASSES → winner = the one that passed
        3. Both FAIL  → winner = higher passCount; if tied → TIE
        4. Neither submitted → TIE
        """
        if self.state == RoomState.FINISHED:
            return
        self.state = RoomState.RESOLVING
        print(f"[Room {self.room_id}] Resolving race. Submissions: {list(self.submissions.keys())}")
        print(f"[Room {self.room_id}] Judge available: {self.judge is not None}")

        # Cancel the timeout if still running
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

        # A race that ends mid-agent-call must not leave an orphaned `claude`
        # (or other adapter) process running against the host's quota.
        await self._cancel_all_agent_tasks()
        # Same for in-flight judging: a verdict for a race that is already
        # over is wasted sandbox work.
        await self._cancel_all_judge_tasks()
        # And any pending disconnect-grace timers — the race is over, so
        # there is nothing left for them to concede.
        await self._cancel_all_grace_tasks()

        # Build submission summaries from verdicts already computed at submit
        # time — nothing is judged twice.
        if self.judge:
            submissions_list = self._resolve_with_judge()
        else:
            submissions_list = self._resolve_timestamp_only()

        # Determine winner
        winner = self._determine_winner(submissions_list)

        self.state = RoomState.FINISHED
        result_msg = {
            "type": "result",
            "winner": winner,
            "submissions": submissions_list,
        }
        self._last_result = result_msg
        await self.broadcast(result_msg)

        if self.on_finished:
            self.on_finished()

    def _resolve_with_judge(self):
        """Summarise each player from the verdicts recorded at submit time."""
        total = len(self.problem.get("testCases", []))
        submissions_list = []

        for p in self.players:
            token = p["token"]
            name = p["name"]
            attempts = self.submissions.get(token, [])
            best = self._best_attempt(token)

            if best is None:
                submissions_list.append({
                    "player": name,
                    "submitted": False,
                    "timeMs": None,
                    "passed": False,
                    "passCount": 0,
                    "totalTests": total,
                    "attempts": len(attempts),
                    "results": [],
                })
                continue

            verdict = best["verdict"]
            submissions_list.append({
                "player": name,
                "submitted": True,
                "timeMs": int((best["timestamp"] - self.race_start_time) * 1000),
                "passed": verdict["passed"],
                "passCount": verdict["pass_count"],
                "totalTests": verdict["total"],
                "attempts": len(attempts),
                "results": self._public_results(verdict["results"]),
            })

        return submissions_list

    def _resolve_timestamp_only(self):
        """Fallback: Phase 1 timestamp-only resolution (no judge available)."""
        submissions_list = []
        for p in self.players:
            token = p["token"]
            name = p["name"]
            attempts = self.submissions.get(token) or []
            if attempts:
                sub = attempts[0]
                elapsed_ms = int((sub["timestamp"] - self.race_start_time) * 1000)
                submissions_list.append({
                    "player": name,
                    "submitted": True,
                    "timeMs": elapsed_ms,
                    "passed": None,
                    "passCount": None,
                    "totalTests": None,
                    "attempts": len(attempts),
                    "results": [],
                })
            else:
                submissions_list.append({
                    "player": name,
                    "submitted": False,
                    "timeMs": None,
                    "passed": None,
                    "passCount": None,
                    "totalTests": None,
                    "attempts": 0,
                    "results": [],
                })
        return submissions_list

    def _determine_winner(self, submissions_list):
        """Apply judging rules to determine the winner.

        A solution is only ever *accepted* by passing every test case, and
        the first player to do so ends the race immediately — that is
        recorded in `winner_name` and short-circuits everything below.

        The remaining rules therefore only decide races that ran out of time
        with nobody fully correct, and they preserve the original ordering:
        1. Both PASS  → winner = earlier timestamp
        2. One PASSES → winner = the one that passed
        3. Both FAIL  → higher passCount wins; if tied → TIE
        4. Neither submitted → TIE
        """
        if self.winner_name:
            return self.winner_name

        submitted = [s for s in submissions_list if s["submitted"]]

        if len(submitted) == 0:
            return None  # TIE — neither submitted

        if len(submitted) == 1:
            return submitted[0]["player"]  # Only one submitted

        # Both submitted — check if we have judge results
        s1, s2 = submitted[0], submitted[1]

        # If no judge (Phase 1 fallback), use timestamp only
        if s1["passed"] is None:
            return min(submitted, key=lambda s: s["timeMs"])["player"]

        # Phase 2: correctness-based judging
        both_pass = s1["passed"] and s2["passed"]
        one_pass = s1["passed"] or s2["passed"]

        if both_pass:
            # Both pass → earliest timestamp wins
            return min(submitted, key=lambda s: s["timeMs"])["player"]
        elif one_pass:
            # One passes → they win regardless of time
            return s1["player"] if s1["passed"] else s2["player"]
        else:
            # Both fail → higher passCount wins; if tied → TIE
            if s1["passCount"] > s2["passCount"]:
                return s1["player"]
            elif s2["passCount"] > s1["passCount"]:
                return s2["player"]
            else:
                return None  # TIE

    # ── Disconnect ─────────────────────────────────────────────

    async def handle_disconnect(self, token):
        """Handle a player's socket closing mid-race.

        A dropped connection is no longer an instant forfeit: the agent
        task is cancelled (its result can't be delivered and it keeps
        burning the player's API budget), but in-flight judging is left to
        finish — the race is decided on submission time, not judging
        latency (see `_accept`'s strict `<` timestamp comparison), so a
        player who submitted a winning answer and then dropped should still
        win. A player who had not attempted yet gets a grace window before
        conceding on their behalf.
        """
        player = self.get_player_by_token(token)
        if not player:
            return

        await self._cancel_agent_task(token)

        if self.state != RoomState.RACING:
            return

        opponent = self.get_opponent_by_token(token)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "disconnected"}
            )

        if not self.submissions.get(token):
            self._start_grace_timer(token)

    def _start_grace_timer(self, token):
        existing = self.grace_tasks.get(token)
        if existing and not existing.done():
            return  # already counting down from an earlier drop
        self.grace_tasks[token] = asyncio.create_task(self._grace_timeout(token))

    async def _grace_timeout(self, token):
        """Concede on behalf of a player who never reconnected and never
        attempted. The race clock itself is untouched by any of this — it
        is an independent task started in start_race, so a dropped
        connection can never pause it."""
        await asyncio.sleep(self.DISCONNECT_GRACE_SECONDS)
        if self.state != RoomState.RACING:
            return
        player = self.get_player_by_token(token)
        if not player or player.get("ws") is not None:
            return  # reconnected during the grace window
        if self.submissions.get(token):
            return  # submitted while we were sleeping
        await self.resolve()
