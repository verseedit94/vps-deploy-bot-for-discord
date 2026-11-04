# 🖥️ VerseVPS

Discord VPS bot **+ web dashboard in one program** — the bot + dashboard start anywhere (VPS, CodeSandbox, Replit, Codespaces ...), but **creating root VPS needs Docker or LXD on the host** (the installer adds Docker on a normal root VPS).

**Made by Verseedit**

## One-click install

```bash
curl -fsSL https://raw.githubusercontent.com/verseedit94/vps-deploy-bot-for-discord/main/install.sh | bash
```

The installer sets everything up, then asks for **1) your bot token** and **2) your Discord admin ID**, starts the bot and waits until it is **online**. It prints the dashboard address (on the server's own IP) and the bot invite link.

Before you start: Discord Developer Portal → your app → **Bot** → turn ON **Message Content Intent**.

## Discord commands

| Command | Who | What |
|---|---|---|
| `!register <username> <password>` | everyone | Your account. **Do it in a DM with the bot** (in a server the bot deletes the message, but others may have seen it). The same username + password is your **dashboard login, the SSH root password, and the password of every VPS you own**. |
| `!changepass <new>` | owner | New password everywhere (stopped VPS get it when started). |
| `!deploy` | everyone | Free **root** VPS (plan set by admins, default 2GB RAM · 10GB SSD · 0.5 CPU). After creation the bot DMs: SSH IP, port, `root`, dashboard URL + username, and an SSHX terminal link. |
| `!create @user <ram> <ssd> <cpu> [os]` | admin | VPS for a **registered** user (`!create @bob 4GB 40GB 2`). Details go to **that user's** DM only. |
| `!deployedit <ram> <ssd> <cpu> [per_user]` | admin | Edit what `!deploy` gives, e.g. `!deployedit 4GB 20GB 1 2`. |
| `!vpsdeploy on` / `off` / `status` | admin | Allow or block `!deploy`. |
| `!myvps` `!manage [id]` | owner | List / **Start · Stop · Restart · Refresh · SSH · SSHX Terminal · Dashboard** buttons. |
| `!reinstall <id>` | owner/admin | Wipe a VPS (login + SSH port stay). |
| `!listall` `!delete <id>` `!status` `!panelurl` `!useraccess` `!revokeaccess` | admin | Management. |

**Root or nothing.** Every VPS is a Docker or LXD container where you are `root@verseedit` (change the name with `VPS_HOSTNAME`). If the host has neither Docker nor LXD (CodeSandbox, Replit, ...), the bot **refuses to create VPS** instead of giving users a rootless shell (`REQUIRE_ROOT=0` turns that off). On a normal root VPS the installer adds Docker for you.

**SSH.** Each VPS gets its own public port (20000-29999) -> `ssh root@<server-ip> -p <port>`. Open that range in your firewall / cloud security group. The bot never stores your plain password (only hashes), so DMs say "the password from `!register`".

**One account, many VPS.** Log into the dashboard once and switch between all your VPS in the drop-down.

**Host capacity is automatic.** The bot reads the host's RAM, SSD and CPU (including container/cgroup limits) and keeps a safety reserve for the host. `!create` and `!deploy` stop when the host is full. Use `!status` to see used/free.

## Dashboard

Runs on the server's own IP: `http://<server-ip>:5000`. Login = your `!register` username + password. Overview with live CPU/RAM/SSD/uptime, start/stop/restart/reinstall, a `root@verseedit` terminal, a file manager (edit, upload, download, delete), and an **Access** tab with the SSH details and an SSHX web-terminal button.

Files are **streamed** in 64 KB chunks — a 600 MB transfer keeps the panel at ~23 MB RAM. The text editor refuses files > 2 MB *before* reading them.
Discord and the dashboard share the same locks (one process), so they cannot reinstall/power the same VPS at the same time.

## Cloudflare Tunnel (https dashboard)

```bash
cd /opt/versevps            # or the folder you installed into
./vpsctl.sh tunnel          # interactive: quick (free, no account) or permanent (your domain)
./vpsctl.sh tunnel quick
./vpsctl.sh tunnel token <TOKEN> https://panel.example.com
./vpsctl.sh tunnel stop
```

## What a "VPS" is on your host

The bot picks the best mode automatically:

| Mode | When | You get |
|---|---|---|
| **LXD** | `lxc` installed | Real VPS: own OS, root, RAM/CPU/**disk** limits |
| **Docker** | Docker installed (the installer adds it on a normal root VPS) | Container with root; RAM/CPU limits (disk limit if the storage driver supports it) |
| **Sandbox** | nothing else works - **blocked by default** (`REQUIRE_ROOT=1`) because it cannot give root | A private home folder + shell. No OS choice/root. RAM enforced by a watchdog; **CPU not limited** |

Sandbox mode runs each VPS as its own unix user **only when the bot runs as root**. Otherwise VPS users share your account and **can read the bot's files, including the token** — `!status` and the installer warn you. Only hand out VPS to people you trust there (or set `ALLOW_UNISOLATED=0`).

## Server commands

```bash
./vpsctl.sh status | logs | restart | stop | update
```

Config lives in `.env` (see `.env.example`). Data is in `data/` — never commit either (`.gitignore` already excludes them).

## Honest notes

* The dashboard is plain **http** on the server IP by default — use the Cloudflare Tunnel for https.
* CodeSandbox/Replit/Codespaces/Gitpod address detection is best-effort; if the dashboard link in the DM is wrong, fix it with `!panelurl https://…`.
* Hosts that sleep (free sandboxes) stop the bot and the VPS processes with them.

— **Made by Verseedit**
