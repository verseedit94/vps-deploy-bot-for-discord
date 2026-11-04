#!/usr/bin/env bash
# VerseVPS one-click installer - Made by Verseedit
#
#   curl -fsSL https://raw.githubusercontent.com/verseedit94/vps-deploy-bot-for-discord/main/install.sh | bash
#
# It installs everything, asks for your Discord bot token, then your admin Discord ID,
# starts the bot + dashboard and waits until the bot is online.
# Works on any Linux: a real VPS (LXD/Docker), CodeSandbox, Replit, Codespaces, Gitpod, Termux-proot ...
#
# Optional variables:  DISCORD_TOKEN= ADMIN_IDS= (skip the questions)   PANEL_PORT=5000
#                      RECONFIGURE=1 (enter a new token)   AUTO_DOCKER=0 (do not install Docker)
set -Eeuo pipefail

REPO="${REPO:-https://github.com/verseedit94/vps-deploy-bot-for-discord.git}"
BRANCH="${BRANCH:-main}"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'; C_OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%s✘ %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }
trap 'printf "%s✘ Installer stopped at line %s.%s\n" "$C_ERR" "$LINENO" "$C_OFF" >&2' ERR

printf '\n%s╔══════════════════════════════════════╗\n║   VerseVPS installer                 ║\n║   Made by Verseedit                  ║\n╚══════════════════════════════════════╝%s\n\n' "$C_B" "$C_OFF"

is_root() { [ "$(id -u)" = 0 ]; }
have() { command -v "$1" >/dev/null 2>&1; }
systemd_ok() { is_root && have systemctl && [ -d /run/systemd/system ]; }

# ---------------------------------------------------------- 1. source code
SELF="${BASH_SOURCE[0]:-}"
SELF_DIR=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then SELF_DIR="$(cd "$(dirname "$SELF")" && pwd)"; fi

if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/main.py" ]; then
  APP_DIR="$SELF_DIR"                       # run from an extracted copy: install in place
  ok "Using the code in $APP_DIR"
else
  case "$REPO" in *YOUR_GITHUB''_USER*) die "REPO is not set. Use the install command from your repo README.";; esac
  if is_root; then APP_DIR="${APP_DIR:-/opt/versevps}"; else APP_DIR="${APP_DIR:-$HOME/versevps}"; fi
  say "Downloading VerseVPS into $APP_DIR ..."
  mkdir -p "$APP_DIR"
  if have git && { [ -d "$APP_DIR/.git" ] || [ -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]; }; then
    if [ -d "$APP_DIR/.git" ]; then
      git -C "$APP_DIR" fetch -q origin "$BRANCH" && git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
    else
      git clone -q --depth 1 -b "$BRANCH" "$REPO" "$APP_DIR"
    fi
  else
    have curl && have tar || die "Need git, or curl + tar, to download the code."
    curl -fsSL "${REPO%.git}/archive/refs/heads/$BRANCH.tar.gz" | tar -xz --strip-components=1 -C "$APP_DIR"
  fi
  ok "Code ready"
fi
cd "$APP_DIR"
chmod +x vpsctl.sh install.sh 2>/dev/null || true

# -------------------------------------------------------------- 2. python
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  if is_root && have apt-get; then apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
  elif is_root && have apk; then apk add --no-cache python3 py3-pip >/dev/null
  elif have pkg; then pkg install -y python >/dev/null
  fi
  PY="$(command -v python3 || true)"
fi
[ -n "$PY" ] || die "Python 3.9+ is required and could not be installed automatically."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9+ is required (found $("$PY" -V 2>&1))."

PYBIN=""
if [ -x "$APP_DIR/venv/bin/python" ]; then PYBIN="$APP_DIR/venv/bin/python"
elif "$PY" -m venv "$APP_DIR/venv" >/dev/null 2>&1; then PYBIN="$APP_DIR/venv/bin/python"
else
  rm -rf "$APP_DIR/venv"
  if is_root && have apt-get; then
    apt-get install -y -qq python3-venv python3-pip >/dev/null 2>&1 || true
    "$PY" -m venv "$APP_DIR/venv" >/dev/null 2>&1 && PYBIN="$APP_DIR/venv/bin/python" || rm -rf "$APP_DIR/venv"
  fi
fi
SYSPIP=0
if [ -z "$PYBIN" ]; then PYBIN="$PY"; SYSPIP=1; warn "No virtualenv available - installing packages for your user instead."; fi

if [ "${SKIP_DEPS:-0}" = 1 ]; then
  warn "SKIP_DEPS=1 - not installing Python packages"
elif "$PYBIN" -c 'import discord, flask, dotenv, psutil, waitress' 2>/dev/null; then
  ok "Python packages already installed"
else
  say "Installing Python packages (1-2 minutes) ..."
  pipi() { "$PYBIN" -m pip install -q --disable-pip-version-check "$@"; }
  if [ "$SYSPIP" = 0 ]; then pipi -r requirements.txt
  else pipi --user -r requirements.txt 2>/dev/null || pipi --user --break-system-packages -r requirements.txt \
       || die "pip failed. Install pip (apt install python3-pip) and run the installer again."; fi
  ok "Python packages installed"
fi
printf '%s' "$PYBIN" > .pybin

# ------------------------------------------------------ 3. token + admin
ENV_FILE="$APP_DIR/.env"
validate_token() {  # exit 0 = valid (prints "id|name"), 1 = rejected, 2 = could not reach Discord
  "$PYBIN" - "$1" <<'PY'
import json, sys, urllib.error, urllib.request
req = urllib.request.Request("https://discord.com/api/v10/users/@me", headers={
    "Authorization": "Bot " + sys.argv[1], "User-Agent": "DiscordBot (https://github.com/Verseedit, 1.0)"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
        print(f"{d['id']}|{d['username']}")
except urllib.error.HTTPError as e:
    sys.exit(1 if e.code in (401, 403) else 2)
except Exception:
    sys.exit(2)
PY
}

BOT_ID=""
if [ -f "$ENV_FILE" ] && grep -q '^DISCORD_TOKEN=.\+' "$ENV_FILE" && [ "${RECONFIGURE:-0}" != 1 ]; then
  ok "Existing configuration found (run with RECONFIGURE=1 to change the token or admin)"
else
  say; say "${C_B}Step 1/2 - Discord bot token${C_OFF}"
  say "${C_DIM}discord.com/developers/applications > your app > Bot > Reset Token. Also switch ON \"Message Content Intent\" on that page.${C_OFF}"
  TOKEN="${DISCORD_TOKEN:-}"; PROVIDED=0; [ -n "$TOKEN" ] && PROVIDED=1; tries=0
  while :; do
    if [ -z "$TOKEN" ]; then
      [ -r /dev/tty ] || die "No terminal to ask for the token. Run again with DISCORD_TOKEN=... ADMIN_IDS=..."
      read -rsp "Bot token (typing is hidden): " TOKEN </dev/tty; echo
    fi
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]"'"'")"
    rc=0; INFO="$(validate_token "$TOKEN")" || rc=$?
    if [ "$rc" = 0 ]; then BOT_ID="${INFO%%|*}"; ok "Token is valid - bot: ${INFO#*|}"; break
    elif [ "$rc" = 2 ]; then warn "Could not reach Discord to check the token - continuing anyway."; break
    else
      warn "Discord rejected that token."
      [ "$PROVIDED" = 1 ] && die "The DISCORD_TOKEN you passed is invalid."
      TOKEN=""; tries=$((tries + 1)); [ "$tries" -ge 4 ] && die "Too many wrong tokens."
    fi
  done

  say; say "${C_B}Step 2/2 - Your Discord user ID (the bot admin)${C_OFF}"
  say "${C_DIM}Discord > Settings > Advanced > Developer Mode ON, then right-click your name > Copy User ID. Several admins: separate with commas.${C_OFF}"
  ADMINS="${ADMIN_IDS:-}"; tries=0
  while :; do
    if [ -z "$ADMINS" ]; then
      [ -r /dev/tty ] || die "No terminal to ask for the admin ID. Run again with ADMIN_IDS=..."
      read -rp "Admin user ID: " ADMINS </dev/tty
    fi
    ADMINS="$(printf '%s' "$ADMINS" | tr -d '[:space:]')"
    if printf '%s' "$ADMINS" | grep -qE '^[0-9]{15,20}(,[0-9]{15,20})*$'; then break; fi
    warn "That is not a Discord ID (it is a number with 17-19 digits)."
    [ -n "${ADMIN_IDS:-}" ] && die "The ADMIN_IDS you passed is invalid."
    ADMINS=""; tries=$((tries + 1)); [ "$tries" -ge 4 ] && die "Too many wrong IDs."
  done
  ( umask 077
    { echo "# VerseVPS - Made by Verseedit"
      echo "DISCORD_TOKEN=$TOKEN"
      echo "ADMIN_IDS=$ADMINS"
      echo "PANEL_PORT=${PANEL_PORT:-5000}"
      echo "BACKEND=auto"
    } > "$ENV_FILE" )
  chmod 600 "$ENV_FILE"
  ok "Saved configuration to .env (private)"
fi

# ------------------------------------------------- 3b. openssl (root passwords)
if ! have openssl; then
  if is_root && have apt-get; then apt-get install -y -qq openssl >/dev/null 2>&1 || true
  elif is_root && have apk; then apk add --no-cache openssl >/dev/null 2>&1 || true
  elif have pkg; then pkg install -y openssl-tool >/dev/null 2>&1 || true; fi
fi
have openssl || warn "openssl is missing - install it (apt install openssl), the bot needs it to set root passwords."

# ------------------------------------------- 4. best backend for this host
if [ "${AUTO_DOCKER:-1}" = 1 ] && is_root && have apt-get && systemd_ok && ! have docker && ! have lxc; then
  VIRT="$(systemd-detect-virt -c 2>/dev/null || true)"
  if [ -z "$VIRT" ] || [ "$VIRT" = none ]; then
    say "Installing Docker so users get real isolated containers ..."
    if apt-get install -y -qq docker.io >/dev/null 2>&1 && systemctl enable --now docker >/dev/null 2>&1; then ok "Docker installed"
    else warn "Docker install failed - the bot will use sandbox mode."; fi
  fi
fi

# ---------------------------------------------------------- 5. firewall
PORT="$(grep -E '^PANEL_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2)"; PORT="${PORT:-5000}"
if is_root; then
  if have ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null 2>&1 && ok "Firewall: opened port $PORT"
    ufw allow 20000:29999/tcp >/dev/null 2>&1 && ok "Firewall: opened SSH ports 20000-29999"; fi
  if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT"/tcp --add-port=20000-29999/tcp >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 && ok "Firewall: opened port $PORT + SSH ports 20000-29999"; fi
fi

# ------------------------------------------------------------ 6. start
START_TS="$(date +%s)"
say "Starting VerseVPS ..."
if systemd_ok; then
  cat >/etc/systemd/system/versevps.service <<EOF
[Unit]
Description=VerseVPS bot + dashboard (Made by Verseedit)
After=network-online.target docker.service snap.lxd.daemon.service
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$APP_DIR
ExecStart=$PYBIN $APP_DIR/main.py
Restart=always
RestartSec=5
RestartPreventExitStatus=2 3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable versevps >/dev/null 2>&1
  systemctl restart versevps
  [ -f data/tunnel.conf ] && ./vpsctl.sh start >/dev/null 2>&1 || true
else
  ./vpsctl.sh restart >/dev/null 2>&1
  if have crontab; then  # come back after a reboot where possible
    ( crontab -l 2>/dev/null | grep -v "versevps-autostart" ; echo "@reboot cd $APP_DIR && ./vpsctl.sh start >/dev/null 2>&1 # versevps-autostart" ) | crontab - 2>/dev/null || true
  fi
fi

# ------------------------------------------------------------ 7. wait
say "Waiting for the bot to come online ..."
STATE=""; BOT=""; PANEL=""; BACKEND=""; ISO=""; extra=0
for _ in $(seq 1 "${WAIT_SECS:-120}"); do
  IFS='|' read -r STATE BOT PANEL _port TS BACKEND ISO < <("$PYBIN" ctl.py status-line 2>/dev/null || echo "|||||||") || true
  [ "${TS:-0}" -ge "$START_TS" ] 2>/dev/null || { STATE=""; }
  case "$STATE" in
    bad_token|intents|no_token) break;;
    online) [ -n "$PANEL" ] && break; extra=$((extra + 1)); [ "$extra" -ge 15 ] && break;;
  esac
  sleep 1
done

echo
case "$STATE" in
  online)
    printf '%s╔═ VerseVPS is ONLINE ═══════════════════════%s\n' "$C_OK" "$C_OFF"
    ok "Bot        : $BOT"
    ok "Dashboard  : ${PANEL:-http://<server-ip>:$PORT}"
    ok "Mode       : ${BACKEND:-?}"
    [ "$BACKEND" = "proc" ] && warn "No Docker/LXD here: the bot cannot give root, so it will NOT create VPS. Install Docker (apt install docker.io) and run: ./vpsctl.sh restart"
    [ "$ISO" = "False" ] && warn "Sandbox mode without user isolation: VPS users can read this bot's files (incl. the token). Only give VPS to people you trust."
    [ -n "$BOT_ID" ] && { echo; say "Invite the bot to your server:"; say "  https://discord.com/oauth2/authorize?client_id=$BOT_ID&scope=bot&permissions=83968"; }
    cat <<EOF

Discord commands
  !register <username> <password>  your account (do it in a DM with the bot) - same login for SSH + dashboard
  !deploy                          free ROOT VPS (admins edit the plan with !deployedit <ram> <ssd> <cpu>)
  !create @user <ram> <ssd> <cpu>  admin: VPS for a registered user (SSH + login go to THEIR DM)
  !manage                          Start / Stop / Restart / Stats / SSH / SSHX terminal
  !help                            everything else

SSH ports 20000-29999 must be reachable from the internet (cloud firewall / security group).

Server commands (run inside $APP_DIR)
  ./vpsctl.sh status | logs | restart | stop | update
  ./vpsctl.sh tunnel               put the dashboard behind a Cloudflare Tunnel (https)

Made by Verseedit
EOF
    ;;
  bad_token)  die "Discord rejected the token. Run again with RECONFIGURE=1";;
  intents)    die "Turn ON \"Message Content Intent\": Developer Portal > your app > Bot > Privileged Gateway Intents. Then: ./vpsctl.sh restart";;
  no_token)   die "No token in .env. Run again with RECONFIGURE=1";;
  *)
    warn "The bot did not report online in time. Last log lines:"
    { systemd_ok && journalctl -u versevps -n 25 --no-pager 2>/dev/null || tail -n 25 data/bot.log 2>/dev/null; } || true
    die "Fix the problem above, then run: ./vpsctl.sh restart"
    ;;
esac
