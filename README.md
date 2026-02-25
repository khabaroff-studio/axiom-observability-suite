# axiom-observability-suite

Готовый стек мониторинга для Docker-серверов. Ставится за минуту, работает незаметно,
пишет в Telegram когда что-то идёт не так.

## Что это делает

Стек решает одну задачу: **знать о проблемах раньше пользователей.**

Как только сервис запущен в Docker — его логи автоматически уходят в Axiom.
Ничего не нужно настраивать внутри контейнера, ничего не нужно менять в коде.
Vector читает stdout всех контейнеров на хосте и отправляет в облако.

Поверх логов работают алерты. Axiom анализирует поток и, если видит ошибки,
отправляет уведомление через бота в Telegram-группу. Один алерт за пять минут
максимум — без шквала одинаковых сообщений.

Параллельно работает наблюдение за здоровьем контейнеров. Если Docker healthcheck
какого-то сервиса переходит в `unhealthy` и остаётся там дольше двух минут — алерт.
Не нужно вручную проверять `docker ps` — проблемы приходят сами.

Сам стек мониторинга тоже под наблюдением. Если падает бот или Vector,
health-watcher доставит алерт напрямую в Telegram, минуя сломанный компонент.
Петли и амплификация исключены архитектурно.

Стек портативный: один `docker compose up -d` на новом сервере — и он под наблюдением.
Бот с Telegram-алертами нужен только на одном сервере, остальные просто шлют логи.

## Компоненты

| Сервис | Что делает |
|--------|-----------|
| **axiom-log-shipper** | Vector собирает логи всех контейнеров → Axiom |
| **axiom-health-watcher** | Следит за Docker healthcheck, алертит при unhealthy |
| **axiom-to-telegram-bot** | Принимает алерты (от Axiom и локальные) → Telegram |

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
- [docs/SELF-MONITORING.md](docs/SELF-MONITORING.md) — как стек мониторит сам себя без петель

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
