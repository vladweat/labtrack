# LabTrack — Workflows, Transactions & Concurrency

**Версия:** 0.3 (проект спецификации)  
**Статус:** design baseline для первой реализации, не работающий backend.  
**Связанные документы:** [Data Model & ERD](06-data-model.md), [Domain Model](03-domain-and-workflows.md), [MVP Requirements](02-mvp-requirements.md).

## 1. Граница ответственности

`Shipment` — **агрегат и единица сериализации**. Его состояние, контейнеры, включённые пробирки, handoffs и итоговая приёмка изменяются через единый command/service layer. Веб-обработчики не выполняют прямые изменения статуса ORM-моделей.

- `DRAFT` допускает изменение контейнеров и их содержимого.
- После `seal` манифест фиксируется: 1..N непустых контейнеров, каждая пробирка в одном контейнере.
- Передачи — **по всей партии и манифесту контейнеров**, отдельные подтверждения разных пользователей.
- Получение контейнеров в лаборатории открывает `RECEIVING`, **но не подтверждает ни одной пробирки**.
- Только результаты каждой пробирки — `ACCEPTED`, `MISSING`, `REJECTED` — определяют итог партии.
- Изолированные по организациям пользователи видят только разрешённые ресурсы; `REGIONAL_ANALYST` читает только агрегаты.

## 2. State machine партии

~~~mermaid
stateDiagram-v2
    [*] --> DRAFT
    DRAFT --> READY_FOR_PICKUP: seal
    DRAFT --> CANCELLED: cancel
    READY_FOR_PICKUP --> CANCELLED: cancel
    READY_FOR_PICKUP --> PICKUP_PENDING: confirm_release (Sender)
    PICKUP_PENDING --> READY_FOR_PICKUP: withdraw_release (Sender)
    PICKUP_PENDING --> IN_TRANSIT: confirm_pickup (Courier)
    IN_TRANSIT --> DELIVERY_PENDING: confirm_delivery (Courier)
    DELIVERY_PENDING --> IN_TRANSIT: withdraw_delivery (Courier)
    DELIVERY_PENDING --> RECEIVING: confirm_receipt (Lab)
    RECEIVING --> COMPLETED: all ACCEPTED
    RECEIVING --> COMPLETED_WITH_ISSUES: any MISSING or REJECTED
~~~

| Команда | Инициатор | Проверка и эффект |
|---|---|---|
| `add_container / add_sample / move_sample` | Sender Operator отправителя | Только `DRAFT`; sample.origin_org = sender; изменение состава под lock партии |
| `assign_courier` | Sender Operator (предлагаемый default) | Только `DRAFT`; активный Courier с разрешением в регионе |
| `seal` | Sender Operator | ≥1 контейнер, каждый непустой; courier назначен; фиксирует `sealed_at` и `READY_FOR_PICKUP` |
| `confirm_release` | Sender Operator | `READY_FOR_PICKUP` → `PICKUP_PENDING`; создаёт `PICKUP PENDING` handoff, фиксирует actor/time |
| `confirm_pickup` | Назначенный Courier | `PICKUP_PENDING` → `IN_TRANSIT`; другой actor, подтверждает тот же manifest; handoff CONFIRMED |
| `confirm_delivery` | Courier партии | `IN_TRANSIT` → `DELIVERY_PENDING`; создаёт `DELIVERY PENDING` handoff |
| `confirm_receipt` | Lab Operator адресной лаборатории | `DELIVERY_PENDING` → `RECEIVING`; другой actor, handoff CONFIRMED, контейнеры получены, но пробирки не обработаны |
| `withdraw_release` | Инициатор pending pickup | `PICKUP_PENDING` → `READY_FOR_PICKUP`; handoff WITHDRAWN, audit |
| `withdraw_delivery` | Инициатор pending delivery | `DELIVERY_PENDING` → `IN_TRANSIT`; handoff WITHDRAWN, audit |
| `record_receipt` | Lab Operator адресной лаборатории | Только `RECEIVING`, конкретная пробирка из sealed manifest; сохраняет статус, причину (если нужна), actor и audit |
| `complete_receiving` | Lab Operator адресной лаборатории | Все container_items имеют результат; все ACCEPTED → COMPLETED, иначе COMPLETED_WITH_ISSUES |
| `cancel` | Sender Operator | Только `DRAFT` или `READY_FOR_PICKUP`; причина обязательна |

**Недопустимые переходы** должны возвращать предсказуемый `409 Conflict`. Отсутствие прав возвращает `403 Forbidden` либо маскирующий `404 Not Found` по единой политике. Нельзя изменять роль/назначение в середине подтверждения, минуя бизнес-проверки.

## 3. Handoff: точные условия

### Pickup

1. Sender выполняет `confirm_release`, сверив все контейнеры манифеста; создаётся `handoff(kind=PICKUP,status=PENDING)`.
2. Courier считывает метки контейнеров и подтверждает получение **того же состава**. Только этот шаг переводит партию в `IN_TRANSIT`.
3. Самоподтверждение запрещено, даже если пользователь состоит сразу в двух ролях. К моменту каждого действия проверяем активный аккаунт и membership.
4. Sender может отозвать свою pending-подпись до действия Courier; history сохраняется. Следующая попытка получает новый `attempt_no`.

### Delivery

1. Courier предъявляет контейнеры и выполняет `confirm_delivery`.
2. Lab Operator из организации назначения сверяет контейнеры, выполняет `confirm_receipt`.
3. Только после этого партия переходит в `RECEIVING` и становится доступна приёмка пробирок.
4. Курьер может отозвать ожидающее подтверждение до действия лаборатории.

**Практическое условие:** каждый участник в интерфейсе сканирует/явно сверяет полный набор кодов контейнеров из sealed manifest. API принимает список кодов/ID подтверждённых контейнеров и сравнивает его как **множество** с ожидаемым набором (без дубликатов). Это защита от случайного подтверждения неверного контейнера, но не криптографическое доказательство физической передачи.

Передача отдельных контейнеров, частичная выдача и отказ от всей партии в P0 не поддерживаются. Если контейнер отсутствует на этапе handoff, **второй участник не подтверждает передачу**; партия остаётся в ожидающем состоянии до внешнего урегулирования. Это сознательное ограничение MVP, не «успешная доставка».

## 4. Приёмка одной пробирки

| Значение | Физически предъявлена? | Причина | Что означает |
|---|---|---|---|
| `ACCEPTED` | Да | Не нужна | Организационно принята лабораторией |
| `MISSING` | Нет | Обязательна | По манифесту должна быть, но не обнаружена |
| `REJECTED` | Да | Обязательна | Лаборатория отказала в приёмке конкретной пробирки |

Сканирование контейнера только открывает список ожидаемых пробирок; результат по каждой позиции фиксируется отдельно. В `RECEIVING` предлагается разрешить исправление ошибочной отметки до завершения с отдельным audit event `RECEIPT_CORRECTED`; после финализации записи не изменяются. Эта возможность — **предложение для удобства MVP** и требует явного сценария UI/API при реализации.

Отказ в приёмке пробирки — операционное решение сотрудника, не автоматическая клиническая оценка. Отдельных категорий «повреждена», «температура нарушена», «возвращена» и т. п. в P0 нет. Причины `MISSING`/`REJECTED` — строки в P0 до согласования кодов.

## 5. Блокировки, порядок и изоляция

**Рекомендация:** PostgreSQL `READ COMMITTED` + короткие транзакции + последовательный `SELECT ... FOR UPDATE` на `shipments` для каждой команды, меняющей агрегат.

Для вложенных изменений всегда соблюдаем порядок:

1. `shipments` (одна строка, `FOR UPDATE`);
2. связанные `transport_containers` / `container_items` или `handoffs`;
3. `audit_events` и `idempotency_records` в той же транзакции.

Запрещаем любые отдельные code paths, которые сначала блокируют позицию или handoff, а затем shipment — это создаст риск deadlock.

~~~python
async def execute_shipment_command(
    session: AsyncSession,
    shipment_id: UUID,
    actor: Actor,
    command: Command,
) -> CommandResult:
    # Псевдокод: детали хранилища/политик зададим в API design.
    # Транзакция открыта на уровне use-case; внутри не делать HTTP/LLM/Celery вызовы.
    shipment = await repository.get_for_update(session, shipment_id)
    policies.require_allowed(actor, command, shipment)
    command.validate_state(shipment)
    result = await command.apply(session, shipment)
    await audit_repository.append(session, result.event)
    shipment.version += 1
    return result
~~~

> Псевдокод показывает invariant ownership. Реальный код должен также атомарно обрабатывать idempotency и ошибки уникальности. Не использовать `with_for_update()` вне активной транзакции.

**Почему не SERIALIZABLE везде:** для небольшого модульного монолита row lock на агрегате проще тестировать; достаточно для корректного seal/pickup/delivery/receiving при дисциплине единого write path. Оставляем `UNIQUE(sample_id)` как последнюю линию защиты, если два разных shipment одновременно включают одну пробирку.

## 6. Транзакционные границы по use case

| Use case | В одной транзакции | Что делать после commit |
|---|---|---|
| Register sample | Валидация, INSERT sample, audit | Ничего обязательного |
| Edit manifest | Lock shipment; проверки; INSERT/DELETE container_item; bump version; audit | Не нужен async |
| Seal | Lock shipment; проверка всех контейнеров и пробирок; set status/sealed_at/version; audit | Best-effort уведомление Courier |
| Initiate/confirm/withdraw handoff | Lock shipment; роль, actor, manifest, попытка handoff, статус, version, audit | Best-effort уведомление другой стороны |
| Record/correct receipt | Lock shipment; проверка sample и текущей приёмки; receipt fields; audit; version | Обновить UI |
| Complete receiving | Lock shipment; COUNT unprocessed=0; result; status/completed_at/version; audit | Best-effort статистика/уведомление |
| Cancel | Lock shipment; status/cancel_reason/version; audit | Best-effort уведомление |

**Никогда** не подтверждать статус, если запись audit не сохранилась; обе операции должны rollback вместе. Если уведомление отправляется через Celery сразу до commit, worker может увидеть ещё несуществующее состояние: запускать только после успешного commit. Без transactional outbox P0 уведомления best-effort: при падении процесса между commit и публикацией сообщение может быть потеряно. Для критичных интеграций потребуется outbox (P1).

## 7. Idempotency и обработка конфликтов

Для create, seal, подтверждений, отмены и финализации используем заголовок `Idempotency-Key`. Ключ включает scope: actor + operation, а payload сравнивается по каноническому request hash.

**Упрощённый алгоритм:**

1. В рамках той же DB transaction зарезервировать строку `idempotency_records` через INSERT с уникальностью `(actor_id,operation,key)`.
2. Если ключ уже существует, дождаться завершения конкурирующей транзакции, сверить request hash и возвратить сохранённый HTTP response; различающийся payload → `409`.
3. Если запись новая, выполнить команду под lock `shipment`; сохранить response_status/response_body и audit, затем commit.
4. Только после commit ответить клиенту и публиковать best-effort уведомления.

Если транзакция падает до commit, и идемпотентная запись, и бизнес-изменение откатываются. Idempotency с разными ключами **не** обещает одинаковый результат: корректность в этом случае обеспечивают lock, state machine и unique constraints. TTL записи предлагается 24 часа в демонстрации и требует документирования в API; после TTL повтор create может создать новый объект, поэтому клиент обязан соблюдать retry policy.

**Ошибки:**

| Ситуация | Ответ |
|---|---|
| Другая организация / отсутствие scope | `403` или `404` по выбранной политике |
| Некорректный переход/необработанная пробирка | `409 Conflict`, стабильный error code |
| Пробирка уже в контейнере | `409 SAMPLE_ALREADY_ASSIGNED` (обработать PostgreSQL `23505` на известном unique constraint) |
| Одинаковый key, другой payload | `409 IDEMPOTENCY_KEY_REUSED` |
| Ошибочная структура запроса | `422` |
| Временная недоступность БД | `503` или контролируемый `500`, без утечки деталей и с корреляционным request ID |

## 8. Security boundaries и аудит

- Scope проверяется для **каждого** чтения и записи: sender org, destination org, назначенный Courier либо scoped Regional Analyst.
- Courier не получает данные других партий; analyst получает агрегаты, не строки `samples`/`container_items`.
- Не отдавать SQL-ошибки клиенту; query параметризованы ORM/driver, не конструировать фильтры из необработанных SQL-фрагментов.
- `AuditEvent` имеет `request_id`, entity ID, actor/time и минимальный безопасный details payload. Пароли, токены, диагнозы, пациентские идентификаторы и PHI не логируются.
- События `RELEASE_CONFIRMED` / `PICKUP_CONFIRMED` / `DELIVERY_CONFIRMED` / `LAB_RECEIPT_CONFIRMED` различны; audit отображает обе стороны.
- Подтверждения не являются квалифицированной подписью и не заменяют регламентированные перевозочные документы.

## 9. Минимальный набор integration и concurrency tests

| ID | Тест | Ожидание |
|---|---|---|
| TX-01 | Два запроса добавляют одну пробирку в разные контейнеры | Одна вставка, вторая `409`; не более одной container_item |
| TX-02 | `seal` одновременно с изменением состава | Либо изменение до seal отражено в манифесте, либо изменение отклонено; «полуманифеста» нет |
| TX-03 | Pickup подтверждает только Sender | `PICKUP_PENDING`, Courier ещё не владелец |
| TX-04 | Назначенный Courier и посторонний Courier подтверждают одновременно | Только назначенный пользователь может завершить handoff |
| TX-05 | Один аккаунт с двумя ролями подтверждает обе стороны | Вторая операция запрещена |
| TX-06 | Повтор подтверждения с тем же Idempotency-Key | Один переход, одна подтверждённая попытка, тот же ответ |
| TX-07 | Courier подтверждает delivery, Lab ещё нет | `DELIVERY_PENDING`; приёмка пробирок закрыта |
| TX-08 | Lab подтверждает контейнеры и пытается автоматически принять все пробирки | Не принимаются автоматически; `receipt_result` остаётся NULL |
| TX-09 | 1 из N пробирок не имеет результата | Финализация отклонена, audit не содержит фиктивного завершения |
| TX-10 | Есть `MISSING` и `REJECTED`, у обоих причина | Партия завершается `COMPLETED_WITH_ISSUES` |
| TX-11 | Причина пустая для `MISSING`/`REJECTED` | CHECK/validation отклоняет |
| TX-12 | Внутри транзакции падает запись audit | Статус и handoff rollback |
| TX-13 | Две финализации одновременно | Не более одного события завершения |
| TX-14 | Sender из организации A читает/изменяет партию B | Доступ запрещён без утечки содержимого |
| TX-15 | Pending handoff отозван и создан повторно | Старый WITHDRAWN, новый attempt_no, не два активных |
| TX-16 | После seal пытаются добавить контейнер или пробирку | Конфликт; ни одна запись не изменилась |
| TX-17 | Повторная команда после сбоя соединения в момент commit | Результат однозначно восстанавливается по Idempotency-Key |
| TX-18 | Данные одной области переданы в shipment другой | FK или валидация отклоняет |

**Практика:** использовать настоящий PostgreSQL для integration и concurrency; SQLite не повторяет нужную семантику row locks. Для одновременных операций нужны отдельные сессии/соединения и синхронизированный старт. Unit-тесты покрывают policy и state machine без БД, E2E — сквозной сценарий 10 пробирок в 2 контейнерах.

## 10. Definition of Ready перед реализацией

- [x] Зафиксирован уровень идентификации: пробирка → контейнер → партия.
- [x] Зафиксированы роли обеих сторон при pickup и delivery.
- [x] Зафиксированы две категории исключений при приёмке.
- [x] Определены основные таблицы, FK/UNIQUE/CHECK, индексы и транзакционные границы.
- [ ] Согласовано, кто назначает Courier и меняет назначение.
- [ ] Согласовано поведение изменения ошибочного результата до финализации.
- [ ] Утверждён набор причин MISSING/REJECTED и формат меток.
- [ ] Подготовлены Alembic migrations и проверены на PostgreSQL.
- [ ] Зафиксированы REST payloads, error codes и OpenAPI.
