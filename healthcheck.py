#!/usr/bin/env python3
"""Configuration-driven server checks using only Python's standard library."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit

LEVEL = {"OK": 0, "WARN": 1, "CRITICAL": 2, "UNKNOWN": 3}


def number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label}: expected {low}..{high}")


def validate(config):
    if set(config) - {"timeout_seconds", "workers", "checks"}:
        raise ValueError("Unknown top-level setting")
    number(config.get("timeout_seconds", 3), 0.1, 30, "timeout_seconds")
    workers = config.get("workers", 4)
    number(workers, 1, 16, "workers")
    if not isinstance(workers, int):
        raise ValueError("workers must be an integer")
    checks = config.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
        raise ValueError("Provide 1..100 checks")
    fields = {
        "disk": {"path", "warn_percent", "critical_percent"},
        "backup": {"path", "warn_hours", "critical_hours"},
        "service": {"unit"},
        "tcp": {"host", "port"},
        "http": {"url", "expected_status"},
    }
    names = set()
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each check must be a table")
        kind, name = check.get("type"), check.get("name")
        if not isinstance(kind, str) or kind not in fields or not isinstance(name, str) or not name.strip():
            raise ValueError("Every check needs a supported type and a non-empty name")
        if len(name) > 100 or name in names or any(ord(c) < 32 for c in name):
            raise ValueError("Check names must be unique, short and single-line")
        names.add(name)
        if set(check) - fields[kind] - {"name", "type"}:
            raise ValueError(f"{name}: unknown setting")
        if kind in {"disk", "backup"}:
            path = check.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError(f"{name}: path must be absolute")
            warn_key, critical_key = (("warn_percent", "critical_percent") if kind == "disk"
                                      else ("warn_hours", "critical_hours"))
            limit = 100 if kind == "disk" else 87600
            number(check.get(warn_key), 0, limit, warn_key)
            number(check.get(critical_key), 0, limit, critical_key)
            if check[warn_key] >= check[critical_key]:
                raise ValueError(f"{name}: warning threshold must be below critical")
        elif kind == "service":
            if not isinstance(check.get("unit"), str) or not re.fullmatch(
                    r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service", check["unit"]):
                raise ValueError(f"{name}: specify one .service unit")
        elif kind == "tcp":
            if not isinstance(check.get("host"), str) or not check["host"].strip():
                raise ValueError(f"{name}: host is required")
            number(check.get("port"), 1, 65535, "port")
            if not isinstance(check["port"], int):
                raise ValueError("port must be an integer")
        elif kind == "http":
            if not isinstance(check.get("url"), str):
                raise ValueError(f"{name}: URL is required")
            url = urlsplit(check["url"])
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment
                    or any(ord(c) < 33 for c in check["url"])):
                raise ValueError(f"{name}: use an HTTP(S) URL without credentials, query or fragment")
            if url.port is not None:
                number(url.port, 1, 65535, "URL port")
            expected = check.get("expected_status", 200)
            number(expected, 100, 599, "expected_status")
            if not isinstance(expected, int):
                raise ValueError("expected_status must be an integer")
    return config


def threshold(value, warn, critical):
    return "CRITICAL" if value >= critical else "WARN" if value >= warn else "OK"


def probe(check, timeout):
    kind = check["type"]
    if kind == "disk":
        if not Path(check["path"]).exists():
            return "UNKNOWN", "Disk path does not exist"
        usage = shutil.disk_usage(check["path"])
        if usage.total <= 0:
            return "UNKNOWN", "Disk capacity is unavailable"
        used = 100 * usage.used / usage.total
        return (threshold(used, check["warn_percent"], check["critical_percent"]),
                f"{used:.1f}% used; {usage.free / 2**30:.2f} GiB free")
    if kind == "backup":
        path = Path(check["path"])
        if not path.is_file():
            return "CRITICAL", "Backup file is missing"
        stat = path.stat()
        if stat.st_size == 0:
            return "CRITICAL", "Backup file is empty"
        age = (time.time() - stat.st_mtime) / 3600
        if age < -1 / 60:
            return "UNKNOWN", "Backup timestamp is in the future; check the clock"
        age = max(0, age)
        return threshold(age, check["warn_hours"], check["critical_hours"]), f"Backup age: {age:.2f} h"
    if kind == "service":
        if not shutil.which("systemctl"):
            return "UNKNOWN", "systemctl is unavailable"
        proc = subprocess.run(["systemctl", "is-active", "--", check["unit"]],
                              capture_output=True, text=True, timeout=timeout, check=False)
        state = proc.stdout.strip()
        if proc.returncode == 0 and state == "active":
            return "OK", "Service is active"
        if state in {"inactive", "failed", "activating", "deactivating", "reloading"}:
            return "CRITICAL", f"Service state: {state}"
        return "UNKNOWN", "Unit or systemd state is unavailable"
    if kind == "tcp":
        with socket.create_connection((check["host"], check["port"]), timeout=timeout):
            return "OK", "TCP connection established"
    if kind == "http":
        url = urlsplit(check["url"])
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": timeout}
        if url.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        connection = cls(url.hostname, url.port, **kwargs)
        try:
            # GET supports health endpoints that do not implement HEAD. No body is downloaded.
            connection.request("GET", url.path or "/", headers={"User-Agent": "server-healthcheck/1.0"})
            response = connection.getresponse()
            expected = check.get("expected_status", 200)
            status = "OK" if response.status == expected else "CRITICAL"
            return status, f"HTTP {response.status}; expected {expected}"
        finally:
            connection.close()
    raise ValueError("Unsupported check type")


def run_check(check, timeout):
    start = time.monotonic()
    try:
        status, message = probe(check, timeout)
    except ssl.SSLCertVerificationError:
        status, message = "CRITICAL", "TLS certificate verification failed"
    except (socket.timeout, TimeoutError, subprocess.TimeoutExpired):
        status, message = "CRITICAL", "Check timed out"
    except (ConnectionError, socket.gaierror, http.client.HTTPException):
        status, message = "CRITICAL", "Network connection failed"
    except PermissionError:
        status, message = "UNKNOWN", "Permission denied"
    except OSError:
        status, message = ("CRITICAL", "Network connection failed") if check["type"] in {
            "tcp", "http"} else ("UNKNOWN", "Operating system check failed")
    return {"name": check["name"], "type": check["type"], "status": status,
            "message": message, "duration_ms": round((time.monotonic() - start) * 1000, 2)}


def collect(config):
    validate(config)
    with ThreadPoolExecutor(max_workers=config.get("workers", 4)) as pool:
        results = list(pool.map(lambda c: run_check(c, config.get("timeout_seconds", 3)), config["checks"]))
    overall = max((r["status"] for r in results), key=LEVEL.get)
    return {"schema_version": 1, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": overall, "checks": results}


def markdown(report):
    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")
    lines = ["# Server health report", "", f"Status: **{report['status']}**",
             f"UTC: {report['checked_at_utc']}", "", "| Check | Type | Status | Result | ms |",
             "|---|---|---|---|---:|"]
    for item in report["checks"]:
        lines.append("| " + " | ".join(cell(item[k]) for k in (
            "name", "type", "status", "message", "duration_ms")) + " |")
    return "\n".join(lines) + "\n"


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".healthcheck-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Write report.json and report.md here")
    args = parser.parse_args(argv)
    try:
        with args.config.open("rb") as handle:
            config = tomllib.load(handle)
        report = collect(config)
        if args.output:
            atomic_write(args.output / "report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            atomic_write(args.output / "report.md", markdown(report))
        print(markdown(report), end="")
        return LEVEL[report["status"]]
    except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"Configuration/report error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
