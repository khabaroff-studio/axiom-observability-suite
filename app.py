"""alertbot — Axiom webhook → Telegram notifier with topic routing.

Accepts alerts from two sources:
- POST /webhook/axiom — Axiom Monitor webhooks (external)
- POST /alert/local  — local alerts from health-watcher (internal, no auth)

Routing rules are defined in routes.yml. Without it, falls back to
TELEGRAM_CHAT_ID / TELEGRAM_TOPIC_ID from environment.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pydantic_settings import BaseSettings

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

    model_config = {"env_file": ".env"}


settings = Settings()
TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


# ── Routes ────────────────────────────────────────────────────────────────────


def _load_routes() -> dict:
    """Load routing config from routes.yml. Returns {} if file missing."""
    routes_file = Path(__file__).parent / "routes.yml"
    if not routes_file.exists():
        return {}
    with open(routes_file) as f:
        return yaml.safe_load(f) or {}


_routes = _load_routes()


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


def _extract_axiom_metadata(payload: dict) -> tuple[set[str], set[str], list[str]]:
    """Extract servers, services, and sample messages from Axiom payload."""
    servers: set[str] = set()
    services: set[str] = set()
    sample_messages: list[str] = []

    for match in payload.get("queryResult", {}).get("matches", [])[:10]:
        data = match.get("data", {})
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


def format_axiom_alert(
    payload: dict, servers: set[str], services: set[str], sample_messages: list[str]
) -> str:
    name = payload.get("name", "Unknown monitor")
    description = payload.get("description", "")
    count = payload.get("matchedCount", "?")
    ts_start = payload.get("queryStartTime", "")
    ts_end = payload.get("queryEndTime", "")

    lines = [f"🚨 <b>{name}</b>"]
    if description:
        lines.append(f"<i>{description}</i>")
    lines.append("")
    lines.append(f"📊 Events: <b>{count}</b>")
    if servers:
        lines.append(f"🖥 Server: {', '.join(sorted(servers))}")
    if services:
        lines.append(f"⚙️ Service: {', '.join(sorted(services))}")
    if ts_start and ts_end:
        lines.append(f"🕐 {_fmt_dt(ts_start)} → {_fmt_dt(ts_end)}")
    if sample_messages:
        lines.append("")
        lines.append("<b>Sample:</b>")
        for m in sample_messages[:3]:
            lines.append(f"<code>{m}</code>")

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
    yield


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

    monitor_name = payload.get("name", "")
    logger.info(f"Axiom alert: {monitor_name!r} — {payload.get('matchedCount')} events")

    servers, services, samples = _extract_axiom_metadata(payload)
    chat_id, topic_id = resolve_target(
        services=services, hosts=servers, monitor=monitor_name
    )

    await send_message(
        format_axiom_alert(payload, servers, services, samples),
        chat_id=chat_id,
        topic_id=topic_id,
    )
    return {"ok": True}
