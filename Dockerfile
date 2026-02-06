ARG ARCH=""
FROM ${ARCH}python:3.11-slim

ADD /src /src

RUN pip install --no-cache-dir -r /src/requirements.txt

RUN adduser --system --no-create-home secretmonkey
USER secretmonkey

CMD ["kopf", "run", "--liveness=http://0.0.0.0:8080/healthz", "-A", "/src/handlers.py"]
