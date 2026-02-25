# axiom-observability-suite

Портативный стек наблюдаемости для Docker-серверов:
- **axiom-log-shipper** — Vector собирает логи всех контейнеров и отправляет в Axiom
- **axiom-health-watcher** — следит за Docker healthcheck-событиями, алертит если контейнер нездоров
- **axiom-to-telegram-bot** — принимает вебхуки от Axiom Monitors и отправляет алерты в Telegram

Все три сервиса управляются одним `docker compose`. `axiom-to-telegram-bot` запускается только там, где задан `COMPOSE_PROFILES=alertbot`.

## Быстрый старт

```bash
cp .env.example .env
# Заполнить AXIOM_TOKEN, AXIOM_DATASET, HOSTNAME
docker compose up -d
```

Логи всех контейнеров пойдут в Axiom, Docker health events — под наблюдением.

### С axiom-to-telegram-bot (на одном сервере)

```bash
# Дополнительно в .env:
# COMPOSE_PROFILES=alertbot
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_TOPIC_ID, AXIOM_MGMT_TOKEN
docker compose --profile alertbot up -d
```

Нужен reverse proxy (nginx или Caddy) для публичного URL вебхука — см. [docs/TELEGRAM-BOT.md](docs/TELEGRAM-BOT.md).

## Документация

- [docs/FOR-DEVELOPERS.md](docs/FOR-DEVELOPERS.md) — **для разработчиков сервисов**: как интегрироваться с мониторингом
- [docs/LOG-SHIPPER.md](docs/LOG-SHIPPER.md) — как устроен axiom-log-shipper, рекомендации по логированию в приложениях
- [docs/MONITORING-MANAGEMENT.md](docs/MONITORING-MANAGEMENT.md) — APL-запросы, мониторы, CLI (`axiom_cli.py`), MCP
- [docs/TELEGRAM-BOT.md](docs/TELEGRAM-BOT.md) — настройка axiom-to-telegram-bot, Telegram, маршрутизация по топикам

## Управление

```bash
# Статус
docker ps | grep axiom-

# Логи
docker logs axiom-log-shipper --tail 50
docker logs axiom-health-watcher --tail 50
docker logs axiom-to-telegram-bot --tail 50

# Перезапуск (новый .env)
docker compose --profile alertbot up -d
```
