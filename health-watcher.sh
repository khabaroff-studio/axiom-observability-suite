#!/bin/sh
# Watches Docker health_status events.
# Logs a JSON line only if the container stays unhealthy for UNHEALTHY_ALERT_DELAY_SECONDS.
# Vector picks up the output and forwards it to Axiom.

DELAY="${UNHEALTHY_ALERT_DELAY_SECONDS:-120}"

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
        printf '{"service":"health-watcher","container":"%s","health_status":"unhealthy"}\n' "$container"
      fi
    ) &
  fi
done
