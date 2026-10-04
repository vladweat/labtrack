# LabTrack — Architecture Overview

**Версия:** 0.4 · **Статус:** проект архитектуры для реализации MVP (код ещё не реализован).  
**Согласованный product scope:** [Vision](01-product-vision.md), [Decision Log](05-design-decisions.md).  
**Спецификации:** [Data Model](06-data-model.md), [Transactions](07-transactions-and-concurrency.md), [REST API](09-api-specification.md).

## 1. Контекст и цели

LabTrack — внутренний региональный сервис прослеживаемости лабораторных пробирок, объединённых в маркированные транспортные контейнеры и партии. Участники: учреждение-отправитель, назначенный курьер, лаборатория назначения и региональный аналитик.

Основные требования: индивидуальная прослеживаемость пробирки; **двустороннее подтверждение** выдачи и доставки контейнеров; раздельная поэкземплярная приёмка; только два исключения `MISSING` и `REJECTED`; аудит, организационная изоляция, согласованность при конкурентных запросах.

Приложение **не** заменяет ЛИС/МИС, не хранит данные реальных пациентов, не выполняет исследования, не определяет клиническую пригодность, не заявляет GPS-отслеживание или юридически значимую электронную подпись. В демонстрации используются только синтетические данные.

## 2. Выбранная архитектура

**Modular monolith + PostgreSQL как system of record.** Один FastAPI process для REST API, отдельно Celery worker/beat для необязательных фоновых задач, Redis как broker/cache. Внешние API и AI не обязательны для MVP.

~~~mermaid
flowchart TB
    UI["Web UI / API clients"] --> N["Nginx / reverse proxy"]
    N --> A["FastAPI application"]
    subgraph M["Modular monolith"]
      A --> ID["identity / access"]
      A --> ORG["organizations"]
      A --> SAMPLE["samples"]
      A --> SHIP["shipments / containers / handoffs / receiving"]
      A --> ANA["analytics"]
      A --> AUD["audit / idempotency"]
    end
    ID --> PG[("PostgreSQL")]
    ORG --> PG
    SAMPLE --> PG
    SHIP --> PG
    ANA --> PG
    AUD --> PG
    A -. "publish after commit, best effort" .-> R[("Redis broker")]
    R --> W["Celery worker"]
    W --> PG
    B["Celery beat"] --> R
~~~

**Почему так:** основная сложность — атомарные изменения одного Shipment, проверка организационного доступа и прослеживаемость. Распределённые транзакции между микросервисами усложнили бы корректность. Отдельный worker оправдан уведомлениями и периодическим обнаружением просрочек, но ни Redis, ни Celery не становятся источником истины по состоянию партии.

## 3. Модули и ownership

| Модуль | Владеет | Основные responsibilities | Зависимости |
|---|---|---|---|
| `identity` | `users`, auth, credential handling | Login, current actor, active user | PostgreSQL |
| `organizations` | `regions`, `organizations`, memberships | Organizational scope, role grants, active memberships | identity |
| `samples` | `samples` | Регистрация одной пробирки, уникальные непрозрачные метки, чтение в своем scope | organizations |
| `shipments` | `shipments`, `transport_containers`, `container_items`, `handoffs` | Редактирование манифеста, seal, два handoff, receiving, финализация | samples, access, audit |
| `audit` | `audit_events` | Append-only журнал бизнес-событий и access-логика для чтения | identity |
| `idempotency` | `idempotency_records` | Атомарное резервирование ключа и повтор сохранённого ответа | identity |
| `analytics` | агрегации SQL, не дублирующие таблицы | Сводки по региону, организации и статусу | shipments, access |
| `notifications` | необязательные задачи/уведомления | Best-effort уведомления, контроль дедлайнов | Celery/Redis/PostgreSQL |

`Shipment` — транзакционный агрегат. `containers`, `handoffs` и `receiving` на MVP остаются частями модуля `shipments`, а не отдельными независимыми микросервисами. Код модуля `analytics` читает данные через query layer и не изменяет состояния партий.

## 4. Слои и рекомендуемая структура Python

~~~text
backend/
├── app/
│   ├── main.py
│   ├── core/               # config, logging, error mapping, security primitives
│   ├── db/                 # engine, AsyncSession factory, migrations boundary
│   ├── api/                # versioned routers, DTO, dependencies
│   │   └── v1/
│   ├── modules/
│   │   ├── identity/
│   │   ├── organizations/
│   │   ├── samples/
│   │   ├── shipments/
│   │   │   ├── domain/     # enums, transitions, invariant validation
│   │   │   ├── application/ # use cases, transaction orchestration
│   │   │   ├── persistence/ # SQLAlchemy entities and queries
│   │   │   └── schemas/     # public Pydantic input/output contracts
│   │   ├── audit/
│   │   ├── idempotency/
│   │   ├── analytics/
│   │   └── notifications/
│   └── workers/            # Celery app, task entrypoints, periodic jobs
├── alembic/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── pyproject.toml
~~~

Это **пример организации**, не требование создавать четыре слоя в каждом небольшом модуле. FastAPI router: parse/validate → current Actor → use case → response. Use case: policy + транзакция + агрегат + audit. SQLAlchemy models не становятся публичными response schemas. Нет самостоятельного generic repository framework, service locator или event bus.

Предлагаемый стек: Python с type hints, FastAPI, Pydantic, SQLAlchemy 2.x async, Alembic, PostgreSQL, Redis, Celery, pytest, Docker Compose и Nginx. Фиксированные версии зависимостей выбираются при bootstrap проекта; здесь не утверждаются.

## 5. AuthN / AuthZ / organizational scope

- **Authentication:** email + пароль, защищённый Argon2id (или сопоставимым современным password hasher), Bearer access token с коротким сроком жизни; сервер независимо проверяет активность пользователя. Refresh tokens/SSO — P1. В MVP допускается повторный login по истечении access token.
- **Principal / Actor:** `user_id`, активные organization memberships (с ролями), региональные роли; брать из проверенной учётной записи/БД, не из произвольных заголовков клиента.
- **Subject scope:** Sender читает только свои отправления и образцы; Lab — только адресованные лаборатории; Courier — только назначенные партии; Org Admin управляет только membership своей организации; Regional Analyst получает агрегаты по своему региону, не чтение отдельных пробирок.
- **RBAC + ownership checks** в query/use case. Случайные UUID и QR-коды **не** являются средствами авторизации.
- Для Courier в пилоте допустима активная роль `COURIER` в логистической организации **того же региона**, что и партия; назначение подтверждает Sender, пока не добавлен dispatcher. При чтении курьер фильтруется по `courier_user_id` и членству в регионе.
- Identity provider, password reset, многофакторная аутентификация и юридически значимые подписи не входят в MVP. Для эксплуатации с реальными медицинскими данными потребуется самостоятельное security/compliance-проектирование.

**Security negative tests:** IDOR на чужие Sample/Shipment/Container/Handoff, самоподтверждение двух сторон пользователем с несколькими ролями, деактивированный Courier, cross-region assignment, доступ Regional Analyst к поэкземплярным данным.

## 6. Транзакции, concurrency и consistency

**Базовая стратегия:** один PostgreSQL `AsyncSession`/transaction per write use case, `READ COMMITTED`, `SELECT ... FOR UPDATE` на родительском `shipments`. Все операции изменения дочерних контейнеров, позиций, handoff и приёмки сначала блокируют строку партии. Согласованный порядок блокировок защищает от deadlocks.

- `seal` фиксирует манифест 1..N контейнеров, каждый содержит ≥1 пробирку; состав и распределение **неизменяемы** после seal.
- `confirm_release` → `PICKUP_PENDING`; только подтверждение **другого** назначенного Courier → `IN_TRANSIT`.
- `confirm_delivery` → `DELIVERY_PENDING`; только подтверждение **другого** Lab Operator в лаборатории назначения → `RECEIVING`.
- Подтверждение передачи контейнера **никогда** не помечает его пробирки как `ACCEPTED`.
- В `RECEIVING` каждая `container_item` получает `ACCEPTED`, `MISSING` или `REJECTED`. Финализация проверяет отсутствие NULL; отклонения требуют причину.
- Каждое изменение статуса, попытка передачи, результат пробирки и audit event сохраняются **атомарно**; при ошибке audit — rollback всей команды.
- `UNIQUE(container_items.sample_id)` предотвращает двойную привязку пробирки даже между разными Shipment (которые блокируются независимо).

**Idempotency:** для критичных write commands хранить `(actor_id,operation,key,request_hash,response)` в PostgreSQL в одной транзакции с изменениями и аудитом. Повтор с тем же payload возвращает сохранённый ответ, с другим → `409 IDEMPOTENCY_KEY_REUSED`. Примерный TTL 24 часа (предлагаемая настройка, не продемонстрированная гарантия). Post-commit уведомления в P0 best-effort; потеря публикации возможна и должна быть явно обозначена.

**Граница гарантии неизменности:** сервисный слой + права БД. Защитный PostgreSQL trigger для запрета модификации sealed manifest рекомендуется добавить до production deployment; до этого не утверждать, что любое прямое SQL-изменение физически невозможно.

Подробный порядок и тесты: [Transactions & Concurrency](07-transactions-and-concurrency.md).

## 7. Наблюдаемость и операции

- Structured JSON logs с `request_id` и безопасными идентификаторами; не логировать access tokens, пароли, пациентов, запросы с секретами.
- `/health/live` не требует БД; `/health/ready` проверяет соединение с PostgreSQL. Отказ Redis не должен превращать успешно выполненную бизнес-команду в ошибку, если задача уведомления best-effort.
- Метрики для будущих графиков: throughput команд, доля `409`, длительность транзакций, lock wait p95, число ожидающих handoff, доля партий с `MISSING`/`REJECTED`, количество просроченных партий, фоновые retries.
- Celery tasks обязаны быть идемпотентны; бизнес-события читаются из PostgreSQL. Уведомление не меняет статус. Периодические scans учитывают неизменное `expected_delivery_at`.
- Alembic миграции на запуске окружения применяются отдельной командой, **не** конкурентно каждым worker.
- Docker Compose для разработки, healthchecks, non-root containers, конфигурация через environment/secrets, HTTPS на reverse proxy в production, резервное копирование PostgreSQL и restore-тесты — до фактического production.
- Защита API: CORS allowlist, rate limiting auth, request size limits, timeouts, dependency pinning и secret scanning. Эти меры являются backlog работ, не уже реализованными controls.

## 8. Тестовая пирамида и порядок реализации

1. **Unit:** переходы `Shipment`, инварианты, RBAC, формирование манифеста и сверка множества контейнеров.
2. **Integration на реальном PostgreSQL:** FK/UNIQUE/CHECK, миграции up/down на тестовой базе, запрет IDOR, атомарность состояния/audit/idempotency.
3. **Concurrency:** два Courier, два изменения одной позиции, `seal` vs edit, уникальность sample в разных партиях, два одновременных `complete_receiving`.
4. **E2E:** 10 пробирок → два контейнера → четыре подтверждения двух handoff → 8 ACCEPTED, 1 MISSING, 1 REJECTED → COMPLETED_WITH_ISSUES; чтение региональной сводки.

Реализацию начинаем с skeleton/config/migrations, затем auth/access, samples/containers, handoff, receiving, dashboard. Не вводим микросервисы или AI до успешного сквозного MVP.

## 9. Предлагаемые default-решения, требующие product approval

В следующих документах эти пункты являются **рабочими допущениями**, а не заменой принятых DEC-001–003:

- Sender Operator назначает активного Courier своего региона **только пока Shipment DRAFT**; dispatcher не требуется.
- В `RECEIVING` Lab Operator может исправить неверную отметку с обязательной записью в audit; после финализации исправление запрещено.
- `MISSING`/`REJECTED` требуют непустой текстовой причины (до согласования справочника кодов).
- QR содержит только непрозрачный code, не пациентские данные; ожидаемые контейнеры при каждом handoff сверяются по полному набору их кодов, без `manifest_hash`.
- В MVP только синхронные REST write use cases и best-effort фоновые уведомления, без transactional outbox.

**Важно:** ни один из этих default не требует менять три ранее согласованных базовых решения.
