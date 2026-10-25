# Claude Code on NVIDIA NIM

Claude Code speaks the Anthropic Messages API. This stack makes it talk to
NVIDIA NIM (and a few other free backends) instead, with the same streaming,
tool calls and thinking budgets.

```
claude (wrapper)  ->  compat shim :8791  ->  LiteLLM proxy :8788  ->  NIM
                          |                       |
                     strips fields          maps claude-* aliases,
                     backends reject        retries, falls back
```

| Layer | File | Port | Supervised by |
| --- | --- | --- | --- |
| Launcher | `~/.local/bin/claude` | – | you |
| Compatibility shim | `compat.py` | 8791 | watchdog |
| Proxy | `config.yaml` via `litellm` | 8788 | watchdog |
| Watchdog | `watchdog.ps1` | – | keeper |
| Keeper | `keeper.ps1` | – | login |

The watchdog restarts the proxy and the shim. The keeper restarts the
watchdog, runs `maintenance.ps1` every 10 minutes, and raises anything that is
missing after a reboot. Both are single-instance through named mutexes, so the
Startup entry and the HKCU autostart entry can both fire without duplication.

## Daily commands

```powershell
python doctor.py               # is everything healthy? exit 1 if not
python doctor.py --smoke       # also send one real tool-calling request
python doctor.py --write-lock  # accept the installed versions as the baseline
.\maintenance.ps1              # self-heal + housekeeping, safe to run anytime
.\register-tasks.ps1           # autostart entry (+ tasks when elevated)
.\register-tasks.ps1 -Check    # what is registered and when it last ran
.\upgrade.ps1 -Check           # installed vs available litellm
.\start-proxy.ps1              # bring the proxy up if it is down
```

State lives next to the scripts:

| File | Meaning |
| --- | --- |
| `health.json` | last `doctor.py` result, one entry per check |
| `maintenance.log` | what the keeper did and whether it was healthy |
| `keeper.log` | keeper lifecycle only |
| `heal.log` | config/settings repairs |
| `litellm-<stamp>.log` | one per proxy start, newest 20 kept |
| `versions.lock.json` | the versions that are known to work |

## When something breaks

**`Connection refused` before the first reply.** The proxy is down.
Run `.\start-proxy.ps1`, then `python doctor.py`. If the proxy is up but the
shim is not, `maintenance.ps1` restarts it on its next pass; run it directly if
you do not want to wait ten minutes.

**`API error` after a long session.** Usually a stalled stream, not the network.
Claude Code compacts at 110k of the 160k window now (see below), which keeps
every request inside the range these backends answer cleanly. If it still
happens, check the newest proxy log for `Timeout` or `RateLimitError`. A
restart is safe; turns already running will fail, so do it between requests.

**The model repeats one sentence and never acts.** Decoder degeneration, not a
prompt problem. A real session produced a 25,828-character message that was
`I need to actually emit tool calls.` repeated 123 times, with no tool call in
it; the turn ended and the user watched a loop. It feeds itself: the loop goes
back into the model's context, so the next turn is worse, and it gets likelier
as the context fills.

The shim adds the repetition penalties Claude Code never sends, in
`tame_repetition()` (`compat.py`):

```
frequency_penalty 0.6   presence_penalty 0.3
```

Measured on this machine with the same loop replayed 40x as the prompt:

| | repeats of one sentence | output | latency |
| --- | --- | --- | --- |
| no penalty | 512 (budget exhausted) | 18,431 chars | 40.7s |
| penalty | 9-11 | 1-4 KB | 2s |

Tool calling is unaffected: `tool_use` still comes back with parsed arguments.
Tune it with `NIM_FREQUENCY_PENALTY` / `NIM_PRESENCE_PENALTY`; `0` disables it,
and a value the client sets explicitly is never overwritten. `stop_guard.py`
also catches the shape after the fact: six or more copies of one sentence in a
single message returns exit 2 with an instruction to emit one call and no
prose.

**"Waiting for N dynamic workflows to finish" for an hour.** Two causes, both
capped in `settings-guard.py` now: a stalled subagent was waited on for 30
minutes (`CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS=240000`), and up to 50 agents were
in flight at once on backends that take 60-90s per request
(`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=6`,
`CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=6`,
`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=80`). A dead agent is now abandoned in
four minutes and the queue stays short enough to finish.

**The model describes a plan and stops.** Claude Code treats a text-only reply
as the end of the turn. `~/.claude/hooks/stop_guard.py` catches the
plan-without-action shape and returns exit 2, which continues the turn with an
instruction to act. It reads the *last sentences* of the message, because that
is where "... Let me fix it and run again." appears; an earlier version only
looked at the first word and missed it. Check the classifier without spending
a model turn:

```powershell
python ~/.claude/hooks/stop_guard.py --selftest
```

If a block seems missing, `stop_guard.log` records each one with the sentence
that triggered it (`blocked a plan-only stop` or `blocked a degenerate stop`). `python doctor.py` reports whether the hook is registered;
settings resets are repaired by `settings-guard.py`.

**`/model` shows only a few entries.** `settings.json` lost
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` or the classifier opt-out. Both
guards re-assert on every proxy start:

```powershell
python settings-guard.py   # idempotent; prints only when it changes something
```

## Upgrades

Never `pip install --upgrade litellm` inside a session. Use the controlled path:

```powershell
.\upgrade.ps1 -Package litellm -Version 1.106.3
```

It records the current version, installs, restarts the proxy, runs a real
tool-call smoke test, and **rolls back automatically** if either step fails.
`claude` itself updates on its own (stable channel); `doctor.py` flags a version
change against `versions.lock.json`, and `--write-lock` accepts it once the
smoke test passes.

## Secrets

Never commit:

* `.env` (NVIDIA key, OpenRouter key, master key)
* `~/.claude/settings.json` (auth tokens)
* `~/.claude.json` (account and session state)
* any `*.log`

`config.yaml` deliberately holds `master_key: os.environ/LITELLM_MASTER_KEY`
rather than the key itself, so it can be copied and diffed safely. Rotating the
key means changing `.env` plus the user environment variable, then restarting
the proxy.

## Why it is built this way

* **One worker.** Granian is limited to a single worker on Windows and reset
  connections mid-body under bursts of subagent requests; uvicorn does not. The
  listen backlog is raised to 256 for the same burst.
* **Explicit fallback chains.** LiteLLM accepts `alias: []` as valid and then
  silently stops failing over. `config-guard.py` rejects that shape and
  restores `config.good.yaml`.
* **Models are declared, not discovered.** Claude-shaped ids keep thinking
  budgets and fine-grained tool streaming enabled; raw provider ids turn them
  off silently.
* **The config is verified on every start,** not edited and hoped for. A bad
  edit is repaired at the next start instead of ending a turn with a 429.
