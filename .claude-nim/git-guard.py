"""Keep GitHub authentication working from any shell.

Symptom: git inside Claude Desktop's Cowork reported "authentication (no
credentials)" and then "remote end hung up", and a push would stall until it
timed out. Cause, measured here:

  * Cowork's shell does not point HOME at C:/Users/ADMIN, so git never reads
    ~/.gitconfig and never sees the `gh` credential helper.
  * It then falls back to the *system* config, whose only helper is Git
    Credential Manager - a GUI helper. Without a desktop session to answer it,
    it blocks: `git ls-remote` sat there until `timeout` killed it (exit 124).

So the helper has to be reachable without HOME, and nothing may block waiting
for a human. Two settings do that:

  GIT_CONFIG_GLOBAL   user env var, points git at the real ~/.gitconfig no
                      matter what HOME is. Verified: with HOME pointed at a
                      nonexistent directory, `git push --dry-run` and
                      `git ls-remote` then both authenticate and finish.
  credential.interactive = never   makes GCM fail fast instead of hanging, so
                      the gh helper answers.

Run with no arguments to repair, `git-guard.py status` to report.

Exit codes: 0 = healthy, 1 = repaired something, 2 = cannot fix.
"""

import os
import subprocess
import sys

GLOBAL_CONFIG = r"C:\Users\ADMIN\.gitconfig"
ENV_NAME = "GIT_CONFIG_GLOBAL"


def user_env(name: str):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"[Environment]::GetEnvironmentVariable('{name}','User')"],
        capture_output=True, text=True, timeout=60,
    )
    return (out.stdout or "").strip()


def set_user_env(name: str, value: str) -> None:
    subprocess.run(["setx", name, value], capture_output=True, timeout=60)


def git_config(args: list[str]) -> tuple[int, str]:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=GLOBAL_CONFIG)
    out = subprocess.run(["git", "config", *args], capture_output=True,
                         text=True, timeout=60, env=env)
    return out.returncode, (out.stdout or "").strip() or (out.stderr or "").strip()


def repair() -> int:
    changes = []

    if not os.path.exists(GLOBAL_CONFIG):
        print("global git config missing at %s; cannot repair" % GLOBAL_CONFIG)
        return 2

    if user_env(ENV_NAME) != GLOBAL_CONFIG:
        set_user_env(ENV_NAME, GLOBAL_CONFIG)
        changes.append(f"{ENV_NAME}={GLOBAL_CONFIG}")

    code, value = git_config(["--global", "--get", "credential.interactive"])
    if code != 0 or value != "never":
        git_config(["--global", "credential.interactive", "never"])
        changes.append("credential.interactive=never")

    # A helper that answers for github.com without a GUI. Present already on a
    # healthy box; re-asserted because a HelperSelector change rewrites it.
    code, value = git_config(["--global", "--get", "credential.https://github.com.helper"])
    if code != 0 or "gh.exe" not in value:
        git_config(["--global", "credential.https://github.com.helper",
                    "!'C:\\Program Files\\GitHub CLI\\gh.exe' auth git-credential"])
        changes.append("github gh helper")

    for change in changes:
        print("repaired: " + change)
    return 1 if changes else 0


def status() -> int:
    print("GIT_CONFIG_GLOBAL user env : %s" % (user_env(ENV_NAME) or "<unset>"))
    print("global config present      : %s" % os.path.exists(GLOBAL_CONFIG))
    print("credential.interactive     : %s" % git_config(
        ["--global", "--get", "credential.interactive"])[1])
    print("github helper              : %s" % git_config(
        ["--global", "--get", "credential.https://github.com.helper"])[1])
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        sys.exit(status())
    sys.exit(repair())