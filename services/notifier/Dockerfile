# orbit-notifier: the outbox + sink adapters. python:slim; psycopg[binary] is the only
# dependency (CNPG Postgres client), everything else is standard library.
FROM python:3.13-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.3.4"
RUN useradd --uid 10001 --create-home orbit
USER 10001
ENV HOME=/home/orbit
COPY app.py /app/app.py
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/app/app.py"]
