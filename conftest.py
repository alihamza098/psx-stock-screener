"""pytest configuration.

- Several engines write to tracked files under cache/ and config/ (SQLite DBs, suggested
  weights). Tests snapshot those files before the session and restore them afterwards so a
  test run never leaves changes in git.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent


def _tracked_runtime_files():
    try:
        out = subprocess.run(["git", "ls-files", "cache", "config"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return [ROOT / p for p in out.splitlines() if p]
    except Exception:
        return [p for d in ("cache", "config") for p in (ROOT / d).rglob("*") if p.is_file()]


_SNAPSHOT = {}


def pytest_sessionstart(session):
    for p in _tracked_runtime_files():
        try:
            _SNAPSHOT[p] = p.read_bytes()
        except OSError:
            pass


def pytest_sessionfinish(session, exitstatus):
    for p, data in _SNAPSHOT.items():
        try:
            if not p.exists() or p.read_bytes() != data:
                p.write_bytes(data)
            for suffix in ("-wal", "-shm"):
                side = p.with_name(p.name + suffix)
                if side.exists():
                    side.unlink()
        except OSError:
            pass
