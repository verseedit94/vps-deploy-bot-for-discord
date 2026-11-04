"""VerseVPS core: config, storage, auth, VPS backends and operations.

Made by Verseedit.

One process runs the Discord bot AND the web panel, so every VPS operation shares the
same locks (see op_guard): Discord and the dashboard can never run two power /
reinstall operations on the same VPS at the same time.
"""
import hashlib
import hmac
import json
import logging
import math
import os
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import psutil
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("versevps")
CREDIT = "Made by Verseedit"
VERSION = "1.0"
MiB = 1024 * 1024


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _bool(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------- config
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DATA_DIR, 0o711)  # other unix users may traverse (sandbox mode) but not list
except OSError:
    pass
DB_PATH = DATA_DIR / "verse.db"
STATUS_FILE = DATA_DIR / "status.json"

PORT = _int("PANEL_PORT", os.getenv("PORT", "5000"))
PANEL_BIND = os.getenv("PANEL_BIND", "0.0.0.0")
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 4096)
MAX_EDIT = 2 * 1024 * 1024          # text editor limit (checked BEFORE reading)
MAX_OUT = 200_000                    # terminal output cap
CMD_TIMEOUT = _int("CMD_TIMEOUT", 300)   # web-terminal limit per command (apt install etc.); SSH/SSHX have none
DOCKER_EXTRA_ARGS = shlex.split(os.getenv("DOCKER_EXTRA_ARGS", ""))  # e.g. --cap-add=NET_ADMIN
RAM_OVERCOMMIT = _float("RAM_OVERCOMMIT", 1.0)    # 1.0 = never allocate more than the host has
DISK_OVERCOMMIT = _float("DISK_OVERCOMMIT", 1.0)
CPU_OVERCOMMIT = _float("CPU_OVERCOMMIT", 1.0)
# Capacity is read from the host automatically. Reserve (kept free for the host + this bot) is automatic too;
# set RESERVE_RAM_MB / RESERVE_DISK_MB only if you want to force a value.
_RES_RAM_ENV = os.getenv("RESERVE_RAM_MB")
_RES_DISK_ENV = os.getenv("RESERVE_DISK_MB")
ALLOW_UNISOLATED = _bool("ALLOW_UNISOLATED", "1")
REQUIRE_ROOT = _bool("REQUIRE_ROOT", "1")         # no root on this host -> no VPS (sandbox mode cannot give root)
_hn = os.getenv("VPS_HOSTNAME", "verseedit").strip()
VPS_HOSTNAME = _hn if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", _hn) else "verseedit"
SSH_PORT_START = _int("SSH_PORT_START", 20000)
SSH_PORT_END = _int("SSH_PORT_END", 29999)
SSH_HOST = (os.getenv("SSH_HOST") or "").strip()  # public address shown for SSH (default: auto-detected IP)
# defaults of the !deploy plan; admins change them with !deployedit (stored in the database)
DEFAULT_DEPLOY = {"ram": os.getenv("DEPLOY_RAM", "2GB"), "disk": os.getenv("DEPLOY_DISK", "10GB"),
                  "cpu": os.getenv("DEPLOY_CPU", "0.5"), "per_user": os.getenv("DEPLOY_PER_USER", "1")}

ENV_ADMINS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "") or "") if x.isdigit()}
ACTIVE_PORT = PORT


class VPSError(Exception):
    """A problem that is safe to show to the user."""


class Busy(VPSError):
    pass


# ----------------------------------------------------------------- helpers
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m|mb|mib|g|gb|gib|t|tb|tib)?$", re.I)


def size_mib(text):
    m = _SIZE_RE.match(str(text).strip())
    if not m:
        raise ValueError(f"Invalid size '{text}'. Use e.g. 512MB, 2GB, 1TB.")
    unit = (m[2] or "g")[0].lower()
    return int(float(m[1]) * {"m": 1, "g": 1024, "t": 1024 * 1024}[unit])


def fmt_mib(mib):
    if mib >= 1024 * 1024 and mib % (1024 * 1024) == 0:
        return f"{mib // (1024 * 1024)}TB"
    if mib >= 1024 and mib % 1024 == 0:
        return f"{mib // 1024}GB"
    return f"{mib}MB"


def parse_cpu(text):
    try:
        v = float(str(text).strip())
    except ValueError:
        raise ValueError(f"Invalid CPU '{text}'. Use a number like 0.5, 1 or 2.")
    if not math.isfinite(v):
        raise ValueError("Invalid CPU value.")
    return round(v, 2)


def fmt_cpu(v):
    return f"{v:g}"


def fmt_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_uptime(sec):
    if sec is None:
        return "-"
    return f"{sec // 86400}d {sec % 86400 // 3600}h {sec % 3600 // 60}m"


def parse_bytes(text):
    m = re.match(r"\s*([\d.]+)\s*([kmgt]?)(i?)b?", str(text), re.I)
    if not m:
        return None
    base = 1024 if m[3] else 1000
    return int(float(m[1]) * base ** "bkmgt".index((m[2] or "b").lower()))


def norm(p):
    return "/" + posixpath.normpath("/" + (p or "/")).lstrip("/")


def _kill(p, kw):
    try:
        if kw.get("start_new_session"):
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def sh(argv, timeout=60, input=None):
    """Small control commands (lxc/docker). Returns (rc, bytes)."""
    try:
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **kw)
        return p.returncode, p.stdout[-20000:]
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"")[-20000:]
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}".encode()
    except OSError as e:
        return 126, str(e).encode()


def run_capped(argv, input=None, timeout=60, cap=MAX_OUT, **kw):
    """Run a command, keep at most `cap` bytes of output in RAM (kills the command beyond that)."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}".encode()
    except OSError as e:
        return 126, str(e).encode()
    buf, state = bytearray(), {"trunc": False}

    def reader():
        try:
            fd = p.stdout.fileno()
            while True:
                chunk = os.read(fd, 65536)  # returns as soon as data is there
                if not chunk:
                    break
                room = cap - len(buf)
                buf.extend(chunk[:max(room, 0)])
                if len(chunk) > room:
                    state["trunc"] = True
                    _kill(p, kw)
                    break
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    if input is not None:
        def writer():
            try:
                p.stdin.write(input)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    p.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=writer, daemon=True).start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p, kw)
        p.wait()
        rc = 124
    # a background job that kept our pipe open must not block us: give the reader a moment, then go on
    t.join(1.5)
    out = bytes(buf)
    if state["trunc"]:
        out += b"\n[output truncated]"
    return rc, out


# ------------------------------------------------------------- settings/db
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    discord_id INTEGER PRIMARY KEY,
    username   TEXT UNIQUE NOT NULL,
    pw_salt    TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    pw_crypt   TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vps (
    id         TEXT PRIMARY KEY,
    container  TEXT UNIQUE NOT NULL,
    discord_id INTEGER NOT NULL,
    ram_mib    INTEGER NOT NULL,
    disk_mib   INTEGER NOT NULL,
    cpu        REAL NOT NULL,
    os         TEXT NOT NULL,
    backend    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'admin',
    ip         TEXT,
    ssh_port   INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vps_user ON vps(discord_id);
CREATE TABLE IF NOT EXISTS admins (discord_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(r):
    return dict(r) if r is not None else None


def get_setting(key, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(key, value):
    with db() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE k=?", (key,))
        else:
            c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))


def deploy_enabled():
    return get_setting("deploy_enabled", "1") == "1"


def set_deploy_enabled(flag):
    set_setting("deploy_enabled", "1" if flag else "0")


def deploy_plan():
    """What !deploy gives. Editable by admins with !deployedit."""
    d = DEFAULT_DEPLOY
    try:
        cpu = parse_cpu(get_setting("deploy_cpu", d["cpu"]))
    except ValueError:
        cpu = 0.5
    try:
        per_user = max(1, int(get_setting("deploy_per_user", d["per_user"])))
    except (TypeError, ValueError):
        per_user = 1
    return {"ram": get_setting("deploy_ram", d["ram"]), "disk": get_setting("deploy_disk", d["disk"]),
            "cpu": cpu, "per_user": per_user}


def set_deploy_plan(ram, disk, cpu, per_user=None):
    ram_mib, disk_mib, cpu_v = size_mib(ram), size_mib(disk), parse_cpu(cpu)
    if ram_mib < 128:
        raise ValueError("Minimum RAM is 128MB.")
    if disk_mib < 1024:
        raise ValueError("Minimum storage is 1GB.")
    if cpu_v < 0.1:
        raise ValueError("Minimum CPU is 0.1.")
    set_setting("deploy_ram", fmt_mib(ram_mib))
    set_setting("deploy_disk", fmt_mib(disk_mib))
    set_setting("deploy_cpu", fmt_cpu(cpu_v))
    if per_user is not None:
        if int(per_user) < 1:
            raise ValueError("Per-user limit must be at least 1.")
        set_setting("deploy_per_user", int(per_user))
    return deploy_plan()


def is_admin(user_id):
    if user_id in ENV_ADMINS:
        return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE discord_id=?", (user_id,)).fetchone() is not None


def add_admin(user_id):
    if is_admin(user_id):
        return False
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins VALUES (?)", (user_id,))
    return True


def remove_admin(user_id):
    with db() as c:
        return c.execute("DELETE FROM admins WHERE discord_id=?", (user_id,)).rowcount > 0


def by_id(vid):
    with db() as c:
        return _row(c.execute("SELECT * FROM vps WHERE id=?", (vid,)).fetchone())


def for_user(discord_id):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM vps WHERE discord_id=? ORDER BY created_at", (discord_id,))]


def all_vps():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM vps ORDER BY created_at")]


def can_manage(user_id, row):
    return row["discord_id"] == user_id or is_admin(user_id)


def _set_ip(vid, ip):
    with db() as c:
        c.execute("UPDATE vps SET ip=? WHERE id=?", (ip, vid))


# ---------------------------------------------------------------- status
_status_lock = threading.Lock()


def write_status(**kw):
    with _status_lock:
        try:
            data = json.loads(STATUS_FILE.read_text())
        except Exception:  # noqa: BLE001
            data = {}
        data.update(kw)
        data["ts"] = int(time.time())
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, STATUS_FILE)


def secret_key():
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    f = DATA_DIR / "secret.key"
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return f.read_text().strip()


# ------------------------------------------------------------- accounts / auth
# One account per Discord user (!register username password). The same password is the dashboard login AND the
# root password of every VPS of that account. Only hashes are stored: a PBKDF2 hash for the dashboard and a
# SHA-512-crypt hash (what /etc/shadow uses) that is applied to the VPS with `chpasswd -e`.
_DUMMY_SALT = "00" * 16
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,23}$")


def hash_pw(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 150_000).hex()


def crypt_hash(password):
    """SHA-512 crypt hash for /etc/shadow (via openssl, or the crypt module on old Pythons)."""
    rc, out = run_capped(["openssl", "passwd", "-6", "-stdin"], input=(password + "\n").encode(), timeout=15, cap=500)
    h = out.decode(errors="replace").strip()
    if rc == 0 and h.startswith("$6$"):
        return h
    try:
        import crypt  # removed in Python 3.13
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except Exception:  # noqa: BLE001
        raise VPSError("The host needs the 'openssl' command to set root passwords (apt install openssl).")


def check_password_rules(password):
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    if len(password) > 128 or not password.isprintable() or any(c in password for c in "\r\n"):
        raise ValueError("Password may not contain control characters (max 128 chars).")


def get_account(discord_id):
    with db() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE discord_id=?", (int(discord_id),)).fetchone())


def account_by_name(name):
    with db() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE username=?", ((name or "").strip().lower(),)).fetchone())


def register(discord_id, username, password):
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-24 characters, letters/digits and . _ - only (e.g. verse_01).")
    check_password_rules(password)
    if get_account(discord_id):
        raise VPSError("You already have an account. Use !changepass <new password> to change the password.")
    if account_by_name(username):
        raise VPSError("That username is taken. Pick another one.")
    salt = secrets.token_hex(16)
    try:
        with db() as c:
            c.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                      (int(discord_id), username, salt, hash_pw(password, salt), crypt_hash(password), int(time.time())))
    except sqlite3.IntegrityError:
        raise VPSError("That username is taken. Pick another one.")
    return get_account(discord_id)


def check_login(username, password):
    row = account_by_name(username)
    h = hash_pw(password or "", row["pw_salt"] if row else _DUMMY_SALT)
    if row and hmac.compare_digest(h, row["pw_hash"]):
        return row
    return None


def session_tag(acct):
    """What the web session stores instead of the password hash; changes when the password changes."""
    return hmac.new(secret_key().encode(), acct["pw_hash"].encode(), "sha256").hexdigest()[:24]


def change_password(discord_id, new_password):
    """New account password -> dashboard login + root password of all VPS. Returns ids that could not be updated now
    (stopped VPS get the new password automatically the next time they are started)."""
    check_password_rules(new_password)
    acct = get_account(discord_id)
    if not acct:
        raise VPSError("Register first: !register <username> <password>")
    salt, crypt = secrets.token_hex(16), crypt_hash(new_password)
    with db() as c:
        c.execute("UPDATE accounts SET pw_salt=?, pw_hash=?, pw_crypt=? WHERE discord_id=?",
                  (salt, hash_pw(new_password, salt), crypt, int(discord_id)))
    failed = []
    for r in for_user(discord_id):
        bk = backend_for(r)
        try:
            if bk.has_root and bk.status(r) == "Running":
                bk.set_root_password(r, crypt)
            elif bk.has_root:
                failed.append(r["id"])
        except Exception:  # noqa: BLE001
            log.exception("password sync failed for %s", r["id"])
            failed.append(r["id"])
    return failed


# ----------------------------------------------------------------- locks
_locks, _guard = {}, threading.Lock()
_create_lock = threading.Lock()


@contextmanager
def op_guard(vid):
    """One power/reinstall/delete operation per VPS - shared by Discord and the web panel."""
    with _guard:
        lock = _locks.setdefault(vid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise Busy("Another operation is already running on this VPS. Try again in a moment.")
    try:
        yield
    finally:
        lock.release()


# -------------------------------------------------------------- backends
def provision_script():
    """Runs INSIDE a new VPS: installs sshd + tools, sets the hostname (root@verseedit), allows root password login."""
    hn = shlex.quote(VPS_HOSTNAME)
    return f"""
export DEBIAN_FRONTEND=noninteractive
HN={hn}
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq openssh-server sudo curl wget nano vim git python3 python3-pip unzip zip tar htop screen tmux lsof \\
    net-tools iproute2 iputils-ping dnsutils ca-certificates procps psmisc xz-utils >/dev/null 2>&1
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache openssh bash sudo curl wget nano vim git python3 py3-pip unzip zip tar htop screen tmux lsof \\
    net-tools iproute2 iputils bind-tools procps psmisc shadow >/dev/null 2>&1
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q openssh-server sudo curl wget nano vim git python3 python3-pip unzip zip tar htop screen tmux lsof \\
    net-tools iproute iputils bind-utils procps-ng psmisc passwd >/dev/null 2>&1
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm --needed openssh sudo curl wget nano vim git python python-pip unzip zip htop screen tmux lsof net-tools iproute2 iputils >/dev/null 2>&1
fi
if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
  echo "openssh-server could not be installed (no internet inside the VPS?)" >&2; exit 3
fi
echo "$HN" > /etc/hostname 2>/dev/null; hostname "$HN" 2>/dev/null
grep -q " $HN" /etc/hosts 2>/dev/null || echo "127.0.1.1 $HN" >> /etc/hosts 2>/dev/null
mkdir -p /etc/cloud/cloud.cfg.d && echo "preserve_hostname: true" > /etc/cloud/cloud.cfg.d/99-verse-hostname.cfg
mkdir -p /run/sshd /etc/ssh/sshd_config.d
printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-verse.conf
sed -i -E '/^(PermitRootLogin|PasswordAuthentication)[[:space:]]/d' /etc/ssh/sshd_config 2>/dev/null
printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' >> /etc/ssh/sshd_config
ssh-keygen -A >/dev/null 2>&1
if [ -d /run/systemd/system ]; then
  systemctl enable ssh >/dev/null 2>&1; systemctl restart ssh >/dev/null 2>&1 || {{ systemctl enable sshd >/dev/null 2>&1; systemctl restart sshd >/dev/null 2>&1; }}
elif command -v rc-service >/dev/null 2>&1; then
  rc-update add sshd >/dev/null 2>&1; rc-service sshd restart >/dev/null 2>&1
else
  pkill -x sshd 2>/dev/null; /usr/sbin/sshd
fi
true
"""


# command a Docker VPS runs at every start: bring sshd up, then keep the container alive
DOCKER_START = "mkdir -p /run/sshd 2>/dev/null; [ -x /usr/sbin/sshd ] && /usr/sbin/sshd; exec tail -f /dev/null"


class Backend:
    name = "?"
    confined = False
    enforces_disk = True
    enforces_cpu = True
    default_os = "ubuntu"
    IMAGES = {}
    label = ""
    has_root = False      # can the VPS owner become root inside the VPS?

    def resolve_os(self, key):
        key = (key or self.default_os).lower()
        if key not in self.IMAGES:
            raise ValueError(f"Unknown OS for {self.name} mode. Available: {', '.join(sorted(self.IMAGES))}")
        return key

    def home(self, row):
        return "/root"

    def argv(self, row, script, timeout=None):
        raise NotImplementedError

    def popen_kw(self, row):
        return {}

    def exec(self, row, script, timeout=60, stdin=None):
        return run_capped(self.argv(row, script, timeout), input=stdin, timeout=timeout + 10, **self.popen_kw(row))

    def popen(self, row, script, stdin=False, stdout=True, stderr=subprocess.DEVNULL):
        """Streaming process (used for file upload/download so nothing is held in RAM)."""
        return subprocess.Popen(self.argv(row, script, None),
                                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE if stdout else subprocess.DEVNULL,
                                stderr=stderr, **self.popen_kw(row))

    def provision(self, s, pw_crypt):
        """Install sshd, set hostname + root password. Synchronous: the VPS is only 'created' when SSH works."""
        rc, out = self.exec(s, provision_script(), timeout=900)
        if rc != 0:
            raise RuntimeError("VPS setup failed: " + out.decode(errors="replace").strip()[-300:])
        self.set_root_password(s, pw_crypt)

    def set_root_password(self, row, pw_crypt):
        last = b""
        for attempt in range(4):  # a freshly started container may need a moment
            rc, last = self.exec(row, "chpasswd -e", 30, stdin=f"root:{pw_crypt}\n".encode())
            if rc == 0:
                return
            time.sleep(3)
        raise RuntimeError("Could not set the root password: " + last.decode(errors="replace").strip()[-200:])

    def ip(self, row, wait=0):
        return None


class LXD(Backend):
    name = "lxd"
    has_root = True
    label = "LXD system container (real VPS: own OS, root, disk quota)"
    IMAGES = {"ubuntu": "ubuntu/22.04", "ubuntu22": "ubuntu/22.04", "ubuntu24": "ubuntu/24.04", "debian": "debian/12",
              "alpine": "alpine/3.20", "almalinux": "almalinux/9", "rocky": "rockylinux/9", "fedora": "fedora/41",
              "arch": "archlinux"}
    REMOTE = os.getenv("LXD_REMOTE", "images")

    def available(self):
        if not shutil.which("lxc"):
            return False, "lxc is not installed"
        rc, out = sh(["lxc", "storage", "list", "--format", "csv"], 20)
        if rc != 0:
            return False, out.decode(errors="replace").strip()[:200]
        if not out.strip():
            return False, "LXD has no storage pool (run: lxd init --auto)"
        return True, ""

    def argv(self, row, script, timeout=None):
        pre = ["timeout", str(timeout)] if timeout else []
        return ["lxc", "exec", row["container"], "--"] + pre + ["sh", "-c", script]

    def create(self, s):
        c, cpu = s["container"], float(s["cpu"])
        base = ["lxc", "init", f"{self.REMOTE}:{self.IMAGES[s['os']]}", c,
                "-c", f"limits.cpu={max(1, math.ceil(cpu))}", "-c", f"limits.memory={s['ram_mib']}MiB"]
        if abs(cpu - round(cpu)) > 1e-9:
            base += ["-c", f"limits.cpu.allowance={int(cpu * 100)}ms/100ms"]
        rc, out = sh(base + ["-d", f"root,size={s['disk_mib']}MiB"], 900)
        if rc != 0 and re.search(r"quota|size|not supported", out.decode(errors="replace"), re.I):
            sh(["lxc", "delete", "-f", c], 60)
            rc, out = sh(base, 900)  # pool without quota support (dir backend)
        if rc != 0:
            raise RuntimeError("LXD init failed: " + out.decode(errors="replace").strip()[-300:])
        try:
            rc, out = sh(["lxc", "config", "device", "add", c, "ssh", "proxy", f"listen=tcp:0.0.0.0:{s['ssh_port']}",
                          "connect=tcp:127.0.0.1:22"], 30)
            if rc != 0:
                raise RuntimeError("LXD SSH port failed: " + out.decode(errors="replace").strip()[-300:])
            rc, out = sh(["lxc", "start", c], 120)
            if rc != 0:
                raise RuntimeError("LXD start failed: " + out.decode(errors="replace").strip()[-300:])
            self._wait_network(s)
            self.provision(s, s["pw_crypt"])
        except Exception:
            sh(["lxc", "delete", "-f", c], 60)
            raise

    def _wait_network(self, s, secs=90):
        end = time.time() + secs
        while time.time() < end:
            if self.stats(s).get("ip") and sh(["lxc", "exec", s["container"], "--", "true"], 15)[0] == 0:
                return
            time.sleep(2)

    def start(self, r):
        self._ok(sh(["lxc", "start", r["container"]], 120))

    def stop(self, r):
        self._ok(sh(["lxc", "stop", r["container"], "--force"], 120))

    def restart(self, r):
        self._ok(sh(["lxc", "restart", r["container"], "--force"], 180))

    def delete(self, r):
        rc, out = sh(["lxc", "delete", "-f", r["container"]], 180)
        if rc != 0 and b"not found" not in out.lower():
            raise RuntimeError(out.decode(errors="replace").strip()[-300:])

    @staticmethod
    def _ok(res):
        if res[0] != 0:
            raise RuntimeError(res[1].decode(errors="replace").strip()[-300:] or "lxc failed")

    def _info(self, r):
        rc, out = sh(["lxc", "list", f"^{r['container']}$", "--format", "json"], 20)
        data = json.loads(out.decode() or "[]") if rc == 0 else []
        return data[0] if data else None

    def status(self, r):
        d = self._info(r)
        return (d.get("status") or "Unknown").title() if d else "Unknown"

    def stats(self, r):
        d = self._info(r)
        out = {"status": "Unknown", "ip": None, "cpu_ns": None, "mem_used": None, "disk_used": None, "uptime": None}
        if not d:
            return out
        out["status"] = (d.get("status") or "Unknown").title()
        st = d.get("state") or {}
        if out["status"] != "Running":
            return out
        out["mem_used"] = (st.get("memory") or {}).get("usage")
        out["cpu_ns"] = (st.get("cpu") or {}).get("usage")
        out["disk_used"] = ((st.get("disk") or {}).get("root") or {}).get("usage")
        for iface, data in (st.get("network") or {}).items():
            if iface == "lo":
                continue
            for a in data.get("addresses", []):
                if a.get("family") == "inet" and a.get("scope") == "global":
                    out["ip"] = a["address"]
                    break
        rc, up = self.exec(r, "cut -d' ' -f1 /proc/uptime", 10)
        try:
            out["uptime"] = int(float(up.split()[0]))
        except Exception:  # noqa: BLE001
            pass
        return out

    def ip(self, r, wait=0):
        end = time.time() + wait
        while True:
            ip = self.stats(r).get("ip")
            if ip or time.time() >= end:
                return ip
            time.sleep(2)


class Docker(Backend):
    name = "docker"
    has_root = True
    label = "Docker container (RAM/CPU limited, root inside)"
    enforces_disk = False
    _disk_cache = {}
    IMAGES = {"ubuntu": "ubuntu:22.04", "ubuntu22": "ubuntu:22.04", "ubuntu24": "ubuntu:24.04", "debian": "debian:12",
              "alpine": "alpine:3.20"}

    def available(self):
        if not shutil.which("docker"):
            return False, "docker is not installed"
        rc, out = sh(["docker", "info"], 25)
        if rc != 0:
            return False, out.decode(errors="replace").strip()[-200:]
        return True, ""

    def argv(self, row, script, timeout=None):
        pre = ["timeout", str(timeout)] if timeout else []
        return ["docker", "exec", "-i", row["container"]] + pre + ["sh", "-c", script]

    def create(self, s):
        c = s["container"]
        args = ["docker", "run", "-d", "--init", "--name", c, "--hostname", VPS_HOSTNAME, "--memory", f"{s['ram_mib']}m",
                "--memory-swap", f"{s['ram_mib']}m", "--cpus", f"{float(s['cpu']):g}", "--pids-limit", "2048",
                "-p", f"{s['ssh_port']}:22", "--restart", "unless-stopped"] + DOCKER_EXTRA_ARGS
        tail = [self.IMAGES[s["os"]], "sh", "-c", DOCKER_START]
        rc, out = sh(args + ["--storage-opt", f"size={s['disk_mib']}m"] + tail, 900)
        if rc != 0 and b"storage-opt" in out:
            sh(["docker", "rm", "-f", c], 60)
            rc, out = sh(args + tail, 900)  # storage driver without per-container size limits
        if rc != 0:
            sh(["docker", "rm", "-f", c], 60)
            raise RuntimeError("docker run failed: " + out.decode(errors="replace").strip()[-300:])
        try:
            self.provision(s, s["pw_crypt"])
        except Exception:
            sh(["docker", "rm", "-f", c], 60)
            raise

    def start(self, r):
        self._ok(sh(["docker", "start", r["container"]], 60))

    def stop(self, r):
        self._ok(sh(["docker", "stop", "-t", "10", r["container"]], 90))

    def restart(self, r):
        self._ok(sh(["docker", "restart", "-t", "10", r["container"]], 90))

    def delete(self, r):
        rc, out = sh(["docker", "rm", "-f", r["container"]], 120)
        if rc != 0 and b"No such container" not in out:
            raise RuntimeError(out.decode(errors="replace").strip()[-300:])

    @staticmethod
    def _ok(res):
        if res[0] != 0:
            raise RuntimeError(res[1].decode(errors="replace").strip()[-300:] or "docker failed")

    def status(self, r):
        rc, out = sh(["docker", "inspect", "-f", "{{.State.Status}}", r["container"]], 20)
        if rc != 0:
            return "Unknown"
        return "Running" if out.strip() == b"running" else "Stopped"

    def ip(self, r, wait=0):
        end = time.time() + wait
        while True:
            rc, out = sh(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                          r["container"]], 20)
            ip = out.decode().strip() if rc == 0 else ""
            if ip or time.time() >= end:
                return ip or None
            time.sleep(2)

    _CG = r"""
if [ -r /sys/fs/cgroup/memory.current ]; then
  m=$(cat /sys/fs/cgroup/memory.current); f=$(awk '$1=="inactive_file"{print $2}' /sys/fs/cgroup/memory.stat)
  c=$(awk '$1=="usage_usec"{printf "%.0f", $2*1000}' /sys/fs/cgroup/cpu.stat)
else
  m=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes); f=$(awk '$1=="total_inactive_file"{print $2}' /sys/fs/cgroup/memory/memory.stat)
  c=$(cat /sys/fs/cgroup/cpuacct/cpuacct.usage)
fi
echo "${m:-0} ${f:-0} ${c:-0}"
"""

    def _disk(self, c, ttl=60):
        hit = self._disk_cache.get(c)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = None
        rc, o = sh(["docker", "ps", "-s", "--filter", f"name=^/{c}$", "--format", "{{.Size}}"], 40)
        if rc == 0 and o.strip():
            txt = o.decode()
            m = re.search(r"virtual\s+([\d.]+\s*[kKmMgGtT]?i?B)", txt)  # writable layer + image = what df shows
            val = parse_bytes(m[1] if m else txt.split()[0])
        self._disk_cache[c] = (time.time(), val)
        return val

    def stats(self, r):
        c = r["container"]
        out = {"status": self.status(r), "ip": None, "cpu_pct": None, "mem_used": None, "disk_used": None,
               "uptime": None}
        if out["status"] != "Running":
            return out
        rc, o = self.exec(r, self._CG, 15)
        try:
            mem, inactive, cpu_ns = (int(x) for x in o.decode().split()[-3:])
            out["mem_used"] = max(0, mem - inactive)
            out["cpu_ns"] = cpu_ns
        except Exception:  # noqa: BLE001
            log.warning("docker stats parse failed for %s: %r", c, o[-200:])
        out["disk_used"] = self._disk(c)
        rc, o = sh(["docker", "inspect", "-f", "{{.State.StartedAt}}", c], 20)
        try:
            import calendar
            ts = calendar.timegm(time.strptime(o.decode().strip()[:19], "%Y-%m-%dT%H:%M:%S"))
            out["uptime"] = max(0, int(time.time()) - ts)
        except Exception:  # noqa: BLE001
            pass
        out["ip"] = self.ip(r)
        return out


class Proc(Backend):
    """Sandbox mode: works anywhere (CodeSandbox, Replit, Codespaces, plain Linux without LXD/Docker).

    Each VPS is a private home directory + shell. If we run as root every VPS also gets its own unix
    user (real file isolation). RAM is enforced by a watchdog, disk by a file-size limit + upload quota.
    CPU is not limited here.
    """
    name = "proc"
    label = "Sandbox shell (no root/OS choice, shared kernel)"
    confined = True
    enforces_disk = False
    enforces_cpu = False
    default_os = "host"
    IMAGES = {"host": "host"}
    drop = False

    def __init__(self):
        self.root = DATA_DIR / "vps"
        self._du_cache = {}

    def resolve_os(self, key):
        return "host"

    @staticmethod
    def uid(row):
        return 100000 + int(row["id"], 16) % 4_000_000_000

    def home(self, row):
        return str(self.root / row["container"])

    def available(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o711)
        except OSError:
            pass
        self._selftest()
        return True, ""

    def _selftest(self):
        self.drop = False
        if os.geteuid() != 0 or sys.version_info < (3, 9) or os.getenv("PROC_NO_DROP") == "1":
            return
        t = self.root / ".selftest"
        try:
            t.mkdir(exist_ok=True)
            os.chown(t, 100001, 100001)
            os.chmod(t, 0o700)
            p = subprocess.run(["sh", "-c", f"cd '{t}' && true"], user=100001, group=100001, extra_groups=[],
                               capture_output=True, timeout=10)
            self.drop = p.returncode == 0
        except Exception:  # noqa: BLE001
            self.drop = False
        finally:
            shutil.rmtree(t, ignore_errors=True)

    def _stopped(self, row):
        return (self.root / f"{row['container']}.stopped").exists()

    def _env(self, row):
        home = self.home(row)
        return {"HOME": home, "USER": row["container"], "LOGNAME": row["container"], "SHELL": "/bin/bash",
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "TERM": "xterm-256color",
                "LANG": "C.UTF-8", "VPSMGR_ID": row["id"]}

    def argv(self, row, script, timeout=None):
        if self._stopped(row):
            return ["sh", "-c", "echo 'VPS is stopped. Start it first.' >&2; exit 1"]
        pre = f"umask 022; ulimit -f {int(row['disk_mib']) * 1024} 2>/dev/null; "
        if self.drop:
            pre += "ulimit -u 512 2>/dev/null; "
        shell = "bash" if shutil.which("bash") else "sh"
        cmd = [shell, "-c", pre + script]
        if shutil.which("nice"):
            cmd = ["nice", "-n", "5"] + cmd
        if timeout and shutil.which("timeout"):
            cmd = ["timeout", "-k", "3", str(timeout)] + cmd
        return cmd

    def popen_kw(self, row):
        kw = {"cwd": self.home(row), "env": self._env(row), "start_new_session": True}
        if self.drop:
            uid = self.uid(row)
            kw.update(user=uid, group=uid, extra_groups=[])
        return kw

    def create(self, s):
        home = Path(self.home(s))
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        if self.drop:
            uid = self.uid(s)
            os.chown(home, uid, uid)
        rc, out = self.exec(s, "echo ok", 15)
        if rc != 0:
            shutil.rmtree(home, ignore_errors=True)
            raise RuntimeError("Sandbox shell failed: " + out.decode(errors="replace").strip()[-200:])
        (self.root / f"{s['container']}.started").write_text(str(int(time.time())))

    def _tagged(self, vid):
        for p in psutil.process_iter():
            try:
                if p.environ().get("VPSMGR_ID") == vid:
                    yield p
            except Exception:  # noqa: BLE001
                continue

    def scan(self):
        usage = {}
        for p in psutil.process_iter():
            try:
                vid = p.environ().get("VPSMGR_ID")
                if not vid:
                    continue
                u = usage.setdefault(vid, {"rss": 0, "procs": 0, "cpu": 0.0})
                u["rss"] += p.memory_info().rss
                u["procs"] += 1
                u["cpu"] += p.cpu_percent(None)
            except Exception:  # noqa: BLE001
                continue
        return usage

    def kill_all(self, vid):
        for p in list(self._tagged(vid)):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def start(self, r):
        try:
            (self.root / f"{r['container']}.stopped").unlink()
        except FileNotFoundError:
            pass
        (self.root / f"{r['container']}.started").write_text(str(int(time.time())))

    def stop(self, r):
        (self.root / f"{r['container']}.stopped").touch()
        self.kill_all(r["id"])

    def restart(self, r):
        self.kill_all(r["id"])
        self.start(r)

    def delete(self, r):
        self.kill_all(r["id"])
        shutil.rmtree(self.home(r), ignore_errors=True)
        try:
            (self.root / f"{r['container']}.stopped").unlink()
        except FileNotFoundError:
            pass

    def status(self, r):
        return "Stopped" if self._stopped(r) else "Running"

    def du(self, r, ttl=30):
        c = r["container"]
        hit = self._du_cache.get(c)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        total = 0
        for dp, _dn, fn in os.walk(self.home(r)):
            for f in fn:
                try:
                    total += os.lstat(os.path.join(dp, f)).st_blocks * 512
                except OSError:
                    pass
        self._du_cache[c] = (time.time(), total)
        return total

    def stats(self, r):
        out = {"status": self.status(r), "ip": None, "cpu_pct": None, "mem_used": None, "disk_used": None,
               "uptime": None}
        if out["status"] == "Running":
            u = self.scan().get(r["id"], {})
            out["mem_used"] = u.get("rss", 0)
            out["cpu_pct"] = min(100.0, u.get("cpu", 0.0) / max(float(r["cpu"]), 0.1))
            out["procs"] = u.get("procs", 0)
            try:
                out["uptime"] = max(0, int(time.time()) - int((self.root / f"{r['container']}.started").read_text()))
            except (OSError, ValueError):
                pass
        out["disk_used"] = self.du(r)
        return out


_backends = {}
ACTIVE = "proc"
NOTES = []


def get_backend(name):
    if name not in _backends:
        inst = {"lxd": LXD, "docker": Docker, "proc": Proc}[name]()
        if name == "proc":
            inst.available()  # creates the folder and runs the unix-user isolation self-test
        _backends[name] = inst
    return _backends[name]


def backend_for(row):
    return get_backend(row["backend"])


def backend_info():
    bk = get_backend(ACTIVE)
    notes = list(NOTES)
    if REQUIRE_ROOT and not bk.has_root:
        notes.append("NO ROOT: this host has no Docker/LXD, so VPS creation is blocked (every VPS must have root). "
                     "Install Docker (apt install docker.io) and restart the bot.")
    if bk.name == "proc":
        if bk.drop:
            notes.append("Sandbox mode: every VPS runs as its own unix user. CPU is not limited, RAM is enforced "
                         "by a watchdog, there is no root or OS choice. Install LXD or Docker for real VPS.")
        else:
            notes.append("Sandbox mode WITHOUT user isolation (not running as root, or the install folder is not "
                         "traversable - install under /opt): VPS users share your account and can read the bot's "
                         "files, including the Discord token. Only give access to people you fully trust, or run "
                         "on a real VPS with LXD/Docker.")
    elif bk.name == "docker":
        notes.append("Docker mode: RAM/CPU limits are enforced; disk size is limited only if the Docker storage "
                     "driver supports it.")
    return {"backend": bk.name, "label": bk.label, "notes": notes, "isolated": not (bk.name == "proc" and not bk.drop)}


def init():
    global ACTIVE
    with db() as c:
        c.executescript(SCHEMA)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    want = os.getenv("BACKEND", "auto").strip().lower()
    order = [want] if want in ("lxd", "docker", "proc") else ["lxd", "docker", "proc"]
    for name in order:
        ok, note = get_backend(name).available()
        if ok:
            ACTIVE = name
            break
        log.warning("backend %s not usable: %s", name, note)
        if name == want:
            NOTES.append(f"BACKEND={want} was requested but is not usable ({note}); using auto-detection.")
    else:
        ACTIVE = "proc"
    log.info("Active backend: %s", ACTIVE)
    return ACTIVE


# --------------------------------------------------------------- capacity
def _read_int(path):
    try:
        v = Path(path).read_text().strip()
        return None if v in ("", "max") else int(v)
    except Exception:  # noqa: BLE001
        return None


def host_resources():
    """Real RAM (MB) and CPU cores this program may use. Container/sandbox limits (cgroups) win over the
    numbers psutil reports for the whole physical machine - important on CodeSandbox, Replit, Docker..."""
    ram = psutil.virtual_memory().total
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        v = _read_int(p)
        if v and 0 < v < ram:
            ram = v
    cpu = float(psutil.cpu_count() or 1)
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpu = min(cpu, int(quota) / int(period))
    except Exception:  # noqa: BLE001
        q, per = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and q > 0 and per:
            cpu = min(cpu, q / per)
    return int(ram // MiB), round(cpu, 2)


def capacity():
    """Host capacity (auto-detected) vs. what is already given out."""
    rows = all_vps()
    host_ram, host_cpu = host_resources()
    du = shutil.disk_usage(DATA_DIR)
    res_ram = int(_RES_RAM_ENV) if _RES_RAM_ENV else max(128, min(1024, int(host_ram * 0.10)))
    res_disk = int(_RES_DISK_ENV) if _RES_DISK_ENV else max(1024, min(10240, int(du.total // MiB * 0.05)))
    used_ram = sum(r["ram_mib"] for r in rows)
    used_disk = sum(r["disk_mib"] for r in rows)
    used_cpu = sum(r["cpu"] for r in rows)
    max_ram = max(0, int(host_ram * RAM_OVERCOMMIT) - res_ram)
    max_disk = max(0, int(du.total // MiB * DISK_OVERCOMMIT) - res_disk)
    max_cpu = host_cpu * CPU_OVERCOMMIT
    return {"host_ram": host_ram, "host_cpu": host_cpu, "host_disk": du.total // MiB, "free_disk": du.free // MiB,
            "used_ram": used_ram, "used_disk": used_disk, "used_cpu": used_cpu,
            "max_ram": max_ram, "max_disk": max_disk, "max_cpu": max_cpu,
            "left_ram": max(0, max_ram - used_ram), "left_disk": max(0, max_disk - used_disk),
            "left_cpu": max(0.0, round(max_cpu - used_cpu, 2)), "count": len(rows)}


def parse_spec(ram, storage, cpu, os_key=None):
    """Validate a request against the minimums and the host capacity. Returns a normalised spec."""
    ram_mib, disk_mib, cpu_v = size_mib(ram), size_mib(storage), parse_cpu(cpu)
    if ram_mib < 128:
        raise ValueError("Minimum RAM is 128MB.")
    if disk_mib < 1024:
        raise ValueError("Minimum storage is 1GB.")
    if cpu_v < 0.1:
        raise ValueError("Minimum CPU is 0.1.")
    bk = get_backend(ACTIVE)
    os_key = bk.resolve_os(os_key)
    cap = capacity()
    if cpu_v > cap["host_cpu"]:
        raise ValueError(f"The host only has {fmt_cpu(cap['host_cpu'])} CPU core(s).")
    problems = []
    if ram_mib > cap["left_ram"]:
        problems.append(f"RAM: {fmt_mib(ram_mib)} requested, {fmt_mib(cap['left_ram'])} left")
    if disk_mib > cap["left_disk"] or disk_mib > cap["free_disk"] * DISK_OVERCOMMIT:
        problems.append(f"SSD: {fmt_mib(disk_mib)} requested, {fmt_mib(min(cap['left_disk'], int(cap['free_disk'] * DISK_OVERCOMMIT)))} left")
    if cpu_v > cap["left_cpu"] + 1e-9:
        problems.append(f"CPU: {fmt_cpu(cpu_v)} requested, {fmt_cpu(cap['left_cpu'])} left")
    if problems:
        raise ValueError("Not enough host capacity - " + "; ".join(problems) + ".")
    return {"ram_mib": ram_mib, "disk_mib": disk_mib, "cpu": cpu_v, "os": os_key}


# ------------------------------------------------------------ vps lifecycle
def ensure_root_backend():
    """Root or nothing: a VPS without root access is not created."""
    bk = get_backend(ACTIVE)
    if REQUIRE_ROOT and not bk.has_root:
        raise VPSError("This host cannot give VPS users root access (no Docker/LXD found - only the sandbox mode is "
                       "available). Install Docker (apt install docker.io) or LXD on the host and restart the bot.")
    return bk


def alloc_ssh_port():
    used = {r["ssh_port"] for r in all_vps() if r["ssh_port"]}
    span = max(1, SSH_PORT_END - SSH_PORT_START + 1)
    start = secrets.randbelow(span)
    for i in range(span):
        port = SSH_PORT_START + (start + i) % span
        if port in used:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        finally:
            sock.close()
        return port
    raise VPSError(f"No free SSH port left in {SSH_PORT_START}-{SSH_PORT_END}.")


def create_vps(discord_id, ram, storage, cpu, os_key=None, source="admin"):
    """Create a root VPS for a registered account. Returns the row. The login is the account's !register password."""
    acct = get_account(discord_id)
    if not acct:
        raise VPSError("Not registered. The user must run  !register <username> <password>  first.")
    bk = ensure_root_backend()
    if bk.name == "proc" and not bk.drop and not ALLOW_UNISOLATED:
        raise VPSError("Sandbox mode cannot isolate users on this host (ALLOW_UNISOLATED=0).")
    with _create_lock:  # limit check + capacity check + creation are one atomic step (no parallel !deploy tricks)
        if source == "deploy":
            plan = deploy_plan()
            mine = [r for r in for_user(discord_id) if r["source"] == "deploy"]
            if len(mine) >= plan["per_user"]:
                raise VPSError(f"You already deployed {len(mine)} VPS (limit {plan['per_user']}). Use !myvps / !manage.")
        spec = parse_spec(ram, storage, cpu, os_key)
        vid = secrets.token_hex(4)
        spec.update(id=vid, container=f"vps-{vid}", backend=ACTIVE, source=source, discord_id=int(discord_id),
                    ssh_port=alloc_ssh_port() if bk.has_root else None, pw_crypt=acct["pw_crypt"])
        with op_guard(vid):
            bk.create(spec)
            try:
                with db() as c:
                    c.execute("INSERT INTO vps (id,container,discord_id,ram_mib,disk_mib,cpu,os,backend,source,ip,ssh_port,"
                              "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (vid, spec["container"], spec["discord_id"], spec["ram_mib"], spec["disk_mib"], spec["cpu"],
                               spec["os"], ACTIVE, source, None, spec["ssh_port"], int(time.time())))
            except Exception:
                bk.delete(spec)
                raise
    row = by_id(vid)
    try:
        _set_ip(vid, bk.ip(row, wait=30))
    except Exception:  # noqa: BLE001
        log.exception("ip lookup failed")
    return by_id(vid)


def deploy_check(discord_id):
    """Everything that can stop a !deploy, checked BEFORE the user gets any DM."""
    if not deploy_enabled():
        raise VPSError("!deploy is currently turned off by an admin.")
    if not get_account(discord_id):
        raise VPSError("Register first (in a DM with the bot is safest):  !register <username> <password>")
    ensure_root_backend()
    plan = deploy_plan()
    mine = [r for r in for_user(discord_id) if r["source"] == "deploy"]
    if len(mine) >= plan["per_user"]:
        raise VPSError(f"You already deployed {len(mine)} VPS (limit {plan['per_user']}). Use !myvps / !manage.")
    parse_spec(plan["ram"], plan["disk"], plan["cpu"])  # ValueError if the host has no room left


def deploy_vps(discord_id):
    """!deploy - the free plan (set with !deployedit)."""
    deploy_check(discord_id)
    p = deploy_plan()
    return create_vps(discord_id, p["ram"], p["disk"], p["cpu"], None, source="deploy")


def delete_vps(row):
    with op_guard(row["id"]):
        backend_for(row).delete(row)
        with db() as c:
            c.execute("DELETE FROM vps WHERE id=?", (row["id"],))
    _stats_cache.pop(row["id"], None)
    _cpu_prev.pop(row["id"], None)


def _sync_root_password(row):
    bk = backend_for(row)
    acct = get_account(row["discord_id"])
    if bk.has_root and acct:
        try:
            bk.set_root_password(row, acct["pw_crypt"])  # the account password may have changed while it was stopped
        except Exception:  # noqa: BLE001
            log.exception("root password sync failed for %s", row["id"])


def power(row, action):
    if action not in ("start", "stop", "restart"):
        raise VPSError("Unknown action.")
    bk = backend_for(row)
    with op_guard(row["id"]):
        getattr(bk, action)(row)
        if action != "stop":
            _set_ip(row["id"], bk.ip(row, wait=40))
            _sync_root_password(row)
    _stats_cache.pop(row["id"], None)


def reinstall(row):
    """Wipe and recreate with the same limits and SSH port (data is lost). Login stays the same."""
    bk = backend_for(row)
    acct = get_account(row["discord_id"])
    if not acct:
        raise VPSError("The owner has no account any more.")
    spec = dict(row, pw_crypt=acct["pw_crypt"])
    with op_guard(row["id"]):
        bk.delete(row)
        bk.create(spec)
        _set_ip(row["id"], bk.ip(row, wait=40))
    _stats_cache.pop(row["id"], None)


# ------------------------------------------------------------- ssh / sshx
def ssh_host():
    return SSH_HOST or public_ip()


def ssh_info(row):
    if not row.get("ssh_port"):
        return None
    host = ssh_host()
    return {"host": host, "port": row["ssh_port"], "user": "root", "hostname": VPS_HOSTNAME,
            "cmd": f"ssh root@{host} -p {row['ssh_port']}"}


_SSHX = r"""
LOG=/tmp/.sshx.log
link() { grep -o 'https://sshx\.io/s/[A-Za-z0-9_#.-]*' "$LOG" 2>/dev/null | head -1; }
if ! { pgrep -x sshx >/dev/null 2>&1 && [ -n "$(link)" ]; }; then
  pkill -x sshx 2>/dev/null; rm -f "$LOG"
  if ! command -v curl >/dev/null 2>&1; then
    { apt-get update -qq && apt-get install -y -qq curl ca-certificates; } >/dev/null 2>&1 || apk add --no-cache curl >/dev/null 2>&1
  fi
  nohup sh -c 'curl -sSf https://sshx.io/get | sh -s run' >"$LOG" 2>&1 </dev/null &
fi
for i in $(seq 1 45); do
  L="$(link)"; [ -n "$L" ] && { echo "$L"; exit 0; }
  sleep 1
done
tail -n 5 "$LOG" >&2; exit 1
"""


def sshx_link(row):
    """Start sshx.io inside the VPS (if not running) and return its web-terminal link. Takes up to ~45 s."""
    bk = backend_for(row)
    if bk.status(row) != "Running":
        raise VPSError("The VPS is stopped. Start it first.")
    rc, out = bk.exec(row, _SSHX, 70)
    text = out.decode("utf-8", "replace").strip()
    m = re.search(r"https://sshx\.io/s/[A-Za-z0-9_#.-]+", text)
    if rc == 0 and m:
        return m[0]
    raise VPSError("Could not start sshx (does the VPS have internet?): " + text[-200:])


# ------------------------------------------------------------------ stats
_stats_cache = {}


def stats(row):
    hit = _stats_cache.get(row["id"])
    if hit and time.time() - hit[0] < 2.5:
        return dict(hit[1])
    bk = backend_for(row)
    s = bk.stats(row)
    if s.get("cpu_pct") is None and s.get("cpu_ns") is not None:
        now, prev = time.monotonic_ns(), _cpu_prev.get(row["id"])
        pct = 0.0
        if prev and s["cpu_ns"] >= prev[1] and now > prev[0]:
            pct = min(100.0, (s["cpu_ns"] - prev[1]) / ((now - prev[0]) * max(float(row["cpu"]), 0.1)) * 100)
        _cpu_prev[row["id"]] = (now, s["cpu_ns"])
        s["cpu_pct"] = pct
    s.pop("cpu_ns", None)
    s["cpu_pct"] = round(s.get("cpu_pct") or 0.0, 1)
    s.update(mem_limit=row["ram_mib"] * MiB, disk_limit=row["disk_mib"] * MiB, cpu_cores=row["cpu"],
             os=row["os"], backend=row["backend"])
    if not s.get("ip"):
        s["ip"] = row.get("ip")
    _stats_cache[row["id"]] = (time.time(), s)
    return dict(s)


_cpu_prev = {}


def public_view(row):
    """Everything the UI may show about a VPS."""
    bk = backend_for(row)
    return {"id": row["id"], "ram": fmt_mib(row["ram_mib"]), "disk": fmt_mib(row["disk_mib"]), "cpu": fmt_cpu(row["cpu"]),
            "os": row["os"], "backend": row["backend"], "ip": row.get("ip"), "root": bk.has_root,
            "hostname": VPS_HOSTNAME if bk.has_root else row["container"], "ssh_port": row.get("ssh_port")}


# ---------------------------------------------------------- terminal / files
MARK = "@@VERSE@@"


def _bk(row):
    return backend_for(row)


def run_cmd(row, cmd, cwd=None):
    bk = _bk(row)
    home = bk.home(row)
    cwd = norm(cwd or home)
    script = (f"cd {shlex.quote(cwd)} 2>/dev/null || cd {shlex.quote(home)}\n{cmd}\n__rc=$?\n"
              f"printf '\\n{MARK}%s\\n{MARK}%s' \"$(pwd)\" \"$__rc\"\n")
    rc, raw = bk.exec(row, script, CMD_TIMEOUT)
    out = raw.decode("utf-8", "replace")
    m = re.search(re.escape("\n" + MARK) + r"(.*)\n" + re.escape(MARK) + r"(\d+)\s*$", out, re.S)
    if m:
        out, cwd, rc = out[:m.start()], norm(m.group(1)), int(m.group(2))
    if rc == 124:
        out += f"\n[timed out after {CMD_TIMEOUT}s - run long jobs over SSH/SSHX or in the background with nohup]"
    return out, cwd, rc


def _confine(row, path, allow_home=True):
    bk = _bk(row)
    p = norm(path or bk.home(row))
    if bk.confined:
        home = bk.home(row)
        if p != home and not p.startswith(home + "/"):
            raise VPSError(f"In sandbox mode files are limited to {home}")
        if p == home and not allow_home:
            raise VPSError("Refusing to touch the VPS home folder itself.")
    return p


def _parse_ls(out, iso):
    entries = []
    for line in out.splitlines():
        if not line or line.startswith("total") or line[0] not in "-dlbcps":
            continue
        parts = line.split(None, 7 if iso else 8)
        if len(parts) < (8 if iso else 9):
            continue
        perms, size, name = parts[0], parts[4], parts[-1]
        kind = "dir" if perms[0] == "d" else "link" if perms[0] == "l" else "file"
        if kind == "link" and " -> " in name:
            name = name.split(" -> ")[0]
        if kind == "dir":
            name = name.rstrip("/")
        entries.append({"name": name, "type": kind, "size": int(size) if size.isdigit() else 0, "perms": perms,
                        "mtime": " ".join(parts[5:7]) if iso else " ".join(parts[5:8])})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def fs_ls(row, path=None):
    bk, p = _bk(row), _confine(row, path)
    q = shlex.quote(p)
    rc, out = bk.exec(row, f"ls -lAp --time-style=long-iso -- {q}", 30)
    iso = True
    if rc != 0 and re.search(rb"unrecognized|invalid option|unknown option", out):
        rc, out = bk.exec(row, f"ls -lAp -- {q}", 30)
        iso = False
    if rc != 0:
        raise VPSError(out.decode("utf-8", "replace").strip()[:200] or "Cannot open directory")
    return p, _parse_ls(out.decode("utf-8", "replace"), iso)


def fs_size(row, path):
    """Size of a regular file - asked BEFORE any content is read."""
    p = _confine(row, path)
    q = shlex.quote(p)
    rc, out = _bk(row).exec(row, f"[ -f {q} ] || exit 2; wc -c < {q}", 20)
    if rc == 2:
        raise VPSError("Not a regular file (or it does not exist).")
    if rc != 0:
        raise VPSError(out.decode("utf-8", "replace").strip()[:200] or "Cannot read file")
    try:
        return p, int(out.split()[0])
    except Exception:  # noqa: BLE001
        raise VPSError("Cannot determine the file size.")


def fs_read_text(row, path):
    p, size = fs_size(row, path)
    if size > MAX_EDIT:
        raise VPSError("File is larger than 2MB - download it instead.")
    proc = _bk(row).popen(row, f"cat -- {shlex.quote(p)}")
    try:
        data = proc.stdout.read(MAX_EDIT + 1)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    if b"\0" in data[:8000]:
        raise VPSError("Binary file - download it instead.")
    return p, data.decode("utf-8", "replace")


def fs_open_download(row, path):
    """Returns (name, size, generator). The file is streamed in 64KB chunks - never loaded into RAM."""
    p, size = fs_size(row, path)
    proc = _bk(row).popen(row, f"cat -- {shlex.quote(p)}")

    def gen():
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    return posixpath.basename(p) or "file", size, gen()


def quota_left(row):
    """Bytes the VPS may still write (only where the backend cannot enforce the disk size itself)."""
    bk = _bk(row)
    if bk.enforces_disk or not hasattr(bk, "du"):
        return None
    return max(0, row["disk_mib"] * MiB - bk.du(row, ttl=5))


def fs_write_stream(row, path, stream, limit=None):
    """Copy `stream` into the VPS file in 64KB chunks (works for 50GB uploads with a few MB of RAM)."""
    import tempfile
    p = _confine(row, path, allow_home=False)
    err = tempfile.TemporaryFile()
    proc = _bk(row).popen(row, f"mkdir -p -- {shlex.quote(posixpath.dirname(p) or '/')} && cat > {shlex.quote(p)}",
                          stdin=True, stdout=False, stderr=err)
    total, too_big = 0, False
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                too_big = True
                break
            proc.stdin.write(chunk)
    except BrokenPipeError:
        pass
    except Exception:
        proc.kill()
        proc.wait()
        raise
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    if too_big:
        proc.kill()
        proc.wait()
        _bk(row).exec(row, f"rm -f -- {shlex.quote(p)}", 20)
        raise VPSError("Not enough free disk space in this VPS for that upload.")
    try:
        rc = proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise VPSError("Upload timed out.")
    if rc != 0:
        err.seek(0)
        raise VPSError(err.read(300).decode("utf-8", "replace").strip() or "Write failed (disk full?)")
    return total


def fs_write_text(row, path, content):
    import io
    data = content.encode()
    if len(data) > MAX_EDIT:
        raise VPSError("The editor handles files up to 2MB - upload larger files instead.")
    fs_write_stream(row, path, io.BytesIO(data), quota_left(row))


_PROTECTED = {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr", "/var"}


def fs_delete(row, path):
    p = _confine(row, path, allow_home=False)
    if p in _PROTECTED:
        raise VPSError("Refusing to delete a system directory.")
    rc, out = _bk(row).exec(row, f"rm -rf -- {shlex.quote(p)}", 120)
    if rc != 0:
        raise VPSError(out.decode("utf-8", "replace").strip()[:200] or "Delete failed")


def fs_mkdir(row, path):
    p = _confine(row, path, allow_home=True)
    rc, out = _bk(row).exec(row, f"mkdir -p -- {shlex.quote(p)}", 20)
    if rc != 0:
        raise VPSError(out.decode("utf-8", "replace").strip()[:200] or "mkdir failed")


# ---------------------------------------------------------- panel address
_public_ip = None


def public_ip():
    global _public_ip
    if _public_ip:
        return _public_ip
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "curl/8"}), timeout=4) as r:
                ip = r.read().decode().strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                _public_ip = ip
                return ip
        except Exception:  # noqa: BLE001
            continue
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def detect_platform():
    e, port = os.environ, ACTIVE_PORT
    if e.get("REPLIT_DEV_DOMAIN"):
        return "replit", f"https://{e['REPLIT_DEV_DOMAIN']}"
    if e.get("CODESPACE_NAME"):
        return "codespaces", f"https://{e['CODESPACE_NAME']}-{port}.{e.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'app.github.dev')}"
    if e.get("GITPOD_WORKSPACE_URL"):
        return "gitpod", e["GITPOD_WORKSPACE_URL"].replace("https://", f"https://{port}-", 1)
    if e.get("CSB") or e.get("CSB_BASE_PREVIEW_HOST"):
        return "codesandbox", f"https://{e.get('CSB_SANDBOX_ID') or socket.gethostname()}-{port}.{e.get('CSB_BASE_PREVIEW_HOST', 'csb.app')}"
    if e.get("RENDER_EXTERNAL_URL"):
        return "render", e["RENDER_EXTERNAL_URL"]
    return None, None


def trust_proxy():
    v = os.getenv("TRUST_PROXY", "auto").strip().lower()
    if v == "auto":
        return detect_platform()[0] is not None
    return v in ("1", "true", "yes", "on")


def panel_url():
    url = get_setting("panel_url") or os.getenv("PANEL_URL") or detect_platform()[1]
    if not url:
        url = f"http://{public_ip()}:{ACTIVE_PORT}"
    return url.rstrip("/")


# --------------------------------------------------------------- watchdog
def start_watchdog():
    """Sandbox mode: kill a VPS' processes when they use more RAM than the VPS was given."""
    def loop():
        while True:
            time.sleep(10)
            try:
                rows = [r for r in all_vps() if r["backend"] == "proc"]
                if not rows:
                    continue
                usage = get_backend("proc").scan()
                for r in rows:
                    u = usage.get(r["id"])
                    if u and u["rss"] > r["ram_mib"] * MiB:
                        log.warning("VPS %s exceeded its RAM (%s > %sMB) - killing its processes", r["id"],
                                    fmt_bytes(u["rss"]), r["ram_mib"])
                        get_backend("proc").kill_all(r["id"])
            except Exception:  # noqa: BLE001
                log.exception("watchdog error")
    threading.Thread(target=loop, daemon=True, name="watchdog").start()
