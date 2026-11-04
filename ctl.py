"""Tiny helper used by vpsctl.sh / install.sh.  Made by Verseedit."""
import json
import sys

import core as C

with C.db() as c:
    c.executescript(C.SCHEMA)
cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
if cmd == "set-url" and len(sys.argv) > 2:
    C.set_setting("panel_url", sys.argv[2].rstrip("/"))
    C.write_status(panel=sys.argv[2].rstrip("/"))
    print(sys.argv[2].rstrip("/"))
elif cmd == "clear-url":
    C.set_setting("panel_url", None)
    C.write_status(panel=C.panel_url())
elif cmd == "show":
    print(C.get_setting("panel_url") or "")
elif cmd == "status-line":  # state|bot|panel|port|ts|backend|isolated
    try:
        d = json.loads(C.STATUS_FILE.read_text())
    except Exception:  # noqa: BLE001
        d = {}
    print("|".join(str(d.get(k, "")) for k in ("state", "bot", "panel", "port", "ts", "backend", "isolated")))
else:
    sys.exit("usage: ctl.py set-url URL | clear-url | show | status-line")
