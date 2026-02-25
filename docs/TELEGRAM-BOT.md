# axiom-to-telegram-bot: единая точка алертов → Telegram

Микросервис, который принимает алерты из двух источников и маршрутизирует в Telegram-группу по топикам.

## Архитектура

```
Axiom Monitor   → POST /webhook/axiom  ─┐
                                         ├→  alertbot  ──routes.yml──→  Telegram topics
health-watcher  → POST /alert/local    ─┘
```

- `/webhook/axiom` — внешний путь для Axiom Monitors (через reverse proxy)
- `/alert/local` — внутренний путь для health-watcher (Docker-сеть, без auth)

Подробнее о self-monitoring: [SELF-MONITORING.md](SELF-MONITORING.md).

## Расположение

```
/opt/axiom-observability-suite/
├── app.py              # весь код axiom-to-telegram-bot
├── Dockerfile
├── docker-compose.yml  # axiom-log-shipper + axiom-to-telegram-bot (profiles)
├── requirements.txt
├── vector.toml         # конфиг axiom-log-shipper
├── routes.yml          # маршрутизация алертов по топикам (не в git)
├── routes.yml.example  # шаблон для routes.yml
├── .env
└── .env.example
```

## Конфигурация

### .env

```
TELEGRAM_BOT_TOKEN=     # токен бота от @BotFather
WEBHOOK_SECRET=         # опционально: Axiom передаёт в заголовке X-Webhook-Secret
```

### routes.yml (маршрутизация по топикам)

```yaml
groups:
  my-group: -100XXXXXXXXXX       # Telegram supergroup ID (с префиксом -100)

topics:
  general: 1                      # General topic
  project-a: 123                  # Topic для проекта A

routes:
  # Substring match по: service (имя контейнера), host (сервер), monitor (имя Axiom монитора).
  # Первый совпавший route побеждает.
  - match: { service: "my-service" }
    group: my-group
    topic: project-a

default_group: my-group
default_topic: general
```

**Как получить ID топика:** из ссылки на сообщение в топике.
Ссылка вида `https://t.me/c/3779402801/97/165` → topic_id = `97`, chat_id = `-1003779402801`.

**Без routes.yml бот не запустится** (fail fast).

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | healthcheck (используется Docker healthcheck) |
| POST | `/webhook/axiom` | вебхуки от Axiom Monitor (внешний, через reverse proxy) |
| POST | `/alert/local` | алерты от health-watcher (внутренний, Docker-сеть) |

Публичный URL (через reverse proxy): `https://your-server.example.com/alertbot/webhook/axiom`

`/alert/local` принимает `{"title": "...", "body": "..."}`, возвращает 502 если Telegram не ответил.

## Формат вебхука от Axiom

Alertbot читает из payload:

- `name` — название монитора
- `description` — описание монитора
- `matchedCount` — количество событий
- `queryStartTime` / `queryEndTime` — временной диапазон
- `queryResult.matches[].data.host` — сервер
- `queryResult.matches[].data.service` — сервис/контейнер
- `queryResult.matches[].data.message` — sample сообщений

## Управление

```bash
# Статус
docker ps | grep axiom-to-telegram-bot

# Логи
docker logs axiom-to-telegram-bot --tail 30

# Пересоздать контейнер (подхватывает новый routes.yml)
docker compose -f /opt/axiom-observability-suite/docker-compose.yml --profile alertbot up -d

# Пересобрать образ (после изменений app.py)
docker compose -f /opt/axiom-observability-suite/docker-compose.yml --profile alertbot up -d --build
```

## Настройка Axiom Monitor

В Axiom создать Monitor:

1. **Query** — APL-запрос, например:
   ```
   ['prod-docker-logs']
   | where service == "my-service"
   | where message contains_cs "ERROR" or message contains_cs "Traceback"
   ```
2. **Threshold** — например, `>= 1` за последние 5 минут.
3. **Notifier** — добавить webhook notifier с URL:
   `https://your-server.example.com/alertbot/webhook/axiom`
4. Опционально: добавить кастомный заголовок `X-Webhook-Secret` и прописать его в `.env`.

## Перенос на другой сервер

1. Скопировать `/opt/axiom-observability-suite/` на новый сервер.
2. Скопировать `.env` (убедиться что `COMPOSE_PROFILES=alertbot` раскомментирован).
3. Скопировать `routes.yml`.
4. Настроить reverse proxy (axiom-to-telegram-bot слушает на `127.0.0.1:8092`):
   - **nginx** (если уже установлен):
     ```nginx
     location /alertbot/ {
         proxy_pass http://127.0.0.1:8092/;
         proxy_set_header Host $host;
     }
     ```
     ```bash
     certbot --nginx -d <domain>
     ```
   - **Caddy** (если nginx нет — проще, TLS из коробки):
     ```
     <domain> {
         reverse_proxy /alertbot/* localhost:8092
     }
     ```
5. `docker compose --profile alertbot up -d --build`
6. Обновить URL вебхука в Axiom Notifier на новый домен.
