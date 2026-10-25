"""PostToolUse syntax check for Edit/Write.

Catching a syntax error at the moment the file is written is far cheaper than
discovering it three steps later when a test run fails. On a real error this
exits 2, which feeds the message back to the model so it self-corrects.

Python and JSON are validated in-process. JavaScript is checked with
`node --check` when node is on PATH. Unknown file types are ignored.
"""
import json
import os
import subprocess
import sys
from datetime import datetime

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks.log")


def log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def check_python(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        return None  # file vanished; nothing useful to say
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        return f"{path}:{exc.lineno}: syntax error: {exc.msg}"
    return None


def check_json(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            json.load(fh)
    except json.JSONDecodeError as exc:
        return f"{path}:{exc.lineno}: invalid JSON: {exc.msg}"
    except OSError:
        return None
    return None


def check_node(path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["node", "--check", path],
            capture_output=True, text=True, timeout=25,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return detail[0] if detail else f"{path}: node --check failed"
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    tool = payload.get("tool_name", "")

    if not path:
        log(f"validate: skip tool={tool} (no file_path)")
        return 0

    ext = os.path.splitext(path)[1].lower()
    checker = {".py": check_python, ".json": check_json,
               ".js": check_node, ".mjs": check_node, ".cjs": check_node}.get(ext)

    if checker is None:
        log(f"validate: skip {ext or 'no-ext'} {path}")
        return 0

    problem = checker(path)
    if problem:
        log(f"validate: FAIL {path}")
        # Advisory only. The user supervises every session and asked for no
        # blocking hooks, so this reports the problem and lets the write stand
        # instead of exiting 2 (which Claude Code treats as a block).
        print(f"Syntax check failed after write: {problem}", file=sys.stderr)
        return 0

    log(f"validate: OK {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
