# Skill: piloting a remote `claude` TUI

You are an agent that wants to delegate work to a *second* `claude` running on another box,
interactively, without burning your own context on terminal frames. Use these tools.

## The happy path

1. `claude_open()` → returns `{session_id, ready, banner}`. The trust dialog is auto-cleared.
2. `claude_ask(session_id, "<your prompt>")` → returns `{status:"ok", answer:"…"}`.
   The `answer` is already ANSI-stripped and bounded — just read it. Do **not** call
   `claude_screen` after a successful ask; the answer is the answer.
3. Repeat `claude_ask`. When finished, `claude_close(session_id)`.

## Integrity (v0.2 — Verifiable Framed Payload)

`claude_ask` returns a **verified** answer by default. Modes via `integrity=`:

- `"hash"` (default): claude writes the full answer to a file on the target and prints a
  marker with `sha256`+`len` from real tools; the facade reads that file **out-of-band** and
  **re-hashes** it. You get `{"status":"ok","verified":true,"sha256":…}` — or
  `{"status":"integrity_fail"}` if anything was truncated/altered (fail-closed). Byte-exact,
  so **tables and code survive intact**. Needs shell approval, auto-granted only for the
  turn's own `/tmp/cp_<nonce>` file.
- `"frame"`: answer wrapped between per-nonce BEGIN/END markers in the pane; exact extraction,
  no tools/approvals, `verified:false`. **Plaintext only** — claude renders markdown/tables in
  the pane, which mangles the markers; for tables/code/anything structured, use `"hash"`.
- `"none"`: legacy chrome-filtered scrape.

Rule of thumb: keep `"hash"` for anything you'll act on; drop to `"frame"` for quick
interactive reads where approvals are inconvenient.

## Handling dialogs

If `claude_ask` returns `{status:"needs_choice", dialog}`, the remote claude is asking for
permission. Decide, then `claude_choose(session_id, "1")` (digit hotkey) — or `"enter"`,
`"down"`, `"esc"`. After choosing, call `claude_ask` again with an empty/continuation prompt
only if needed; usually the original turn resumes on its own — poll once with
`claude_screen(session_id, "tail")`.

## Pacing (delay-er)

For autonomous loops, `claude_ask(session_id, prompt, pace=true)` blocks a random 2–9 min
before sending. Use it to look human-paced and stay friendly to rate limits. Leave it off
(`pace` omitted) for interactive, hands-on work.

## Cost discipline (why this exists)

- Prefer `claude_ask` (returns the answer) over `claude_screen` (returns a view).
- Keep prompts single-purpose; ask the remote claude to write long output to a file and
  return only a summary.
- Use plan-mode style prompts ("propose, don't execute") to avoid permission round-trips.

## Gotchas

- Newlines submit in the TUI, so a prompt is sent as a single line; the facade appends the
  sentinel instruction for you.
- If `claude_ask` returns `{status:"timeout"}`, the marker never printed — inspect with
  `claude_screen(session_id, "screen")` and consider a longer `timeout_s`.
