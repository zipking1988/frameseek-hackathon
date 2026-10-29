"""Offline, trusted operator retention; no HTTP route or spending approval changes."""

import argparse
import fcntl
import os
from datetime import datetime
from pathlib import Path

from frameseek.workspace import Workspace


def main():
    parser = argparse.ArgumentParser(description="Extend explicitly authorized existing retention")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--until", required=True, help="Timezone-aware ISO timestamp")
    parser.add_argument("--confirm-authorized-reference", required=True)
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.until)
    if cutoff.tzinfo is None:
        parser.error("--until must include a timezone")
    if not (args.data_dir / "workspace.sqlite3").is_file():
        parser.error("existing workspace database required")
    lock = os.open(args.data_dir / "serve.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("stop the workspace service before trusted retention administration")
        ws = Workspace(args.data_dir)
        ws.extend_retention(
            args.owner, args.video, cutoff.timestamp(), args.confirm_authorized_reference
        )
    finally:
        os.close(lock)
    print("Authorized retention extended. No model, budget, ownership or provider grant changed.")


if __name__ == "__main__":
    main()
