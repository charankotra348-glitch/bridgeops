"""Manage BridgeOps accounts from the command line.

    python scripts/users.py create you@example.com      # asks for the password
    python scripts/users.py list
"""

import getpass
import sys

from app import auth, migrate, models
from app.config import get_settings
from app.db import SessionLocal


def main(argv: list[str]) -> int:
    migrate.upgrade(get_settings().database_url)
    with SessionLocal() as db:
        if argv[:1] == ["create"] and len(argv) == 2:
            password = getpass.getpass("Password (10+ characters): ")
            if getpass.getpass("Same password again: ") != password:
                print("The passwords don't match.")
                return 1
            try:
                auth.create_user(db, argv[1], password)
            except ValueError as exc:
                print(f"Not created: {exc}.")
                return 1
            print(f"Created {auth.normalize_email(argv[1])}. Sign in at the BridgeOps UI.")
            return 0
        if argv == ["list"]:
            for u in db.query(models.User).order_by(models.User.created_at):
                print(f"{u.email}  (last sign-in: {u.last_login_at or 'never'})")
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
