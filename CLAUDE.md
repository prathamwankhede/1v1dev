# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`1v1dev` is a real-time, two-player coding race platform: two competitors (human or AI agent) get matched, receive the same problem, and race to get a solution accepted. The build follows a 6-phase incremental plan documented in `implementation_plan.md`. The codebase is through **Phase 3 (Agent Backend Integration)** — matchmaking, room lifecycle, Piston-based judging, and four agent copilot adapters are implemented. **Phase 4 (Chaos Mode) is the next unbuilt phase**; Phases 4-5 have no code yet.

The problem bank holds two kinds of problem, tagged by an optional `kind` field:
- **`algorithmic`** (default) — `two-sum`, `fizz-buzz`, `reverse-string`. Small one-shot transformations.
- **`implementation`** — `kv-store-transactions`. A line-oriented command interpreter that tests system design (an undo stack for nested transactions) rather than recall. These get a longer clock and hidden test tiers.

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

# Pin every match to one problem instead of picking at random (testing aid)
FORCE_PROBLEM_ID=kv-store-transactions python server/main.py

# Run all tests
python -m unittest tests.test_smoke tests.test_agents

# Run a single test case
python -m unittest tests.test_smoke.TestRetryLoop.test_rejected_then_corrected_submission_wins
```

There is no separate lint/build step — this is a dependency-light aiohttp backend + vanilla JS/HTML/CSS frontend (no bundler, no npm).

Check sandbox health any time via `GET /health` — reports whether Piston is reachable.

## Architecture

**Request flow**: `server/main.py` is the single aiohttp entry point. It serves static files from `client/` (with directory-traversal protection), exposes `/health`, and handles all game logic over one `/ws` WebSocket route. There is no REST API for gameplay — everything (join, submit, play-again) is a JSON message over the socket, dispatched by `msg_type` in `websocket_handler`.

**Core object graph**, wired together in `create_app()`:
- `ProblemBank` (`server/problems.py`) — loads and validates problem JSON files from `problems/` at startup; each problem requires `id`, `title`, `description`, `starterCode`, `testCases`. Validation is presence-only, so optional fields (`kind`, `timeLimitSeconds`, per-case `sample`) cost nothing. Adding a new problem = dropping a new JSON file in `problems/`. `get_by_id` backs the `FORCE_PROBLEM_ID` override.
- `Sandbox` (`server/sandbox.py`) — async client for a self-hosted [Piston](https://github.com/engineer-man/piston) instance (run via `docker-compose.yml`). Maps language names to Piston runtime/version pairs (`LANGUAGE_MAP`), enforces CPU/memory/wall-clock limits, and executes one submission against one test case's stdin per call.
- `Judge` (`server/judge.py`) — runs a submission against every test case in a problem via `Sandbox.execute`, compares stripped stdout, and produces a verdict (`passed`, `pass_count`, `total`, per-test `results`). Test cases run **concurrently** under a `MAX_CONCURRENT_TESTS` semaphore; `asyncio.gather` keeps `results` aligned with `testCases`.
- `Lobby` (`server/lobby.py`) — FIFO matchmaker. Tracks a waiting queue plus `player_rooms` (ws → room_id) and `rooms` (room_id → Room). The moment two players are queued, it pops both, creates a `Room`, and kicks off the countdown as a background task (`asyncio.create_task`). `_pick_problem` honours `forced_problem_id`, else picks at random.
- `Room` (`server/room.py`) — per-match state machine: `COUNTDOWN → RACING → RESOLVING → FINISHED`. Owns the race clock, attempt storage, and winner determination. **Judging happens at submit time, not at race end**, and a submission must pass *every* test case to be accepted:
  - `handle_submit` validates, then dispatches `_run_judging` as a background task and returns. It must never judge inline — that would stall the player's socket read loop (the bug fixed for agent calls in `f245bb7`). Guards: one in-flight judging per player, plus `RESUBMIT_COOLDOWN_SECONDS`.
  - A rejected attempt sends `submissionResult` with `accepted: false` and leaves the race RACING, so the player fixes it and submits again. `submissions[name]` is a **list of attempts**.
  - The first attempt to pass everything wins immediately (`_accept` → `winner_name` → `resolve`). Because judging is concurrent, `_accept` first waits for any opponent attempt submitted *earlier* that is still being judged, so the race is decided on submission time, not judging latency. The strict `<` timestamp comparison is what stops the two judging tasks from ever waiting on each other.
  - `resolve()` reads verdicts already recorded at submit time — nothing is judged twice. `_determine_winner` short-circuits on `winner_name`; its remaining rules only decide races that ran out of time with nobody fully correct (higher `passCount`, tie → `None`).
  - Race clock is per-problem: `timeLimitSeconds` from the problem JSON, else `RACE_TIMEOUT_SECONDS = 120`. On timeout, `_drain_judging` gives an attempt made just before the buzzer a bounded chance to finish rather than cancelling it.
  - Hidden tests: a case is a sample unless any case in the problem carries a `sample` key. `start_race` sends only samples; `_public_results` strips hidden cases down to pass/fail so retries can't reconstruct the suite.
  - If `judge` is `None`, `Room` falls back to Phase 1 one-shot timestamp resolution (`_handle_submit_timestamp_only` / `_resolve_timestamp_only`).
- `AgentBackend` (`server/agents/interface.py`) — abstract contract (`async def run(prompt) -> {"code", "log"}`). Four adapters exist (`anthropic`, `openai_compatible`, `claude_code`, plus the unused `manual` stub), wired through `server/agents/registry.py`. The agent is a copilot the player prompts; it never submits. Note `_build_agent_context` passes the description **verbatim and without test cases**, so a problem's I/O contract has to be fully specified in its description prose — and descriptions should avoid ``` fences, which collide with the fences that builder adds.

**State lives in memory only** — `app["clients"]`, `Lobby.queue/rooms/player_rooms`, and each `Room`'s submissions dict are plain Python objects with no persistence layer. A server restart drops all lobbies/rooms.

**Client** (`client/`) is a single-page vanilla JS/HTML/CSS app (no build step, no framework) that speaks the same WebSocket protocol described above: `join` → `matched` → `countdown` → `raceStart` (carries the problem, its `timeLimitSeconds`, and sample test cases only) → `submit` → `judging` → `submissionResult`. A rejected `submissionResult` re-enables the submit button and the loop repeats; an accepted one is followed by `result`.

**Concurrency model**: aiohttp's single-threaded event loop; rooms run independently via `asyncio.create_task` (countdown, race timeout, per-player judging, agent calls, disconnect handling). Anything that can outlive its room is tracked in a dict (`judge_tasks`, `agent_tasks`) and cancelled on resolve, timeout, and disconnect. `_cancel_judge_task` skips `asyncio.current_task()`, since `resolve()` is reached from inside a judging task whenever an attempt is accepted.

## Working in this codebase

- When extending game logic, `Room` is the state machine to modify — resist adding game rules to `main.py`'s `websocket_handler`, which should stay a thin dispatcher.
- Piston must be running (`docker compose up -d`) for real judging; without it, `Sandbox.execute` raises and rooms fall back to all submissions failing (not to timestamp-only — that fallback is only when `Judge` itself is `None`).
- Test coverage lives in `tests/test_smoke.py` (lifecycle + judging) and `tests/test_agents.py` (agent plumbing), using `unittest.IsolatedAsyncioTestCase`, spinning up a real aiohttp server on an ephemeral port per test class and driving it over real WebSocket connections — follow this pattern (`asyncSetUp`/`recv_type` helpers) for new integration tests rather than mocking the WebSocket layer. Judging tests need Piston up, and cost real wall-clock time (5s countdown per race, plus the resubmit cooldown).
- **Pin the problem in any test that asserts on problem specifics** via `create_app(forced_problem_id=...)` — otherwise `get_random()` makes the test depend on which problem the bank happens to pick. This bit the agent tests once the bank gained a problem carrying its own `timeLimitSeconds`, which overrides a patched `RACE_TIMEOUT_SECONDS`.
- Adding a problem field means touching **two** places or it silently vanishes: `problems.py` validation, and the explicit field whitelist in `Room.start_race`'s `raceStart` payload.
- Respect the phase boundaries in `implementation_plan.md`: don't pull forward Chaos Mode work into unrelated changes unless that's explicitly the task. Note the `kind` tag on problems exists partly to give Phase 4's "spec change" chaos event something meaningful to target — that event only makes sense against an `implementation` problem.
