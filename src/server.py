#!/usr/bin/env python3
"""
server.py — MCP facade that lets an AI agent drive a remote, interactive `claude` TUI
without screen-scraping it by hand.

The whole point: collapse the blind keystroke choreography (ssh -t → claude → trust
dialog → type → Enter → poll the screen N times) into a few high-level *intent* calls,
and stop shipping whole ANSI frames back into the agent's context.

It is a STANDALONE pilot (it does NOT depend on the upstream `terminal-control` MCP):
it drives tmux + ssh directly. Each session is one tmux window running
`ssh -t jump_fleet claudeusr@claude-code claude`.

Pre-layer (intent → keystrokes)   — claude_open / claude_ask / claude_choose
Post-layer (output → filtered)     — ANSI strip, sentinel-bounded answer extraction,
                                     await-until-done (replaces polling), size caps.

Tools (progressive disclosure — the agent normally only needs claude_open + claude_ask):
  claude_open(target?, workdir?)            → start a piloted claude session
  claude_ask(session_id, prompt, pace?)     → send a prompt, wait for the answer, return clean text
  claude_choose(session_id, option)         → answer a permission/selection dialog
  claude_screen(session_id, mode?)          → minimal, ANSI-stripped view (delta|tail|screen)
  claude_sessions()                         → list active piloted sessions
  claude_close(session_id)                  → tear a session down

Resources (self-discovery):
  skill://claude-terminal-control    → SKILL.md
  readme://claude-terminal-control   → README.md

Transport: Streamable HTTP on /mcp (matches the other MCPs on this host; Tailscale-only).
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
import random
import shlex
import logging
import subprocess
import threading
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP

# --------------------------------------------------------------------------- config
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8770"))
PATH = os.environ.get("MCP_PATH", "/mcp")

# How the pilot reaches the box running claude. Defaults match the 219→269 jump path.
SSH_KEY      = os.environ.get("SSH_KEY", "/app/.ssh/jump_fleet")
SSH_TARGET   = os.environ.get("SSH_TARGET", "claudeusr@claude-code")
SSH_PROXYJUMP = os.environ.get("SSH_PROXYJUMP", "").strip()   # "user@host" for portability off-219
KNOWN_HOSTS  = os.environ.get("KNOWN_HOSTS", "/app/.ssh/known_hosts")
CLAUDE_CMD   = os.environ.get("CLAUDE_CMD", "claude")

# Delay-er: block for a randomized cooldown BEFORE sending a paced prompt. Human-ish
# pacing + rate-limit friendliness. User-chosen behaviour: blocking (server-side sleep).
PACING_DEFAULT = os.environ.get("PACING_DEFAULT", "false").lower() in ("1", "true", "yes")
PACING_MIN_S   = int(os.environ.get("PACING_MIN_S", "120"))   # 2 min
PACING_MAX_S   = int(os.environ.get("PACING_MAX_S", "540"))   # 9 min

# Good-use / safety guardrails.
MAX_SESSIONS        = int(os.environ.get("MAX_SESSIONS", "4"))
IDLE_TTL_S          = int(os.environ.get("IDLE_TTL_S", "1800"))     # auto-reap idle sessions
MAX_PROMPT_CHARS    = int(os.environ.get("MAX_PROMPT_CHARS", "8000"))
MAX_OUTPUT_CHARS    = int(os.environ.get("MAX_OUTPUT_CHARS", "8000"))
MAX_ASKS_PER_SESSION = int(os.environ.get("MAX_ASKS_PER_SESSION", "200"))
ASK_TIMEOUT_S       = int(os.environ.get("ASK_TIMEOUT_S", "180"))
PANE_WIDTH          = int(os.environ.get("PANE_WIDTH", "220"))      # wide → no table/marker wrap
PANE_HEIGHT         = int(os.environ.get("PANE_HEIGHT", "50"))
POLL_INTERVAL_S     = float(os.environ.get("POLL_INTERVAL_S", "1.5"))
SCROLLBACK          = int(os.environ.get("SCROLLBACK", "3000"))
AUTH_TOKEN          = os.environ.get("AUTH_TOKEN", "").strip()     # optional Bearer on the HTTP layer
# Auto-approve permission dialogs. OFF by default: powerful, so surface to the caller.
AUTO_APPROVE        = os.environ.get("AUTO_APPROVE", "false").lower() in ("1", "true", "yes")
# Default answer-extraction mode: hash (verified file channel) | frame | none (legacy scrape).
INTEGRITY_DEFAULT   = os.environ.get("INTEGRITY_DEFAULT", "hash").lower()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("claude-pilot")

# ANSI / OSC escape stripper (CSI, OSC, and lone two-byte escapes).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Non-breaking space tmux sometimes renders in the input line.
_NBSP = "\xa0"


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s).replace(_NBSP, " ")


# Lines that are claude UI chrome, never part of an answer.
_CHROME = re.compile(
    r"(\? for shortcuts|for agents|esc to interrupt|Auto-update failed|shift\+tab to cycle|"
    r"plan mode on|accept edits on|/effort|Welcome back|Tips for getting started|"
    r"Run /init|release-notes|What's new|Synthesizing|Warping|Worked for|Running \d+ shell|"
    r"^\s*[╭╰│─╮╯┌┐└┘├┤┬┴┼>•⎿✻✢✶✻●·]+\s*$)"
)


def _looks_like_chrome(line: str) -> bool:
    t = line.strip()
    if not t:
        return True
    if _CHROME.search(t):
        return True
    # box-drawing / spinner glyphs only
    if all(ch in "╭╰│─╮╯┌┐└┘├┤┬┴┼ ❯>•⎿✻✢✶●·\t" for ch in t):
        return True
    return False


# --------------------------------------------------------------------------- tmux glue
def _tmux(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)


def _tmux_running(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def _capture(name: str) -> str:
    r = _tmux("capture-pane", "-p", "-S", f"-{SCROLLBACK}", "-t", name)
    return strip_ansi(r.stdout)


def _capture_screen(name: str) -> str:
    return strip_ansi(_tmux("capture-pane", "-p", "-t", name).stdout)


def _send_text(name: str, text: str) -> None:
    # `-l` = literal (no key-name interpretation): the robust way to type into a TUI.
    _tmux("send-keys", "-t", name, "-l", text)


def _send_key(name: str, key: str) -> None:
    # key is a tmux key-name: Enter, Down, Up, Escape, C-c, BTab, etc.
    _tmux("send-keys", "-t", name, key)


def _ssh_base_argv() -> list[str]:
    argv = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=15",
    ]
    if SSH_PROXYJUMP:
        argv += ["-J", SSH_PROXYJUMP]
    return argv


def _build_ssh_cmd(target: str, workdir: str | None) -> str:
    remote = CLAUDE_CMD if not workdir else f"cd {shlex.quote(workdir)} && exec {CLAUDE_CMD}"
    return shlex.join(_ssh_base_argv() + ["-tt", target, remote])


def _ssh_exec(target: str, remote_cmd: str, timeout: int = 30) -> tuple[int, bytes]:
    """Run a command on the target over a SEPARATE ssh, OUT-OF-BAND from the claude pane.
    This is the zero-trust read path: the answer artifact is fetched at the source and
    re-hashed here, never trusting the (lossy, ANSI-laden) terminal render."""
    r = subprocess.run(_ssh_base_argv() + [target, remote_cmd],
                       capture_output=True, timeout=timeout)
    return r.returncode, r.stdout


def _sha256_len(data: bytes) -> tuple[str, int]:
    import hashlib
    return hashlib.sha256(data).hexdigest(), len(data)


# --------------------------------------------------------------------------- sessions
@dataclass
class Session:
    sid: str
    tmux: str
    target: str
    workdir: str | None
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    asks: int = 0


_SESSIONS: dict[str, Session] = {}
_LOCK = threading.RLock()


def _reap_idle() -> None:
    now = time.time()
    with _LOCK:
        dead = [s for s in _SESSIONS.values()
                if now - s.last_used > IDLE_TTL_S or not _tmux_running(s.tmux)]
    for s in dead:
        LOG.info("reaping idle/dead session %s", s.sid)
        _kill(s)


def _kill(s: Session) -> None:
    try:
        if _tmux_running(s.tmux):
            _send_key(s.tmux, "C-c")
            time.sleep(0.3)
            _send_text(s.tmux, "/exit")
            _send_key(s.tmux, "Enter")
            time.sleep(0.5)
            _tmux("kill-session", "-t", s.tmux)
    except Exception as e:  # pragma: no cover
        LOG.warning("kill error for %s: %s", s.sid, e)
    with _LOCK:
        _SESSIONS.pop(s.sid, None)


def _wait_for(name: str, needles, timeout: int, absent=None):
    """Poll the pane until any needle appears (and optional `absent` is gone)."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if not _tmux_running(name):
            return False, last
        last = _capture(name)
        if any(n in last for n in needles):
            if not absent or not any(a in last for a in absent):
                return True, last
        time.sleep(POLL_INTERVAL_S)
    return False, last


# --------------------------------------------------------------------------- MCP app
mcp = FastMCP(
    "claude-terminal-control",
    instructions=(
        "Drive a remote interactive `claude` TUI through high-level intent calls instead of "
        "raw keystrokes. Start with `claude_open()` to launch a piloted session, then "
        "`claude_ask(session_id, prompt)` to send a prompt and get back the clean answer text "
        "(ANSI stripped, bounded by an internal sentinel, no screen frames). Use "
        "`claude_choose` to answer permission/selection dialogs, `claude_screen` for a minimal "
        "view, and `claude_close` when done. A blocking delay-er can pace prompts by a random "
        "2–9 min cooldown (pace=true). Designed to keep YOUR context tiny: it returns answers, "
        "not terminal redraws."
    ),
)


def _doc(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, name), os.path.join(here, "..", name), os.path.join("/app", name)):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return fh.read()
    return f"({name} not bundled)"


@mcp.resource("skill://claude-terminal-control")
def skill_doc() -> str:
    """Agent-facing usage guide."""
    return _doc("SKILL.md")


@mcp.resource("readme://claude-terminal-control")
def readme_doc() -> str:
    """Human-facing README."""
    return _doc("README.md")


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    with _LOCK:
        n = len(_SESSIONS)
    return JSONResponse({"status": "ok", "service": "claude-terminal-control",
                         "sessions": n, "max_sessions": MAX_SESSIONS})


# ------------------------------------------------------------------ tools
@mcp.tool
def claude_open(target: str | None = None, workdir: str | None = None) -> dict:
    """Launch a piloted `claude` session (ssh -t → claude) and clear the trust dialog.

    Returns {session_id, ready, banner}. Use the session_id with the other tools.
    """
    _reap_idle()
    with _LOCK:
        if len(_SESSIONS) >= MAX_SESSIONS:
            return {"error": f"session cap reached ({MAX_SESSIONS}); close one first"}
    sid = "cp_" + uuid.uuid4().hex[:8]
    name = f"cp_{sid}"
    tgt = target or SSH_TARGET
    ssh_cmd = _build_ssh_cmd(tgt, workdir)

    r = _tmux("new-session", "-d", "-s", name, "-x", str(PANE_WIDTH), "-y", str(PANE_HEIGHT), ssh_cmd)
    if r.returncode != 0:
        return {"error": f"tmux new-session failed: {r.stderr.strip()}"}

    # Clear the "trust this folder" gate if it appears (option 1 is pre-selected → Enter).
    ok, _ = _wait_for(name, ["trust this folder", "Quick safety check"], timeout=20)
    if ok:
        _send_key(name, "Enter")
    # Wait until the input box / welcome banner is up.
    ready, screen = _wait_for(name, ["for shortcuts", "Welcome back", "? for shortcuts"], timeout=30)
    sess = Session(sid=sid, tmux=name, target=tgt, workdir=workdir)
    with _LOCK:
        _SESSIONS[sid] = sess
    banner = "\n".join(l for l in _capture_screen(name).splitlines() if l.strip())[:600]
    return {"session_id": sid, "ready": ready, "target": tgt, "banner": banner}


@mcp.tool
def claude_ask(session_id: str, prompt: str, pace: bool | None = None,
               timeout_s: int | None = None, integrity: str | None = None) -> dict:
    """Send `prompt`, wait for completion, return the answer — by default *verified*.

    integrity (Verifiable Framed Payload):
      "hash"  (default) — claude writes the COMPLETE answer to /tmp/cp_<nonce> and prints a
                          marker carrying sha256+len from `sha256sum`/`wc` (deterministic
                          tools, not model math). The facade reads that file OUT-OF-BAND
                          (separate ssh, never the pane), RE-computes sha256+len and verifies.
                          Byte-exact; truncation/tamper ⇒ status="integrity_fail" (fail-closed).
                          May need shell approval — auto-approved ONLY for this turn's
                          /tmp/cp_<nonce> path (zero-trust scoping).
      "frame" — answer wrapped between per-nonce BEGIN/END marker lines in the pane; the
                facade extracts exactly between matched markers (deterministic boundaries,
                no hash, no tools/approvals). verified=false.
      "none"  — legacy best-effort chrome-filtered scrape. verified=false.

    pace: if true (or PACING_DEFAULT), sleep a random 2–9 min BEFORE sending (blocking).
    """
    with _LOCK:
        s = _SESSIONS.get(session_id)
    if not s:
        return {"error": "unknown session_id (open one with claude_open)"}
    if not _tmux_running(s.tmux):
        _kill(s)
        return {"error": "session is dead; open a new one"}
    if len(prompt) > MAX_PROMPT_CHARS:
        return {"error": f"prompt too long (>{MAX_PROMPT_CHARS} chars)"}
    if s.asks >= MAX_ASKS_PER_SESSION:
        return {"error": f"per-session ask cap reached ({MAX_ASKS_PER_SESSION})"}

    mode = (integrity or INTEGRITY_DEFAULT).lower()
    if mode not in ("hash", "frame", "none"):
        return {"error": "integrity must be hash|frame|none"}

    paced_s = 0.0
    do_pace = PACING_DEFAULT if pace is None else pace
    if do_pace:
        paced_s = random.uniform(PACING_MIN_S, PACING_MAX_S)
        LOG.info("pacing session %s: sleeping %.0fs before prompt", s.sid, paced_s)
        time.sleep(paced_s)

    nonce = uuid.uuid4().hex[:10]
    p = prompt.strip()
    if mode == "hash":
        path = f"/tmp/cp_{nonce}.txt"
        line = (f"{p}   --- IMPORTANTE: NON stampare la risposta nel terminale. Scrivi la "
                f"risposta COMPLETA nel file {path} (usa il tool Write). Poi con Bash esegui "
                f"`sha256sum {path}` e `wc -c {path}`. Infine stampa SOLO una riga, esattamente "
                f"questa, sostituendo HASH e N con i valori reali: "
                f"<<<CP nonce={nonce} sha256=HASH len=N>>>")
    elif mode == "frame":
        begin, end = f"<<<CPBEGIN {nonce}>>>", f"<<<CPEND {nonce}>>>"
        line = (f"{p}   --- Racchiudi la risposta ESATTAMENTE tra due righe a se stanti: "
                f"`{begin}` prima e `{end}` dopo. Nessun altro testo fuori da quelle due righe.")
    else:
        marker = f"###CP-{nonce}###"
        line = f"{p}   [A fine risposta scrivi SOLO questo, su una riga separata: {marker}]"

    _send_text(s.tmux, line)
    time.sleep(0.25)
    _send_key(s.tmux, "Enter")

    timeout = timeout_s or ASK_TIMEOUT_S
    deadline = time.time() + timeout
    captured = ""
    claimed = None
    done = False
    needs_choice = False
    while time.time() < deadline:
        if not _tmux_running(s.tmux):
            break
        captured = _capture(s.tmux)
        if mode == "hash":
            # robust to spacing/order: the claimed line carries nonce + a 64-hex sha256 + len.
            # The prompt echo has 'sha256=HASH' (not 64-hex) so it never false-matches.
            for l in captured.splitlines():
                if f"nonce={nonce}" not in l:
                    continue
                hm = re.search(r"sha256=([0-9a-fA-F]{64})", l)
                lm = re.search(r"len=(\d+)", l)
                if hm and lm:
                    claimed = (hm.group(1).lower(), int(lm.group(1)))
                    done = True
                    break
            if done:
                break
        elif mode == "frame":
            if any(l.strip() == end for l in captured.splitlines()):
                done = True
                break
        else:
            if any(l.strip() == marker for l in captured.splitlines()):
                done = True
                break
        if "Do you want to proceed?" in captured or "Yes, and don" in captured:
            if AUTO_APPROVE or (mode == "hash" and _auto_ok(captured, nonce)):
                _send_text(s.tmux, "1")  # 1 = Yes (hotkey)
                time.sleep(0.6)
            else:
                needs_choice = True
                break
        time.sleep(POLL_INTERVAL_S)

    with _LOCK:
        s.asks += 1
        s.last_used = time.time()

    if needs_choice:
        dialog = "\n".join(l for l in _capture_screen(s.tmux).splitlines() if l.strip())[-1200:]
        return {"status": "needs_choice", "paced_s": round(paced_s),
                "dialog": dialog, "hint": "call claude_choose(session_id, '1'|'2'|'3')"}
    if not done:
        tail = "\n".join(l for l in captured.splitlines() if not _looks_like_chrome(l))[-MAX_OUTPUT_CHARS:]
        return {"status": "timeout", "paced_s": round(paced_s),
                "answer": tail, "note": f"no completion within {timeout}s"}

    # ---- post: verify / extract ----
    if mode == "hash":
        rc, data = _ssh_exec(s.target, f"cat {shlex.quote(path)}")
        _ssh_exec(s.target, f"rm -f {shlex.quote(path)}")  # cleanup the per-turn artifact
        if rc != 0:
            return {"status": "integrity_fail", "verified": False, "paced_s": round(paced_s),
                    "reason": "answer artifact unreadable at source"}
        h, n = _sha256_len(data)
        ch_h, ch_n = claimed
        verified = (h == ch_h and n == ch_n)
        answer = data.decode("utf-8", "replace").rstrip("\n")
        return {"status": "ok" if verified else "integrity_fail", "verified": verified,
                "paced_s": round(paced_s), "len": n, "sha256": h,
                "claimed_len": ch_n, "claimed_sha256": ch_h,
                "answer": answer[:MAX_OUTPUT_CHARS]}
    if mode == "frame":
        answer = _extract_between(captured, begin, end)
        return {"status": "ok", "verified": False, "paced_s": round(paced_s),
                "answer": answer[:MAX_OUTPUT_CHARS]}
    answer = _extract_answer(captured, line, marker)
    return {"status": "ok", "verified": False, "paced_s": round(paced_s),
            "answer": answer[:MAX_OUTPUT_CHARS]}


def _extract_answer(captured: str, prompt_line: str, marker: str) -> str:
    lines = captured.splitlines()
    # last marker-alone line bounds the end
    end = max((i for i, l in enumerate(lines) if l.strip() == marker), default=len(lines))
    # find the prompt echo (line containing the head of what we typed) before `end`
    head = prompt_line.strip()[:40]
    start = 0
    for i in range(end - 1, -1, -1):
        if head[:24] in lines[i]:
            start = i + 1
            break
    body = []
    for l in lines[start:end]:
        if _looks_like_chrome(l):
            continue
        # strip claude's leading assistant bullet ("● "/"⏺ ") so the answer is clean text
        body.append(re.sub(r"^\s*[●⏺•·]\s+", "", l))
    out = "\n".join(body).strip()
    return out or "(no textual answer captured — try claude_screen)"


# Zero-trust auto-approval: in hash mode, auto-confirm a permission dialog ONLY when it
# references this turn's own /tmp/cp_<nonce> artifact AND a safe verb, and contains no
# dangerous token. Anything else is surfaced to the caller.
_SAFE_VERB = re.compile(r"\b(sha256sum|wc|cat|Write|Writing|Create|Update|Append)\b", re.I)
_DANGER = re.compile(r"\b(rm\s+-rf|sudo|curl|wget|ssh|scp|nc|chmod|chown|mkfs|dd|eval|base64\s+-d)\b"
                     r"|>\s*/(?!tmp/cp_)", re.I)


def _auto_ok(dialog: str, nonce: str) -> bool:
    if f"/tmp/cp_{nonce}" not in dialog:
        return False
    if _DANGER.search(dialog):
        return False
    return bool(_SAFE_VERB.search(dialog))


def _extract_between(captured: str, begin: str, end: str) -> str:
    lines = captured.splitlines()
    try:
        bi = max(i for i, l in enumerate(lines) if l.strip() == begin)
        ei = min(i for i, l in enumerate(lines) if l.strip() == end and i > bi)
    except ValueError:
        return "(framing markers not found — inspect with claude_screen)"
    body = [re.sub(r"^\s*[●⏺•·]\s+", "", l) for l in lines[bi + 1:ei]]
    return "\n".join(body).strip()


@mcp.tool
def claude_choose(session_id: str, option: str) -> dict:
    """Answer a permission/selection dialog. option: a digit ('1'..'9') hotkey, or a key
    name: 'enter', 'up', 'down', 'esc'. Returns a short screen delta."""
    with _LOCK:
        s = _SESSIONS.get(session_id)
    if not s or not _tmux_running(s.tmux):
        return {"error": "unknown or dead session"}
    opt = option.strip().lower()
    keymap = {"enter": "Enter", "up": "Up", "down": "Down", "esc": "Escape", "tab": "BTab"}
    if opt in keymap:
        _send_key(s.tmux, keymap[opt])
    elif opt.isdigit():
        _send_text(s.tmux, opt)         # numeric hotkey selects+confirms in claude
    else:
        return {"error": "option must be a digit or one of enter/up/down/esc/tab"}
    time.sleep(1.0)
    with _LOCK:
        s.last_used = time.time()
    screen = "\n".join(l for l in _capture_screen(s.tmux).splitlines() if l.strip())[-1000:]
    return {"status": "ok", "screen": screen}


@mcp.tool
def claude_screen(session_id: str, mode: str = "tail", lines: int = 20) -> dict:
    """Minimal, ANSI-stripped view of a session. mode: 'tail' (last N non-empty lines),
    'screen' (current visible pane), 'full' (scrollback, capped)."""
    with _LOCK:
        s = _SESSIONS.get(session_id)
    if not s or not _tmux_running(s.tmux):
        return {"error": "unknown or dead session"}
    if mode == "screen":
        txt = _capture_screen(s.tmux)
    elif mode == "full":
        txt = _capture(s.tmux)[-MAX_OUTPUT_CHARS:]
    else:
        non_empty = [l for l in _capture(s.tmux).splitlines() if l.strip()]
        txt = "\n".join(non_empty[-max(1, min(lines, 200)):])
    return {"status": "ok", "content": txt[-MAX_OUTPUT_CHARS:]}


@mcp.tool
def claude_sessions() -> dict:
    """List active piloted sessions."""
    _reap_idle()
    with _LOCK:
        out = [{"session_id": s.sid, "target": s.target, "asks": s.asks,
                "age_s": round(time.time() - s.created),
                "idle_s": round(time.time() - s.last_used),
                "alive": _tmux_running(s.tmux)} for s in _SESSIONS.values()]
    return {"sessions": out, "count": len(out), "max": MAX_SESSIONS}


@mcp.tool
def claude_close(session_id: str) -> dict:
    """Exit claude and tear the session down."""
    with _LOCK:
        s = _SESSIONS.get(session_id)
    if not s:
        return {"error": "unknown session_id"}
    _kill(s)
    return {"status": "closed", "session_id": session_id}


# ------------------------------------------------------------------ optional bearer
def _maybe_install_auth() -> None:
    """If AUTH_TOKEN is set, gate every MCP request on a Bearer header. Fail CLOSED:
    if the token is set but the hook can't be wired, refuse to start (no false security)."""
    if not AUTH_TOKEN:
        LOG.info("AUTH_TOKEN unset — relying on Tailscale-only exposure as the boundary")
        return
    try:
        from fastmcp.server.middleware import Middleware, MiddlewareContext
        from fastmcp.server.dependencies import get_http_headers

        class _Bearer(Middleware):
            async def on_request(self, context: MiddlewareContext, call_next):
                hdrs = get_http_headers() or {}
                if hdrs.get("authorization", "") != f"Bearer {AUTH_TOKEN}":
                    raise PermissionError("missing/invalid Bearer token")
                return await call_next(context)

        mcp.add_middleware(_Bearer())
        LOG.info("Bearer auth enabled")
    except Exception as e:  # fail closed
        LOG.error("AUTH_TOKEN set but Bearer hook could not be installed: %s", e)
        sys.exit(2)


if __name__ == "__main__":
    LOG.info("claude-terminal-control on %s:%s%s  target=%s proxyjump=%s pacing=%s",
             HOST, PORT, PATH, SSH_TARGET, SSH_PROXYJUMP or "-", PACING_DEFAULT)
    _maybe_install_auth()
    mcp.run(transport="http", host=HOST, port=PORT, path=PATH)
