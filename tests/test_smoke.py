"""Phase 0 smoke tests: HTTP serving + WebSocket player count tracking."""
import asyncio
import sys
import unittest
from pathlib import Path

# Allow `from server.main import ...` without packaging
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web
from server.main import create_app


class TestSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "localhost", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://localhost:{self.port}"
        self.ws_url = f"http://localhost:{self.port}/ws"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    # -- helpers --
    async def recv(self, ws, timeout=5):
        """Receive one JSON message with a timeout to prevent hanging."""
        return await asyncio.wait_for(ws.receive_json(), timeout=timeout)

    # -- tests --
    async def test_serves_index_html(self):
        async with self.session.get(self.base_url) as resp:
            self.assertEqual(resp.status, 200)
            text = await resp.text()
            self.assertIn("<title>1v1dev", text)

    async def test_player_count_tracking(self):
        ws1 = await self.session.ws_connect(self.ws_url)
        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 1})

        ws2 = await self.session.ws_connect(self.ws_url)
        msg = await self.recv(ws2)
        self.assertEqual(msg, {"type": "playerCount", "count": 2})

        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 2})

        await ws2.close()
        msg = await self.recv(ws1)
        self.assertEqual(msg, {"type": "playerCount", "count": 1})

        await ws1.close()


if __name__ == "__main__":
    unittest.main()
