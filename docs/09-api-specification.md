# LabTrack — REST API Specification

> **Исторический документ (до v0.6).** Здесь могут встречаться устаревшие правила передачи всей партии, одностороннего разрешения контейнерных проблем и old API paths. Актуальные и приоритетные требования: [Decisions v0.6](15-decisions-v06.md), [Domain v0.6](16-domain-workflows-v06.md), [Data Model v0.6](17-data-model-v06.md), [API v0.6](18-api-v06.md). Не использовать этот документ в одиночку как нормативный контракт.

**Версия документа:** 0.5 (baseline v0.4 + delta) · **Machine-readable draft:** [OpenAPI 3.1 JSON v0.5](../openapi/labtrack-v1.json) · **API base path:** `/api/v1` · **Статус:** draft contract, **не реализован**.  
**Обязательные исходные требования:** [MVP Requirements](02-mvp-requirements.md), [Domain](03-domain-and-workflows.md), [Data Model](06-data-model.md), [Transactions](07-transactions-and-concurrency.md), [Architecture](08-architecture-overview.md).

> **Актуальные изменения v0.5:** [API Delta](13-api-v05.md) имеет приоритет перед устаревшими payload-примерами ниже: контейнерные issues, LAB_MANAGER reopening, offline Courier sync, `reason_code`, новые Shipment статусы и роли. Машинно-читаемый OpenAPI v0.5 содержит актуальные схемы.

## 1. Принципы контракта

- JSON UTF-8, `Content-Type: application/json`; идентификаторы `UUID`, метки (`code`) — отдельные непрозрачные строки.
- Время в ответах — RFC 3339 / ISO 8601 с UTC: `2026-11-01T08:00:00Z`. Запросы на `expected_delivery_at` допускают offset, нормализуются в UTC.
- Все бизнес-endpoints требуют `Authorization: Bearer <access_token>`, кроме `POST /auth/login` и `/health/*`.
- Actor и организационные роли извлекаются из проверенного server-side контекста; клиент не передаёт `actor_id` в write commands.
- Обязателен `X-Request-ID`? **Нет**: сервер генерирует UUID, если заголовок отсутствует, возвращает `X-Request-ID` и фиксирует его в audit. Клиентский заголовок должен проходить валидацию UUID, иначе сервер назначает собственный.
- Все списки: `limit` (default 20, 1..100), `offset` (default 0, max 10000); сортировка `created_at DESC, id DESC`, ответ `{items,total,limit,offset}`. Для крупного production-потока следует перейти на cursor pagination.
- Поля, отсутствующие в JSON request, не изменяют существующее состояние. Непредусмотренные поля отвергаются (`extra="forbid"`). Строковые причины: trim, длина 1..500; пустая/whitespace-only причина не допускается.
- Ни один публичный ответ или QR не содержит сведения о пациенте. Не пересылать в API персональные данные, диагнозы или результаты исследований.
- Успешные write endpoints синхронно фиксируют транзакцию до ответа; фоновые уведомления не влияют на получаемый статус.
- Непустые входные массивы меток контейнеров при handoff должны точно совпадать по множеству с sealed manifest; дубликаты запрещены.

## 2. Authentication и permissions

### Auth

| Метод | Path | Request | Response |
|---|---|---|---|
| `POST` | `/auth/login` | `{email,password}` | `200 {access_token,token_type:"bearer",expires_in:900}` |
| `GET` | `/auth/me` | — | `200 ActorView` |

**Предложение для MVP:** short-lived access token 15 минут, re-login после истечения, refresh/SSO вне MVP. Для `login` единое сообщение об ошибке логина; rate limit, hashing и активность account проверяет identity module. Точный auth mechanism/TTL будут конфигурационными параметрами и нуждаются в реализации/тестировании.

**RBAC + resource scope:** Sender — свои исходящие; Courier — назначенные ему в рамках активного membership региона; Lab — входящие своей лаборатории; Org Admin — только membership своей организации; Regional Analyst — только агрегаты своего региона. В случае отсутствия scope ответ `404` (скрыть существование объекта); для заведомо доступного объекта, но запрещённой команды — `403`.

### Membership / reference endpoints

| Метод | Path | Кто | Ответ |
|---|---|---|---|
| `GET` | `/organizations` | Authenticated, scoped | `200 Paged<Organization>`, фильтры `region_id` и `kind` |
| `GET` | `/organizations/{org_id}/memberships` | Org Admin этой организации | `200 Paged<Membership>` |
| `POST` | `/organizations/{org_id}/memberships` | Org Admin | `201 Membership`, body `{user_id,role}` |
| `PATCH` | `/organizations/{org_id}/memberships/{membership_id}` | Org Admin | `200 Membership`, body `{is_active:boolean}` |
| `GET` | `/couriers?region_id=...` | Sender Operator региона | `200 Paged<CourierOption>` — активные назначаемые Courier |

Создание региона и организаций выполняется bootstrap CLI/seed; публичного admin API для этого в P0 нет. Создание первичного User/приглашение сотрудника вне P0; `POST memberships` требует существующего пользователя (bootstrap). В MVP `role` membership неизменяем: менять роль через деактивацию существующего membership + создание нового.

## 3. Samples: каждая пробирка — отдельная сущность

| Метод | Path | Кто | Response |
|---|---|---|---|
| `POST` | `/samples` | Sender своего отправителя | `201 Sample` |
| `GET` | `/samples` | Sender своего scope (Lab — только по доступным партиям, через manifest) | `200 Paged<Sample>` |
| `GET` | `/samples/{sample_id}` | Sender исходной организации, назначенный Courier или адресная Lab в контексте доступной партии | `200 Sample` (scoped) |

**CreateSampleRequest:**
~~~json
{
  "origin_organization_id": "11111111-1111-4111-8111-111111111111",
  "material_type": "BLOOD",
  "collected_at": "2026-11-01T08:00:00Z"
}
~~~

- `material_type`: рабочий справочник `BLOOD | URINE | OTHER`; этот выбор **не** несёт клинических нормативов.
- `collected_at` optional/nullable, нет данных пациента.
- Ответ `201 Sample`: `id,code,origin_organization_id,material_type,collected_at,created_at`. Код генерирует сервер (opaque, random, не UUID пациента).
- `POST /samples` требует idempotency, чтобы повтор при сетевом сбое не создал новую пробирку.

## 4. Shipments и контейнерный манифест

| Метод | Path | Кто | Request / Response |
|---|---|---|---|
| `POST` | `/shipments` | Sender | `201 Shipment`, body `CreateShipmentRequest` |
| `GET` | `/shipments` | Sender/Courier/Lab (scoped) | `200 Paged<Shipment>`; фильтры `status,sender_org_id,destination_org_id` применяются **после** scope |
| `GET` | `/shipments/{shipment_id}` | Scoped participant | `200 Shipment` |
| `GET` | `/shipments/{shipment_id}/manifest` | Scoped participant | `200 ShipmentManifest` — контейнеры и пробирки с текущими результатами |
| `PATCH` | `/shipments/{shipment_id}/courier` | Sender | `200 Shipment`, body `{courier_user_id:UUID}`, только `DRAFT` |
| `POST` | `/shipments/{shipment_id}/containers` | Sender | `201 TransportContainer`, только `DRAFT`; request `{}` |
| `DELETE` | `/shipments/{shipment_id}/containers/{container_id}` | Sender | `204`, только `DRAFT` и контейнер пуст |
| `POST` | `/shipments/{shipment_id}/containers/{container_id}/items` | Sender | `201 ContainerItem`, body `{sample_id:UUID}` |
| `DELETE` | `/shipments/{shipment_id}/containers/{container_id}/items/{item_id}` | Sender | `204`, только `DRAFT` |
| `POST` | `/shipments/{shipment_id}/seal` | Sender | `200 Shipment`, body `{}` |
| `POST` | `/shipments/{shipment_id}/cancel` | Sender | `200 Shipment`, body `{reason:string}` |

**CreateShipmentRequest:**
~~~json
{
  "sender_org_id": "11111111-1111-4111-8111-111111111111",
  "destination_org_id": "22222222-2222-4222-8222-222222222222",
  "expected_delivery_at": "2026-11-01T12:00:00Z"
}
~~~

`region_id` определяется сервером из организаций; отправитель и назначение должны быть активны, иметь один регион и быть разными. Destination обязана иметь `kind=LABORATORY`. `code`, `status=DRAFT`, `version=1` генерирует сервер. Курьер назначается отдельной командой до seal.

**Create container:** сервер назначает `id` и `code`. В ответе `TransportContainer {id,code,shipment_id,created_at}`. Можно создать пустой контейнер в `DRAFT`, но `seal` потребует каждый контейнер непустым. Удалить непустой контейнер — `409 CONTAINER_NOT_EMPTY`, сначала удалить его items.

**Create item:** `ContainerItem {id,container_id,sample_id,receipt_result:null,receipt_reason_code:null,receipt_comment:null}`. Пробирка из другой организации → `404` (не раскрывать), уже добавленная в любой контейнер/партию → `409 SAMPLE_ALREADY_ASSIGNED`. При любой конкурентной операции защита на `UNIQUE(container_items.sample_id)`.

**ShipmentManifest:** список `containers: [{id,code,items:[{id,sample_id,sample_code,material_type,receipt_result,receipt_reason}]}]` плюс `shipment_id,status,sealed_at`; для подтверждений handoff клиент отправляет **только массив кодов контейнеров**, но не статусы пробирок.

**Seal:** проверяет ≥1 контейнер, каждый с ≥1 пробиркой, наличие активного Courier и соответствие организаций; переводит `DRAFT → READY_FOR_PICKUP`, фиксирует `sealed_at`. С этого момента нельзя менять распределение/состав контейнеров. `version` увеличивается на успешную мутацию агрегата.

## 5. Handoffs: два подтверждения на каждом этапе

Все четыре команды подтверждения принимают **HandoffConfirmRequest**:
~~~json
{
  "container_codes": ["LT-C-EXAMPLE-01", "LT-C-EXAMPLE-02"]
}
~~~

Пример кодов иллюстративный: в реализации сервер выдаёт криптографически случайные opaque values. Список должен совпадать с фактическими кодами всех контейнеров sealed manifest, без дубликатов. Нельзя отправлять подмножество.

| Метод | Path | Роль | State transition | Response |
|---|---|---|---|---|
| `POST` | `/shipments/{id}/handoffs/pickup/confirm-release` | Sender | `READY_FOR_PICKUP → PICKUP_PENDING` | `200 HandoffActionResult` |
| `POST` | `/shipments/{id}/handoffs/pickup/confirm-pickup` | Assigned Courier | `PICKUP_PENDING → IN_TRANSIT` | `200 HandoffActionResult` |
| `POST` | `/shipments/{id}/handoffs/delivery/confirm-delivery` | Assigned Courier | `IN_TRANSIT → DELIVERY_PENDING` | `200 HandoffActionResult` |
| `POST` | `/shipments/{id}/handoffs/delivery/confirm-receipt` | Destination Lab Operator | `DELIVERY_PENDING → RECEIVING` | `200 HandoffActionResult` |
| `POST` | `/shipments/{id}/handoffs/pickup/withdraw` | Original Sender actor | `PICKUP_PENDING → READY_FOR_PICKUP` | `200 HandoffActionResult` |
| `POST` | `/shipments/{id}/handoffs/delivery/withdraw` | Original Courier actor | `DELIVERY_PENDING → IN_TRANSIT` | `200 HandoffActionResult` |

**WithdrawRequest:** `{reason:string}`; причина обязательна для операционной прозрачности. Отзыв до второго подтверждения переводит `handoff` в `WITHDRAWN`; следующая попытка создаёт новый `attempt_no`. Нет нового типа инцидента.

**HandoffActionResult:**
~~~json
{
  "shipment_id": "33333333-3333-4333-8333-333333333333",
  "shipment_status": "PICKUP_PENDING",
  "version": 5,
  "handoff": {
    "id": "44444444-4444-4444-8444-444444444444",
    "kind": "PICKUP",
    "attempt_no": 1,
    "status": "PENDING",
    "initiated_by": "55555555-5555-4555-8555-555555555555",
    "initiated_at": "2026-11-01T09:00:00Z",
    "confirmed_by": null,
    "confirmed_at": null
  }
}
~~~

**Ограничения:**
- Одна сторона **не может подтверждать действие за вторую**. `confirmed_by != initiated_by` вне зависимости от набора ролей.
- Активный Courier обязан совпадать с `shipments.courier_user_id` и иметь допустимую роль в регионе партии.
- Lab Operator обязан действовать от имени `destination_org_id` и иметь соответствующую роль.
- Сверка контейнеров не означает принятие пробирок. Когда партия получает `RECEIVING`, `container_items.receipt_result` ещё NULL.
- При отсутствии контейнера **не подтверждаем handoff** и не имитируем частичную успешную передачу. Отказ в приёмке отдельной пробирки оформляется позже, на стадии receiving.
- Сведения actor/time формирует сервер из authentication context и DB clock. Действия, изменение статуса и audit выполняются в одной PostgreSQL transaction.

## 6. Receiving: индивидуальный результат пробирки

| Метод | Path | Кто | Request / Response |
|---|---|---|---|
| `PATCH` | `/shipments/{id}/receiving/items/{item_id}` | Destination Lab Operator | `200 ContainerItem`, body `ReceiptResultRequest`, только `RECEIVING` |
| `POST` | `/shipments/{id}/receiving/complete` | Destination Lab Operator | `200 Shipment`, body `{}`, только `RECEIVING` |

**ReceiptResultRequest:**
~~~json
{
  "result": "MISSING",
  "reason_code": "SAMPLE_NOT_FOUND", "comment": "Не обнаружена при сверке содержимого контейнера"
}
~~~

Правила:
- `result=ACCEPTED` → `reason_code` и `comment` должны быть null либо отсутствовать.
- `result=MISSING` → пробирка не обнаружена; обязателен валидный `reason_code` с scope=SAMPLE,kind=MISSING, комментарий необязателен.
- `result=REJECTED` → пробирка предъявлена, но лаборатория отказала; обязателен `reason_code` с scope=SAMPLE,kind=REJECTED.
- Причина берётся из справочника reason codes по scope/kind. Свободный комментарий необязателен, до 500 символов. Не добавлять самостоятельные категории повреждений/температурных инцидентов.
- Нельзя принимать позицию из другой партии, вне своего scope или до двух подтверждений Delivery.
- **Утверждено v0.5:** до `complete` исправление разрешено с receipt revision и audit. После завершения прямое исправление запрещено; LAB_MANAGER может повторно открыть приёмку с причиной и новой receiving session.
- `complete` читает **все** контейнеры партии и все `container_items` внутри transaction с lock на Shipment. Если есть `receipt_result IS NULL` → `409 RECEIVING_INCOMPLETE`. Все `ACCEPTED` → `COMPLETED`; иначе `COMPLETED_WITH_ISSUES`.
- При `REJECTED` лаборатория не обязана в P0 отслеживать дальнейшую физическую судьбу пробирки: возвраты и перераспределение вне scope.

## 7. Audit и региональная аналитика

| Метод | Path | Роль | Ответ |
|---|---|---|---|
| `GET` | `/shipments/{id}/events` | Scoped participant | `200 Paged<AuditEvent>` по `occurred_at ASC,id ASC` |
| `GET` | `/analytics/shipments/summary` | Sender, Lab, Org Admin, Regional Analyst | `200 ShipmentSummary`; фильтры `region_id,organization_id,from,to` с принудительным scope |

**AuditEvent**: `id,shipment_id,entity_type,entity_id,event_type,actor_id,occurred_at,request_id,details`. События выдачи и получения не объединяются: `RELEASE_CONFIRMED`, `PICKUP_CONFIRMED`, `DELIVERY_CONFIRMED`, `LAB_RECEIPT_CONFIRMED`. В запросе истории на небольших данных сортировка по возрастанию времени.

**ShipmentSummary**: `region_id,from,to,total,by_status:{status:count},completed_with_issues,missing_samples,rejected_samples,overdue_active`. Показатели вычисляются по фактическим записям; нет утверждений о готовой performance/accuracy. Региональный аналитик не может запросить данные других регионов.

## 8. Баркоды, lookup и границы безопасности

`POST /scan/resolve` (authenticated, read-only POST; **без** Idempotency-Key):
~~~json
{"code":"LT-C-EXAMPLE-01"}
~~~

Ответ `200`: `{kind:"CONTAINER",id,shipment_id}` или `{kind:"SAMPLE",id,shipment_id:null|UUID}` с учётом scope. Для неизвестного/недоступного кода — одинаковый `404 RESOURCE_NOT_FOUND` без раскрытия данных. Этот endpoint **не меняет состояние**: ни pickup, ни receiving не происходят от простого сканирования QR.

QR-метка содержит лишь код, не URL с токеном, не ссылку на пациента и не медицинскую информацию. Отображение метки и печать — UI/client concern, а генерацию непрозрачного кода обеспечивает backend.

## 9. Idempotency-Key и concurrency

`Idempotency-Key` — обязательный header (UUID либо 8..128 ASCII visible chars, конфигурация клиента) для:
- `POST /samples`, `POST /shipments`, `POST /shipments/{id}/containers`, `POST .../items`;
- `PATCH .../courier`, `POST .../seal`, `POST .../cancel`;
- всех `POST .../handoffs/*`, `PATCH .../receiving/items/*` и `POST .../receiving/complete`.

Для membership creates/changes также требуется ключ. Для `DELETE` допускается повторный запрос без idempotency key: повтор после удаления может вернуть `404`, состояние не восстанавливается.

**Порядок обработки:**
1. Authenticate actor, validate payload, determine operation = method + canonical route + concrete resource IDs.
2. В транзакции зарезервировать `(actor_id,operation,key)`, сопоставить canonical request hash; при повторе вернуть сохранённый status/body.
3. Для новой команды выполнить организационные policy checks и `SELECT ... FOR UPDATE` Shipment (если команда изменяет Shipment aggregate). Записать изменения + audit + идемпотентный response в **той же** транзакции.
4. После commit вернуть зафиксированный ответ. Любая ошибка в транзакции откатывает mutation, audit и идемпотентную запись.

Транзакция не включает отправку Celery, сетевые запросы или ожидание внешних API. TTL сохранённого ответа для MVP предлагается 24 часа; очистка идемпотентных ключей не изменяет события аудита. Параллельный запрос с **другим ключом** может получить `409 INVALID_STATE` или `409 VERSION_CONFLICT`.

Пример:
~~~http
POST /api/v1/shipments/33333333-3333-4333-8333-333333333333/handoffs/pickup/confirm-pickup
Authorization: Bearer <token>
Idempotency-Key: 8e04d4bd-c687-4f34-8532-5c98e4903431
Content-Type: application/json

{"container_codes":["LT-C-EXAMPLE-01","LT-C-EXAMPLE-02"]}
~~~

## 10. Стандартный error contract

Все ошибки (кроме штатной платформенной 422 до преобразования) приводятся API exception handlers к единой JSON форме:

~~~json
{
  "error": {
    "code": "INVALID_STATE",
    "message": "Shipment cannot be confirmed in its current state",
    "details": {"current_status": "DRAFT", "required_status": "PICKUP_PENDING"},
    "request_id": "66666666-6666-4666-8666-666666666666"
  }
}
~~~

| HTTP | Error code | Когда |
|---|---|---|
| 400 | `INVALID_REQUEST` | Нарушена семантика запроса за пределами schema validation |
| 401 | `UNAUTHORIZED` | Отсутствует/просрочен/невалиден token |
| 403 | `FORBIDDEN` | Ресурс доступен, операция запрещена ролью |
| 404 | `RESOURCE_NOT_FOUND` | Не найдено либо скрыто по policy |
| 409 | `INVALID_STATE` | Команда не разрешена текущим статусом |
| 409 | `SAMPLE_ALREADY_ASSIGNED` | Пробирка уже в контейнере |
| 409 | `CONTAINER_NOT_EMPTY` | Попытка удалить непустой контейнер |
| 409 | `MANIFEST_MISMATCH` | Сканированный набор контейнеров не совпадает с манифестом |
| 409 | `RECEIVING_INCOMPLETE` | Не все пробирки обработаны |
| 409 | `SELF_CONFIRMATION_FORBIDDEN` | Одно лицо пытается завершить обе стороны handoff |
| 409 | `IDEMPOTENCY_KEY_REUSED` | Один ключ и отличный payload |
| 409 | `VERSION_CONFLICT` | Конкурентное изменение состояния/версии |
| 422 | `VALIDATION_ERROR` | Pydantic validation (включая недостающую/пустую причину) |
| 429 | `RATE_LIMITED` | Превышен auth/request лимит (после реализации) |
| 503 | `SERVICE_UNAVAILABLE` | Временная недоступность PostgreSQL или обязательной зависимости |

`details` должны быть безопасны: не включать чужие данные при недостаточном scope, не возвращать SQL/stacktrace/password_hash. Ошибка 500 — общий, не раскрывающий внутренности текст с `request_id`.

## 11. Endpoint-to-role матрица

| Операция | Sender | Courier | Lab | Org Admin | Regional Analyst |
|---|---|---|---|---|---|
| Create Sample / Shipment / Container / Item | Own org | — | — | — | — |
| Seal, Cancel, Assign Courier | Own shipments | — | — | — | — |
| Pickup: confirm release / withdraw | Own shipment, own pending action | — | — | — | — |
| Pickup: confirm pickup | — | Assigned shipment | — | — | — |
| Delivery: confirm delivery / withdraw | — | Assigned shipment | — | — | — |
| Delivery: confirm receipt | — | — | Destination org | — | — |
| Record/complete sample receiving | — | — | Destination org | — | — |
| Read shipment manifest/events | Own | Assigned | Destination | — | — |
| Membership administration | — | — | — | Own organization | — |
| Analytics | Own org | — | Own lab | Own org | Own region |

Если пользователь имеет несколько ролей, **каждая команда** выполняется от одной проверенной role/membership context; две стороны одного handoff всё равно не могут подтверждаться одним `user_id`.

## 12. Сквозные примеры и acceptance

**Happy path:** 10 отдельных пробирок → Shipment → 2 контейнера → 10 позиций → назначение курьера → seal → sender confirm-release → courier confirm-pickup → courier confirm-delivery → lab confirm-receipt → по каждой пробирке `PATCH receiving/items` → complete. Для 8 `ACCEPTED`, 1 `MISSING`, 1 `REJECTED` итог `COMPLETED_WITH_ISSUES`, две причины и четыре независимых подтверждения видны в журнале.

**Критические API tests:**
- До ответа Courier статус не может стать `IN_TRANSIT`; до ответа Lab не может стать `RECEIVING`.
- После handoff лаборатория видит все `receipt_result=null`; сканирование контейнера не проставляет `ACCEPTED`.
- Попытка подтвердить одновременно чужим курьером, тем же actor, при несовпадении кодов или в другом регионе отклоняется.
- Конкурентные добавления одного sample в разные контейнеры дают один успех, один `409`.
- `seal` одновременно с изменением состава не оставляет полуманифеста.
- Повтор с `Idempotency-Key` после network timeout не создаёт второй audit event.
- Конец receiving с необработанной пробиркой даёт `409`; после complete изменение возможно только через LAB_MANAGER reopen с revisions.
- Regional Analyst не получает строки Sample/ContainerItem по прямым ID.

## 13. Product decisions v0.5 и открытые допущения

Согласовано: назначение Courier Sender в DRAFT; поэкземплярная и контейнерная MISSING/REJECTED; manager reopening; reason-code catalog; offline Courier queue; одна центральная Lab; синтетические external references; PostgreSQL manifest triggers. Все расширения API перечислены в [API Delta v0.5](13-api-v05.md).

До реального пилота отдельно утвердить фактическую ответственность за отказанный контейнер, юридическую семантику offline timestamp, длительность ручного review и конкретные справочники reason codes. Машинно-читаемый [OpenAPI v0.5](../openapi/labtrack-v1.json) остаётся draft до реализации FastAPI и контрактных тестов.
