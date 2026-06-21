# Install / deploy

## Prerequisites (on the host running the container)

- Docker + Docker Compose.
- The jump key at `~/.ssh/jump_fleet` (mode 600), whose public key is in the target's
  `authorized_keys` with a `from="<this host's Tailscale IP>"` restriction.
- The target reachable over Tailscale (default `claudeusr@claude-code`).

> **Host note.** The intended deploy path is `/opt/mcp-claude-terminal-control` as a
> **container** (not systemd). The original request named host `212`, which does not resolve
> on the tailnet; it was deployed on **219 / vLL-vault (100.94.187.21)** — the only host
> consistent with the `from="100.94.187.21"` restriction already set on the target. The
> container is portable: to move it, `docker compose up -d --build` on the new host and either
> add that host's Tailscale IP to the target's `from=`, or set `SSH_PROXYJUMP=llmadmin@100.94.187.21`.

## Deploy

```bash
sudo mkdir -p /opt/mcp-claude-terminal-control && sudo chown "$(id -u)":"$(id -g)" /opt/mcp-claude-terminal-control
# (copy the repo here)
cd /opt/mcp-claude-terminal-control
export HOST_TS_IP=$(tailscale ip -4 | head -1)
export APP_UID=$(id -u)                 # so the read-only-mounted ssh key is readable
docker compose up -d --build
curl -s "http://$HOST_TS_IP:8770/health" ; echo
```

## Register (for the agent's Claude Code)

```bash
claude mcp add --scope user --transport http claude-terminal-control http://100.94.187.21:8770/mcp
```
or in `.claude.json`:
```json
{ "mcpServers": { "claude-terminal-control": { "type": "http", "url": "http://100.94.187.21:8770/mcp" } } }
```
The tools appear on the next session/reconnect.

## Smoke test

```bash
pip install fastmcp==3.3.1
python how_to_use.py            # connects to the live endpoint, opens a session, asks one prompt
```

## Troubleshooting

- **`Permissions … too open` / key unreadable** → rebuild with `APP_UID=$(id -u)` so the
  container user matches the host key owner.
- **`Permission denied (publickey)` to target** → the host's Tailscale IP is not in the
  target's `from="…"` list, or `jump_fleet` is not authorised.
- **`claude_ask` returns `timeout`** → raise `ASK_TIMEOUT_S`, or the prompt triggered a
  permission dialog (`needs_choice`) — answer with `claude_choose`.
- **Pacing hangs the call** → that is the blocking delay-er (2–9 min). Disable per call by
  omitting `pace`, or globally with `PACING_DEFAULT=false`.
