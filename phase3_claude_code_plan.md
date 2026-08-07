# Phase 3 Addendum — Local Claude Code as an Agent Backend

## Goal

Add a third agent adapter, `claude-code`, that shells out to the **`claude` CLI already installed
and logged in on the machine running the server** (`/Users/prathamwankhede/.local/bin/claude`,
v2.1.220). The player picks "Local Claude Code" in the agent panel and types an instruction — no
API key, no base URL, no model config required, because the CLI uses the operator's existing local
Claude Code auth.

This reverses decision §6 of `phase3_implementation_plan.md` ("CLI shell-out → deferred") for this
one adapter only. The security surface that decision was worried about is addressed explicitly in
§5 below rather than by deferral.

Everything else about the Phase 3 interaction model is unchanged: the agent is a copilot, it
returns code into the player's editor, and **only the player's own Submit click ever submits**.

## 1. Invocation contract

One `claude` process per player instruction, one-shot, no tools, no shell:

```
claude -p
  --output-format json
  --model <validated model or omitted>
  --disallowed-tools "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite,SlashCommand,Skill"
  --permission-mode manual
  --strict-mcp-config          # no --mcp-config → loads no MCP servers
  --disable-slash-commands
  --no-session-persistence
  --setting-sources ""         # see §5.6 — confirm empty is accepted
  --system-prompt "<code-block-only instruction>"
```

> **Verified against the installed CLI (2.1.220) — two corrections from the first draft:**
> - **`--max-turns` does not exist in this build.** `claude --help | grep -i turn` finds no such
>   flag (it's an Agent SDK option, not a CLI one). Removed. Bounding agentic looping is handled by
>   the permission mode + tool denylist instead, not by a turn cap.
> - **`--disallowed-tools` is variadic (`<tools...>`).** Space-separated values risk swallowing
>   whatever argument follows. Pass a single comma-separated string — the help explicitly allows it.
>
> `--permission-mode manual`, `--strict-mcp-config`, `--disable-slash-commands`,
> `--no-session-persistence`, `--setting-sources`, `--system-prompt`, and `--append-system-prompt`
> were each confirmed present in this build.

**`--system-prompt`, not `--append-system-prompt`.** Append leaves the full Claude Code agent
harness persona in place; replace gives a predictable pure code-generator and drops the dynamic
system-prompt sections. This matters for §5.6 as well.

- **Prompt over stdin, not argv.** `Room._build_agent_context` produces a large string (problem +
  editor buffer + up to 20 history turns); macOS `ARG_MAX` is ~256 KB and quoting is a needless
  risk. Write it to the child's stdin and close.
- **`asyncio.create_subprocess_exec`** (argv list) — never `create_subprocess_shell`.
- **`cwd` = a fresh `tempfile.mkdtemp()` per call**, removed in `finally`. Critical: running in the
  repo root would pull *this project's* `CLAUDE.md`, git status, and file tree into the racing
  agent's context, which is both a leak and a distraction. An empty dir keeps it to problem context
  only.
- **`env`**: inherit (the CLI needs `HOME`/keychain access for the local login) but strip
  `ANTHROPIC_API_KEY` if present so behavior doesn't silently switch auth modes between machines.
- **Do NOT use `--bare`.** Its help text is otherwise appealing (skips hooks, CLAUDE.md
  auto-discovery, plugin sync) but it forces auth to `ANTHROPIC_API_KEY`/`apiKeyHelper` only and
  never reads OAuth or the keychain — which defeats the entire point of this adapter. The explicit
  flags above get most of the same isolation while keeping the local login.

**Output**: `--output-format json` prints a single JSON object. Use `result` (final assistant text)
and treat `is_error: true`, a non-zero exit code, or unparseable stdout as failure. Feed `result`
into the existing `parsing.extract_code_block(text, language)` so extraction behaves identically
across all three adapters. Log `session_id` / `duration_ms` / `total_cost_usd` server-side if
present; never send them to the client.

## 2. New file — `server/agents/claude_code.py`

`ClaudeCodeAgent(AgentBackend)`, ~110 lines. Shape:

```python
CLI_BINARY   = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL_RE     = re.compile(r"^[A-Za-z0-9._-]{1,64}$")   # rejects anything starting with "-"
MAX_CONCURRENT = int(os.environ.get("CLAUDE_CLI_CONCURRENCY", "2"))
PROC_TIMEOUT = 90                                      # seconds, see §4

class ClaudeCodeAgent(AgentBackend):
    def __init__(self, config):
        if os.environ.get("ENABLE_LOCAL_CLAUDE_CODE") != "1":
            raise ValueError("Local Claude Code backend is disabled on this server")
        model = (config.get("model") or "").strip()
        if model and not MODEL_RE.match(model):
            raise ValueError("Invalid model name")
        self.model = model or None          # omit the flag → CLI default
        self.language = config.get("language", "python")

    async def run(self, prompt: str) -> dict: ...
```

`run()` responsibilities, in order:

1. Acquire the concurrency semaphore (see the Python 3.9 warning below).
2. `workdir = tempfile.mkdtemp(prefix="1v1dev-agent-")`.
3. Build argv per §1, `create_subprocess_exec(..., stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=workdir, env=env)`.
4. `await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=PROC_TIMEOUT)`.
5. `finally`: if the process is still alive (timeout **or** cancellation from the caller), `proc.kill()`
   then `await proc.wait()`; `shutil.rmtree(workdir, ignore_errors=True)`.
6. On failure raise `RuntimeError` with a short message — `stderr` truncated to ~300 chars, since
   `Room.handle_agent_prompt` puts `str(e)` straight into an `agentStatus` message the player sees.
7. On success return `{"code": extract_code_block(text, self.language), "log": text}`.

Step 5 is the part that is easy to get wrong: `Room` wraps the call in `asyncio.wait_for(...,
AGENT_TIMEOUT_SECONDS)`, and cancelling the coroutine does **not** kill the child. Without an
explicit kill in `finally`, every timed-out race leaks a live `claude` process. Handle
`asyncio.CancelledError` there too, and re-raise it.

> ### ⚠ Do not create the semaphore at module level — this repo runs Python 3.9
>
> `python3 --version` here is **3.9.12**. On 3.9, `asyncio.Semaphore()` captures
> `get_event_loop()` at construction time. A module-level semaphore therefore binds to the
> import-time loop, and `tests/test_agents.py` uses `IsolatedAsyncioTestCase`, which runs every test
> on a *fresh* loop. Verified behavior:
>
> - **Uncontended** (`Semaphore(2)`, sequential acquires): passes. No waiter future is ever created,
>   so the stale loop is never touched.
> - **Contended** (two coroutines racing for a slot): `RuntimeError: got Future attached to a
>   different loop`.
>
> That combination is the worst case — it is green in naive tests and blows up only in a real
> two-player race, which is precisely the scenario the semaphore exists to handle. **Fix:**
> construct the semaphore lazily inside `run()` on first use, or build it in `create_app()` and hand
> it to the adapter through `config`. (Python 3.10+ removed the eager binding; do not rely on that
> here.)

## 2b. ⚠ Blocking bug found in review — must be fixed as part of this work

**This is the most serious finding, and the first draft of this plan missed it.**

`server/main.py:86` awaits the agent call *inline inside the socket read loop*:

```python
async for msg in ws:            # main.py:54
    ...
    elif msg_type == "agentPrompt":
        await room.handle_agent_prompt(...)   # main.py:86 — blocks the loop
```

Nothing else is read from that player's socket until the agent returns. Consequences:

- **The player cannot submit while their agent is thinking.** Their Submit click sits in the TCP
  buffer, unread.
- **Their submission timestamp is stamped late.** `handle_submit` timestamps at *processing* time,
  so the queued submit is recorded whenever the agent call finally unblocks the loop — directly
  corrupting the timestamp-based judging in `_determine_winner`.
- With a 90s adapter timeout inside a 120s race, one Ask Agent click can consume the player's entire
  race.

This is a **pre-existing bug in the shipped Phase 3 code**, not something the CLI introduces —
`phase3_implementation_plan.md` explicitly specified "run it as a background `asyncio.create_task`
… so a slow/hung call can't block the room", and the implementation did not do that. But the CLI
adapter makes it far worse: process cold start plus non-streaming output pushes typical latency well
above the HTTP adapters'. **Do not ship the CLI adapter on top of the inline await.**

Fix, in `Room`:

- Dispatch `handle_agent_prompt` via `asyncio.create_task` and return immediately, so `main.py`
  stays a thin non-blocking dispatcher.
- Track the task per player (`self.agent_tasks[name]`). Reject or cancel-and-replace a second
  prompt while one is in flight — the client already disables the Ask Agent button, but the server
  must not trust that.
- **Cancel in-flight agent tasks in every room-exit path**: race timeout, `resolve()`, and player
  disconnect (`lobby.remove_player`). Without this, a race that ends mid-call leaves an orphaned
  `claude` process running against the host's quota — the adapter's own `finally` kill only fires if
  something actually cancels or times out the awaiting coroutine.

Cancellation correctness depends on the adapter's `finally` block (§2, step 5) actually killing the
child. The two halves must land together.

## 3. Modified files

| File | Change |
|---|---|
| `server/agents/registry.py` | `AGENT_TYPES["claude-code"] = ClaudeCodeAgent` (one line + import). |
| `client/index.html` | One `<option value="claude-code">Local Claude Code</option>` in `#agent-type-select`. |
| `client/app.js` | Extend the existing `change` handler (line 173): hide `#agent-baseurl-input` **and** `#agent-apikey-input` when the type is `claude-code`; swap the model placeholder to `Model (optional — e.g. sonnet, opus)`. The send payload at line 191 already carries everything needed; no protocol change. |
| `server/room.py` | ~~No change~~ → **change required.** Add `self.agent_tasks = {}`, dispatch agent runs as tasks, cancel them on resolve/timeout/disconnect, and reject overlapping prompts per player. See §2b. |
| `server/main.py` | Still no *protocol* change — the existing `agentPrompt` branch already builds `{base_url, model, api_key}` and the adapter ignores the two it doesn't need — but the `await` at line 86 becomes a non-blocking dispatch per §2b. |
| `README.md` | Short "Using your local Claude Code" section: set `ENABLE_LOCAL_CLAUDE_CODE=1`, requires `claude` on `PATH` and an already-logged-in CLI. |

No new WS message types, no new server dependencies (stdlib `asyncio.subprocess` only).

**Conversation history stays where it is.** `Room.agent_sessions` + `_build_agent_context` already
fold prior turns into the prompt string, so the adapter can be fully stateless and symmetric with
the other two. *Alternative considered:* per-player `--session-id <uuid>` on turn 1 then `--resume
<uuid>` after, letting Claude Code own the history (better prompt-cache reuse, real conversation
semantics). It needs session persistence on, session cleanup after the race, and a `Room` change to
track UUIDs — worth doing only if one-shot context quality proves insufficient. Not in this pass.

## 4. Timeout budget

`Room.AGENT_TIMEOUT_SECONDS` is 60s today, sized for an HTTP API call. The CLI adds process cold
start (a few seconds of Node boot + config load) on top of model latency, and this runs inside a
120s race. Recommendation:

- Adapter-internal `PROC_TIMEOUT = 90`, and raise `AGENT_TIMEOUT_SECONDS` to 90 as well — or make it
  a per-adapter attribute (`agent.timeout_seconds`, defaulting to 60) that `handle_agent_prompt`
  reads. The per-adapter attribute is cleaner and is a ~3-line change to `Room`.
- Keep the adapter's own timeout **less than or equal to** the Room's, so the adapter reliably owns
  process cleanup instead of relying on the cancellation path.
- **Don't let queueing eat the budget** *(missed in the first draft)*: if the semaphore is acquired
  inside `run()`, a second player waiting for a slot burns their timeout waiting on the *queue*
  rather than on the model, and reports "Agent timed out" having never called anything. Either
  acquire the slot before the Room starts its clock, or split the budget into a short queue-wait
  timeout with a distinct "agent busy, try again" message and a separate execution timeout.

### Latency vs. the race clock — a product decision, not just a tuning knob

A 120s race with a 20-60s non-streaming agent call means a third to a half of the race is spent
staring at a disabled button. The HTTP adapters are faster and mask this; the CLI will not. Pick one
deliberately before building:

1. **Accept it** — cheapest; the local agent is simply a slow-but-free option.
2. **Stream progress** — `--output-format stream-json --include-partial-messages` (both confirmed
   present in this build, both require `--print`) feeding the `agentProgress` message type the
   architecture doc already defines in §4. Best UX, most work, and it changes the adapter's parsing
   from one JSON object to a line-delimited stream.
3. **Raise `RACE_TIMEOUT_SECONDS`** when either player has an agent session active.

## 5. Security & resource decisions

The two existing adapters make an outbound HTTPS call. This one spawns a local process **with the
operator's own Claude credentials**, which is a different threat model and deserves explicit
answers:

1. **Off by default.** Gate on `ENABLE_LOCAL_CLAUDE_CODE=1`, checked in `__init__` so a disabled
   server rejects with a clean `agentStatus` error. This adapter is for local dev / LAN play, not
   the single-VPS deployment in `implementation_plan.md` §7 — anyone who can reach `/ws` can spend
   the host's Claude quota, and there is no per-player auth in this codebase.
2. **No argv injection.** Model name validated against `MODEL_RE`; anything starting with `-` is
   rejected, so a player can't smuggle `--dangerously-skip-permissions` through the model field.
   `create_subprocess_exec` (no shell) means no metacharacter risk in the prompt itself.
3. **No tools, no filesystem.** `--disallowed-tools` covering the file/exec/network tools,
   `--permission-mode manual` (an unanswerable prompt in headless mode denies rather than
   auto-approves), `--strict-mcp-config` with no MCP config, and an empty temp `cwd`. The player's
   instruction is untrusted text going to a model; it must not be able to reach the host FS.
4. **Bounded concurrency.** Module-level semaphore (default 2). Each `claude` process is heavy;
   without a cap, repeated Ask Agent clicks from two players can exhaust the box.
5. **No credential leakage to clients.** Error messages are truncated stderr only; never echo argv,
   env, paths, cost, or session IDs into `agentStatus`.
6. **The empty temp `cwd` is not enough on its own** *(missed in the first draft)*. It defeats
   *project*-level `CLAUDE.md` discovery, but the **user-level** config still applies:
   - `~/.claude/CLAUDE.md` — this machine's global memory gets injected into every racing agent's
     context. It is noise at best and a leak of the operator's private instructions at worst.
   - **User-level settings can define hooks, which execute arbitrary shell commands** on events like
     prompt submission. A hook firing on every player instruction is a far bigger hole than any tool
     the denylist covers.

   Mitigations, in order of preference: `--setting-sources ""` (confirm the CLI accepts an empty
   value; otherwise pass the narrowest accepted value), `--system-prompt` to replace rather than
   append, and evaluating **`--safe-mode`**, which disables customizations wholesale. `--safe-mode`
   is worth testing precisely because — unlike `--bare` — its help text says nothing about
   restricting auth, so it may give `--bare`-grade isolation while keeping the local login. Verify
   that before relying on it.
7. **Treat the tool denylist as defense in depth, not as the guard** *(correction)*. Any hand-written
   list is incomplete — the first draft's omitted `TodoWrite`, `SlashCommand`, `Skill`, `BashOutput`,
   `KillShell`, `ExitPlanMode`, and every MCP tool. And `--allowed-tools` cannot be used as a
   whitelist substitute: it is an *auto-approve* list, not a restriction list, so an empty value
   restricts nothing. The load-bearing control is **`--permission-mode manual`**: in headless mode no
   one can answer a permission prompt, so tool calls are denied by default. The denylist is a second
   layer, and `--strict-mcp-config` plus the empty `cwd` are the third.

## 6. Testing

Add to `tests/test_agents.py` (no real CLI invocation in the suite):

- **Argv construction** — monkeypatch `asyncio.create_subprocess_exec` with a recorder returning
  canned stdout; assert `-p`, `--output-format json`, the permission mode, the comma-joined
  disallowed-tools string, and the absence of `--model` when the field is blank.
- **Injection** — `model="--dangerously-skip-permissions"` and `model="; rm -rf /"` both raise
  `ValueError` from `__init__`.
- **Gate** — with `ENABLE_LOCAL_CLAUDE_CODE` unset, `build_agent("claude-code", {})` raises.
- **Parsing** — canned JSON whose `result` holds a fenced ```python block yields exactly the code;
  `is_error: true`, exit code 1, and non-JSON stdout each raise `RuntimeError`.
- **Timeout cleanup** — fake process that never exits: `run()` raises and `kill()` was called.
- **Room level** — reuse the existing `FakeAgent` integration tests unchanged; they already prove an
  `agentPrompt` never submits. Just confirm `"claude-code"` resolves through `build_agent`.

Plus these, covering §2b (all use a `FakeAgent` that sleeps, no CLI involved):

- **Non-blocking dispatch** — send `agentPrompt` followed immediately by `submit`; the submission is
  recorded while the agent is still running, and its timestamp reflects when it arrived, not when
  the agent finished. This is the regression test for the judging-correctness bug.
- **Cancellation on race end** — race timeout fires with an agent call in flight; the agent task
  ends cancelled and the room still resolves normally.
- **Cancellation on disconnect** — player drops mid-call; no lingering task.
- **Overlapping prompts** — a second `agentPrompt` while one is in flight is rejected (or cleanly
  replaces the first), never runs two processes for one player.
- **Contended semaphore** — two concurrent `run()` calls on one loop, exercising the waiter path
  that the Python 3.9 module-level bug hides from uncontended tests.

A real end-to-end check stays manual: `ENABLE_LOCAL_CLAUDE_CODE=1 python server/main.py`, two tabs,
one player asks the local agent for a solution, reviews it, submits.

## 7. Confirm at implementation time

Each of these needs one manual `claude` invocation to settle. They are cheap to check and expensive
to get wrong, so check them **before** writing the adapter, not after.

- **Does `claude -p` with no positional argument read the prompt from stdin?** The whole §1
  invocation contract rests on this and it is **not verified** — it is the documented
  `echo "..." | claude -p` pattern, but confirm it on this build. Note the related subtlety: when a
  positional prompt *and* piped stdin are both present, stdin is treated as additional context
  rather than as the prompt.
- Exact key names in `--output-format json` output (`result`, `is_error`, `session_id`) against the
  installed CLI version — pin the parse to what this box actually emits, and fail loudly rather than
  silently returning empty code if the shape changes.
- Whether `--setting-sources ""` is accepted, and whether `--safe-mode` preserves the local OAuth
  login (§5.6). If `--safe-mode` does preserve it, it likely replaces several other flags.
- Whether `--permission-mode manual` or `--permission-mode dontAsk` behaves better headless with all
  tools already disallowed (either should be fine; pick whichever exits cleanly).
- Cold-start latency of a single call on this machine — it sets the §4 timeout and decides which
  latency option to take.

One caveat for the §6 tests: `tests/test_agents.py` patches `server.room.build_agent`, so the room
tests never touch a real adapter. The argv/parsing/kill tests must target `ClaudeCodeAgent` directly
by patching `asyncio.create_subprocess_exec`.
