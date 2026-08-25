# Runner control dashboard.
#
# A built image rather than `apk add` on every container start: faster boot,
# reproducible, and no dependency on the network being up when the container
# restarts.
FROM python:3.12-alpine

RUN apk add --no-cache docker-cli bash coreutils tzdata \
 && pip install --no-cache-dir flask==3.0.3 flask-sock==0.7.0

WORKDIR /app
COPY app.py docker_ops.py forgejo_api.py github_api.py history.py oidc.py providers.py runner_detail.py users.py ./
COPY templates/ ./templates/

ENV PYTHONUNBUFFERED=1 \
    DASH_PORT=9200 \
    DASH_DATA=/data

EXPOSE 9200
CMD ["python", "-u", "app.py"]
