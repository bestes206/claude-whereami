# whereami

[![CI](https://github.com/bestes206/claude-whereami/actions/workflows/ci.yml/badge.svg)](https://github.com/bestes206/claude-whereami/actions/workflows/ci.yml)

A two-line [Claude Code](https://claude.com/claude-code) statusline that answers
*where am I?* at a glance, for zero tokens of context.

```
parser retry logic · ⏱ 42m · ⊠ 63%
❯ ok now let's make the retry logic exponential with jitter and cap it at 5
```

**Line 1** is the *gist*: a few words on what the session is doing right now,
colored by drift from the session's original goal (green: on track, amber:
drifting, red: off track; red is the cue to start a fresh session). Then
elapsed time and context usage.

**Line 2** is the last message *you* typed, read live from the transcript.
Background-task notices, messages from other sessions, compact summaries and
slash-command output are skipped; messages you type while Claude is working
are included.

## What Claude Code does natively, and what this adds

Checked against Claude Code 2.1.332 (2026-11-25):

| Need | Native | whereami |
|---|---|---|
| "Where was I?" summary on return | Session recap (automatic after 3+ min away) and `/recap` | No |
| Last prompt + final answer, condensed | `/focus` (fullscreen only) | No |
| Session name | Auto title from the first prompt, `/rename` | No |
| Always-visible last message you typed | No | Line 2 |
| Live "what is it doing now" + drift color | No | Line 1 |

## Install

As a Claude Code plugin:

```
/plugin marketplace add bestes206/claude-whereami
/plugin install whereami@claude-whereami
```

Or standalone with pipx (or pip):

```sh
pipx install git+https://github.com/bestes206/claude-whereami
```

Then wire the statusline (and, for non-plugin installs, the Stop hook):

```sh
whereami install
```

Installed as a plugin, so `whereami` isn't on your PATH? Run the bundled copy:

```sh
python3 "$(ls -dt ~/.claude/plugins/cache/*/whereami/*/scripts/install.py | head -1)"
```

The installer backs up `~/.claude/settings.json` first and won't replace a
statusline that isn't whereami's unless you pass `--force`. `--dry-run` shows
what would change. Pure stdlib, Python 3.9+.

## How it works

```
Stop hook ──every 6 turns / on return from idle──▶ detached child ──one Haiku call──▶ cache
                                                                                         │
statusline renderer ◀──────reads cache + transcript, no network─────────────────────────┘
```

- The **Stop hook** counts turns. Every 6 turns, or when you come back after
  `WHEREAMI_IDLE_MIN` minutes away (default `10`), it starts a short-lived
  detached process. Headless sessions (`claude -p`, SDK) are never scored.
- That process makes **one Haiku call** through your logged-in `claude` CLI
  (your subscription, no API key) and writes the score and gist to
  `~/.claude/whereami/<session>.json`. The call is stripped to ~1,300 input
  tokens with thinking off, about 2 seconds end to end, and passes
  `--no-session-persistence` so it leaves no session behind.
- The **renderer** is read-only and network-free. It never raises: a broken
  cache shows a dim placeholder, never a broken statusline.

If a scoring call fails, the last good gist stays and retries back off for
10 minutes.

Set `WHEREAMI_SHOW_COST=1` to add session cost to line 1.

## Development

```sh
git clone https://github.com/bestes206/claude-whereami
cd claude-whereami
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q   # all offline; tests never touch your real cache or CLI
```

[`.claude/settings.example.json`](.claude/settings.example.json) shows the
non-plugin wiring against a venv path.

whereami reads Claude Code's transcript JSONL, which Anthropic documents as an
internal format that can change between versions. The tests use entry shapes
copied from real transcripts; if line 2 starts showing system text after a
Claude Code update, that is the place to look.

## Uninstall

```
/plugin uninstall whereami@claude-whereami
```

Then remove the `statusLine` block (and any whereami Stop hook) from
`~/.claude/settings.json`, and `rm -rf ~/.claude/whereami`.

## License

MIT © 2026 Bryan Estes
