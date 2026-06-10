# whereami

[![CI](https://github.com/bestes206/claude-whereami/actions/workflows/ci.yml/badge.svg)](https://github.com/bestes206/claude-whereami/actions/workflows/ci.yml)

Live in-session orientation for [Claude Code](https://claude.com/claude-code),
in your statusline.

You come back to a session — after lunch, after a meeting, after twenty
seconds of reading something else — and the question is always the same:
*where am I?* What was this session for, what is it doing now, and is it
waiting on me? **whereami** answers that at a glance, continuously, for zero
tokens of context.

## What it shows

**Normal mode** — two quiet lines under every session:

```
parser retry logic · ⏱ 42m · ⊠ 63%
❯ ok now let's make the retry logic exponential with jitter and cap it at 5
```

Line 1 is the *gist* — a few words describing what the session is doing right
now, colored by drift (green: on track, amber: wandering, red: far from the
original goal) — plus elapsed time and context usage. Line 2 is your own last
message, live from the transcript.

**Peek mode** — press a hotkey and the statusline expands into a full
orientation panel for ~30 seconds, then collapses on its own:

```
drift 58 · CI retry backoff logic   (goal: in-session reorientation tool)
❯ ok now let's make the retry logic exponential with jitter and cap it
  at 5 attempts, and while you're in there rename the helper
⊙ your turn: choose a backoff strategy
⏱ 42m · ⊠ 63% · scored 4m ago
```

<!-- TODO: replace the mocks above with a real screenshot:
     ![whereami in a live session](docs/screenshot.png) -->

The panel adds the drift score, the distilled original goal, your last
message near-full, the *open loop* (`⊙ your turn:` — what the agent is
waiting on from you), honest time-based staleness, and — when drift and
context pressure are both high — a `· split?` hint that it's time for a
fresh session.

A bundled read-only `/whereami` skill remains for the deep view: the model's
own in-context summary of the session, designed to be `Esc Esc`-rewound away
without leaving an artifact.

## The niche

Claude Code shows you context usage, cost, and git state — mechanical
telemetry. Nothing native or community-built does live in-session *semantic*
orientation: goal-vs-now, drift, the open loop, a split recommendation
(checked against Claude Code 2.1.170, 2026-06-10). That's the gap this fills.
It's most useful if you run several long sessions in parallel and pay a
reorientation tax every time you switch.

## Install

### 1. The plugin

```
/plugin marketplace add bestes206/claude-whereami
/plugin install whereami@claude-whereami
```

This wires the Stop hook (turn counting + throttled orientation computes)
and the `/whereami` skill automatically. whereami is pure stdlib — no
dependencies, no build step, Python 3.9+.

### 2. The statusline (one manual settings edit)

Plugins can't set the main statusline, so add this to
`~/.claude/settings.json` yourself:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"$(ls -dt ~/.claude/plugins/cache/*/whereami/*/scripts/statusline.py | head -1)\"",
    "refreshInterval": 3
  }
}
```

The `ls -dt … | head -1` picks the newest installed plugin version, so the
wiring survives plugin updates. If you prefer an explicit path, use
`~/.claude/plugins/cache/claude-whereami/whereami/<version>/scripts/statusline.py`
and refresh it when the plugin updates.

`refreshInterval: 3` matters: it re-runs the statusline on a timer, which is
what lets the peek panel appear and collapse while the session is idle.
Without it everything still works, but the panel only updates on
conversation events.

### 3. The peek hotkey (30 seconds)

Peek is triggered by touching a file — no CLI, no daemon:

```sh
mkdir -p ~/.claude/whereami && touch ~/.claude/whereami/peek
```

Bind that one-liner to a hotkey (e.g. ⌥W) with Raycast, Hammerspoon, or
anything that can run a shell command — paste-ready snippets in
[docs/peek-hotkey.md](docs/peek-hotkey.md).

## How it works

```
Stop hook ──every 6 turns──▶ detached sidecar ──one Haiku call──▶ cache
                             (logged-in claude CLI)                 │
statusline renderer ◀──reads cache + transcript tail, no network───┘
```

- A **Stop hook** counts turns. Every 6 turns (or on a stale peek) it spawns
  a short-lived, detached sidecar process.
- The **sidecar** makes one Haiku call through your logged-in `claude` CLI
  and writes the result — score, gist, open loop, goal — to a per-session
  cache under `~/.claude/whereami/`. Model calls happen *only* in this
  detached child, never in anything latency-sensitive.
- The **renderer** is read-only and network-free: it reads the cache and the
  transcript tail and prints two lines (or the peek panel). It is built to
  never raise — a broken cache degrades to a dim placeholder, never a broken
  statusline.

**Cost:** orientation calls run on your Pro/Max subscription via the CLI —
no API key. Each is a single sub-cent Haiku call, at most one per 6 turns
per session, with a failure backoff that caps a persistently broken CLI at
~6 retries/hour. If a compute fails, the last good orientation is kept and
honestly ages ("scored 3h ago") rather than being papered over.

## Development

```sh
git clone https://github.com/bestes206/claude-whereami
cd claude-whereami
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q   # 117 tests, all offline
```

The editable install gives you `whereami-statusline` and `whereami-hook`
console scripts; [`.claude/settings.example.json`](.claude/settings.example.json)
shows the non-plugin wiring against a venv path. Tests stub the CLI and the
clock — the suite never touches the network or your real cache.

One deliberate duplication: `skills/whereami/SKILL.md` (shipped with the
plugin) and `.claude/skills/whereami/SKILL.md` (used when developing in this
repo) are two real copies of the same file, not a symlink — GitHub's zip
archives flatten symlinks, which would break zip-based plugin installs. Edit
them together; they change rarely.

## Uninstall

```
/plugin uninstall whereami@claude-whereami
```

Then remove the `statusLine` block from `~/.claude/settings.json` and, if
you want a clean slate, `rm -rf ~/.claude/whereami` (per-session orientation
caches).

## License

MIT © 2026 Bryan Estes
