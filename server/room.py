"""Room state machine — manages a single two-player race.

States: COUNTDOWN → RACING → RESOLVING → FINISHED

Phase 2 judging: code is executed against test cases via sandbox.
Winner is determined by correctness first, then timestamp as tiebreaker.
"""

import asyncio
import json
import time

from server.agents.registry import build_agent


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
    RESUBMIT_COOLDOWN_SECONDS = 3  # floor between one player's attempts
    JUDGE_DRAIN_SECONDS = 15  # grace for judging an attempt made before time ran out

    def __init__(self, room_id, player1, player2, problem, judge=None):
        """Create a new room.

        Args:
            room_id: Short unique identifier.
            player1: dict with keys "ws" (WebSocketResponse) and "name" (str).
            player2: dict with keys "ws" (WebSocketResponse) and "name" (str).
            problem: Problem dict from the problem bank.
            judge: Judge instance for code evaluation (Phase 2+).
        """
        self.room_id = room_id
        self.players = [player1, player2]
        self.problem = problem
        self.judge = judge
        self.state = RoomState.COUNTDOWN
        # player_name → [{ code, language, timestamp, verdict }, ...] — one
        # entry per attempt, since a rejected submission can be retried.
        self.submissions = {}
        self.agent_sessions = {}  # player_name → [{ role, content }, ...]
        self.agent_tasks = {}  # player_name → asyncio.Task running _run_agent_prompt
        self.judge_tasks = {}  # player_name → asyncio.Task running _run_judging
        self.last_submit_at = {}  # player_name → monotonic time of last attempt
        self.accepted = {}  # player_name → the attempt that passed every test
        self.winner_name = None  # set as soon as an accepted attempt is settled
        self.race_start_time = None
        self._timeout_task = None
        self._countdown_task = None

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
            try:
                await p["ws"].send_str(data)
            except Exception:
                pass

    async def send_to(self, player, msg):
        """Send a JSON message to a single player."""
        try:
            await player["ws"].send_str(json.dumps(msg))
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

    async def start_race(self):
        """Broadcast the problem and begin the race timer."""
        self.state = RoomState.RACING
        self.race_start_time = time.time()
        await self.broadcast({
            "type": "raceStart",
            "problem": {
                "id": self.problem["id"],
                "title": self.problem["title"],
                "description": self.problem["description"],
                "starterCode": self.problem.get("starterCode", {}),
                "testCases": self._sample_test_cases(),
                "totalTests": len(self.problem.get("testCases", [])),
                "timeLimitSeconds": self.time_limit,
                "kind": self.problem.get("kind", "algorithmic"),
            },
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

    async def handle_submit(self, ws, code, language):
        """Accept one attempt and judge it in the background.

        Returns as soon as the attempt is queued. Judging takes seconds and
        this runs inside the player's socket read loop, so blocking here
        would stall everything else that player sends — the same bug fixed
        for agent calls in f245bb7.

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
        name = player["name"]

        # With no judge there is no verdict to retry against, so keep the
        # Phase 1 one-shot, first-to-submit behaviour.
        if not self.judge:
            await self._handle_submit_timestamp_only(player, code, language)
            return

        in_flight = self.judge_tasks.get(name)
        if in_flight and not in_flight.done():
            await self.send_to(
                player,
                {"type": "error", "message": "Your last submission is still being judged."},
            )
            return

        # Each attempt costs a full sandbox run per test case, for both
        # players — a tight retry loop would otherwise flood Piston.
        waited = time.monotonic() - self.last_submit_at.get(name, float("-inf"))
        if waited < self.RESUBMIT_COOLDOWN_SECONDS:
            remaining = max(1, int(round(self.RESUBMIT_COOLDOWN_SECONDS - waited)))
            await self.send_to(player, {
                "type": "error",
                "message": f"Wait {remaining}s before resubmitting.",
            })
            return
        self.last_submit_at[name] = time.monotonic()

        attempt = {
            "code": code,
            "language": language,
            "timestamp": time.time(),
            "verdict": None,
        }
        self.submissions.setdefault(name, []).append(attempt)

        await self.send_to(player, {
            "type": "judging",
            "attempt": len(self.submissions[name]),
        })
        opponent = self.get_opponent(ws)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "submitted"}
            )

        self.judge_tasks[name] = asyncio.create_task(
            self._run_judging(player, attempt)
        )

    async def _handle_submit_timestamp_only(self, player, code, language):
        """Phase 1 fallback used when no judge is configured."""
        name = player["name"]
        if self.submissions.get(name):
            await self.send_to(
                player,
                {"type": "error", "message": "You have already submitted."},
            )
            return

        self.submissions[name] = [{
            "code": code,
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
        name = player["name"]
        test_cases = self.problem.get("testCases", [])

        try:
            verdict = await self.judge.evaluate(
                attempt["code"], attempt["language"], test_cases
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
            }

        attempt["verdict"] = verdict
        print(f"[Room {self.room_id}] {name} attempt "
              f"{len(self.submissions.get(name, []))}: "
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
            "attempt": len(self.submissions.get(name, [])),
            "results": self._public_results(verdict["results"]),
        })

        opponent = self.get_opponent(player["ws"])
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

        self.accepted[player["name"]] = attempt

        await self.send_to(player, {
            "type": "submissionResult",
            "accepted": True,
            "passCount": attempt["verdict"]["pass_count"],
            "totalTests": attempt["verdict"]["total"],
            "attempt": len(self.submissions.get(player["name"], [])),
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

        winner, best = min(
            self.accepted.items(), key=lambda kv: kv[1]["timestamp"]
        )

        for p in self.players:
            name = p["name"]
            if name == winner:
                continue
            pending = self._latest_attempt(name)
            if pending is None or pending["timestamp"] >= best["timestamp"]:
                continue
            if pending["verdict"] is not None:
                continue  # already decided, and it did not win
            task = self.judge_tasks.get(name)
            # `task is current_task()` means we are that judging task calling
            # in after recording our own verdict — settled, not in flight.
            if (task and not task.done()
                    and task is not asyncio.current_task()):
                # That attempt was sent first and could still beat this one.
                return

        self.winner_name = winner
        await self.resolve()

    def _latest_attempt(self, name):
        """The most recent attempt from a player, if any."""
        attempts = self.submissions.get(name)
        return attempts[-1] if attempts else None

    def _best_attempt(self, name):
        """The attempt that stands as this player's result.

        A passing attempt always wins; otherwise the highest pass count, with
        the earliest submission breaking ties.
        """
        attempts = [
            a for a in self.submissions.get(name, []) if a["verdict"] is not None
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

    # ── Agent prompting ────────────────────────────────────────
    # The agent is a copilot the player directs — it never submits on its
    # own. A prompt only ever returns code to the requesting player; the
    # player still has to click Submit themselves for the race to resolve.

    def _build_agent_context(self, language, current_code, history, instruction):
        """Assemble the full prompt sent to the agent for one turn."""
        parts = [
            "You are pair-programming with a player racing to solve this "
            f"problem. Respond with the complete updated solution in a "
            f"single {language} code block.",
            f"Problem: {self.problem['title']}\n{self.problem['description']}",
        ]

        starter = self.problem.get("starterCode", {}).get(language, "")
        if current_code:
            parts.append(f"Player's current code:\n```{language}\n{current_code}\n```")
        elif starter:
            parts.append(f"Starter code:\n```{language}\n{starter}\n```")

        for turn in history:
            speaker = "Player" if turn["role"] == "user" else "Agent"
            parts.append(f"{speaker}: {turn['content']}")

        parts.append(f"Player: {instruction}")
        return "\n\n".join(parts)

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
        name = player["name"]

        existing = self.agent_tasks.get(name)
        if existing and not existing.done():
            await self.send_to(
                player,
                {"type": "agentStatus", "status": "error", "message": "Agent is still working on your last instruction."},
            )
            return

        self.agent_tasks[name] = asyncio.create_task(
            self._run_agent_prompt(player, agent_type, config, instruction, language, current_code)
        )

    async def _run_agent_prompt(self, player, agent_type, config, instruction, language, current_code):
        """Background worker for one agent turn — the actual agent call."""
        name = player["name"]
        history = self.agent_sessions.setdefault(name, [])
        context = self._build_agent_context(language, current_code, history, instruction)

        opponent = self.get_opponent(player["ws"])
        if opponent:
            await self.send_to(opponent, {"type": "opponentStatus", "status": "agent-thinking"})

        try:
            agent = build_agent(agent_type, {**config, "language": language})
            timeout = getattr(agent, "timeout_seconds", self.AGENT_TIMEOUT_SECONDS)
            result = await asyncio.wait_for(agent.run(context), timeout=timeout)
        except asyncio.TimeoutError:
            await self.send_to(player, {"type": "agentStatus", "status": "error", "message": "Agent timed out."})
        except Exception as e:
            await self.send_to(player, {"type": "agentStatus", "status": "error", "message": str(e)})
        else:
            history.append({"role": "user", "content": instruction})
            history.append({"role": "agent", "content": result["code"]})
            del history[: -self.AGENT_HISTORY_LIMIT]
            await self.send_to(
                player,
                {"type": "agentResponse", "code": result["code"], "log": result.get("log", "")},
            )

        if opponent:
            status = "using-agent" if self.agent_sessions.get(name) else "writing"
            await self.send_to(opponent, {"type": "opponentStatus", "status": status})

    async def _cancel_agent_task(self, name):
        """Cancel one player's in-flight agent task, if any, and await its
        teardown so the adapter's process-kill `finally` actually runs
        before we move on (room exit, disconnect, etc.)."""
        task = self.agent_tasks.get(name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _cancel_all_agent_tasks(self):
        for name in list(self.agent_tasks.keys()):
            await self._cancel_agent_task(name)

    async def _cancel_judge_task(self, name):
        """Cancel one player's in-flight judging, if any.

        Skips the caller's own task: `resolve()` is reached from inside a
        judging task whenever an attempt is accepted, and a task that
        cancelled and then awaited itself would deadlock.
        """
        task = self.judge_tasks.get(name)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _cancel_all_judge_tasks(self):
        for name in list(self.judge_tasks.keys()):
            await self._cancel_judge_task(name)

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

        # Build submission summaries from verdicts already computed at submit
        # time — nothing is judged twice.
        if self.judge:
            submissions_list = self._resolve_with_judge()
        else:
            submissions_list = self._resolve_timestamp_only()

        # Determine winner
        winner = self._determine_winner(submissions_list)

        self.state = RoomState.FINISHED
        await self.broadcast({
            "type": "result",
            "winner": winner,
            "submissions": submissions_list,
        })

    def _resolve_with_judge(self):
        """Summarise each player from the verdicts recorded at submit time."""
        total = len(self.problem.get("testCases", []))
        submissions_list = []

        for p in self.players:
            name = p["name"]
            attempts = self.submissions.get(name, [])
            best = self._best_attempt(name)

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
            name = p["name"]
            attempts = self.submissions.get(name) or []
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

    async def handle_disconnect(self, ws):
        """Handle a player disconnecting mid-race."""
        opponent = self.get_opponent(ws)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "disconnected"}
            )

        # The disconnecting player is gone regardless of whether this ends
        # the race for the opponent too — don't leave their agent running,
        # or burn sandbox runs judging an attempt nobody will see.
        player = self.get_player(ws)
        if player:
            await self._cancel_agent_task(player["name"])
            await self._cancel_judge_task(player["name"])

        # If the race is live and the disconnecting player never attempted,
        # resolve immediately so the remaining player wins.
        if self.state == RoomState.RACING:
            if player and not self.submissions.get(player["name"]):
                await self.resolve()
            else:
                # Cancelling their judging means it will never report back,
                # so an opponent attempt that was waiting on it can settle.
                await self._maybe_finish()
