# Self-monitoring: кто сторожит сторожей

Как observability-стек мониторит сам себя, не создавая петель.

---

## Проблема

Обычные контейнеры мониторятся через цепочку:

```
контейнер → stdout → Vector → Axiom → Monitor → webhook → bot → Telegram
```

Если мониторить observability-стек через эту же цепочку:

- **bot умирает** → webhook приходит в мёртвого бота → тишина
- **Vector умирает** → логи перестают течь → мониторы молчат → тишина
- **Ошибки в боте** → логи ошибок → Axiom → monitor → webhook → бот → ещё ошибки → амплификация

---

## Решение: отдельный путь

Для `axiom-*` контейнеров health-watcher использует локальный HTTP-эндпоинт бота
вместо Axiom pipeline:

```
Обычные контейнеры:    unhealthy → stdout → Vector → Axiom → Monitor → bot → Telegram
Observability стек:    unhealthy → health-watcher → POST /alert/local → bot → Telegram
```

### Трёхуровневый fallback

Если `axiom-*` контейнер остаётся unhealthy дольше `UNHEALTHY_ALERT_DELAY_SECONDS`:

1. **POST `http://axiom-to-telegram-bot:8080/alert/local`** — основной путь.
   Бот форматирует и шлёт в Telegram. Если Telegram не ответил — возвращает 502.

2. **wget в Telegram API напрямую** — если бот недоступен (connection refused / 502).
   Использует `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TOPIC_ID` из `.env`.

3. **stdout** — последний шанс. На серверах без alertbot это единственный путь,
   события уйдут в Axiom через Vector как обычно.

### Почему это безопасно

- **Нет петли**: `axiom-*` события не идут через Axiom pipeline на серверах с alertbot
- **Нет амплификации**: health-watcher шлёт один алерт на контейнер (проверяет "всё ещё unhealthy?" после задержки)
- **Fallback прозрачен**: если бот лежит — Telegram получит алерт с пометкой "(alertbot unavailable)"

---

## Healthcheck'и

Все три observability-контейнера имеют Docker healthcheck:

| Контейнер | Проверка | Что подтверждает |
|-----------|----------|-----------------|
| axiom-to-telegram-bot | `python3 urllib → /health` | FastAPI отвечает |
| axiom-log-shipper | `test -f /proc/1/status` | Процесс Vector жив |
| axiom-health-watcher | `docker info` | Docker socket доступен |

health-watcher слушает `health_status` события Docker — когда healthcheck контейнера
переходит в `unhealthy`, он получает событие и обрабатывает его.

---

## Эндпоинт `/alert/local`

Локальный синк для алертов от внутренних сервисов (health-watcher).

```
POST /alert/local
Content-Type: application/json

{"title": "Container unhealthy: axiom-log-shipper", "body": "Details..."}
```

- Доступен только внутри Docker-сети (не проброшен наружу)
- Не требует аутентификации (внутренний трафик)
- Возвращает `502` если не удалось отправить в Telegram (для fallback)

---

## Что НЕ делать

- **Не создавать Axiom-мониторы для `axiom-*` сервисов** — это создаст петлю
  (монитор → webhook → бот, который может быть причиной проблемы)
- **Не добавлять heartbeat** — Docker `restart: unless-stopped` обрабатывает крэши,
  а мониторинг "весь VPS лежит" требует внешнего сервиса и выходит за рамки этого стека
