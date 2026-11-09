# Backup/restore drill для synthetic sandbox

## Что покрывает проверка

Корневой `compose.yaml` поднимает PostgreSQL 15 с loopback-only портом и
именованным volume. Миграции выполняются ролью-владельцем БД; приложение
использует отдельную runtime-роль с DML grants. API запускается отдельно от
этого Compose файла. Проект `labtrack-demo-sandbox` и volume
`labtrack_demo_sandbox_postgres_data` отделены от development и public demo.

В репозитории не было воспроизводимой проверки восстановления с проверкой
версии Alembic, данных workflow и runtime grants. Public demo smoke — это
неаутентифицированные GET проверки UI/readiness, а не проверка восстановления.
Deployment checkout и его базовая Compose конфигурация не входят в этот drill;
он не доказывает восстановимость public demo или production.

Скрипт делает custom-format `pg_dump` только из локальной opted-in БД
`labtrack_demo_sandbox`. PostgreSQL формирует архив из согласованного snapshot
одной БД. До и после dump скрипт сравнивает версию схемы, выбранные counts и
synthetic-инварианты; при изменении данных drill завершается ошибкой. Остановите
API и writers заранее, чтобы сравнение было стабильным.

Архив восстанавливается в новую БД с именем
`labtrack_restore_test_<12 hex>`. Перед созданием проверяется отсутствие БД;
после создания проверяются имя, владелец, `datistemplate=false` и разрешение
подключения. Произвольные имена, включая `postgres`, `labtrack` и production
имена, отклоняются. Проверяющий PostgreSQL запускается с `--network none` и
хранит data directory в tmpfs. Тестовая БД удаляется после проверки, а контейнер
удаляется при успехе и обычной ошибке.

Проверка сравнивает Alembic head и counts таблиц `regions`, `organizations`,
`users`, `samples`, `shipments`, `transport_containers`, `container_items`,
`handoffs` и `audit_events`. Она также требует synthetic email с доменом
`.example.test`, организацию `DEMO_SENDER` и хотя бы одну связь Shipment →
TransportContainer. Runtime-роль восстанавливается тем же init script, затем
проверяются login/connect/read-write grants, отсутствие owner/admin прав,
`CREATE` на database/schema, `TRUNCATE` и `TRIGGER`. Финальный workflow-запрос
выполняется от этой роли в `READ ONLY` транзакции. Перед ним проверяется
парольная аутентификация роли.

## Запуск локально

Сначала создайте изолированную synthetic demo БД, примените миграции и выполните
`seed-demo` по шагам [demo deployment guide](deploy.md). Используйте только
локальный `.env.demo.sandbox` с правами `0600`. Убедитесь, что в этом Compose
проекте запущен только PostgreSQL; остановите локальный API и все процессы,
которые могут писать в БД. Скрипт дополнительно откажется работать при других
запущенных Compose services или активных DB-сессиях runtime-роли.

Нужны Docker Compose, backend `.venv` с dev-зависимостями и уже кешированный
образ `postgres:15`. Скрипт не скачивает образы. Из корня репозитория:

```bash
docker image inspect postgres:15 >/dev/null
docker compose --project-name labtrack-demo-sandbox \
  --env-file .env.demo.sandbox --file compose.yaml ps
install -d -m 700 backups
archive="backups/labtrack-demo-$(date -u +%Y%m%dT%H%M%SZ).dump"
cd backend
.venv/bin/python -m app.ops.backup_restore_drill --archive "../$archive"
cd ..
(cd backups && sha256sum -c "$(basename "$archive").sha256")
```

Команда читает приватный env-файл сама, не исполняет его как shell и не печатает
значения. Для проверки отказа по имени можно передать, например,
`--restore-database labtrack`; цель будет отклонена до обращения к Docker.
Используйте новый путь для каждого архива: существующий архив или checksum
никогда не перезаписываются.

При успешной проверке команда оставляет `.dump` и `.sha256` в `backups/`, оба
файла имеют mode `0600`. Временная restore-БД и контейнер удаляются. При ошибке
неполный архив/checksum удаляются, а исходная БД не изменяется. Сообщения
команды не включают пароли, email или содержимое строк; подробный вывод
subprocess намеренно подавляется. Пароли поступают из `.env.demo.sandbox` и не
передаются в аргументах команд. Пользователи Docker daemon всё равно могут
инспектировать окружение работающих контейнеров.

## Хранение и ограничения

- Восстановление проверено только для согласованного snapshot одной PostgreSQL
  БД. WAL archive, point-in-time recovery, восстановление ролей кластера,
  Redis/Celery state и согласование с внешними системами не реализованы.
- Автоматической ротации и заданного retention срока нет. Оператор выбирает
  срок хранения, ограничивает доступ, удаляет устаревшие файлы и при переносе
  использует утвержденное шифрование. Не добавляйте архивы или env-файлы в Git,
  CI artifacts, issue или публичные логи.
- Архив содержит synthetic учётные записи, password hashes, audit и workflow
  данные; несмотря на synthetic границу, храните его как приватный файл.
- Drill проверяет восстановление из `pg_dump`, но не резервное копирование
  volume, не аварийный recovery host и не SLA. Не используйте его для public
  demo или production БД.
- `.github/workflows/public-demo-smoke.yml` остаётся GET-only и unauthenticated.
  Любой будущий authenticated E2E smoke запускайте только на отдельной
  контролируемой staging/fixture БД с собственными synthetic аккаунтами,
  volume и reset-процедурой; не входите и не меняйте данные общей public demo.

CI запускает unit safety tests в backend workflow с отдельным service
`labtrack_test`. Они используют in-memory restore adapter, не вызывают Docker и
не имеют production host/config. Реальный restore drill запускается только
вручную на локальном sandbox.
