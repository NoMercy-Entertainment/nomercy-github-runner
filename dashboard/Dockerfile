# Runner control dashboard.
#
# A built image rather than `apk add` on every container start: faster boot,
# reproducible, and no dependency on the network being up when the container
# restarts.
FROM python:3.12-alpine

RUN apk add --no-cache docker-cli bash coreutils tzdata \
 && pip install --no-cache-dir flask==3.0.3

WORKDIR /app
COPY app.py docker_ops.py ./
COPY templates/ ./templates/

ENV PYTHONUNBUFFERED=1 \
    DASH_PORT=9200 \
    DASH_DATA=/data

EXPOSE 9200
CMD ["python", "-u", "app.py"]
