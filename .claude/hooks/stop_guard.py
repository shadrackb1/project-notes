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
  * the plan phrase must be in the FINAL sentences, where a plan lives; a
    message that merely mentions one early is an answer
  * never twice in a row (`stop_hook_active` is true on the continued turn)
  * "let me know ..." is a closing courtesy, and an ending "?" is a real
    question to the user; both are ignored

Run `python stop_guard.py --selftest` to check the classifier's cases.

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

# A sentence that announces the next action instead of performing it.
#
# The first version anchored this at the *start of the message*, which missed
# the most common real shape by a mile: a model inspects something, reports
# what it found, and then says what it is about to do in the LAST sentence.
#   "I see the issue - the path on line 846 is corrupted. Let me fix it."
# That ended the turn and nothing ran. The check now looks at the final
# sentences, where a plan actually lives.
PLAN_PHRASE = re.compile(
    r"^(?:let me|let's|let us|i'll|i will|i am going to|i'm going to|"
    r"i need to|i should|we should|we'll|we will|"
    r"next,? i|next,? let|next i'll|next: |now i'll|now let me|now,? i|"
    r"first,? i|first,? let|then i'll|after that,? i'll|"
    r"here'?s (?:my|the) plan|my plan is|the plan is)\b",
    re.IGNORECASE,
)

# Closing courtesies open like a plan but end a turn legitimately.
COURTESY = ("let me know", "i'll keep you posted", "let me summarize")

# How much of the end counts as "the last thing it said".
TAIL_CHARS = 600

# A very long message is a report; only a short one can be nothing but a plan.
MAX_PLAN_CHARS = 4000

REASON = (
    "You stopped after describing the next action instead of taking it. "
    "Take that action now by calling the appropriate tool. Do not repeat the "
    "plan, do not ask for confirmation, and do not reply with another summary."
)


def plan_sentence(text: str) -> str | None:
    """Return the sentence that announces an action, if the message is a plan.

    Deliberately conservative in three ways: a reply that ends in a question is
    a real request for the user, "let me know" is a real close, and a long
    message is treated as an answer.
    """
    lowered = text.lower()
    if any(courtesy in lowered for courtesy in COURTESY):
        return None
    if text.rstrip().endswith("?"):
        return None
    if len(text) > MAX_PLAN_CHARS:
        return None

    tail = text[-TAIL_CHARS:]
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", tail) if part.strip()]
    for sentence in sentences[-2:]:
        if PLAN_PHRASE.match(sentence):
            return sentence

    # No punctuation to split on (a one-line answer), so check the whole thing.
    if len(text) <= 320:
        for sentence in re.split(r"(?<=[.!?])\s+|;\s+", text):
            sentence = sentence.strip()
            if sentence and PLAN_PHRASE.match(sentence):
                return sentence
    return None


# Cases kept beside the classifier so a future edit can be checked in a second
# instead of by reproducing a live stalled turn.
SELFTEST = (
    ("block", "I see the issue - the path on line 846 is corrupted. Let me fix it and run again."),
    ("block", "Let me create the document now."),
    ("block", "The extractor works. Now I'll wire it into the page."),
    ("block", "Config looks right. Next, I'll update the migration."),
    ("block", "Tests pass. I'm going to add the missing case."),
    ("allow", "Done - the file is at src/app.ts and the tests pass."),
    ("allow", "Let me know if you want the other format."),
    ("allow", "I've updated the schema and verified the endpoint returns 200."),
    ("allow", "The guard blocks a plan-only stop. Should I also cover subagents?"),
    ("allow", "Results: 14 passed, 0 failed, coverage unchanged."),
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
    # Checked before reading stdin: a terminal run has no EOF, so reading first
    # would hang the one command meant to be quick.
    if "--selftest" in sys.argv[1:]:
        failures = 0
        for expected, sample in SELFTEST:
            verdict = "block" if plan_sentence(sample) else "allow"
            if verdict != expected:
                failures += 1
                print("FAIL expected %s got %s: %r" % (expected, verdict, sample))
        print("selftest: %d/%d" % (len(SELFTEST) - failures, len(SELFTEST)))
        return 1 if failures else 0

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

    sentence = plan_sentence(text)
    if not sentence:
        return 0

    log("blocked a plan-only stop: %r" % sentence[:120])
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
