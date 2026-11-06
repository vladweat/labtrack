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

Дополнительные команды и поведение PWA описаны в [web README](../web/README.md). Перед использованием новых локальных данных проверьте синтетическую границу в [данных и целостности](data-and-integrity.md).
