"""Guard for the Claude Code proxy config.

Why this exists
---------------
LiteLLM treats an *empty* fallback list as valid configuration:

    fallbacks:
      - "claude-fable-5.1": []

Nothing is retried on another model when that alias is rate limited, and the
failure is silent until a real 429 kills a turn. That exact shape was in
config.yaml and produced 208 rate-limit and 64 server errors, each ending the
user's session instead of degrading to another model.

So the config is checked on every proxy start:

  * every alias Claude Code can ask for must name at least one fallback
  * no fallback entry may be an empty list
  * the file must parse

If the active config fails, the known-good snapshot (`config.good.yaml`) is
restored and the reason is written to `heal.log`. That makes the fix permanent
instead of an edit someone can silently undo six weeks later.

Exit codes: 0 = clean, 1 = problems found and repaired, 2 = problems remain.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - yaml ships with the proxy
    print("config-guard: PyYAML missing; skipping check")
    raise SystemExit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("CLAUDE_PROXY_CONFIG") or os.path.join(HERE, "config.yaml")
GOOD = os.environ.get("CLAUDE_PROXY_GOOD") or os.path.join(HERE, "config.good.yaml")
PREV = GOOD + ".prev"
LOG = os.path.join(HERE, "heal.log")

# Every model name the launcher / settings.json can send.
REQUIRED_ALIASES = (
    "claude-fable-5.1",
    "claude-fable-5",
    "claude-opus-5.5",
    "claude-sonnet-5.5",
    "claude-haiku-5.5",
)


def log(message: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} config-guard: {message}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    # stdout, not stderr: the caller runs with $ErrorActionPreference = 'Stop',
    # where any stderr from a native command aborts the script mid-call.
    print(line.strip())


def inspect(path: str) -> tuple[list[str], list[str]]:
    """Return (problems, warnings) for the config at `path`."""
    try:
        with open(path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        return [f"cannot read {path}: {error}"], []

    if not isinstance(config, dict):
        return [f"{path} is not a mapping"], []

    entries = (config.get("litellm_settings") or {}).get("fallbacks") or []
    if not isinstance(entries, list) or not entries:
        return ["litellm_settings.fallbacks is missing or empty"], []

    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"fallback entry is not a mapping: {entry!r}")
            continue
        for source, targets in entry.items():
            seen.add(str(source))
            if not targets:
                problems.append(f'fallback for "{source}" is an empty list (nothing is retried)')

    for alias in REQUIRED_ALIASES:
        if alias not in seen:
            problems.append(f'alias "{alias}" has no fallback chain')

    # A model group that is referenced as a fallback but never defined would
    # fail at request time, not at startup, so check that too.
    groups = {
        str(model.get("model_name"))
        for model in (config.get("model_list") or [])
        if isinstance(model, dict)
    }
    warnings: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for targets in entry.values():
            for target in targets or []:
                if target in groups or any(character in str(target) for character in "*["):
                    continue
                warnings.append(f'fallback target "{target}" is not a model group')

    return problems, warnings


def refresh_snapshot() -> None:
    """Keep config.good.yaml equal to the newest config that validated.

    The restore path is only useful if the snapshot holds the current model
    list. It was taken once and never updated, so restoring it silently rolled
    back every alias added afterwards. The previous snapshot is kept beside it.
    """
    try:
        if os.path.exists(GOOD):
            with open(GOOD, "rb") as old, open(CONFIG, "rb") as live:
                if old.read() == live.read():
                    return
            shutil.copyfile(GOOD, PREV)
        shutil.copyfile(CONFIG, GOOD)
        log("refreshed config.good.yaml from the validated config")
    except OSError as error:
        log(f"could not refresh config.good.yaml: {error}")


def main() -> int:
    problems, warnings = inspect(CONFIG)
    for warning in warnings:
        log(f"warning: {warning}")

    if not problems:
        refresh_snapshot()
        return 0

    for problem in problems:
        log(f"problem: {problem}")

    if os.path.exists(GOOD):
        good_problems, _ = inspect(GOOD)
        if not good_problems:
            shutil.copyfile(GOOD, CONFIG)
            log(f"restored {os.path.basename(CONFIG)} from config.good.yaml")
            return 1
        log("known-good snapshot is itself invalid; not restoring")

    log("config is still invalid; the proxy may abort turns on rate limits")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
