#!/usr/bin/env bash
# VerseVPS control script - Made by Verseedit
#
#   ./vpsctl.sh start | stop | restart | status | logs | update
#   ./vpsctl.sh tunnel            interactive Cloudflare Tunnel setup (dashboard on https)
#   ./vpsctl.sh tunnel quick      free https://xxxx.trycloudflare.com address, no account needed
#   ./vpsctl.sh tunnel token <TOKEN-or-"cloudflared service install TOKEN"> <https://panel.yourdomain.com>
#   ./vpsctl.sh tunnel status | stop
cd "$(dirname "$(readlink -f "$0")")" || exit 1
APP_DIR="$(pwd)"
PYBIN="$(cat .pybin 2>/dev/null || echo python3)"
mkdir -p data
PIDFILE=data/run.pid; LOG=data/bot.log
TPID=data/tunnel.pid; TLOG=data/tunnel.log; TCONF=data/tunnel.conf

c_ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
c_die()  { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

is_root() { [ "$(id -u)" = 0 ]; }
has_unit() { is_root && [ -f "/etc/systemd/system/$1.service" ]; }
is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

set_env() {  # set_env KEY VALUE  (.env stays private)
  touch .env; chmod 600 .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

# ------------------------------------------------------------------ bot
cmd_run() {  # foreground supervisor (used by nohup and systemd-less hosts)
  while true; do
    "$PYBIN" main.py; rc=$?
    case $rc in 2|3) echo "[vpsctl] fatal config problem (exit $rc) - not restarting. Fix .env, then: ./vpsctl.sh start"; break;; esac
    echo "[vpsctl] bot exited ($rc) - restarting in 5s"; sleep 5
  done
}

cmd_start() {
  if has_unit versevps; then systemctl start versevps
  elif is_running; then c_warn "already running (pid $(cat "$PIDFILE"))"
  else nohup setsid bash "$0" run >>"$LOG" 2>&1 & echo $! >"$PIDFILE"; fi
  if [ -f "$TCONF" ] && ! has_unit versevps-tunnel && ! tunnel_running; then tunnel_start_bg; fi
  c_ok "started"
}

cmd_stop() {
  if has_unit versevps; then systemctl stop versevps
  elif [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  c_ok "stopped"
}

cmd_restart() { cmd_stop; sleep 1; cmd_start; }

cmd_status() {
  IFS='|' read -r state bot panel port ts backend isolated < <("$PYBIN" ctl.py status-line 2>/dev/null)
  if has_unit versevps; then systemctl is-active --quiet versevps && run="running (systemd)" || run="stopped"
  elif is_running; then run="running (pid $(cat "$PIDFILE"))"; else run="stopped"; fi
  echo "Process : $run"
  echo "Bot     : ${state:-unknown} ${bot:+($bot)}"
  echo "Mode    : ${backend:-?}${isolated:+  isolated=$isolated}"
  live="$("$PYBIN" ctl.py show 2>/dev/null)"; [ -n "$live" ] && panel="$live"
  echo "Panel   : ${panel:-?}"
  tunnel_running && echo "Tunnel  : running" || echo "Tunnel  : off"
}

cmd_logs() { if has_unit versevps; then journalctl -u versevps -f -n 80; else tail -n 80 -f "$LOG"; fi; }

cmd_update() {
  [ -d .git ] || c_die "Not a git install. Re-run the installer to update."
  git fetch -q origin && git reset -q --hard "origin/$(git rev-parse --abbrev-ref HEAD)"
  "$PYBIN" -m pip install -q -r requirements.txt 2>/dev/null || true
  c_ok "updated"; cmd_restart
}

# --------------------------------------------------------------- tunnel
tunnel_running() {
  if has_unit versevps-tunnel; then systemctl is-active --quiet versevps-tunnel; return; fi
  [ -f "$TPID" ] && kill -0 "$(cat "$TPID")" 2>/dev/null
}

get_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then command -v cloudflared; return; fi
  if [ -x data/bin/cloudflared ]; then echo "$APP_DIR/data/bin/cloudflared"; return; fi
  case "$(uname -m)" in x86_64|amd64) a=amd64;; aarch64|arm64) a=arm64;; armv7l|armv6l|arm) a=arm;; i386|i686) a=386;;
    *) c_die "Unsupported CPU for cloudflared: $(uname -m)";; esac
  mkdir -p data/bin
  c_warn "Downloading cloudflared (official Cloudflare release)..."
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$a"
  { curl -fsSL "$url" -o data/bin/cloudflared || wget -qO data/bin/cloudflared "$url"; } 2>/dev/null \
    || c_die "Could not download cloudflared. Install it yourself, then retry."
  chmod +x data/bin/cloudflared
  echo "$APP_DIR/data/bin/cloudflared"
}

panel_port() { "$PYBIN" -c "import json;print(json.load(open('data/status.json')).get('port',5000))" 2>/dev/null || echo 5000; }

cmd__tunnel_loop() {  # internal: supervised cloudflared, restarts forever
  # shellcheck disable=SC1090
  . "$TCONF"
  CF="$(get_cloudflared)"
  while true; do
    if [ "$MODE" = token ]; then
      "$CF" tunnel --no-autoupdate run --token "$TOKEN" >>"$TLOG" 2>&1 &
      cpid=$!
      "$PYBIN" ctl.py set-url "$URL" >/dev/null 2>&1
      wait "$cpid"
    else
      : >"$TLOG"
      "$CF" tunnel --no-autoupdate --url "http://127.0.0.1:$(panel_port)" >>"$TLOG" 2>&1 &
      cpid=$!
      u=""
      for _ in $(seq 1 90); do
        u="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TLOG" 2>/dev/null | head -1)"
        [ -n "$u" ] && break; sleep 1
      done
      [ -n "$u" ] && "$PYBIN" ctl.py set-url "$u" >/dev/null 2>&1
      wait "$cpid"
    fi
    sleep 5
  done
}

tunnel_start_bg() {
  if is_root && [ -d /run/systemd/system ]; then
    cat >/etc/systemd/system/versevps-tunnel.service <<EOF
[Unit]
Description=VerseVPS Cloudflare Tunnel (Made by Verseedit)
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$APP_DIR
ExecStart=/bin/bash $APP_DIR/vpsctl.sh _tunnel_loop
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload && systemctl enable versevps-tunnel >/dev/null 2>&1; systemctl restart versevps-tunnel
  else
    nohup setsid bash "$0" _tunnel_loop >>"$TLOG" 2>&1 & echo $! >"$TPID"
  fi
}

tunnel_stop() {
  if has_unit versevps-tunnel; then systemctl disable --now versevps-tunnel >/dev/null 2>&1; rm -f /etc/systemd/system/versevps-tunnel.service; systemctl daemon-reload; fi
  if [ -f "$TPID" ]; then pid="$(cat "$TPID")"; kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true; rm -f "$TPID"; fi
  rm -f "$TCONF"; "$PYBIN" ctl.py clear-url >/dev/null 2>&1 || true
  c_ok "tunnel stopped; dashboard URL reset to the server address"
}

cmd_tunnel() {
  local sub="${1:-}"; [ $# -gt 0 ] && shift
  case "$sub" in
    stop) tunnel_stop; return;;
    status) tunnel_running && c_ok "tunnel running" || c_warn "tunnel not running"
            [ -f "$TCONF" ] && grep -o '^MODE=.*' "$TCONF"; echo "Panel URL: $("$PYBIN" ctl.py show)"; return;;
    "")
      echo "Cloudflare Tunnel for the dashboard - Made by Verseedit"
      echo "  1) Quick      free, no account, random https://xxxx.trycloudflare.com address"
      echo "  2) Permanent  your own domain, needs a tunnel token from Cloudflare Zero Trust"
      [ -r /dev/tty ] || c_die "No terminal. Use: ./vpsctl.sh tunnel quick   or   ./vpsctl.sh tunnel token TOKEN https://panel.example.com"
      read -rp "Choose [1/2]: " ch </dev/tty
      if [ "$ch" = 2 ]; then sub=token; else sub=quick; fi;;
  esac
  case "$sub" in
    quick) MODE=quick; TOKEN=""; URL="";;
    token)
      MODE=token; raw="${1:-}"; URL="${2:-}"
      if [ -z "$raw" ]; then
        echo; echo "In Cloudflare: Zero Trust > Networks > Tunnels > Create a tunnel > Cloudflared."
        echo "Copy the token (or the whole 'cloudflared service install ...' command)."
        read -rsp "Tunnel token: " raw </dev/tty; echo
      fi
      TOKEN="$(printf '%s' "$raw" | grep -oE 'eyJ[A-Za-z0-9=_-]+' | head -1)"
      [ -n "$TOKEN" ] || c_die "That does not look like a Cloudflare tunnel token (it starts with eyJ...)."
      if [ -z "$URL" ]; then
        echo "In the tunnel's Public Hostname tab add your hostname with Service = HTTP, URL = localhost:$(panel_port)"
        read -rp "Your public hostname (e.g. panel.example.com): " URL </dev/tty
      fi
      URL="${URL#http://}"; URL="${URL#https://}"; URL="https://${URL%/}";;
    *) c_die "usage: ./vpsctl.sh tunnel [quick | token <TOKEN> <https://host> | status | stop]";;
  esac

  tunnel_stop >/dev/null 2>&1 || true
  umask 077
  printf 'MODE=%q\nTOKEN=%q\nURL=%q\n' "$MODE" "$TOKEN" "$URL" >"$TCONF"
  set_env TRUST_PROXY true            # real visitor IP + https cookies behind the tunnel
  if is_running || has_unit versevps; then cmd_restart >/dev/null 2>&1; fi
  tunnel_start_bg
  c_ok "tunnel starting..."
  for _ in $(seq 1 60); do
    u="$("$PYBIN" ctl.py show 2>/dev/null)"
    case "$u" in https://*) break;; esac; sleep 1
  done
  case "$u" in
    https://*) c_ok "Dashboard is now at: $u"; echo "   (new logins and the bot's DMs use this address; the server IP:port still works too)";;
    *) c_warn "No tunnel address yet. Check: ./vpsctl.sh tunnel status   and   tail $TLOG";;
  esac
  [ "$MODE" = quick ] && echo "   Quick tunnels get a new address when the tunnel restarts; the bot picks it up automatically."
}

case "${1:-}" in
  run) cmd_run;;
  start) cmd_start;;
  stop) cmd_stop;;
  restart) cmd_restart;;
  status) cmd_status;;
  logs) cmd_logs;;
  update) cmd_update;;
  tunnel) shift; cmd_tunnel "$@";;
  _tunnel_loop) cmd__tunnel_loop;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1;;
esac
