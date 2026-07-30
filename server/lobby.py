"""Lobby — FIFO matchmaker that pairs players into rooms.

When two players are in the queue, they're matched into a new Room
and the countdown begins automatically.
"""

import asyncio
import json
import uuid

from server.room import Room, RoomState


class Lobby:
    """Manages the waiting queue and active rooms."""

    def __init__(self, problem_bank, judge=None):
        self.queue = []           # list of { "ws": ..., "name": ... }
        self.rooms = {}           # room_id → Room
        self.player_rooms = {}    # ws → room_id
        self.problem_bank = problem_bank
        self.judge = judge

    async def add_player(self, ws, name):
        """Add a player to the matchmaking queue.

        If two players are queued, immediately creates a room.
        """
        # Prevent duplicate joins
        for entry in self.queue:
            if entry["ws"] is ws:
                return
        if ws in self.player_rooms:
            return

        self.queue.append({"ws": ws, "name": name})

        # Try to match
        if len(self.queue) >= 2:
            p1 = self.queue.pop(0)
            p2 = self.queue.pop(0)
            await self._create_room(p1, p2)

    async def _create_room(self, player1, player2):
        """Create a room, notify both players, and start the countdown."""
        room_id = uuid.uuid4().hex[:8]
        problem = self.problem_bank.get_random()
        room = Room(room_id, player1, player2, problem, judge=self.judge)

        self.rooms[room_id] = room
        self.player_rooms[player1["ws"]] = room_id
        self.player_rooms[player2["ws"]] = room_id

        # Notify both players they've been matched
        for p in (player1, player2):
            opponent = player2 if p is player1 else player1
            await room.send_to(p, {
                "type": "matched",
                "roomId": room_id,
                "opponent": opponent["name"],
            })

        # Start countdown → race in the background
        asyncio.create_task(room.start_countdown())

    def get_room(self, ws):
        """Return the Room a player is in, or None."""
        room_id = self.player_rooms.get(ws)
        if room_id:
            return self.rooms.get(room_id)
        return None

    def remove_player(self, ws):
        """Remove a player from queue or room on disconnect."""
        # Remove from queue
        self.queue = [p for p in self.queue if p["ws"] is not ws]

        # Remove from room
        room_id = self.player_rooms.pop(ws, None)
        if room_id and room_id in self.rooms:
            room = self.rooms[room_id]
            asyncio.create_task(room.handle_disconnect(ws))
            # Clean up finished rooms
            if room.state == RoomState.FINISHED:
                # Also remove the opponent's mapping
                for p in room.players:
                    self.player_rooms.pop(p["ws"], None)
                del self.rooms[room_id]
