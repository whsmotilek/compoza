FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/compoza
COPY core/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home compoza
COPY core ./core
COPY app ./app
USER 10001:10001
ENV COMPOZA_DB=/data/core.sqlite3 COMPOZA_STATIC_DIR=/srv/compoza/app
EXPOSE 8000
CMD ["uvicorn", "core.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "172.19.0.5"]
