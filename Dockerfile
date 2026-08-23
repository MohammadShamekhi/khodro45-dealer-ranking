FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG JFROG_USER
ARG JFROG_PASS

COPY requirements.txt .
RUN pip install \
    --index-url "https://${JFROG_USER}:${JFROG_PASS}@artifact.technical45.com/artifactory/api/pypi/pypi-local/simple" \
    --extra-index-url "https://repo.hmirror.ir/python/simple" \
    --trusted-host "artifact.technical45.com" \
    -r requirements.txt
COPY . .
    

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "8", "--no-access-log"]
