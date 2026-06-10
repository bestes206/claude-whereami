"""Tests for `whereami install` — the one-command setup.

Everything here is offline and filesystem-isolated (tmp_path + a fake $HOME).
The installer never runs brew, never touches the real ~/.claude, and never
prompts in these tests: side effects are injected (path_exists, which, argv0)
and the risky decisions (settings patching, hotkey choice) are pure functions.
"""
import io
import json
import os
from pathlib import Path

import pytest

from whereami import install


class _Proc:
    def __init__(self, returncode):
        self.returncode = returncode


# --- statusline / hook command resolution -------------------------------

def test_plugin_install_uses_glob_command_and_skips_hook():
    # In a plugin-cache location the wired command must be the version-proof
    # `ls -dt … | head -1` glob, and the Stop hook is left to the plugin's
    # bundled hooks.json (so the installer must NOT add a second one).
    scripts = Path("/Users/x/.claude/plugins/cache/claude-whereami/whereami/0.2.0/scripts")
    sl, hook, in_plugin = install.resolve_commands(scripts_dir=scripts,
                                                   argv0=None, which=lambda n: None)
    assert in_plugin is True
    assert "ls -dt" in sl and "statusline.py" in sl
    assert hook is None


def test_clone_install_uses_absolute_python_paths_and_adds_hook():
    scripts = Path("/home/dev/claude-whereami/scripts")
    sl, hook, in_plugin = install.resolve_commands(scripts_dir=scripts,
                                                   argv0=None, which=lambda n: None)
    assert in_plugin is False
    assert sl == 'python3 "/home/dev/claude-whereami/scripts/statusline.py"'
    assert hook == 'python3 "/home/dev/claude-whereami/scripts/hook.py"'


def test_console_install_uses_sibling_bin_scripts():
    # Run as the `whereami-install` console script from a venv/pipx bin dir:
    # wire the sibling console scripts by their absolute path (matches the
    # existing settings.example.json and the live editable setup).
    found = {"whereami-statusline": "/opt/venv/bin/whereami-statusline",
             "whereami-hook": "/opt/venv/bin/whereami-hook"}
    sl, hook, in_plugin = install.resolve_commands(
        scripts_dir=None, argv0="/opt/venv/bin/whereami-install",
        which=lambda n: found.get(n))
    assert in_plugin is False
    assert sl == "/opt/venv/bin/whereami-statusline"
    assert hook == "/opt/venv/bin/whereami-hook"


# --- settings.json patching (the risky part) ----------------------------

def _block(cmd):
    return {"type": "command", "command": cmd, "refreshInterval": 3}


def test_statusline_set_on_empty_settings():
    new, report = install.compute_settings_update(
        {}, statusline_cmd="CMD", hook_cmd=None, force=False)
    assert new["statusLine"] == _block("CMD")
    assert report["statusline"] == "set"


def test_statusline_unchanged_when_already_ours_and_identical():
    current = {"statusLine": _block('python3 "/x/scripts/statusline.py"')}
    new, report = install.compute_settings_update(
        current, statusline_cmd='python3 "/x/scripts/statusline.py"',
        hook_cmd=None, force=False)
    assert report["statusline"] == "unchanged"


def test_statusline_replaced_when_ours_but_stale_path():
    current = {"statusLine": _block('python3 "/old/scripts/statusline.py"')}
    new, report = install.compute_settings_update(
        current, statusline_cmd='python3 "/new/scripts/statusline.py"',
        hook_cmd=None, force=False)
    assert new["statusLine"]["command"] == 'python3 "/new/scripts/statusline.py"'
    assert report["statusline"] == "set"


def test_foreign_statusline_kept_without_force():
    current = {"statusLine": _block("my-own-fancy-statusline")}
    new, report = install.compute_settings_update(
        current, statusline_cmd="CMD", hook_cmd=None, force=False)
    assert new["statusLine"]["command"] == "my-own-fancy-statusline"
    assert report["statusline"] == "kept_foreign"


def test_foreign_statusline_overwritten_with_force():
    current = {"statusLine": _block("my-own-fancy-statusline")}
    new, report = install.compute_settings_update(
        current, statusline_cmd="CMD", hook_cmd=None, force=True)
    assert new["statusLine"]["command"] == "CMD"
    assert report["statusline"] == "set"


def test_unrelated_settings_keys_are_preserved():
    current = {"theme": "dark", "permissions": {"allow": ["Bash"]}}
    new, _ = install.compute_settings_update(
        current, statusline_cmd="CMD", hook_cmd=None, force=False)
    assert new["theme"] == "dark"
    assert new["permissions"] == {"allow": ["Bash"]}


def test_compute_does_not_mutate_input():
    current = {"statusLine": _block("foreign")}
    install.compute_settings_update(current, statusline_cmd="CMD",
                                    hook_cmd="HOOK", force=True)
    assert current["statusLine"]["command"] == "foreign"  # untouched


def test_hook_added_when_absent():
    new, report = install.compute_settings_update(
        {}, statusline_cmd="CMD", hook_cmd="HOOK", force=False)
    stop = new["hooks"]["Stop"]
    cmds = [h["command"] for group in stop for h in group["hooks"]]
    assert "HOOK" in cmds
    assert report["hook"] == "added"


def test_hook_not_duplicated_on_reinstall():
    first, _ = install.compute_settings_update(
        {}, statusline_cmd="CMD", hook_cmd="HOOK", force=False)
    second, report = install.compute_settings_update(
        first, statusline_cmd="CMD", hook_cmd="HOOK", force=False)
    stop = second["hooks"]["Stop"]
    ours = [h for group in stop for h in group["hooks"] if h["command"] == "HOOK"]
    assert len(ours) == 1
    assert report["hook"] == "unchanged"


def test_hook_preserves_existing_foreign_stop_hooks():
    current = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python3 ~/.claude/hooks/journal.py"}]}]}}
    new, _ = install.compute_settings_update(
        current, statusline_cmd="CMD", hook_cmd="HOOK", force=False)
    cmds = [h["command"] for group in new["hooks"]["Stop"] for h in group["hooks"]]
    assert "python3 ~/.claude/hooks/journal.py" in cmds  # not clobbered
    assert "HOOK" in cmds


def test_hook_skipped_when_command_is_none():
    new, report = install.compute_settings_update(
        {}, statusline_cmd="CMD", hook_cmd=None, force=False)
    assert "hooks" not in new
    assert report["hook"] == "skipped_plugin"


# --- load / backup -------------------------------------------------------

def test_load_missing_settings_returns_empty(tmp_path):
    assert install.load_settings(tmp_path / "nope.json") == {}


def test_load_empty_file_returns_empty(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("   \n")
    assert install.load_settings(p) == {}


def test_load_malformed_settings_raises_not_clobbers(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json ")
    with pytest.raises(ValueError):
        install.load_settings(p)


def test_backup_copies_existing_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"a":1}')
    dst = install.backup(p)
    assert dst == p.with_name("settings.json.bak-whereami")
    assert dst.read_text() == '{"a":1}'


def test_backup_noop_when_missing(tmp_path):
    assert install.backup(tmp_path / "settings.json") is None


# --- hotkey detection ----------------------------------------------------

def test_detect_hammerspoon_via_app(tmp_path):
    exists = lambda p: p == "/Applications/Hammerspoon.app"
    assert install.detect_hotkey_tool(tmp_path, path_exists=exists) == "hammerspoon"


def test_detect_hammerspoon_via_init_lua(tmp_path):
    init = tmp_path / ".hammerspoon" / "init.lua"
    exists = lambda p: p == str(init)
    assert install.detect_hotkey_tool(tmp_path, path_exists=exists) == "hammerspoon"


def test_detect_raycast(tmp_path):
    exists = lambda p: p == "/Applications/Raycast.app"
    assert install.detect_hotkey_tool(tmp_path, path_exists=exists) == "raycast"


def test_detect_none(tmp_path):
    assert install.detect_hotkey_tool(tmp_path, path_exists=lambda p: False) is None


def test_hammerspoon_preferred_over_raycast_when_both(tmp_path):
    assert install.detect_hotkey_tool(tmp_path, path_exists=lambda p: True) == "hammerspoon"


# --- hotkey choice decision ---------------------------------------------

def test_decide_auto_uses_detected():
    assert install.decide_hotkey(detected="raycast", requested="auto") == "raycast"


def test_decide_auto_with_nothing_detected_means_prompt():
    assert install.decide_hotkey(detected=None, requested="auto") == "prompt"


def test_decide_explicit_flag_overrides_detection():
    assert install.decide_hotkey(detected="hammerspoon", requested="none") == "none"
    assert install.decide_hotkey(detected=None, requested="hammerspoon") == "hammerspoon"


def test_decide_no_input_never_prompts():
    # With --no-input and nothing detected, we skip rather than block on a prompt.
    assert install.decide_hotkey(detected=None, requested="auto",
                                 no_input=True) == "none"


# --- hammerspoon wiring --------------------------------------------------

def test_wire_hammerspoon_appends_marked_binding(tmp_path):
    init = tmp_path / ".hammerspoon" / "init.lua"
    result = install.wire_hammerspoon(init, peek_path="/home/u/.claude/whereami/peek")
    assert result == "added"
    text = init.read_text()
    assert install.HS_MARKER in text
    assert "/home/u/.claude/whereami/peek" in text
    assert 'hs.hotkey.bind' in text and '"alt"' in text and '"W"' in text


def test_wire_hammerspoon_is_idempotent(tmp_path):
    init = tmp_path / ".hammerspoon" / "init.lua"
    install.wire_hammerspoon(init, peek_path="/p")
    before = init.read_text()
    assert install.wire_hammerspoon(init, peek_path="/p") == "already"
    assert init.read_text() == before  # no duplicate append


def test_wire_hammerspoon_preserves_existing_config(tmp_path):
    init = tmp_path / ".hammerspoon" / "init.lua"
    init.parent.mkdir(parents=True)
    init.write_text('hs.alert.show("hi")\n')
    install.wire_hammerspoon(init, peek_path="/p")
    assert 'hs.alert.show("hi")' in init.read_text()


# --- raycast wiring ------------------------------------------------------

def test_wire_raycast_writes_executable_script(tmp_path):
    script = install.wire_raycast(tmp_path / "scripts", peek_path="/home/u/peek")
    assert script.exists()
    body = script.read_text()
    assert "@raycast.title whereami peek" in body
    assert "touch /home/u/peek" in body
    assert os.access(script, os.X_OK)  # chmod +x so Raycast can run it


# --- main() orchestration ------------------------------------------------

def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    scripts = Path("/clone/scripts")
    rc = install.main(["--dry-run", "--hotkey", "none"], scripts_dir=scripts)
    assert rc == 0
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".claude" / "whereami").exists()


def test_main_wires_statusline_and_creates_peek_dir(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    scripts = Path("/clone/scripts")
    rc = install.main(["--hotkey", "none"], scripts_dir=scripts)
    assert rc == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["statusLine"]["command"] == 'python3 "/clone/scripts/statusline.py"'
    assert settings["statusLine"]["refreshInterval"] == 3
    # clone install (non-plugin) also wires the Stop hook
    cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert 'python3 "/clone/scripts/hook.py"' in cmds
    assert (home / ".claude" / "whereami").is_dir()


def test_main_backs_up_existing_settings_before_writing(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "dark"}')
    install.main(["--hotkey", "none"], scripts_dir=Path("/clone/scripts"))
    backup = settings.with_name("settings.json.bak-whereami")
    assert json.loads(backup.read_text()) == {"theme": "dark"}  # original preserved
    assert json.loads(settings.read_text())["theme"] == "dark"  # and carried forward


# --- hammerspoon reload (graceful fallback) ------------------------------

def test_reload_falls_back_to_manual_hint_when_cli_errors():
    # `hs` exists but the reload fails (Hammerspoon not running / no ipc) — show
    # the manual ⌃⌥⌘R hint instead of leaking the raw subprocess error.
    out = io.StringIO()
    install._reload_hammerspoon(out, runner=lambda *a, **k: _Proc(1),
                                which=lambda n: "/usr/local/bin/hs")
    assert "⌃⌥⌘R" in out.getvalue()


def test_reload_reports_success_quietly():
    out = io.StringIO()
    install._reload_hammerspoon(out, runner=lambda *a, **k: _Proc(0),
                                which=lambda n: "/usr/local/bin/hs")
    assert "reloaded" in out.getvalue().lower()
    assert "⌃⌥⌘R" not in out.getvalue()


def test_reload_without_cli_gives_manual_hint():
    out = io.StringIO()
    install._reload_hammerspoon(out, runner=lambda *a, **k: _Proc(0),
                                which=lambda n: None)
    assert "⌃⌥⌘R" in out.getvalue()
