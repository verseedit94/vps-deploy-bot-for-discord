"""VerseVPS entry point: web dashboard + Discord bot in ONE process.  Made by Verseedit."""
import logging
import os
import sys
import threading

os.umask(0o077)  # data files are private; VPS shells set their own umask
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("versevps")

import core as C  # noqa: E402


def main():
    C.write_status(state="starting", pid=os.getpid(), panel="", bot="")
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        C.write_status(state="no_token")
        log.error("DISCORD_TOKEN is missing - run install.sh again (RECONFIGURE=1).")
        sys.exit(2)
    backend = C.init()
    info = C.backend_info()
    C.write_status(backend=backend, isolated=info["isolated"], notes=info["notes"])
    for n in info["notes"]:
        log.warning(n)
    import panel  # noqa: E402  (after init so the backend is known)
    import bot  # noqa: E402
    threading.Thread(target=panel.serve, daemon=True, name="panel").start()
    C.start_watchdog()
    log.info("%s - VerseVPS %s", C.CREDIT, C.VERSION)
    bot.run(token)


if __name__ == "__main__":
    main()
