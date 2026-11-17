# Развёртывание демонстрационного стенда и локальная песочница

## Что демонстрирует этот репозиторий

LabTrack моделирует передачу ответственности за пробирки и лабораторные перевозки. Существующий синтетический демонстрационный сценарий — отправитель → курьер → лаборатория: он создаёт четыре демонстрационные учётные записи с ограниченными областями доступа, создаёт пробирку и партию, запечатывает контейнер и оставляет партию в статусе `READY_FOR_PICKUP`.

Пароли начальных учётных записей читаются из переменных окружения `DEMO_*_PASSWORD`; они не встроены в код заполнения данных. Используйте уникальные сгенерированные значения в закрытом файле окружения. Демонстрационные адреса электронной почты заканчиваются на `.example.test`. Заполнение обращается только к локальному API LabTrack. Хранимое в репозитории дополнение Compose ограничивает одноразовый сервис заполнения внутренней сетью данных.

## Защита заполнения демонстрационными данными

`python -m app.cli seed-demo` отказывается запускаться, если не выполнены все условия:

- `APP_ENV=demo`
- `DEMO_SEED_ENABLED=true`
- `POSTGRES_DB` содержит компонент `demo` или `test` и не содержит компоненты `prod`, `production` или `live`
- `DEMO_API_URL` указывает на `http://api:8000`, localhost или интерфейс обратной петли на порту 8000

По умолчанию явное разрешение отключено. Сервисы производственного API и миграций сохраняют `APP_ENV=production`; только одноразовый сервис заполнения получает демонстрационный режим и явное разрешение из [compose.demo-seed.yaml](../compose.demo-seed.yaml). Проект Compose для развёртывания использует `labtrack_demo_postgres_data`; разработка использует независимый том `labtrack_dev_postgres_data`.

## Локальное воспроизведение с изолированными синтетическими данными

Эти команды используют отдельный проект Compose и том PostgreSQL. Они не используют публичную демонстрационную среду или её базу данных. Выполняйте их из корня репозитория. Образ `postgres:15` уже должен быть доступен в Docker, поскольку Compose для разработки настроен без скачивания образов.

Создайте закрытый файл окружения с новыми учётными данными только для синтетической среды; скрипт записывает его с правами `0600` и не печатает значения:

```bash
python3 - <<'PY'
import os
from pathlib import Path
from secrets import token_urlsafe

target = Path(".env.demo.sandbox")
if target.exists():
    raise SystemExit(".env.demo.sandbox already exists; refusing to overwrite it")

values = {
    "POSTGRES_DB": "labtrack_demo_sandbox",
    "POSTGRES_USER": "labtrack_owner",
    "POSTGRES_PASSWORD": token_urlsafe(32),
    "APP_POSTGRES_USER": "labtrack_app",
    "APP_POSTGRES_PASSWORD": token_urlsafe(32),
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "POSTGRES_VOLUME_NAME": "labtrack_demo_sandbox_postgres_data",
    "APP_ENV": "demo",
    "DEMO_SEED_ENABLED": "true",
    "DEMO_API_URL": "http://127.0.0.1:8000",
    "JWT_SECRET_KEY": token_urlsafe(48),
    "DEMO_SENDER_EMAIL": "sender@demo.example.test",
    "DEMO_SENDER_PASSWORD": token_urlsafe(24),
    "DEMO_COURIER_EMAIL": "courier@demo.example.test",
    "DEMO_COURIER_PASSWORD": token_urlsafe(24),
    "DEMO_LAB_OPERATOR_EMAIL": "lab-operator@demo.example.test",
    "DEMO_LAB_OPERATOR_PASSWORD": token_urlsafe(24),
    "DEMO_LAB_MANAGER_EMAIL": "lab-manager@demo.example.test",
    "DEMO_LAB_MANAGER_PASSWORD": token_urlsafe(24),
}
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
    env_file.writelines(f"{name}={value}\n" for name, value in values.items())
print("Created .env.demo.sandbox with private synthetic credentials (values omitted).")
PY
```

Запустите изолированную базу данных, примените миграции и запустите API в одном терминале:

```bash
docker compose --project-name labtrack-demo-sandbox \
  --env-file .env.demo.sandbox up -d postgres
cd backend
set -a
source ../.env.demo.sandbox
set +a
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

В другом терминале из `backend/` заполните синтетический рабочий процесс:

```bash
set -a
source ../.env.demo.sandbox
set +a
.venv/bin/python -m app.cli seed-demo
```

Команда идемпотентна: она повторно использует четыре учётные записи с правильными областями доступа и те же ключи идемпотентности для синтетических пробирки и партии. Она выводит синтетический адрес электронной почты курьера и идентификатор партии, но никогда не выводит пароль. Веб-клиент можно запустить отдельно командой `cd web && npm ci && npm run dev`; для локального знакомства используйте только сгенерированные учётные записи `.example.test`.

Чтобы сбросить только эту песочницу, остановите API и выполните из корня репозитория:

```bash
docker compose --project-name labtrack-demo-sandbox \
  --env-file .env.demo.sandbox down --volumes --remove-orphans
rm .env.demo.sandbox
```

Это удаляет том песочницы `labtrack_demo_sandbox_postgres_data`, сохраняя тома разработки и публичного демонстрационного стенда. Чтобы начать заново, пересоздайте файл окружения и повторите настройку. Не используйте эту команду сброса с проектом публичного развёртывания.

Для проверки резервного копирования и восстановления только этой локальной синтетической БД см. [воспроизводимую проверку](backup-restore.md). Она не является инструкцией восстановления публичного демонстрационного стенда.

## Публичный синтетический демонстрационный стенд

Базовый файл `deploy/demo/compose.yaml` и его `.env` — закрытые файлы среды развёртывания, которых нет в этой копии исходников. Выполняйте следующие команды из рабочей копии развёртывания, содержащей эти файлы; дополнение хранится в этом репозитории. Запустите одноразовое заполнение:

```bash
docker compose --project-name labtrack-demo \
  --env-file deploy/demo/.env \
  --file deploy/demo/compose.yaml \
  --file compose.demo-seed.yaml \
  --profile demo-seed run --rm seed
```

Дополнение задаёт `APP_ENV=demo` и `DEMO_SEED_ENABLED=true` только для этого сервиса, ограничивает его внутренней сетью и по умолчанию сохраняет существующие имена томов. Оно принимает переопределения `DEMO_POSTGRES_VOLUME`, `DEMO_CADDY_DATA_VOLUME` и `DEMO_CADDY_CONFIG_VOLUME`, когда отдельной песочнице развёртывания нужны собственные тома. Храните `DEMO_*_PASSWORD`, пароли базы данных и `JWT_SECRET_KEY` в закрытом файле окружения развёртывания. Не копируйте их в этот репозиторий, вывод в задачах, скриншоты или общедоступные журналы. Заполнение идемпотентно и не сбрасывает общий публичный демонстрационный стенд.


### Адрес источника входа за публичным пограничным прокси

Публичный репозиторий не содержит `deploy/demo/compose.yaml` или активной конфигурации Caddy/Nginx. В старой закрытой рабочей копии также нет отслеживаемой конфигурации пограничного прокси. Dockerfile API в этом репозитории запускают Uvicorn с `--no-proxy-headers`, но параметр `command` в закрытом Compose может изменить это поведение. Команда запуска развёрнутого Uvicorn, правила заголовков, доступность API и фактический адрес прокси остаются непроверенными по материалам репозитория.

Оставьте `LOGIN_TRUSTED_PROXY_NETWORKS` пустым, пока владелец развёртывания не проверит все условия по работающей конфигурации:

1. Uvicorn запускается с `--no-proxy-headers`; обработчик прокси-заголовков не подменяет `request.client.host` до чтения непосредственного транспортного узла ограничителем входа.
2. Недоверенные клиенты не могут подключаться к API напрямую в обход пограничного прокси.
3. Пограничный прокси удаляет или перезаписывает входящий `X-Real-IP`, затем задаёт проверенный адрес клиента. Если перед Caddy/Nginx находится балансировщик, доверие к нему задаётся явно, а адрес клиента определяется по проверенной цепочке прокси. Входящий `X-Forwarded-For` удаляется; приложение не использует его для ограничения попыток входа.
4. `LOGIN_TRUSTED_PROXY_NETWORKS` содержит только закреплённый адрес непосредственного прокси `/32` или `/128`, без общей подсети Docker или широкого диапазона прокси.

При прямом подключении клиента к Caddy обработчик обратного прокси может формировать `X-Real-IP` из `{client_ip}` и удалять `X-Forwarded-For`. Эти настройки необходимо проверить или добавить в закрытый Caddyfile; их наличие в текущем развёртывании не подтверждено. Если адрес прокси нельзя закрепить и изолировать, оставьте настройку пустой: ограничитель использует транспортный адрес, а клиенты за общим прокси делят один счётчик. По умолчанию приложение не доверяет адресам, переданным клиентом.
