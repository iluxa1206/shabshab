# Multi-stage build для Floaters Desk.
# Stage 1: сборка React-фронта (Vite) → dist/. Stage 2: Python-рантайм с uvicorn.

# --- Stage 1: frontend build ---
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci
COPY frontend-react/ ./
RUN npm run build

# --- Stage 2: python runtime ---
FROM python:3.12-slim AS runtime
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код бэкенда (dockerignore отсекает лишнее).
COPY . .

# React-билд из stage 1 (перекрывает любой локальный dist).
COPY --from=frontend /build/dist ./frontend-react/dist

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
