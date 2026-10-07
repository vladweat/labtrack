# LabTrack — PostgreSQL Data Model & ERD

> **Исторический документ (до v0.6).** Здесь могут встречаться устаревшие правила передачи всей партии, одностороннего разрешения контейнерных проблем и old API paths. Актуальные и приоритетные требования: [Decisions v0.6](15-decisions-v06.md), [Domain v0.6](16-domain-workflows-v06.md), [Data Model v0.6](17-data-model-v06.md), [API v0.6](18-api-v06.md). Не использовать этот документ в одиночку как нормативный контракт.

**Версия:** 0.5 (базовая ERD и инварианты; полное расширение — [v0.5 delta](12-data-model-v05.md))  
**Статус:** proposed for implementation; согласованные бизнес-решения — [Decision Log](05-design-decisions.md).  
**Scope:** пилотный регион, несколько отправителей, **одна центральная лаборатория**, синтетические данные.

> **Примечание:** ERD ниже показывает базовые таблицы. В v0.5 добавлены `container_issues`, `issue_reason_codes`, `receiving_sessions`, `receipt_revisions`, `sample_external_references` и два новых состояния. Актуальное физическое расширение и DB triggers описаны в [Data Model v0.5](12-data-model-v05.md); не следует считать старую диаграмму полной схемой v0.5.

## 1. Архитектурные решения

- Источник истины для жизненного цикла, составов, приёмки и подтверждений — **PostgreSQL**.
- Единица учёта — **одна физическая пробирка = один `samples`**. `transport_containers` содержат пробирки через `container_items`; `shipments` содержат контейнеры.
- Подтверждение передачи партии/контейнеров (`handoffs`) отделено от фактической поэкземплярной приёмки (`container_items.receipt_result`).
- После `seal` состав контейнеров и пробирок неизменяем в рамках MVP.
- Вся прикладная целостность обеспечивается комбинацией PK/FK/UNIQUE/CHECK, сериализации команд по строке партии и проверок доменных инвариантов. Одних CHECK недостаточно для правил, пересекающих таблицы.
- Храним время в `TIMESTAMPTZ` (UTC по соглашению приложения); IDs — `UUID`, генерируемые приложением; коды QR — независимые высокоэнтропийные непрозрачные значения.
- Избегаем нативных PostgreSQL ENUM в MVP: статусы храним как `TEXT` с `CHECK`. Изменение набора значений требует миграции CHECK и обновления Python `StrEnum`.

## 2. ERD

~~~mermaid
erDiagram
    regions ||--o{ organizations : includes
    regions ||--o{ regional_memberships : scopes
    users ||--o{ regional_memberships : has
    users ||--o{ organization_memberships : has
    organizations ||--o{ organization_memberships : has
    organizations ||--o{ samples : registers
    organizations ||--o{ shipments : sends
    organizations ||--o{ shipments : receives
    users ||--o{ shipments : courier_assignment
    shipments ||--|{ transport_containers : contains
    transport_containers ||--|{ container_items : holds
    samples ||--o| container_items : assigned_once
    shipments ||--o{ handoffs : transfers
    users ||--o{ handoffs : confirms
    shipments ||--o{ audit_events : history
    users ||--o{ audit_events : actor
    users ||--o{ idempotency_records : commands

    regions {
        uuid id PK
        text code UK
        text name
    }
    organizations {
        uuid id PK
        uuid region_id FK
        text code UK
        text name
        text kind
        boolean is_active
    }
    users {
        uuid id PK
        text email_normalized UK
        text password_hash
        boolean is_active
    }
    organization_memberships {
        uuid id PK
        uuid user_id FK
        uuid organization_id FK
        text role
        boolean is_active
    }
    regional_memberships {
        uuid id PK
        uuid user_id FK
        uuid region_id FK
        text role
        boolean is_active
    }
    samples {
        uuid id PK
        text code UK
        uuid origin_organization_id FK
        text material_type
        timestamptz collected_at
        uuid created_by FK
    }
    shipments {
        uuid id PK
        uuid region_id FK
        uuid sender_org_id FK
        uuid destination_org_id FK
        uuid courier_user_id FK
        text code UK
        text status
        int version
        timestamptz expected_delivery_at
    }
    transport_containers {
        uuid id PK
        uuid shipment_id FK
        text code UK
    }
    container_items {
        uuid id PK
        uuid container_id FK
        uuid sample_id FK,UK
        text receipt_result
        text receipt_reason
        uuid receipt_recorded_by FK
        timestamptz receipt_recorded_at
    }
    handoffs {
        uuid id PK
        uuid shipment_id FK
        text kind
        int attempt_no
        text status
        uuid initiated_by FK
        uuid confirmed_by FK
        timestamptz initiated_at
        timestamptz confirmed_at
        timestamptz withdrawn_at
    }
    audit_events {
        uuid id PK
        uuid shipment_id FK
        uuid actor_id FK
        text event_type
        timestamptz occurred_at
        uuid request_id
        jsonb details
    }
    idempotency_records {
        uuid id PK
        uuid actor_id FK
        text operation
        text key
        text request_hash
        int response_status
        jsonb response_body
        timestamptz expires_at
    }
~~~

**Примечание к ERD:** обозначения `|{` показывают обязательность непустых контейнеров **в момент seal**, а не в состоянии `DRAFT`; это условный доменный инвариант, а не автоматический FK. Лаборатория и отправитель — две связи к `organizations`. Семантика `handoffs` описана ниже.

## 3. Таблицы и ключевые поля

Все mutable-таблицы, если не оговорено иное, имеют `created_at TIMESTAMPTZ NOT NULL` и `updated_at TIMESTAMPTZ NOT NULL`. Полные SQL/ORM-модели будут оформлены в миграциях; здесь фиксируются контракты данных.

### 3.1. Справочники, сотрудники и scope

| Таблица | Поля и типы | Ограничения |
|---|---|---|
| `regions` | `id UUID PK`, `code TEXT NOT NULL`, `name TEXT NOT NULL` | `UNIQUE(code)` |
| `organizations` | `id UUID PK`, `region_id UUID NOT NULL`, `code TEXT NOT NULL`, `name TEXT NOT NULL`, `kind TEXT NOT NULL`, `is_active BOOLEAN NOT NULL DEFAULT true` | FK region; `UNIQUE(code)`; `kind IN ('HEALTHCARE_FACILITY','LABORATORY','LOGISTICS')`; `UNIQUE(id,region_id)` для составных FK |
| `users` | `id UUID PK`, `email_normalized TEXT NOT NULL`, `password_hash TEXT NOT NULL`, `is_active BOOLEAN NOT NULL DEFAULT true` | `UNIQUE(email_normalized)`; нормализация email на уровне приложения, пароль только как хэш |
| `organization_memberships` | `id UUID PK`, `user_id UUID NOT NULL`, `organization_id UUID NOT NULL`, `role TEXT NOT NULL`, `is_active BOOLEAN NOT NULL DEFAULT true` | FK user/org; `UNIQUE(user_id,organization_id,role)`; role ∈ `SENDER_OPERATOR`, `COURIER`, `LAB_OPERATOR`, `ORG_ADMIN` |
| `regional_memberships` | `id UUID PK`, `user_id UUID NOT NULL`, `region_id UUID NOT NULL`, `role TEXT NOT NULL`, `is_active BOOLEAN NOT NULL DEFAULT true` | FK user/region; `UNIQUE(user_id,region_id,role)`; в MVP `REGIONAL_ANALYST` |

Один сотрудник может иметь несколько ролей через разные строки membership. Отдельный bootstrap-admin для создания начальных организаций допускается через CLI/seed, без универсальной роли с неограниченным доступом в публичном API. Деактивация учётной записи или membership не удаляет историю.

### 3.2. Пробирки и логистическая структура

| Таблица | Поля и типы | Ограничения |
|---|---|---|
| `samples` | `id UUID PK`, `code TEXT NOT NULL`, `origin_organization_id UUID NOT NULL`, `material_type TEXT NOT NULL`, `collected_at TIMESTAMPTZ NULL`, `created_by UUID NOT NULL` | `UNIQUE(code)`; FK organization/user; без пациента, диагноза или результата анализа |
| `shipments` | `id UUID PK`, `code TEXT NOT NULL`, `region_id UUID NOT NULL`, `sender_org_id UUID NOT NULL`, `destination_org_id UUID NOT NULL`, `courier_user_id UUID NULL`, `status TEXT NOT NULL DEFAULT 'DRAFT'`, `version INTEGER NOT NULL DEFAULT 1`, `expected_delivery_at TIMESTAMPTZ NULL`, `sealed_at TIMESTAMPTZ NULL`, `completed_at TIMESTAMPTZ NULL`, `cancel_reason TEXT NULL` | `UNIQUE(code)`, `CHECK(version > 0)`, `CHECK(sender_org_id <> destination_org_id)`, CHECK status из `9`; составные FK (`sender_org_id,region_id`) и (`destination_org_id,region_id`) к `organizations(id,region_id)` |
| `transport_containers` | `id UUID PK`, `shipment_id UUID NOT NULL`, `code TEXT NOT NULL`, `created_by UUID NOT NULL` | FK shipment/user; `UNIQUE(code)`; удалять/переносить только до seal |
| `container_items` | `id UUID PK`, `container_id UUID NOT NULL`, `sample_id UUID NOT NULL`, `receipt_result TEXT NULL`, `receipt_reason_code_id UUID NULL`, `receipt_comment TEXT NULL`, `receipt_recorded_by UUID NULL`, `receipt_recorded_at TIMESTAMPTZ NULL` | FK container/sample/user; **`UNIQUE(sample_id)`** на весь MVP; CHECK для результата/причины/времени, см. `4` |

`shipments.region_id` намеренно дублирует регион: два составных FK исключают смешение организаций разных регионов даже при последующем расширении продукта. Правило `samples.origin_organization_id = shipments.sender_org_id` нельзя выразить простым CHECK через существующие таблицы — проверяем в транзакционном доменном сервисе.

Идентификационные поля `samples.code` и `samples.origin_organization_id` считаются неизменяемыми: в MVP нет команды сменить происхождение пробирки.

**Важный trade-off:** `UNIQUE(container_items.sample_id)` запрещает повторно использовать зарегистрированную пробирку даже после отмены партии. Это соответствует текущему MVP без повторной отправки. Для полноценной эксплуатации нужна отдельная политика повторной комплектации и архивных манифестов — **не меняем ограничение молча**.

### 3.3. Двусторонние передачи и события

| Таблица | Поля и типы | Ограничения |
|---|---|---|
| `handoffs` | `id UUID PK`, `shipment_id UUID NOT NULL`, `kind TEXT NOT NULL`, `attempt_no INTEGER NOT NULL`, `status TEXT NOT NULL`, `initiated_by UUID NOT NULL`, `initiated_at TIMESTAMPTZ NOT NULL`, `confirmed_by UUID NULL`, `confirmed_at TIMESTAMPTZ NULL`, `withdrawn_at TIMESTAMPTZ NULL` | `kind IN ('PICKUP','DELIVERY')`; `status IN ('PENDING','CONFIRMED','WITHDRAWN')`; `UNIQUE(shipment_id,kind,attempt_no)`; `CHECK(attempt_no>0)` и согласованность времён/actor |
| `audit_events` | `id UUID PK`, `shipment_id UUID NULL`, `actor_id UUID NULL`, `event_type TEXT NOT NULL`, `entity_type TEXT NOT NULL`, `entity_id UUID NOT NULL`, `occurred_at TIMESTAMPTZ NOT NULL`, `request_id UUID NOT NULL`, `details JSONB NOT NULL DEFAULT '{}'::jsonb` | FK shipment/user; append-only, без персональных данных пациентов; системные события допускают `actor_id NULL` |
| `idempotency_records` | `id UUID PK`, `actor_id UUID NOT NULL`, `operation TEXT NOT NULL`, `key TEXT NOT NULL`, `request_hash TEXT NOT NULL`, `response_status INTEGER NULL`, `response_body JSONB NULL`, `created_at TIMESTAMPTZ NOT NULL`, `expires_at TIMESTAMPTZ NOT NULL` | FK actor; `UNIQUE(actor_id,operation,key)`; response-поля временно NULL до выполнения команды, оба заполнены до commit; CHECK требует их согласованной NULL/non-NULL парности |

`ShipmentEvent` в прежней концептуальной модели — **представление по `audit_events`** с непустым `shipment_id`, не отдельная таблица на данном этапе. Это не требует дублировать события.

`idempotency_records` создаётся в той же транзакции, что и команда: response-поля временно NULL; перед успешным commit оба заполнены. В случае rollback запись не сохраняется. CHECK: `(response_status IS NULL) = (response_body IS NULL)`.

`handoffs` допускает новый `attempt_no` после отзыва предыдущей попытки. Частичный уникальный индекс гарантирует не больше одной неотозванной попытки на `(shipment_id,kind)`. Подтверждённая запись остаётся неизменной; событие об обеих действиях сохраняется отдельно. Для `PICKUP` initiator — Sender, confirmer — назначенный Courier; для `DELIVERY` initiator — этот Courier, confirmer — Lab Operator адресной лаборатории.

## 4. CHECK-ограничения, которые нельзя оставлять только в Python

**`container_items` (v0.5):**

```sql
CHECK (
    (receipt_result IS NULL AND receipt_reason_code_id IS NULL AND
     receipt_comment IS NULL AND receipt_recorded_at IS NULL AND receipt_recorded_by IS NULL)
 OR (receipt_result = 'ACCEPTED' AND receipt_reason_code_id IS NULL AND
     receipt_comment IS NULL AND receipt_recorded_at IS NOT NULL AND receipt_recorded_by IS NOT NULL)
 OR (receipt_result IN ('MISSING','REJECTED') AND receipt_reason_code_id IS NOT NULL
     AND receipt_recorded_at IS NOT NULL AND receipt_recorded_by IS NOT NULL)
);
```

Причина должна соответствовать `scope=SAMPLE,kind=receipt_result` — через composite FK или защищённую проверку БД + service validation. Все изменения пробирок во время receiving порождают append-only `receipt_revisions`.

**`handoffs`:** разрешённые комбинации состояния:
- `PENDING`: только initiator и `initiated_at`, confirmer/withdrawn_at отсутствуют;
- `CONFIRMED`: `confirmed_by` и `confirmed_at` обязательны, `confirmed_by <> initiated_by`;
- `WITHDRAWN`: `withdrawn_at` обязателен, `confirmed_by`/`confirmed_at` отсутствуют.

Частичный индекс:

~~~sql
CREATE UNIQUE INDEX uq_handoffs_not_withdrawn
    ON handoffs (shipment_id, kind)
    WHERE status IN ('PENDING', 'CONFIRMED');
~~~

**`shipments`:** `status` ограничен `DRAFT`, `READY_FOR_PICKUP`, `PICKUP_PENDING`, `IN_TRANSIT`, `DELIVERY_PENDING`, `DELIVERY_BLOCKED`, `RECEIVING`, `COMPLETED`, `COMPLETED_WITH_ISSUES`, `CLOSED_UNDELIVERED`, `CANCELLED`. Синхронизацию `sealed_at`/`completed_at` со статусом проверяет сервис; простой CHECK не способен проверить состав партии и результаты.

## 5. Индексы (начальный набор)

~~~sql
CREATE INDEX ix_shipments_sender_status_created
    ON shipments (sender_org_id, status, created_at DESC);
CREATE INDEX ix_shipments_destination_status_created
    ON shipments (destination_org_id, status, created_at DESC);
CREATE INDEX ix_shipments_courier_status_created
    ON shipments (courier_user_id, status, created_at DESC);
CREATE INDEX ix_shipments_region_status
    ON shipments (region_id, status);
CREATE INDEX ix_shipments_active_deadline
    ON shipments (expected_delivery_at)
    WHERE status IN (
        'READY_FOR_PICKUP', 'PICKUP_PENDING',
        'IN_TRANSIT', 'DELIVERY_PENDING', 'RECEIVING'
    );
CREATE INDEX ix_transport_containers_shipment
    ON transport_containers (shipment_id);
CREATE INDEX ix_container_items_container
    ON container_items (container_id);
CREATE INDEX ix_audit_events_shipment_time
    ON audit_events (shipment_id, occurred_at, id);
CREATE INDEX ix_memberships_org_role_active
    ON organization_memberships (organization_id, role)
    WHERE is_active = true;
CREATE INDEX ix_idempotency_expires_at
    ON idempotency_records (expires_at);
~~~

Unique-ограничения сами создают необходимые уникальные индексы: не дублируем `UNIQUE(sample_id)` или `UNIQUE(code)`. Индексы предварительные: уточнить через `EXPLAIN (ANALYZE, BUFFERS)` после появления реалистичных синтетических данных.

## 6. Referential integrity и правила удаления

- Внешние ключи на партии, контейнеры, пробирки, подтверждения и историю — `ON DELETE RESTRICT/NO ACTION`; физическое удаление бизнес-данных в MVP не предусмотрено.
- Пользователей и организации деактивируем, а не удаляем. Исторические `initiated_by` и `confirmed_by` сохраняются.
- `container_items` не должны физически удаляться после seal. В `DRAFT` удаление допустимо через доменную команду.
- `audit_events` доступны только на вставку для runtime-роли БД; UPDATE/DELETE запрещены через права и тесты. Для более сильной защиты возможны триггеры и отдельная роль на миграции.
- Окончательная модель ретенции данных и аудита требует отдельной security/legal политики; бессрочное хранение не следует считать принятой политикой.

## 7. Что обеспечивает БД, а что — транзакционный сервис

| Инвариант | Механизм |
|---|---|
| Уникальность кодов пробирок/контейнеров/партий | `UNIQUE(code)` |
| Один `Sample` только в одном контейнере | `UNIQUE(container_items.sample_id)` |
| Контейнер принадлежит существующей партии | FK |
| Отправитель и лаборатория одной области | Составные FK по `region_id` |
| Образец зарегистрирован организацией-отправителем | Проверка сервиса под lock партии |
| Все контейнеры непусты на seal | Проверка агрегата в транзакции |
| Двусторонняя передача разными людьми с нужными ролями | Сервисная проверка auth/scope; CHECK для `confirmed_by != initiated_by` |
| Ровно один неотозванный handoff каждого вида | Partial UNIQUE index |
| Каждая пробирка явно обработана до финализации | Проверка под lock партии и запрет новых изменений состава |
| Неизменяемый состав после seal | Сервисный write path под lock партии; DB permissions; защитный trigger — отдельное решение |
| Статус, handoff, приемка и audit не расходятся | **Одна транзакция PostgreSQL** |
| Запрещённые переходы статусов | State machine в domain service; `CHECK` гарантирует только допустимый набор строк |
| Идемпотентные повторные запросы | `UNIQUE(actor_id,operation,key)` + транзакционная проверка payload hash и результата |

**Решение v0.5:** PostgreSQL triggers для запрета изменения sealed manifest обязательны уже в MVP вместе с сервисными проверками. Прямой SQL UPDATE/DELETE/INSERT после seal должен быть отвергнут; trigger и seal сериализуются под родительской блокировкой Shipment. См. [v0.5 delta](12-data-model-v05.md).

## 8. Объём данных и миграции

- Для демо: ~10 организаций, ~100 пользователей, до 10 000 пробирок; цели из PRD — не результаты нагрузочных тестов.
- Миграции через Alembic: сначала справочники и пользователи, затем `samples`/`shipments`, контейнеры/позиции, handoffs, audit, idempotency и индексы.
- Все FK, unique и check задаются в миграциях. Использовать `nullable=False` на обязательных полях, а не полагаться на Pydantic.
- В seed — **только синтетические данные**, без ФИО пациентов, медицинских заключений и реальных исследований.
- API и ORM будут строиться на этой схеме, но сам документ **не означает, что миграции уже созданы или применены**.

## 9. Открытые решения перед SQL migration

1. Отправитель назначает Courier в `DRAFT` (предлагаемый MVP default), либо добавляется отдельный Dispatcher.
2. Перечень причин `MISSING`/`REJECTED`: свободный текст в P0 с обязательной причиной либо отдельные справочники.
3. Нужен ли `manifest_hash` для подтверждения состава при handoff? В текущей схеме не обязателен: source of truth — зафиксированные контейнеры и позиции.
4. Нужен ли DB trigger для физической неизменяемости манифеста после seal уже в MVP? Рекомендуется до production deployment.
5. Пересмотр ограничения `UNIQUE(sample_id)` при отменённой партии — только отдельным решением с моделью повторных перевозок.

См. [Workflows, Transactions & Concurrency](07-transactions-and-concurrency.md) для правил сериализации команд и acceptance tests.

## 10. Расширение модели v0.5

Базовая ERD остаётся как учебно-архитектурный фундамент, но не является полной актуальной схемой. [Data Model Delta](12-data-model-v05.md) определяет новые сущности и ограничения: контейнерные инциденты, коды причин, версии приёмки и её reopening, opaque LIS references, замороженную `manifest_revision`, региональные роли и trigger-guards. [Offline Protocol](11-offline-sync-protocol.md) использует те же транзакционные команды.
