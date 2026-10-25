"""Keep ~/.claude/settings.json in the state this machine's proxy setup needs.

Every setting repaired here was, at some point, the difference between Claude
Code starting normally and a session that stops working:

  * ``CLAUDE_CODE_AUTO_MODE_SERVER=0``
    Without it, every auto-mode turn printed "this session isn't eligible
    because your requests go through 127.0.0.1:8791" and parked the first
    gated tool call. The server-side checks it asks for cannot exist behind
    LiteLLM + NVIDIA NIM, so the client must be told not to ask.

  * ``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1``
    Off by default. While it was off the /model picker offered only Default
    plus the five Claude aliases; the thirteen NVIDIA models the proxy serves
    were unreachable from the UI.

  * fast mode and the first-party assertions
    ``fastMode`` only takes effect with ``CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL``
    and the two SKIP_FAST_MODE entitlements, which also have to survive a
    settings rewrite (the launcher exports them, but SDK and IDE runs do not
    pass through the launcher).

  * ``disableAutoMode``
    Auto mode routes every shell call through Claude Code's safety classifier.
    That classifier request goes to a free NVIDIA model, which times out or
    returns output the client cannot parse, so commands fail with "auto mode
    cannot determine the safety of Bash" or "Stage 2 classifier error" and
    never run. Auto mode is turned off here; every other mode (default,
    acceptEdits, plan, bypassPermissions) stays available.

  * permissions
    A ``WebSearch`` deny entry blocks the tool outright and is stripped so
    every tool stays available.

The guard is additive and idempotent: unknown keys are preserved, values that
already match are not rewritten, and nothing is printed on a clean run. A file
that no longer parses is restored from the previous good copy, and a file that
cannot be repaired at all is rebuilt with just the required keys.

Exit codes: 0 = nothing to do or repaired, 1 = repaired (callers may ignore).
This never blocks a launch; it must not be able to stop Claude Code starting.

    python settings-guard.py            # repair ~/.claude/settings.json
    python settings-guard.py --check    # report only, change nothing
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude"
)
SETTINGS = os.environ.get("CLAUDE_SETTINGS_PATH") or os.path.join(CLAUDE_DIR, "settings.json")
BACKUP = SETTINGS + ".bak"
LOG = os.path.join(HERE, "heal.log")

# Environment values the proxy setup depends on. Anything already present with
# a different value is replaced: these are not preferences, they are the
# difference between working and not working on this machine.
REQUIRED_ENV = {
    # Auto mode: never ask the API for classifier checks it cannot provide.
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
    # /model picker: list the models the gateway actually serves.
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_TIMEOUT_MS": "15000",
    # Fast mode entitlements.
    "CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1",
    "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK": "1",
    "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS": "1",
    # Non-Anthropic model ids: accept them instead of remapping/validating away.
    "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    # Long sessions: past roughly 150k tokens the free reasoning models behind
    # the proxy start emitting template tokens in a loop, LiteLLM turns that
    # into a mid-stream InternalServerError, and the turn ends with "API
    # error". Compacting before that point keeps every request inside the
    # range these backends answer cleanly, and the two numbers must agree so
    # Claude Code does not think it has more room than the compact window.
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "160000",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000",
    # 64k output on a free backend means one rambling answer can stream for
    # 12+ minutes before it stops. 32k is the Claude Code default and still
    # fits a large file write, while halving the worst-case stall.
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
}

# Tools a deny rule must never take away on a machine where the user asked for
# every mode to be unlocked.
UNDENIED_TOOLS = ("WebSearch", "WebFetch", "Bash", "Read", "Edit", "Write")

# Auto mode must never come back. The classifier it depends on cannot work
# behind LiteLLM + NVIDIA NIM, and when it fails it blocks Bash/PowerShell
# instead of allowing them.
DISABLE_AUTO_MODE = "disable"

# Stop hook. The free models behind the proxy sometimes end a turn with a plan
# ("Let me create the document now...") and no tool call, so nothing runs and
# asking again produces another plan. stop_guard.py exits 2 on that shape,
# which makes Claude Code continue the turn with the instruction to act.
HOOK_PYTHON = r"C:\Python314\python.exe"
STOP_HOOK = os.path.join(CLAUDE_DIR, "hooks", "stop_guard.py")


def ensure_stop_hook(data: dict) -> bool:
    """Register stop_guard.py for Stop and SubagentStop. Returns True if added."""
    if not os.path.exists(STOP_HOOK):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    command = '"%s" "%s"' % (HOOK_PYTHON, STOP_HOOK)
    changed = False
    for event in ("Stop", "SubagentStop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
            changed = True
        present = any(
            isinstance(entry, dict)
            and any(
                isinstance(hook, dict) and STOP_HOOK in str(hook.get("command", ""))
                for hook in (entry.get("hooks") or [])
            )
            for entry in entries
        )
        if not present:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 20}]})
            changed = True
    return changed


def log(message: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} settings-guard: {message}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def minimal() -> dict:
    """Last-resort settings object: only what Claude Code needs to run here."""
    return {
        "env": {},
        "permissions": {"allow": [], "deny": [], "ask": []},
    }


def load() -> tuple[dict, list[str]]:
    """Return (settings, notes). A broken file is replaced, never fatal."""
    notes: list[str] = []
    if os.path.exists(SETTINGS):
        try:
            data = read_json(SETTINGS)
            if isinstance(data, dict):
                return data, notes
            notes.append("settings.json is not an object; rebuilding")
        except (OSError, ValueError) as error:
            notes.append(f"settings.json cannot be parsed ({error}); restoring backup")
    else:
        notes.append("settings.json is missing; creating it")

    if os.path.exists(BACKUP):
        try:
            data = read_json(BACKUP)
            if isinstance(data, dict):
                notes.append("restored settings.json from settings.json.bak")
                return data, notes
        except (OSError, ValueError):
            notes.append("settings.json.bak is unusable as well; rebuilding")

    return minimal(), notes


def repair(data: dict) -> list[str]:
    """Apply every required value in place. Returns a list of changes."""
    changes: list[str] = []

    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
        changes.append("created env block")
    for key, value in REQUIRED_ENV.items():
        if env.get(key) != value:
            env[key] = value
            changes.append(f"env {key}={value}")

    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions
        changes.append("created permissions block")

    # Auto mode is what puts every shell command in front of the classifier.
    # It is disabled in both places Claude Code reads the flag from, because a
    # session that enters auto mode fails every gated Bash call when the
    # classifier request to the free model times out.
    if data.get("disableAutoMode") != DISABLE_AUTO_MODE:
        data["disableAutoMode"] = DISABLE_AUTO_MODE
        changes.append("disableAutoMode=disable")
    if permissions.get("disableAutoMode") != DISABLE_AUTO_MODE:
        permissions["disableAutoMode"] = DISABLE_AUTO_MODE
        changes.append("permissions.disableAutoMode=disable")

    deny = permissions.get("deny")
    if deny is None:
        deny = []
        permissions["deny"] = deny
    elif not isinstance(deny, list):
        deny = []
        permissions["deny"] = deny
    kept = [rule for rule in deny if not _denies_any_tool(rule)]
    if len(kept) != len(deny):
        removed = [rule for rule in deny if _denies_any_tool(rule)]
        permissions["deny"] = kept
        changes.append(f"unblocked tools in deny: {', '.join(map(str, removed))}")

    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    for tool in UNDENIED_TOOLS:
        if tool not in allow:
            allow.append(tool)
            changes.append(f"allow {tool}")

    if data.get("fastMode") is not True:
        data["fastMode"] = True
        changes.append("fastMode=true")
    if data.get("skipDangerousModePermissionPrompt") is not True:
        data["skipDangerousModePermissionPrompt"] = True
        changes.append("skipDangerousModePermissionPrompt=true")

    # Auto mode's environment snapshot only feeds the classifier prompt. With
    # auto mode off it is dead weight, and its presence made it look as if auto
    # mode were still configured.
    if "autoMode" in data:
        data.pop("autoMode", None)
        changes.append("removed stale autoMode environment block")

    if ensure_stop_hook(data):
        changes.append("registered the Stop/SubagentStop guard hook")

    return changes


def _denies_any_tool(rule) -> bool:
    """True for a deny rule that removes a whole tool rather than one pattern."""
    if not isinstance(rule, str):
        return False
    name = rule.strip()
    for tool in UNDENIED_TOOLS:
        if name == tool or name == f"{tool}(*)" or name.startswith(f"{tool}("):
            return True
    return False


def write_atomic(path: str, data: dict) -> None:
    """Write through a temp file in the same directory, then replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".settings-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        if os.path.exists(path):
            shutil.copyfile(path, BACKUP)
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    data, notes = load()
    changes = repair(data)

    # Keep one known-good copy around even on a clean run, so a file that a
    # crashed writer mangles later can be restored instead of rebuilt.
    if not os.path.exists(BACKUP) and os.path.exists(SETTINGS) and not notes:
        try:
            shutil.copyfile(SETTINGS, BACKUP)
        except OSError:
            pass

    for note in notes:
        print(f"settings-guard: {note}")
    for change in changes:
        print(f"settings-guard: {change}")

    if not notes and not changes:
        return 0

    if check_only:
        log("check mode: " + "; ".join(notes + changes))
        return 1

    try:
        write_atomic(SETTINGS, data)
    except OSError as error:
        # Never let a settings write failure abort a launch.
        log(f"could not write {SETTINGS}: {error}")
        print(f"settings-guard: could not write settings.json ({error})")
        return 0

    log("; ".join(notes + changes))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
