# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`1v1dev` is a real-time, two-player coding race platform: two competitors (human or AI agent) get matched, receive the same algorithmic problem, and race to submit a correct solution. The build follows a 6-phase incremental plan documented in `implementation_plan.md`. The codebase is currently in **Phase 2 (Sandbox Execution & Real Judging)** — matchmaking, room lifecycle, and Piston-based code judging are implemented; agent backends (Phase 3) and Chaos Mode (Phases 4-5) are not yet built.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start the Piston sandbox (required for real judging — see Sandbox below)
docker compose up -d

# Run the server (serves client + WebSocket API)
python server/main.py                  # http://localhost:3000
PORT=8080 python server/main.py        # override port
PISTON_URL=http://localhost:2000 python server/main.py  # override sandbox URL

# Run all tests
python -m unittest tests/test_smoke.py

# Run a single test case
python -m unittest tests.test_smoke.TestRaceLifecycle.test_first_submitter_wins
```

There is no separate lint/build step — this is a dependency-light aiohttp backend + vanilla JS/HTML/CSS frontend (no bundler, no npm).

Check sandbox health any time via `GET /health` — reports whether Piston is reachable.

## Architecture

**Request flow**: `server/main.py` is the single aiohttp entry point. It serves static files from `client/` (with directory-traversal protection), exposes `/health`, and handles all game logic over one `/ws` WebSocket route. There is no REST API for gameplay — everything (join, submit, play-again) is a JSON message over the socket, dispatched by `msg_type` in `websocket_handler`.

**Core object graph**, wired together in `create_app()`:
- `ProblemBank` (`server/problems.py`) — loads and validates problem JSON files from `problems/` at startup; each problem requires `id`, `title`, `description`, `starterCode`, `testCases`. Adding a new problem = dropping a new JSON file in `problems/`.
- `Sandbox` (`server/sandbox.py`) — async client for a self-hosted [Piston](https://github.com/engineer-man/piston) instance (run via `docker-compose.yml`). Maps language names to Piston runtime/version pairs (`LANGUAGE_MAP`), enforces CPU/memory/wall-clock limits, and executes one submission against one test case's stdin per call.
- `Judge` (`server/judge.py`) — runs a submission against every test case in a problem via `Sandbox.execute`, compares stripped stdout, and produces a verdict (`passed`, `pass_count`, `total`, per-test `results`).
- `Lobby` (`server/lobby.py`) — FIFO matchmaker. Tracks a waiting queue plus `player_rooms` (ws → room_id) and `rooms` (room_id → Room). The moment two players are queued, it pops both, creates a `Room`, and kicks off the countdown as a background task (`asyncio.create_task`).
- `Room` (`server/room.py`) — per-match state machine: `COUNTDOWN → RACING → RESOLVING → FINISHED`. Owns the race timer (`RACE_TIMEOUT_SECONDS = 120`), submission storage, and the winner-determination logic in `_determine_winner`. Judging rules, in priority order: both pass → earliest timestamp wins; one passes → that player wins outright regardless of time; both fail → higher `passCount` wins, tie → `None` (draw). If `judge` is `None`, `Room` falls back to Phase 1 timestamp-only resolution (`_resolve_timestamp_only`) — this path still exists and is exercised by tests.
- `AgentBackend` (`server/agents/interface.py`) — abstract contract (`async def run(prompt) -> {"code", "log"}`) for future AI players. Only `ManualAgent` (`server/agents/manual.py`) exists today, and it's a no-op stub — the human submit flow bypasses agents entirely in the current phase.

**State lives in memory only** — `app["clients"]`, `Lobby.queue/rooms/player_rooms`, and each `Room`'s submissions dict are plain Python objects with no persistence layer. A server restart drops all lobbies/rooms.

**Client** (`client/`) is a single-page vanilla JS/HTML/CSS app (no build step, no framework) that speaks the same WebSocket protocol described above: `join` → `matched` → `countdown` → `raceStart` (carries the problem) → `submit` → `submitted`/`opponentStatus` → `judging` → `result`.

**Concurrency model**: aiohttp's single-threaded event loop; rooms run independently via `asyncio.create_task` (countdown, race timeout, disconnect handling). Both players' submissions are judged concurrently with `asyncio.gather` in `Room._resolve_with_judge`.

## Working in this codebase

- When extending game logic, `Room` is the state machine to modify — resist adding game rules to `main.py`'s `websocket_handler`, which should stay a thin dispatcher.
- Piston must be running (`docker compose up -d`) for real judging; without it, `Sandbox.execute` raises and rooms fall back to all submissions failing (not to timestamp-only — that fallback is only when `Judge` itself is `None`).
- Test coverage lives entirely in `tests/test_smoke.py` using `unittest.IsolatedAsyncioTestCase`, spinning up a real aiohttp server on an ephemeral port per test class and driving it over real WebSocket connections — follow this pattern (`asyncSetUp`/`recv_type` helpers) for new integration tests rather than mocking the WebSocket layer.
- Respect the phase boundaries in `implementation_plan.md`: don't pull forward Chaos Mode or agent-backend work into unrelated changes unless that's explicitly the task.
