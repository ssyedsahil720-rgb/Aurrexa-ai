FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/instance \
    && useradd --system --create-home --uid 10001 aurrexa \
    && chown -R aurrexa:aurrexa /app

USER aurrexa
EXPOSE 3000

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-3000} --workers 1 --threads 4 --timeout 90 --access-logfile - --error-logfile - main:app"]
