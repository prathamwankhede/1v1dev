# 1v1dev

`1v1dev` is a real-time, two-player coding race platform designed for high-performance, secure, and head-to-head programming showdowns. Competitors (either humans or AI agents) race against the clock in a secure, sandboxed environment, under the threat of dynamic **Chaos Mode** events (such as spec changes, code corruption, or tool blackouts).

Challenges come in two flavours: quick **algorithmic** puzzles, and longer **implementation** problems that ask you to build a small system (for example, an in-memory key-value store with nested transactions) on a longer clock. Submissions are judged the moment you send them — anything short of passing every test case comes back rejected, and you fix it and submit again. The first player to get a solution accepted wins.

---

## Project Overview

The core vision of `1v1dev` is to create a lightweight, responsive arena with minimal external dependencies. The architecture is planned in a sequence of iterative stages to isolate complexities (like sandbox execution and real-time state synchronization) and ensure robust security.

Currently, the project is through **Phase 3: AI Agent Backend Integration**. Phase 4 (Chaos Mode) is next.

### Technical Stack
- **Backend:** Python 3 + [aiohttp](https://docs.aiohttp.org/) (Asynchronous HTTP & WebSocket server)
- **Frontend:** Vanilla HTML5, CSS3 (CSS Variables, glowing glassmorphism theme, responsiveness), and Vanilla JS.
- **Typography:** Google Fonts (*Outfit* for UI headers, *JetBrains Mono* for code elements)
- **Testing:** Built-in Python `unittest` library (utilizing `IsolatedAsyncioTestCase` for async endpoints and WebSocket smoke tests).

---

## Repository Structure

- [server/main.py] — Asynchronous `aiohttp` server and websocket handler.
  - [server/room.py] — Per-match state machine, judging-on-submit, winner determination.
  - [server/judge.py] / [server/sandbox.py] — Test-case evaluation via the Piston sandbox.
  - [server/lobby.py] / [server/problems.py] — Matchmaking and the problem bank.
  - [server/agents/] — Agent copilot adapters behind a common interface.
- [client/] — Frontend assets:
  - [index.html] — Main web interface (Coding Arena UI)
  - [style.css] — Premium glowing modern dark-mode styles
  - [app.js] — WebSocket connection & UI interaction logic
- [problems/] — One JSON file per problem (id, title, description, starter code, test cases).
- [tests/test_smoke.py] — Integration tests for connectivity, the race lifecycle, and judging.
- [tests/test_agents.py] — Agent adapter and copilot plumbing tests.
- [requirements.txt] — Python dependency definition.
- [implementation_plan.md] — Architectural details and phase breakdowns.

---

## Getting Started

### Prerequisites
- Python 3.8 or higher installed on your system.

### Installation

1. **Navigate to the workspace:**
   ```bash
   cd 1v1dev
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install the required packages:**
   ```bash
   pip install -r requirements.txt
   ```

### Running the Application

Start the local server by running:
```bash
python server/main.py
```

By default, the server will launch on `http://localhost:3000`. You can override the port using the `PORT` environment variable:
```bash
PORT=8080 python server/main.py
```

Real judging needs the Piston sandbox running (`docker compose up -d`); check it with `GET /health`.

To play or test one specific problem instead of a random one, pin it by id (the id is the problem's filename in `problems/` without the `.json`):
```bash
FORCE_PROBLEM_ID=kv-store-transactions python server/main.py
```

Open multiple browser tabs at `http://localhost:3000` to see real-time player count synchronization via WebSockets!

### Using your local Claude Code

The "Local Claude Code" agent option shells out to a `claude` CLI already installed and logged in
on the machine running the server — no API key needed, since it uses that CLI's existing local
auth. It's off by default and intended for local dev / LAN play only (anyone who can reach the
server spends the host's Claude quota). To enable it:

```bash
ENABLE_LOCAL_CLAUDE_CODE=1 python server/main.py
```

Requires `claude` on `PATH` (or set `CLAUDE_CLI_PATH` to its location) and an already-logged-in
CLI session.

### Running Tests

Execute the test suite to verify server routing, WebSocket connectivity, judging, and agent plumbing:
```bash
python -m unittest tests.test_smoke tests.test_agents
```

The judging tests execute real code in Piston, so start it first (`docker compose up -d`).

---

## Roadmap & Development Phases

The project follows a 6-phase development roadmap outlined in [implementation_plan.md](file:///Users/prathamwankhede/Documents/1v1dev/implementation_plan.md):

- [x] **Phase 0: Skeleton & Local Dev Loop**
  - Basic asynchronous backend and WebSocket handshake.
  - Presence tracking (active player count).
  - Fast local smoke testing loop.
- [x] **Phase 1: Core Race Loop (No Sandbox)**
  - Lobby/matchmaking system matching players into rooms.
  - Problems loaded from JSON files.
  - Winner determined solely by submission timestamps (no execution). Still the fallback when no judge is configured.
- [x] **Phase 2: Sandbox Execution & Real Judging**
  - Integration with a self-hosted [Piston](https://github.com/engineer-man/piston) code execution backend.
  - Strict resource constraints (CPU, memory, wall time).
  - Pass/fail test case verification, judged on submit with retries; hidden test cases; per-problem time limits.
- [x] **Phase 3: AI Agent Backend Integration**
  - Bring-your-own-model copilot: Anthropic, any OpenAI-compatible endpoint, or a local `claude` CLI.
  - The agent is a copilot the player prompts and iterates with — it never submits on its own.
- [ ] **Phase 4: Chaos Mode (Server-Push)**
  - Dynamic in-game disruptions (spec modifications, client-side code corruption, tool blackouts) delivered to both players simultaneously.
- [ ] **Phase 5: MCP-Based Chaos (Model Context Protocol)**
  - Intercepting agent tool calls via a thin proxy server to simulate chaos events directly within the LLM's workspace environment.
