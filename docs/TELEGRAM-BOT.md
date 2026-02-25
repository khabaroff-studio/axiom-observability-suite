# axiom-to-telegram-bot: Axiom → Telegram

Микросервис, который принимает вебхуки от Axiom и отправляет алерты в Telegram-группу.

## Архитектура

```
Axiom Monitor → POST /webhook/axiom → axiom-to-telegram-bot → Telegram group/topic
```

Alertbot — единая точка для всех серверов. Аксиом агрегирует логи с нескольких серверов,
axiom-to-telegram-bot принимает его вебхуки и маршрутизирует в нужные топики группы.

## Расположение

```
/opt/axiom-observability-suite/
├── app.py              # весь код axiom-to-telegram-bot
├── Dockerfile
├── docker-compose.yml  # axiom-log-shipper + axiom-to-telegram-bot (profiles)
├── requirements.txt
├── vector.toml         # конфиг axiom-log-shipper
├── .env
├── .env.example
└── docs/
```

## Конфигурация (.env)

```
TELEGRAM_BOT_TOKEN=     # токен бота от @BotFather
TELEGRAM_CHAT_ID=       # ID группы (отрицательное число, напр. -1003779402801)
TELEGRAM_TOPIC_ID=      # ID топика (число из ссылки на сообщение в топике)
SETUP_MODE=false        # true — режим настройки (см. ниже)
WEBHOOK_SECRET=         # опционально: Axiom передаёт в заголовке X-Webhook-Secret
```

### Как получить TELEGRAM_CHAT_ID и TELEGRAM_TOPIC_ID

1. Создать бота через @BotFather, получить токен.
2. Добавить бота в нужную группу.
3. В `.env` выставить `SETUP_MODE=true`, запустить контейнер.
4. Упомянуть бота (`@botname`) в нужном топике группы.
5. Бот ответит сообщением с `TELEGRAM_CHAT_ID` и `TELEGRAM_TOPIC_ID`.
6. Вписать значения в `.env`, выставить `SETUP_MODE=false`, перезапустить.

**Альтернативно:** взять topic_id из ссылки на любое сообщение в топике.
Ссылка вида `https://t.me/c/3779402801/97/165` → topic_id = `97`.

**Важно:** в группах с топиками (Forum) бот получает упоминания, но не все сообщения
без права "читать все сообщения". Для setup mode достаточно упоминания.

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | healthcheck |
| POST | `/webhook/axiom` | принимает вебхук от Axiom Monitor |

Публичный URL (через reverse proxy): `https://your-server.example.com/alertbot/webhook/axiom`

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

# Пересоздать контейнер (подхватывает новый .env)
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

## Маршрутизация по топикам (план)

Сейчас все алерты идут в один топик. Когда понадобится маршрутизация:

**Подход:** отдельный Axiom Monitor на каждый проект/сервис.
Бот не думает — название монитора определяет топик.

**Планируемый формат `.env`:**

```
TOPIC_ROUTES=service-a:123,service-b:456,service-c:789
TOPIC_DEFAULT=97
```

Реализация — ~10 строк в `app.py`: парсим `TOPIC_ROUTES`,
ищем совпадение по полю `name` из payload, fallback на `TOPIC_DEFAULT`.

## Перенос на другой сервер

1. Скопировать `/opt/axiom-observability-suite/` на новый сервер.
2. Скопировать `.env` (убедиться что `COMPOSE_PROFILES=alertbot` раскомментирован).
3. Настроить reverse proxy (axiom-to-telegram-bot слушает на `127.0.0.1:8092`):
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
4. `docker compose --profile alertbot up -d --build`
5. Обновить URL вебхука в Axiom Notifier на новый домен.
