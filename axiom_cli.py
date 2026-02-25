#!/usr/bin/env python3
"""CLI для управления мониторами и нотификаторами Axiom."""

import json
import sys
import urllib.request
import urllib.error
from typing import Any

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE = "https://api.axiom.co"
DATASET = "prod-docker-logs"


def get_token() -> str:
    import os
    from pathlib import Path

    token = os.environ.get("AXIOM_MGMT_TOKEN")
    if token:
        return token
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("AXIOM_MGMT_TOKEN="):
                return line.split("=", 1)[1].strip()
    print("Error: set AXIOM_MGMT_TOKEN in .env or environment", file=sys.stderr)
    sys.exit(1)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def api(method: str, path: str, payload: Any = None) -> Any:
    token = get_token()
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            err = json.loads(body)
        except Exception:
            err = {"raw": body.decode()}
        print(f"API error {e.code}: {err}", file=sys.stderr)
        sys.exit(1)


# ── Notifiers ─────────────────────────────────────────────────────────────────

def list_notifiers():
    items = api("GET", "/v2/notifiers") or []
    for n in items:
        url = n.get("properties", {}).get("webhook", {}).get("url", "—")
        print(f"  {n['id']}  {n['name']}  →  {url}")


def create_notifier(name: str, webhook_url: str) -> str:
    result = api("POST", "/v2/notifiers", {
        "name": name,
        "properties": {"webhook": {"url": webhook_url}},
    })
    print(f"Created notifier: {result['id']}  {result['name']}")
    return result["id"]


def delete_notifier(notifier_id: str):
    api("DELETE", f"/v2/notifiers/{notifier_id}")
    print(f"Deleted notifier: {notifier_id}")


# ── Monitors ──────────────────────────────────────────────────────────────────

def list_monitors():
    items = api("GET", "/v2/monitors") or []
    for m in items:
        status = "disabled" if m.get("disabled") else "active"
        print(f"  {m['id']}  [{status}]  {m['name']}")
        print(f"           every {m.get('intervalMinutes')}m, "
              f"threshold {m.get('comparison')} {m.get('threshold')}")


def create_monitor(
    name: str,
    description: str,
    service: str,
    notifier_id: str,
    interval_minutes: int = 5,
    threshold: int = 1,
):
    """Создать Threshold-монитор для Docker-сервиса по ошибкам."""
    apl = (
        f"['{DATASET}']"
        f" | where service == \"{service}\""
        f" | where message contains \"ERROR\""
        f"   or message contains \"error\""
        f"   or message contains \"Traceback\""
        f"   or message contains \"Exception\""
        f" | count"
    )
    result = api("POST", "/v2/monitors", {
        "name": name,
        "description": description,
        "type": "Threshold",
        "aplQuery": apl,
        "intervalMinutes": interval_minutes,
        "rangeMinutes": interval_minutes,
        "threshold": threshold,
        "operator": "AboveOrEqual",
        "alertOnNoData": False,
        "notifiers": [notifier_id],
        "notifyByGroup": False,
        "disabledUntil": "0001-01-01T00:00:00Z",
    })
    print(f"Created monitor: {result['id']}  {result['name']}")
    return result["id"]


def create_health_watcher_monitor(notifier_id: str, interval_minutes: int = 5) -> str:
    """Создать монитор для health-watcher: алерт если любой контейнер нездоров."""
    apl = f"['{DATASET}'] | where service == \"axiom-health-watcher\" | count"
    result = api("POST", "/v2/monitors", {
        "name": "health-watcher — unhealthy containers",
        "description": (
            "Алерт если какой-либо Docker-контейнер остаётся unhealthy "
            f"дольше UNHEALTHY_ALERT_DELAY_SECONDS секунд. "
            f"Окно проверки {interval_minutes} мин."
        ),
        "type": "Threshold",
        "aplQuery": apl,
        "intervalMinutes": interval_minutes,
        "rangeMinutes": interval_minutes,
        "threshold": 1,
        "operator": "AboveOrEqual",
        "alertOnNoData": False,
        "notifiers": [notifier_id],
        "notifyByGroup": False,
        "disabledUntil": "0001-01-01T00:00:00Z",
    })
    print(f"Created monitor: {result['id']}  {result['name']}")
    return result["id"]


def delete_monitor(monitor_id: str):
    api("DELETE", f"/v2/monitors/{monitor_id}")
    print(f"Deleted monitor: {monitor_id}")


# ── CLI ───────────────────────────────────────────────────────────────────────

USAGE = """
Usage:
  axiom_cli.py notifiers list
  axiom_cli.py notifiers create <name> <webhook-url>
  axiom_cli.py notifiers delete <id>

  axiom_cli.py monitors list
  axiom_cli.py monitors create <service> [--interval N] [--threshold N]
  axiom_cli.py monitors create-health-watcher [--interval N]
  axiom_cli.py monitors delete <id>

Examples:
  axiom_cli.py monitors create my-service
  axiom_cli.py monitors create my-api --interval 10 --threshold 1
  axiom_cli.py monitors create-health-watcher
  axiom_cli.py monitors list
"""


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(USAGE)
        sys.exit(0)

    entity, cmd, *rest = args

    if entity == "notifiers":
        if cmd == "list":
            list_notifiers()
        elif cmd == "create" and len(rest) == 2:
            create_notifier(rest[0], rest[1])
        elif cmd == "delete" and len(rest) == 1:
            delete_notifier(rest[0])
        else:
            print(USAGE)

    elif entity == "monitors":
        if cmd == "list":
            list_monitors()

        elif cmd == "create" and rest:
            service = rest[0]
            interval = 5
            threshold = 1
            i = 1
            while i < len(rest):
                if rest[i] == "--interval" and i + 1 < len(rest):
                    interval = int(rest[i + 1]); i += 2
                elif rest[i] == "--threshold" and i + 1 < len(rest):
                    threshold = int(rest[i + 1]); i += 2
                else:
                    i += 1

            # Find notifier automatically (first one)
            notifiers = api("GET", "/v2/notifiers") or []
            if not notifiers:
                print("No notifiers found. Create one first:", file=sys.stderr)
                print("  axiom_cli.py notifiers create alertbot-telegram <url>", file=sys.stderr)
                sys.exit(1)
            notifier_id = notifiers[0]["id"]
            notifier_name = notifiers[0]["name"]
            print(f"Using notifier: {notifier_id}  {notifier_name}")

            create_monitor(
                name=f"{service} — ошибки",
                description=(
                    f"Алерт если {service} залогировал ошибку. "
                    f"Окно {interval} мин, дедупликация окном."
                ),
                service=service,
                notifier_id=notifier_id,
                interval_minutes=interval,
                threshold=threshold,
            )

        elif cmd == "create-health-watcher":
            interval = 5
            i = 0
            while i < len(rest):
                if rest[i] == "--interval" and i + 1 < len(rest):
                    interval = int(rest[i + 1]); i += 2
                else:
                    i += 1
            notifiers = api("GET", "/v2/notifiers") or []
            if not notifiers:
                print("No notifiers found. Create one first:", file=sys.stderr)
                print("  axiom_cli.py notifiers create alertbot-telegram <url>", file=sys.stderr)
                sys.exit(1)
            notifier_id = notifiers[0]["id"]
            print(f"Using notifier: {notifier_id}  {notifiers[0]['name']}")
            create_health_watcher_monitor(notifier_id, interval)

        elif cmd == "delete" and rest:
            delete_monitor(rest[0])

        else:
            print(USAGE)

    else:
        print(USAGE)


if __name__ == "__main__":
    main()
