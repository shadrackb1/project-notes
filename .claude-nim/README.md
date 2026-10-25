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
The shim caps repeated streaming chunks (`REPEATED_STREAMING_CHUNK_LIMIT`) and
the wrapper caps output tokens (`CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000`), so a
looping answer ends instead of hanging. If it still happens, check the newest
proxy log for `Timeout` or `RateLimitError`. A restart is safe; turns already
running will fail, so do it between requests.

**The model describes a plan and stops.** Claude Code treats a text-only reply
as the end of the turn. `~/.claude/hooks/stop_guard.py` catches the
plan-without-action shape and returns exit 2, which continues the turn with an
instruction to act. If it seems inactive, `python doctor.py` reports whether
the hook is registered; settings resets are repaired by `settings-guard.py`.

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
