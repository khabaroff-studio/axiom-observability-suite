"""alertbot — Axiom webhook → Telegram notifier with topic routing.

Accepts alerts from two sources:
- POST /webhook/axiom — Axiom Monitor webhooks (external)
- POST /alert/local  — local alerts from health-watcher (internal, no auth)

Routing rules are defined in routes.yml. Without it, falls back to
TELEGRAM_CHAT_ID / TELEGRAM_TOPIC_ID from environment.
"""

import asyncio
import html
import json
import re
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from routes_validation import validate_routes_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("alertbot")


# ── Config ────────────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_chat_id: str = ""
    telegram_topic_id: str = ""
    webhook_secret: str = ""  # optional, matched against X-Webhook-Secret header
    axiom_mgmt_token: str = ""
    axiom_api_base: str = "https://api.axiom.co"
    axiom_attach_interval_seconds: int = 300
    alertbot_include_resolved: bool = False
    axiom_dataset: str = ""
    axiom_query_base: str = "https://cloud.axiom.co"

    model_config = {"env_file": ".env"}


settings = Settings()
TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
AXIOM_API_BASE = settings.axiom_api_base.rstrip("/")
AXIOM_QUERY_BASE = settings.axiom_query_base.rstrip("/")


# ── Axiom notifier sync ───────────────────────────────────────────────────────


def _axiom_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.axiom_mgmt_token}",
        "Content-Type": "application/json",
    }


async def _attach_notifiers_once() -> int:
    if not settings.axiom_mgmt_token:
        return 0

    headers = _axiom_headers()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        notifiers_resp = await client.get(
            f"{AXIOM_API_BASE}/v2/notifiers", headers=headers, timeout=10
        )
        notifiers_resp.raise_for_status()
        notifiers = notifiers_resp.json() or []
        if not notifiers:
            logger.warning("Axiom auto-attach: no notifiers found")
            return 0

        notifier_id = notifiers[0]["id"]
        monitors_resp = await client.get(
            f"{AXIOM_API_BASE}/v2/monitors", headers=headers, timeout=10
        )
        monitors_resp.raise_for_status()
        monitors = monitors_resp.json() or []
        if not monitors:
            return 0

        updated = 0
        for monitor in monitors:
            if monitor.get("notifierIds"):
                continue
            monitor_id = monitor["id"]
            detail_resp = await client.get(
                f"{AXIOM_API_BASE}/v2/monitors/{monitor_id}",
                headers=headers,
                timeout=10,
            )
            detail_resp.raise_for_status()
            payload = detail_resp.json() or {}
            payload.pop("id", None)
            payload.pop("createdAt", None)
            payload["notifierIds"] = [notifier_id]
            update_resp = await client.put(
                f"{AXIOM_API_BASE}/v2/monitors/{monitor_id}",
                headers=headers,
                json=payload,
                timeout=10,
            )
            update_resp.raise_for_status()
            updated += 1

        return updated


async def _auto_attach_notifiers_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            updated = await _attach_notifiers_once()
            if updated:
                logger.info("Axiom auto-attach: updated %s monitor(s)", updated)
        except httpx.HTTPError as exc:
            logger.error("Axiom auto-attach failed: %s", exc)
        except Exception as exc:
            logger.error("Axiom auto-attach unexpected error: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.axiom_attach_interval_seconds
            )
        except asyncio.TimeoutError:
            continue


# ── Routes ────────────────────────────────────────────────────────────────────


def _load_routes() -> dict:
    """Load routing config from routes.yml. Returns {} if file missing."""
    routes_file = Path(__file__).parent / "routes.yml"
    if not routes_file.exists():
        return {}
    with open(routes_file) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("routes.yml must be a mapping at top level")
    schema_path = Path(__file__).parent / "routes.schema.json"
    validate_routes_config(data, schema_path)
    return data


_routes = _load_routes()


def _config_section(name: str, default: Any) -> Any:
    if not _routes:
        return default
    value = _routes.get(name)
    return default if value is None else value


def _config_defaults() -> dict[str, Any]:
    defaults = _config_section("defaults", {})
    return defaults if isinstance(defaults, dict) else {}


def _config_tags() -> dict[str, str]:
    tags = _config_section("tags", {})
    if not isinstance(tags, dict):
        tags = {}
    return {
        "user_impact": str(tags.get("user_impact", "#user-impact")),
        "service_errors": str(tags.get("service_errors", "#service-errors")),
    }


def _config_drop_rules() -> list[dict]:
    drop_rules = _config_section("drop", [])
    return drop_rules if isinstance(drop_rules, list) else []


def _config_profiles() -> dict[str, Any]:
    profiles = _config_section("profiles", {})
    return profiles if isinstance(profiles, dict) else {}


def _config_services() -> dict[str, Any]:
    services = _config_section("services", {})
    return services if isinstance(services, dict) else {}


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return fallback


def _coerce_int(value: Any, fallback: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return fallback
    return fallback


def _get_service_profiles(services: set[str]) -> list[str]:
    services_cfg = _config_services()
    profile_names: list[str] = []
    for service in sorted(services):
        cfg = services_cfg.get(service, {}) if isinstance(services_cfg, dict) else {}
        if not isinstance(cfg, dict):
            continue
        profiles = cfg.get("profiles", [])
        if isinstance(profiles, list):
            profile_names.extend([str(p) for p in profiles if p])

    seen: set[str] = set()
    unique: list[str] = []
    for name in profile_names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _match_rule(rule: dict, context: dict[str, Any]) -> bool:
    match = rule.get("match", rule)
    if not isinstance(match, dict):
        return False
    field = match.get("field")
    op = match.get("op")
    expected = match.get("value")
    if not field or not op:
        return False

    actual_values = _to_list(context.get(str(field)))
    if not actual_values:
        return False

    expected_values = _to_list(expected)
    if not expected_values and op not in {"eq"}:
        return False

    if op == "contains":
        needle = expected_values[0] if expected_values else ""
        return any(needle in actual for actual in actual_values)
    if op == "contains_any":
        return any(
            needle in actual for actual in actual_values for needle in expected_values
        )
    if op == "regex":
        pattern = expected_values[0] if expected_values else ""
        return any(re.search(pattern, actual) for actual in actual_values)
    if op == "in":
        return any(
            str(actual) == str(expected)
            for actual in actual_values
            for expected in expected_values
        )
    if op == "prefix_in":
        return any(
            actual.startswith(prefix)
            for actual in actual_values
            for prefix in expected_values
        )
    if op == "eq":
        needle = expected_values[0] if expected_values else ""
        return any(actual == needle for actual in actual_values)
    return False


def _should_drop(context: dict[str, Any]) -> bool:
    for rule in _config_drop_rules():
        if isinstance(rule, dict) and _match_rule(rule, context):
            return True
    return False


def _is_p1(profile_names: list[str], context: dict[str, Any]) -> bool:
    profiles_cfg = _config_profiles()
    for profile_name in profile_names:
        profile = profiles_cfg.get(profile_name, {})
        if not isinstance(profile, dict):
            continue
        p1_rules = profile.get("p1", [])
        if isinstance(p1_rules, list) and any(
            isinstance(rule, dict) and _match_rule(rule, context) for rule in p1_rules
        ):
            return True
    return False


def _resolve_runbook(services: set[str], profile_names: list[str]) -> list[str]:
    services_cfg = _config_services()
    if isinstance(services_cfg, dict):
        for service in sorted(services):
            cfg = services_cfg.get(service, {})
            if isinstance(cfg, dict) and isinstance(cfg.get("runbook"), list):
                return [str(line) for line in cfg.get("runbook", [])]

    profiles_cfg = _config_profiles()
    for profile_name in profile_names:
        profile = profiles_cfg.get(profile_name, {})
        if isinstance(profile, dict) and isinstance(profile.get("runbook"), list):
            return [str(line) for line in profile.get("runbook", [])]

    defaults = _config_defaults()
    runbook = defaults.get("runbook", []) if isinstance(defaults, dict) else []
    return [str(line) for line in runbook] if isinstance(runbook, list) else []


def _truncate(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _render_runbook(
    steps: list[str], host: str, service: str, monitor: str
) -> list[str]:
    host_value = host or "нужный хост"
    service_value = service or "нужный сервис"
    monitor_value = monitor or "нужный монитор"
    rendered: list[str] = []
    for step in steps:
        try:
            rendered.append(
                step.format(
                    host=host_value,
                    service=service_value,
                    monitor=monitor_value,
                )
            )
        except KeyError:
            rendered.append(step)
    return rendered


def _most_common(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def _sample_messages(messages: list[str], sample_count: int) -> list[str]:
    seen: set[str] = set()
    samples: list[str] = []
    for message in messages:
        if message in seen:
            continue
        seen.add(message)
        samples.append(message)
        if len(samples) >= sample_count:
            break
    return samples


def _extract_match_fields(
    matches: list[dict],
) -> tuple[set[str], set[str], list[str], list[str], list[str], list[str]]:
    servers: set[str] = set()
    services: set[str] = set()
    messages: list[str] = []
    statuses: list[str] = []
    user_agents: list[str] = []
    paths: list[str] = []

    for match in matches:
        data = match.get("data", match)
        if not isinstance(data, dict):
            continue
        if host := data.get("host"):
            servers.add(str(host))
        if service := data.get("service"):
            services.add(str(service))

        for key in ("message", "msg", "log", "_raw"):
            value = data.get(key)
            if value:
                messages.append(str(value))
                break

        for key in ("status", "status_code", "code"):
            value = data.get(key)
            if value is not None:
                statuses.append(str(value))
                break

        for key in ("user_agent", "userAgent", "ua"):
            value = data.get(key)
            if value:
                user_agents.append(str(value))
                break

        for key in ("path", "url", "request_path", "requestPath"):
            value = data.get(key)
            if value:
                paths.append(str(value))
                break

    return servers, services, messages, statuses, user_agents, paths


def _format_host_service(servers: set[str], services: set[str]) -> tuple[str, str, str]:
    host = sorted(servers)[0] if servers else ""
    service = sorted(services)[0] if services else ""
    if host and service:
        display = f"{host}:{service}"
    elif service:
        display = service
    elif host:
        display = host
    else:
        return "", "", ""

    if len(servers) > 1 or len(services) > 1:
        display += " (multiple)"
    return host, service, display


_REDACT_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+"), r"\1<redacted>"),
]
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _redact(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _sanitize_line(text: str, limit: int = 200) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    return html.escape(_truncate(_redact(cleaned), limit), quote=False)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace(" UTC", "+00:00")
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_time_range(ts_start: str, ts_end: str) -> tuple[str, str]:
    start_dt = _parse_time(ts_start)
    end_dt = _parse_time(ts_end)
    if start_dt and end_dt and end_dt > start_dt:
        return _to_rfc3339(start_dt), _to_rfc3339(end_dt)

    now = datetime.now(timezone.utc)
    return _to_rfc3339(now - timedelta(minutes=5)), _to_rfc3339(now)


def _rows_from_tabular(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    fields = [field.get("name") for field in table.get("fields", [])]
    columns = table.get("columns", [])
    if not fields or not columns:
        return []
    row_count = len(columns[0]) if columns else 0
    rows: list[dict[str, Any]] = []
    for row_index in range(row_count):
        row: dict[str, Any] = {}
        for col_index, field_name in enumerate(fields):
            if col_index >= len(columns):
                continue
            column = columns[col_index]
            if row_index >= len(column):
                continue
            if field_name:
                row[field_name] = column[row_index]
        rows.append(row)
    return rows


def _rows_from_query_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _rows_from_tabular(payload)
    if rows:
        return rows

    matches = payload.get("matches")
    if isinstance(matches, list):
        extracted: list[dict[str, Any]] = []
        for match in matches:
            if isinstance(match, dict) and isinstance(match.get("data"), dict):
                extracted.append(match["data"])
        return extracted

    return []


async def _query_axiom_rows(
    *,
    dataset: str,
    service: str,
    host: str,
    ts_start: str,
    ts_end: str,
) -> list[dict[str, Any]]:
    if not settings.axiom_mgmt_token or not dataset or not service:
        return []

    start_time, end_time = _resolve_time_range(ts_start, ts_end)
    service_value = service.replace('"', '\\"')
    apl_parts = [f'| where service contains "{service_value}"']
    if host:
        host_value = host.replace('"', '\\"')
        apl_parts.append(f'| where host == "{host_value}"')
    apl_parts.append(
        '| where message contains "ERROR" or message contains "error" '
        'or message contains "Traceback" or message contains "Exception" '
        'or message contains "CRITICAL"'
    )
    apl_parts.append(
        "| project _time, host, service, message, msg, log, _raw, "
        "status, status_code, code, user_agent, path, url, request_path, requestPath"
    )
    apl_parts.append("| limit 50")
    apl = " ".join(apl_parts)

    headers = _axiom_headers()
    url = f"{AXIOM_QUERY_BASE}/api/v1/datasets/{dataset}/query"
    payload = {"apl": apl, "startTime": start_time, "endTime": end_time}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return []
            return _rows_from_query_payload(payload)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Axiom enrichment query failed: %s", exc)
            return []


def _extract_fields_from_rows(
    rows: list[dict[str, Any]],
) -> tuple[set[str], set[str], list[str], list[str], list[str], list[str]]:
    servers: set[str] = set()
    services: set[str] = set()
    messages: list[str] = []
    statuses: list[str] = []
    user_agents: list[str] = []
    paths: list[str] = []

    for row in rows:
        if host := row.get("host"):
            servers.add(str(host))
        if service := row.get("service"):
            services.add(str(service))

        for key in ("message", "msg", "log", "_raw"):
            value = row.get(key)
            if value:
                messages.append(str(value))
                break

        for key in ("status", "status_code", "code"):
            value = row.get(key)
            if value is not None:
                statuses.append(str(value))
                break

        for key in ("user_agent", "userAgent", "ua"):
            value = row.get(key)
            if value:
                user_agents.append(str(value))
                break

        for key in ("path", "url", "request_path", "requestPath"):
            value = row.get(key)
            if value:
                paths.append(str(value))
                break

    return servers, services, messages, statuses, user_agents, paths


def _match_route(
    rules: dict, *, services: set[str], hosts: set[str], monitor: str
) -> bool:
    """Check if alert metadata matches a route's rules (substring, case-insensitive)."""
    for key, pattern in rules.items():
        p = str(pattern).lower()
        if key == "service":
            if not any(p in s.lower() for s in services):
                return False
        elif key == "host":
            if not any(p in h.lower() for h in hosts):
                return False
        elif key == "monitor":
            if p not in monitor.lower():
                return False
    return True


def resolve_target(
    *,
    services: set[str] | None = None,
    hosts: set[str] | None = None,
    monitor: str = "",
) -> tuple[str, int | None]:
    """Determine chat_id and topic_id for an alert based on routes.yml.

    Falls back to TELEGRAM_CHAT_ID / TELEGRAM_TOPIC_ID env vars if no routes.yml.
    """
    if not _routes:
        chat_id = settings.telegram_chat_id
        topic_id = (
            int(settings.telegram_topic_id) if settings.telegram_topic_id else None
        )
        return chat_id, topic_id

    services = services or set()
    hosts = hosts or set()
    groups = _routes.get("groups", {})
    topics = _routes.get("topics", {})

    for route in _routes.get("routes", []):
        if _match_route(
            route.get("match", {}), services=services, hosts=hosts, monitor=monitor
        ):
            gname = route.get("group", _routes.get("default_group", ""))
            tname = route.get("topic", _routes.get("default_topic", ""))
            return str(groups.get(gname, "")), topics.get(tname)

    gname = _routes.get("default_group", "")
    tname = _routes.get("default_topic", "")
    return str(groups.get(gname, "")), topics.get(tname)


# ── Telegram sender ───────────────────────────────────────────────────────────


async def send_message(
    text: str,
    chat_id: str,
    topic_id: int | None = None,
) -> bool:
    """Send message to Telegram. Returns True on success, False on failure."""
    if not chat_id:
        logger.warning("No chat_id configured — dropping message")
        return False

    # Telegram max message length is 4096 chars
    if len(text) > 4000:
        text = text[:3997] + "…"

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if topic_id is not None:
        payload["message_thread_id"] = topic_id

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10
            )
            if not r.is_success:
                logger.error(f"Telegram API error {r.status_code}: {r.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False


# ── Formatters ────────────────────────────────────────────────────────────────


def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso


def _get_nested(payload: dict, *keys: str) -> Any | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _first_value(*values: Any) -> Any | None:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _coerce_event_body(event: dict[str, Any]) -> dict[str, Any] | None:
    body = event.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_matches(payload: dict) -> list[dict]:
    roots: list[dict[str, Any]] = [payload]
    event = payload.get("event")
    event_dict: dict[str, Any] = event if isinstance(event, dict) else {}
    if event_dict:
        roots.append(event_dict)
    body_dict = _coerce_event_body(event_dict) or {}
    if body_dict:
        roots.append(body_dict)

    for root in roots:
        candidates = [
            _get_nested(root, "queryResult", "matches"),
            _get_nested(root, "result", "matches"),
            _get_nested(root, "matches", "matches"),
            _get_nested(root, "alert", "matches"),
            root.get("matches"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def _guess_service_from_monitor(monitor_name: str) -> str | None:
    if "—" in monitor_name:
        guess = monitor_name.split("—", 1)[0].strip()
        return guess or None
    if " - " in monitor_name:
        guess = monitor_name.split(" - ", 1)[0].strip()
        return guess or None
    return None


def _normalize_monitor_name(name: str) -> str:
    if ": " not in name:
        return name
    prefix, rest = name.split(": ", 1)
    if prefix.lower() in {"triggered", "resolved"}:
        return rest
    return name


def _extract_alert_status(name: str) -> tuple[str | None, str]:
    if ": " not in name:
        return None, name
    prefix, rest = name.split(": ", 1)
    status = prefix.lower()
    if status in {"triggered", "resolved"}:
        return status, rest
    return None, name


def _normalize_service_name(service: str) -> str:
    cleaned = _normalize_monitor_name(service).strip()
    if "—" in cleaned:
        return cleaned.split("—", 1)[0].strip()
    return cleaned


def _extract_axiom_metadata_from_matches(
    matches: list[dict],
) -> tuple[set[str], set[str], list[str]]:
    servers: set[str] = set()
    services: set[str] = set()
    sample_messages: list[str] = []

    for match in matches[:10]:
        data = match.get("data", match)
        if not isinstance(data, dict):
            continue
        if h := data.get("host"):
            servers.add(h)
        if s := data.get("service"):
            services.add(s)
        for key in ("message", "msg", "log", "_raw"):
            if msg := data.get(key):
                if msg not in sample_messages:
                    sample_messages.append(str(msg)[:200])
                break

    return servers, services, sample_messages


def _extract_axiom_alert_fields(
    payload: dict,
) -> tuple[str, str, int | str | None, str, str, list[dict]]:
    event = payload.get("event")
    event_dict: dict[str, Any] = event if isinstance(event, dict) else {}
    body_dict = _coerce_event_body(event_dict) or {}
    name = _first_value(
        payload.get("name"),
        payload.get("monitorName"),
        _get_nested(payload, "monitor", "name"),
        _get_nested(payload, "alert", "monitor", "name"),
        _get_nested(payload, "alert", "monitorName"),
        _get_nested(event_dict, "title"),
        _get_nested(event_dict, "monitor", "name"),
        _get_nested(event_dict, "monitorName"),
        _get_nested(event_dict, "alert", "monitor", "name"),
        _get_nested(event_dict, "alert", "monitorName"),
        _get_nested(body_dict, "name"),
        _get_nested(body_dict, "title"),
        _get_nested(body_dict, "monitor", "name"),
        _get_nested(body_dict, "monitorName"),
        _get_nested(body_dict, "alert", "monitor", "name"),
        _get_nested(body_dict, "alert", "monitorName"),
    )
    description = _first_value(
        payload.get("description"),
        _get_nested(payload, "monitor", "description"),
        _get_nested(payload, "alert", "monitor", "description"),
        _get_nested(event_dict, "description"),
        _get_nested(event_dict, "monitor", "description"),
        _get_nested(event_dict, "alert", "monitor", "description"),
        _get_nested(body_dict, "description"),
        _get_nested(body_dict, "monitor", "description"),
        _get_nested(body_dict, "alert", "monitor", "description"),
    )
    matched_count = _first_value(
        payload.get("matchedCount"),
        _get_nested(payload, "alert", "matchedCount"),
        _get_nested(payload, "alert", "matchCount"),
        _get_nested(payload, "matches", "count"),
        _get_nested(payload, "result", "count"),
        _get_nested(event_dict, "value"),
        _get_nested(event_dict, "valueString"),
        _get_nested(event_dict, "extraCount"),
        _get_nested(event_dict, "matchedCount"),
        _get_nested(event_dict, "alert", "matchedCount"),
        _get_nested(event_dict, "alert", "matchCount"),
        _get_nested(event_dict, "matches", "count"),
        _get_nested(event_dict, "result", "count"),
        _get_nested(body_dict, "matchedCount"),
        _get_nested(body_dict, "alert", "matchedCount"),
        _get_nested(body_dict, "alert", "matchCount"),
        _get_nested(body_dict, "matches", "count"),
        _get_nested(body_dict, "result", "count"),
    )
    if isinstance(matched_count, dict):
        matched_count = matched_count.get("count")

    ts_start = _first_value(
        payload.get("queryStartTime"),
        _get_nested(payload, "alert", "window", "start"),
        _get_nested(payload, "window", "start"),
        _get_nested(payload, "query", "startTime"),
        payload.get("startTime"),
        _get_nested(event_dict, "queryStartTime"),
        _get_nested(event_dict, "alert", "window", "start"),
        _get_nested(event_dict, "window", "start"),
        _get_nested(event_dict, "query", "startTime"),
        event_dict.get("startTime"),
        _get_nested(body_dict, "queryStartTime"),
        _get_nested(body_dict, "alert", "window", "start"),
        _get_nested(body_dict, "window", "start"),
        _get_nested(body_dict, "query", "startTime"),
        body_dict.get("startTime"),
    )
    ts_end = _first_value(
        payload.get("queryEndTime"),
        _get_nested(payload, "alert", "window", "end"),
        _get_nested(payload, "window", "end"),
        _get_nested(payload, "query", "endTime"),
        payload.get("endTime"),
        _get_nested(event_dict, "queryEndTime"),
        _get_nested(event_dict, "alert", "window", "end"),
        _get_nested(event_dict, "window", "end"),
        _get_nested(event_dict, "query", "endTime"),
        event_dict.get("endTime"),
        _get_nested(body_dict, "queryEndTime"),
        _get_nested(body_dict, "alert", "window", "end"),
        _get_nested(body_dict, "window", "end"),
        _get_nested(body_dict, "query", "endTime"),
        body_dict.get("endTime"),
    )

    matches = _coerce_matches(payload)
    if matched_count is None and matches:
        matched_count = len(matches)

    return (
        str(name or ""),
        str(description or ""),
        matched_count,
        str(ts_start or ""),
        str(ts_end or ""),
        matches,
    )


def format_axiom_alert(
    *,
    name: str,
    status: str | None,
    count: int | str | None,
    ts_start: str,
    ts_end: str,
    servers: set[str],
    services: set[str],
    sample_messages: list[str],
    top_error: str | None,
    tag: str,
    host_service: str,
    runbook: list[str],
) -> str:
    display_name = name or "Unknown monitor"
    display_count = count if count is not None else "?"
    title_prefix = "✅" if status == "resolved" else "🚨"
    header = (
        f"{tag} {title_prefix} <b>{display_name}</b>"
        if tag
        else f"{title_prefix} <b>{display_name}</b>"
    )

    lines = [header]
    if host_service:
        lines.append(f"📍 {host_service}")
    else:
        if servers:
            lines.append(f"🖥 Server: {', '.join(sorted(servers))}")
        if services:
            lines.append(f"⚙️ Service: {', '.join(sorted(services))}")
    lines.append(f"📊 Событий: <b>{display_count}</b>")
    if ts_start and ts_end:
        lines.append(f"🕐 {_fmt_dt(ts_start)} → {_fmt_dt(ts_end)}")
    if top_error:
        lines.append(f"🧾 Топ-ошибка: <code>{_sanitize_line(top_error, 200)}</code>")
    if sample_messages:
        lines.append("🧾 Примеры:")
        for m in sample_messages:
            lines.append(f"<code>{_sanitize_line(m, 200)}</code>")
    if runbook:
        lines.append("Что делать:")
        lines.append("<blockquote>")
        for step in runbook:
            lines.append(_sanitize_line(step, 300))
        lines.append("</blockquote>")

    return "\n".join(lines)


# ── App ───────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _routes:
        n = len(_routes.get("routes", []))
        logger.info(f"Loaded {n} route(s) from routes.yml")
    elif settings.telegram_chat_id:
        logger.info("No routes.yml — using TELEGRAM_CHAT_ID/TELEGRAM_TOPIC_ID fallback")
    else:
        logger.warning("No routes.yml and no TELEGRAM_CHAT_ID — alerts will be dropped")
    stop_event = asyncio.Event()
    auto_task = None
    if settings.axiom_mgmt_token:
        logger.info(
            "Axiom auto-attach enabled - interval %ss",
            settings.axiom_attach_interval_seconds,
        )
        auto_task = asyncio.create_task(_auto_attach_notifiers_loop(stop_event))
    else:
        logger.info("AXIOM_MGMT_TOKEN not set - Axiom auto-attach disabled")
    yield
    if auto_task is not None:
        stop_event.set()
        auto_task.cancel()
        with suppress(asyncio.CancelledError):
            await auto_task


app = FastAPI(title="alertbot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


class LocalAlert(BaseModel):
    title: str
    body: str = ""


@app.post("/alert/local")
async def local_alert(alert: LocalAlert):
    """Accept alerts from local services (health-watcher).

    Returns 502 if Telegram delivery fails, so the caller can fall back
    to sending directly via Telegram API.
    """
    lines = [f"🔧 <b>{alert.title}</b>"]
    if alert.body:
        lines.append(f"<code>{alert.body}</code>")
    text = "\n".join(lines)

    logger.info(f"Local alert: {alert.title!r}")

    # Extract service from title for routing (format: "Container unhealthy: <name>")
    service = (
        alert.title.split(":", 1)[-1].strip() if ":" in alert.title else alert.title
    )
    chat_id, topic_id = resolve_target(services={service})

    ok = await send_message(text, chat_id=chat_id, topic_id=topic_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram send failed")
    return {"ok": True}


@app.post("/webhook/axiom")
async def axiom_webhook(request: Request):
    if settings.webhook_secret:
        if request.headers.get("X-Webhook-Secret") != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    (
        monitor_name,
        _description,
        matched_count,
        ts_start,
        ts_end,
        matches,
    ) = _extract_axiom_alert_fields(payload)
    status, normalized_name = _extract_alert_status(monitor_name)
    defaults = _config_defaults()
    include_resolved = _coerce_bool(
        defaults.get("include_resolved"), settings.alertbot_include_resolved
    )
    if not monitor_name:
        event_keys: list[str] = []
        if isinstance(payload.get("event"), dict):
            event_keys = list(payload.get("event", {}).keys())
        logger.warning(
            "Axiom webhook missing monitor name, keys=%s event_keys=%s",
            list(payload.keys()),
            event_keys,
        )
    if status == "resolved" and not include_resolved:
        logger.info(
            "Axiom resolved alert skipped: %r — %s events",
            normalized_name,
            matched_count,
        )
        return {"ok": True}
    logger.info(
        "Axiom alert: %s %r — %s events",
        status or "triggered",
        normalized_name,
        matched_count,
    )

    route_monitor = normalized_name or _normalize_monitor_name(monitor_name)
    (
        servers,
        services,
        messages,
        statuses,
        user_agents,
        paths,
    ) = _extract_match_fields(matches)
    if not services:
        guessed_service = _guess_service_from_monitor(route_monitor)
        if guessed_service:
            services.add(guessed_service)

    service_hint = sorted(services)[0] if services else ""
    host_hint = sorted(servers)[0] if servers else ""
    if (
        not messages
        and settings.axiom_mgmt_token
        and settings.axiom_dataset
        and service_hint
    ):
        rows = await _query_axiom_rows(
            dataset=settings.axiom_dataset,
            service=service_hint,
            host=host_hint,
            ts_start=ts_start,
            ts_end=ts_end,
        )
        if rows:
            (
                row_servers,
                row_services,
                row_messages,
                row_statuses,
                row_user_agents,
                row_paths,
            ) = _extract_fields_from_rows(rows)
            servers |= row_servers
            services |= row_services
            messages.extend(row_messages)
            statuses.extend(row_statuses)
            user_agents.extend(row_user_agents)
            paths.extend(row_paths)

    services = {_normalize_service_name(s) for s in services if s}

    top_error_enabled = _coerce_bool(defaults.get("top_error"), True)
    top_error = _most_common(messages) if top_error_enabled else None
    sample_count = _coerce_int(defaults.get("sample_count"), 2)
    sample_messages = _sample_messages(messages, sample_count)

    top_status = _most_common(statuses) or ""
    top_user_agent = _most_common(user_agents) or ""
    top_path = _most_common(paths) or ""

    host_value, service_value, host_service = _format_host_service(servers, services)
    context = {
        "title": route_monitor,
        "message": top_error or "",
        "status": top_status,
        "user_agent": top_user_agent,
        "path": top_path,
        "host": host_value,
        "service": service_value,
    }
    if _should_drop(context):
        logger.info("Axiom alert dropped by filter: %r", route_monitor)
        return {"ok": True}

    profile_names = _get_service_profiles(services)
    is_p1 = _is_p1(profile_names, context)
    tags = _config_tags()
    tag = tags["user_impact"] if is_p1 else tags["service_errors"]
    runbook = _render_runbook(
        _resolve_runbook(services, profile_names),
        host_value,
        service_value,
        route_monitor,
    )

    chat_id, topic_id = resolve_target(
        services=services, hosts=servers, monitor=route_monitor
    )

    await send_message(
        format_axiom_alert(
            name=route_monitor or normalized_name,
            status=status,
            count=matched_count,
            ts_start=ts_start,
            ts_end=ts_end,
            servers=servers,
            services=services,
            sample_messages=sample_messages,
            top_error=top_error,
            tag=tag,
            host_service=host_service,
            runbook=runbook,
        ),
        chat_id=chat_id,
        topic_id=topic_id,
    )
    return {"ok": True}
