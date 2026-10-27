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
    every tool stays available. ``defaultMode`` is held at
    ``bypassPermissions`` and ``blockReadsOutsideWorkingDirectories`` at
    ``false``, because those two keys decide whether the agent acts on the
    machine or stops to ask.

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
    # The models behind this proxy are native-1M models (Claude Code reports
    # contextWindow=1000000 for all five aliases), and the hosted NIM endpoint
    # accepted a 300k-token prompt in a direct probe. Use the full window: the
    # old 160k/110k pair was a workaround for decoder degeneration, but the
    # repetition penalties in compat.py now address that at the sampler, so a
    # smaller window only threw context away. 1M is also the highest value
    # Claude Code accepts; there is no unlimited setting.
    #
    # Auto-compact is the user's choice and is not forced here. When it is on,
    # 1M is the documented default for native-1M models; Claude Code caps it at
    # the model window and reserves 2%, which is why a fresh session reports
    # effectiveWindow=980000. When it is off, the same numbers describe the
    # usable window, so a session is not artificially limited either way.
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    # Windows Python defaults stdio to cp1252. Scripts that print arrows,
    # checkmarks or non-Latin text then die with UnicodeEncodeError. UTF-8 mode
    # and unbuffered output make script runs readable and stream as they work.
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    # Native PowerShell tool on Windows: Bash heredocs break on content with
    # backticks and `$`, and here-strings are the safer shape for a long file.
    "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",
    # A stalled subagent used to be waited on for 30 minutes, and the main
    # agent sat at "Waiting for N dynamic workflows to finish" for over an
    # hour while nothing progressed. Four minutes is far beyond the ~90s first
    # byte these backends take, so a real answer still arrives and a dead agent
    # is abandoned instead of blocking the session.
    "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS": "240000",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "240000",
    # 50 concurrent agents, each request taking 60-90s on a free tier, is how a
    # workflow barrier turns into an hour of apparent hang. Six keeps the
    # parallelism useful and the queue short.
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "6",
    "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS": "6",
    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "80",
    # 64k output on a free backend means one rambling answer can stream for
    # 12+ minutes before it stops. 32k is the Claude Code default and still
    # fits a large file write, while halving the worst-case stall.
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
}

# Tools a deny rule must never take away on a machine where the user asked for
# every mode to be unlocked.
UNDENIED_TOOLS = (
    "WebSearch",
    "WebFetch",
    "Bash",
    "PowerShell",
    "Read",
    "Edit",
    "Write",
)

# Auto mode must never come back. The classifier it depends on cannot work
# behind LiteLLM + NVIDIA NIM, and when it fails it blocks Bash/PowerShell
# instead of allowing them.
DISABLE_AUTO_MODE = "disable"

# Hooks. The Stop/SubagentStop guard is deliberately *removed*: it turned a
# finished turn into an error toast, and the model answered the complaint
# instead of doing the work. The remaining hook runs before a write and blocks
# a truncated JSON/Python/JavaScript body, which is the actual "corrupted Write
# tool" failure: the backend ended the tool call mid-object and Claude Code
# saved whatever had arrived.
HOOK_PYTHON = r"C:\Python314\python.exe"
STOP_HOOK = os.path.join(CLAUDE_DIR, "hooks", "stop_guard.py")
VALIDATE_HOOK = os.path.join(CLAUDE_DIR, "hooks", "validate.py")
PRE_WRITE_HOOK = os.path.join(CLAUDE_DIR, "hooks", "pre_write_guard.py")


def _strip_hooks(data: dict, event: str, needle: str) -> bool:
    """Remove hook entries whose command mentions *needle*. True if changed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return False
    kept_entries = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            kept_entries.append(entry)
            continue
        keep = [
            hook
            for hook in inner
            if not (
                isinstance(hook, dict)
                and needle in str(hook.get("command", "")).replace("\\", "/").lower()
            )
        ]
        if len(keep) != len(inner):
            changed = True
            if not keep:
                continue
            entry = dict(entry)
            entry["hooks"] = keep
        kept_entries.append(entry)
    if changed:
        if kept_entries:
            hooks[event] = kept_entries
        else:
            hooks.pop(event, None)
        if not hooks:
            data.pop("hooks", None)
    return changed


def remove_stop_hooks(data: dict) -> bool:
    """Delete stop_guard.py from Stop/SubagentStop. Returns True if removed."""
    changed = False
    for event in ("Stop", "SubagentStop"):
        if _strip_hooks(data, event, "stop_guard.py"):
            changed = True
    return changed


def remove_validate_hook(data: dict) -> bool:
    """Delete the old advisory PostToolUse validator. Returns True if removed."""
    return _strip_hooks(data, "PostToolUse", "validate.py")


def ensure_pre_write_hook(data: dict) -> bool:
    """Register pre_write_guard.py for Write/Edit with an absolute Python path."""
    if not os.path.exists(PRE_WRITE_HOOK):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.get("PreToolUse")
    if not isinstance(entries, list):
        entries = []
        hooks["PreToolUse"] = entries

    command = '"%s" "%s"' % (HOOK_PYTHON, PRE_WRITE_HOOK)
    found = False
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matcher = str(entry.get("matcher") or "")
        if "Write" not in matcher and "Edit" not in matcher:
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        keep = []
        for hook in inner:
            refers = isinstance(hook, dict) and "pre_write_guard.py" in str(
                hook.get("command", "")
            ).replace("\\", "/").lower()
            if not refers:
                keep.append(hook)
                continue
            if found:
                changed = True  # drop a duplicate registration
                continue
            found = True
            if hook.get("command") != command:
                hook["command"] = command
                changed = True
            keep.append(hook)
        if len(keep) != len(inner):
            entry["hooks"] = keep
            changed = True

    entries[:] = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("hooks"), list)
            and not entry["hooks"]
        )
    ]
    if found:
        return changed

    entries.append(
        {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [{"type": "command", "command": command, "timeout": 30}],
        }
    )
    return True


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

    # Full local access is the point of this machine: the user asked for a
    # Claude that runs tools instead of asking permission for each one, and
    # that can read a file outside the folder the session was opened in. Both
    # keys are ordinary preferences, so a settings rewrite (a reset, a new
    # profile, a crash during save) silently puts every action back behind a
    # prompt. Re-assert them here so the posture survives.
    if permissions.get("defaultMode") != "bypassPermissions":
        permissions["defaultMode"] = "bypassPermissions"
        changes.append("permissions.defaultMode=bypassPermissions")
    if permissions.get("blockReadsOutsideWorkingDirectories") is not False:
        permissions["blockReadsOutsideWorkingDirectories"] = False
        changes.append("permissions.blockReadsOutsideWorkingDirectories=false")

    # The top-level window applies to every model that has no per-model
    # override. Set it here as well as in env: env is what Claude Code reads,
    # but a settings reset otherwise leaves /autocompact at the old small
    # value. 1M is the maximum the client accepts.
    if data.get("autoCompactWindow") != 1000000:
        data["autoCompactWindow"] = 1000000
        changes.append("autoCompactWindow=1000000")

    # Autocompact must stay off: the user runs long sessions and the summary
    # loses the exact instructions they are working from. The 1M window above
    # is still set as a belt-and-braces ceiling for a future client version.
    if data.get("autoCompactEnabled") is not False:
        data["autoCompactEnabled"] = False
        changes.append("autoCompactEnabled=false")

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

    if remove_stop_hooks(data):
        changes.append("removed the Stop/SubagentStop guard hooks")
    if remove_validate_hook(data):
        changes.append("removed the old PostToolUse validator")
    if not os.path.exists(PRE_WRITE_HOOK):
        changes.append("pre_write_guard.py is missing; write validation is off")
    elif ensure_pre_write_hook(data):
        changes.append("registered the PreToolUse write validator")

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
