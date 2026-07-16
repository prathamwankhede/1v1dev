"""Room state machine — manages a single two-player race.

States: COUNTDOWN → RACING → RESOLVING → FINISHED

Phase 1 judging: first to submit wins (timestamp-only, no code execution).
"""

import asyncio
import json
import time


class RoomState:
    COUNTDOWN = "countdown"
    RACING = "racing"
    RESOLVING = "resolving"
    FINISHED = "finished"


class Room:
    COUNTDOWN_SECONDS = 5
    RACE_TIMEOUT_SECONDS = 120  # 2 minutes

    def __init__(self, room_id, player1, player2, problem):
        """Create a new room.

        Args:
            room_id: Short unique identifier.
            player1: dict with keys "ws" (WebSocketResponse) and "name" (str).
            player2: dict with keys "ws" (WebSocketResponse) and "name" (str).
            problem: Problem dict from the problem bank.
        """
        self.room_id = room_id
        self.players = [player1, player2]
        self.problem = problem
        self.state = RoomState.COUNTDOWN
        self.submissions = {}  # player_name → { code, language, timestamp }
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

    # ── Resolution ─────────────────────────────────────────────

    async def resolve(self):
        """Determine the winner and broadcast the result."""
        if self.state == RoomState.FINISHED:
            return
        self.state = RoomState.RESOLVING

        # Cancel the timeout if still running
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

        # Build submission summaries
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
                })
            else:
                submissions_list.append({
                    "player": name,
                    "submitted": False,
                    "timeMs": None,
                })

        # Winner logic (Phase 1: timestamp-only)
        submitted = [s for s in submissions_list if s["submitted"]]
        if len(submitted) == 2:
            winner = min(submitted, key=lambda s: s["timeMs"])["player"]
        elif len(submitted) == 1:
            winner = submitted[0]["player"]
        else:
            winner = None  # Tie — neither submitted

        self.state = RoomState.FINISHED
        await self.broadcast({
            "type": "result",
            "winner": winner,
            "submissions": submissions_list,
        })

    # ── Disconnect ─────────────────────────────────────────────

    async def handle_disconnect(self, ws):
        """Handle a player disconnecting mid-race."""
        opponent = self.get_opponent(ws)
        if opponent:
            await self.send_to(
                opponent, {"type": "opponentStatus", "status": "disconnected"}
            )

        # If the race is live and the disconnecting player hasn't submitted,
        # resolve immediately so the remaining player wins.
        if self.state == RoomState.RACING:
            player = self.get_player(ws)
            if player and player["name"] not in self.submissions:
                await self.resolve()
