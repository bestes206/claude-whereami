"""One-command setup for whereami.

`whereami install` (or `python3 scripts/install.py`) wires the statusline and
Stop hook into ~/.claude/settings.json, creates the peek directory, and sets up
the ⌥W peek hotkey — auto-detecting Hammerspoon or Raycast, and offering to
install one if neither is present.

Design mirrors the rest of the package: the risky decisions are pure functions
(no I/O, fully unit-tested), and main() is a thin orchestration layer over them.
Stdlib only — no dependencies, Python 3.9+.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

# The version-proof statusline command for plugin installs: pick the newest
# installed plugin version, so the wiring survives `/plugin` updates.
STATUSLINE_GLOB = (
    'python3 "$(ls -dt ~/.claude/plugins/cache/*/whereami/*/scripts/'
    'statusline.py | head -1)"'
)

HS_MARKER = "-- whereami peek hotkey"

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
    return {"type": "command", "command": cmd, "refreshInterval": 3}


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


# --- hotkey detection & choice ------------------------------------------

def detect_hotkey_tool(home: Path,
                       path_exists: Callable[[str], bool] = os.path.exists
                       ) -> Optional[str]:
    """Return 'hammerspoon', 'raycast', or None. Hammerspoon wins ties — it's
    the only one we can wire end-to-end with no GUI step."""
    if (path_exists("/Applications/Hammerspoon.app")
            or path_exists(str(Path(home) / ".hammerspoon" / "init.lua"))):
        return "hammerspoon"
    if path_exists("/Applications/Raycast.app"):
        return "raycast"
    return None


def decide_hotkey(*, detected: Optional[str], requested: str,
                  no_input: bool = False) -> str:
    """Resolve the --hotkey flag against what's installed.

    Returns one of: 'hammerspoon', 'raycast', 'none', or 'prompt' (ask the user
    whether to install a tool). 'prompt' never escapes when --no-input is set.
    """
    if requested != "auto":
        return requested
    if detected is not None:
        return detected
    return "none" if no_input else "prompt"


# --- hotkey wiring -------------------------------------------------------

def hammerspoon_snippet(peek_path: str) -> str:
    return (
        "\n{marker}\n"
        'hs.hotkey.bind({{"alt"}}, "W", function() '
        'hs.execute("touch {peek}") end)\n'
    ).format(marker=HS_MARKER, peek=peek_path)


def wire_hammerspoon(init_path: Path, peek_path: str) -> str:
    """Append a marker-guarded ⌥W binding to init.lua. Idempotent."""
    init_path = Path(init_path)
    text = init_path.read_text() if init_path.exists() else ""
    if HS_MARKER in text:
        return "already"
    init_path.parent.mkdir(parents=True, exist_ok=True)
    init_path.write_text(text + hammerspoon_snippet(peek_path))
    return "added"


def raycast_script_content(peek_path: str) -> str:
    return (
        "#!/bin/bash\n"
        "# @raycast.schemaVersion 1\n"
        "# @raycast.title whereami peek\n"
        "# @raycast.mode silent\n"
        "# @raycast.packageName whereami\n"
        "touch {}\n"
    ).format(peek_path)


def wire_raycast(scripts_dir: Path, peek_path: str) -> Path:
    scripts_dir = Path(scripts_dir)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script = scripts_dir / "whereami-peek.sh"
    script.write_text(raycast_script_content(peek_path))
    script.chmod(0o755)
    return script


# --- orchestration -------------------------------------------------------

def _settings_path(home: Path) -> Path:
    return Path(home) / ".claude" / "settings.json"


def _peek_path(home: Path) -> str:
    return str(Path(home) / ".claude" / "whereami" / "peek")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whereami install",
        description="Wire whereami's statusline, hook, and peek hotkey.")
    p.add_argument("--hotkey", choices=["auto", "hammerspoon", "raycast", "none"],
                   default="auto", help="hotkey tool (default: auto-detect)")
    p.add_argument("--yes", action="store_true",
                   help="accept install prompts (e.g. brew) non-interactively")
    p.add_argument("--no-input", action="store_true",
                   help="never prompt; skip the hotkey if no tool is detected")
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

    detected = detect_hotkey_tool(home)
    choice = decide_hotkey(detected=detected, requested=args.hotkey,
                           no_input=args.no_input)

    if args.dry_run:
        print("  (dry run — no files written)", file=out)
        print("  hotkey: would use {}".format(choice), file=out)
        return 0

    if new != current:
        backup(settings_path)
        save_settings(settings_path, new)
    Path(_peek_path(home)).parent.mkdir(parents=True, exist_ok=True)

    _apply_hotkey(choice, home, out, no_input=args.no_input, yes=args.yes)
    print("Done. Restart Claude Code (or wait for the next statusline tick).",
          file=out)
    return 0


def _apply_hotkey(choice: str, home: Path, out, *, no_input: bool, yes: bool) -> None:
    peek = _peek_path(home)
    if choice == "none":
        print("  hotkey: skipped (trigger peek with `touch {}`)".format(peek),
              file=out)
        return
    if choice == "prompt":
        choice = _prompt_install_tool(out) if not no_input else "none"
        if choice in (None, "none"):
            print("  hotkey: skipped (re-run `whereami install` to set one up)",
                  file=out)
            return
        _brew_install(choice, out, yes=yes)
    if choice == "hammerspoon":
        result = wire_hammerspoon(home / ".hammerspoon" / "init.lua", peek)
        print("  hotkey: ⌥W via Hammerspoon ({})".format(result), file=out)
        _reload_hammerspoon(out)
    elif choice == "raycast":
        script = wire_raycast(home / ".raycast-scripts", peek)
        print("  hotkey: Raycast script written to {}".format(script), file=out)
        print("  → In Raycast: add this folder under Extensions ▸ Script Commands,",
              file=out)
        print("    then assign a hotkey to 'whereami peek'.", file=out)


def _prompt_install_tool(out) -> Optional[str]:
    print("\nNo hotkey tool found. Install one to enable the ⌥W peek hotkey?",
          file=out)
    print("  [1] Hammerspoon  (recommended — fully wires ⌥W, no extra clicks)",
          file=out)
    print("  [2] Raycast", file=out)
    print("  [3] Skip", file=out)
    try:
        answer = input("Choice [1/2/3]: ").strip()
    except EOFError:
        return None
    return {"1": "hammerspoon", "2": "raycast"}.get(answer)


def _brew_install(cask: str, out, *, yes: bool) -> None:
    if not shutil.which("brew"):
        print("  Homebrew not found. Install {} from its website, then re-run "
              "`whereami install`.".format(cask), file=out)
        return
    if not yes:
        try:
            if input("Run `brew install --cask {}`? [y/N]: ".format(cask)
                     ).strip().lower() not in ("y", "yes"):
                return
        except EOFError:
            return
    subprocess.run(["brew", "install", "--cask", cask], check=False)


def _reload_hammerspoon(out, runner=subprocess.run, which=shutil.which) -> None:
    """Reload via the `hs` CLI when it works; otherwise (no CLI, or the reload
    errors because Hammerspoon isn't running with the ipc module) fall back to
    the manual hint rather than leaking the raw subprocess error."""
    if which("hs"):
        proc = runner(["hs", "-c", "hs.reload()"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if getattr(proc, "returncode", 1) == 0:
            print("  → Hammerspoon reloaded.", file=out)
            return
    print("  → Reload Hammerspoon (⌃⌥⌘R) to activate the hotkey.", file=out)


if __name__ == "__main__":
    raise SystemExit(main())
