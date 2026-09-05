"""Lobby — FIFO matchmaker that pairs players into rooms.

When two players are in the queue, they're matched into a new Room
and the countdown begins automatically.

Player identity is a durable session token (`Lobby.sessions`), not the
WebSocket object — a dropped connection is recoverable within the room's
disconnect grace window (see `Room.DISCONNECT_GRACE_SECONDS`). `ws` on a
session dict is a mutable field that `resume()` reassigns; `Room.players`
holds the very same session dicts, so a reassignment is visible to the
room with no extra sync path.
"""

import asyncio
import json
import secrets
import time
import uuid

from server.room import Room, RoomState


class Lobby:
    """Manages the waiting queue, active rooms, and player sessions."""

    def __init__(self, problem_bank, judge=None, forced_problem_id=None):
        self.queue = []            # list of session dicts, FIFO
        self.rooms = {}            # room_id → Room
        self.player_rooms = {}     # token → room_id
        self.sessions = {}         # token → session dict
        self.ws_tokens = {}        # ws → token, for currently-connected sockets
        self.problem_bank = problem_bank
        self.judge = judge
        # Testing aid: pin every room to one problem instead of picking at
        # random, so a specific problem can be exercised repeatedly.
        self.forced_problem_id = forced_problem_id

    def _pick_problem(self):
        """Choose the problem for a new room."""
        if self.forced_problem_id:
            problem = self.problem_bank.get_by_id(self.forced_problem_id)
            if problem:
                return problem
            print(f"[Lobby] ⚠ FORCE_PROBLEM_ID='{self.forced_problem_id}' "
                  f"not found in the problem bank — falling back to random.")
        return self.problem_bank.get_random()

    async def _send(self, ws, msg):
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            pass

    async def add_player(self, ws, name):
        """Add a player to the matchmaking queue.

        Mints a durable session token for this socket and sends it back
        immediately, so a client can persist it and resume later even if it
        never gets matched into a room. The moment two players are queued,
        this pops both and creates a room.
        """
        if ws in self.ws_tokens:
            return  # already joined (queued, racing, or resumed) on this socket

        token = secrets.token_urlsafe(32)
        session = {
            "token": token,
            "name": name,
            "ws": ws,
            "room_id": None,
            "disconnected_at": None,
            "tree": {},
            "rev": 0,
        }
        self.sessions[token] = session
        self.ws_tokens[ws] = token
        self.queue.append(session)

        await self._send(ws, {"type": "session", "token": token})

        if len(self.queue) >= 2:
            p1 = self.queue.pop(0)
            p2 = self.queue.pop(0)
            await self._create_room(p1, p2)

    async def _create_room(self, session1, session2):
        """Create a room, notify both players, and start the countdown."""
        room_id = uuid.uuid4().hex[:8]
        problem = self._pick_problem()
        room = Room(
            room_id, session1, session2, problem, judge=self.judge,
            on_finished=lambda: self._schedule_teardown(room_id),
        )

        self.rooms[room_id] = room
        for s in (session1, session2):
            s["room_id"] = room_id
            self.player_rooms[s["token"]] = room_id

        # Notify both players they've been matched
        for s in (session1, session2):
            opponent = session2 if s is session1 else session1
            await room.send_to(s, {
                "type": "matched",
                "roomId": room_id,
                "opponent": opponent["name"],
            })

        # Start countdown → race in the background
        asyncio.create_task(room.start_countdown())

    def _schedule_teardown(self, room_id):
        """Room.resolve() calls this once the room reaches FINISHED.

        Deferred rather than immediate so a rejoining player within the
        window still gets their result screen instead of a lobby dump.
        """
        asyncio.create_task(self._teardown_room(room_id))

    async def _teardown_room(self, room_id):
        await asyncio.sleep(Room.DISCONNECT_GRACE_SECONDS)
        room = self.rooms.pop(room_id, None)
        if not room:
            return
        for p in room.players:
            token = p["token"]
            self.player_rooms.pop(token, None)
            self.sessions.pop(token, None)
            ws = p.get("ws")
            if ws is not None:
                self.ws_tokens.pop(ws, None)

    def get_room(self, ws):
        """Return the Room a player is in, or None."""
        token = self.ws_tokens.get(ws)
        if not token:
            return None
        room_id = self.player_rooms.get(token)
        if room_id:
            return self.rooms.get(room_id)
        return None

    def handle_socket_closed(self, ws):
        """Called from main.py's `finally` block whenever a socket's read
        loop ends — a genuine disconnect, or the losing side of a resumed
        duplicate-socket claim tearing itself down.

        Does not remove the session outright: the room owns the disconnect
        grace window and eventual forfeit (`Room.handle_disconnect`); this
        just marks the socket gone and lets the room react.
        """
        token = self.ws_tokens.pop(ws, None)
        if token is None:
            self.queue = [s for s in self.queue if s["ws"] is not ws]
            return

        self.queue = [s for s in self.queue if s["token"] != token]

        session = self.sessions.get(token)
        if not session or session["ws"] is not ws:
            # A resumed connection already replaced this socket for the
            # session — this is that stale socket's own teardown finishing.
            return

        room_id = self.player_rooms.get(token)
        if not room_id:
            # Never matched into a room — nothing to reconnect to.
            del self.sessions[token]
            return

        session["ws"] = None
        session["disconnected_at"] = time.monotonic()

        room = self.rooms.get(room_id)
        if room:
            asyncio.create_task(room.handle_disconnect(token))

    def remove_player(self, ws):
        """Voluntary exit ("Play Again"): drop this socket's session outright
        so the player can join fresh.

        Unlike a real disconnect, there is no reconnection path back into
        the old room afterwards — if they were still racing, this feeds into
        the same grace-then-forfeit path a genuine drop would.
        """
        token = self.ws_tokens.pop(ws, None)
        self.queue = [s for s in self.queue if s["ws"] is not ws]
        if not token:
            return

        room_id = self.player_rooms.pop(token, None)
        session = self.sessions.pop(token, None)
        if room_id and room_id in self.rooms:
            if session is not None:
                session["ws"] = None
            asyncio.create_task(self.rooms[room_id].handle_disconnect(token))

    async def resume(self, ws, token):
        """Reattach a client to its session after a dropped/refreshed
        connection.

        Verification is a chain — each failure mode gets a distinct
        response, since collapsing it to valid/invalid would dump a player
        whose race legitimately ended while they were offline straight to
        the lobby with no explanation.
        """
        session = self.sessions.get(token)
        if not session:
            await self._send(ws, {"type": "resumeFailed", "reason": "unknown"})
            return

        room_id = session.get("room_id")
        room = self.rooms.get(room_id) if room_id else None
        if not room:
            self.sessions.pop(token, None)
            self.player_rooms.pop(token, None)
            await self._send(ws, {"type": "resumeFailed", "reason": "expired"})
            return

        # TCP can take 30+ seconds to notice a dead peer, so the common case
        # here is a zombie socket the server still believes is live — close
        # it rather than reject the new connection.
        old_ws = session.get("ws")
        if old_ws is not None and old_ws is not ws:
            self.ws_tokens.pop(old_ws, None)
            try:
                await old_ws.close()
            except Exception:
                pass

        session["ws"] = ws
        session["disconnected_at"] = None
        self.ws_tokens[ws] = token

        payload = room.build_resume_payload(token)
        if payload:
            await self._send(ws, payload)

        if room.state == RoomState.RACING:
            opponent = room.get_opponent_by_token(token)
            if opponent:
                await room.send_to(
                    opponent, {"type": "opponentStatus", "status": "writing"}
                )
