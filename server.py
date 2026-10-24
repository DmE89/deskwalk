#!/usr/bin/env python3
"""deskwalk: minimal web controller for the Kingsmith WalkingPad A1 Pro.

    python3 server.py          talk to the belt via Bluetooth (needs: pip install bleak)
    python3 server.py --sim    simulated belt, no hardware needed

Then open http://127.0.0.1:5000. The wire protocol is documented in PROTOCOL.md.
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from deskwalk import __version__
from deskwalk.logs import setup_logging
from deskwalk.web import App, lan_ip, make_server

HERE = Path(__file__).resolve().parent
log = logging.getLogger("deskwalk")


def main():
    parser = argparse.ArgumentParser(description="Web controller for the Kingsmith WalkingPad A1 Pro")
    parser.add_argument("--sim", action="store_true", help="simulated belt, no Bluetooth")
    parser.add_argument("--host", default="0.0.0.0", help="127.0.0.1 = this computer only (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--data", default=str(HERE / "data.json"), help="data file (default: data.json)")
    parser.add_argument("--address", help="Bluetooth address of the belt instead of scanning")
    parser.add_argument("--version", action="version", version=f"deskwalk {__version__}")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        sys.exit("deskwalk needs Python 3.10 or newer")

    memory_log = setup_logging(Path(args.data).resolve().parent / "deskwalk.log")
    app = App(args.data, sim=args.sim, address=args.address)
    app.memory_log = memory_log
    app.worker.start()
    server = make_server(app, args.host, args.port)

    stopping = threading.Event()

    def shutdown(*_):
        if stopping.is_set():
            return
        stopping.set()
        app.worker.shutdown()
        with app.worker.lock:
            app.tracker.flush()
        log.info("Stopped, data saved")
        # serve_forever runs in this thread, so stop it from another one
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    print(f"\n  deskwalk {__version__}:  http://127.0.0.1:{args.port}", flush=True)
    ip = lan_ip() if args.host == "0.0.0.0" else None
    if ip:
        print(f"  Phone (Wi-Fi):   http://{ip}:{args.port}", flush=True)
    print("  Stop with Ctrl+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
