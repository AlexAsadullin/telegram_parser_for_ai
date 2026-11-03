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
# GPU (linux branch): install ROCm + PyTorch(HIP) + openai-whisper for an
# AMD Radeon automatically (asks for sudo; idempotent):
#   ./run.sh --with-gpu         (or just run ./run.sh and answer "y")
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
        --with-gpu) : ;;  # handled in the GPU section below
        *) die "неизвестный аргумент: $arg (доступны --setup-only, --with-gpu)" ;;
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

# --- GPU (linux branch) ---------------------------------------------------
# Detect an AMD Radeon (RX 580 / gfx803). If ROCm is missing, run.sh can
# install it automatically (--with-gpu); otherwise it prints the concrete
# setup plan instead of silently running on CPU.

warn() {
    printf '\033[33mВНИМАНИЕ:\033[0m %s\n' "$1"
}

amd_gpu_present() {
    command -v lspci >/dev/null 2>&1 || return 1
    lspci -nn 2>/dev/null | grep -qiE 'VGA compatible controller.*\[1002:'
}

rocm_installed() {
    [ -d /opt/rocm ] && /opt/rocm/bin rocminfo >/dev/null 2>&1
}

hip_torch_installed() {
    venv/bin/python - >/dev/null 2>&1 <<'PYEOF'
import sys
try:
    import torch
except Exception:
    sys.exit(1)
sys.exit(0 if getattr(torch.version, "hip", None) else 1)
PYEOF
}

ubuntu_codename() {
    if command -v lsb_release >/dev/null 2>&1; then
        lsb_release -cs
    elif [ -r /etc/os-release ]; then
        . /etc/os-release
        case "${VERSION_CODENAME:-}" in
            noble|jammy) echo "$VERSION_CODENAME" ;;
            *) echo "" ;;
        esac
    else
        echo ""
    fi
}

groups_changed=0

add_user_groups() {
    if id -nG "$USER" | tr ' ' '\n' | grep -qx render && \
       id -nG "$USER" | tr ' ' '\n' | grep -qx video; then
        return 0
    fi
    info "Добавляю пользователя $USER в группы render,video..."
    sudo usermod -aG render,video "$USER" \
        || warn "не удалось добавить в группы — выполните вручную: sudo usermod -aG render,video $USER"
    groups_changed=1
}

install_rocm() {
    local codename deb url tmp
    command -v lspci >/dev/null 2>&1 || die "не найден lspci (установите пакет pciutils)"
    command -v sudo >/dev/null 2>&1 || die "для установки ROCm нужен sudo"
    command -v wget >/dev/null 2>&1 || die "не найден wget (установите: sudo apt install wget)"
    codename=$(ubuntu_codename)
    [ -n "$codename" ] || die "автоустановка ROCm поддерживает только Ubuntu 22.04 (jammy) и 24.04 (noble)"

    info "Устанавливаю ROCm для Ubuntu ${codename} (пакет amdgpu-install, ~2 ГБ)..."; 
    deb="amdgpu-install_6.2.60204-1_all.deb"
    url="https://repo.radeon.com/amdgpu-install/6.2.4/ubuntu/${codename}/${deb}"
    tmp="/tmp/${deb}"
    wget -q --show-progress -O "$tmp" "$url" || die "не скачался amdgpu-install с ${url}"
    sudo apt install -y "$tmp" || die "не удалось установить ${deb}"
    sudo amdgpu-install -y --usecase=rocm --no-dkms || die "amdgpu-install завершился неудачей"
    rm -f "$tmp"
    add_user_groups
    info "ROCm установлен."
}

install_hip_torch() {
    info "Устанавливаю PyTorch с HIP + openai-whisper в venv (~3–4 ГБ)..."
    venv/bin/pip install --index-url https://download.pytorch.org/whl/rocm6.2 \
        openai-whisper "torch>=2.4" torchaudio || die "pip install (rocm6.2) завершился неудачей"
    touch venv/.deps-installed
    info "PyTorch/HIP готов."
}

confirm() {
    # confirm "вопрос?" -> 0 если y/yes
    printf '%s [y/N] ' "$1"
    read -r ans || ans=""
    case "${ans,,}" in y|yes) return 0 ;; *) return 1 ;; esac
}

if amd_gpu_present; then
    # ./run.sh --with-gpu  →  non-interactive full setup (idempotent).
    if [ "${1:-}" = "--with-gpu" ]; then
        rocm_installed || install_rocm
        hip_torch_installed || install_hip_torch
        shift
    fi

    if ! rocm_installed; then
        printf '\n\033[33mНайдена видеокарта AMD, но ROCm не установлен — транскрибация пойдёт на CPU.\033[0m\n'
        if confirm "Установить ROCm сейчас? Требуется sudo (~2 ГБ)"; then
            install_rocm
        else
            printf '   Пропускаю. Позже: \033[36m./run.sh --with-gpu\033[0m\n'
        fi
    fi

    if rocm_installed && ! hip_torch_installed; then
        if confirm "Установить PyTorch(HIP) + openai-whisper в venv (~3–4 ГБ)?"; then
            install_hip_torch
        else
            printf '   Пропускаю. Без torch с HIP сборкой GPU использоваться не будет. Команда:\n'
            printf '     venv/bin/pip install --index-url https://download.pytorch.org/whl/rocm6.2 openai-whisper "torch>=2.4" torchaudio\n'
        fi
    fi

    if rocm_installed && hip_torch_installed; then
        info "GPU-окружение готово (ROCm + torch/HIP)."
        if [ "$groups_changed" -eq 1 ]; then
            printf '\n\033[33mГруппы добавлены, но действуют только после перелогина.\033[0m\n'
            printf 'Проверьте после перезагрузки: \033[36mrocminfo\033[0m (должна быть gfx803), затем снова ./run.sh\n'
            if ! confirm "Продолжить запуск прямо сейчас (может остаться на CPU)?"; then
                trap - INT TERM EXIT
                exit 0
            fi
        fi
    fi
fi

# Print whatever the detector still reports (e.g. vfio-passthrough warning).
# HSA_OVERRIDE_GFX_VERSION is exported for Polaris cards so the backend (and
# any ROCm child processes) inherit it without touching ~/.bashrc.
gpu_env=""
if amd_gpu_present && grep -qi 'polaris\|rx 5[0-8]0' <<<"$(lspci -nn 2>/dev/null)"; then
    export HSA_OVERRIDE_GFX_VERSION="${HSA_OVERRIDE_GFX_VERSION:-10.3.0}"
    export OCL_ENABLE_SYSTEM_SCOPE="${OCL_ENABLE_SYSTEM_SCOPE:-0}"
    info "Polaris (gfx803): экспортирую HSA_OVERRIDE_GFX_VERSION=$HSA_OVERRIDE_GFX_VERSION для этого запуска"
    gpu_env="HSA_OVERRIDE_GFX_VERSION=$HSA_OVERRIDE_GFX_VERSION OCL_ENABLE_SYSTEM_SCOPE=$OCL_ENABLE_SYSTEM_SCOPE"
fi
gpu_status=$(TG_API_ID=dummy TG_API_HASH=dummy env $gpu_env venv/bin/python -c '
import sys; sys.path.insert(0, ".")
from backend import gpu_detect as g
plan = g.setup_plan()
kind = plan.get("kind", "")
print(kind)
for s in plan.get("steps", []):
    print("  • " + s)
' 2>/dev/null || true)
gpu_kind="${gpu_status%%$'\n'*}"
case "$gpu_kind" in
    ready)
        info "GPU готов к транскрибации (ROCm/HIP активен)" ;;
    setup_required)
        printf '\n\033[33mТранскрибация пойдёт на CPU. Осталось настроить:\033[0m\n'
        printf '%s\n' "${gpu_status#*$'\n'}"
        printf '\033[36mИли выполните: ./run.sh --with-gpu (или перезапустите ./run.sh и согласитесь на установку)\033[0m\n\n' ;;
    passthrough)
        printf '\n\033[33m%s\033[0m\n\n' "$(printf '%s' "$gpu_status" | tail -n +2)" ;;
    no_amd_gpu|*)
        : ;; # silent when there is no AMD card here
esac

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
