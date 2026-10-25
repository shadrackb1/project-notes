"""One command that says whether the whole Claude Code stack is healthy.

The setup has four moving parts (Claude Code, the wrapper, the compat shim,
the LiteLLM proxy) plus three files that must stay in a known state
(settings.json, config.yaml, the hooks). Checking them by hand is how a small
breakage turns into a broken afternoon.

    python doctor.py            # report, exit 1 if anything is wrong
    python doctor.py --smoke    # also send one real tool-calling request
    python doctor.py --write-lock   # record current versions as the baseline

Writes health.json next to this file so the scheduled maintenance task, the
toast, and a human all read the same result.
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
CONFIG = os.path.join(HERE, "config.yaml")
LOCK = os.path.join(HERE, "versions.lock.json")
HEALTH = os.path.join(HERE, "health.json")
CLAUDE_EXE = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
PYTHON = r"C:\Python314\python.exe"

REQUIRED_MODELS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5.5",
    "claude-sonnet-5.5",
    "claude-haiku-5.5",
    "nvidia/nemotron-3-ultra-550b-a55b",
)


def read_env() -> dict:
    values = {}
    path = os.path.join(HERE, ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


ENV = read_env()


def http_text(url: str, timeout: float = 6.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def models(base: str) -> list[str]:
    request = urllib.request.Request(base + "/v1/models")
    request.add_header("x-api-key", ENV.get("LITELLM_MASTER_KEY", ""))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except Exception:  # noqa: BLE001
        return []
    return [entry.get("id") for entry in payload.get("data") or [] if isinstance(entry, dict)]


def version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except Exception:  # noqa: BLE001
        return None


def claude_version() -> str | None:
    for candidate in (CLAUDE_EXE, "claude"):
        try:
            result = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").strip().splitlines()
        if text:
            return text[0].strip()
    return None


def run_script(name: str) -> tuple[int, str]:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return 0, "missing"
    try:
        result = subprocess.run(
            [PYTHON, path] + (["--check"] if name == "settings-guard.py" else []),
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def smoke() -> tuple[bool, str]:
    """One real request: does a tool call survive the whole chain?"""
    payload = {
        "model": "claude-haiku-5.5",
        "max_tokens": 64,
        "stream": False,
        "tools": [{
            "name": "Ping",
            "description": "Return the word pong",
            "input_schema": {"type": "object", "properties": {"word": {"type": "string"}}},
        }],
        "messages": [{"role": "user", "content": "Call the Ping tool with word=pong."}],
    }
    request = urllib.request.Request(
        "http://127.0.0.1:8791/v1/messages",
        data=json.dumps(payload).encode(), method="POST",
    )
    request.add_header("x-api-key", ENV.get("LITELLM_MASTER_KEY", ""))
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.loads(response.read())
    except Exception as error:  # noqa: BLE001
        return False, repr(error)[:120]
    blocks = body.get("content") or []
    used = [block.get("name") for block in blocks if block.get("type") == "tool_use"]
    return bool(used), "tool_use=%s" % used


def main() -> int:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    proxy = http_text("http://127.0.0.1:8788/health/liveliness") or ""
    shim = http_text("http://127.0.0.1:8791/health/liveliness") or ""
    add("proxy :8788", "alive" in proxy.lower(), proxy.strip()[:40] or "no answer")
    add("shim :8791", "alive" in shim.lower(), shim.strip()[:40] or "no answer")

    served = models("http://127.0.0.1:8791")
    missing = [name for name in REQUIRED_MODELS if name not in served]
    add("model list", not missing, "%d served%s" % (
        len(served), "" if not missing else "; missing " + ", ".join(missing)))

    config_code, config_out = run_script("config-guard.py")
    add("config.yaml", config_code == 0, config_out.splitlines()[-1] if config_out else "valid")

    settings_code, settings_out = run_script("settings-guard.py")
    add("settings.json", settings_code == 0, settings_out.splitlines()[-1] if settings_out else "clean")

    data = settings()
    env = data.get("env") or {}
    hooks = data.get("hooks") or {}
    hook_ok = all(
        any("stop_guard.py" in str(h.get("command", "")) for e in (hooks.get(event) or [])
            for h in (e.get("hooks") or []))
        for event in ("Stop", "SubagentStop")
    )
    add("stop hook", hook_ok, "registered" if hook_ok else "NOT registered")
    add("auto mode", data.get("disableAutoMode") == "disable",
        str(data.get("disableAutoMode")))
    # Not a fixed number: what matters is enough headroom that a turn is still
    # generated below the range where these backends start to loop. 30k is the
    # floor; the configured pair is 110k of 160k.
    window = env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    context = env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    try:
        headroom = int(context) - int(window)
    except (TypeError, ValueError):
        headroom = 0
    add("compaction", headroom >= 30000,
        "%s of %s (%+d headroom)" % (window, context, headroom))

    current = {
        "litellm": version("litellm"),
        "mcp": version("mcp"),
        "claude_code": claude_version(),
    }
    drift = []
    baseline = {}
    if os.path.exists(LOCK):
        try:
            with open(LOCK, encoding="utf-8") as handle:
                baseline = json.load(handle)
        except (OSError, ValueError):
            baseline = {}
    for name, value in current.items():
        if baseline.get(name) and value and baseline[name] != value:
            drift.append("%s %s -> %s" % (name, baseline[name], value))
    add("versions", not drift, "; ".join(drift) if drift else "match the baseline")

    if "--write-lock" in sys.argv[1:]:
        with open(LOCK, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2)
            handle.write("\n")
        print("wrote %s: %s" % (LOCK, json.dumps(current)))

    if "--smoke" in sys.argv[1:]:
        ok, detail = smoke()
        add("smoke", ok, detail)

    problems = [check for check in checks if not check["ok"]]
    report = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "healthy": not problems,
        "checks": checks,
    }
    try:
        with open(HEALTH, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    except OSError:
        pass

    for check in checks:
        print("%s %-14s %s" % ("ok  " if check["ok"] else "FAIL", check["check"], check["detail"]))
    print("healthy" if not problems else "%d problem(s)" % len(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
