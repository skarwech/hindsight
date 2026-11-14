#!/bin/sh
# Local MariaDB 12.3 for Hindsight (stands in for the plan's `docker run mariadb:lts`; no Docker here).
#   db/server.sh start | stop | status | sql [args]
# Idempotent: `start` initialises the datadir on first run, then applies db/bootstrap.sql.
# ponytail: one process, project-local datadir; move to docker-compose if the team needs it.
set -e
cd "$(dirname "$0")/.."
BIN="$(brew --prefix mariadb@12.3)/bin"
DATA="$PWD/.mariadb/data"            # this path has spaces: always quoted
SOCK=/tmp/hindsight-mdb.sock          # macOS caps socket paths at 104 bytes; this folder's path is longer
PORT=${DB_PORT:-3306}
ME=$(whoami)
admin() { "$BIN/mariadb-admin" --no-defaults --socket=$SOCK -u "$ME" "$@"; }

case "$1" in
start)
  mkdir -p logs
  if [ ! -d "$DATA/mysql" ]; then
    mkdir -p "$DATA"
    "$BIN/mariadb-install-db" --no-defaults --datadir="$DATA" >/dev/null
  fi
  if ! admin ping >/dev/null 2>&1; then
    nohup "$BIN/mariadbd" --no-defaults --datadir="$DATA" --port="$PORT" --bind-address=127.0.0.1 \
      --socket=$SOCK --log-error="$PWD/logs/mariadb.err" \
      --max-allowed-packet=256M --innodb-buffer-pool-size=4G \
      --character-set-server=utf8mb4 --default-time-zone=+00:00 >/dev/null 2>&1 &
    i=0; until admin ping >/dev/null 2>&1; do
      i=$((i+1)); [ $i -gt 60 ] && { echo "MariaDB did not start; see logs/mariadb.err"; exit 1; }; sleep 0.5; done
  fi
  "$BIN/mariadb" --no-defaults --socket=$SOCK -u "$ME" < db/bootstrap.sql
  echo "MariaDB $("$BIN/mariadb" --no-defaults --socket=$SOCK -u "$ME" -N -e 'SELECT VERSION()') on 127.0.0.1:$PORT"
  ;;
stop)   admin shutdown ;;
status) admin status ;;
sql)    shift; exec "$BIN/mariadb" --no-defaults -h 127.0.0.1 -P "$PORT" -u hindsight -phindsight "$@" ;;
*)      echo "usage: db/server.sh start|stop|status|sql"; exit 1 ;;
esac
