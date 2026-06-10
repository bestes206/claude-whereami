# whereami token diet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut whereami's per-orientation-compute token footprint ~30× and latency ~30× by stripping the `claude -p` invocation and disabling extended thinking (behind a capability probe with graceful fallback), then add a return-from-idle recompute trigger so the gist is fresh the instant you come back.

**Architecture:** Phase 1 factors the subprocess call behind an `_invoke(args, env) -> dict` seam, builds argv from a `STRIP_FLAGS` constant, sets `MAX_THINKING_TOKENS=0`, and gates the stripped path on a version-keyed capability cache (`capabilities.json`) populated by a reasoning-prompt probe that also confirms thinking is off. Older CLIs that reject the flags fall back to today's exact unstripped behavior. Phase 2 reads transcript `timestamp` fields to fire a recompute when the human-to-human gap crosses `WHEREAMI_IDLE_MIN` (default 10 min), additive to the existing 6-turn cadence.

**Tech Stack:** Python 3.9+ (no `X | Y` unions — use `Optional`/`Union`), stdlib only, pytest. CI matrix is 3.9 + 3.14.

**Source of truth:** `docs/superpowers/specs/2026-06-10-whereami-token-diet-design.md`.

> **Annotation — measured at implementation (2026-06-10, CLI 2.1.172).** The
> per-call figures reproduced throughout this plan (`~870` input / `~64` output /
> `~0.12¢` / `~1 s`, and "~30×") are the spec's design-time estimate. Measured on
> the real orientation prompt, the stripped call is **~1,300 input / 33 output
> tokens, ~1 s model time (~2 s end-to-end), ~$0.0015 (≈0.15¢)** vs **~30,000
> context / ~2,300 output / ~23 s / ~3.8¢** unstripped — a ~20–25× cut. The
> source comments and README/CHANGELOG carry the measured numbers; see the
> annotated table in the spec.

---

## File Structure

- `src/whereami/cache.py` — add `load_caps`/`save_caps` (global `capabilities.json`, same atomic-write pattern); add `parse_iso` (ISO→datetime with `Z`-normalization) and route `ts_to_epoch` through it.
- `src/whereami/drift.py` — add `_invoke` seam, `_cli_version`, `STRIP_FLAGS`/`THINKING_OFF_ENV`/`_build_argv`, probe helpers + `_stripped_supported`, `_run_claude` rewrite; add `_read_idle_threshold`/`IDLE_THRESHOLD`, `returned_from_idle`, `hook_due` idle param, `run_hook` wiring.
- `src/whereami/transcript.py` — add `human_turn_timestamps` (imports `cache.parse_iso`).
- `src/whereami/install.py` — warm the capability probe during install (injectable, best-effort).
- `tests/test_cache.py`, `tests/test_drift.py`, `tests/test_transcript.py`, `tests/test_install.py` — new tests per task.
- `README.md`, `CHANGELOG.md` — cost/latency numbers, `WHEREAMI_IDLE_MIN`, min-CLI note.

**Out of scope (do not touch):** `src/whereami/statusline.py`, the `/whereami` skill, peek rendering, drift bands, `THROTTLE_TURNS`. No local models/embeddings. No new dependencies.

---

# PHASE 1 — Strip the invocation + disable thinking

## Task 1: Capability cache (`load_caps` / `save_caps`)

**Files:**
- Modify: `src/whereami/cache.py`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache.py`:

```python
def test_caps_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "2.1.172", "stripped_ok": True})
    assert cache.load_caps() == {"cli_version": "2.1.172", "stripped_ok": True}


def test_caps_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.load_caps() == {}


def test_caps_corrupt_or_non_dict_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.caps_path().write_text("{not json")
    assert cache.load_caps() == {}
    cache.caps_path().write_text("[1, 2]")
    assert cache.load_caps() == {}


def test_caps_save_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "x", "stripped_ok": False})
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert cache.caps_path().name == "capabilities.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cache.py -k caps -q`
Expected: FAIL with `AttributeError: module 'whereami.cache' has no attribute 'caps_path'`.

- [ ] **Step 3: Implement `caps_path`/`load_caps`/`save_caps`**

In `src/whereami/cache.py`, after `peek_path()` (around line 28), add:

```python
def caps_path() -> Path:
    """Global (not session-scoped) capability cache; keyed internally by
    `claude --version` so it self-heals across CLI upgrades."""
    return CACHE_DIR / "capabilities.json"
```

After `save_cache()` (around line 96), add:

```python
def load_caps() -> Dict:
    try:
        with open(caps_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_caps(data: Dict) -> None:
    _atomic_write(caps_path(), json.dumps(data))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cache.py -k caps -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/cache.py tests/test_cache.py
git commit -m "feat: capability cache (load_caps/save_caps)"
```

---

## Task 2: `_invoke` subprocess seam

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py`:

```python
def test_invoke_returns_full_envelope_and_passes_args(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = '{"result": "hi", "usage": {"output_tokens": 7}}'

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(drift.subprocess, "run", fake_run)
    env = drift._invoke(["claude", "-p", "x"], {"MAX_THINKING_TOKENS": "0"})
    assert env == {"result": "hi", "usage": {"output_tokens": 7}}
    assert captured["args"] == ["claude", "-p", "x"]
    assert captured["env"]["MAX_THINKING_TOKENS"] == "0"
    assert "PATH" in captured["env"]   # merged OVER os.environ, not replaced


def test_invoke_none_env_inherits_parent(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = '{"result": "ok"}'

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(drift.subprocess, "run", fake_run)
    drift._invoke(["claude"], None)
    assert captured["env"] is None   # today's behavior: inherit verbatim


def test_invoke_degrades_to_empty_dict(monkeypatch):
    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout

    for stdout in ("[1, 2]", "null", "not json", ""):
        monkeypatch.setattr(drift.subprocess, "run",
                            lambda *a, stdout=stdout, **k: FakeProc(stdout))
        assert drift._invoke(["claude"], None) == {}


def test_invoke_subprocess_error_is_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("no claude")

    monkeypatch.setattr(drift.subprocess, "run", boom)
    assert drift._invoke(["claude"], None) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k invoke -q`
Expected: FAIL with `AttributeError: module 'whereami.drift' has no attribute '_invoke'`.

- [ ] **Step 3: Implement `_invoke`**

In `src/whereami/drift.py`, add above `_run_claude` (around line 24):

```python
def _invoke(args, env=None) -> Dict:
    """Low-level `claude` CLI call. Returns the FULL parsed JSON envelope
    (`result`, `usage`, …) or {} on any failure. `env`, when given, is merged
    OVER the current environment (so MAX_THINKING_TOKENS=0 rides alongside the
    inherited login/PATH); env=None inherits the parent verbatim — today's
    behavior. Returning the whole envelope lets the probe read
    usage.output_tokens for the thinking-off check."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=CLI_TIMEOUT,
            env=(dict(os.environ, **env) if env else None),
        )
        envelope = json.loads(proc.stdout)
        return envelope if isinstance(envelope, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_drift.py -k invoke -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: _invoke seam returning the full claude -p envelope"
```

---

## Task 3: `_cli_version`, `STRIP_FLAGS`, `_build_argv`

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py`:

```python
def test_cli_version_returns_stripped_stdout(monkeypatch):
    class FakeProc:
        stdout = "2.1.172 (Claude Code)\n"

    monkeypatch.setattr(drift.subprocess, "run", lambda *a, **k: FakeProc())
    assert drift._cli_version() == "2.1.172 (Claude Code)"


def test_cli_version_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError()

    monkeypatch.setattr(drift.subprocess, "run", boom)
    assert drift._cli_version() == ""


def test_build_argv_unstripped_is_todays_behavior():
    argv = drift._build_argv("PROMPT", stripped=False)
    assert argv == ["claude", "-p", "PROMPT",
                    "--model", drift.HAIKU_MODEL, "--output-format", "json"]


def test_build_argv_stripped_adds_all_six_flags():
    argv = drift._build_argv("PROMPT", stripped=True)
    assert argv[:7] == ["claude", "-p", "PROMPT", "--model", drift.HAIKU_MODEL,
                        "--output-format", "json"]
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert "--strict-mcp-config" in argv
    # empty-string args are load-bearing: --tools "" drops ~11K of tool schemas
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    sp = argv[argv.index("--system-prompt") + 1]
    assert "JSON-only classifier" in sp
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k "cli_version or build_argv" -q`
Expected: FAIL with `AttributeError: ... has no attribute '_cli_version'`.

- [ ] **Step 3: Implement the constants and helpers**

In `src/whereami/drift.py`, add after the `FAILURE_BACKOFF`/`SWEEP_AGE` constants (around line 21):

```python
# The stripped invocation (validated on CLI 2.1.172): drops ~29K of Claude
# Code context to ~870 input tokens. Empty-string args are load-bearing and
# version-brittle — see _stripped_supported's probe + fallback.
STRIP_FLAGS = [
    "--system-prompt",
    "You are a JSON-only classifier. Reply with only the requested JSON "
    "object, no prose.",
    "--exclude-dynamic-system-prompt-sections",
    "--strict-mcp-config",
    "--setting-sources", "",
    "--tools", "",
]
# MAX_THINKING_TOKENS=0 kills extended thinking (64 output tokens, ~1s).
# DISABLE_INTERLEAVED_THINKING=1 does NOT — it only disables interleaving.
THINKING_OFF_ENV = {"MAX_THINKING_TOKENS": "0"}
```

Add the helpers above `_run_claude` (after `_invoke`):

```python
def _cli_version() -> str:
    """`claude --version`, stripped, or '' if it can't be read. The caps-cache
    key, so the probe re-runs across CLI upgrades/downgrades."""
    try:
        proc = subprocess.run(["claude", "--version"],
                              capture_output=True, text=True, timeout=CLI_TIMEOUT)
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _build_argv(prompt: str, stripped: bool):
    argv = ["claude", "-p", prompt, "--model", HAIKU_MODEL, "--output-format", "json"]
    if stripped:
        argv = argv + STRIP_FLAGS
    return argv
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_drift.py -k "cli_version or build_argv" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: STRIP_FLAGS, _build_argv, _cli_version"
```

---

## Task 4: Probe classification helpers

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py`:

```python
def test_probe_json_ok_accepts_object_and_fences():
    assert drift._probe_json_ok('{"answer": 59}') is True
    assert drift._probe_json_ok('```json\n{"answer": 59}\n```') is True
    assert drift._probe_json_ok('here: {"answer": 59} done') is True


def test_probe_json_ok_rejects_non_object():
    assert drift._probe_json_ok("sorry, no JSON") is False
    assert drift._probe_json_ok(None) is False
    assert drift._probe_json_ok("[1, 2]") is False   # array is not an object


def test_output_tokens_reads_usage():
    assert drift._output_tokens({"usage": {"output_tokens": 42}}) == 42
    assert drift._output_tokens({}) is None
    assert drift._output_tokens({"usage": {}}) is None
    assert drift._output_tokens({"usage": {"output_tokens": True}}) is None  # bool


def test_stripped_probe_ok_requires_json_AND_low_tokens():
    ok = {"result": '{"answer": 59}', "usage": {"output_tokens": 20}}
    assert drift._stripped_probe_ok(ok) is True
    # thinking still on → output spikes → reject even though JSON parses
    hot = {"result": '{"answer": 59}', "usage": {"output_tokens": 2025}}
    assert drift._stripped_probe_ok(hot) is False
    # no usage → can't confirm thinking-off → reject
    assert drift._stripped_probe_ok({"result": '{"answer": 59}'}) is False
    # not parseable → reject
    assert drift._stripped_probe_ok(
        {"result": "thinking out loud", "usage": {"output_tokens": 5}}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k "probe_json or output_tokens or stripped_probe" -q`
Expected: FAIL with `AttributeError: ... has no attribute '_probe_json_ok'`.

- [ ] **Step 3: Implement the helpers**

In `src/whereami/drift.py`, add after `_build_argv`:

```python
PROBE_MAX_OUTPUT_TOKENS = 300


def _probe_json_ok(result) -> bool:
    """The probe's OWN parse check — deliberately NOT parse_orientation (which
    requires score+gist and would reject a probe reply, mis-classifying every
    CLI as unsupported). Just: is there a JSON OBJECT in the reply? Greedy regex
    tolerates ```json fences and surrounding prose."""
    if not isinstance(result, str):
        return False
    match = re.search(r"\{.*\}", result, re.DOTALL)
    if not match:
        return False
    try:
        return isinstance(json.loads(match.group(0)), dict)
    except ValueError:
        return False


def _output_tokens(envelope: Dict) -> Optional[int]:
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        tokens = usage.get("output_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            return tokens
    return None


def _stripped_probe_ok(envelope: Dict) -> bool:
    """Stripped probe passes iff the reply is parseable JSON AND output_tokens
    is low. Low output is the thinking-off proof: a reasoning prompt with
    thinking ON spikes output; if a future CLI ignores MAX_THINKING_TOKENS=0
    the count spikes and the probe catches the regression instead of silently
    paying the thinking tax. Missing usage → can't confirm → not ok."""
    if not _probe_json_ok(envelope.get("result")):
        return False
    tokens = _output_tokens(envelope)
    return tokens is not None and tokens < PROBE_MAX_OUTPUT_TOKENS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_drift.py -k "probe_json or output_tokens or stripped_probe" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: probe classification helpers (json + thinking-off token check)"
```

---

## Task 5: `_stripped_supported` (version-keyed cache + probe + cooldown)

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py` (the file already imports `datetime, timedelta`; add nothing new):

```python
def test_stripped_supported_cache_hit_skips_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    cache.save_caps({"cli_version": "2.1.172", "stripped_ok": True})

    def no_probe(*a, **k):
        raise AssertionError("must not probe on a cache hit")

    monkeypatch.setattr(drift, "_invoke", no_probe)
    assert drift._stripped_supported() is True


def test_stripped_supported_cache_hit_false(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "1.0.0")
    cache.save_caps({"cli_version": "1.0.0", "stripped_ok": False})
    monkeypatch.setattr(drift, "_invoke",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert drift._stripped_supported() is False


def test_stripped_supported_probes_and_caches_true(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    monkeypatch.setattr(drift, "_invoke",
                        lambda args, env=None: {"result": '{"answer": 59}',
                                                "usage": {"output_tokens": 20}})
    assert drift._stripped_supported() is True
    assert cache.load_caps() == {"cli_version": "2.1.172", "stripped_ok": True}


def test_stripped_supported_flags_unsupported_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "1.0.0")
    seq = iter([
        {},  # stripped probe fails (old CLI rejects the flags)
        {"result": '{"answer": 59}', "usage": {"output_tokens": 1500}},  # unstripped works
    ])
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: next(seq))
    assert drift._stripped_supported() is False
    assert cache.load_caps() == {"cli_version": "1.0.0", "stripped_ok": False}


def test_stripped_supported_transient_failure_backs_off(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    calls = []

    def failing(*a, **k):
        calls.append(a)
        return {}

    monkeypatch.setattr(drift, "_invoke", failing)
    assert drift._stripped_supported() is False
    assert len(calls) == 2   # stripped + unstripped disambiguation
    caps = cache.load_caps()
    assert caps.get("probed_at") and "stripped_ok" not in caps
    # second compute within FAILURE_BACKOFF → reuse, do NOT re-probe
    calls.clear()
    assert drift._stripped_supported() is False
    assert calls == []


def test_stripped_supported_reprobes_after_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    stale = (datetime.now().astimezone()
             - timedelta(seconds=drift.FAILURE_BACKOFF + 60)
             ).isoformat(timespec="seconds")
    cache.save_caps({"cli_version": "2.1.172", "probed_at": stale})
    monkeypatch.setattr(drift, "_invoke",
                        lambda *a, **k: {"result": '{"answer": 1}',
                                         "usage": {"output_tokens": 10}})
    assert drift._stripped_supported() is True   # cooldown elapsed → re-probe


def test_stripped_supported_reprobes_on_version_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "OLD", "stripped_ok": True})
    monkeypatch.setattr(drift, "_cli_version", lambda: "NEW")
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: {})   # both calls fail
    assert drift._stripped_supported() is False
    assert cache.load_caps().get("cli_version") == "NEW"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k stripped_supported -q`
Expected: FAIL with `AttributeError: ... has no attribute '_stripped_supported'`.

- [ ] **Step 3: Implement `_stripped_supported`**

In `src/whereami/drift.py`, add after `_stripped_probe_ok`:

```python
def _stripped_supported(now: Optional[float] = None) -> bool:
    """Decide stripped vs. unstripped for the current CLI version. Lazy,
    version-keyed cache is the source of truth; probe only on a miss.

    All-or-nothing fallback: if the stripped call fails for ANY reason, we use
    a trivial unstripped call to disambiguate flags-unsupported (cache False
    permanently) from a broken/transient CLI (record probed_at, back off
    FAILURE_BACKOFF so a logged-out/offline CLI does not fire two extra probe
    calls every compute)."""
    now = time.time() if now is None else now
    version = _cli_version()
    caps = cache.load_caps()
    if caps.get("cli_version") == version:
        if isinstance(caps.get("stripped_ok"), bool):
            return caps["stripped_ok"]          # decided — no probe
        probed = cache.ts_to_epoch(caps.get("probed_at"))
        if probed is not None and 0 <= (now - probed) < FAILURE_BACKOFF:
            return False                        # broken CLI cooling down
    return _probe(version)


def _probe(version: str) -> bool:
    if _stripped_probe_ok(_invoke(_build_argv(_PROBE_PROMPT, True), THINKING_OFF_ENV)):
        cache.save_caps({"cli_version": version, "stripped_ok": True})
        return True
    # Stripped failed. Does a trivial UNSTRIPPED call work? (Skip the token
    # check — thinking is on here, so output is expected to be high.)
    if _probe_json_ok(_invoke(_build_argv(_PROBE_PROMPT, False)).get("result")):
        cache.save_caps({"cli_version": version, "stripped_ok": False})
        return False
    cache.save_caps({"cli_version": version, "probed_at": _now_iso()})
    return False
```

Add the probe prompt constant next to the other constants (after `THINKING_OFF_ENV`):

```python
# A SMALL REASONING prompt, not {"ok": true}: a trivial reply is tiny whether
# or not thinking is on, so it can't verify the thinking-off lever. This would
# burn reasoning tokens if thinking were on; checked for low output_tokens, it
# confirms the flags AND MAX_THINKING_TOKENS=0 in one shot.
_PROBE_PROMPT = ('Reply with ONLY this JSON object, no prose: {"answer": <int>}. '
                 "What is 17 times 4, minus 9?")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_drift.py -k stripped_supported -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: _stripped_supported — version-keyed caps probe with cooldown"
```

---

## Task 6: Rewrite `_run_claude` to choose stripped vs. unstripped

**Files:**
- Modify: `src/whereami/drift.py:24-40`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py`:

```python
def test_run_claude_uses_stripped_when_supported(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: True)
    captured = {}

    def fake_invoke(args, env=None):
        captured["args"] = args
        captured["env"] = env
        return {"result": '{"score": 1, "gist": "x"}'}

    monkeypatch.setattr(drift, "_invoke", fake_invoke)
    assert drift._run_claude("PROMPT") == '{"score": 1, "gist": "x"}'
    assert "--tools" in captured["args"]
    assert captured["env"] == {"MAX_THINKING_TOKENS": "0"}


def test_run_claude_uses_unstripped_when_unsupported(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: False)
    captured = {}

    def fake_invoke(args, env=None):
        captured["args"] = args
        captured["env"] = env
        return {"result": "reply"}

    monkeypatch.setattr(drift, "_invoke", fake_invoke)
    assert drift._run_claude("PROMPT") == "reply"
    assert captured["args"] == ["claude", "-p", "PROMPT", "--model",
                                drift.HAIKU_MODEL, "--output-format", "json"]
    assert captured["env"] is None   # today's behavior verbatim: no env override


def test_run_claude_non_str_result_is_empty(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: True)
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: {"result": {"x": 1}})
    assert drift._run_claude("PROMPT") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k "run_claude_uses or run_claude_non_str" -q`
Expected: FAIL — the old `_run_claude` builds its own argv and ignores `_stripped_supported`/`_invoke`, so `captured` is never populated (KeyError / assertion failure).

- [ ] **Step 3: Rewrite `_run_claude`**

In `src/whereami/drift.py`, replace the existing `_run_claude` (lines 24-40) with:

```python
def _run_claude(prompt: str) -> str:
    """Call Haiku via the logged-in `claude` CLI (subscription, no API key).
    Stripped argv + thinking-off when the CLI supports it (~870 in / ~64 out /
    ~1s); otherwise today's unstripped call, verbatim. Returns the model's
    text, or '' on any failure."""
    stripped = _stripped_supported()
    envelope = _invoke(_build_argv(prompt, stripped),
                       THINKING_OFF_ENV if stripped else None)
    result = envelope.get("result", "")
    return result if isinstance(result, str) else ""
```

- [ ] **Step 4: Run the full drift suite to verify pass + no regressions**

Run: `pytest tests/test_drift.py -q`
Expected: PASS. In particular `test_run_claude_tolerates_shape_changed_envelope` still passes — with `subprocess.run` mocked to one fixed `stdout`, the probe both-calls-fail → transient → unstripped, and the non-dict/non-str result still degrades to `""`.

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: _run_claude picks stripped vs unstripped argv + thinking-off env"
```

---

## Task 7: Warm the capability probe during install

**Files:**
- Modify: `src/whereami/install.py`
- Test: `tests/test_install.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install.py`:

```python
def test_main_warms_capability_probe(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    called = []
    install.main(["--hotkey", "none"], scripts_dir=Path("/clone/scripts"),
                 warm=lambda out: called.append(True))
    assert called == [True]


def test_main_dry_run_does_not_warm(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    called = []
    install.main(["--dry-run", "--hotkey", "none"], scripts_dir=Path("/clone/scripts"),
                 warm=lambda out: called.append(True))
    assert called == []


def test_warm_capabilities_never_raises(monkeypatch):
    from whereami import drift

    def boom():
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(drift, "_stripped_supported", boom)
    out = io.StringIO()
    install._warm_capabilities(out)   # must not raise
    assert "skipped" in out.getvalue()
```

Also update the two existing non-dry-run `main()` tests so they do not shell out to `claude` during the unit run. In `test_main_wires_statusline_and_creates_peek_dir`, change the `install.main(...)` call to:

```python
    rc = install.main(["--hotkey", "none"], scripts_dir=scripts,
                      warm=lambda out: None)
```

In `test_main_backs_up_existing_settings_before_writing`, change the call to:

```python
    install.main(["--hotkey", "none"], scripts_dir=Path("/clone/scripts"),
                 warm=lambda out: None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_install.py -k "warm or warms" -q`
Expected: FAIL with `TypeError: main() got an unexpected keyword argument 'warm'`.

- [ ] **Step 3: Implement warming**

In `src/whereami/install.py`, add a function above `main` (around line 266):

```python
def _warm_capabilities(out) -> None:
    """Prime the version-keyed capability cache so the first real compute isn't
    delayed by the probe. Best-effort: never fail the install over it. The lazy
    cache stays the source of truth and self-heals across CLI upgrades."""
    try:
        from whereami import drift
        ok = drift._stripped_supported()
        print("  capability probe: {}".format("stripped" if ok else "unstripped"),
              file=out)
    except Exception:
        print("  capability probe: skipped", file=out)
```

Change the `main` signature (line 267) from:

```python
def main(argv=None, *, scripts_dir=None) -> int:
```

to:

```python
def main(argv=None, *, scripts_dir=None, warm=_warm_capabilities) -> int:
```

Then, in `main`, just before the final `print("Done. ...")` (around line 303), add:

```python
    warm(out)
```

(The `if args.dry_run:` branch returns earlier, so dry-run never warms.)

- [ ] **Step 4: Run the install suite to verify pass**

Run: `pytest tests/test_install.py -q`
Expected: PASS (all green, including the two edited main() tests).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/install.py tests/test_install.py
git commit -m "feat: warm capability probe during install (best-effort, injectable)"
```

---

## Checkpoint: full suite after Phase 1

- [ ] Run: `pytest tests/ -x -q`
Expected: PASS. Phase 1 changes behavior only in `_run_claude`'s argv/env; all orientation/parse/hook tests inject a `runner` and are untouched.

---

# PHASE 2 — Return-from-idle trigger

## Task 8: `cache.parse_iso` + `Z`-normalized `ts_to_epoch`

**Files:**
- Modify: `src/whereami/cache.py:31-38`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache.py`:

```python
def test_parse_iso_normalizes_trailing_z():
    dt = cache.parse_iso("2026-06-10T21:42:50.649Z")
    assert dt is not None
    assert dt.utcoffset().total_seconds() == 0   # 'Z' parsed as UTC


def test_parse_iso_offset_form_and_garbage():
    assert cache.parse_iso("2026-06-09T10:00:00-07:00") is not None
    assert cache.parse_iso(None) is None
    assert cache.parse_iso("") is None
    assert cache.parse_iso("garbage") is None
    assert cache.parse_iso(12345) is None


def test_ts_to_epoch_handles_literal_Z_suffix():
    # THE Python 3.9/3.10 regression guard: datetime.fromisoformat rejects bare
    # 'Z' before 3.11. Transcript timestamps use 'Z'; without normalization this
    # returns None and silently disables the idle trigger on the 3.9 floor.
    assert cache.ts_to_epoch("2026-06-10T21:42:50Z") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cache.py -k "parse_iso or literal_Z" -q`
Expected: FAIL — `parse_iso` missing; on Python 3.9/3.10 `test_ts_to_epoch_handles_literal_Z_suffix` also fails because `ts_to_epoch` returns `None` for the `Z` form.

- [ ] **Step 3: Implement `parse_iso` and route `ts_to_epoch` through it**

In `src/whereami/cache.py`, replace the existing `ts_to_epoch` (lines 31-38) with:

```python
def parse_iso(value) -> Optional[datetime]:
    """ISO-8601 string → datetime, normalizing a trailing 'Z' (UTC) to
    '+00:00'. datetime.fromisoformat rejects bare 'Z' on Python 3.9/3.10, and
    Claude Code transcript timestamps use the 'Z' form — without this, every
    transcript gap routes to the unparseable path and silently disables the
    idle trigger on the project's 3.9 floor. None if absent/garbage."""
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def ts_to_epoch(value) -> Optional[float]:
    """Parse an iso-8601 timestamp to epoch seconds; None if absent/garbage."""
    dt = parse_iso(value)
    return dt.timestamp() if dt is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cache.py -k "parse_iso or literal_Z or ts_to_epoch" -q`
Expected: PASS (existing `test_ts_to_epoch` still passes; the offset-form path is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/cache.py tests/test_cache.py
git commit -m "fix: parse_iso normalizes trailing Z so 3.9/3.10 parse transcript timestamps"
```

---

## Task 9: `transcript.human_turn_timestamps`

**Files:**
- Modify: `src/whereami/transcript.py`
- Test: `tests/test_transcript.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript.py`:

```python
def _ts_line(obj, ts):
    obj = dict(obj)
    obj["timestamp"] = ts
    return json.dumps(obj)


def test_human_turn_timestamps_skips_tool_results(tmp_path):
    # type=="user" is overwhelmingly tool_result envelopes; filtering on that
    # alone would pick two adjacent tool-results seconds apart and idle would
    # never fire. Must filter via human_text() like the rest of the module.
    p = tmp_path / "t.jsonl"
    lines = [
        _ts_line({"type": "user", "message": {"role": "user", "content": "first ask"}},
                 "2026-06-10T21:00:00Z"),
        _ts_line({"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
                 "2026-06-10T21:00:05Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "out"}]}}, "2026-06-10T21:00:06Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "out2"}]}}, "2026-06-10T21:00:07Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "second ask"}},
                 "2026-06-10T21:20:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    stamps = transcript.human_turn_timestamps(str(p), n=2)
    assert len(stamps) == 2
    assert (stamps[0] - stamps[1]).total_seconds() == 20 * 60   # 21:20 − 21:00


def test_human_turn_timestamps_most_recent_first():
    pass  # covered by the gap sign in the test above (positive → [recent, prev])


def test_human_turn_timestamps_skips_garbage_timestamps(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        _ts_line({"type": "user", "message": {"role": "user", "content": "a"}},
                 "2026-06-10T21:00:00Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "b"}}, "nope"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "c"}},
                 "2026-06-10T21:05:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    stamps = transcript.human_turn_timestamps(str(p), n=2)
    assert len(stamps) == 2   # 'b' (garbage ts) skipped; 'c' then 'a'


def test_human_turn_timestamps_missing_file_is_empty(tmp_path):
    assert transcript.human_turn_timestamps(str(tmp_path / "none.jsonl")) == []
```

(Delete the placeholder `test_human_turn_timestamps_most_recent_first` — it is only here to document intent; the gap-sign assertion above proves ordering. Do not leave a `pass` test in the suite.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_transcript.py -k human_turn_timestamps -q`
Expected: FAIL with `AttributeError: module 'whereami.transcript' has no attribute 'human_turn_timestamps'`.

- [ ] **Step 3: Implement `human_turn_timestamps`**

In `src/whereami/transcript.py`, add the imports at the top (after the existing `import json`/`import re`):

```python
from datetime import datetime

from . import cache
```

And update the typing import line to include `List` if not already present (it is: `from typing import Callable, List, Optional`).

Add the function at the end of the file:

```python
def human_turn_timestamps(path: str, n: int = 2) -> List[datetime]:
    """Timestamps of the last `n` genuine human turns, most-recent-first. Pairs
    each human turn (filtered via human_text — NOT type=='user', which is mostly
    tool_result envelopes) with its `timestamp`, Z-normalized. Skips turns with
    missing/garbage timestamps."""
    out = []  # type: List[datetime]
    for line in reversed(_tail_lines(path)):
        entry = _parse(line)
        if entry is None:
            continue
        if human_text(entry) is None:
            continue
        ts = cache.parse_iso(entry.get("timestamp"))
        if ts is None:
            continue
        out.append(ts)
        if len(out) >= n:
            break
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_transcript.py -q`
Expected: PASS (all transcript tests green).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/transcript.py tests/test_transcript.py
git commit -m "feat: human_turn_timestamps — human-turn timestamps, tool_results excluded"
```

---

## Task 10: `returned_from_idle` + `IDLE_THRESHOLD`

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

At the top of `tests/test_drift.py`, change the datetime import line:

```python
from datetime import datetime, timedelta
```

to:

```python
from datetime import datetime, timedelta, timezone
```

Append to `tests/test_drift.py`:

```python
def _ts(obj, timestamp):
    obj = dict(obj)
    obj["timestamp"] = timestamp
    return json.dumps(obj)


def _idle_transcript(tmp_path, gap_seconds):
    base = datetime(2026, 6, 10, 21, 0, 0, tzinfo=timezone.utc)
    later = base + timedelta(seconds=gap_seconds)
    z = lambda d: d.isoformat().replace("+00:00", "Z")   # exercise Z-normalization
    p = tmp_path / "idle.jsonl"
    p.write_text("\n".join([
        _ts({"type": "user", "message": {"role": "user", "content": "first ask"}}, z(base)),
        _ts({"type": "assistant", "message": {"role": "assistant", "content": "ok"}}, z(base)),
        _ts({"type": "user", "message": {"role": "user", "content": "second ask"}}, z(later)),
    ]) + "\n")
    return p


def test_returned_from_idle_true_when_gap_exceeds(tmp_path):
    p = _idle_transcript(tmp_path, gap_seconds=20 * 60)
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is True


def test_returned_from_idle_false_when_gap_small(tmp_path):
    p = _idle_transcript(tmp_path, gap_seconds=60)
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is False


def test_returned_from_idle_false_with_fewer_than_two_human_turns(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_ts({"type": "user", "message": {"role": "user",
                  "content": "only ask"}}, "2026-06-10T21:00:00Z") + "\n")
    assert drift.returned_from_idle(str(p), 600) is False


def test_returned_from_idle_false_on_missing_file(tmp_path):
    assert drift.returned_from_idle(str(tmp_path / "none.jsonl"), 600) is False


def test_long_agent_turn_trips_idle_gap(tmp_path):
    # The human-to-human gap includes the previous agent turn's runtime: a long
    # autonomous run crosses the threshold with no human idleness. Benign by
    # design — a gist from before a long run is exactly the kind that's stale.
    p = tmp_path / "t.jsonl"
    lines = [
        _ts({"type": "user", "message": {"role": "user", "content": "go"}},
            "2026-06-10T21:00:00Z"),
        _ts({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "..."}]}}, "2026-06-10T21:04:00Z"),
        _ts({"type": "user", "message": {"role": "user", "content": "next"}},
            "2026-06-10T21:08:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is False
    assert drift.returned_from_idle(str(p), threshold_seconds=5 * 60) is True


def test_read_idle_threshold_default_and_overrides(monkeypatch):
    monkeypatch.delenv("WHEREAMI_IDLE_MIN", raising=False)
    assert drift._read_idle_threshold() == 600
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "5")
    assert drift._read_idle_threshold() == 300
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "0")
    assert drift._read_idle_threshold() == 600    # non-positive → default
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "-3")
    assert drift._read_idle_threshold() == 600
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "garbage")
    assert drift._read_idle_threshold() == 600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k "returned_from_idle or read_idle or long_agent" -q`
Expected: FAIL with `AttributeError: ... has no attribute 'returned_from_idle'`.

- [ ] **Step 3: Implement the threshold reader and `returned_from_idle`**

In `src/whereami/drift.py`, add near the other constants (after `THROTTLE_TURNS`):

```python
def _read_idle_threshold() -> int:
    """WHEREAMI_IDLE_MIN minutes → seconds. Bad/non-positive → 10-minute
    default. Read at import; each Stop hook is a fresh process, so a changed
    env var takes effect on the next turn."""
    try:
        minutes = int(os.environ.get("WHEREAMI_IDLE_MIN", "10"))
    except (TypeError, ValueError):
        return 600
    return minutes * 60 if minutes > 0 else 600


IDLE_THRESHOLD = _read_idle_threshold()
```

Add `returned_from_idle` near `hook_due` (after `peek_due`):

```python
def returned_from_idle(path: str, threshold_seconds: int) -> bool:
    """True iff the gap between the last two genuine human turns ≥ threshold —
    i.e. the user was away ≥ threshold before the turn that just completed.
    False when fewer than two human turns exist or timestamps are unparseable;
    the periodic cadence still covers those cases. Never raises (the Stop hook
    must exit 0)."""
    stamps = transcript.human_turn_timestamps(path, n=2)
    if len(stamps) < 2:
        return False
    try:
        gap = (stamps[0] - stamps[1]).total_seconds()
    except (TypeError, OverflowError):
        return False   # mixed naive/aware datetimes — degrade, don't crash
    return gap >= threshold_seconds
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_drift.py -k "returned_from_idle or read_idle or long_agent" -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: returned_from_idle + WHEREAMI_IDLE_MIN threshold"
```

---

## Task 11: Wire idle into `hook_due` and `run_hook`

**Files:**
- Modify: `src/whereami/drift.py:87-95` (`hook_due`), `src/whereami/drift.py:107-123` (`run_hook`)
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_drift.py`:

```python
def test_hook_due_idle_returned_forces_due():
    data = {"ts": "2026-06-09T10:00:00-07:00", "gist": "parser work",
            "turns_at_last_compute": 5}
    assert drift.hook_due(data, 6) is False                       # delta 1 < 6
    assert drift.hook_due(data, 6, idle_returned=True) is True    # idle forces due


def test_hook_due_default_idle_param_keeps_existing_behavior():
    data = {"ts": "x", "gist": "g", "turns_at_last_compute": 5}
    assert drift.hook_due(data, 6) is False   # 3-arg call still valid (no churn)


def test_hook_spawns_on_idle_return_below_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _idle_transcript(tmp_path, gap_seconds=20 * 60)   # > default 10 min
    cache.save_turns("s1", 5)
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00", "gist": "g",
                            "turns_at_last_compute": 5})
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(p)}
    _run_hook_with(monkeypatch, payload, spawned)   # turns 5→6, cadence not due
    assert len(spawned) == 1   # spawned via the idle-return arm


def test_hook_no_idle_spawn_when_gap_small(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _idle_transcript(tmp_path, gap_seconds=60)
    cache.save_turns("s1", 5)
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00", "gist": "g",
                            "turns_at_last_compute": 5})
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(p)}
    _run_hook_with(monkeypatch, payload, spawned)
    assert spawned == []   # small gap + cadence not due → no spawn
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_drift.py -k "hook_due_idle or hook_due_default or idle_return or idle_spawn" -q`
Expected: FAIL — `hook_due()` takes no `idle_returned` kwarg, and `run_hook` doesn't compute it, so `test_hook_spawns_on_idle_return_below_throttle` finds `spawned == []`.

- [ ] **Step 3: Add the `idle_returned` param and wire `run_hook`**

In `src/whereami/drift.py`, change `hook_due` (lines 87-95) from:

```python
def hook_due(data: Dict, turns: int) -> bool:
    if not data.get("ts"):
        return True
    if not data.get("gist"):
        return True   # v1→v2 transition self-heal (turn-delta may be negative)
    delta = turns - cache.turns_at_last_compute(data)
    # Negative delta = a reset/lost .turns file behind a surviving cache:
    # inconsistent state is due, not "fresh for the next 45 turns".
    return delta >= THROTTLE_TURNS or delta < 0
```

to:

```python
def hook_due(data: Dict, turns: int, idle_returned: bool = False) -> bool:
    if not data.get("ts"):
        return True
    if not data.get("gist"):
        return True   # v1→v2 transition self-heal (turn-delta may be negative)
    delta = turns - cache.turns_at_last_compute(data)
    # Negative delta = a reset/lost .turns file behind a surviving cache:
    # inconsistent state is due, not "fresh for the next 45 turns".
    # idle_returned = the user came back after >= WHEREAMI_IDLE_MIN away: refresh
    # the gist on return, additive to the periodic cadence.
    return delta >= THROTTLE_TURNS or delta < 0 or idle_returned
```

In `run_hook` (lines 107-123), change the block:

```python
    turns = cache.increment_turns(session_id)
    data = cache.load_cache(session_id)
    if hook_due(data, turns):
        maybe_spawn_compute(session_id, transcript_path, data=data)
```

to:

```python
    turns = cache.increment_turns(session_id)
    data = cache.load_cache(session_id)
    idle_returned = returned_from_idle(transcript_path, IDLE_THRESHOLD)
    if hook_due(data, turns, idle_returned):
        maybe_spawn_compute(session_id, transcript_path, data=data)
```

- [ ] **Step 4: Run the full drift suite to verify pass + no regressions**

Run: `pytest tests/test_drift.py -q`
Expected: PASS. Existing hook tests pass nonexistent/timestamp-less transcripts, so `returned_from_idle` returns False and their spawn counts are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: fire recompute on return-from-idle (hook_due + run_hook)"
```

---

## Checkpoint: full suite after Phase 2

- [ ] Run: `pytest tests/ -x -q`
Expected: PASS (all green on the local interpreter). The literal-`Z` cache test arms the 3.9 CI job.

---

# Docs

## Task 12: README + CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update the "How it works" diagram and add the env var (README)**

In `README.md`, the "How it works" fenced diagram currently reads:

```
Stop hook ──every 6 turns──▶ detached sidecar ──one Haiku call──▶ cache
                             (logged-in claude CLI)                 │
statusline renderer ◀──reads cache + transcript tail, no network───┘
```

Replace the first line so the idle trigger is visible:

```
Stop hook ──every 6 turns / on return from idle──▶ detached sidecar ──one Haiku call──▶ cache
                                                   (logged-in claude CLI)                 │
statusline renderer ◀──────reads cache + transcript tail, no network─────────────────────┘
```

Immediately after the diagram's closing fence, add a short paragraph documenting cost, the env var, and the fallback:

```markdown
Each compute is one stripped Haiku call on your subscription — roughly **0.12¢
and ~1 second** (the invocation drops Claude Code's ~29K-token system context to
~870 tokens and disables extended thinking). On an older CLI that doesn't accept
the stripping flags, whereami detects that once per CLI version and falls back to
the full call automatically — no configuration, no error (validated against CLI
2.1.172).

Besides the 6-turn cadence, a recompute also fires when you **return from idle** —
when the gap between your last two messages crosses `WHEREAMI_IDLE_MIN` minutes
(default `10`), so the gist is fresh the moment you come back. Set
`WHEREAMI_IDLE_MIN=0`… (any non-positive or invalid value) keeps the 10-minute
default.
```

- [ ] **Step 2: Reconcile any stale cost numbers (README)**

Run: `grep -nE "¢|cents|tokens|every 6 turns|32 ?s|seconds" README.md`
For any sentence that still quotes the old per-call cost or latency (e.g. "~1–3¢", "~32 s", "~29K tokens" framed as the live cost), update it to the new figures: **~0.12¢ / ~1 s / ~870 input tokens**. Leave the ~29K figure only where it describes the *pre-diet* baseline being eliminated. (If grep shows none, this step is a no-op — record that.)

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, insert a new section directly under the `# Changelog` preamble and above `## [0.2.0] - 2026-06-10`:

```markdown
## [Unreleased]

The token diet. Each orientation compute is ~20× cheaper, so the periodic
cadence is affordable and a freshness trigger becomes practical.

### Added

- **Return-from-idle recompute**: a recompute now also fires when you come back
  after being away — when the gap between your last two messages crosses
  `WHEREAMI_IDLE_MIN` minutes (default `10`) — so the gist is fresh the instant
  you return, on top of the existing 6-turn cadence.
- **Capability probe**: a once-per-CLI-version probe (cached in
  `~/.claude/whereami/capabilities.json`) decides whether the installed `claude`
  accepts the token-stripping flags, and confirms extended thinking is actually
  off by checking the probe's output-token count. `whereami install` warms it so
  the first compute isn't delayed.

### Changed

- **Stripped Haiku invocation**: the drift call now drops Claude Code's ~29K
  tokens of system context (`--tools ""`, `--exclude-dynamic-system-prompt-sections`,
  `--strict-mcp-config`, `--setting-sources ""`, a one-line classifier
  `--system-prompt`) and disables extended thinking (`MAX_THINKING_TOKENS=0`).
  Per call: **~29,400 → ~870 input tokens, ~32 s → ~1 s, ~2.6¢ → ~0.12¢**.
  Older CLIs that reject the flags fall back to the previous full call
  automatically, per CLI version.

### Fixed

- Transcript timestamps end in `Z` (UTC); `datetime.fromisoformat` rejects bare
  `Z` before Python 3.11. `parse_iso` now normalizes `Z` → `+00:00` so the idle
  trigger works on the 3.9/3.10 floor instead of silently never firing.
```

- [ ] **Step 4: Verify the docs render sanely**

Run: `grep -nE "WHEREAMI_IDLE_MIN|0\.12¢|Unreleased" README.md CHANGELOG.md`
Expected: `WHEREAMI_IDLE_MIN` and the new cost figure appear in both files as edited; `Unreleased` appears in `CHANGELOG.md`.

(Version bump 0.2.0 → 0.3.0 is a **release decision, flagged not assumed** — keep the entry under `[Unreleased]`; do not edit `pyproject.toml`/`__init__` version or add the `[0.3.0]` link footer in this task.)

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: token-diet cost/latency, WHEREAMI_IDLE_MIN, CLI fallback note"
```

---

# Final verification

- [ ] **Run the whole suite:** `pytest tests/ -x -q` → all pass.
- [ ] **No `X | Y` unions introduced:** `grep -nE ': *[A-Za-z]+ *\| *[A-Za-z]' src/whereami/*.py` → no new hits (3.9 floor).
- [ ] **Manual smoke (optional, requires a logged-in `claude`):** trigger one live compute and confirm a sane gist/score in ~1 s — e.g. delete `~/.claude/whereami/capabilities.json`, run `python3 -c "from whereami import drift; print(drift._stripped_supported())"` (expect `True`), then watch the next statusline tick.

---

## Self-Review (run after writing the plan)

**Spec coverage:**
- Phase 1 invocation/STRIP_FLAGS/thinking-off → Tasks 3, 6. ✓
- `_invoke` seam returning full envelope → Task 2. ✓
- `_cli_version`, caps cache (`capabilities.json`) → Tasks 1, 3. ✓
- Probe with reasoning prompt + output-token thinking-off check, NOT reusing `parse_orientation` → Tasks 4, 5. ✓
- All-or-nothing fallback; unstripped == today's behavior verbatim → Tasks 5, 6. ✓
- Transient-failure cooldown (reuse `FAILURE_BACKOFF`) → Task 5. ✓
- Install warms the probe → Task 7. ✓
- `Z`-normalization in a shared helper + literal-`Z` unit test → Task 8. ✓
- `human_turn_timestamps` filters via `human_text` + interleaved tool_result fixture → Task 9. ✓
- `returned_from_idle`, `IDLE_THRESHOLD`/`WHEREAMI_IDLE_MIN` with bad-value guard, long-agent-turn case → Task 10. ✓
- `hook_due(..., idle_returned=False)` + `run_hook` wiring, defaulted param keeps tests green → Task 11. ✓
- Docs: cost/latency, `WHEREAMI_IDLE_MIN`, min-CLI note, version bump flagged → Task 12. ✓
- Out-of-scope items (statusline, skill, peek, THROTTLE_TURNS, no deps) → untouched by every task. ✓

**Placeholder scan:** Every code step shows complete code. The one `pass` test in Task 9 is explicitly flagged for deletion (documentation-only) so it never lands in the suite.

**Type consistency:** `_invoke(args, env=None)`, `_build_argv(prompt, stripped)`, `_stripped_supported(now=None)`, `_probe(version)`, `_stripped_probe_ok(envelope)`, `_probe_json_ok(result)`, `_output_tokens(envelope)`, `returned_from_idle(path, threshold_seconds)`, `human_turn_timestamps(path, n=2)`, `hook_due(data, turns, idle_returned=False)`, `cache.parse_iso`/`load_caps`/`save_caps`/`caps_path` — names are used identically across the tasks that define and call them.
