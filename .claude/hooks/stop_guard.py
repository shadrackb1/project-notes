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

# A promise to watch work that is already running. These start like a plan but
# are the correct close after a background agent, workflow, or shell job was
# launched. Blocking them makes Claude Code re-answer a turn that is already
# progressing, which is how "I'll notify you when it completes" turned into a
# stop-hook error in a healthy session.
CLOSING_PROMISE = re.compile(
    r"^(?:i'll|i will|i'm going to|i am going to|let me)\s+"
    r"(?:monitor|watch|wait for|notify|report|check back|follow up|"
    r"let you know|keep you posted)\b",
    re.IGNORECASE,
)

# Tools that hand work to something running after this turn ends. A closing
# promise is only legitimate when one of these was called in the same turn.
BACKGROUND_TOOLS = {"Task", "Agent", "Workflow"}

# How much of the end counts as "the last thing it said".
TAIL_CHARS = 600

# A very long message is a report; only a short one can be nothing but a plan.
MAX_PLAN_CHARS = 4000

REASON = (
    "You stopped after describing the next action instead of taking it. "
    "Take that action now by calling the appropriate tool. Do not repeat the "
    "plan, do not ask for confirmation, and do not reply with another summary."
)

# A different failure with the same visible result. One real message was 25,828
# characters of "I need to actually emit tool calls." repeated 123 times, with
# no tool call anywhere in it. The client treats that as a finished turn, so the
# user watches a loop that never acts.
#
# The instruction has to differ from REASON: repeating the same text back at a
# model that is already looping just selects the loop again. This one names the
# failure, forbids restating it, and asks for a single call.
LOOP_REPEATS = 6
LOOP_SENTENCE_MIN = 25

LOOP_REASON = (
    "Your last message repeated the same sentence over and over and contained "
    "no tool call. That is a decoding failure, not an answer, and the work is "
    "still undone. Do not explain, apologise or restate anything. Reply with "
    "exactly one tool call - no prose before it."
)


def degenerate_repeat(text: str) -> tuple[str, int] | None:
    """Return (sentence, count) when one message loops on itself.

    Checked before the length exemption in `plan_sentence`, because a looping
    message is long by construction and would otherwise be treated as a report.
    A real answer can repeat a short phrase; six copies of a full sentence
    cannot be anything but degeneration.
    """
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", text)
        if len(part.strip()) >= LOOP_SENTENCE_MIN
    ]
    if len(sentences) < LOOP_REPEATS:
        return None
    counts: dict[str, int] = {}
    for sentence in sentences:
        counts[sentence] = counts.get(sentence, 0) + 1
    sentence, count = max(counts.items(), key=lambda item: item[1])
    if count >= LOOP_REPEATS:
        return sentence, count
    return None


def plan_sentence(text: str, allow_closing: bool = False) -> str | None:
    """Return the sentence that announces an action, if the message is a plan.

    Deliberately conservative in three ways: a reply that ends in a question is
    a real request for the user, "let me know" is a real close, and a long
    message is treated as an answer.

    ``allow_closing`` is set only when the current turn already started a
    background agent/workflow/job. In that case a final "I'll monitor them" or
    "I'll notify you when it completes" is a close on running work, not a plan
    with nothing behind it.
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
    if allow_closing and sentences and CLOSING_PROMISE.match(sentences[-1]):
        # Only the final promise is excused. "Let me fix it. I'll notify you."
        # is still a plan to fix something, so a plan sentence immediately
        # before the promise keeps the block.
        prior = sentences[-2] if len(sentences) > 1 else ""
        if not prior or not PLAN_PHRASE.match(prior):
            return None
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
    # The observed loop: 123 copies of one sentence, no tool call.
    ("block", "I need to actually emit tool calls. " * 40),
    ("block", "Let me make the calls now. " * 8 + "That is the plan."),
    # Repetition that is not degeneration: a short list, or a sentence twice.
    ("allow", "Tests: 3 passed. Tests: 3 passed. Lint clean. Build clean."),
)

# Closing promises that are legitimate only after background work was started.
# With the gate off they must still block, or the guard would allow a model to
# promise monitoring without launching anything.
CLOSING_SELFTEST = (
    "The verification workflow has been launched. I'll notify you when it completes.",
    "Three agents are running. I'll monitor them and report results when complete.",
    "The job is running in the background. I'll report back when it finishes.",
)


def log(message: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (dt.datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass


def is_tool_result_entry(entry: dict) -> bool:
    """True for the user-role entries that carry a tool result, not a prompt."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    blocks = [block for block in content if isinstance(block, dict)]
    return bool(blocks) and all(block.get("type") == "tool_result" for block in blocks)


def turn_started_background(path: str) -> bool:
    """True when this turn launched an agent, workflow, or background shell job.

    Only used to excuse a closing promise. A message that says "I'll monitor
    them" after a workflow was launched is a close; the same message with no
    launched work is exactly the plan-only stop this hook exists to catch.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return False
    for line in reversed(lines[-800:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "user" and not is_tool_result_entry(entry):
            return False  # reached the prompt that started this turn
        if kind != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        for block in content_blocks(message):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            payload = block.get("input")
            if name in BACKGROUND_TOOLS:
                return True
            if name == "Bash" and isinstance(payload, dict) and payload.get("run_in_background"):
                return True
    return False


def last_assistant_message(path: str) -> dict | None:
    """Return the newest assistant entry in the transcript JSONL."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in reversed(lines[-800:]):
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
        checked = len(SELFTEST)
        for sample in CLOSING_SELFTEST:
            checked += 2
            if not plan_sentence(sample):
                failures += 1
                print("FAIL closing promise must block without background work: %r" % sample)
            if plan_sentence(sample, allow_closing=True):
                failures += 1
                print("FAIL closing promise must be allowed after background work: %r" % sample)
        print("selftest: %d/%d" % (checked - failures, checked))
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

    loop = degenerate_repeat(text)
    if loop:
        sentence, count = loop
        log("blocked a degenerate stop: one sentence x%d (%d chars): %r" % (count, len(text), sentence[:100]))
        print(LOOP_REASON, file=sys.stderr)
        return 2

    sentence = plan_sentence(
        text, allow_closing=turn_started_background(transcript) if transcript else False
    )
    if not sentence:
        return 0

    log("blocked a plan-only stop: %r" % sentence[:120])
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
