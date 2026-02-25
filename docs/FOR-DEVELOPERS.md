# Руководство разработчика: интеграция с мониторингом

Этот документ описывает, что нужно сделать при разработке сервиса (Telegram-бота, API, воркера),
чтобы он полноценно работал с системой мониторинга.

---

## Как устроен мониторинг

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

### ✅ 1. Писать логи в stdout

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

### ✅ 2. Логировать исключения со стектрейсом

Монитор ловит события, содержащие `ERROR` или `Traceback`. Важно, чтобы стектрейс
попал в одно событие, а не размазался по строкам.

```python
# ✓ правильно — весь стектрейс в одном log-событии
try:
    await do_something()
except Exception:
    logger.exception("Failed to do something")  # автоматически exc_info=True

# ✗ неправильно — стектрейс теряется
except Exception as e:
    logger.error(f"Failed: {e}")
```

Не глотать исключения молча:
```python
# ✗ мониторы не сработают — ошибка исчезает
except Exception:
    pass
```

### ✅ 3. Добавить HEALTHCHECK в docker-compose.yml

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

### ✅ 4. Сообщить имя контейнера DevOps

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
