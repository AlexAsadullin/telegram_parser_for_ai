#!/usr/bin/env bash
#
# Stop whatever run.sh left behind.
#
# Ctrl+C in the run.sh terminal is the normal way to stop; this is the fallback
# for a closed terminal or a server that outlived its parent.
#
#   ./stop.sh
#   BACKEND_PORT=8001 FRONTEND_PORT=5174 ./stop.sh

set -uo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

stopped=0

stop_port() {
    local port="$1" label="$2" pids
    pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)"

    if [ -z "$pids" ]; then
        printf '  %-9s порт %s свободен\n' "$label" "$port"
        return
    fi

    for pid in $pids; do
        kill "$pid" 2>/dev/null
    done

    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)"
        [ -z "$pids" ] && break
        sleep 0.3
    done

    for pid in $pids; do
        kill -9 "$pid" 2>/dev/null
    done

    printf '  \033[32m%-9s остановлен (порт %s)\033[0m\n' "$label" "$port"
    stopped=1
}

printf '\033[36mОстанавливаю проект...\033[0m\n'
stop_port "$FRONTEND_PORT" "frontend"
stop_port "$BACKEND_PORT" "backend"

if [ "$stopped" -eq 1 ]; then
    printf '\033[32mГотово.\033[0m\n'
else
    printf 'Нечего останавливать.\n'
fi
