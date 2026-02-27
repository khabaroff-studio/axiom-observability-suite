# Лучшие практики по алертам Axiom

Короткая выжимка по тому, как сделать алерты полезными и малошумными,
с опорой на документацию Axiom и практики SRE.

## Что дает вебхук Axiom

Кастомный webhook настраивается через Go-шаблоны. Ключевые поля:

- Action: Open (triggered) или Closed (resolved)
- Title: название монитора
- Description: описание монитора
- Body: текст (для threshold — описание сработавшего значения)
- Value: значение, вызвавшее алерт (только threshold)
- QueryStartTime / QueryEndTime
- GroupKeys / GroupValues (если в APL есть group by по не-временным полям)
- MatchedEvent (только для Match-мониторов)

Ссылка:
- https://axiom.co/docs/monitor-data/custom-webhook-notifier

## Threshold и Match мониторы

Threshold:
- Хорош для агрегатов (count/rate)
- Вебхук обычно не содержит конкретных строк логов
- Обычно есть только count + окно времени

Match:
- Срабатывает на конкретном событии
- Вебхук содержит MatchedEvent (можно цитировать ошибку)
- Может быть шумным, если не дедуплицировать

## Практики, чтобы алерты были полезными

- Добавлять контекст: host:service
  - Если возможно, группировать по host/service, чтобы иметь GroupKeys/GroupValues
- Показывать 1-3 sample строки ошибки
  - Либо Match-монитор, либо дополнительный запрос в Axiom при триггере
- Резать шум
  - По умолчанию слать только Open/triggered
  - Resolved — только если нужно для процесса
- Делать сообщение коротким и сканируемым
  - Название, host:service, count, окно, короткий sample

## Как это применить в этом стеке

Рекомендуемый формат сообщения:

- Заголовок
- host:service
- count + окно
- top error + 1-2 sample строки
- короткий runbook

Варианты реализации:

1) Оставить Threshold-мониторы и добавить enrichment в alertbot
   - На trigger делать короткий Axiom-запрос и брать sample строки
   - Нужен AXIOM_MGMT_TOKEN и узкое окно времени

2) Добавить Match-мониторы для отдельных сервисов
   - Использовать MatchedEvent из вебхука
   - Threshold оставить для агрегатов

## Общие принципы

- Алерты должны быть actionable и с минимальным шумом
- Лучше алертить симптомы, чем «возможные причины»
- Дедупликация и агрегация обязательны

Ссылки:
- https://sre.google/sre-book/monitoring-distributed-systems/
- https://sre.google/sre-book/practical-alerting/
