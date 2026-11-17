# API

## API работающего приложения

FastAPI публикует схему работающего приложения по `/openapi.json`, а Swagger UI — по `/docs`. В путях работающего приложения есть префикс `/api/v1`: например, `POST /api/v1/auth/token`, `POST /api/v1/courier/sync`, а также маршруты забора, доставки отдельного контейнера и его приёмки под `/api/v1/shipments`.

Чтобы выгрузить точную схему из `app.openapi()` в стабильном JSON с отсортированными ключами, выполните из `backend/`:

```bash
python scripts/export_runtime_openapi.py > /tmp/labtrack-runtime-openapi.json
python scripts/export_runtime_openapi.py --check
python scripts/export_runtime_openapi.py --check-file /tmp/labtrack-runtime-openapi.json
```

Генератор импортирует ASGI-приложение, не запускает его обработчик жизненного цикла и не подключается к PostgreSQL, Redis или внешним сервисам. Режим `--check` проверяет реализованные пути, имена операций, модели запросов и успешных ответов, объявленные форматы ошибок и детерминированность сериализации. Режим `--check-file` сравнивает выгрузку с сохранённым файлом и выявляет расхождения. Отдельная схема работающего приложения в репозитории не хранится: поиск не выявил клиентов или генераторов, которым она нужна. Существующий JSON оставлен историческим проектным документом; отдельные серверные тесты используют его только для проверки соответствия выбранным требованиям v0.6.1. Если артефакт схемы работающего приложения будет добавлен позже, включите `--check-file <artifact>` в CI.

Текущая схема OpenAPI объявляет ошибки, которые возвращает приложение: `POST /api/v1/auth/token` указывает `401` и `429` с телом `APIErrorResponse`, а также заголовки `WWW-Authenticate` и `Retry-After`. Общая схема `422` для `/api/v1` тоже указывает `APIErrorResponse`, как и глобальный обработчик ошибок проверки данных. Формат тела ошибки: `{ "error": { "code": "…", "message": "…", "details": {}, "request_id": "…" } }`.

Примеры операций работающего приложения:

| Операция | Запрос | Успешный ответ |
|---|---|---|
| `POST /api/v1/auth/token` | `LoginRequest` | `AccessTokenResponse` |
| `POST /api/v1/courier/sync` | `CourierSyncRequest` | `CourierSyncResponse` |
| `POST /api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release` | `ContainerCodesRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup` | `ContainerCodesRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-delivery` | `ContainerCodeRequest` | `HandoffActionResult` |
| `POST /api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-receipt` | `ContainerCodeRequest` | `HandoffActionResult` |

Другие группы маршрутов отвечают за организации и справочники, образцы, инциденты передачи, проблемы контейнеров, операции, аналитику и уведомления. Полный список доступен в схеме работающего приложения `/openapi.json`.

## Исторический проектный документ v0.6.1

Файл [`openapi/labtrack-v1.json`](../openapi/labtrack-v1.json) сохранён как исторический черновик OpenAPI 3.1 для проектной версии v0.6.1. В нём есть `/auth/login`, а пути указаны относительно сервера `/api/v1`; это имена и соглашения проектной версии, а не список маршрутов работающего приложения. В реализации используется `/api/v1/auth/token`, а схема работающего приложения содержит полный префикс путей. Не используйте исторический файл для генерации клиента к запущенному приложению.

Связанный документ [API и эксплуатация v0.6.1](24-api-and-operations-v061.md) описывает продуктовые требования. См. также [архитектуру](architecture.md) и [рабочие процессы](workflows.md).
