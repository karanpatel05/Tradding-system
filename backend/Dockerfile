# Hosted image: build the React UI, then run FastAPI serving the API + that UI
# same-origin. Build from the REPO ROOT (the compose file sets context: .) so both
# frontend/ and backend/ are available.

# --- Stage 1: build the React UI ---
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build                 # production build is same-origin (see frontend/src/api.js)

# --- Stage 2: FastAPI backend (serves the UI same-origin via FRONTEND_DIST) ---
FROM python:3.14-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=ui /ui/dist ./frontend_dist
ENV FRONTEND_DIST=/app/frontend_dist

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
