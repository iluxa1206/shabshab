# Multi-stage build для Floaters Desk.
# Stage 1: сборка React-фронта (Vite) → dist/. Stage 2: Python-рантайм с uvicorn.

# --- Stage 1: frontend build ---
# node:24 (npm 11) — тем же npm, каким сгенерён package-lock.json. На node:20
# (npm 10) `npm ci` падал EUSAGE «Missing: esbuild@0.28.2 from lock file»:
# vitest 4 тянет собственный vite, и npm 10 разрешает это дерево иначе.
FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci
COPY frontend-react/ ./
RUN npm run build

# --- Stage 2: python runtime ---
FROM python:3.12-slim AS runtime
WORKDIR /app

# Таймзона контейнера = Москва. Без tzdata переменная TZ молча игнорируется и
# время остаётся UTC — а сервисный слой опирается на наивный date.today(), и в
# окне 00:00-03:00 МСК «сегодня» съезжало на вчера. Пакет ставим явно, чтобы
# фикс не зависел от того, что окажется в базовом образе.
# Знак по POSIX инвертирован: Etc/GMT-3 == UTC+3 == МСК.
ENV TZ=Etc/GMT-3
# fonts-dejavu-core — шрифт для картинок Telegram-дайджеста (кириллица + знак
# рубля). Без него Pillow отдаёт встроенный битмап, и подписи становятся
# квадратами; см. services/charts_png.fonts_ok.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata fonts-dejavu-core \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код бэкенда (dockerignore отсекает лишнее).
COPY . .

# React-билд из stage 1 (перекрывает любой локальный dist).
COPY --from=frontend /build/dist ./frontend-react/dist

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
