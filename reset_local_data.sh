#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGETS=(
  "$ROOT_DIR/server/db"
  "$ROOT_DIR/client/auth"
  "$ROOT_DIR/client/chats"
  "$ROOT_DIR/client/keys"
)

echo "Se borraran estos directorios de datos locales:"
for target in "${TARGETS[@]}"; do
  echo "  - $target"
done

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Continuar? [y/N]: " answer
  case "$answer" in
    y|Y|yes|YES)
      ;;
    *)
      echo "Operacion cancelada."
      exit 0
      ;;
  esac
fi

for target in "${TARGETS[@]}"; do
  rm -rf "$target"
done

echo "Datos locales eliminados."
