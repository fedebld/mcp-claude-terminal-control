# mcp-claude-terminal-control

An MCP **facade** that lets an AI agent drive a *remote, interactive* `claude` TUI through
a handful of high-level **intent** calls — instead of choreographing raw keystrokes and
screen-scraping ANSI frames.

It is the "pre + post" layer around terminal automation:

- **Pre** (intent → keystrokes): `claude_open`, `claude_ask`, `claude_choose` encode the
  ssh/tmux/escape/sentinel mechanics. You say *what* you want, not *which keys*.
- **Post** (output → filtered): answers come back ANSI-stripped and bounded by an internal
  sentinel — the agent receives the **answer**, not terminal redraws. This keeps the
  orchestrator's context small (the original motivation: a screen-scraped session ballooned
  to ~120k tokens; intent calls return a few hundred).

Standalone: it drives `tmux` + `ssh` directly and does **not** depend on the upstream
`terminal-control` MCP.

## Architecture

```
agent ──MCP/http──▶ claude-terminal-control (container, Tailscale-only :8770)
                         │  tmux window per session
                         ▼
                    ssh -tt -i jump_fleet  claudeusr@claude-code  claude   (the TUI)
```

Each session is one tmux window running `ssh -t … claude`. The facade types into it with
`tmux send-keys -l` (literal) + a separate `Enter`, reads it with `capture-pane`, strips
ANSI, and waits for a per-prompt sentinel line that it asks `claude` to print when done.

## Tools

| Tool | Purpose |
|------|---------|
| `claude_open(target?, workdir?)` | start a piloted session, clear the trust dialog |
| `claude_ask(session_id, prompt, pace?)` | send a prompt, wait for completion, return clean answer |
| `claude_choose(session_id, option)` | answer a permission/selection dialog (`'1'`/`'enter'`/`'down'`…) |
| `claude_screen(session_id, mode?)` | minimal ANSI-stripped view (`tail`/`screen`/`full`) |
| `claude_sessions()` | list active sessions |
| `claude_close(session_id)` | tear a session down |
| `claude_attach(tmux_session, target?)` | adopt a PRE-EXISTING tmux session (orchestration mode) |
| `claude_send(session_id, text)` | fire-and-forget inject + submit, no wait |
| `claude_wait(session_id, timeout_s)` | block until idle / dialog (deterministic) |
| `claude_tail(session_id, lines)` | bounded chrome-filtered progress |
| `claude_status(session_id)` | `{state: working\|idle\|needs_choice\|dead}` |

Resources: `skill://claude-terminal-control`, `readme://claude-terminal-control`.

## Orchestration mode (attached sessions)

`claude_open` spawns a *local* tmux pane running `ssh -> claude`. To instead drive a
**pre-existing** tmux session -- a long-running / HITL `claude` someone already launched on the
target (e.g. a deploy) -- call `claude_attach(tmux_session)`. For attached sessions every tmux
op runs **over ssh on the target** (the same out-of-band channel `claude_ask` uses for the hash
artifact). Then orchestrate with `claude_send` (fire-and-forget inject), `claude_wait`
(deterministic idle-detection -- spinner gone for N samples, early-return on a dialog),
`claude_tail` (bounded progress) and `claude_status` (`working|idle|needs_choice|dead`, decided
by the tool). `claude_ask` is for piloted sessions only and **refuses** attached ones.
`claude_close` on an attached session **only detaches** -- it never kills the remote tmux, and
the idle reaper never tears down attached sessions.

## Answer integrity & the `frame` decision

`claude_ask(integrity=…)` extraction modes:

- **`hash`** (default, the only trusted path): claude writes the answer to a file on the
  target and prints `sha256`+`len` from real tools; the facade reads it out-of-band and
  re-hashes → `verified:true`, byte-exact, or `integrity_fail` (fail-closed).
- **`none`**: legacy chrome-filtered pane scrape, explicit opt-in, `verified:false`.
- **`frame`**: **DISABLED** — returns `{"status":"disabled"}`.

**Why `frame` is disabled** (decisional priority order, high → low):

1. **Deterministic correctness** — the answer is exact and reproducible
2. **Zero-trust verifiability** — integrity proven by a hash, not by trusting the render
3. **Fail-closed + supervision** — on any doubt, return an error and call a human; never guess
4. **Token economy / latency**
5. **Convenience / coverage** — answering without tools or permission prompts

`frame` read the answer out of the **rendered** TUI pane, which the terminal reflows and
box-draws. That maximised priority (5) — no tools, no approvals — but broke (1), (2) and (3):
the output was non-deterministic and impossible to *prove* correct; at best it failed closed
with an error. Since (1–3) outrank (5), and `hash` already covers the same use case **verified
and byte-exact**, `frame` earned its keep nowhere and was removed. On
`nondeterministic` / `integrity_fail` / `timeout`, the facade stops and calls the operator via
the telegram-notify gateway.

## Delay-er

`claude_ask(..., pace=true)` (or `PACING_DEFAULT=true`) blocks for a **randomized 2–9 min**
cooldown before sending — human-ish pacing and rate-limit friendliness. Blocking by design;
clients must allow long tool timeouts when pacing is on.

## Security / good-use

- ssh: `BatchMode`, `IdentitiesOnly`, dedicated key **mounted read-only** (never baked into
  the image), `known_hosts` pinned (`accept-new`). The key is authorised on the target with a
  `from="<219 Tailscale IP>"` source restriction.
- Exposure is **Tailscale-only** (`100.94.187.21:8770`); optional `AUTH_TOKEN` Bearer on top
  (fail-closed if set).
- Guardrails: session cap, idle reaper (TTL), per-session ask cap, prompt/output size caps,
  permission dialogs **surfaced** (not auto-approved) by default.

See `INSTALL.md` for deploy + registration.
