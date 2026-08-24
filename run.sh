#!/usr/bin/env bash
#
# Start the whole project with one command.
#
#   ./run.sh          backend on :8000, frontend on :5173
#   Ctrl+C            stops both
#
# Ports can be overridden:  BACKEND_PORT=8001 FRONTEND_PORT=5174 ./run.sh
#
# uvicorn and vite are invoked directly rather than through `npm run` or an
# activated venv: one process each, so Ctrl+C reaches them instead of stopping
# a wrapper and orphaning the real server.

set -uo pipefail

cd "$(dirname "$0")"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

backend_pid=""
frontend_pid=""
shutting_down=0

shutdown() {
    [ "$shutting_down" -eq 1 ] && return
    shutting_down=1
    trap - INT TERM EXIT

    printf '\n\033[33mОстанавливаю...\033[0m\n'
    for pid in "$frontend_pid" "$backend_pid"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done

    # Give them a moment to exit cleanly, then insist.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        still_running=0
        for pid in "$frontend_pid" "$backend_pid"; do
            [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && still_running=1
        done
        [ "$still_running" -eq 0 ] && break
        sleep 0.3
    done
    for pid in "$frontend_pid" "$backend_pid"; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    done

    printf '\033[33mОстановлено.\033[0m\n'
    exit 0
}
trap shutdown INT TERM EXIT

die() {
    printf '\033[31mОшибка:\033[0m %s\n' "$1" >&2
    exit 1
}

port_busy() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# --- prerequisites -------------------------------------------------------

[ -x venv/bin/uvicorn ] || die "не найден venv/bin/uvicorn. Создайте окружение и выполните: pip install -r requirements.txt"
[ -f .env ] || die "нет файла .env. Скопируйте шаблон: cp .env.example .env — и впишите TG_API_ID и TG_API_HASH"

if [ ! -d frontend/node_modules ]; then
    printf '\033[36mУстанавливаю зависимости фронтенда (только первый раз)...\033[0m\n'
    (cd frontend && npm install) || die "npm install завершился неудачей"
fi

port_busy "$BACKEND_PORT" && die "порт $BACKEND_PORT уже занят. Освободите его или запустите: ./stop.sh"
port_busy "$FRONTEND_PORT" && die "порт $FRONTEND_PORT уже занят. Освободите его или запустите: ./stop.sh"

# --- start ---------------------------------------------------------------

printf '\033[36mЗапускаю backend на порту %s...\033[0m\n' "$BACKEND_PORT"
venv/bin/uvicorn backend.main:app --reload --port "$BACKEND_PORT" &
backend_pid=$!

# Wait for the API to answer before starting the UI, so the first page load
# does not race the backend and show a connection error.
for _ in $(seq 1 60); do
    kill -0 "$backend_pid" 2>/dev/null || die "backend завершился при запуске (смотрите вывод выше)"
    curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/dms" >/dev/null 2>&1 && break
    sleep 0.5
done

printf '\033[36mЗапускаю frontend на порту %s...\033[0m\n' "$FRONTEND_PORT"
(cd frontend && exec node_modules/.bin/vite --port "$FRONTEND_PORT" --strictPort) &
frontend_pid=$!

for _ in $(seq 1 60); do
    kill -0 "$frontend_pid" 2>/dev/null || die "frontend завершился при запуске (смотрите вывод выше)"
    curl -sf "http://localhost:${FRONTEND_PORT}/" >/dev/null 2>&1 && break
    sleep 0.5
done

printf '\n\033[32m  Готово.\033[0m  Интерфейс: \033[1mhttp://localhost:%s\033[0m\n' "$FRONTEND_PORT"
printf '           API:        http://localhost:%s/docs\n' "$BACKEND_PORT"
printf '           Остановить: Ctrl+C (или ./stop.sh из другого терминала)\n\n'

# Hold the script open; if either server dies on its own, stop the other too.
while :; do
    kill -0 "$backend_pid" 2>/dev/null  || { printf '\033[31mbackend остановился\033[0m\n'; break; }
    kill -0 "$frontend_pid" 2>/dev/null || { printf '\033[31mfrontend остановился\033[0m\n'; break; }
    sleep 1
done

shutdown
