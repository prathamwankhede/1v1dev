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
    RACE_TIMEOUT_SECONDS = 120  # 2 minutes
    AGENT_TIMEOUT_SECONDS = 60
    AGENT_HISTORY_LIMIT = 20  # messages (10 player/agent turn pairs), FIFO

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
        self.submissions = {}  # player_name → { code, language, timestamp }
        self.agent_sessions = {}  # player_name → [{ role, content }, ...]
        self.agent_tasks = {}  # player_name → asyncio.Task running _run_agent_prompt
        self.race_start_time = None
        self._timeout_task = None
        self._countdown_task = None

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
                "testCases": self.problem.get("testCases", []),
            },
        })
        # Start the wall-clock timeout
        self._timeout_task = asyncio.create_task(self._race_timeout())

    async def _race_timeout(self):
        """Auto-resolve after RACE_TIMEOUT_SECONDS."""
        await asyncio.sleep(self.RACE_TIMEOUT_SECONDS)
        if self.state == RoomState.RACING:
            # Notify players that time is up
            await self.broadcast({"type": "timeout"})
            await self.resolve()

    # ── Submission ─────────────────────────────────────────────

    async def handle_submit(self, ws, code, language):
        """Record a player's submission and possibly resolve the race."""
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

        # Already submitted? Reject duplicate.
        if player["name"] in self.submissions:
            await self.send_to(
                player,
                {"type": "error", "message": "You have already submitted."},
            )
            return

        self.submissions[player["name"]] = {
            "code": code,
            "language": language,
            "timestamp": time.time(),
        }

        # Confirm to the submitter
        await self.send_to(player, {"type": "submitted"})

        # Notify opponent
        opponent = self.get_opponent(ws)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "submitted"}
            )

        # If both players have submitted, resolve immediately
        if len(self.submissions) >= 2:
            await self.resolve()

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

        # Notify players that judging is in progress
        await self.broadcast({"type": "judging"})

        # Build submission summaries and run judge if available
        submissions_list = []

        if self.judge:
            submissions_list = await self._resolve_with_judge()
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

    async def _resolve_with_judge(self):
        """Run sandbox-based judging on all submissions."""
        test_cases = self.problem.get("testCases", [])

        # Build tasks for concurrent evaluation
        async def evaluate_player(player):
            name = player["name"]
            if name not in self.submissions:
                return {
                    "player": name,
                    "submitted": False,
                    "timeMs": None,
                    "passed": False,
                    "passCount": 0,
                    "totalTests": len(test_cases),
                    "results": [],
                }

            sub = self.submissions[name]
            elapsed_ms = int((sub["timestamp"] - self.race_start_time) * 1000)

            try:
                print(f"[Room {self.room_id}] Judging {name}: lang={sub['language']}, code_len={len(sub['code'])}")
                verdict = await self.judge.evaluate(
                    sub["code"], sub["language"], test_cases
                )
                print(f"[Room {self.room_id}] Verdict for {name}: passed={verdict['passed']}, {verdict['pass_count']}/{verdict['total']}")
            except Exception as e:
                # If judging fails entirely, treat as all-fail
                import traceback
                print(f"[Room {self.room_id}] ⚠ Judge error for {name}: {e}")
                traceback.print_exc()
                verdict = {
                    "passed": False,
                    "pass_count": 0,
                    "total": len(test_cases),
                    "results": [],
                }

            return {
                "player": name,
                "submitted": True,
                "timeMs": elapsed_ms,
                "passed": verdict["passed"],
                "passCount": verdict["pass_count"],
                "totalTests": verdict["total"],
                "results": verdict["results"],
            }

        # Run both evaluations concurrently
        results = await asyncio.gather(
            evaluate_player(self.players[0]),
            evaluate_player(self.players[1]),
        )
        return list(results)

    def _resolve_timestamp_only(self):
        """Fallback: Phase 1 timestamp-only resolution (no judge available)."""
        submissions_list = []
        for p in self.players:
            name = p["name"]
            if name in self.submissions:
                sub = self.submissions[name]
                elapsed_ms = int((sub["timestamp"] - self.race_start_time) * 1000)
                submissions_list.append({
                    "player": name,
                    "submitted": True,
                    "timeMs": elapsed_ms,
                    "passed": None,
                    "passCount": None,
                    "totalTests": None,
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
                    "results": [],
                })
        return submissions_list

    def _determine_winner(self, submissions_list):
        """Apply judging rules to determine the winner.

        Rules:
        1. Both PASS  → winner = earlier timestamp
        2. One PASSES → winner = the one that passed
        3. Both FAIL  → higher passCount wins; if tied → TIE
        4. Neither submitted → TIE
        """
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
        # the race for the opponent too — don't leave their agent running.
        player = self.get_player(ws)
        if player:
            await self._cancel_agent_task(player["name"])

        # If the race is live and the disconnecting player hasn't submitted,
        # resolve immediately so the remaining player wins.
        if self.state == RoomState.RACING:
            if player and player["name"] not in self.submissions:
                await self.resolve()
