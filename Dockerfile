FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY job_tracker ./job_tracker
# Default config baked in as a fallback; docker-compose mounts the live one
# over it so edits don't need a rebuild.
COPY config.toml ./

ENTRYPOINT ["python", "-m", "job_tracker"]
