#!/usr/bin/env bash
#
# Start the whole project with one command.
#
#   ./run.sh          backend on :8000, frontend on :5173
#   Ctrl+C            stops both
#
# Ports can be overridden:  BACKEND_PORT=8001 FRONTEND_PORT=5174 ./run.sh
# Setup only (venv, .env, npm deps, SQLite db), without starting servers:
#   ./run.sh --setup-only        (alias: SETUP_ONLY=1 ./run.sh)
#
# uvicorn and vite are invoked directly rather than through `npm run` or an
# activated venv: one process each, so Ctrl+C reaches them instead of stopping
# a wrapper and orphaning the real server.

set -uo pipefail

cd "$(dirname "$0")"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

SETUP_ONLY="${SETUP_ONLY:-0}"

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

# --- prerequisites -------------------------------------------------------

die() {
    printf '\033[31mОшибка:\033[0m %s\n' "$1" >&2
    exit 1
}

info() {
    printf '\033[36m%s\033[0m\n' "$1"
}

port_busy() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# Command-line arguments are parsed after the helpers above are defined.
for arg in "$@"; do
    case "$arg" in
        --setup-only) SETUP_ONLY=1 ;;
        *) die "неизвестный аргумент: $arg (доступен только --setup-only)" ;;
    esac
done

# Python virtual environment (gitignored — created here on first run).
if [ ! -x venv/bin/python ]; then
    info "Создаю виртуальное окружение venv/ (только первый раз)..."
    python3 -m venv venv || die "не удалось создать venv (установлен python3?)"
fi
venv/bin/python -m pip install --quiet --upgrade pip || die "не удалось обновить pip"

# Dependencies. Stamp file tracks requirements.txt so reinstall happens only
# when it changes.
if [ ! -x venv/bin/uvicorn ] || [ requirements.txt -nt venv/.deps-installed ]; then
    info "Устанавливаю зависимости бэкенда (requirements.txt)..."
    venv/bin/pip install -r requirements.txt || die "pip install завершился неудачей"
    touch venv/.deps-installed
fi

# .env is gitignored and holds the Telegram API credentials.
if [ ! -f .env ]; then
    if [ "$SETUP_ONLY" -eq 1 ]; then
        info "Нет файла .env — копирую шаблон: cp .env.example .env"
        cp .env.example .env
        printf '\033[33mВНИМАНИЕ:\033[0m заполните TG_API_ID и TG_API_HASH в .env (получить: https://my.telegram.org)\n'
    else
        die "нет файла .env. Скопируйте шаблон: cp .env.example .env — и впишите TG_API_ID и TG_API_HASH"
    fi
fi

# Interactive credential entry (run.sh is interactive by design, so this does
# not block automated runs).
if grep -q 'your_api_id_here' .env; then
    printf '\nВ файле .env ещё не заполнены Telegram API credentials.\n'
    printf 'Получите их на https://my.telegram.org (API development tools).\n'
    read -r -p "Ввести TG_API_ID и TG_API_HASH сейчас? [y/N] " answer || true
    if [ "${answer,,}" = "y" ] || [ "${answer,,}" = "yes" ]; then
        read -r -p "TG_API_ID: " tg_id
        read -r -s -p "TG_API_HASH: " tg_hash; echo
        sed -i.bak "s/^TG_API_ID=.*/TG_API_ID=${tg_id}/; s/^TG_API_HASH=.*/TG_API_HASH=${tg_hash}/" .env && rm -f .env.bak
        info "Сохранено в .env"
    fi
fi

if [ ! -d frontend/node_modules ]; then
    command -v npm >/dev/null 2>&1 || die "не найден npm — установите Node.js, затем повторите запуск"
    info "Устанавливаю зависимости фронтенда (только первый раз)..."
    (cd frontend && npm install) || die "npm install завершился неудачей"
fi

# SQLite database + tables (backend/history.db by default, path from DB_PATH
# in .env). Normally the backend creates it on startup; doing it here means the
# db exists even before the first run and survives without starting servers.
info "Инициализирую базу данных..."
if TG_API_ID=dummy TG_API_HASH=dummy venv/bin/python -m backend.init_db >/dev/null 2>&1; then
    : # ok, dummy credentials are enough just to create the schema
else
    # Dummy run failed for a real reason (broken venv, bad DB_PATH…) —
    # retry with the project's .env and full output.
    venv/bin/python -m backend.init_db || die "не удалось инициализировать базу данных"
fi
if grep -q '^DB_PATH=' .env; then
    _dbmsg=$(sed -n 's/^DB_PATH=//p' .env | head -n1)
else
    _dbmsg="backend/history.db"
fi
info "База данных готова: ${_dbmsg}"

if [ "$SETUP_ONLY" -eq 1 ]; then
    printf '\n\033[32m  Готово.\033[0m  Окружение настроено: venv/, .env, node_modules, база данных.\n'
    printf '           Запустите серверы командой: ./run.sh\n\n'
    trap - INT TERM EXIT
    exit 0
fi

port_busy "$BACKEND_PORT" && die "порт $BACKEND_PORT уже занят. Освободите его или запустите: ./stop.sh"
port_busy "$FRONTEND_PORT" && die "порт $FRONTEND_PORT уже занят. Освободите его или запустите: ./stop.sh"

# --- start ---------------------------------------------------------------

info "Запускаю backend на порту $BACKEND_PORT..."
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
