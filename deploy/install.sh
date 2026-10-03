#!/usr/bin/env bash
# SvBG Shop: установщик для Ubuntu 22.04 / 24.04 и Debian 12.
#
#   git clone https://github.com/BiggSm0ke/svbg-shop.git
#   cd svbg-shop && sudo bash deploy/install.sh
#
# Меню: всё с нуля (Docker, панель Remnawave, страница подписки, Caddy, бот) / бот рядом с панелью на этом
# сервере / только бот (панель на другом сервере) / обновить / статус / удалить.
# Без меню: install.sh --update [--force] | --status (так их вызывает команда svbg).
#
# Образ бота скачивается готовым из ghcr.io (его собирает GitHub Actions для amd64 и arm64). На сервере он
# собирается, только если так выбрал владелец (SVBG_BUILD_LOCAL=1 или пункт меню) или скачать не вышло.
# Свой тег, например закреплённую версию: SVBG_IMAGE=ghcr.io/biggsm0ke/svbg-shop:1.2.0. Оба выбора
# запоминаются в install.conf.
#
# Повторный запуск ничего не ломает: готовое не пересоздаётся, секреты не перегенерируются.
# Лог: /var/log/svbg-install.log. Токены и пароли не пишутся ни в лог, ни на экран и не попадают в командную
# строку процессов (её видно через ps): curl, jq, awk и контейнер получают их через stdin или окружение.
set -Eeuo pipefail
umask 022

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SRC_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
SVBG_DIR=${SVBG_DIR:-/opt/svbg}
DATA=$SVBG_DIR/data
STATE=$SVBG_DIR/install.conf
SVBG_BIN=${SVBG_BIN:-/usr/local/bin/svbg}
RW_DIR=${RW_DIR:-/opt/remnawave}
SUB_DIR=$RW_DIR/subscription
CADDY_DIR=${CADDY_DIR:-/opt/caddy}
LOG=${SVBG_LOG:-/var/log/svbg-install.log}
IMAGE_DEFAULT=ghcr.io/biggsm0ke/svbg-shop:latest
LOCAL_IMAGE=svbg-shop:local
PREV_IMAGE=svbg-shop:previous
LOW_MEM=1800
RW_RAW=https://raw.githubusercontent.com/remnawave/backend/refs/heads/main
BOT_HOOK=http://svbg-shop:8080/webhooks/remnawave
# Секрет вебхуков из .env.sample панели: он публичный, оставлять его нельзя.
RW_SAMPLE_SECRET=vsmu67Kmg6R8FjIOF1WUY8LWBHie4scdEqrfsKmyf4IAf8dY3nFS0wwYHkhh6ZvQ
BT="SvBG Shop · установка"
WH=22
WW=76
export DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0

# Выбор владельца. Сохраняется в install.conf (секретов там нет).
# IMAGE: откуда скачивать образ; BUILD_LOCAL=1: собирать его на этом сервере.
MODE="" NET="" DB_MODE="" PANEL_DOMAIN="" SUB_DOMAIN="" BOT_DOMAIN="" RW_URL="" BOT_NAME="" RW_TOKEN_SET="" SVBG_SRC=""
IMAGE="" BUILD_LOCAL=""
STATE_KEYS=(MODE NET DB_MODE PANEL_DOMAIN SUB_DOMAIN BOT_DOMAIN RW_URL BOT_NAME RW_TOKEN_SET SVBG_SRC IMAGE BUILD_LOCAL)
# Образ, который сейчас пойдёт в compose: $IMAGE или svbg-shop:local после сборки.
BOT_IMAGE=""
# Секреты: живут только в памяти этого запуска.
BOT_TOKEN="" RW_TOKEN="" RW_AUTH="" ADMIN_USER="" ADMIN_PASS="" HOOK_SECRET="" PG_PASS="" DATABASE_URL=""
OWNER_ID="" ADMIN_CHAT="" ADMIN_ACTION="" FIRST_INSTALL="" FORCE=""
RW_API="" RW_CODE="" RW_OUT="" CADDYFILE="" CADDY_C="" CADDY_NEW="" CADDY_HOSTNET="" PROXY_NOTE=""

# ================================================================================================ вывод и лог

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null || true; }
step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; log "== $*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; log "ok: $*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; log "warn: $*"; }
die() {
    printf '\n\033[31m✗ %s\033[0m\n  Лог: %s\n' "$*" "$LOG" >&2
    log "error: $*"
    exit 1
}
# Команда без секретов в аргументах; её вывод уходит только в лог.
run() {
    log "\$ $*"
    "$@" >>"$LOG" 2>&1
}

on_err() {
    local rc=$? line=$1
    trap - ERR
    log "сбой: код $rc, строка $line"
    printf '\n\033[31m✗ Установка остановилась (строка %s, код %s).\033[0m\n' "$line" "$rc" >&2
    printf '  Хвост лога %s:\n' "$LOG" >&2
    tail -n 12 "$LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
    printf '  Исправьте причину и запустите установщик ещё раз: готовые шаги он пропустит.\n' >&2
    exit "$rc"
}

cleanup() { if [[ -n $RW_OUT ]]; then rm -f "$RW_OUT"; fi; }

start_log() {
    mkdir -p "$(dirname "$LOG")"
    touch "$LOG"
    chmod 600 "$LOG"
    log "---- install.sh $* (из $SRC_DIR)"
}

# ================================================================================================ диалоги

ui_init() {
    local l c
    l=$(tput lines 2>/dev/null || echo 24)
    c=$(tput cols 2>/dev/null || echo 80)
    WH=$((l > 26 ? 24 : l - 2))
    WW=$((c > 84 ? 78 : c - 4))
    if ((WH < 14 || WW < 56)); then
        die "Окно терминала маловато: растяните хотя бы до 60×16 и запустите снова."
    fi
    export NEWT_COLORS='
root=white,black
window=white,black
border=cyan,black
shadow=black,black
title=brightcyan,black
textbox=white,black
button=black,cyan
actbutton=black,brightcyan
compactbutton=white,black
listbox=white,black
actlistbox=black,cyan
actsellistbox=black,cyan
entry=white,black
disentry=gray,black
roottext=brightcyan,black
helpline=white,black'
}

confirm_quit() {
    if whiptail --backtitle "$BT" --title "Выйти?" --defaultno --yes-button "Выйти" --no-button "Вернуться" \
        --yesno "Прервать установку?

Что уже сделано, то останется. Следующий запуск продолжит с того же места." 12 "$WW"; then
        clear
        echo "Установка прервана."
        exit 1
    fi
}

ui_msg() {
    whiptail --backtitle "$BT" --title "$1" --scrolltext --msgbox "$2" "$WH" "$WW" || true
}

# ui_yesno "заголовок" "текст" [no] → 0 да, 1 нет
ui_yesno() {
    local def=()
    if [[ ${3-} == no ]]; then def=(--defaultno); fi
    whiptail --backtitle "$BT" --title "$1" "${def[@]}" --yes-button "Да" --no-button "Нет" --yesno "$2" "$WH" "$WW"
}

trim() {
    local s=$1
    s=${s#"${s%%[![:space:]]*}"}
    s=${s%"${s##*[![:space:]]}"}
    printf '%s' "$s"
}

# ask VAR "заголовок" "текст" [по умолчанию]
ask() {
    local _a_var=$1 _a_val
    while :; do
        if _a_val=$(whiptail --backtitle "$BT" --title "$2" --cancel-button "Выход" \
            --inputbox "$3" "$WH" "$WW" "${4-}" 3>&1 1>&2 2>&3); then
            printf -v "$_a_var" '%s' "$(trim "$_a_val")"
            return 0
        fi
        confirm_quit
    done
}

# ask_secret VAR "заголовок" "текст": ввод не виден; значение как есть, без обрезки пробелов
ask_secret() {
    local _s_var=$1 _s_val
    while :; do
        if _s_val=$(whiptail --backtitle "$BT" --title "$2" --cancel-button "Выход" \
            --passwordbox "$3" "$WH" "$WW" 3>&1 1>&2 2>&3); then
            printf -v "$_s_var" '%s' "$_s_val"
            return 0
        fi
        confirm_quit
    done
}

# choose VAR "заголовок" "текст" тег_по_умолчанию тег пункт [тег пункт]…
choose() {
    local _c_var=$1 _c_title=$2 _c_text=$3 _c_def=$4 _c_val _c_h
    shift 4
    _c_h=$(($# / 2))
    if ((_c_h > WH - 9)); then _c_h=$((WH - 9)); fi
    while :; do
        if _c_val=$(whiptail --backtitle "$BT" --title "$_c_title" --notags --default-item "$_c_def" \
            --cancel-button "Выход" --menu "$_c_text" "$WH" "$WW" "$_c_h" "$@" 3>&1 1>&2 2>&3); then
            printf -v "$_c_var" '%s' "$_c_val"
            return 0
        fi
        confirm_quit
    done
}

# ================================================================================================ мелочи

rand_hex() { openssl rand -hex "$1"; }

# env_get ФАЙЛ КЛЮЧ: значение без кавычек (пусто, если нет)
env_get() {
    [[ -f $1 ]] || return 0
    K=$2 awk -F= 'BEGIN { k = ENVIRON["K"]; q = sprintf("%c", 39) }
        $1 == k { v = substr($0, length(k) + 2) }
        END { if (v ~ /^".*"$/ || v ~ ("^" q ".*" q "$")) v = substr(v, 2, length(v) - 2); print v }' "$1"
}

env_has() { [[ -f $1 ]] && grep -q "^$2=" "$1"; }

# env_set ФАЙЛ КЛЮЧ ЗНАЧЕНИЕ: заменить строку или дописать. Значение идёт в awk через окружение, не через argv.
env_set() {
    local f=$1 tmp
    tmp=$(mktemp "$f.XXXXXX")
    K=$2 V=$3 awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
        index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }' "$f" >"$tmp"
    chmod --reference="$f" "$tmp"
    chown --reference="$f" "$tmp"
    mv -f "$tmp" "$f"
}

file_sum() { if [[ -f $1 ]]; then md5sum "$1" | cut -d' ' -f1; fi; }

container_running() { [[ -n $(docker ps -q --filter "name=^$1\$" 2>/dev/null) ]]; }
panel_present() { [[ -f $RW_DIR/docker-compose.yml && -f $RW_DIR/.env ]]; }
bot_running() { container_running svbg-shop; }
compose_bot() { docker compose -f "$SVBG_DIR/docker-compose.yml" "$@"; }

panel_port() {
    local p
    p=$(env_get "$RW_DIR/.env" APP_PORT)
    printf '%s' "${p:-3000}"
}

# ================================================================================================ проверки сервера

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Нужны права root: sudo bash ${BASH_SOURCE[0]}"
        exit 1
    fi
}

need_tty() {
    if [[ -t 0 ]]; then return 0; fi
    if { : </dev/tty; } 2>/dev/null; then
        exec </dev/tty
    else
        echo "Установщику нужен терминал (запустите его по SSH: sudo bash deploy/install.sh)."
        exit 1
    fi
}

check_os() {
    local id ver pretty answer
    # shellcheck source=/dev/null
    id=$(. /etc/os-release 2>/dev/null && echo "${ID:-}") || id=""
    # shellcheck source=/dev/null
    ver=$(. /etc/os-release 2>/dev/null && echo "${VERSION_ID:-}") || ver=""
    # shellcheck source=/dev/null
    pretty=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}") || pretty=""
    pretty=${pretty:-неизвестная система}
    case "$id:$ver" in
        ubuntu:22.04 | ubuntu:24.04 | debian:12) log "ОС: $pretty" ;;
        *)
            printf 'Система: %s. Проверял на Ubuntu 22.04/24.04 и Debian 12.\n' "$pretty"
            if [[ $id != ubuntu && $id != debian ]]; then
                echo "Нужен apt (Ubuntu или Debian). Остановился."
                exit 1
            fi
            read -r -p "Всё равно продолжить? [y/N] " answer
            if [[ $answer != [yYдД]* ]]; then exit 1; fi
            ;;
    esac
}

install_deps() {
    local pkgs=()
    command -v whiptail >/dev/null 2>&1 || pkgs+=(whiptail)
    command -v curl >/dev/null 2>&1 || pkgs+=(curl)
    command -v jq >/dev/null 2>&1 || pkgs+=(jq)
    command -v openssl >/dev/null 2>&1 || pkgs+=(openssl)
    command -v git >/dev/null 2>&1 || pkgs+=(git)
    command -v ss >/dev/null 2>&1 || pkgs+=(iproute2)
    [[ -f /etc/ssl/certs/ca-certificates.crt ]] || pkgs+=(ca-certificates)
    if ((${#pkgs[@]} == 0)); then return 0; fi
    echo "Ставлю пакеты: ${pkgs[*]}…"
    run apt-get -o DPkg::Lock::Timeout=300 update -q || die "apt-get update не прошёл, смотрите лог."
    run apt-get -o DPkg::Lock::Timeout=300 install -y -q "${pkgs[@]}" || die "Не поставились пакеты: ${pkgs[*]}"
}

mem_mb() { awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo; }
swap_mb() { awk '/^SwapTotal:/ { print int($2 / 1024) }' /proc/meminfo; }
free_mb() { df -Pm / | awk 'NR == 2 { print $4 }'; }

check_disk() { # check_disk МБ
    local need=$1 free
    free=$(free_mb)
    if ((free < need)); then
        die "Мало места на диске: свободно $((free / 1024)) ГБ, а нужно хотя бы $((need / 1024)) ГБ."
    fi
    ok "Место на диске: свободно $((free / 1024)) ГБ"
}

ensure_swap() {
    local mem size free
    mem=$(mem_mb)
    if ((mem >= LOW_MEM)); then
        ok "Память: $mem МБ, swap не нужен"
        return 0
    fi
    if (($(swap_mb) > 0)); then
        ok "Память: $mem МБ, swap уже есть ($(swap_mb) МБ)"
        return 0
    fi
    if [[ -f /swapfile ]] && run swapon /swapfile; then
        ok "Включил уже существующий /swapfile"
        return 0
    fi
    free=$(free_mb)
    size=2048
    if ((free - 4096 < size)); then size=$((free - 4096)); fi
    if ((size < 512)); then
        warn "Памяти $mem МБ, а места на диске под swap не хватает."
        return 0
    fi
    step "Памяти всего $mem МБ, добавляю swap на $size МБ (/swapfile)"
    if ! run fallocate -l "${size}M" /swapfile; then
        run dd if=/dev/zero of=/swapfile bs=1M count="$size"
    fi
    chmod 600 /swapfile
    run mkswap /swapfile
    run swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
    echo 'vm.swappiness=10' >/etc/sysctl.d/99-svbg-swap.conf
    run sysctl -p /etc/sysctl.d/99-svbg-swap.conf || true
    ok "swap включён и пропишется в fstab"
}

ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        run systemctl enable --now docker || true
        ok "Docker уже стоит: $(docker --version | sed 's/,.*//')"
        return 0
    fi
    step "Ставлю Docker (официальный скрипт get.docker.com, пара минут)"
    local f
    f=$(mktemp)
    curl -fsSL https://get.docker.com -o "$f" || die "Не скачался get.docker.com. Есть ли интернет на сервере?"
    run sh "$f" || die "Docker не установился, причина в логе."
    rm -f "$f"
    run systemctl enable --now docker
    docker compose version >/dev/null 2>&1 || die "Docker встал, но docker compose v2 не нашёлся."
    ok "Docker установлен"
}

ensure_network() {
    if ! docker network inspect "$1" >/dev/null 2>&1; then
        run docker network create "$1"
        ok "Создал сеть Docker $1"
    fi
}

open_ports() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        run ufw allow 80/tcp
        run ufw allow 443/tcp
        ok "ufw: открыл 80 и 443"
    fi
}

# ================================================================================================ домены и DNS

server_ip() {
    local ip u
    for u in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
        ip=$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]') || ip=""
        if [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            printf '%s' "$ip"
            return 0
        fi
    done
    ip route get 1.1.1.1 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }'
}

# A-записи домена (по строке). Сначала спрашиваем dns.google: у локального резолвера бывает старый кэш.
resolve_a() {
    local out
    out=$(curl -fsS --max-time 6 "https://dns.google/resolve?name=$1&type=A" 2>/dev/null |
        jq -r '.Answer[]? | select(.type == 1) | .data' 2>/dev/null) || out=""
    if [[ -z $out ]]; then
        out=$(getent ahostsv4 "$1" 2>/dev/null | awk '{ print $1 }' | sort -u) || out=""
    fi
    printf '%s' "$out"
}

valid_domain() { [[ $1 =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]]; }

# ask_domain VAR "заголовок" "текст" [по умолчанию] [optional]
ask_domain() {
    local _d_v
    while :; do
        ask _d_v "$2" "$3" "${4-}"
        _d_v=${_d_v,,}
        _d_v=${_d_v#https://}
        _d_v=${_d_v#http://}
        _d_v=${_d_v%%/*}
        if [[ -z $_d_v && ${5-} == optional ]]; then
            printf -v "$1" '%s' ""
            return 0
        fi
        if valid_domain "$_d_v"; then
            printf -v "$1" '%s' "$_d_v"
            return 0
        fi
        ui_msg "Адрес не подходит" "«$_d_v» не похоже на домен. Нужно что-то вроде bot.example.com: без https:// и без слэшей."
    done
}

# check_dns домен… → 0: всё указывает на сервер (или владелец решил продолжить); 1: хочет ввести другие адреса
check_dns() {
    local ip d recs bad act
    ip=$(server_ip) || ip=""
    if [[ -z $ip ]]; then
        warn "Не узнал внешний IP сервера, DNS не проверяю: $*"
        return 0
    fi
    while :; do
        bad=""
        for d in "$@"; do
            recs=$(resolve_a "$d")
            if ! grep -qxF "$ip" <<<"$recs"; then
                bad+="  $d → $(printf '%s' "${recs:-записи нет}" | paste -sd' ')"$'\n'
            fi
        done
        if [[ -z $bad ]]; then
            log "DNS ок: $* → $ip"
            return 0
        fi
        choose act "Проверка DNS" "IP этого сервера: $ip. Эти адреса на него пока не указывают:

$bad
Добавьте у регистратора A-запись на $ip для каждого. Обычно запись доходит за 5–30 минут.
Если домен в Cloudflare, поставьте «DNS only» (серое облако), иначе сертификат не выпустится." retry \
            retry "Проверить ещё раз" \
            go "Продолжить так (сертификат может не выпуститься)" \
            edit "Ввести другие адреса"
        case $act in
            go)
                log "DNS не совпал ($*), владелец продолжил"
                return 0
                ;;
            edit) return 1 ;;
        esac
    done
}

ask_domains_full() {
    local how base
    while :; do
        choose how "Домены" "Нужны три адреса, и все должны смотреть на IP этого сервера:

  panel.…  панель Remnawave
  sub.…    страница подписки для клиентов
  bot.…    вебхуки бота и касс

Как зададим?" base \
            base "У меня есть домен, поддомены panel/sub/bot подставь сам" \
            each "Введу каждый адрес сам"
        if [[ $how == base ]]; then
            ask_domain base "Домен" "Ваш домен, например example.com.
Будут адреса panel.example.com, sub.example.com и bot.example.com." "${PANEL_DOMAIN#panel.}"
            PANEL_DOMAIN=panel.$base SUB_DOMAIN=sub.$base BOT_DOMAIN=bot.$base
        else
            ask_domain PANEL_DOMAIN "Адрес панели" "Адрес панели Remnawave, например panel.example.com" "$PANEL_DOMAIN"
            ask_domain SUB_DOMAIN "Адрес страницы подписки" "Его увидят клиенты в ссылке подписки, например sub.example.com" "$SUB_DOMAIN"
            ask_domain BOT_DOMAIN "Адрес бота" "Сюда стучатся кассы и панель, например bot.example.com" "$BOT_DOMAIN"
        fi
        if [[ $PANEL_DOMAIN == "$SUB_DOMAIN" || $PANEL_DOMAIN == "$BOT_DOMAIN" || $SUB_DOMAIN == "$BOT_DOMAIN" ]]; then
            ui_msg "Адреса совпадают" "Нужны три разных адреса."
            continue
        fi
        if check_dns "$PANEL_DOMAIN" "$SUB_DOMAIN" "$BOT_DOMAIN"; then return 0; fi
    done
}

ask_bot_domain() { # ask_bot_domain [optional]
    local text="Поддомен для бота, например bot.example.com. Нужна A-запись на IP этого сервера.
Через него ходят вебхуки касс и панели; всё остальное снаружи закрыто."
    if [[ ${1-} == optional ]]; then
        text+="

Можно оставить пустым: бот будет работать, но кассы с вебхуками и вебхуки панели не подключатся, пока не появится адрес."
    fi
    while :; do
        ask_domain BOT_DOMAIN "Адрес бота" "$text" "$BOT_DOMAIN" "${1-}"
        if [[ -z $BOT_DOMAIN ]] || check_dns "$BOT_DOMAIN"; then return 0; fi
    done
}

# ================================================================================================ Telegram

# getMe: URL с токеном идёт в curl через stdin (-K -), а не через argv.
tg_username() {
    printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$BOT_TOKEN" |
        curl -fsS --max-time 15 -K - 2>/dev/null | jq -er '.result.username' 2>/dev/null
}

ask_bot_token() {
    local t act name
    if [[ -n $BOT_NAME && -s $DATA/.env ]]; then
        choose act "Токен бота" "Бот уже настроен: @$BOT_NAME" keep keep "Оставить этот токен" new "Ввести новый токен"
        if [[ $act == keep ]]; then return 0; fi
    fi
    while :; do
        ask_secret t "Токен бота" "Откройте @BotFather: /newbot (или /mybots → API Token) и вставьте токен сюда.
Выглядит так: 123456789:AAH4… Символы при вводе не видны, так и задумано."
        t=${t//[[:space:]]/}
        if [[ ! $t =~ ^[0-9]{5,15}:[A-Za-z0-9_-]{30,}$ ]]; then
            ui_msg "Не похоже на токен" "Токен состоит из цифр, двоеточия и примерно 35 символов. Скопируйте его из @BotFather целиком."
            continue
        fi
        BOT_TOKEN=$t
        if name=$(tg_username); then
            if ui_yesno "Токен подошёл" "Telegram узнал бота: @$name

Это он?"; then
                BOT_NAME=$name
                return 0
            fi
            continue
        fi
        choose act "Telegram не ответил" "Telegram не принял токен, или сервер не достучался до api.telegram.org.

Если токен точно верный (бывает, что Telegram на сервере заблокирован), оставьте его: бот проверит токен при запуске." retry \
            retry "Ввести ещё раз" \
            keep "Оставить этот токен"
        if [[ $act == keep ]]; then
            BOT_NAME=""
            return 0
        fi
    done
}

ask_owner() {
    local v
    while :; do
        ask v "Ваш Telegram ID (можно пропустить)" "Числовой ID владельца, его покажет @userinfobot.
Укажете — бот сразу будет знать, кто владелец.
Оставите пустым — в конце дам одноразовую ссылку: кто первым откроет, тот и владелец." "$OWNER_ID"
        if [[ -z $v || $v =~ ^[0-9]{4,15}$ ]]; then
            OWNER_ID=$v
            break
        fi
        ui_msg "Не похоже на ID" "ID состоит только из цифр, например 123456789."
    done
    while :; do
        ask v "Админ-группа (можно пропустить)" "ID группы с темами для уведомлений, вида -1001234567890.
Проще оставить пустым и выбрать группу кнопкой в мастере бота." "$ADMIN_CHAT"
        if [[ -z $v || $v =~ ^-100[0-9]{6,}$ ]]; then
            ADMIN_CHAT=$v
            break
        fi
        ui_msg "Не похоже на ID группы" "ID супергруппы начинается с -100, например -1001234567890."
    done
}

# ================================================================================================ API панели

rw_init() {
    RW_API=${1%/}
    if [[ -z $RW_OUT ]]; then RW_OUT=$(mktemp); fi
    chmod 600 "$RW_OUT"
}

# rw_req МЕТОД ПУТЬ: JSON-тело из stdin (кроме GET), ответ в $RW_OUT, HTTP-код в $RW_CODE.
# Bearer-токен (RW_AUTH) идёт в curl через файл заголовков 0600. X-Forwarded-* нужны панели, когда к ней
# ходят мимо reverse proxy (http://127.0.0.1:3000): без них она закрывает соединение.
rw_req() {
    local method=$1 path=$2 hdr
    local args=(-sS --max-time 20 -o "$RW_OUT" -w '%{http_code}' -X "$method")
    hdr=$(mktemp)
    chmod 600 "$hdr"
    printf 'Content-Type: application/json\nX-Forwarded-For: 127.0.0.1\nX-Forwarded-Proto: https\n' >"$hdr"
    if [[ -n $RW_AUTH ]]; then printf 'Authorization: Bearer %s\n' "$RW_AUTH" >>"$hdr"; fi
    args+=(-H "@$hdr")
    if [[ $method != GET ]]; then args+=(--data-binary @-); fi
    RW_CODE=$(curl "${args[@]}" "$RW_API$path" 2>>"$LOG") || RW_CODE=000
    rm -f "$hdr"
    log "panel $method $path → $RW_CODE"
    [[ $RW_CODE == 2* ]]
}

rw_message() { jq -r '(.message // .error // empty) | tostring' "$RW_OUT" 2>/dev/null | head -c 300 || true; }

panel_wait() { # до ~4 минут: первый старт панели с миграциями небыстрый
    for _ in $(seq 1 80); do
        if RW_AUTH="" rw_req GET /api/auth/status </dev/null; then return 0; fi
        sleep 3
    done
    return 1
}

panel_can_register() {
    RW_AUTH="" rw_req GET /api/auth/status </dev/null &&
        [[ $(jq -r '.response.isRegisterAllowed' "$RW_OUT" 2>/dev/null) == true ]]
}

# panel_signin register|login: логин и пароль идут в jq через окружение, в curl через stdin.
panel_signin() {
    local path=/api/auth/login
    if [[ $1 == register ]]; then path=/api/auth/register; fi
    RW_AUTH=""
    if ! rw_req POST "$path" < <(U=$ADMIN_USER P=$ADMIN_PASS jq -nc '{username: env.U, password: env.P}'); then
        log "panel $path: $(rw_message)"
        return 1
    fi
    RW_AUTH=$(jq -r '.response.accessToken // empty' "$RW_OUT")
    : >"$RW_OUT"
    [[ -n $RW_AUTH ]]
}

# panel_new_token VAR имя: API-токен с правами «*» на 10 лет (нужен JWT админа в RW_AUTH)
panel_new_token() {
    local _t_val
    if ! rw_req POST /api/tokens < <(N=$2 jq -nc '{name: env.N, expiresInDays: 3650, scopes: ["*"]}'); then
        log "panel /api/tokens: $(rw_message)"
        return 1
    fi
    _t_val=$(jq -r '.response.token // empty' "$RW_OUT")
    : >"$RW_OUT"
    [[ -n $_t_val ]] || return 1
    printf -v "$1" '%s' "$_t_val"
}

# Секрет вебхуков, который примут и панель, и бот: 32–256 латинских букв и цифр.
hook_secret_ok() { [[ $1 =~ ^[A-Za-z0-9]+$ ]] && ((${#1} >= 32 && ${#1} <= 256)); }

password_ok() { # правило Remnawave: от 24 символов, есть A-Z, a-z и 0-9
    local p=$1
    ((${#p} >= 24)) &&
        [[ $p == *[ABCDEFGHIJKLMNOPQRSTUVWXYZ]* && $p == *[abcdefghijklmnopqrstuvwxyz]* && $p == *[0123456789]* ]]
}

ask_admin_new() {
    local p2
    while :; do
        ask ADMIN_USER "Админ панели" "Придумайте логин администратора панели Remnawave (латиница и цифры)." "${ADMIN_USER:-admin}"
        if [[ $ADMIN_USER =~ ^[A-Za-z0-9_.-]{3,64}$ ]]; then break; fi
        ui_msg "Логин не подходит" "Только латинские буквы, цифры, точка, дефис и подчёркивание, от 3 символов."
    done
    while :; do
        ask_secret ADMIN_PASS "Пароль админа панели" "Remnawave требует пароль от 24 символов, в нём должны быть заглавные и строчные латинские буквы и цифры.

Установщик пароль не сохраняет. Запишите его в менеджер паролей: с ним вы будете входить в панель."
        if ! password_ok "$ADMIN_PASS"; then
            ui_msg "Пароль не подходит" "Нужно от 24 символов, хотя бы одна заглавная латинская буква, одна строчная и одна цифра.
Сейчас символов: ${#ADMIN_PASS}."
            continue
        fi
        ask_secret p2 "Пароль ещё раз" "Повторите пароль."
        if [[ $p2 == "$ADMIN_PASS" ]]; then break; fi
        ui_msg "Не совпало" "Пароли не совпали, давайте ещё раз."
    done
    ADMIN_ACTION=register
}

ask_admin_login() {
    ask ADMIN_USER "Вход в панель" "Логин администратора панели Remnawave." "${ADMIN_USER:-admin}"
    ask_secret ADMIN_PASS "Вход в панель" "Пароль администратора. Нужен один раз, чтобы выпустить API-токен для бота; нигде не сохраняется."
}

ask_panel_token() {
    while :; do
        ask_secret RW_TOKEN "API-токен панели" "В панели: Настройки → API-токены → Создать. Права «*» (все).
Вставьте токен сюда, символы не видны."
        RW_TOKEN=${RW_TOKEN//[[:space:]]/}
        if [[ -n $RW_TOKEN ]]; then return 0; fi
    done
}

# Как бот получит API-токен панели, когда админ в панели уже есть.
ask_panel_access() {
    local opts=()
    if [[ $RW_TOKEN_SET == 1 ]]; then opts+=(keep "Оставить токен, который уже есть у бота"); fi
    opts+=(login "Выпусти токен сам: введу логин и пароль админа панели")
    opts+=(token "Вставлю готовый API-токен (Настройки → API-токены)")
    opts+=(skip "Пропустить: впишу токен потом в мастере бота")
    choose ADMIN_ACTION "Доступ к панели" "Боту нужен API-токен панели Remnawave." "${opts[0]}" "${opts[@]}"
    case $ADMIN_ACTION in
        login) ask_admin_login ;;
        token) ask_panel_token ;;
    esac
}

# Получить RW_TOKEN выбранным способом. Не вышло: объяснить, как сделать руками, и спросить, что дальше.
panel_obtain_token() {
    local act
    while :; do
        case $ADMIN_ACTION in
            keep | skip) return 0 ;;
            token)
                if RW_AUTH=$RW_TOKEN rw_req GET /api/system/stats </dev/null; then
                    ok "Панель приняла API-токен"
                    return 0
                fi
                ;;
            register | login)
                if panel_signin "$ADMIN_ACTION" && panel_new_token RW_TOKEN svbg-shop; then
                    if [[ $ADMIN_ACTION == register ]]; then
                        ok "Админ панели $ADMIN_USER создан, API-токен для бота выпущен"
                    else
                        ok "Вошёл в панель как $ADMIN_USER, API-токен для бота выпущен"
                    fi
                    return 0
                fi
                ;;
        esac
        choose act "Токен не получен" "Панель ответила: ${RW_CODE} $(rw_message)

Можно сделать руками: откройте ${PANEL_DOMAIN:+https://$PANEL_DOMAIN}${PANEL_DOMAIN:-панель}, войдите (или зарегистрируйте админа), затем Настройки → API-токены → Создать, права «*». Готовый токен вставьте здесь." token \
            token "Вставлю токен" \
            login "Попробую логин и пароль ещё раз" \
            skip "Пропустить, впишу токен в боте позже"
        ADMIN_ACTION=$act
        case $act in
            token) ask_panel_token ;;
            login) ask_admin_login ;;
        esac
    done
}

# ================================================================================================ панель Remnawave

panel_install() {
    step "Панель Remnawave ($RW_DIR)"
    mkdir -p "$RW_DIR"
    if [[ ! -f $RW_DIR/docker-compose.yml ]]; then
        curl -fsSL "$RW_RAW/docker-compose-prod.yml" -o "$RW_DIR/docker-compose.yml" ||
            die "Не скачался docker-compose-prod.yml из github.com/remnawave/backend"
        ok "docker-compose.yml взят из remnawave/backend"
    else
        ok "docker-compose.yml уже есть, не трогаю"
    fi
    if [[ ! -f $RW_DIR/.env ]]; then
        local tmp=$RW_DIR/.env.new pw user db k
        curl -fsSL "$RW_RAW/.env.sample" -o "$tmp" || die "Не скачался .env.sample из github.com/remnawave/backend"
        chmod 600 "$tmp"
        for k in JWT_AUTH_SECRET JWT_API_TOKENS_SECRET APP_SECRET METRICS_PASS; do
            if env_has "$tmp" "$k"; then env_set "$tmp" "$k" "$(rand_hex 64)"; fi
        done
        env_set "$tmp" WEBHOOK_SECRET_HEADER "$(rand_hex 32)"
        pw=$(rand_hex 24)
        user=$(env_get "$tmp" POSTGRES_USER)
        db=$(env_get "$tmp" POSTGRES_DB)
        env_set "$tmp" POSTGRES_PASSWORD "$pw"
        env_set "$tmp" DATABASE_URL "\"postgresql://${user:-postgres}:$pw@remnawave-db:5432/${db:-postgres}\""
        mv -f "$tmp" "$RW_DIR/.env"
        ok "Секреты панели сгенерированы через openssl, .env с правами 600"
    else
        ok ".env панели уже есть, секреты не меняю"
    fi
    env_set "$RW_DIR/.env" FRONT_END_DOMAIN "$PANEL_DOMAIN"
    if env_has "$RW_DIR/.env" PANEL_DOMAIN; then env_set "$RW_DIR/.env" PANEL_DOMAIN "$PANEL_DOMAIN"; fi
    env_set "$RW_DIR/.env" SUB_PUBLIC_DOMAIN "$SUB_DOMAIN"
    panel_hooks_env
    echo "  Скачиваю образы и запускаю панель (первый раз несколько минут)…"
    run docker compose -f "$RW_DIR/docker-compose.yml" up -d || die "Панель не запустилась, причина в логе."
    rw_init "http://127.0.0.1:3000"
    panel_wait || die "Панель не ответила за 4 минуты. Смотрите: cd $RW_DIR && docker compose logs remnawave"
    ok "Панель работает"
}

# Перезапустить контейнер панели, если её .env поменялся (сумма до правки — в $1).
panel_reload_if_changed() {
    if [[ $1 == "$(file_sum "$RW_DIR/.env")" ]]; then return 0; fi
    step "Перезапускаю панель с новыми настройками"
    run docker compose -f "$RW_DIR/docker-compose.yml" up -d --force-recreate remnawave ||
        die "Панель не перезапустилась, причина в логе."
    rw_init "http://127.0.0.1:3000"
    if panel_wait; then ok "Панель перезапущена"; else warn "Панель долго не отвечает: cd $RW_DIR && docker compose logs remnawave"; fi
}

# Вебхуки панели → бот: WEBHOOK_ENABLED, WEBHOOK_URL (+ наш адрес через запятую), общий секрет → HOOK_SECRET.
panel_hooks_env() {
    local f=$RW_DIR/.env enabled urls secret
    enabled=$(env_get "$f" WEBHOOK_ENABLED)
    urls=$(env_get "$f" WEBHOOK_URL)
    secret=$(env_get "$f" WEBHOOK_SECRET_HEADER)
    if [[ $enabled != true ]]; then
        urls=$BOT_HOOK
        if ! hook_secret_ok "$secret" || [[ $secret == "$RW_SAMPLE_SECRET" ]]; then secret=$(rand_hex 32); fi
    else
        case ",$urls," in
            *",$BOT_HOOK,"*) ;;
            *) urls=${urls:+$urls,}$BOT_HOOK ;;
        esac
        if ! hook_secret_ok "$secret"; then
            warn "WEBHOOK_SECRET_HEADER панели не подходит боту (нужно 32+ латинских букв и цифр). Вебхуки панели бот принимать не будет, сверка пойдёт по расписанию."
            return 0
        fi
        if [[ $secret == "$RW_SAMPLE_SECRET" ]]; then
            warn "У панели секрет вебхуков из примера .env.sample. Смените его в $f и у всех, кто принимает вебхуки."
        fi
    fi
    env_set "$f" WEBHOOK_ENABLED true
    env_set "$f" WEBHOOK_URL "$urls"
    env_set "$f" WEBHOOK_SECRET_HEADER "$secret"
    HOOK_SECRET=$secret
    log "вебхуки панели: $urls"
}

panel_hooks_remove() {
    local f=$RW_DIR/.env urls sum
    [[ -f $f ]] || return 0
    sum=$(file_sum "$f")
    urls=$(env_get "$f" WEBHOOK_URL | tr ',' '\n' | grep -vxF "$BOT_HOOK" | paste -sd, || true)
    if [[ -z $urls ]]; then
        env_set "$f" WEBHOOK_ENABLED false
        env_set "$f" WEBHOOK_URL "https://your-webhook-url.com/endpoint"
    else
        env_set "$f" WEBHOOK_URL "$urls"
    fi
    panel_reload_if_changed "$sum"
}

sub_install() {
    step "Страница подписки ($SUB_DIR)"
    mkdir -p "$SUB_DIR"
    if [[ ! -f $SUB_DIR/docker-compose.yml ]]; then
        cat >"$SUB_DIR/docker-compose.yml" <<'YML'
services:
  remnawave-subscription-page:
    image: remnawave/subscription-page:latest
    container_name: remnawave-subscription-page
    hostname: remnawave-subscription-page
    restart: always
    env_file:
      - .env
    ports:
      - '127.0.0.1:3010:3010'
    networks:
      - remnawave-network
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

networks:
  remnawave-network:
    name: remnawave-network
    external: true
YML
    fi
    if [[ ! -f $SUB_DIR/.env ]]; then
        printf 'APP_PORT=3010\n' >"$SUB_DIR/.env"
    fi
    chmod 600 "$SUB_DIR/.env"
    env_set "$SUB_DIR/.env" REMNAWAVE_PANEL_URL "http://remnawave:$(panel_port)"
    env_set "$SUB_DIR/.env" TRUST_PROXY 1
    if [[ -z $(env_get "$SUB_DIR/.env" REMNAWAVE_API_TOKEN) ]]; then
        local tok=""
        if [[ -n $RW_AUTH ]] && panel_new_token tok subscription-page; then
            log "для страницы подписки выпущен отдельный токен"
        else
            tok=$RW_TOKEN
        fi
        if [[ -n $tok ]]; then env_set "$SUB_DIR/.env" REMNAWAVE_API_TOKEN "$tok"; fi
    fi
    if [[ -z $(env_get "$SUB_DIR/.env" REMNAWAVE_API_TOKEN) ]]; then
        warn "Странице подписки нужен API-токен. Впишите REMNAWAVE_API_TOKEN=… в $SUB_DIR/.env и выполните: cd $SUB_DIR && docker compose up -d"
        return 0
    fi
    run docker compose -f "$SUB_DIR/docker-compose.yml" up -d || die "Страница подписки не запустилась, причина в логе."
    ok "Страница подписки запущена"
}

# ================================================================================================ бот

pg_password() { # пароль роли svbg: генерируется один раз, дальше берётся из data/.pg_password
    if [[ ! -s $DATA/.pg_password ]]; then
        (
            umask 077
            rand_hex 24 >"$DATA/.pg_password"
        )
    fi
    # 0644 внутри каталога 0700: читать его может только контейнер PostgreSQL (через secrets).
    chmod 0644 "$DATA/.pg_password"
    PG_PASS=$(<"$DATA/.pg_password")
}

db_setup() {
    pg_password
    if [[ $DB_MODE != panel ]]; then
        DATABASE_URL="postgresql://svbg:$PG_PASS@svbg-db:5432/svbg"
        ok "База бота: свой контейнер svbg-db (postgres:17-alpine)"
        return 0
    fi
    local user db
    user=$(env_get "$RW_DIR/.env" POSTGRES_USER)
    db=$(env_get "$RW_DIR/.env" POSTGRES_DB)
    # SQL идёт через stdin: пароля нет в argv.
    docker exec -i remnawave-db psql -v ON_ERROR_STOP=1 -q -U "${user:-postgres}" -d "${db:-postgres}" >>"$LOG" 2>&1 <<SQL ||
SELECT 'CREATE ROLE svbg LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'svbg')\gexec
ALTER ROLE svbg WITH LOGIN PASSWORD '$PG_PASS';
SELECT 'CREATE DATABASE svbg OWNER svbg' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'svbg')\gexec
REVOKE ALL ON DATABASE svbg FROM PUBLIC;
SQL
        die "Не получилось создать базу svbg в PostgreSQL панели (remnawave-db), причина в логе."
    DATABASE_URL="postgresql://svbg:$PG_PASS@remnawave-db:5432/svbg"
    ok "База бота: отдельная база svbg в PostgreSQL панели"
}

render_compose() { # render_compose [файл]: по умолчанию $SVBG_DIR/docker-compose.yml
    local f=${1:-$SVBG_DIR/docker-compose.yml}
    mkdir -p "$SVBG_DIR"
    {
        cat <<YML
# SvBG Shop. Файл пишет установщик (deploy/install.sh), ручные правки затрёт следующий запуск.
# Настройки бота лежат в data/.env (или в самом боте: /settings). Команды: svbg help
name: svbg

services:
  bot:
    image: ${BOT_IMAGE:-$IMAGE_DEFAULT}
    container_name: svbg-shop
    hostname: svbg-shop
    restart: unless-stopped
YML
        if [[ $DB_MODE != panel ]]; then
            cat <<'YML'
    depends_on:
      db: { condition: service_healthy }
YML
        fi
        cat <<'YML'
    volumes:
      - ./data:/app/data
    ports:
      - "127.0.0.1:8080:8080"
    networks: [default, shared]
    mem_limit: 512m
    stop_grace_period: 30s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
YML
        if [[ $DB_MODE != panel ]]; then
            cat <<'YML'

  db:
    image: postgres:17-alpine
    container_name: svbg-db
    restart: unless-stopped
    command: >-
      postgres -c shared_buffers=128MB -c max_connections=30 -c work_mem=4MB
      -c effective_cache_size=384MB
    volumes:
      - ./pg:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: svbg
      POSTGRES_USER: svbg
      POSTGRES_PASSWORD_FILE: /run/secrets/pg
    secrets: [pg]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U svbg -d svbg"]
      interval: 10s
      timeout: 5s
      retries: 10
    mem_limit: 512m
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

secrets:
  pg:
    file: ./data/.pg_password
YML
        fi
        cat <<YML

networks:
  shared:
    name: $NET
    external: true
YML
    } >"$f"
    log "записан $f (сеть $NET, база $DB_MODE)"
}

# ------------------------------------------------------------------------------------------------ образ бота

# Откуда брать образ: install.conf, поверх него переменные SVBG_IMAGE и SVBG_BUILD_LOCAL.
# Старые установки (svbg-shop:local, в install.conf про образ ничего нет) переходят на готовый образ.
image_prefs() {
    if [[ -n ${SVBG_IMAGE-} ]]; then IMAGE=$SVBG_IMAGE; fi
    case ${SVBG_BUILD_LOCAL-} in
        1 | yes | true) BUILD_LOCAL=1 ;;
        0 | no | false) BUILD_LOCAL="" ;;
    esac
    if [[ $IMAGE == "$LOCAL_IMAGE" ]]; then BUILD_LOCAL=1 IMAGE=""; fi
    IMAGE=${IMAGE:-$IMAGE_DEFAULT}
    if [[ $BUILD_LOCAL == 1 ]]; then BOT_IMAGE=$LOCAL_IMAGE; else BOT_IMAGE=$IMAGE; fi
}

image_label() {
    if [[ $BUILD_LOCAL == 1 ]]; then printf 'соберу на этом сервере из %s' "$SRC_DIR"; else printf '%s' "$IMAGE"; fi
}

# ID образа, на котором сейчас стоит контейнер бота (пусто, если контейнера нет).
bot_image_id() { docker container inspect -f '{{.Image}}' svbg-shop 2>/dev/null || true; }
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

# image_digest образ: короткий дайджест из ghcr.io и коммит, или ID, если образ собран здесь.
image_digest() {
    local d rev
    d=$(docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$1" 2>/dev/null |
        sed -n 's/.*@sha256:\([0-9a-f]\{12\}\).*/\1/p' | head -n 1) || d=""
    if [[ -z $d ]]; then
        d=$(image_id "$1")
        d=${d#sha256:}
        printf 'id %s, собран на сервере' "${d:0:12}"
        return 0
    fi
    printf 'sha256:%s' "$d"
    rev=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null) || rev=""
    if [[ $rev =~ ^[0-9a-f]{7,}$ ]]; then printf ', коммит %s' "${rev:0:7}"; fi
}

image_status() {
    local ref id
    ref=$(docker container inspect -f '{{.Config.Image}}' svbg-shop 2>/dev/null) || ref=""
    id=$(bot_image_id)
    if [[ -z $ref ]]; then
        ref=$BOT_IMAGE
        id=$(image_id "$ref")
    fi
    if [[ -z $id ]]; then
        printf '%s (на сервере его нет)' "$ref"
    else
        printf '%s (%s)' "$ref" "$(image_digest "$id")"
    fi
}

bot_build() {
    local mem
    [[ -f $SRC_DIR/Dockerfile ]] ||
        die "Не нашёл Dockerfile в $SRC_DIR. Запускайте установщик из папки репозитория: sudo bash deploy/install.sh"
    mem=$(mem_mb)
    if ((mem < LOW_MEM)); then
        warn "Памяти $mem МБ, а сборка может подвесить такой сервер на несколько минут. Сначала проверю swap."
        ensure_swap
    fi
    step "Собираю образ бота из $SRC_DIR (первый раз 3–7 минут)"
    DOCKER_BUILDKIT=1 run docker build -t "$LOCAL_IMAGE" "$SRC_DIR" || die "Образ не собрался, причина в логе."
    BOT_IMAGE=$LOCAL_IMAGE
    ok "Образ $LOCAL_IMAGE готов"
}

# Готовый образ из ghcr.io. Собираем здесь, только если так выбрал владелец или скачать не вышло.
bot_image() {
    if [[ $BUILD_LOCAL == 1 ]]; then
        bot_build
        return 0
    fi
    step "Скачиваю образ бота $IMAGE"
    if run docker pull "$IMAGE"; then
        BOT_IMAGE=$IMAGE
        ok "Образ готов: $(image_digest "$IMAGE")"
        return 0
    fi
    warn "Образ $IMAGE не скачался (нет доступа к ghcr.io или такого тега нет). Соберу его здесь из исходников."
    bot_build
}

# keep_previous старый_id новый_id: прежний образ остаётся под тегом svbg-shop:previous, на него можно откатиться.
keep_previous() {
    if [[ -n $1 && $1 != "$2" ]]; then run docker tag "$1" "$PREV_IMAGE" || true; fi
}

# Старые образы бота, на которые больше ничего не ссылается. Чужие образы не трогаем.
drop_stale_images() {
    if [[ $BOT_IMAGE != "$LOCAL_IMAGE" && -n $(image_id "$LOCAL_IMAGE") ]]; then
        run docker image rm "$LOCAL_IMAGE" || true
    fi
    run docker image prune -f --filter "label=org.opencontainers.image.title=SvBG Shop" || true
}

# Строки KEY=VALUE для data/.env. Только то, что спросили в этом запуске: остальное не трогаем.
settings_lines() {
    if [[ -n $BOT_TOKEN ]]; then printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN"; fi
    printf 'DATABASE_URL=%s\n' "$DATABASE_URL"
    if [[ $FIRST_INSTALL == 1 ]]; then printf 'BOT_MODE=polling\n'; fi
    if [[ -n $BOT_DOMAIN ]]; then printf 'PUBLIC_URL=https://%s\n' "$BOT_DOMAIN"; fi
    if [[ -n $RW_URL ]]; then printf 'REMNAWAVE_URL=%s\n' "$RW_URL"; fi
    if [[ -n $RW_TOKEN ]]; then printf 'REMNAWAVE_TOKEN=%s\n' "$RW_TOKEN"; fi
    if [[ -n $HOOK_SECRET ]]; then printf 'REMNAWAVE_WEBHOOK_SECRET=%s\n' "$HOOK_SECRET"; fi
    if [[ -n $OWNER_ID ]]; then printf 'OWNER_IDS=%s\n' "$OWNER_ID"; fi
    if [[ -n $ADMIN_CHAT ]]; then printf 'ADMIN_CHAT_ID=%s\n' "$ADMIN_CHAT"; fi
}

# То же, что «python -m svbg set KEY=VALUE», но строки читаются из stdin: значения не светятся в ps.
PY_SET='import sys
from svbg.__main__ import main
rc = 0
for line in sys.stdin:
    line = line.rstrip("\r\n")
    if line:
        rc = main(["set", line], configure_logging=False) or rc
sys.exit(rc)'

bot_settings() {
    log "\$ svbg set (ключи: $(settings_lines | cut -d= -f1 | paste -sd' '))"
    if ! settings_lines | compose_bot run --rm --no-deps -T bot python -c "$PY_SET" >>"$LOG" 2>&1; then
        die "Не записались настройки бота в $DATA/.env, причина в логе."
    fi
    if [[ -n $RW_TOKEN ]]; then RW_TOKEN_SET=1; fi
    ok "Настройки записаны в $DATA/.env"
}

bot_wait() {
    for _ in $(seq 1 60); do
        if docker exec svbg-shop python -m svbg health >/dev/null 2>&1; then return 0; fi
        sleep 3
    done
    return 1
}

install_cli() {
    install -m 0755 "$SCRIPT_DIR/svbg" "$SVBG_BIN"
    log "команда svbg: $SVBG_BIN"
}

bot_install() {
    local was_running="" cur_id
    step "Бот SvBG Shop ($SVBG_DIR)"
    mkdir -p "$DATA"
    chown 1000:1000 "$DATA"
    chmod 0700 "$DATA"
    if [[ ! -s $DATA/.env ]]; then FIRST_INSTALL=1; fi
    if bot_running; then was_running=1; fi
    cur_id=$(bot_image_id)
    db_setup
    bot_image
    keep_previous "$cur_id" "$(image_id "$BOT_IMAGE")"
    render_compose
    run compose_bot run --rm --no-deps -T bot python -m svbg env init </dev/null ||
        die "Не создался $DATA/.env, причина в логе."
    bot_settings
    install_cli
    save_state
    step "Запускаю бота"
    run compose_bot up -d --remove-orphans || die "Бот не запустился, причина в логе."
    if [[ -n $was_running ]]; then run compose_bot restart bot; fi
    if bot_wait; then
        ok "Бот запущен и отвечает"
    else
        warn "Бот пока не ответил на проверку. Посмотрите: svbg logs"
    fi
}

# ================================================================================================ Caddy

caddy_find() {
    docker ps --format '{{.Names}}	{{.Image}}' 2>/dev/null |
        awk -F'\t' '!f && $2 ~ /(^|\/)caddy([:@-]|$)/ { print $1; f = 1 }' || true
}

caddy_file_of() {
    docker inspect -f '{{range .Mounts}}{{.Destination}}|{{.Source}}{{"\n"}}{{end}}' "$1" 2>/dev/null |
        awk -F'|' '$1 == "/etc/caddy/Caddyfile" { f = $2 } $1 == "/etc/caddy" && f == "" { f = $2 "/Caddyfile" } END { print f }' || true
}

ports_busy() { [[ -n $(ss -Hltn '( sport = :80 or sport = :443 )' 2>/dev/null) ]]; }

# caddy_put имя текст: вставить или заменить блок между метками # >>> svbg:имя / # <<< svbg:имя
caddy_put() {
    local tmp
    tmp=$(mktemp)
    awk -v b="# >>> svbg:$1" -v e="# <<< svbg:$1" '$0 == b { s = 1; next } $0 == e { s = 0; next } !s' "$CADDYFILE" >"$tmp"
    if [[ -n ${2-} ]]; then printf '# >>> svbg:%s\n%s\n# <<< svbg:%s\n' "$1" "$2" "$1" >>"$tmp"; fi
    # Пишем поверх, а не через mv: файл смонтирован в контейнер, новый inode Caddy не увидит.
    cat "$tmp" >"$CADDYFILE"
    rm -f "$tmp"
}

# Домен уже описан в Caddyfile вручную (вне наших меток)?
caddy_has_foreign() {
    D=$1 awk 'BEGIN { d = ENVIRON["D"] } /^# >>> svbg:/ { s = 1 } /^# <<< svbg:/ { s = 0; next } !s && index($0, d) { f = 1 } END { exit !f }' "$CADDYFILE"
}

caddy_upstream() { # caddy_upstream контейнер:порт локальный_порт
    if [[ $CADDY_HOSTNET == 1 ]]; then printf '127.0.0.1:%s' "$2"; else printf '%s' "$1"; fi
}

caddy_block() { # caddy_block panel|sub|bot
    local port
    port=$(panel_port)
    case $1 in
        panel) printf 'https://%s {\n    reverse_proxy * http://%s\n}' "$PANEL_DOMAIN" "$(caddy_upstream "remnawave:$port" 3000)" ;;
        sub) printf 'https://%s {\n    reverse_proxy * http://%s\n}' "$SUB_DOMAIN" "$(caddy_upstream remnawave-subscription-page:3010 3010)" ;;
        bot)
            cat <<EOF
https://$BOT_DOMAIN {
    # Снаружи открыты только вебхуки, Telegram, медиа и короткие ссылки. Остальное отдаёт 404.
    @public path /webhooks/* /tg/* /m/* /r/*
    handle @public {
        reverse_proxy $(caddy_upstream svbg-shop:8080 8080)
    }
    handle {
        respond 404
    }
}
EOF
            ;;
    esac
}

caddy_domain() {
    case $1 in
        panel) printf '%s' "$PANEL_DOMAIN" ;;
        sub) printf '%s' "$SUB_DOMAIN" ;;
        bot) printf '%s' "$BOT_DOMAIN" ;;
    esac
}

proxy_manual() { # proxy_manual причина имя…: инструкция для своего reverse proxy
    local why=$1 f=$SVBG_DIR/reverse-proxy.txt n
    shift
    mkdir -p "$SVBG_DIR"
    {
        echo "# Caddy не настроен автоматически: $why"
        echo "# Пропишите в своём reverse proxy (HTTPS обязателен):"
        for n in "$@"; do
            echo
            CADDY_HOSTNET=1 caddy_block "$n"
            echo
        done
        if [[ " $* " == *" bot "* ]]; then
            cat <<EOF

# То же для nginx (бот слушает 127.0.0.1:8080):
# location ~ ^/(webhooks|tg|m|r)/ {
#     proxy_pass http://127.0.0.1:8080;
#     proxy_set_header Host \$host;
#     proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
#     proxy_set_header X-Forwarded-Proto \$scheme;
# }
# location / { return 404; }
EOF
        fi
    } >"$f"
    warn "Caddy не настроен: $why. Что прописать в своём прокси: $f"
    PROXY_NOTE=$f
}

caddy_install() {
    step "Ставлю Caddy ($CADDY_DIR): сертификаты Let's Encrypt он выпустит сам"
    mkdir -p "$CADDY_DIR"
    if [[ ! -f $CADDY_DIR/Caddyfile ]]; then
        printf '# Caddy. Блоки между метками "# >>> svbg:…" пишет установщик SvBG Shop.\n' >"$CADDY_DIR/Caddyfile"
    fi
    cat >"$CADDY_DIR/docker-compose.yml" <<YML
services:
  caddy:
    image: caddy:2
    container_name: caddy
    hostname: caddy
    restart: always
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    networks: [shared]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy-data:/data
      - caddy-config:/config
    mem_limit: 256m
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

networks:
  shared:
    name: $NET
    external: true

volumes:
  caddy-data:
    name: caddy-ssl-data
  caddy-config:
    name: caddy-config
YML
    CADDYFILE=$CADDY_DIR/Caddyfile CADDY_C=caddy CADDY_NEW=1 CADDY_HOSTNET=""
}

# caddy_setup panel|sub|bot…: найти Caddy (или поставить свой), прописать блоки, применить.
caddy_setup() {
    local c n d bak
    CADDY_NEW="" CADDY_HOSTNET=""
    c=$(caddy_find)
    if [[ -n $c ]]; then
        CADDYFILE=$(caddy_file_of "$c")
        if [[ -z $CADDYFILE || ! -f $CADDYFILE ]]; then
            proxy_manual "Caddy ($c) работает, но его Caddyfile не нашёлся" "$@"
            return 0
        fi
        CADDY_C=$c
        if [[ $(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null) == host ]]; then CADDY_HOSTNET=1; fi
        step "Caddy: дописываю адреса в $CADDYFILE (контейнер $c)"
    elif ports_busy; then
        proxy_manual "порты 80/443 заняты другим веб-сервером" "$@"
        return 0
    else
        caddy_install
    fi
    bak=$CADDYFILE.bak-svbg
    cp -p "$CADDYFILE" "$bak"
    for n in "$@"; do
        d=$(caddy_domain "$n")
        [[ -n $d ]] || continue
        if caddy_has_foreign "$d"; then
            warn "$d уже описан в $CADDYFILE вручную, его блок не трогаю"
            continue
        fi
        caddy_put "$n" "$(caddy_block "$n")"
    done
    if [[ -n $CADDY_NEW ]]; then
        run docker compose -f "$CADDY_DIR/docker-compose.yml" up -d || die "Caddy не запустился, причина в логе."
        ok "Caddy запущен, сертификаты появятся в течение минуты-двух"
        return 0
    fi
    if [[ $CADDY_HOSTNET != 1 ]]; then docker network connect "$NET" "$CADDY_C" >/dev/null 2>&1 || true; fi
    if ! run docker exec "$CADDY_C" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
        cat "$bak" >"$CADDYFILE"
        proxy_manual "Caddy не принял новый Caddyfile (вернул прежний, копия: $bak)" "$@"
        return 0
    fi
    run docker exec "$CADDY_C" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile ||
        run docker restart "$CADDY_C"
    ok "Caddy перечитал конфиг"
}

caddy_remove_bot() {
    local c
    c=$(caddy_find)
    [[ -n $c ]] || return 0
    CADDYFILE=$(caddy_file_of "$c")
    if [[ -z $CADDYFILE || ! -f $CADDYFILE ]] || ! grep -q '^# >>> svbg:bot$' "$CADDYFILE"; then return 0; fi
    caddy_put bot ""
    run docker exec "$c" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile || true
    ok "Убрал адрес бота из Caddy"
}

# ================================================================================================ состояние и итог

save_state() {
    local k v
    # shellcheck disable=SC2034 # читается через ${!k} ниже
    SVBG_SRC=$SRC_DIR
    mkdir -p "$SVBG_DIR"
    {
        echo "# SvBG Shop: что выбрано при установке. Секретов здесь нет. Читают install.sh и svbg."
        for k in "${STATE_KEYS[@]}"; do
            v=${!k-}
            printf "%s='%s'\n" "$k" "${v//\'/\'\\\'\'}"
        done
    } >"$STATE"
    chmod 600 "$STATE"
}

load_state() {
    if [[ -f $STATE ]]; then
        # shellcheck source=/dev/null
        . "$STATE"
    fi
    image_prefs
}

containers_table() {
    local stats
    stats=$(docker stats --no-stream --format '{{.Name}}	{{.MemUsage}}' 2>/dev/null || true)
    docker ps -a --format '{{.Names}}	{{.State}}' 2>/dev/null |
        S=$stats awk -F'\t' 'BEGIN { n = split(ENVIRON["S"], L, "\n"); for (i = 1; i <= n; i++) { split(L[i], p, "\t"); m[p[1]] = p[2] } }
            $1 ~ /^(svbg-shop|svbg-db|remnawave|remnawave-db|remnawave-redis|remnawave-subscription-page|caddy)$/ {
                mem = ($1 in m) ? m[$1] : "-"; sub(/ \/.*/, "", mem)
                printf "  %-30s %-10s %s\n", $1, $2, mem }' || true
}

status_text() {
    local mt mu sw health="не отвечает" db="свой контейнер svbg-db"
    read -r mt mu < <(free -m | awk '/^Mem:/ { print $2, $3 }')
    sw=$(free -m | awk '/^Swap:/ { print $2 }')
    if docker exec svbg-shop python -m svbg health >/dev/null 2>&1; then health="работает"; fi
    if [[ $DB_MODE == panel ]]; then db="svbg в PostgreSQL панели"; fi
    echo "Адреса"
    if [[ -n $PANEL_DOMAIN ]]; then printf '  %-18s https://%s\n' "Панель" "$PANEL_DOMAIN"; fi
    if [[ -n $SUB_DOMAIN ]]; then printf '  %-18s https://%s\n' "Страница подписки" "$SUB_DOMAIN"; fi
    if [[ -n $BOT_DOMAIN ]]; then printf '  %-18s https://%s (только вебхуки)\n' "Бот" "$BOT_DOMAIN"; fi
    if [[ -n $BOT_NAME ]]; then printf '  %-18s https://t.me/%s\n' "Бот в Telegram" "$BOT_NAME"; fi
    if [[ -n $RW_URL ]]; then printf '  %-18s %s\n' "Бот ходит в панель" "$RW_URL"; fi
    echo
    printf 'Бот: %s. База: %s.\n' "$health" "$db"
    printf 'Образ: %s\n' "$(image_status)"
    echo
    printf '  %-30s %-10s %s\n' "Контейнер" "Состояние" "Память"
    containers_table
    echo
    printf 'Память сервера: занято %s из %s МБ, swap %s МБ.\n' "$mu" "$mt" "${sw:-0}"
}

finish() {
    local link=""
    clear || true
    printf '\n\033[1;32m Готово.\033[0m\n\n'
    status_text
    echo
    if [[ -z $OWNER_ID ]]; then
        link=$(docker exec svbg-shop python -m svbg owner-link 2>>"$LOG" | tail -n 1) || link=""
        if [[ $link == https://* ]]; then
            printf 'Ссылка владельца (одноразовая, кто первым откроет, тот и владелец):\n\n  \033[1m%s\033[0m\n\n' "$link"
        else
            printf 'Ссылку владельца выдаст команда: svbg owner-link\n\n'
        fi
    else
        printf 'Вы записаны владельцем (ID %s). Откройте бота и отправьте /start.\n\n' "$OWNER_ID"
    fi
    echo "Что дальше:"
    echo "  1. Откройте бота: он проведёт через мастер /setup (панель, админ-группа, проверка)."
    echo "  2. Подключите кассу: /settings → Платёжки. Подробно: docs/guide/payments.md"
    echo "  3. Сохраните ключ шифрования в менеджер паролей: svbg show-key"
    if [[ $MODE == full ]]; then
        echo "  4. Зайдите в панель https://$PANEL_DOMAIN и добавьте ноды: без них VPN не заработает."
    fi
    if [[ -n $PROXY_NOTE ]]; then
        echo "  !  Пропишите адреса в своём reverse proxy: $PROXY_NOTE"
    fi
    if [[ $MODE == remote && -n $BOT_DOMAIN ]]; then
        echo "  !  Вебхуки панели: строки для .env панели лежат в $SVBG_DIR/panel-webhook.env (cat его на этом сервере)."
    fi
    echo
    echo "Команды: svbg status | logs | update | backup | owner-link | help"
    echo "Лог установки: $LOG"
}

ask_rw_url() { # ask_rw_url домен_панели
    local act port
    port=$(panel_port)
    if [[ -z $1 ]]; then
        RW_URL=http://remnawave:$port
        return 0
    fi
    choose act "Как бот ходит в панель" "Бот и панель на одном сервере. Ходить в панель можно через домен или напрямую по внутренней сети Docker." https \
        https "https://$1 (через домен и Caddy)" \
        local "http://remnawave:$port (напрямую, не зависит от DNS и сертификата)"
    if [[ $act == local ]]; then RW_URL=http://remnawave:$port; else RW_URL=https://$1; fi
}

confirm_plan() {
    if ! ui_yesno "Проверьте" "$1
Начинаем? Дальше вопросов не будет, займёт 5–15 минут."; then
        clear
        echo "Остановился до установки. Запустите снова, когда будете готовы."
        exit 0
    fi
    clear || true
}

access_label() {
    case $ADMIN_ACTION in
        register) printf 'создам админа %s и выпущу API-токен' "$ADMIN_USER" ;;
        login) printf 'войду как %s и выпущу API-токен' "$ADMIN_USER" ;;
        token) printf 'ваш API-токен' ;;
        keep) printf 'оставлю токен, что уже есть у бота' ;;
        *) printf 'токена нет, впишете в мастере бота' ;;
    esac
}

owner_label() { if [[ -n $OWNER_ID ]]; then printf 'ID %s' "$OWNER_ID"; else printf 'ссылкой в конце установки'; fi; }

# ================================================================================================ режимы

flow_full() {
    MODE=full NET=remnawave-network DB_MODE=panel
    check_disk 5120
    ask_domains_full
    ask_bot_token
    if command -v docker >/dev/null 2>&1 && panel_present && container_running remnawave; then
        rw_init "http://127.0.0.1:3000"
        if panel_can_register; then ask_admin_new; else ask_panel_access; fi
    else
        ask_admin_new
    fi
    ask_rw_url "$PANEL_DOMAIN"
    ask_owner
    confirm_plan "Панель:            https://$PANEL_DOMAIN
Страница подписки: https://$SUB_DOMAIN
Бот:               https://$BOT_DOMAIN ${BOT_NAME:+(@$BOT_NAME)}
Доступ к панели:   $(access_label)
Бот ходит в:       $RW_URL
Владелец:          $(owner_label)
База бота:         отдельная база svbg в PostgreSQL панели
Образ бота:        $(image_label)
"
    ensure_swap
    ensure_docker
    panel_install
    caddy_setup panel sub bot
    panel_obtain_token
    sub_install
    bot_install
    open_ports
    save_state
    finish
}

flow_near() {
    local sum d db_label="свой контейнер postgres:17-alpine"
    MODE=near NET=remnawave-network
    ensure_docker
    if ! container_running remnawave; then
        echo "Панель не запущена, запускаю…"
        run docker compose -f "$RW_DIR/docker-compose.yml" up -d || die "Панель не запустилась, причина в логе."
    fi
    rw_init "http://127.0.0.1:3000"
    panel_wait || die "Панель на этом сервере не отвечает. Смотрите: cd $RW_DIR && docker compose logs remnawave"
    for d in "$(env_get "$RW_DIR/.env" PANEL_DOMAIN)" "$(env_get "$RW_DIR/.env" FRONT_END_DOMAIN)"; do
        if valid_domain "$d" && [[ $d != panel.domain.com ]]; then
            PANEL_DOMAIN=$d
            break
        fi
    done
    check_disk 3072
    ask_bot_domain
    ask_bot_token
    if panel_can_register; then ask_admin_new; else ask_panel_access; fi
    ask_rw_url "$PANEL_DOMAIN"
    ask_owner
    if [[ -d $SVBG_DIR/pg ]] || ! container_running remnawave-db; then
        DB_MODE=own
    else
        DB_MODE=panel db_label="отдельная база svbg в PostgreSQL панели"
    fi
    confirm_plan "Панель:          ${PANEL_DOMAIN:+https://$PANEL_DOMAIN}${PANEL_DOMAIN:-$RW_DIR}
Бот:             https://$BOT_DOMAIN ${BOT_NAME:+(@$BOT_NAME)}
Доступ к панели: $(access_label)
Бот ходит в:     $RW_URL
Вебхуки панели:  допишу $BOT_HOOK в .env панели и перезапущу её
Владелец:        $(owner_label)
База бота:       $db_label
Образ бота:      $(image_label)
"
    ensure_swap
    if [[ $DB_MODE == own ]]; then ensure_network "$NET"; fi
    panel_obtain_token
    sum=$(file_sum "$RW_DIR/.env")
    panel_hooks_env
    bot_install
    panel_reload_if_changed "$sum"
    caddy_setup bot
    open_ports
    save_state
    finish
}

flow_remote() {
    local v act
    MODE=remote DB_MODE=own NET=svbg-network
    while :; do
        ask v "Адрес панели" "Адрес панели Remnawave на другом сервере, вместе с https://
Например: https://panel.example.com" "$RW_URL"
        v=${v%/}
        if [[ $v =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
            RW_URL=$v
            break
        fi
        ui_msg "Адрес не подходит" "Нужен полный адрес с https://, например https://panel.example.com"
    done
    rw_init "$RW_URL"
    while :; do
        if [[ $RW_TOKEN_SET == 1 ]] && ui_yesno "API-токен панели" "У бота уже есть API-токен панели. Оставить его?"; then
            ADMIN_ACTION=keep
            break
        fi
        ask_secret RW_TOKEN "API-токен панели" "В панели: Настройки → API-токены → Создать, права «*».
Вставьте токен (символы не видны). Пусто — впишете потом в мастере бота."
        RW_TOKEN=${RW_TOKEN//[[:space:]]/}
        if [[ -z $RW_TOKEN ]]; then
            ADMIN_ACTION=skip
            break
        fi
        ADMIN_ACTION=token
        if RW_AUTH=$RW_TOKEN rw_req GET /api/system/stats </dev/null; then break; fi
        choose act "Панель не приняла токен" "Ответ панели: $RW_CODE $(rw_message)

Код 000 значит, что до адреса не достучаться; 401 и 403, что токен не тот." retry \
            retry "Ввести токен ещё раз" \
            keep "Оставить как есть, разберусь потом"
        if [[ $act == keep ]]; then break; fi
    done
    ask_bot_domain optional
    HOOK_SECRET=""
    if [[ -n $BOT_DOMAIN ]]; then
        while :; do
            ask_secret HOOK_SECRET "Секрет вебхуков панели" "Если в .env панели уже есть WEBHOOK_SECRET_HEADER (например, для другого бота), вставьте его.
Пусто — сгенерирую новый и положу готовые строки для .env панели в файл."
            HOOK_SECRET=${HOOK_SECRET//[[:space:]]/}
            if [[ -z $HOOK_SECRET ]]; then
                HOOK_SECRET=$(rand_hex 32)
                break
            fi
            if hook_secret_ok "$HOOK_SECRET"; then break; fi
            ui_msg "Секрет не подходит" "Бот принимает секрет из 32+ латинских букв и цифр. Если у панели другой, оставьте поле пустым и замените секрет в панели на новый."
        done
    fi
    ask_bot_token
    ask_owner
    confirm_plan "Панель:          $RW_URL
Доступ к панели: $(access_label)
Бот:             ${BOT_DOMAIN:+https://$BOT_DOMAIN}${BOT_DOMAIN:-без домена (кассы с вебхуками подключатся позже)} ${BOT_NAME:+(@$BOT_NAME)}
Владелец:        $(owner_label)
База бота:       свой контейнер postgres:17-alpine
Образ бота:      $(image_label)
"
    check_disk 3072
    ensure_swap
    ensure_docker
    ensure_network "$NET"
    bot_install
    if [[ -n $BOT_DOMAIN ]]; then
        caddy_setup bot
        {
            echo "# Строки для .env панели Remnawave (на сервере панели). Потом: docker compose up -d --force-recreate remnawave"
            echo "# Если WEBHOOK_URL уже задан для другого бота, допишите наш адрес через запятую, без пробела."
            echo "WEBHOOK_ENABLED=true"
            echo "WEBHOOK_URL=https://$BOT_DOMAIN/webhooks/remnawave"
            echo "WEBHOOK_SECRET_HEADER=$HOOK_SECRET"
        } >"$SVBG_DIR/panel-webhook.env"
        chmod 600 "$SVBG_DIR/panel-webhook.env"
    fi
    open_ports
    save_state
    finish
}

git_pull() {
    local owner g
    if [[ ! -d $SRC_DIR/.git ]]; then
        warn "$SRC_DIR не git-репозиторий, скрипты не обновляю"
        return 0
    fi
    step "Забираю свежие скрипты (git pull)"
    owner=$(stat -c %U "$SRC_DIR")
    g=(git -C "$SRC_DIR" -c "safe.directory=$SRC_DIR" pull --ff-only)
    if [[ $owner != root ]] && id "$owner" >/dev/null 2>&1; then g=(runuser -u "$owner" -- "${g[@]}"); fi
    if run "${g[@]}"; then
        ok "Код: $(git -C "$SRC_DIR" -c "safe.directory=$SRC_DIR" log -1 --format='%h %s' 2>/dev/null || echo обновлён)"
    else
        warn "git pull не прошёл (нет сети до GitHub или есть локальные правки). Проверьте: git -C $SRC_DIR status. Продолжаю с тем, что лежит в папке."
    fi
}

src_rev() { git -C "$SRC_DIR" -c "safe.directory=$SRC_DIR" rev-parse -q --verify HEAD 2>/dev/null || true; }

# scripts_changed коммит: поменялось ли что-то в deploy/ с этого коммита до текущего.
scripts_changed() {
    local new
    new=$(src_rev)
    if [[ -z $1 || -z $new || $1 == "$new" ]]; then return 1; fi
    ! git -C "$SRC_DIR" -c "safe.directory=$SRC_DIR" diff --quiet "$1" "$new" -- deploy/ 2>/dev/null
}

flow_update() {
    local from sum cur_id new_id tmp same_compose="" args=(--update)
    load_state
    [[ -f $SVBG_DIR/docker-compose.yml && -n $NET ]] ||
        die "Бот не установлен этим установщиком. Запустите: sudo bash $SRC_DIR/deploy/install.sh"
    if [[ -n ${SVBG_UPDATE_FROM+x} ]]; then
        # Сюда попадаем из прежней версии установщика: git pull она уже сделала.
        from=$SVBG_UPDATE_FROM
        unset SVBG_UPDATE_FROM
    else
        step "Обновление SvBG Shop"
        from=$(src_rev)
        sum=$(file_sum "$SCRIPT_DIR/install.sh")
        git_pull
        if [[ $(file_sum "$SCRIPT_DIR/install.sh") != "$sum" ]]; then
            ok "Установщик обновился, дальше работает новая версия"
            if [[ $FORCE == 1 ]]; then args+=(--force); fi
            export SVBG_UPDATE_FROM=$from
            exec bash "$SCRIPT_DIR/install.sh" "${args[@]}"
        fi
    fi
    cur_id=$(bot_image_id)
    bot_image
    new_id=$(image_id "$BOT_IMAGE")
    tmp=$(mktemp)
    render_compose "$tmp"
    if [[ $(file_sum "$tmp") == "$(file_sum "$SVBG_DIR/docker-compose.yml")" ]]; then same_compose=1; fi
    rm -f "$tmp"
    if [[ -n $cur_id && $cur_id == "$new_id" && -n $same_compose ]] && ! scripts_changed "$from" && bot_running; then
        install_cli
        save_state
        ok "Образ и скрипты те же, что уже работают. Бот не перезапускаю."
        return 0
    fi
    keep_previous "$cur_id" "$new_id"
    if bot_running; then
        step "Бэкап перед обновлением"
        if docker exec svbg-shop python -m svbg backup --reason pre_update >>"$LOG" 2>&1; then
            ok "Бэкап в $DATA/backups"
        elif [[ $FORCE == 1 ]]; then
            warn "Бэкап не получился, продолжаю без него (--force)"
        else
            die "Бэкап не получился, обновление остановлено. Причина в логе. Обновить без бэкапа: svbg update --force"
        fi
    fi
    if [[ $DB_MODE == own ]]; then ensure_network "$NET"; fi
    render_compose
    install_cli
    save_state
    step "Миграции базы"
    run compose_bot stop bot || true
    if ! run compose_bot run --rm -T bot python -m svbg migrate </dev/null; then
        warn "Миграции не прошли. Откат: docker tag $PREV_IMAGE $BOT_IMAGE && svbg up"
        die "Миграции не прошли, бот остановлен. Причина в логе."
    fi
    ok "База в актуальной схеме"
    run compose_bot up -d --remove-orphans
    if bot_wait; then
        ok "Бот обновлён и отвечает"
    else
        warn "Бот не ответил на проверку. Посмотрите svbg logs. Откат: docker tag $PREV_IMAGE $BOT_IMAGE && svbg up"
    fi
    drop_stale_images
    if [[ -n $cur_id && $cur_id != "$new_id" ]]; then
        echo
        echo "Если что-то пошло не так, прежний образ сохранён как $PREV_IMAGE:"
        echo "  docker tag $PREV_IMAGE $BOT_IMAGE && svbg up"
        echo "  и при необходимости: svbg restore $DATA/backups/<файл pre_update>"
    fi
}

flow_remove() {
    local wipe=0 keep_copy=0 dropdb=0 copy="" pg_user pg_db
    load_state
    if [[ ! -f $SVBG_DIR/docker-compose.yml ]]; then
        ui_msg "Удалять нечего" "Бот не установлен: нет $SVBG_DIR/docker-compose.yml."
        return 0
    fi
    ui_yesno "Удалить бота?" "Остановлю и удалю контейнер бота и его образ, уберу бота из Caddy и из вебхуков панели.
Панель Remnawave, страница подписки и Caddy останутся.

Про данные (/opt/svbg/data) спрошу отдельно." no || return 0
    if ui_yesno "Данные бота" "Удалить и данные бота: настройки, бэкапы, файлы ($DATA)?
Вернуть их будет нельзя." no; then
        wipe=1
        if ui_yesno "Копия бэкапов" "Перед удалением сделать свежий бэкап и сохранить все бэкапы в /root?"; then keep_copy=1; fi
        if [[ $DB_MODE == panel ]] && ui_yesno "База в PostgreSQL панели" "Удалить базу svbg и роль svbg из PostgreSQL панели?" no; then dropdb=1; fi
    fi
    clear || true
    step "Удаляю бота"
    if [[ $keep_copy == 1 ]]; then
        if bot_running; then docker exec svbg-shop python -m svbg backup --reason pre_remove >>"$LOG" 2>&1 || warn "Свежий бэкап не получился"; fi
        if [[ -d $DATA/backups ]]; then
            copy=/root/svbg-backups-$(date +%Y%m%d-%H%M%S)
            cp -a "$DATA/backups" "$copy"
            ok "Бэкапы скопированы в $copy"
        fi
    fi
    run compose_bot down --remove-orphans || true
    caddy_remove_bot
    if panel_present && grep -qF "$BOT_HOOK" "$RW_DIR/.env"; then panel_hooks_remove; fi
    if [[ $dropdb == 1 ]] && container_running remnawave-db; then
        pg_user=$(env_get "$RW_DIR/.env" POSTGRES_USER)
        pg_db=$(env_get "$RW_DIR/.env" POSTGRES_DB)
        docker exec -i remnawave-db psql -q -U "${pg_user:-postgres}" -d "${pg_db:-postgres}" >>"$LOG" 2>&1 <<'SQL' || warn "Базу svbg удалить не вышло, смотрите лог"
DROP DATABASE IF EXISTS svbg WITH (FORCE);
DROP ROLE IF EXISTS svbg;
SQL
    fi
    run docker image rm "$IMAGE" "$LOCAL_IMAGE" "$PREV_IMAGE" || true
    rm -f "$SVBG_BIN"
    if [[ $wipe == 1 ]]; then
        rm -rf "$SVBG_DIR"
        ok "Бот и его данные удалены"
    else
        rm -f "$SVBG_DIR/docker-compose.yml" "$STATE"
        ok "Бот удалён, данные остались в $DATA"
    fi
}

usage() {
    cat <<'TXT'
SvBG Shop: установщик.

  sudo bash deploy/install.sh            меню: установка, обновление, статус, удаление
  sudo bash deploy/install.sh --update   обновить бота (git pull, свежий образ, бэкап, миграции, перезапуск)
  sudo bash deploy/install.sh --status   что запущено и сколько памяти занято

Образ бота (выбор запоминается в /opt/svbg/install.conf):
  SVBG_IMAGE=ghcr.io/biggsm0ke/svbg-shop:1.2.0   скачивать этот тег вместо latest
  SVBG_BUILD_LOCAL=1                            собирать образ на этом сервере (0 вернёт готовый)
TXT
}

# Пункт меню «Образ бота»: скачивать готовый или собирать здесь.
choose_image() {
    local how cur=pull mem
    mem=$(mem_mb)
    if [[ $BUILD_LOCAL == 1 ]]; then cur=build; fi
    choose how "Образ бота" "Обычно установщик скачивает готовый образ:
  $IMAGE
Его собирает GitHub для amd64 и arm64.

Собрать образ можно и здесь, из $SRC_DIR. Это 3–7 минут, а памяти у сервера $mem МБ: если она меньше 2 ГБ, сборка может подвесить сервер." "$cur" \
        pull "Скачивать готовый образ" \
        build "Собрать образ на этом сервере"
    if [[ $how == build ]]; then BUILD_LOCAL=1; else BUILD_LOCAL=""; fi
    if [[ -f $STATE ]]; then save_state; fi
    ui_msg "Образ бота" "Запомнил: $(image_label).
Сработает при установке или обновлении."
}

main() {
    local choice start=full
    case ${1:-} in
        -h | --help)
            usage
            return 0
            ;;
    esac
    need_root
    trap 'on_err $LINENO' ERR
    trap cleanup EXIT
    start_log "$@"
    case ${1:-} in
        --update)
            if [[ ${2:-} == --force ]]; then FORCE=1; fi
            flow_update
            return 0
            ;;
        --status)
            load_state
            status_text
            return 0
            ;;
        "") ;;
        *)
            usage
            return 2
            ;;
    esac
    need_tty
    check_os
    install_deps
    ui_init
    load_state
    if [[ -f $STATE ]]; then start=update; fi
    while :; do
        choose choice "SvBG Shop" "Бот продажи VPN для панели Remnawave.$([[ -f $STATE ]] && printf '\n\nБот уже установлен на этом сервере. Чтобы поставить новую версию, выберите «Обновить».')

Что делаем?" "$start" \
            full "Всё с нуля: панель Remnawave, страница подписки, Caddy, бот" \
            near "Бот рядом с панелью, которая уже стоит на этом сервере" \
            remote "Только бот, панель на другом сервере" \
            update "Обновить бота" \
            status "Статус" \
            image "Образ бота: скачать готовый или собрать здесь" \
            remove "Удалить бота" \
            quit "Выход"
        case $choice in
            full)
                flow_full
                break
                ;;
            near)
                if panel_present; then
                    flow_near
                    break
                fi
                ui_msg "Панель не найдена" "В $RW_DIR нет docker-compose.yml и .env панели.
Поставьте всё с нуля (первый пункт) или выберите «Только бот», если панель на другом сервере."
                ;;
            remote)
                flow_remote
                break
                ;;
            update)
                clear || true
                flow_update
                break
                ;;
            status) ui_msg "Статус" "$(status_text 2>/dev/null || echo 'Docker не установлен')" ;;
            image) choose_image ;;
            remove)
                flow_remove
                break
                ;;
            quit)
                clear || true
                break
                ;;
        esac
    done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
