"""Disabled Stop/SubagentStop guard.

The original hook forced another model turn whenever a reply looked like a
plan, and it also ran after turns that were already complete. The model then
answered the hook's instruction instead of doing the work, which is the loop
the user saw as "Claude stops after the hook fires".

The registration is removed from settings.json by settings-guard.py. This file
stays as a no-op so a Claude Code process that was already running when the
setting changed cannot keep firing the old behaviour. The original script is
kept next to it as stop_guard.py.disabled if it is ever needed again.

Always exits 0 and prints nothing: a hook must never block a finished turn.
"""

import sys

if "--selftest" in sys.argv[1:]:
    print("stop guard disabled (no-op; registered hooks are removed)")
else:
    try:
        sys.stdin.read()
    except OSError:
        pass

raise SystemExit(0)
