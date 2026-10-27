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

## Claude Desktop

The Desktop app (chat and its Code sessions) can use the same gateway as the
CLI. Its Developer Mode calls this "third-party inference", and it stores the
profile differently from Claude Code:

* **Not** `claude_desktop_config.json`. That file registers MCP servers only;
  an API host added there is silently ignored.
* The profile lives in `%LOCALAPPDATA%\Claude-3p\configLibrary\`: one
  `<uuid>.json` per profile plus `_meta.json`, whose `appliedId` selects the
  active one. The app reads it once, at launch.
* Desktop then talks to `compat.py` on :8791 (not :8788). The shim is the layer
  that makes Claude-shaped model ids, server tools and repetition behave.

That profile is now re-applied automatically. `maintenance.ps1` runs
`desktop-3p.py ensure --quiet` on every pass (ten minutes, and at login
through the keeper), so an app upgrade or a profile reset cannot leave the
Desktop on the claude.ai sign-in screen. The same pass restores the rest of
the Desktop setup:

* the MCP servers from `~/.claude.json` are added to the Desktop profile
  (existing entries win; the file is backed up before a change), so Desktop
  has the same `filesystem`, `memory`, `playwright`, `search` and other tools
  as the CLI instead of an empty tool list;
* any top-level skill that exists in `~/.agents/skills` but is missing from
  `~/.claude/skills` is copied in, which is what keeps the imported skills
  from disappearing when a profile is reset or a new one is created.

Like the gateway profile, these are only read at launch: after a repair,
quit the app fully and reopen it.

```powershell
python desktop-3p.py status    # provider, URL, key, MCP count, skill count; exit 1 if not applied
python desktop-3p.py ensure    # write/repair the profile now
python desktop-3p.py remove    # delete only the localhost gateway profile
```

The script reads `LITELLM_MASTER_KEY` from the process environment or
`HKCU\Environment`, and the port from `CLAUDE_PROXY_PORT`, so it never holds a
secret of its own. It is idempotent: on a correct profile it writes nothing
and says nothing, which is why the maintenance log stays quiet unless
something was actually repaired.

After a change, quit the app fully (all `Claude.exe` processes) and reopen it -
closing the window is not enough. Confirm from its own log:

```text
C:\Users\ADMIN\AppData\Local\Claude-3p\logs\main.log
  [custom-3p] Credentials loaded { configSource: 'local', provider: 'gateway' }
  [custom-3p] 3P mode active { provider: 'gateway' }
  [custom-3p] Model discovery: 7 found in 481ms; picker = 7 (discovery)
  [custom-3p] ConfigHealth recomputed { state: 'healthy', provider: 'gateway' }
```

To go back to the normal Claude account: `python desktop-3p.py remove`, then
relaunch. The 3P mode keeps its own data directory, so the ordinary profile
(and its chats) is untouched while this is enabled, but it is also not visible
from inside 3P mode. Never paste an Anthropic API key into the Desktop sign-in
screen to make this work: that bills Anthropic and does not route NIM models.

## Git

Pushes from a Cowork shell used to fail twice over: `authentication (no
credentials)`, then `remote end hung up` until the command timed out.

The shell does not point `HOME` at `C:/Users/ADMIN`, so git never read
`~/.gitconfig` and never saw the `gh` credential helper. It fell back to the
*system* config, whose only helper is Git Credential Manager - a GUI helper.
With nobody to answer it, `git ls-remote` blocked until `timeout` killed it
(exit 124, measured with `HOME=/c/nonexistent-home`). Two settings fix it
without admin rights:

| Setting | Effect |
| --- | --- |
| `GIT_CONFIG_GLOBAL=C:\Users\ADMIN\.gitconfig` (user env) | git reads the real global config whatever `HOME` is |
| `credential.interactive=never` (global) | GCM fails fast instead of hanging, so `gh` answers |

Verified with `HOME` pointed at a nonexistent directory: `git ls-remote` and
`git push --dry-run` both authenticate and finish instead of stalling.
`git-guard.py` re-asserts both on every maintenance pass, so a HelperSelector
tool or a config rewrite cannot bring the hang back.

```powershell
python git-guard.py status    # show the three settings
python git-guard.py           # repair, silent when already correct
```

Tokens do not belong in remote URLs: `git remote set-url origin
https://github.com/owner/repo.git` removes one, and the helper still
authenticates after it is gone (checked on `scope-to-launch`, whose remote was
pointing at `tchukor/scope-to-launch`). `~/.git-credentials` is a leftover
plaintext store that no configured helper reads - it is excluded in
`.git/info/exclude` so it cannot be committed, and the token pasted into a
chat session should be revoked at github.com/settings/tokens.

## When something breaks

**`Connection refused` before the first reply.** The proxy is down.
Run `.\start-proxy.ps1`, then `python doctor.py`. If the proxy is up but the
shim is not, `maintenance.ps1` restarts it on its next pass; run it directly if
you do not want to wait ten minutes.

**`API error` after a long session.** Usually a stalled stream or a rate limit,
not the network. The window is the full 1M now: all five aliases resolve to
native-1M NIM models, and a direct probe accepted a 300k-token prompt. 1M is
the hard ceiling (the model's window and the largest value Claude Code
accepts); there is no unlimited setting. Claude Code reserves 2%, so a fresh
session reports `effectiveWindow=980000`. Auto-compact is off on this machine
by the user's choice; if you turn it back on, it uses the same window. If a
turn still fails, check the newest proxy log for `Timeout` or `RateLimitError`. A
restart is safe; turns already running will fail, so do it between requests.

**`429` or `Too Many Requests` under load.** Two NVIDIA keys are configured.
LiteLLM lists every model twice, once per key, under the same `model_name`, so
`simple-shuffle` splits requests between them and a 429 on one is retried on
the other. The second key lives only in `.env` as `NVIDIA_API_KEY_2`; it is
never written to `config.yaml` or committed. Add a third or fourth by
appending `NVIDIA_API_KEY_3` (or `_4`) to `.env`, then run `build_config.py`
and restart the proxy; the generator discovers the new key automatically.

**Python scripts fail, or print `UnicodeEncodeError`.** python.org installs
only `python.exe`, so Claude's `python3 script.py` used to exit 127 with
`python3: command not found`. `~/.local/bin/python3.exe` is a copy of
`C:\Python314\python.exe` on the user PATH; `maintenance.ps1` refreshes it
when Python updates. `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` and
`PYTHONUNBUFFERED=1` are set in settings, the launcher and the guard, so
arrows and emoji no longer die on cp1252 and progress output streams instead
of waiting for the buffer to fill. Bash heredocs are still fragile on Windows:
write files with the Write/Edit tool, not `cat <<EOF`.

**`File has been modified since read` when writing.** This is Claude Code's
safety check, not a broken Write tool: the file changed after the Read, often
because `npm install`, a formatter, or a generator touched it. Read the file
again and retry. There is no supported switch to turn the check off; if it
stays stuck on an unchanged file, use the filesystem MCP writer or start a new
session, and prefer `Edit` over `Write` for targeted changes.

**The model repeats one sentence and never acts.** Decoder degeneration, not a
prompt problem. A real session produced a 25,828-character message that was
`I need to actually emit tool calls.` repeated 123 times, with no tool call in
it; the turn ended and the user watched a loop. It feeds itself: the loop goes
back into the model's context, so the next turn is worse, and it gets likelier
as the context fills.

The shim adds the repetition penalties Claude Code never sends, in
`tame_repetition()` (`compat.py`). The value matters more than it looks: a
penalty discounts the tokens the answer is built from, and at 0.6 it was
eating characters inside generated code.

```
frequency_penalty 0.2   presence_penalty 0.1
```

Loop, measured with the same degenerate message replayed 40x as the prompt:

| | repeats of one sentence | output | latency |
| --- | --- | --- | --- |
| no penalty | 512 (budget exhausted) | 18,431 chars | 40.7s |
| penalty 0.6 | 9-11 | 1-4 KB | 2s |

Code, measured the same way - "write a 120-line `analysis.py`, use the Write
tool", 4 samples each, the returned file checked with `ast.parse`:

| frequency/presence | valid files | what broke |
| --- | --- | --- |
| 0.6 / 0.3 (old default) | 0 of 4 | missing `=`, unterminated string, stray U+FEFF, invalid syntax |
| 0.2 / 0.1 (now) | 3 of 3 | - |
| 0 (off) | 4 of 4 | - |

That 0-for-4 was the "Claude writes a script, it is corrupted, then it gives up
and only describes what it would do" failure: the model was not hallucinating
about its own bugs, it was reading back files whose operators had been
penalised away. Any client that sets its own penalty still wins.
Tune it with `NIM_FREQUENCY_PENALTY` / `NIM_PRESENCE_PENALTY`; `0` disables it. Repetition is
handled only by the penalty now: the old Stop hook that inspected the finished
message caused more turn-ending errors than it prevented, so it is no longer
registered.

**"Waiting for N dynamic workflows to finish" for an hour.** Two causes, both
capped in `settings-guard.py` now: a stalled subagent was waited on for 30
minutes (`CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS=240000`), and up to 50 agents were
in flight at once on backends that take 60-90s per request
(`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=6`,
`CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=6`,
`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=80`). A dead agent is now abandoned in
four minutes and the queue stays short enough to finish.

**The model describes a plan and stops.** Claude Code treats a text-only reply
as the end of the turn. The Stop/SubagentStop hook that used to force a
continuation is **removed**: when the model was already done, the hook printed
an error and the next turn answered the complaint instead of acting. The
agent directive in `compat.py` stays as the softer nudge, and the model picker
is the real control (a model that plans instead of calling tools cannot be
fixed by a hook). `settings-guard.py` deletes any stale `stop_guard.py`
registration on every repair.

**The model emits XML text instead of a tool call.** Nemotron 3 Ultra
sometimes writes MiniMax-style `<invoke name="Write"><parameter
name="content">...` as *text* rather than a structured `tool_use` block.
Claude Code reads that as prose and ends the turn. `compat.py` parses complete
`<invoke>`/`<parameter>` blocks in streaming and non-streaming responses and
emits real `tool_use` blocks; ordinary text streams unchanged. This is the
root-cause adapter fix, not a stop-hook rule.

**The Write tool writes a truncated file.** This is the same model failure seen
from the other side: the backend can end a Write call in the middle of a JSON
object or Python body, and Claude Code saves whatever arrived. The tool is not
corrupted; the argument is. `~/.claude/hooks/pre_write_guard.py` rebuilds the
post-write file in memory and rejects it with exit 2 when `.json`, `.py` or
`.js` would be invalid, so the model gets a readable error and retries instead
of leaving a broken file on disk. It checks Edit and MultiEdit the same way,
including the exact replacement. Nothing is written when the hook blocks.

Check the validator without spending a model turn:

```powershell
python ~/.claude/hooks/pre_write_guard.py --selftest
```

Each block is logged to `~/.claude/hooks/write_guard.log`. `python doctor.py`
reports the write guard and the removed stop hooks; settings resets are
repaired by `settings-guard.py`.

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
