# API

## Runtime API

FastAPI публикует runtime-схему по `/openapi.json`, а Swagger UI — по `/docs`. В runtime-путях есть префикс `/api/v1`: например, `POST /api/v1/auth/token`, `POST /api/v1/courier/sync`, а также маршруты Pickup, Delivery по отдельному контейнеру и его приёмки под `/api/v1/shipments`.

Чтобы выгрузить точную схему из `app.openapi()` в стабильном JSON с отсортированными ключами, выполните из `backend/`:

```bash
python scripts/export_runtime_openapi.py > /tmp/labtrack-runtime-openapi.json
python scripts/export_runtime_openapi.py --check
python scripts/export_runtime_openapi.py --check-file /tmp/labtrack-runtime-openapi.json
```

Генератор импортирует ASGI-приложение, не запускает его lifespan и не подключается к PostgreSQL, Redis или внешним сервисам. Режим `--check` проверяет реализованные пути, имена операций, модели запросов и успешных ответов, объявленные error-envelope и детерминированность сериализации. Режим `--check-file` сравнивает выгрузку с сохранённым файлом и выявляет drift. Отдельная runtime-схема в репозитории не хранится: поиск не выявил клиентов или генераторов, которым она нужна. Существующий JSON оставлен историческим design-документом; отдельные backend-тесты используют его только для проверки соответствия выбранным требованиям v0.6.1. Если runtime-артефакт будет добавлен позже, включите `--check-file <artifact>` в CI.

Runtime OpenAPI описывает то, что сейчас объявлено в маршрутах. Есть известный пробел в описании ошибок: `/api/v1/auth/token` может вернуть `401` и `429`, но эти статусы не перечислены в OpenAPI для этой операции. Для `422` сгенерированная схема указывает FastAPI `HTTPValidationError`, тогда как глобальный validation handler приложения фактически возвращает `APIErrorResponse`. Здесь это расхождение зафиксировано без изменения деклараций маршрутов или схем. Другие маршруты, объявляющие `API_ERROR_RESPONSES`, ссылаются на `APIErrorResponse` с телом вида `{ "error": { "code": "…", "message": "…", "details": {}, "request_id": "…" } }`.

Примеры runtime-операций:

| Операция | Запрос | Успешный ответ |
|---|---|---|
| `POST /api/v1/auth/token` | `LoginRequest` | `AccessTokenResponse` |
| `POST /api/v1/courier/sync` | `CourierSyncRequest` | `CourierSyncResponse` |
| `POST /api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release` | `ContainerCodesRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup` | `ContainerCodesRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-delivery` | `ContainerCodeRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-receipt` | `ContainerCodeRequest` | `HandoffActionResult` |

Другие группы маршрутов отвечают за организации и справочники, образцы, инциденты передачи, проблемы контейнеров, operations, analytics и notifications. Полный список доступен в runtime `/openapi.json`.

## Исторический design-документ v0.6.1

Файл [`openapi/labtrack-v1.json`](../openapi/labtrack-v1.json) сохранён как исторический черновик OpenAPI 3.1 для дизайна v0.6.1. В нём есть `/auth/login`, а пути указаны относительно сервера `/api/v1`; это имена и соглашения design-версии, а не список runtime-маршрутов. В реализации используется `/api/v1/auth/token`, а runtime-схема содержит полный префикс путей. Не используйте исторический файл для генерации клиента к запущенному приложению.

Связанный документ [API & Operations v0.6.1](24-api-and-operations-v061.md) описывает продуктовые требования. См. также [архитектуру](architecture.md) и [рабочие процессы](workflows.md).
