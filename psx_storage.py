#!/usr/bin/env python3
"""
Persistent storage switch.

Many modules write to "cache/..." (SQLite DBs, JSON caches) and a few runtime files live in the
repo root (licences, trials, feedback). On hosts with an ephemeral filesystem (Render/Railway)
all of that is lost on every restart. Set PSX_DATA_DIR to a persistent disk (e.g. /var/data) and,
before anything else touches the filesystem, server.py calls init_persistent_storage(), which:

  1. copies the bundled cache/ files into PSX_DATA_DIR — never overwriting files already there,
     so persisted data always wins over what ships in the repo;
  2. replaces the repo's cache/ directory with a symlink to PSX_DATA_DIR;
  3. points the root-level runtime files at PSX_DATA_DIR the same way.

Every existing "cache/..." path then reads and writes the persistent disk. Without PSX_DATA_DIR
nothing changes.
"""

import os
import shutil
from pathlib import Path
from typing import Dict, Optional

ROOT_RUNTIME_FILES = ("trial_data.json", "licenses.json", "feedback.json")


def init_persistent_storage(base_dir: Optional[Path] = None, data_dir: Optional[str] = None) -> Dict[str, object]:
    base = Path(base_dir) if base_dir else Path(__file__).parent
    target = data_dir if data_dir is not None else os.environ.get("PSX_DATA_DIR", "")
    if not target:
        return {"enabled": False}
    data = Path(target).resolve()
    data.mkdir(parents=True, exist_ok=True)
    cache = base / "cache"
    copied = 0

    if cache.is_symlink():
        if cache.resolve() != data:
            cache.unlink()
            cache.symlink_to(data, target_is_directory=True)
    else:
        if cache.is_dir():
            if cache.resolve() == data:
                return {"enabled": True, "data_dir": str(data), "seeded_files": 0, "note": "cache/ is the data dir"}
            for src in cache.rglob("*"):
                if src.is_file() and not src.name.endswith(("-wal", "-shm")):
                    dst = data / src.relative_to(cache)
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        copied += 1
            shutil.rmtree(cache)
        cache.symlink_to(data, target_is_directory=True)

    for name in ROOT_RUNTIME_FILES:
        f, dst = base / name, data / name
        if f.is_symlink():
            if Path(os.readlink(f)) != dst:
                f.unlink()
                f.symlink_to(dst)
            continue
        if f.exists():
            if not dst.exists():
                shutil.copy2(f, dst)
                copied += 1
            f.unlink()
        f.symlink_to(dst)  # may dangle until first write; open(..., "w") creates the target

    return {"enabled": True, "data_dir": str(data), "seeded_files": copied}
