# Локальная разработка

## Требования

- Python 3.12
- Node.js 22.13+ в линейке Node 22 и npm 10
- Docker Compose

Используйте `.env.example` только для локальной разработки и задайте локальные значения до запуска Compose. Не используйте их вне своей машины.

## Инфраструктура

Для API и основного workflow достаточно PostgreSQL. Из корня репозитория:

```bash
test -f .env || cp .env.example .env
docker compose up -d postgres
```

Compose также описывает Redis, worker и Beat для best-effort фоновых уведомлений; они не нужны для локального входа или основного workflow.

## Backend

Из `backend/` установите зависимости, примените миграции и запустите API:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
```

API доступен на `http://127.0.0.1:8000`; интерактивная схема — `/docs`. Аккаунты и scoped membership настраиваются оператором через CLI из `backend/`, публичного API администрирования нет. Примеры CLI и настройки ролей: [backend README](../backend/README.md).

## Web

В отдельном терминале:

```bash
cd web
npm ci
npm run dev
```

Vite перенаправляет `/api/v1` к локальному backend. Команда `npm run check` выполняет web lint, unit checks, TypeScript build и браузерные проверки. Отдельная локальная browser/API acceptance запускается командой `npm run test:acceptance:local`; это отдельный сценарий с disposable PostgreSQL.

### Сквозной Sender → Courier → Lab acceptance

Этот real browser/API-сценарий запускает FastAPI и Vite локально и проверяет
сценарий на новой синтетической PostgreSQL. Он не подключается к внешней или
демо-БД. Скрипт создаёт контейнер `postgres:15` без сети и опубликованных
портов, использует Unix socket и удаляет контейнер, сокет и временные файлы
через `trap`. Образ PostgreSQL должен быть заранее скачан: сам скрипт никогда
не выполняет `docker pull`.

На Linux установите зависимости и запустите сценарий из корня репозитория:

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd ../web
npm ci
npx playwright install --with-deps chromium
docker pull postgres:15
npm run test:acceptance:local
```

Скрипт сам использует `backend/.venv/bin/python`, `web/node_modules` и кешированный
браузер. Для нестандартных путей задайте `LABTRACK_PYTHON_BIN` и
`LABTRACK_WEB_NODE_MODULES`. При сбое локальный запуск показывает этап, где
остановился runner, а затем диагностические сообщения Playwright и API с
редакцией сгенерированных email и паролей. Для проверки пропущенных зависимостей
сначала убедитесь, что доступны Docker, Python 3.12, Node 22, Chromium и образ
`postgres:15`. В CI вывод сценария намеренно скрыт, а artifacts не загружаются;
выделенный workflow `.github/workflows/real-acceptance.yml` запускает эту же
команду на соответствующих PR. Если CI job завершается ошибкой, повторите
команду локально, чтобы изучить диагностику. Не публикуйте непроверенные логи:
они могут содержать синтетические идентификаторы или тела ответов.

Дополнительные команды и поведение PWA описаны в [web README](../web/README.md). Перед использованием новых локальных данных проверьте синтетическую границу в [данных и целостности](data-and-integrity.md).

Локальный synthetic PostgreSQL backup/restore drill, его ограничения и точные
команды описаны в [руководстве backup/restore](backup-restore.md). Он требует
отдельный demo sandbox и не касается public demo.
