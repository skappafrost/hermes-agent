#!/usr/bin/env python3
"""
weekly_system_maintenance.py
Read-only system health scanner for the default Hermes profile.
Run weekly to detect drift, not to fix anything.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "C:/Users/Ha Trung/AppData/Local/hermes"))
PROFILE = "main"  # default / root profile


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def run(cmd, **kwargs):
    """Run a shell command and return (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, shell=True, **kwargs)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def check_hermes_cli():
    rc, out, err = run("hermes --version")
    return {
        "name": "hermes_cli",
        "status": "ok" if rc == 0 else "fail",
        "rc": rc,
        "version": out if out else err,
    }


def check_profiles():
    rc, out, err = run("hermes profile list --json 2>/dev/null || hermes profile list")
    return {
        "name": "hermes_profiles",
        "status": "ok" if rc == 0 else "warn",
        "rc": rc,
        "output": (out or err)[:500],
    }


def check_vault():
    vault_path = Path("C:/Users/Ha Trung/Hermes Memory")
    try:
        note_count = len(list(vault_path.rglob("*.md"))) if vault_path.exists() else 0
        return {
            "name": "vault",
            "status": "ok" if vault_path.exists() else "warn",
            "path": str(vault_path),
            "exists": vault_path.exists(),
            "note_count": note_count,
        }
    except Exception as e:
        return {"name": "vault", "status": "warn", "error": str(e)}


def check_memory():
    return check_local_http_service("memory_recall", "http://localhost:3111/")


def check_web_search():
    # Ping the configured local search/firecrawl gateway
    return check_local_http_service("web_search", "http://localhost:13002/")


def check_file_io():
    test_path = HERMES_HOME / "tmp_maintenance_test.txt"
    try:
        test_path.write_text("probe", encoding="utf-8")
        content = test_path.read_text(encoding="utf-8")
        test_path.unlink(missing_ok=True)
        return {
            "name": "file_io",
            "status": "ok" if content == "probe" else "warn",
            "rc": 0,
            "write_read": content == "probe",
        }
    except Exception as e:
        return {"name": "file_io", "status": "fail", "rc": -1, "error": str(e)}


def check_local_http_service(name, url, timeout=5):
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            return {
                "name": name,
                "status": "ok",
                "url": url,
                "http_code": resp.status,
                "latency_ms": None,  # optional, kept simple
            }
    except URLError as e:
        return {"name": name, "status": "warn", "url": url, "error": str(e.reason if hasattr(e, 'reason') else e)}
    except Exception as e:
        return {"name": name, "status": "warn", "url": url, "error": str(e)}


def check_disk_space():
    try:
        import shutil
        total, used, free = shutil.disk_usage("C:/")
        pct_used = round(used / total * 100, 1)
        return {
            "name": "disk_space",
            "status": "ok" if pct_used < 90 else "warn",
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "free_gb": round(free / 1e9, 1),
            "pct_used": pct_used,
        }
    except Exception as e:
        return {"name": "disk_space", "status": "warn", "error": str(e)}


def check_cron_list():
    rc, out, err = run("hermes cron list --json 2>/dev/null || hermes cron list")
    return {
        "name": "cron_list",
        "status": "ok" if rc == 0 else "warn",
        "rc": rc,
        "output": (out or err)[:800],
    }


def check_git_repos():
    """Quickly check a few known repos for dirty state or pending commits."""
    repos = [
        Path("C:/Users/Ha Trung/AppData/Local/hermes/hermes-agent"),
    ]
    results = []
    for repo in repos:
        if not (repo / ".git").exists():
            continue
        rc, out, err = run("git status --short --branch && git log --oneline -1", cwd=str(repo))
        results.append({
            "repo": str(repo),
            "status": "ok" if rc == 0 else "warn",
            "dirty": bool(out.strip()) and not out.strip().startswith("##"),
            "last_commit": out.splitlines()[-1] if out else err,
        })
    return {"name": "git_repos", "status": "ok", "repos": results}


def main():
    report = {
        "scan_time": now_iso(),
        "profile": PROFILE,
        "hermes_home": str(HERMES_HOME),
        "checks": [],
    }

    report["checks"].append(check_hermes_cli())
    report["checks"].append(check_profiles())
    report["checks"].append(check_vault())
    report["checks"].append(check_memory())
    report["checks"].append(check_web_search())
    report["checks"].append(check_file_io())
    report["checks"].append(check_cron_list())
    report["checks"].append(check_disk_space())
    report["checks"].append(check_git_repos())

    # Local services (best effort, no fail if down)
    services = [
        ("firecrawl_api", "http://localhost:13002/"),
        ("vilao_monitor", "http://localhost:8620/"),
        ("hermes_dashboard", "http://localhost:9119/"),
        ("hermes_relay", "http://localhost:8767/"),
    ]
    for name, url in services:
        report["checks"].append(check_local_http_service(name, url))

    # Determine overall status
    statuses = [c["status"] for c in report["checks"]]
    if "fail" in statuses:
        report["overall"] = "fail"
    elif "warn" in statuses:
        report["overall"] = "warn"
    else:
        report["overall"] = "ok"

    # Write report to file (read-only side effect: output artifact)
    out_path = HERMES_HOME / "cron" / "output" / "weekly-system-review" / f"maintenance_scan_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Print summary
    print(f"# Weekly System Maintenance Scan")
    print(f"**Profile:** {PROFILE}")
    print(f"**Scan time:** {report['scan_time']}")
    print(f"**Overall:** {report['overall'].upper()}")
    print(f"**Report file:** {out_path}")
    print()
    print("| Check | Status | Detail |")
    print("|-------|--------|--------|")
    for c in report["checks"]:
        detail = ""
        if "error" in c:
            detail = c["error"]
        elif "http_code" in c:
            detail = f"HTTP {c['http_code']}"
        elif "pct_used" in c:
            detail = f"{c['free_gb']}GB free ({c['pct_used']}% used)"
        elif "output" in c:
            detail = c["output"].replace("\n", " ")[:80]
        elif "version" in c:
            detail = c["version"]
        elif "write_read" in c:
            detail = "write/read ok" if c["write_read"] else "mismatch"
        elif "repos" in c:
            detail = f"{len(c['repos'])} repos checked"
        elif "note_count" in c:
            detail = f"{c['note_count']} notes @ {Path(c['path']).name}"
        else:
            detail = ""
        print(f"| {c['name']} | {c['status']} | {detail} |")

    # Exit non-zero on fail to make cron mark it
    if report["overall"] == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
