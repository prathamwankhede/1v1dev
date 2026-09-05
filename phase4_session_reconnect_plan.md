# Phase 4 — Session Identity & Reconnection

## Context

Today a dropped WebSocket is a **forfeit**. `Room.handle_disconnect` ([room.py:841](server/room.py:841)) cancels the player's agent and judging tasks, and if they hadn't submitted yet it calls `resolve()` immediately — handing the race to their opponent. `Lobby.remove_player` then drops them from `player_rooms` entirely. There is no way back in.

That is survivable for a 120s algorithmic race. It is not survivable for the direction the problem bank is heading: implementation and debugging problems run 480s+, and a wifi blip eight minutes into a multi-file debugging race destroys the match. Refreshing the tab does the same thing, because the editor buffer lives only in CodeMirror.

The root cause is that **the WebSocket object itself is the player's identity**. `lobby.player_rooms` is keyed by `ws`, and `Room.get_player`/`get_opponent` compare `ws` by `is`. When the socket dies, the player ceases to exist.

This phase replaces that with a durable session token, adds a grace window instead of an instant forfeit, and syncs the player's working buffer to the server so a rejoining client is restored rather than reset.

> [!NOTE]
> This phase is deliberately **independent of the execution-model decision** (Piston `files[]` vs. per-room containers) and of multi-file problems. It touches identity, lifecycle, and state sync — none of which change based on how code gets run. It is safe to build while that choice is still open.

---

## Roadmap change

`implementation_plan.md` currently reserves Phase 4 for Chaos Mode. This work becomes the new Phase 4; Chaos Mode → Phase 5, MCP-Based Chaos → Phase 6. Update the phase table and the cross-references in `CLAUDE.md` ("Phase 4 (Chaos Mode) is the next unbuilt phase").

---

## Stage 1 — Session token replaces `ws` as identity

| Item | Detail |
|---|---|
| **Token** | `secrets.token_urlsafe(32)`, minted in `Lobby.add_player`. **Not** `random` — `problems.py` already imports it for `get_random()` and it is an easy wrong reach; Mersenne Twister output is reconstructible, and this token is the only thing preventing an opponent from claiming your race slot. |
| **Store** | `Lobby.sessions: token → {name, ws, room_id, disconnected_at, tree, rev}`. `ws` becomes a mutable field, not the key. |
| **Re-key** | `Lobby.player_rooms` becomes `token → room_id`. `Lobby.get_room(ws)` keeps its signature (resolve ws → token internally) so `main.py`'s dispatcher needs no change here. |
| **Room** | `Room.players` entries gain `token`. `get_player`/`get_opponent` keep their `ws` signatures; add `get_player_by_token`. |
| **Client** | New `session` message after `join` carries the token; client writes it to `localStorage`. |

### Fix the name-collision bug while re-keying

Six per-player structures in `Room` are keyed by **player name**: `submissions`, `agent_sessions`, `agent_tasks`, `judge_tasks`, `last_submit_at`, `accepted`. Nothing anywhere deduplicates names, and `main.py:65` defaults an empty name to `"Anonymous"`. Two players with the same name today share submission history, agent transcript, and cooldown state.

Re-key all six on `token`. This is mechanical, and it is free now because the token is being introduced anyway — doing it later means touching the same six dicts twice. `_determine_winner`'s `if name == winner` comparison ([room.py:415](server/room.py:415)) and the result payload still surface `name` for display; only the keys change.

---

## Stage 2 — Grace window instead of instant forfeit

Add `DISCONNECT_GRACE_SECONDS = 45` alongside the existing class constants in `Room` (`COUNTDOWN_SECONDS`, `RESUBMIT_COOLDOWN_SECONDS`, `JUDGE_DRAIN_SECONDS` — same pattern, patchable in tests).

**`main.py`'s `finally` block** stops calling `lobby.remove_player(ws)` and calls `lobby.handle_socket_closed(ws)` instead, which clears `session["ws"]`, stamps `disconnected_at`, and notifies the room.

**`Room.handle_disconnect` splits in two:**

| Behaviour | Now | After |
|---|---|---|
| Opponent notified | yes | yes, unchanged |
| Agent task | cancelled | **cancelled** — the result can't be delivered and it burns the player's API credits |
| Judging task | cancelled | **allowed to finish** — see below |
| No submissions yet | `resolve()` immediately | start grace timer |
| Grace expires | — | today's forfeit path runs |

> [!IMPORTANT]
> Letting in-flight judging finish is a behaviour change, not just a deferral. The race is decided on **submission** time, not judging latency (see `_accept`'s strict `<` timestamp comparison). A player who submits a winning answer and immediately loses their connection should win. Cancelling their judging silently converts that into a loss.

**Two invariants to hold:**

- **The race clock keeps running during grace.** `_race_timeout` is an independent task started in `start_race`, so this is automatic — but do not "helpfully" pause it. In a 1v1 race, a pause button you trigger by unplugging your ethernet is the single most abusable thing shippable.
- **Grace expiry must no-op on a finished room.** `_race_timeout` can fire *during* the grace window and resolve the race; the grace timer then wakes into a `FINISHED` room and must do nothing.

**Room teardown** currently happens inside `Lobby.remove_player` when the state is `FINISHED`. With deferred removal that path no longer runs, so `resolve()` schedules teardown after a retention window (reuse `DISCONNECT_GRACE_SECONDS`) — long enough that a rejoining player still gets their result screen — then deletes the room and its sessions. Without this, `rooms` and `sessions` grow unboundedly for the process lifetime.

---

## Stage 3 — Rejoin

Client sends `{type: "resume", token}` on connect when it has a stored token, instead of `join`. New dispatch branch in `websocket_handler` alongside `join`/`submit`/`agentPrompt`/`playAgain` — keep it thin, per CLAUDE.md; the logic belongs in `Lobby.resume`.

Verification is a **chain**, and each outcome needs a distinct client response — collapsing it to valid/invalid means a player whose race legitimately ended while offline gets dumped to the lobby with no explanation:

| Check fails | Response |
|---|---|
| Token not in `sessions` | `resumeFailed{reason:"unknown"}` → client clears storage, shows lobby |
| No `room_id`, or room gone | `resumeFailed{reason:"expired"}` → lobby |
| Room is `FINISHED` | attach socket, send the `result` payload |
| Grace window expired | attach, send `result` (they forfeited) |
| Session already has a live `ws` | **close the old socket, attach the new one** |

That last row matters more than it looks. TCP can take 30+ seconds to notice a dead peer, so the common reconnect case is a *zombie socket the server still believes is live*. Rejecting the new connection locks the player out during exactly the window they need it.

Do **not** rotate the token on resume. A client that reconnects and immediately drops again would be left holding an id the server no longer recognises — reintroducing the forfeit this phase exists to prevent.

**`resumeState` payload** — reuses the field whitelist from `start_race`'s `raceStart` (per CLAUDE.md, adding a problem field means touching both places, so build one helper and call it from both):

- problem (same whitelist), `timeLimitSeconds`, **remaining** time computed from `race_start_time`
- own attempt history from `submissions[token]`, passed through `_public_results` so hidden test cases stay stripped
- agent transcript from `agent_sessions[token]`
- opponent name and current status
- server's copy of the working tree + `rev`

---

## Stage 4 — Working-tree sync

| Piece | Where | Job |
|---|---|---|
| Session token | `localStorage` | identity across reconnects |
| Working buffer | `localStorage` | covers edits since the last server sync |
| Working buffer | `sessions[token]["tree"]` | recovery baseline |

**Not cookies.** The token is minted inside a WebSocket frame, so the client would have to set it via `document.cookie` — no `HttpOnly`, which was a cookie's only advantage here. What remains is the downside: `main.py` serves all static assets from the same origin as `/ws`, so a cookie rides every asset request, and a buffer-sized cookie would blow aiohttp's 8KB header cap and surface as random asset failures mid-race.

**Message**: `{type: "syncTree", rev, files: {path: content}}`. Server accepts only if `rev > session["rev"]`. Cap total payload (~256KB) so a malicious client can't park megabytes in server memory.

**Shape now, tree later.** Multi-file isn't built, so the client sends `{files: {"solution": code}}` — one entry, placeholder key. Building the map shape now costs nothing and avoids a second migration when the file tree lands. Document the placeholder at the call site.

**Sync triggers**: debounced on change (≥2s apart, ≤15s while dirty). **Not** `beforeunload` over the WebSocket — a `ws.send()` there is best-effort and browsers may tear the socket down first. Instead write `localStorage` synchronously in `beforeunload` (always reliable) and let the server copy be slightly stale. On resume, the client compares its `localStorage` rev against the server's and keeps the higher. There is exactly one writer per buffer, so no merge logic is needed.

> [!NOTE]
> Keep the client the **sole writer**. Today the agent returns code and the client applies it. If HackerRank-style Agent Mode (assistant edits files directly) is ever added, that introduces a second writer and this stops being trivial.

---

## Files touched

| File | Change |
|---|---|
| `server/lobby.py` | `sessions` store, token minting, re-key `player_rooms`, `resume()`, `handle_socket_closed()` |
| `server/room.py` | token on `players`, six dicts re-keyed, `DISCONNECT_GRACE_SECONDS`, split disconnect handling, grace timer, `resumeState` builder, teardown scheduling |
| `server/main.py` | `resume` + `syncTree` dispatch branches; `finally` block calls `handle_socket_closed` |
| `client/app.js` | localStorage read/write, `resume` on connect, `syncTree` debounce, `resumeState`/`resumeFailed`/`session` cases in the `switch` at [app.js:470](client/app.js:470) |
| `tests/test_session.py` | new |
| `implementation_plan.md` | renumber Phase 4→5, 5→6; insert this phase |
| `CLAUDE.md` | document the session/identity model and the new message types |

---

## Security note

Once a session token lives in `localStorage`, XSS becomes race-integrity-critical, and the highest-risk surface is agent output — attacker-influenced in a BYOM setup where the player supplies the model config.

The client is currently clean: every `innerHTML` path goes through `escapeHtml`, including the agent transcript renderer ([app.js:282](client/app.js:282)), and `problemDescription` uses `textContent`. Add a comment at the transcript renderer stating that the escaping is load-bearing for session security — it is the code most likely to attract a later "just render the markdown" change.

---

## Verification

Follow the existing integration-test pattern in `tests/test_smoke.py` and `tests/test_agents.py`: real aiohttp server on an ephemeral port via `asyncSetUp`, driven over real WebSocket connections. Pin the problem with `create_app(forced_problem_id=...)` per CLAUDE.md, and patch `DISCONNECT_GRACE_SECONDS`/`COUNTDOWN_SECONDS` down so the suite doesn't spend a minute per case.

Cases:

1. `join` returns a token; token is stable across a resume.
2. Disconnect mid-race no longer resolves the race — opponent sees `disconnected`, race stays `RACING`.
3. Reconnect within grace restores problem, remaining time, attempt history, and agent transcript.
4. Reconnect after grace expiry gets the forfeit result, not a lobby dump.
5. **Clock keeps running across a disconnect** — the anti-pause invariant.
6. Duplicate socket claim on a live session: old socket closed, new one attached.
7. Unknown/garbage token → `resumeFailed`, clean lobby state, no server error.
8. `syncTree` round-trips through resume; a lower `rev` is rejected.
9. Two players who both join as `"Anonymous"` keep separate submissions and agent sessions (the collision fix).
10. In-flight judging survives a disconnect and still records its verdict.

Manual end-to-end: two browser tabs, start a race, kill one tab's network (DevTools offline), confirm the opponent sees `disconnected` and the clock keeps ticking; restore network, confirm the editor buffer and race state come back.

```bash
python -m unittest tests.test_smoke tests.test_agents tests.test_session
```
