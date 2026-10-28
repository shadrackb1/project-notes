"""Compatibility shim between Claude Code and the local LiteLLM proxy.

Two jobs, both about requests the free backends cannot take as-is:

1. Fast mode. Claude Code sends `"speed": "fast"`; LiteLLM forwards it to
   NVIDIA NIM, which rejects it, and the client waits forever on an empty
   response. The field is deleted, together with the `fast-mode-*` beta token.

2. Unknown model ids. Model pickers, stale environment variables and sessions
   started before the alias switch still ask for raw provider ids such as
   `nvidia/nemotron-3.5-lightning-30b-a3b`. LiteLLM only serves the names in
   its config and answers everything else with
   `400 Invalid model name passed in model=...`, which kills the whole agent
   turn. Any model the upstream does not serve is mapped to the alias for its
   tier, and the substitution is logged to compat.log.

3. Gateway model ids. Claude Code wraps every model it discovers through
   /v1/models as `claude-router-<hex of the model id>`. Those ids are not in
   the proxy config, so the tier guess treated all of them as unknown and
   rewrote every picker choice to the default (sonnet) tier: the 550B model
   the user selected was never the model that answered. The hex is decoded
   back to the real id first, so the selected model is the one that runs.

Everything else is a transparent relay: streaming responses are forwarded as
they arrive, so SSE behaviour is unchanged.

    python compat.py        # listens on :8791, forwards to :8788
"""

import html
import http.client
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8788
# Two LiteLLM instances run the same config on adjacent ports (run-proxy.bat
# takes the port as its third argument). The shim prefers whichever answers,
# so a proxy that is restarting — or one that wedged before binding its socket,
# which is exactly what happened on 2026-10-28 — costs a few milliseconds
# instead of failing the turn.
UPSTREAM_PORTS = (8788, 8789)
PORT = 8791

# A port that just refused a connection is tried last for a short while, so
# requests during a restart do not each pay the dead port's timeout first.
_upstream_down: dict[int, float] = {}
_upstream_down_lock = threading.Lock()


def upstream_ports() -> tuple:
    """Candidate upstream ports, ones that recently failed last."""
    now = time.time()
    with _upstream_down_lock:
        dead = {port for port, until in _upstream_down.items() if until > now}
    healthy = [port for port in UPSTREAM_PORTS if port not in dead]
    return tuple(healthy + [port for port in UPSTREAM_PORTS if port in dead])


def note_upstream(port: int, ok: bool) -> None:
    """Remember which upstream answered, and for how long to distrust it."""
    with _upstream_down_lock:
        if ok:
            _upstream_down.pop(port, None)
        else:
            _upstream_down[port] = time.time() + 15

DROP_BODY_KEYS = ("speed",)

# Anthropic's server-side tools (web search, code execution, ...) are listed in
# `tools` and executed by Anthropic's own infrastructure. The free backends
# behind this proxy cannot execute them: LiteLLM passes the entries through,
# NIM ignores them, and Claude Code then waits for a `web_search_tool_result`
# that never arrives - the turn emits `tool_progress` forever and never
# finishes. Removing them makes the turn complete; `mcp__search__*` and
# WebFetch cover the same need with tools that actually run.
SERVER_TOOL_PREFIXES = (
    "web_search_",
    "web_fetch_",
    "code_execution_",
    "bash_",
    "text_editor_",
    "computer_",
    "str_replace_based_edit_tool",
)

# The free models behind this proxy answer with a *plan* instead of acting:
# "Let me create the file and then run the tests", no tool call, turn over.
# Claude Code treats that as a finished turn, so the work never happens. The
# instruction is appended to the end of the system prompt, where recency gives
# it the most weight, and only on requests that actually carry tools.
AGENT_DIRECTIVE = (
    "IMPORTANT: you are an autonomous coding agent. Act, do not narrate. When "
    "a task requires reading, writing, searching or running anything, call "
    "the appropriate tool in this same response. Never end a turn with "
    "'Let me...', 'I'll...', 'First, I...', a colon followed by a plan, or a "
    "summary of what you are about to do; emit the tool call instead. If you "
    "catch yourself describing the next step, stop and call the tool now. A "
    "reply that only describes what you are about to do ends the turn and the "
    "work is never done."
)

# --- Repetition control -----------------------------------------------------
# Observed failure: one assistant message of 25,828 characters that was the
# sentence "I need to actually emit tool calls." repeated 123 times. No file was
# read, written or run, and the turn ended in a loop the user had to watch.
# That is decoder degeneration, and it feeds itself: the loop is replayed back
# to the model from its own history, which makes the next turn worse.
#
# Claude Code never sends a repetition penalty, so the shim adds one - but the
# penalty is itself a correctness hazard, so it stays small. Measured on this
# machine, same prompt ("write a 120-line analysis.py, use the Write tool"),
# 4 samples each, checking the returned file with ast.parse:
#   0.6/0.3 -> 4 of 4 SYNTAX ERRORS: missing operator after a bare name,
#              unterminated string literal, stray U+FEFF, invalid syntax.
#   0.2/0.1 -> 3 of 3 valid, 108-140 lines.
#   none    -> 4 of 4 valid.
# A frequency penalty of 0.6 discounts the very tokens Python code is made of
# (=, quotes, indentation, common identifiers), so long file writes come back
# with characters dropped - which is exactly what "my script got corrupted"
# looked like from the Claude Desktop side. 0.2/0.1 is the trade: still caps
# the runaway loop, still emits intact code.
# Set NIM_FREQUENCY_PENALTY=0 / NIM_PRESENCE_PENALTY=0 to turn either off.
REPETITION_PENALTY = float(os.environ.get("NIM_FREQUENCY_PENALTY", "0.2"))
PRESENCE_PENALTY = float(os.environ.get("NIM_PRESENCE_PENALTY", "0.1"))
_penalty_announced = False

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat.log")
# One generation of history is enough to diagnose a failed session, and an
# unbounded log eventually costs seconds per request on Windows.
LOG_MAX_BYTES = 5 * 1024 * 1024

# --- Reasoning-trace control -------------------------------------------------
# NVIDIA's Nemotron models are reasoning models. They spend the token budget on
# a `thinking` trace and can return a response that carries no text at all when
# the budget runs out first - Claude Code then shows an empty turn that never
# finishes ("claude keeps loading but does nothing"). NIM exposes a chat
# template switch for this; the shim sets it so every turn comes back with an
# answer. Set NIM_ENABLE_THINKING=1 to keep the reasoning trace instead.
THINKING_ENV = "NIM_ENABLE_THINKING"
# Floor for the token budget. Without it a small max_tokens is spent entirely
# on the reasoning trace and the text answer never appears.
MIN_MAX_TOKENS = 4096
MIN_MAX_TOKENS_THINKING = 32768

# Tier aliases the proxy always serves (see model_list in config.yaml).
OPUS = "claude-opus-5.5"
SONNET = "claude-sonnet-5.5"
HAIKU = "claude-haiku-5.5"
DEFAULT_MODEL = SONNET

ROUTER_PREFIX = "claude-router-"

# Exact ids Claude Code discovers through the gateway. Naming them explicitly
# keeps a picker choice exact instead of leaving it to the substring guess.
MODEL_TIERS = {
    "nvidia/nemotron-3-ultra-550b-a55b": OPUS,
    "nvidia/nemotron-3.5-super-vl-120b-a12b": SONNET,
    "stepfun-ai/step-5-preview": SONNET,
    "nvidia/nemotron-3.5-lightning-30b-a3b": HAIKU,
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": HAIKU,
    "meta/muse-glimmer-30b": HAIKU,
    "poolside/laguna-xs-2.1": HAIKU,
    "nvidia/ising-calibration-1.5-31b": HAIKU,
    "meta/llama-3.2-11b-vision-instruct": HAIKU,
}

# Substrings that identify the tier of a raw provider id, most specific first.
# `mythos` is Claude Code's own name for the top model; sessions and settings
# that still use it must land on the flagship, not on the default tier.
TIER_HINTS = (
    (OPUS, ("opus", "fable", "mythos", "ultra", "550b")),
    (HAIKU, ("haiku", "flash", "lightning", "small", "mini", "a3b", "20b", "11b", "xs")),
    (SONNET, ("sonnet", "super", "medium", "a12b", "a55b")),
)

_log_lock = threading.Lock()


def log(message: str) -> None:
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
            except OSError:
                pass  # no log yet, or a previous rotation is still open
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


# `retry_at` is a negative cache: while the proxy is down the catalog probe is
# attempted by every request, and each attempt blocked the request for the
# probe timeout. Backing off turns a restart into a short stall instead of
# minutes of per-token retries.
_served = {"names": set(), "at": 0.0, "retry_at": 0.0}
_served_lock = threading.Lock()
# The catalog endpoint needs the same credentials as the request, so the probe
# borrows them from the most recent call.
_auth = {}


def served_models(force: bool = False) -> set:
    """Model names the upstream proxy accepts, cached for a minute.

    A failure here is not fatal: an empty set leaves requests untouched.
    """
    with _served_lock:
        fresh = _served["names"] and (time.time() - _served["at"] < 60)
        if fresh and not force:
            return _served["names"]
        if not _auth:
            return _served["names"]
        if not force and time.time() < _served["retry_at"]:
            return _served["names"]
        names = set()
        last_exc = None
        payload = {}
        for port in upstream_ports():
            try:
                conn = http.client.HTTPConnection(UPSTREAM_HOST, port, timeout=5)
                conn.request("GET", "/v1/models", headers=dict(_auth))
                payload = json.loads(conn.getresponse().read() or b"{}")
                conn.close()
                note_upstream(port, True)
                break
            except Exception as exc:
                last_exc = exc
                note_upstream(port, False)
        if not payload and last_exc is not None:
            _served["retry_at"] = time.time() + 5
            log("could not list upstream models: %s" % last_exc)
        for entry in payload.get("data") or []:
            name = entry.get("id") if isinstance(entry, dict) else None
            if name:
                names.add(name)
        if names:
            _served["names"] = names
            _served["at"] = time.time()
            _served["retry_at"] = 0.0
        return _served["names"]


def remember_auth(headers) -> None:
    for key in ("x-api-key", "authorization"):
        value = headers.get(key)
        if value:
            _auth[key] = value


def decode_router_model(model: str) -> str | None:
    """Recover the real model id from `claude-router-<hex>`.

    Claude Code's gateway discovery registers each `/v1/models` entry under a
    name it builds itself, and asks for that name on every request. The hex is
    the UTF-8 model id. Undecodable names (a future wrapper, a truncated id)
    return None and keep the old behaviour.
    """
    if not model.startswith(ROUTER_PREFIX):
        return None
    blob = model[len(ROUTER_PREFIX):].strip()
    if not blob or len(blob) % 2:
        return None
    try:
        decoded = bytes.fromhex(blob).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded and decoded.isprintable() else None


_announced = set()
_announced_lock = threading.Lock()
_directive_announced = False


def announce(source: str, target: str) -> None:
    """Log a rewrite once. Every token of a long session repeats it otherwise."""
    key = (source, target)
    with _announced_lock:
        if key in _announced:
            return
        _announced.add(key)
    log("model %r is routed to %r" % (source, target))


def tier_for(model: str) -> str:
    exact = MODEL_TIERS.get(model)
    if exact:
        return exact
    low = model.lower()
    for alias, hints in TIER_HINTS:
        if any(hint in low for hint in hints):
            return alias
    return DEFAULT_MODEL


def add_agent_directive(payload: dict) -> bool:
    """Make "describe, don't act" responses less likely. Returns True if added.

    Anthropic requests carry the prompt in `system` (string or block list);
    OpenAI-shaped ones carry it as the first message. Both are handled, and an
    already-present directive is never duplicated, so a client retry of the
    same conversation does not accumulate copies.
    """
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return False

    global _directive_announced

    def note(message: str) -> None:
        global _directive_announced
        if not _directive_announced:
            _directive_announced = True
            log(message)

    system = payload.get("system")
    if isinstance(system, str):
        if AGENT_DIRECTIVE in system:
            return False
        payload["system"] = system.rstrip() + "\n\n" + AGENT_DIRECTIVE
        note("agent directive appended to a string system prompt")
        return True
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and AGENT_DIRECTIVE in str(block.get("text", "")):
                return False
        payload["system"] = system + [{"type": "text", "text": AGENT_DIRECTIVE}]
        note("agent directive appended to a block system prompt")
        return True

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "system":
        content = first.get("content")
        text = content if isinstance(content, str) else ""
        if AGENT_DIRECTIVE in text:
            return False
        first["content"] = (text.rstrip() + "\n\n" + AGENT_DIRECTIVE).strip()
        note("agent directive appended to an OpenAI system message")
        return True
    return False


def tame_repetition(payload: dict) -> bool:
    """Add the repetition penalties Claude Code never sends. True if added.

    Only fills in a value the caller left out, so an explicit choice (including
    a deliberate 0) always wins. Both are standard OpenAI-compatible sampling
    fields and the backend accepted them for chat, tool and thinking requests.
    """
    global _penalty_announced
    changed = False
    if not isinstance(payload.get("frequency_penalty"), (int, float)) and REPETITION_PENALTY > 0:
        payload["frequency_penalty"] = REPETITION_PENALTY
        changed = True
    if not isinstance(payload.get("presence_penalty"), (int, float)) and PRESENCE_PENALTY > 0:
        payload["presence_penalty"] = PRESENCE_PENALTY
        changed = True
    if changed and not _penalty_announced:
        _penalty_announced = True
        log("repetition penalties applied: frequency=%s presence=%s"
            % (REPETITION_PENALTY, PRESENCE_PENALTY))
    return changed


def drop_server_tools(payload: dict) -> bool:
    """Remove Anthropic server-side tool entries the backend cannot execute."""
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    kept = []
    dropped = []
    for tool in tools:
        kind = str(tool.get("type") or "") if isinstance(tool, dict) else ""
        if kind.startswith(SERVER_TOOL_PREFIXES):
            dropped.append(tool.get("name") or kind)
        else:
            kept.append(tool)
    if not dropped:
        return False
    payload["tools"] = kept
    log("dropped server-side tools the backend cannot run: %s" % ", ".join(dropped))
    return True


def normalise_model(payload: dict) -> bool:
    """Point the request at a model the proxy serves. Returns True if rewritten."""
    requested = payload.get("model")
    if not isinstance(requested, str) or not requested:
        return False

    # Decode before matching: a gateway id hides the real model, and without
    # this every /model choice was treated as unknown and downgraded.
    decoded = decode_router_model(requested)
    model = decoded or requested
    served = served_models()

    # A decoded id is a real config entry. Use it even when the catalog probe
    # failed (proxy still starting), because it is what the user selected.
    if model in served or (decoded and not served):
        if model != requested:
            payload["model"] = model
            announce(requested, model)
            return True
        return False
    if not served:
        return False

    alias = tier_for(model)
    if alias in served:
        payload["model"] = alias
        announce(requested, alias)
        return True
    # The tier alias itself is not in the served NVIDIA list (e.g. claude-sonnet-5.5
    # vs. nvidia/nemotron-3.5-super-vl-120b-a12b). Pick a real served model in the
    # same tier by matching the tier hints against the served ids.
    # TIER_HINTS holds (alias, (hint, ...)) pairs, so the hints of one tier are
    # the second element. Iterating the pairs directly yields the tuple of
    # hints, not the strings, and `hint in low` then raises TypeError - which
    # used to kill every request with an empty reply.
    hints = tuple(hint for _, group in TIER_HINTS if _ == alias for hint in group)
    for candidate in sorted(served):
        low = candidate.lower()
        if any(hint in low for hint in hints):
            payload["model"] = candidate
            announce(requested, candidate)
            return True
    return False


def control_thinking(payload: dict) -> bool:
    """Turn the backend's reasoning trace on or off and guarantee a text answer.

    Anthropic's `thinking.budget_tokens` has no NIM equivalent, so the request
    field is replaced by the chat-template switch the backend understands.
    """
    changed = False
    thinking = payload.get("thinking")
    enable = isinstance(thinking, dict) and thinking.get("type") == "enabled"
    if "thinking" in payload:
        payload.pop("thinking", None)
        changed = True
    if os.environ.get(THINKING_ENV, "0") == "1":
        enable = True
    kwargs = payload.get("chat_template_kwargs")
    original = dict(kwargs) if isinstance(kwargs, dict) else None
    merged = dict(original) if original else {}
    # The flag has to be written explicitly: an absent chat_template_kwargs
    # leaves the backend's default (reasoning on), and a backend that reasons
    # first can answer with no text at all.
    merged["enable_thinking"] = enable
    if merged != original:
        payload["chat_template_kwargs"] = merged
        changed = True
    floor = MIN_MAX_TOKENS_THINKING if enable else MIN_MAX_TOKENS
    max_tokens = payload.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens < floor:
        payload["max_tokens"] = floor
        changed = True
    return changed


def read_body(handler) -> bytes:
    """Read a request body that may be chunked or length-delimited."""
    if handler.headers.get("transfer-encoding", "").lower().strip() == "chunked":
        chunks = []
        while True:
            line = handler.rfile.readline(65536).strip()
            if not line:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                handler.rfile.readline(65536)  # trailing CRLF
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.read(2)  # CRLF after chunk
        return b"".join(chunks)

    length = int(handler.headers.get("content-length") or 0)
    return handler.rfile.read(length) if length else b""


def scrub(body: bytes, content_type: str) -> bytes:
    if not body or "json" not in content_type.lower():
        return body
    try:
        payload = json.loads(body)
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    changed = False
    for key in DROP_BODY_KEYS:
        if key in payload:
            payload.pop(key, None)
            changed = True
    if normalise_model(payload):
        changed = True
    if control_thinking(payload):
        changed = True
    if drop_server_tools(payload):
        changed = True
    if add_agent_directive(payload):
        changed = True
    if tame_repetition(payload):
        changed = True
    if not changed:
        return body
    return json.dumps(payload, separators=(",", ":")).encode()


# --- Native tool-call text --------------------------------------------------
# Nemotron 3 Ultra occasionally emits its tool call as MiniMax-style XML text
# instead of an OpenAI tool_calls / Anthropic tool_use block:
#
#   <minimax:tool_call>
#   <invoke name="Read">
#   <parameter name="file_path">/some/file</parameter>
#   </invoke>
#   </minimax:tool_call>
#
# Claude Code sees a text-only answer, ends the turn, and the stop hook fires.
# The next turn then answers instead of acting, which is exactly the "stops
# after the hook" behaviour. This is an adapter mismatch, not a model plan, so
# the shim parses the markup and emits real tool_use blocks. The parser is
# deliberately tolerant: the model also writes `name="command>` (missing quote)
# and `<parameter=task_id>`.
TEXT_TOOL_PREFIXES = ("<minimax:tool_call", "<invoke", "<tool_call", "<function_call")
TEXT_TOOL_OPEN = re.compile(r"<\s*(?:minimax:)?tool_call\b", re.IGNORECASE)
TEXT_TOOL_INVOKE = re.compile(
    r"<\s*invoke\s+name\s*=\s*[\"']?([^\"'\s>]+)", re.IGNORECASE
)
TEXT_TOOL_PARAM = re.compile(
    r"<\s*parameter\s*(?:name\s*=\s*[\"']?([^\"'\s>]+)[\"']?|=\s*([^\"'\s>]+))\s*>",
    re.IGNORECASE,
)
TEXT_TOOL_PARAM_END = re.compile(r"</\s*parameter\s*>", re.IGNORECASE)
TEXT_TOOL_INVOKE_END = re.compile(r"</\s*invoke\s*>", re.IGNORECASE)
TEXT_TOOL_WRAPPER_END = re.compile(r"</\s*(?:minimax:)?tool_call\s*>", re.IGNORECASE)

# Parameter values that are text even when they happen to look like JSON.
TEXT_PARAMS = {
    "content", "command", "old_string", "new_string", "prompt", "file_path",
    "path", "pattern", "description", "task_id", "subject", "activeform",
    "status", "query", "url", "skill", "notebook_path", "cell_id", "source",
    "new_source", "output", "title", "message",
}

TOOL_NAMES = {
    name.lower(): name
    for name in (
        "Read", "Write", "Edit", "MultiEdit", "Bash", "PowerShell", "Glob",
        "Grep", "Task", "TaskStop", "TodoWrite", "NotebookEdit", "WebFetch",
        "WebSearch", "Skill", "KillShell", "BashOutput", "ExitPlanMode",
        "AskUserQuestion",
    )
}


def canonical_tool(name: str) -> str:
    return TOOL_NAMES.get(name.strip().lower(), name.strip())


def coerce_param(key: str, value: str):
    """Turn one <parameter> body into a tool input value.

    Most Claude Code inputs are strings. Keep those verbatim (a Write body or a
    Bash command can contain JSON that must not be parsed). Only decode a value
    that looks like a JSON object/array for a parameter that is not a known
    text field, which covers TodoWrite-style structured inputs.
    """
    if key.lower() in TEXT_PARAMS:
        if key.lower() in {"content", "old_string", "new_string", "new_source", "source"}:
            return value.strip("\n")
        return value.strip()
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return stripped


def parse_text_tool_calls(text: str) -> tuple[str, list[dict], str]:
    """Split a response into (prefix, tool calls, suffix).

    Returns the text unchanged with no calls when the response is ordinary
    prose. Only complete <invoke> blocks are converted; a truncated block is
    left as text so a half-written call can never turn into a fake tool use.
    """
    starts = [m.start() for m in TEXT_TOOL_OPEN.finditer(text)]
    starts += [m.start() for m in TEXT_TOOL_INVOKE.finditer(text)]
    if not starts:
        return text, [], ""
    first = min(starts)

    calls: list[dict] = []
    last_end = first
    pos = first
    while True:
        match = TEXT_TOOL_INVOKE.search(text, pos)
        if not match:
            break
        end = TEXT_TOOL_INVOKE_END.search(text, match.end())
        if not end:
            break
        name = canonical_tool(html.unescape(match.group(1)))
        body = text[match.end():end.start()]
        params: dict = {}
        ppos = 0
        while True:
            pm = TEXT_TOOL_PARAM.search(body, ppos)
            if not pm:
                break
            key = html.unescape((pm.group(1) or pm.group(2) or "").strip())
            vend = TEXT_TOOL_PARAM_END.search(body, pm.end())
            if not vend:
                break
            raw_value = body[pm.end():vend.start()]
            if raw_value.startswith("\n"):
                raw_value = raw_value[1:]
            if raw_value.endswith("\n"):
                raw_value = raw_value[:-1]
            params[key] = coerce_param(key, html.unescape(raw_value))
            ppos = vend.end()
        if name:
            calls.append({"name": name, "input": params})
        last_end = end.end()
        pos = end.end()

    if not calls:
        return text, [], ""

    suffix = text[last_end:]
    suffix = TEXT_TOOL_WRAPPER_END.sub("", suffix, count=1)
    return text[:first], calls, suffix


def tool_use_block(call: dict) -> dict:
    return {
        "type": "tool_use",
        "id": "toolu_" + uuid.uuid4().hex,
        "name": call["name"],
        "input": call["input"],
    }


def transform_message(message: dict) -> dict:
    """Convert textual tool calls in a non-streaming Anthropic message."""
    content = message.get("content")
    if not isinstance(content, list):
        return message
    new_content = []
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            prefix, calls, suffix = parse_text_tool_calls(block.get("text") or "")
            if calls:
                changed = True
                if prefix:
                    new_content.append({"type": "text", "text": prefix})
                new_content.extend(tool_use_block(call) for call in calls)
                if suffix.strip():
                    new_content.append({"type": "text", "text": suffix})
                continue
        new_content.append(block)
    if changed:
        message["content"] = new_content
        message["stop_reason"] = "tool_use"
        log("converted textual tool call(s) in a non-streaming response")
    return message


class SSETransformer:
    """Rewrite one Anthropic SSE response while it streams.

    Normal text is forwarded as it arrives. Only a suffix that could be the
    start of a native tool tag is held back; at content_block_stop the held
    text is parsed, and real tool_use blocks replace it when complete. That
    keeps ordinary streaming unchanged while fixing the 5% of turns that would
    otherwise end as text.
    """

    def __init__(self, write):
        self.write = write
        self.text_blocks: dict[int, dict] = {}
        self.tool_mode = False
        self.stop_reason = None

    def emit(self, obj: dict) -> None:
        payload = "event: %s\ndata: %s\n\n" % (
            obj.get("type", "message"),
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        )
        self.write(payload.encode("utf-8"))

    def emit_text(self, index: int, text: str) -> None:
        if text:
            self.emit({
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            })

    def release(self, index: int, block: dict, final: bool = False) -> None:
        """Forward safe text and keep only a possible native-tool tag."""
        pending = block.get("pending", "")
        if not pending or self.tool_mode:
            return
        lowered = pending.lower()
        tag_at = None
        for tag in TEXT_TOOL_PREFIXES:
            pos = lowered.find(tag)
            if pos != -1 and (tag_at is None or pos < tag_at):
                tag_at = pos
        if tag_at is not None:
            if tag_at:
                self.emit_text(index, pending[:tag_at])
            block["pending"] = pending[tag_at:]
            return

        hold = 0
        if not final:
            for tag in TEXT_TOOL_PREFIXES:
                for length in range(1, min(len(tag), len(pending)) + 1):
                    if pending[-length:].lower() == tag[:length]:
                        hold = max(hold, length)
        safe = pending[:-hold] if hold else pending
        if safe:
            self.emit_text(index, safe)
        block["pending"] = pending[-hold:] if hold else ""

    def feed(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        event = ""
        data = []
        for line in text.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if not data:
            if raw.strip():
                self.write(raw.rstrip(b"\r\n") + b"\n\n")
            return
        try:
            obj = json.loads("\n".join(data))
        except ValueError:
            self.write(raw.rstrip(b"\r\n") + b"\n\n")
            return
        self.handle(obj)

    def handle(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "content_block_start":
            if self.tool_mode:
                return
            block = obj.get("content_block") or {}
            index = int(obj.get("index", 0))
            if block.get("type") == "text":
                initial = block.get("text") or ""
                start = dict(obj)
                start["content_block"] = dict(block)
                start["content_block"]["text"] = ""
                self.emit(start)
                state = {"pending": initial}
                self.text_blocks[index] = state
                self.release(index, state)
            else:
                self.emit(obj)
        elif kind == "content_block_delta":
            if self.tool_mode:
                return
            index = int(obj.get("index", 0))
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta" and index in self.text_blocks:
                state = self.text_blocks[index]
                state["pending"] = state.get("pending", "") + (delta.get("text") or "")
                self.release(index, state)
            else:
                self.emit(obj)
        elif kind == "content_block_stop":
            if self.tool_mode:
                return
            index = int(obj.get("index", 0))
            state = self.text_blocks.pop(index, None)
            if state is None:
                self.emit(obj)
                return
            self.release(index, state, final=True)
            pending = state.get("pending", "")
            prefix, calls, suffix = parse_text_tool_calls(pending)
            if not calls:
                self.emit_text(index, pending)
                self.emit(obj)
                return

            if prefix:
                self.emit_text(index, prefix)
            self.emit({"type": "content_block_stop", "index": index})
            for offset, call in enumerate(calls, start=1):
                new_index = index + offset
                self.emit({
                    "type": "content_block_start",
                    "index": new_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_" + uuid.uuid4().hex,
                        "name": call["name"],
                        "input": {},
                    },
                })
                self.emit({
                    "type": "content_block_delta",
                    "index": new_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(call["input"], ensure_ascii=False),
                    },
                })
                self.emit({"type": "content_block_stop", "index": new_index})
            if suffix.strip():
                new_index = index + len(calls) + 1
                self.emit({
                    "type": "content_block_start",
                    "index": new_index,
                    "content_block": {"type": "text", "text": ""},
                })
                self.emit_text(new_index, suffix)
                self.emit({"type": "content_block_stop", "index": new_index})
            self.tool_mode = True
            self.stop_reason = "tool_use"
            log("converted %d textual tool call(s) into tool_use" % len(calls))
        elif kind == "message_delta":
            if self.stop_reason:
                obj.setdefault("delta", {})["stop_reason"] = self.stop_reason
            self.emit(obj)
        elif kind in ("message_stop", "ping", "error"):
            self.emit(obj)
        else:
            if not self.tool_mode:
                self.emit(obj)


def clean_headers(headers):
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if low in ("host", "content-length", "transfer-encoding", "connection"):
            continue
        if low == "anthropic-beta":
            kept = [b for b in value.split(",") if not b.strip().startswith("fast-mode-")]
            if not kept:
                continue
            value = ",".join(kept)
        out[key] = value
    return out


class Shim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-compat"

    def log_message(self, *args):
        pass

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _finish_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _fail(self, status, message):
        """Answer with a JSON error instead of dropping the connection.

        An unhandled exception in the handler closed the socket with no bytes
        at all, which Claude Code reads as a hung request rather than an error.
        """
        payload = json.dumps({"error": {"type": "shim_error", "message": message}}).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def _relay(self):
        try:
            self._relay_inner()
        except Exception as exc:  # noqa: BLE001
            log("relay failed: %r" % (exc,))
            self._fail(500, str(exc))

    def _relay_inner(self):
        # Liveness is answered here, not forwarded. Callers (the launcher and
        # the watchdog) use it to decide whether the shim itself is up; when it
        # was relayed, a proxy restart made the shim look dead and the watchdog
        # restarted a healthy process instead of the proxy.
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/health/liveliness", "/health/readiness"):
            payload = b"I'm alive!"
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        remember_auth(self.headers)
        body = read_body(self)
        started = time.time()
        body_size = len(body)
        body = scrub(body, self.headers.get("content-type", ""))
        headers = clean_headers(self.headers)
        if body:
            headers["Content-Length"] = str(len(body))

        conn = None
        resp = None
        last_error = None
        # Failover across the two proxy instances. Both are restarted on config
        # changes and by the watchdog, and either can wedge before binding; a
        # retry against the other one turns "connection refused" into a normal
        # answer instead of a 502 the client has to give up on. The connection
        # is only established here — once the response is streaming there is no
        # second copy of the request to replay, so a mid-stream failure still
        # surfaces to the client.
        for port in upstream_ports():
            for attempt in range(2):
                try:
                    conn = http.client.HTTPConnection(UPSTREAM_HOST, port, timeout=900)
                    conn.request(self.command, self.path, body=body or None, headers=headers)
                    resp = conn.getresponse()
                    note_upstream(port, True)
                    break
                except Exception as exc:  # this instance is down or restarting
                    last_error = exc
                    note_upstream(port, False)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if attempt < 1:
                        time.sleep(0.6)
            if resp is not None:
                break
        if resp is None:
            payload = json.dumps({"error": {"type": "upstream_unreachable", "message": str(last_error)}}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        content_type = (resp.getheader("content-type") or "").lower()
        content_encoding = (resp.getheader("content-encoding") or "").lower()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(key, value)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        first_byte = None
        try:
            reader = getattr(resp, "read1", resp.read)
            transformable = content_encoding in ("", "identity")
            if transformable and "text/event-stream" in content_type:
                transformer = SSETransformer(self._write_chunk)
                buffer = b""
                while True:
                    chunk = reader(8192)
                    if not chunk:
                        break
                    if first_byte is None:
                        first_byte = time.time() - started
                    buffer += chunk
                    while True:
                        split = re.search(rb"\r?\n\r?\n", buffer)
                        if not split:
                            break
                        raw_event = buffer[:split.start()]
                        buffer = buffer[split.end():]
                        if raw_event.strip():
                            transformer.feed(raw_event)
                if buffer.strip():
                    transformer.feed(buffer)
                self._finish_chunks()
            elif transformable and "application/json" in content_type:
                body = resp.read()
                if first_byte is None:
                    first_byte = time.time() - started
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "message":
                    payload = transform_message(payload)
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._write_chunk(body)
                self._finish_chunks()
            else:
                while True:
                    chunk = reader(8192)
                    if not chunk:
                        break
                    if first_byte is None:
                        first_byte = time.time() - started
                    self._write_chunk(chunk)
                self._finish_chunks()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            # Timing per request. Without it, a turn that looked frozen was
            # impossible to attribute: slow prefill, a stalled upstream and a
            # client-side wait all produce the same blank screen.
            if body_size > 4000 or (time.time() - started) > 5:
                log(
                    "%s %s body=%dB status=%s first_byte=%s total=%.1fs"
                    % (
                        self.command,
                        self.path,
                        body_size,
                        resp.status,
                        "%.1fs" % first_byte if first_byte is not None else "none",
                        time.time() - started,
                    )
                )

    do_GET = _relay
    do_POST = _relay
    do_DELETE = _relay
    do_PUT = _relay


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # The listen backlog defaults to 5. With dozens of subagents in flight the
    # kernel drops new connections and the client sees ECONNRESET or a connect
    # timeout - Claude Code reports that as an API error, and the turn dies
    # before it reaches the proxy. 256 is the Windows socket limit.
    request_queue_size = 256
    # On Windows SO_REUSEADDR lets a second process bind the same port, so two
    # shims could end up sharing :8791 and answering at random (that happened:
    # duplicate copies from the wrapper and the startup script). Binding
    # exclusively makes the second copy fail and exit instead.
    allow_reuse_address = False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    try:
        server = Server(("127.0.0.1", port), Shim)
    except OSError as exc:
        # Another copy already holds the port: nothing to do.
        log("port %d unavailable (%s); exiting" % (port, exc))
        sys.exit(0)
    print(
        "compat shim on 127.0.0.1:%d -> %s:%s"
        % (port, UPSTREAM_HOST, ", ".join(str(p) for p in UPSTREAM_PORTS)),
        flush=True,
    )
    log("shim listening on %d" % port)
    server.serve_forever()
