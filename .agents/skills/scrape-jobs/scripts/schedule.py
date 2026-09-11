"""Manage only this user's job-scraper LaunchAgent; no root or wake alarms."""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from storage import atomic_file

LABEL = "com.matthew.findingwork.scrape-jobs"


def calendar_time(config: dict) -> tuple[int, int]:
    value = config["daily_scraper"].get("schedule", "07:00")
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("daily_scraper.schedule must be local HH:MM")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def definition(workspace: Path, script: Path, config: dict) -> dict:
    hour, minute = calendar_time(config)
    root = workspace / "job-search-daily"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", "-B", str(script), "run", "--scheduled"],
        "WorkingDirectory": str(workspace),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Background",
        "ThrottleInterval": 60, "Umask": 0o077,
        "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(root / "launchd.log"),
        "StandardErrorPath": str(root / "launchd-error.log"),
    }


def _launchctl(*args: str, check: bool = True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=10)
    if check and result.returncode:
        raise ValueError("launchctl " + args[0] + ": " + (result.stderr.strip() or result.stdout.strip()))
    return result


def inspect(workspace: Path, script: Path, config: dict) -> dict:
    if sys.platform != "darwin":
        return {"supported": False, "loaded": False, "note": "automatic scheduling requires macOS; manual commands remain available"}
    path = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    if path.is_symlink():
        raise ValueError("LaunchAgent is a symlink; refusing to follow it")
    actual = plistlib.loads(path.read_bytes()) if path.exists() else None
    result = _launchctl("print", f"gui/{os.getuid()}/{LABEL}", check=False)
    if result.returncode and not re.search(r"could not find service|bad request", result.stderr, re.I):
        raise ValueError("unable to inspect LaunchAgent: " + result.stderr.strip())
    loaded = result.returncode == 0
    state = re.search(r"^\s*state = (.+)$", result.stdout, re.M)
    exit_code = re.search(r"^\s*last exit code = (-?\d+)$", result.stdout, re.M)
    trigger = definition(workspace, script, config)["StartCalendarInterval"]
    trigger_matches = all(re.search(r'"' + key + r'"\s*=>\s*' + str(value) + r'\b', result.stdout) for key, value in trigger.items())
    return {"supported": True, "installed": actual is not None, "loaded": loaded,
            "definition_matches": actual == definition(workspace, script, config),
            "loaded_trigger_matches": bool(loaded and trigger_matches), "path": str(path),
            "state": state.group(1).strip() if state else "not loaded",
            "last_exit_code": int(exit_code.group(1)) if exit_code else None,
            "schedule": config["daily_scraper"].get("schedule", "07:00"), "timezone": "Mac local"}


def manage(action: str, workspace: Path, script: Path, config: dict) -> dict:
    current = inspect(workspace, script, config)
    if action == "status":
        return current
    if not current["supported"]:
        raise ValueError("LaunchAgent installation/removal requires macOS")
    path = Path(current["path"])
    if path.exists():
        actual = plistlib.loads(path.read_bytes())
        if actual.get("Label") != LABEL or actual.get("WorkingDirectory") != str(workspace):
            raise ValueError("existing LaunchAgent is not owned by this workspace; refusing to replace it")
    if current["loaded"] and current["state"] != "not running":
        raise ValueError("scraper is running; wait for completion before changing its schedule")
    domain = f"gui/{os.getuid()}"
    if current["loaded"]:
        _launchctl("bootout", domain + "/" + LABEL)
    if action == "remove":
        if path.exists():
            path.unlink()
        return {"status": "removed", "path": str(path), "daily_history_preserved": True}
    if action != "install":
        raise ValueError("unknown schedule action")
    path.parent.mkdir(parents=True, exist_ok=True)
    (workspace / "job-search-daily").mkdir(mode=0o700, exist_ok=True)
    with atomic_file(path) as handle:
        handle.write(plistlib.dumps(definition(workspace, script, config), sort_keys=False))
    _launchctl("enable", domain + "/" + LABEL)
    _launchctl("bootstrap", domain, str(path))
    result = inspect(workspace, script, config)
    if not result["loaded"] or not result["loaded_trigger_matches"]:
        raise ValueError("LaunchAgent did not register its configured calendar trigger")
    return result
