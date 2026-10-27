#!/usr/bin/env bash
# Image entrypoint: start an embedded Redis by default, then the city service
# (or the command given). If TERASIM_REDIS_HOST points at an external Redis, the
# embedded one is skipped (same image, env-only switch, no rebuild).
#
# Runs as an unprivileged user with an arbitrary uid (OpenShift restricted-v2):
# nothing here needs root, the embedded Redis binds 6379 (> 1024) and keeps its
# pid file / working directory under /tmp so a read-only root filesystem works
# (mount a writable /tmp there, e.g. an emptyDir).
set -e

echo "[entrypoint] running as uid=$(id -u) gid=$(id -g)"

REDIS_HOST="${TERASIM_REDIS_HOST:-127.0.0.1}"
case "$REDIS_HOST" in
  localhost | 127.0.0.1 | ::1 | "")
    echo "[entrypoint] embedded Redis mode (single container)"
    redis-server --daemonize yes --save "" --appendonly no \
                 --dir /tmp --pidfile /tmp/redis.pid --bind 127.0.0.1 -::1
    redis_ready=0
    for _ in $(seq 1 40); do
      if redis-cli ping >/dev/null 2>&1; then redis_ready=1; break; fi
      sleep 0.5
    done
    if [ "$redis_ready" -ne 1 ]; then
      echo "[entrypoint] ERROR: embedded Redis did not become ready after 20s" >&2
      exit 1
    fi
    ;;
  *)
    echo "[entrypoint] external Redis mode: ${REDIS_HOST}:${TERASIM_REDIS_PORT:-6379} (embedded Redis skipped)"
    ;;
esac

if [ "$#" -gt 0 ]; then
  exec "$@"
else
  exec terasim-service
fi
