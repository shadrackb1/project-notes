#!/usr/bin/env python3
"""Keep Claude Desktop pointed at the local Claude/NIM gateway.

Claude Desktop's Developer Mode stores third-party inference settings in a
local config library, not in claude_desktop_config.json - that file only
registers MCP servers. The library is read once, at launch, so an app upgrade,
a profile reset or a fresh install can drop the entry; the app then falls back
to the claude.ai sign-in screen, which looks like "the models disappeared"
rather than like a configuration problem.

This script makes that state re-appliable:

    python desktop-3p.py ensure    # idempotent; silent when already correct
    python desktop-3p.py status    # human-readable check, exit 1 when not applied
    python desktop-3p.py remove    # delete only the localhost gateway profile

The same pass also restores the two things a reset silently drops: the MCP
servers Desktop should share with Claude Code, and any top-level skill that
exists in ~/.agents/skills but is missing from ~/.claude/skills.

maintenance.ps1 runs `ensure` every ten minutes, so the profile comes back on
its own. Values come from the same place the rest of the stack uses:
LITELLM_MASTER_KEY (process env, then HKCU\\Environment) and CLAUDE_PROXY_PORT
(8791 by default). The key is never printed and never written anywhere except
the app's own profile file, which already holds it.

Overrides, useful for a packaged (MSIX) install or a different port:
    CLAUDE_DESKTOP_CONFIG_DIR   full path to the configLibrary directory
    CLAUDE_DESKTOP_GATEWAY      full gateway base URL
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

ENTRY_NAME = "Claude NIM gateway"

try:
    import winreg
except ImportError:  # portable / non-Windows use
    winreg = None


def library_dir() -> Path:
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("LOCALAPPDATA is not set; pass CLAUDE_DESKTOP_CONFIG_DIR")
    return Path(local) / "Claude-3p" / "configLibrary"


def gateway_url() -> str:
    override = os.environ.get("CLAUDE_DESKTOP_GATEWAY")
    if override:
        return override.rstrip("/")
    port = os.environ.get("CLAUDE_PROXY_PORT") or "8791"
    return f"http://127.0.0.1:{port}"


def master_key() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY")
    if key:
        return key.strip()
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            value, _ = winreg.QueryValueEx(k, "LITELLM_MASTER_KEY")
        return str(value).strip()
    except OSError:
        return ""


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Write through a temporary file so a crash cannot leave half a profile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def desired_settings(base_url: str, key: str) -> dict:
    return {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayApiKey": key,
        "inferenceGatewayAuthScheme": "x-api-key",
    }


def same_gateway(settings: dict, base_url: str) -> bool:
    if not isinstance(settings, dict):
        return False
    provider = settings.get("inferenceProvider")
    url = settings.get("inferenceGatewayBaseUrl")
    if provider != "gateway" or not isinstance(url, str):
        return False
    return url.rstrip("/").lower() == base_url.rstrip("/").lower()


def entry_path(lib: Path, entry_id: str) -> Path:
    return lib / f"{entry_id}.json"


def meta_entries(meta: dict) -> list:
    entries = meta.get("entries")
    if not isinstance(entries, list):
        entries = []
        meta["entries"] = entries
    # Drop anything that is not a usable {id, name} record so a corrupt meta
    # file cannot make the app lose every profile.
    entries[:] = [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]
    return entries


def desktop_config_path() -> Path:
    return Path.home() / "AppData" / "Local" / "Claude-3p" / "claude_desktop_config.json"


def ensure_mcp_servers() -> str:
    """Copy Claude Code's user-scope MCP servers into the Desktop profile.

    The Desktop app reads MCP servers from its own config, not from
    ~/.claude.json, so a reset or a fresh profile leaves it without the tools
    the CLI already has. Existing entries win: this only adds what is missing.
    """
    cfg_path = desktop_config_path()
    if not cfg_path.exists():
        return ""
    cfg = read_json(cfg_path, None)
    if not isinstance(cfg, dict):
        return ""

    source = read_json(Path.home() / ".claude.json", {})
    global_mcp = source.get("mcpServers") if isinstance(source, dict) else None
    if not isinstance(global_mcp, dict) or not global_mcp:
        return ""

    current = cfg.get("mcpServers")
    merged = dict(current) if isinstance(current, dict) else {}
    added = [name for name in global_mcp if name not in merged]
    if not added:
        return ""

    for name in added:
        merged[name] = global_mcp[name]
    cfg["mcpServers"] = merged

    backup = cfg_path.with_name(cfg_path.name + ".bak")
    try:
        shutil.copy2(cfg_path, backup)
    except OSError:
        pass
    write_json(cfg_path, cfg)
    return f"added MCP servers: {', '.join(sorted(added))}"


def ensure_skills() -> str:
    """Import top-level skills that exist for Codex but not for Claude."""
    source = Path.home() / ".agents" / "skills"
    target = Path.home() / ".claude" / "skills"
    if not source.is_dir() or not target.is_dir():
        return ""

    copied = []
    for child in source.iterdir():
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        destination = target / child.name
        if destination.exists():
            continue
        try:
            shutil.copytree(child, destination)
        except OSError:
            continue
        copied.append(child.name)

    if not copied:
        return ""
    return f"imported {len(copied)} skill(s): {', '.join(sorted(copied))}"


def ensure(quiet: bool = True) -> int:
    base_url = gateway_url()
    key = master_key()
    if not key:
        print("desktop-3p: LITELLM_MASTER_KEY is not set; cannot configure the gateway", file=sys.stderr)
        return 2

    notes = []
    lib = library_dir()
    lib.mkdir(parents=True, exist_ok=True)
    meta_file = lib / "_meta.json"
    meta = read_json(meta_file, {})
    entries = meta_entries(meta)
    applied = meta.get("appliedId")
    wanted = desired_settings(base_url, key)

    target = None
    if isinstance(applied, str):
        target = next((e for e in entries if e["id"] == applied), None)
    if target is None:
        for entry in entries:
            if same_gateway(read_json(entry_path(lib, entry["id"]), {}), base_url):
                target = entry
                break

    changed = False
    if target is not None:
        settings = read_json(entry_path(lib, target["id"]), {})
        if not isinstance(settings, dict):
            settings = {}
        if not all(settings.get(k) == v for k, v in wanted.items()):
            settings.update(wanted)
            write_json(entry_path(lib, target["id"]), settings)
            changed = True
            note = f"updated profile {target['id']}"
        else:
            note = ""
    else:
        entry_id = str(uuid.uuid4())
        write_json(entry_path(lib, entry_id), wanted)
        entry = {"id": entry_id, "name": ENTRY_NAME}
        entries.append(entry)
        target = entry
        changed = True
        note = f"added profile {entry_id}"
        if not isinstance(applied, str) or not applied:
            # No profile was selected yet, so this one becomes the default.
            meta["appliedId"] = entry_id
        else:
            # Another profile is deliberately selected; add ours but do not
            # hijack the user's choice. `status` reports the mismatch.
            note += " (another profile is applied; select it in Developer settings)"

    if not isinstance(meta.get("appliedId"), str) or not meta.get("appliedId"):
        meta["appliedId"] = target["id"]
        changed = True

    if changed:
        write_json(meta_file, meta)
        if note:
            notes.append(note)

    for extra in (ensure_mcp_servers(), ensure_skills()):
        if extra:
            notes.append(extra)

    if notes and not quiet:
        print("desktop-3p: " + "; ".join(notes) + "; restart Claude Desktop to apply")
    elif not quiet:
        print("desktop-3p: already correct")
    return 0


def status() -> int:
    base_url = gateway_url()
    lib = library_dir()
    meta = read_json(lib / "_meta.json", {})
    entries = meta_entries(meta) if isinstance(meta, dict) else []
    applied = meta.get("appliedId") if isinstance(meta, dict) else None

    print(f"library : {lib}")
    print(f"gateway : {base_url}")
    print(f"applied : {applied or '(none)'}")

    applied_entry = next((e for e in entries if e["id"] == applied), None)
    if applied_entry is None:
        print("state   : no applied profile; Claude Desktop will show the sign-in screen")
        return 1

    settings = read_json(entry_path(lib, applied), {})
    if not isinstance(settings, dict):
        settings = {}
    key = settings.get("inferenceGatewayApiKey")
    provider = settings.get("inferenceProvider")
    url = settings.get("inferenceGatewayBaseUrl")
    print(f"provider: {provider or '(unset)'}")
    print(f"url     : {url or '(unset)'}")
    print(f"auth    : {settings.get('inferenceGatewayAuthScheme') or '(unset)'}")
    print(f"key     : {'set (' + str(len(key)) + ' chars)' if isinstance(key, str) and key else '(missing)'}")

    desktop_cfg = read_json(desktop_config_path(), {})
    servers = desktop_cfg.get("mcpServers") if isinstance(desktop_cfg, dict) else {}
    print(f"mcp     : {len(servers) if isinstance(servers, dict) else 0} servers")
    print(f"skills  : {sum(1 for _ in (Path.home() / '.claude' / 'skills').glob('*/SKILL.md'))} in ~/.claude/skills")

    ok = same_gateway(settings, base_url) and isinstance(key, str) and bool(key)
    print("state   : " + ("configured" if ok else "not pointing at this gateway"))
    return 0 if ok else 1


def remove() -> int:
    base_url = gateway_url()
    lib = library_dir()
    meta_file = lib / "_meta.json"
    meta = read_json(meta_file, {})
    if not isinstance(meta, dict):
        meta = {}
    entries = meta_entries(meta)

    removed = []
    keep = []
    for entry in entries:
        settings = read_json(entry_path(lib, entry["id"]), {})
        if same_gateway(settings, base_url):
            try:
                entry_path(lib, entry["id"]).unlink()
            except FileNotFoundError:
                pass
            removed.append(entry["id"])
        else:
            keep.append(entry)

    if not removed:
        print("desktop-3p: no localhost gateway profile found; nothing removed")
        return 0

    meta["entries"] = keep
    if meta.get("appliedId") in removed:
        meta.pop("appliedId", None)
    write_json(meta_file, meta)
    print(f"desktop-3p: removed {len(removed)} profile(s); restart Claude Desktop to apply")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in argv[1:]
    command = (args[0] if args else "ensure").lower()
    if command in {"ensure", "apply"}:
        return ensure(quiet=quiet)
    if command == "status":
        return status()
    if command == "remove":
        return remove()
    print("usage: desktop-3p.py [ensure|status|remove]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
