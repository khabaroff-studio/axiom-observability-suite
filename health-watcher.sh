#!/bin/sh
# Watches Docker health_status events.
# Logs a JSON line only if the container stays unhealthy for UNHEALTHY_ALERT_DELAY_SECONDS.
# Vector picks up the output and forwards it to Axiom.
#
# For axiom-* containers (the observability stack itself), alerts go through
# a separate path to avoid feedback loops:
#   1. POST to alertbot's /alert/local endpoint
#   2. Fallback: direct wget to Telegram API
#   3. Last resort: stdout (Axiom path)

DELAY="${UNHEALTHY_ALERT_DELAY_SECONDS:-120}"
ALERTBOT_URL="http://axiom-to-telegram-bot:8080/alert/local"

# ── Alert functions ──────────────────────────────────────────────────────────

alert_via_stdout() {
  printf '{"service":"health-watcher","container":"%s","health_status":"unhealthy"}\n' "$1"
}

alert_via_local() {
  container="$1"
  json="{\"title\":\"Container unhealthy: ${container}\",\"body\":\"${container} has been unhealthy for ${DELAY}s\"}"

  # Try local alertbot endpoint (returns non-zero on connection refused or 5xx)
  if wget -q -O /dev/null -T 3 \
    --header 'Content-Type: application/json' \
    --post-data "$json" \
    "$ALERTBOT_URL" 2>/dev/null; then
    return 0
  fi

  # Fallback: direct Telegram API
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    text="🔧 <b>Container unhealthy: ${container}</b>
<code>${container} has been unhealthy for ${DELAY}s (alertbot unavailable)</code>"
    tg_payload="{\"chat_id\":\"${TELEGRAM_CHAT_ID}\",\"text\":\"${text}\",\"parse_mode\":\"HTML\"}"
    if [ -n "$TELEGRAM_TOPIC_ID" ]; then
      tg_payload="{\"chat_id\":\"${TELEGRAM_CHAT_ID}\",\"text\":\"${text}\",\"parse_mode\":\"HTML\",\"message_thread_id\":${TELEGRAM_TOPIC_ID}}"
    fi
    if wget -q -O /dev/null -T 5 \
      --header 'Content-Type: application/json' \
      --post-data "$tg_payload" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" 2>/dev/null; then
      return 0
    fi
  fi

  # Last resort: stdout for Axiom (on servers without alertbot, this is the only path)
  alert_via_stdout "$container"
}

# ── Main loop ────────────────────────────────────────────────────────────────

docker events \
  --filter event=health_status \
  --format '{{.Actor.Attributes.name}} {{.Actor.Attributes.healthStatus}}' \
| while IFS= read -r line; do
  container="${line% *}"
  status="${line##* }"

  if [ "$status" = "unhealthy" ]; then
    (
      sleep "$DELAY"
      current=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null)
      if [ "$current" = "unhealthy" ]; then
        case "$container" in
          axiom-*) alert_via_local "$container" ;;
          *)       alert_via_stdout "$container" ;;
        esac
      fi
    ) &
  fi
done
