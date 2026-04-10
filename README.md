# Tender Parser / Парсер товаров по заявке

**Live demo:** https://parcer.bau-meister.ru

A web application for automated product search based on procurement tender documents. Upload a tender specification (Excel/Word), and the system extracts every line item, searches Yandex Shopping for matching products, optionally enriches results with GigaChat AI analysis, and generates a ready-to-use Excel report.

---

Веб-приложение для автоматического поиска товаров по тендерным заявкам. Загрузите спецификацию (Excel/Word) — система извлечёт позиции, выполнит поиск по Яндекс Маркету, при необходимости обогатит данные через GigaChat и сформирует Excel-отчёт.

## Features / Возможности

- **Multi-user** — roles: `root`, `admin`, `user`
- **File parsing** — Excel (`.xlsx`, `.xls`) and Word (`.docx`) tender specs
- **Yandex Shopping search** — sync and async search API
- **GigaChat AI** — product name normalization and description generation
- **Excel reports** — per-tender downloadable reports
- **Per-user GigaChat tokens** — each user can configure their own API key

## Tech Stack

- **Backend:** Python 3.11, FastAPI, SQLite
- **AI:** Sber GigaChat API (optional), Yandex Search API
- **Auth:** JWT (cookies), bcrypt
- **Frontend:** Jinja2 templates, plain JS

## Quick Start

```bash
pip install -r requirements.txt

# Set environment variables (or create .env)
export SECRET_KEY=your-random-secret
export ROOT_PASSWORD=your-admin-password   # default: admin

uvicorn main:app --host 0.0.0.0 --port 8000
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | JWT signing key | `change-me-in-production` |
| `ROOT_PASSWORD` | Initial root user password | `admin` |

## API Keys (configured in UI per user)

- **Yandex Search API** — set in admin panel
- **GigaChat API** — each user sets their own token in profile
