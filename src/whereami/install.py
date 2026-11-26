"""One-command setup for whereami.

`whereami install` (or `python3 scripts/install.py`) wires the statusline and
Stop hook into ~/.claude/settings.json.

Design mirrors the rest of the package: the risky decisions are pure functions
(no I/O, fully unit-tested), and main() is a thin orchestration layer over them.
Stdlib only — no dependencies, Python 3.9+.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

# The version-proof statusline command for plugin installs: pick the newest
# installed plugin version, so the wiring survives `/plugin` updates.
STATUSLINE_GLOB = (
    'python3 "$(ls -dt ~/.claude/plugins/cache/*/whereami/*/scripts/'
    'statusline.py | head -1)"'
)

# Both wired statusline forms reference whereami; a foreign statusline won't.
_OURS_MARKERS = ("whereami", "statusline.py")


# --- command resolution --------------------------------------------------

def is_plugin_path(path) -> bool:
    """True when running from a `/plugin`-installed copy under the cache."""
    return "/.claude/plugins/cache/" in str(path)


def resolve_commands(scripts_dir=None, argv0=None,
                     which: Callable[[str], Optional[str]] = shutil.which
                     ) -> Tuple[str, Optional[str], bool]:
    """Return (statusline_cmd, hook_cmd_or_None, in_plugin) for the way whereami
    was installed.

    - plugin cache  → glob command; hook_cmd is None (the plugin's bundled
      hooks.json already wires the Stop hook — adding another would double-fire).
    - clone/fixed   → `python3 "<scripts>/statusline.py"` + matching hook shim.
    - console (pipx/venv) → the absolute path of the sibling console scripts.
    """
    if scripts_dir is not None:
        sd = Path(scripts_dir)
        if is_plugin_path(sd):
            return STATUSLINE_GLOB, None, True
        sl = 'python3 "{}"'.format(sd / "statusline.py")
        hook = 'python3 "{}"'.format(sd / "hook.py")
        return sl, hook, False

    # Console-script install: locate the sibling whereami-statusline/-hook.
    bindir = Path(argv0).resolve().parent if argv0 else None

    def _bin(name: str) -> str:
        if bindir is not None:
            cand = bindir / name
            if cand.exists():
                return str(cand)
        found = which(name)
        return found if found else name

    return _bin("whereami-statusline"), _bin("whereami-hook"), False


# --- settings.json patching (pure) --------------------------------------

def _statusline_block(cmd: str) -> dict:
    # No refreshInterval: the statusline redraws on Claude Code's own events,
    # which is all the gist and last-message lines need.
    return {"type": "command", "command": cmd}


def _is_ours(cmd: str) -> bool:
    return any(m in cmd for m in _OURS_MARKERS)


def _hook_is_ours(cmd: str) -> bool:
    return "whereami-hook" in cmd or "hook.py" in cmd


def compute_settings_update(current: dict, *, statusline_cmd: str,
                            hook_cmd: Optional[str], force: bool
                            ) -> Tuple[dict, dict]:
    """Return (new_settings, report) without mutating `current`.

    statusline report ∈ {set, unchanged, kept_foreign};
    hook report ∈ {added, unchanged, skipped_plugin}.
    """
    data = json.loads(json.dumps(current))  # deep copy via the JSON it already is
    report = {}

    sl = data.get("statusLine")
    existing = sl.get("command") if isinstance(sl, dict) else None
    wanted = _statusline_block(statusline_cmd)
    if existing is None:
        data["statusLine"] = wanted
        report["statusline"] = "set"
    elif _is_ours(existing):
        if sl == wanted:
            report["statusline"] = "unchanged"
        else:
            data["statusLine"] = wanted
            report["statusline"] = "set"
    elif force:
        data["statusLine"] = wanted
        report["statusline"] = "set"
    else:
        report["statusline"] = "kept_foreign"

    if hook_cmd is None:
        report["hook"] = "skipped_plugin"
    else:
        stop = data.setdefault("hooks", {}).setdefault("Stop", [])
        already = any(_hook_is_ours(h.get("command", "")) or h.get("command") == hook_cmd
                      for group in stop for h in group.get("hooks", []))
        if already:
            report["hook"] = "unchanged"
        else:
            stop.append({"hooks": [{"type": "command", "command": hook_cmd}]})
            report["hook"] = "added"

    return data, report


# --- settings.json I/O ---------------------------------------------------

def load_settings(path: Path) -> dict:
    """Load settings, treating missing/empty as {}. Malformed JSON raises rather
    than risk clobbering a file we can't safely parse."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("settings.json is not valid JSON: {}".format(e))


def backup(path: Path) -> Optional[Path]:
    """Copy an existing file to `<name>.bak-whereami`. No-op if missing."""
    path = Path(path)
    if not path.exists():
        return None
    dst = path.with_name(path.name + ".bak-whereami")
    shutil.copy2(path, dst)
    return dst


def save_settings(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# --- orchestration -------------------------------------------------------

def _settings_path(home: Path) -> Path:
    return Path(home) / ".claude" / "settings.json"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whereami install",
        description="Wire whereami's statusline and Stop hook.")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing non-whereami statusline")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would change without writing anything")
    return p


def main(argv=None, *, scripts_dir=None) -> int:
    args = build_arg_parser().parse_args(argv)
    home = Path(os.path.expanduser("~"))
    out = sys.stdout

    statusline_cmd, hook_cmd, in_plugin = resolve_commands(
        scripts_dir=scripts_dir, argv0=(sys.argv[0] if argv is None else None))

    settings_path = _settings_path(home)
    current = load_settings(settings_path)
    new, report = compute_settings_update(
        current, statusline_cmd=statusline_cmd, hook_cmd=hook_cmd,
        force=args.force)

    print("whereami install", file=out)
    print("  statusline: {}".format(report["statusline"]), file=out)
    print("  stop hook:  {}".format(report["hook"]), file=out)
    if report["statusline"] == "kept_foreign":
        print("  ! a different statusLine is set; re-run with --force to replace it.",
              file=out)

    if args.dry_run:
        print("  (dry run — no files written)", file=out)
        return 0

    if new != current:
        backup(settings_path)
        save_settings(settings_path, new)
    print("Done. Restart Claude Code (or wait for the next statusline tick).",
          file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
