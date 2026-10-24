"""python -m webapp [--host 127.0.0.1] [--port 8000] [--no-worker]

Starts the web system. Fill the database first: python -m webapp.importer --demo --demo-users
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this computer only (default)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-worker", action="store_true", help="do not run uploaded analyses in the background")
    args = ap.parse_args(argv)
    import uvicorn
    from . import db
    from .app import create_app
    from .settings import load_settings
    settings = load_settings()
    conn = db.connect(settings.db_path)
    n_users = db.scalar(conn, "SELECT COUNT(*) FROM users")
    n_demo = db.scalar(conn, "SELECT COUNT(*) FROM users WHERE demo = 1 AND active = 1")
    conn.close()
    if not n_users:
        print("No accounts yet: run  python -m webapp.importer --demo --demo-users  first.")
    if n_demo and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: {n_demo} demo accounts with published passwords are active and the server listens on "
              f"{args.host}. Deactivate them (Admin > Users) before letting other computers connect.")
    print(f"SECS Vision on http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}  "
          f"(data: {settings.data_dir})")
    uvicorn.run(create_app(settings, start_worker=not args.no_worker), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
