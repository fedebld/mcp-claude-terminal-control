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

Resources: `skill://claude-terminal-control`, `readme://claude-terminal-control`.

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
