"""alertbot — Axiom webhook → Telegram notifier.

Accepts alerts from two sources:
- POST /webhook/axiom — Axiom Monitor webhooks (external)
- POST /alert/local  — local alerts from health-watcher (internal, no auth)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
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
    setup_mode: bool = False
    webhook_secret: str = ""  # optional, matched against X-Webhook-Secret header

    model_config = {"env_file": ".env"}


settings = Settings()
TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


# ── Telegram sender ───────────────────────────────────────────────────────────


async def send_message(
    text: str,
    chat_id: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Send message to Telegram. Returns True on success, False on failure."""
    target_chat = chat_id or settings.telegram_chat_id
    target_thread = thread_id or settings.telegram_topic_id or None

    if not target_chat:
        logger.warning("No TELEGRAM_CHAT_ID configured — dropping message")
        return False

    # Telegram max message length is 4096 chars
    if len(text) > 4000:
        text = text[:3997] + "…"

    payload: dict[str, Any] = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
    }
    if target_thread:
        payload["message_thread_id"] = int(target_thread)

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


def format_axiom_alert(payload: dict) -> str:
    name = payload.get("name", "Unknown monitor")
    description = payload.get("description", "")
    count = payload.get("matchedCount", "?")
    ts_start = payload.get("queryStartTime", "")
    ts_end = payload.get("queryEndTime", "")

    # Extract servers/services from matched events (if Axiom includes them)
    servers: set[str] = set()
    services: set[str] = set()
    sample_messages: list[str] = []

    matches = payload.get("queryResult", {}).get("matches", [])
    for match in matches[:10]:
        data = match.get("data", {})
        if h := data.get("host"):
            servers.add(h)
        if s := data.get("service"):
            services.add(s)
        # grab a sample log message if present
        for key in ("message", "msg", "log", "_raw"):
            if msg := data.get(key):
                if msg not in sample_messages:
                    sample_messages.append(str(msg)[:200])
                break

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


# ── Setup mode: polling to discover chat_id / thread_id ──────────────────────


async def _setup_polling() -> None:
    logger.info("SETUP MODE active — send any message in the target group/topic")
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=40,
                )
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue

                    chat_id = msg["chat"]["id"]
                    thread_id = msg.get("message_thread_id")
                    chat_title = msg["chat"].get("title", str(chat_id))

                    logger.info(
                        f"Message in {chat_title!r}: "
                        f"chat_id={chat_id}, thread_id={thread_id}"
                    )

                    reply = (
                        f"✅ <b>Setup info for:</b> {chat_title}\n\n"
                        f"TELEGRAM_CHAT_ID=<code>{chat_id}</code>\n"
                        f"TELEGRAM_TOPIC_ID=<code>{thread_id or ''}</code>\n\n"
                        f"Add these to .env, set SETUP_MODE=false, restart."
                    )
                    reply_payload: dict[str, Any] = {
                        "chat_id": chat_id,
                        "text": reply,
                        "parse_mode": "HTML",
                    }
                    if thread_id:
                        reply_payload["message_thread_id"] = thread_id
                    await client.post(f"{TELEGRAM_API}/sendMessage", json=reply_payload)

            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)


# ── App ───────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.setup_mode:
        asyncio.create_task(_setup_polling())
    else:
        if not settings.telegram_chat_id:
            logger.warning(
                "TELEGRAM_CHAT_ID not set — set SETUP_MODE=true to discover it"
            )
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

    ok = await send_message(text)
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

    logger.info(
        f"Axiom alert: {payload.get('name')!r} — {payload.get('matchedCount')} events"
    )

    await send_message(format_axiom_alert(payload))
    return {"ok": True}
