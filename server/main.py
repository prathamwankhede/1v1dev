"""1v1dev race server — HTTP + WebSocket entry point.

Serves static client files and manages WebSocket connections
for the lobby, matchmaking, and race rooms.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path for package imports
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import aiohttp
from aiohttp import web

from server.problems import ProblemBank
from server.lobby import Lobby

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems"


async def broadcast_player_count(app):
    """Send current player count to every connected WebSocket."""
    clients = app["clients"]
    msg = json.dumps({"type": "playerCount", "count": len(clients)})
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except ConnectionError:
            clients.discard(ws)


async def websocket_handler(request):
    """Handle a WebSocket connection: lobby join, submissions, disconnect."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app["clients"].add(ws)
    await broadcast_player_count(request.app)

    lobby: Lobby = request.app["lobby"]

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "join":
                        player_name = data.get("playerName", "Anonymous").strip()
                        if not player_name:
                            player_name = "Anonymous"
                        await lobby.add_player(ws, player_name)

                    elif msg_type == "submit":
                        room = lobby.get_room(ws)
                        if room:
                            await room.handle_submit(
                                ws,
                                data.get("code", ""),
                                data.get("language", "python"),
                            )
                    elif msg_type == "playAgain":
                        # Remove from current room and allow re-queue
                        lobby.remove_player(ws)

                except json.JSONDecodeError:
                    pass
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        request.app["clients"].discard(ws)
        lobby.remove_player(ws)
        await broadcast_player_count(request.app)

    return ws


async def static_handler(request):
    """Serve files from CLIENT_DIR. '/' → index.html."""
    path = request.match_info.get("path", "") or "index.html"
    file_path = (CLIENT_DIR / path).resolve()
    # Prevent directory traversal
    if not str(file_path).startswith(str(CLIENT_DIR.resolve())):
        return web.Response(status=403, text="Forbidden")
    if not file_path.is_file():
        return web.Response(status=404, text="Not Found")
    return web.FileResponse(file_path)


def create_app():
    app = web.Application()
    app["clients"] = set()

    # Load problem bank
    problem_bank = ProblemBank(PROBLEMS_DIR)
    app["problem_bank"] = problem_bank

    # Create lobby with the problem bank
    app["lobby"] = Lobby(problem_bank)

    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/{path:.*}", static_handler)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, port=int(os.environ.get("PORT", 3000)))
