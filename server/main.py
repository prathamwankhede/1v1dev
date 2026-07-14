import asyncio
import json
import os
from pathlib import Path

from aiohttp import web

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
}


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
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app["clients"].add(ws)
    await broadcast_player_count(request.app)
    try:
        async for _msg in ws:
            pass  # No client→server messages in Phase 0
    finally:
        request.app["clients"].discard(ws)
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
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/{path:.*}", static_handler)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, port=int(os.environ.get("PORT", 3000)))
