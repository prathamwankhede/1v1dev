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
from server.sandbox import Sandbox
from server.judge import Judge

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems"

# Piston URL — override with PISTON_URL env var if needed
PISTON_URL = os.environ.get("PISTON_URL", "http://localhost:2000")


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
                        print(f"[WS] join: name={player_name}, ws_id={id(ws)}")
                        await lobby.add_player(ws, player_name)

                    elif msg_type == "submit":
                        room = lobby.get_room(ws)
                        print(f"[WS] submit: ws_id={id(ws)}, room={room.room_id if room else 'None'}, room_state={room.state if room else 'N/A'}")
                        if room:
                            await room.handle_submit(
                                ws,
                                data.get("code", ""),
                                data.get("language", "python"),
                            )
                        else:
                            print(f"[WS] ⚠ No room found! player_rooms keys: {[id(k) for k in lobby.player_rooms.keys()]}")
                    elif msg_type == "agentPrompt":
                        room = lobby.get_room(ws)
                        if room:
                            config = {
                                "base_url": data.get("baseUrl"),
                                "model": data.get("model"),
                                "api_key": data.get("apiKey"),
                            }
                            await room.handle_agent_prompt(
                                ws,
                                data.get("agentType"),
                                config,
                                data.get("instruction", ""),
                                data.get("language", "python"),
                                data.get("code", ""),
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


async def health_handler(request):
    """Health check endpoint — reports server and sandbox status."""
    sandbox: Sandbox = request.app["sandbox"]
    piston_ok = await sandbox.is_available()
    status = {
        "server": "ok",
        "piston": "ok" if piston_ok else "unavailable",
        "piston_url": sandbox.piston_url,
    }
    code = 200 if piston_ok else 503
    return web.json_response(status, status=code)


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


async def on_startup(app):
    """Log startup info and check Piston availability."""
    sandbox: Sandbox = app["sandbox"]
    if await sandbox.is_available():
        runtimes = await sandbox.get_runtimes()
        langs = [r["language"] for r in runtimes]
        print(f"✓ Piston sandbox connected at {sandbox.piston_url}")
        print(f"  Available runtimes: {', '.join(langs)}")
    else:
        print(f"⚠ Piston sandbox not available at {sandbox.piston_url}")
        print("  Code execution will fail until Piston is running.")
        print(f"  Start it with: docker compose up -d")


async def on_cleanup(app):
    """Clean up resources on shutdown."""
    sandbox: Sandbox = app["sandbox"]
    await sandbox.close()


def create_app():
    app = web.Application()
    app["clients"] = set()

    # Load problem bank
    problem_bank = ProblemBank(PROBLEMS_DIR)
    app["problem_bank"] = problem_bank

    # Create sandbox and judge
    sandbox = Sandbox(piston_url=PISTON_URL)
    judge = Judge(sandbox)
    app["sandbox"] = sandbox
    app["judge"] = judge

    # Create lobby with judge
    app["lobby"] = Lobby(problem_bank, judge=judge)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/{path:.*}", static_handler)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, port=int(os.environ.get("PORT", 3000)))
