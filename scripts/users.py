"""Create and list BridgeOps accounts. Run from the project folder, like the API:

    python scripts/users.py create you@example.com     # asks for the password
    python scripts/users.py list

It uses the same .env and database as the API started from this folder.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)                       # same .env and relative DATABASE_URL as the API
sys.path.insert(0, str(ROOT / "api"))

from app.users import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
