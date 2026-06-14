#!/bin/sh
set -eu

LOG_DIR="${FLOCK_LOG_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/logs}"
MODE="all"
RAW="0"

for arg in "$@"; do
  case "$arg" in
    --raw)
      RAW="1"
      ;;
    server|client|all)
      MODE="$arg"
      ;;
    *)
      echo "Use: $0 [server|client|all] [--raw]" >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  server)
    FILES="$LOG_DIR/server.log"
    ;;
  client)
    FILES="$LOG_DIR/client.log $LOG_DIR/console.log"
    ;;
  *)
    FILES="$LOG_DIR/server.log $LOG_DIR/client.log $LOG_DIR/console.log"
    ;;
esac

existing_files=""
for file in $FILES; do
  if [ -f "$file" ]; then
    existing_files="$existing_files $file"
  fi
done

if [ -z "$existing_files" ]; then
  echo "No log files found in $LOG_DIR yet." >&2
  exit 1
fi

if [ "$RAW" = "1" ] || ! command -v jq >/dev/null 2>&1; then
  tail -q -F $existing_files
  exit 0
fi

tail -q -F $existing_files | jq -r '
  def show($v): if $v == null then "-" elif ($v|type) == "object" or ($v|type) == "array" then ($v|tojson) else ($v|tostring) end;
  [
    (.timestamp // "-"),
    (.level // "-"),
    (.component // "-"),
    (.node // "-"),
    (.event // "-"),
    (.peer // "-"),
    (.username // "-"),
    show(.range),
    show(.result)
  ] | @tsv
'
