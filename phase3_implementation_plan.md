# Phase 3 Implementation Plan — Agent Backend Integration

## Goal

A player can direct an LLM/agent backend of their choice (Anthropic, OpenAI, OpenRouter, a local Ollama/vLLM instance, OpenCode's gateway, etc.) with their own natural-language instructions to help write the solution. The agent is a copilot the player prompts and iterates with — it never runs autonomously and never submits on its own. The player reviews whatever code the agent produces, can keep editing it by hand, and only the player's explicit click of the existing "Submit Solution" button submits anything. The `handle_submit` → `Judge` path is completely unchanged.

## Interaction model

1. Player picks a backend (Anthropic, or a Custom OpenAI-compatible endpoint) and enters config (model, API key, base URL if custom).
2. Player types an instruction into a prompt box next to the editor — e.g. "write a solution using a hash map for O(n) time" or "fix the off-by-one in the loop" — and clicks **Ask Agent** (a separate control from Submit).
3. Server builds full context for the call: problem description + starter code + **the player's current editor contents** + the running conversation history for that player in this room + the new instruction.
4. Agent responds with generated code (and optional explanation/log text).
5. Server sends the code back to that player only; the client loads it into the editor for the player to review/edit — it is **not** auto-submitted.
6. Player can send another instruction (multi-turn, e.g. "now handle the empty-array case") and the agent revises, using the conversation history for continuity.
7. Whenever the player is satisfied, they click Submit themselves, same as a fully-manual player.

This means `AgentBackend.run(prompt)` is called once per player instruction, not once per race — the "prompt" it receives is the full assembled context string described above, not just the static problem text.

## Design: BYOM, not a single hardcoded provider

Two adapters implementing the existing `AgentBackend` contract (`server/agents/interface.py`) cover almost every real-world backend:

- **`AnthropicAgent`** (`server/agents/anthropic.py`) — native Anthropic Messages API, for players who want Claude specifically.
- **`OpenAICompatibleAgent`** (`server/agents/openai_compatible.py`) — hits any `POST {baseUrl}/chat/completions` endpoint. This is what makes it BYOM: it covers OpenAI, OpenRouter, Ollama, local vLLM, and any OpenCode-style gateway, since they all implement the same request/response shape. The player supplies `baseUrl`, `model`, and `apiKey` (optional for local/no-auth backends).

A third option, a **CLI-shell-out adapter** (e.g. running `opencode` or `aider` as a subprocess), is deferred to a stretch item — see security note below.

## New files

- **`server/agents/anthropic.py`** — `AnthropicAgent(AgentBackend)`. `async def run(prompt)` calls the Anthropic Messages API via `aiohttp`, returns `{"code": ..., "log": <full response text>}`.
- **`server/agents/openai_compatible.py`** — `OpenAICompatibleAgent(AgentBackend)`, constructed with `base_url`, `model`, `api_key`. Validates `base_url`'s host against `ALLOWED_HOSTS` (see Decisions §1) before making any request, raising if it doesn't match. `async def run(prompt)` POSTs a standard chat-completions payload and parses the response the same way.
- **`server/agents/parsing.py`** — shared `extract_code_block(text, language) -> str` helper (first fenced code block matching the language, else whole response). Used by both adapters so extraction behavior is consistent and testable in one place.
- **`server/agents/registry.py`** — `AGENT_TYPES = {"anthropic": AnthropicAgent, "openai-compatible": OpenAICompatibleAgent}` plus a `build_agent(agent_type, config) -> AgentBackend` factory.

## Modified files

- **`server/room.py`**:
  - Add `self.agent_sessions = {}` — `player_name → list[{"role": "user"|"agent", "content": str}]`, the per-player conversation history used to build context on each turn, truncated to the last `AGENT_HISTORY_LIMIT = 20` entries (FIFO).
  - Add `async def handle_agent_prompt(self, ws, agent_type, config, instruction, language, current_code)`:
    - Reject if `state != RACING` (mirror `handle_submit`'s guard).
    - Assemble the full context: problem description + starter code + `current_code` (the player's live editor buffer, sent up with each request) + this player's `agent_sessions` history + `instruction`.
    - Build the agent via `registry.build_agent(agent_type, config)` and run it as a background `asyncio.create_task` with a bounded timeout (recommend ~60s), so a slow/hung call can't block the room or eat the whole race clock.
    - Broadcast `{"type": "opponentStatus", "status": "agent-thinking"}` to the opponent while the call is in flight, reverting to a steady `"using-agent"` status once a response lands (extend `setOpponentStatus`'s label map client-side). The opponent never sees the prompt text or the generated code — only that an agent session is active.
    - On success, append the turn to `agent_sessions[player_name]` and send `{"type": "agentResponse", "code": ..., "log": ...}` to that player. **No call to `handle_submit` here** — submission stays a fully separate, player-initiated action.
    - On failure (bad key, unreachable endpoint, no code block found, timeout), send `{"type": "agentStatus", "status": "error", "message": ...}` to that player only; they keep whatever code is already in their editor and can keep typing manually.
- **`server/main.py`** — add a `msg_type == "agentPrompt"` branch in `websocket_handler` that pulls `agentType`, `baseUrl`, `model`, `apiKey`, `language`, `instruction`, `code` (current editor contents) from the message and calls `room.handle_agent_prompt(...)`.
- **`client/app.js`**:
  - Add a provider picker (agent type: Anthropic / Custom endpoint; for Custom, fields for Base URL, Model, API Key).
  - Add a prompt textarea + **Ask Agent** button next to the editor, distinct from **Submit Solution**.
  - On Ask Agent click, send `{type: "agentPrompt", agentType, baseUrl, model, apiKey, language, instruction, code: editor.getValue()}`.
  - On `agentResponse`, load `data.code` into the CodeMirror editor (`editor.setValue(...)`) and append the exchange to a small conversation log in the UI; leave Submit untouched so the player must click it explicitly.
  - Add `agentStatus` and `agent-thinking`/`using-agent` cases to `handleMessage`/`setOpponentStatus`.
- **`client/index.html`** — add the provider-picker UI, prompt box, and Ask Agent button to the editor panel.

## Protocol additions (WS messages)

- Client → server: `{"type": "agentPrompt", "agentType": "anthropic"|"openai-compatible", "baseUrl"?: "...", "model": "...", "apiKey"?: "...", "language": "python", "instruction": "...", "code": "<current editor contents>"}`
- Server → client (requester only): `{"type": "agentResponse", "code": "...", "log": "..."}`
- Server → client (requester only, on failure): `{"type": "agentStatus", "status": "error", "message": "..."}`
- Opponent sees the existing `opponentStatus` channel extended with `"agent-thinking"` (in-flight) and `"using-agent"` (session active, idle) values — no new message type needed there, and no code/prompt content ever reaches the opponent.
- Submission still goes over the pre-existing `{"type": "submit", "code", "language"}` message, unchanged, fired only by the player's own Submit click.

## Security note: SSRF risk from client-supplied `baseUrl`

Because the server (not the browser) makes the outbound HTTP request, letting a client fully control `baseUrl` is a textbook SSRF vector — a malicious player could point it at internal services or a cloud metadata endpoint (`169.254.169.254`) reachable from the server's network. This needs an explicit decision before coding (see below), not just an implicit "trust the client" default.

## Decisions (finalized)

1. **How open is "bring your own"?** → **Allowlist of known hosts.** `OpenAICompatibleAgent` validates `baseUrl`'s host against a fixed allowlist before making any request; anything else is rejected server-side with an `agentStatus` error (never silently ignored). Starting list, defined as `ALLOWED_HOSTS` in `server/agents/openai_compatible.py`:
   - `api.openai.com`
   - `openrouter.ai`
   - `localhost` / `127.0.0.1` / RFC1918 private ranges (covers self-hosted Ollama, vLLM, OpenCode gateway, etc. run alongside the server)
   Adding a new public provider later is a one-line change to this list, not a design change.
2. **API key fallback** → **None.** No server-side env var fallback for any adapter. If a player leaves the API key field blank (and the target isn't a no-auth local endpoint), the request fails fast with `{"type": "agentStatus", "status": "error", "message": "API key required"}` — checked client-side before sending and again server-side before calling the provider.
3. **Code-block extraction rule** → `parsing.extract_code_block(text, language)`: return the first fenced block tagged with the requested language (```python / ```javascript); if none, the first fenced block regardless of tag; if no fenced block exists at all, the full response text stripped. Both adapters and their unit tests share this exact rule.
4. **Timeout budget** → **60s** per agent call (`asyncio.wait_for(..., timeout=60)` in `handle_agent_prompt`), distinct from `Room.RACE_TIMEOUT_SECONDS` (120s).
5. **Conversation history size** → cap `agent_sessions[player_name]` at the **last 20 messages** (10 user/agent turn pairs) before folding into context — oldest turns drop off first-in-first-out. Defined as `AGENT_HISTORY_LIMIT = 20` in `server/room.py`.
6. **CLI-shell-out adapter (opencode, aider, etc.)** → **Deferred**, out of scope for this phase. Revisit as a separate follow-up once the HTTP-based adapters are working, since it introduces its own security surface (arbitrary subprocess args/env) beyond what's being solved here.

## Testing

Add `tests/test_agents.py` with a `FakeAgent(AgentBackend)` test double (no real network calls) to verify:

- `Room.handle_agent_prompt` populates `agent_sessions` and sends `agentResponse` back to the requester only, **without** triggering a submission.
- A player can send a second prompt and the fake agent receives the accumulated history in its context.
- The player must still send an explicit `submit` message for the race to resolve — an `agentPrompt` alone never ends the race.
- Opponent receives `opponentStatus` updates (`agent-thinking` / `using-agent`) but never sees `instruction`, `code`, or `log` content.

Follow the existing `IsolatedAsyncioTestCase` + real-WebSocket pattern already used in `tests/test_smoke.py`. Add focused unit tests for `parsing.extract_code_block` covering: clean single fence, no fence, multiple fences, fence with wrong language tag.
