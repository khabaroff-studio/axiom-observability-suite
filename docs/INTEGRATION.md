# Интеграция и логирование: как сервис попадает в Axiom

Кому читать: разработчикам сервисов и тем, кто настраивает логирование/healthcheck.

---

## Как устроено

```
сервис → stdout → Docker → axiom-log-shipper (Vector) → Axiom
                                                             ↓
                                              Axiom Monitor (при ошибках)
                                                             ↓
                                        axiom-to-telegram-bot → Telegram

Docker healthcheck → health_status event → axiom-health-watcher → алерт
```

**Автоматически** (ничего делать не нужно):
- Все логи из stdout/stderr собираются и отправляются в Axiom
- Каждый контейнер доступен в Axiom по полю `service` = имя контейнера
- Если контейнер имеет healthcheck и остаётся `unhealthy` дольше ~2 минут — алерт в Telegram

**Требует настройки:**
- Алерт на ошибки — создаётся один раз DevOps через `axiom_cli.py monitors create <service>`
- Healthcheck в `docker-compose.yml` — для алертов на недоступность (см. ниже)

---

## Чеклист для нового сервиса

### 1. Писать логи в stdout

Vector собирает только stdout/stderr. Файловые логи внутри контейнера в Axiom не попадут.

**Python:**
```python
import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
```

**TypeScript / Node.js** — использовать `pino` (пишет в stdout по умолчанию):
```typescript
import pino from "pino";
const logger = pino({ level: "info" });
```

### 2. Логировать исключения со стектрейсом

Монитор ловит события, содержащие `ERROR` или `Traceback`. Важно, чтобы стектрейс
попал в одно событие, а не размазался по строкам. Подробности и примеры — ниже.

### 3. Добавить HEALTHCHECK в docker-compose.yml

`axiom-health-watcher` следит за Docker health-статусом всех контейнеров.
Если контейнер остаётся `unhealthy` дольше ~2 минут — приходит алерт в Telegram.

Добавь в `docker-compose.yml` своего сервиса:
```yaml
services:
  my-service:
    # ...
    healthcheck:
      test: ["CMD-SHELL", "<команда проверки>"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
```

**Примеры test-команды:**

Для HTTP-сервера:
```yaml
test: ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"]
```

Если curl нет в образе (Python):
```yaml
test: ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5)\""]
```

Проверка что процесс жив (если нет HTTP):
```yaml
test: ["CMD-SHELL", "tr '\\0' ' ' < /proc/1/cmdline | grep -q 'my_module.main'"]
```

### 4. Сообщить имя контейнера DevOps

После деплоя DevOps создаёт Axiom-монитор одной командой:
```bash
python3 /opt/axiom-observability-suite/axiom_cli.py monitors create <имя_контейнера>
```

Монитор алертит при любом событии с `ERROR`, `error`, `Traceback` или `Exception` в логах.
Один алерт в 5 минут максимум — дедупликация встроена.

---

## Что попадает в Axiom

Каждое событие имеет поля:
- `host` — имя сервера
- `service` — имя Docker-контейнера
- `message` — строка из stdout
- `_time` — время

Если приложение пишет **JSON в stdout** — поля JSON становятся отдельными колонками в Axiom,
что удобно для фильтрации. Но обычный текстовый лог тоже работает.

---

## Vector: настройка и управление

Расположение на сервере: `/opt/axiom-observability-suite/`

**vector.toml:**
```toml
[sources.docker]
type = "docker_logs"
exclude_containers = ["axiom-log-shipper"]

[transforms.enrich]
type = "remap"
inputs = ["docker"]
source = '''
.host = "${HOSTNAME}"       # имя сервера
.service = .container_name  # имя контейнера
.source = "docker"
'''

[sinks.axiom]
type = "axiom"              # нативный sink (Vector 0.34+)
inputs = ["enrich"]
token = "${AXIOM_TOKEN}"
dataset = "${AXIOM_DATASET}"
```

**docker-compose.yml:**
```yaml
services:
  axiom-log-shipper:
    image: timberio/vector:0.36.0-alpine
    container_name: axiom-log-shipper
    restart: unless-stopped
    env_file: .env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./vector.toml:/etc/vector/vector.toml:ro
    command: ["--config", "/etc/vector/vector.toml"]  # без этого грузит дефолтный конфиг!
```

**Управление:**
```bash
docker ps | grep axiom-log-shipper
docker logs axiom-log-shipper --tail 50
docker restart axiom-log-shipper                                      # новый vector.toml
docker compose -f /opt/axiom-observability-suite/docker-compose.yml up -d    # новый .env
```

**Добавить новый сервер:** скопировать `/opt/axiom-observability-suite/` на новый сервер,
заполнить `.env` (без `COMPOSE_PROFILES`), запустить `docker compose up -d`.

**Подводные камни:**
- Без `command: ["--config", ...]` Vector игнорирует `vector.toml` и грузит дефолтный `vector.yaml`
- `type = "http"` вместо `type = "axiom"` требует заголовок `Content-Type: application/x-ndjson`, иначе 415

---

## Рекомендации по логированию в приложениях

### Главное правило: писать в stdout

Vector собирает только stdout/stderr. Если приложение пишет только в файл внутри
контейнера — эти логи в Axiom не попадут.

```python
# ✓ правильно — пишет в stdout
logging.StreamHandler(sys.stdout)

# ✗ неправильно для мониторинга — только файл
logging.FileHandler("/app/logs/app.log")
```

### Уровни логирования

Использовать уровни по назначению — это важно для фильтрации мониторов:

| Уровень | Когда использовать |
|---------|-------------------|
| `DEBUG` | детали выполнения, только при разработке |
| `INFO` | нормальные события: старт, обработка запроса, успех |
| `WARNING` | что-то нештатное, но приложение продолжает работу |
| `ERROR` | ошибка, операция не выполнена |
| `CRITICAL` | критический сбой, приложение может не работать |

### Исключения: всегда логировать со стектрейсом

```python
# ✓ правильно — стектрейс включается в одно log-событие
try:
    result = await db.query(...)
except Exception:
    logger.exception("DB query failed")          # автоматически exc_info=True
    # или
    logger.error("DB query failed", exc_info=True)

# ✗ теряем стектрейс
except Exception as e:
    logger.error(f"DB query failed: {e}")         # только строка исключения
```

Почему это важно: `logger.exception()` пишет весь стектрейс одним `write()`,
Docker видит это как **одно событие**. Если трейс уходит сырым в stderr —
каждая строка становится отдельным событием, и мониторы в Axiom могут не сработать.

### Структурированное логирование (опционально, но удобно)

Если приложение пишет JSON в stdout — Vector передаёт поля напрямую в Axiom,
и их можно фильтровать как отдельные колонки.

**Python — через `python-json-logger`:**
```python
from pythonjsonlogger import jsonlogger

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
))
```

Пример события в Axiom:
```json
{"timestamp": "2025-01-15T12:00:00Z", "level": "ERROR", "name": "my-service", "message": "DB unavailable", "user_id": 123}
```

Это позволяет фильтровать в APL: `| where level == "ERROR"` вместо `| where message contains "ERROR"`.

**TypeScript/Node.js — через `pino`:**
```typescript
import pino from "pino";
const logger = pino({ level: "info" });  // пишет JSON в stdout по умолчанию

logger.error({ userId: 123, err }, "DB unavailable");
```

**TypeScript/Node.js — через `winston`:**
```typescript
import winston from "winston";
const logger = winston.createLogger({
    transports: [new winston.transports.Console()],
    format: winston.format.combine(
        winston.format.timestamp(),
        winston.format.json()
    )
});
```

### Если структурированное логирование не используется

Обычный текстовый лог тоже работает — Vector передаёт строку как поле `message`.
Главное соблюдать два правила выше: stdout и `logger.exception()` для исключений.

### Не глотать исключения молча

```python
# ✗ ошибка исчезает, мониторы не сработают
try:
    await process()
except Exception:
    pass

# ✓ хотя бы залогировать
try:
    await process()
except Exception:
    logger.exception("process failed")
    raise  # или обработать
```
