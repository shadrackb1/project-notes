"""Stop hook: make "I'll do it" actually do it.

The free models behind this machine's proxy sometimes answer a task with a
plan instead of a tool call - "Let me create the document now. I'll build it
with python-docx." - and Claude Code treats that as a finished turn, so
nothing runs. Repeating the prompt just produces another plan.

Claude Code runs Stop hooks when a turn ends. Exiting 2 blocks the stop and
feeds the message on stderr back to the model, which then continues the same
turn. That is the one place where this can be fixed for every model at once,
whatever the proxy is pointed at.

Safety:
  * only fires when the last assistant message has no tool_use block
  * only when the text opens with a plan phrase and is short enough to be one
  * never twice in a row (`stop_hook_active` is true on the continued turn)
  * "let me know ..." is a closing courtesy, not a plan, and is ignored

Exit codes: 0 = allow the turn to end, 2 = continue the turn.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "stop_guard.log")

# Phrases that open a *plan* rather than a *result*. Anchored at the start of
# the message: "Let me check the config." is narration; a long answer that
# happens to contain "I'll" in the middle is not.
PLAN_OPENERS = re.compile(
    r"^(let me|let's|i'll|i will|i am going to|i'm going to|i need to|"
    r"first,? i|first,? let|next,? i|now,? i|now,? let|alright,? (i'll|let me)|"
    r"okay,? (i'll|let me)|great,? (i'll|let me)|"
    r"here'?s (my|the) plan|my plan is|the plan is)\b",
    re.IGNORECASE,
)

# A plan is short. A long message is an answer, even if it opens the same way.
MAX_PLAN_CHARS = 900

# Closing courtesies that start like a plan but are not one.
COURTESY = ("let me know", "let me summarize", "i'll keep you posted")

REASON = (
    "You ended the turn after describing an action instead of taking it. "
    "Call the tool now and do the work. Do not restate the plan, do not ask "
    "for confirmation, and do not answer with another summary."
)


def log(message: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (dt.datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass


def last_assistant_message(path: str) -> dict | None:
    """Return the newest assistant entry in the transcript JSONL."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in reversed(lines[-400:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message
    return None


def content_blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0

    if payload.get("stop_hook_active"):
        return 0

    transcript = payload.get("transcript_path") or ""
    message = last_assistant_message(transcript) if transcript else None
    if not message:
        return 0

    blocks = content_blocks(message)
    if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks):
        return 0

    text = " ".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        return 0

    # Test switch: proves the hook is actually wired into a live turn. Never
    # set in normal use.
    if os.environ.get("STOP_GUARD_FORCE") == "1":
        log("forced block for wiring test")
        print(REASON, file=sys.stderr)
        return 2

    lowered = text.lower()
    if any(courtesy in lowered for courtesy in COURTESY):
        return 0
    if len(text) > MAX_PLAN_CHARS:
        return 0
    if not PLAN_OPENERS.match(text):
        return 0

    log("blocked a plan-only stop: %r" % text[:120])
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
