"""Small helpers shared by every pipeline step: loading apps, picking which
apps to run, hashing, and JSON files on disk.

Kept in one place so each step file only contains its own logic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from agent.config import APPS_CSV


def sha1(text: str) -> str:
    """Stable short key for cache file names (URLs and prompts can be long)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any | None:
    """Read a JSON file, or None if it doesn't exist yet."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write a temp file then rename, so a crash
    mid-write never leaves a half-written cache file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def load_apps() -> list[dict]:
    """All 100 apps from data/apps.csv as dicts: id, app, category, hint."""
    df = pd.read_csv(APPS_CSV).fillna("")
    return df.to_dict(orient="records")


def parse_ids(spec: str) -> set[int]:
    """'1-10,15,20-22' -> {1..10, 15, 20, 21, 22}."""
    ids: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.update(range(int(lo), int(hi) + 1))
        elif part:
            ids.add(int(part))
    return ids


def add_app_selection_args(parser: argparse.ArgumentParser) -> None:
    """The same --all / --ids / --app flags on every step's CLI."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="all 100 apps")
    group.add_argument("--ids", help="id list/ranges, e.g. 1-10,15")
    group.add_argument("--app", help="one app by name (case-insensitive)")
    parser.add_argument("--force", action="store_true", help="ignore cached results for these apps")


def select_apps(args: argparse.Namespace) -> list[dict]:
    """Apply --all / --ids / --app to the app list."""
    apps = load_apps()
    if args.all:
        return apps
    if args.ids:
        wanted = parse_ids(args.ids)
        return [a for a in apps if a["id"] in wanted]
    name = args.app.strip().lower()
    chosen = [a for a in apps if a["app"].lower() == name]
    if not chosen:
        raise SystemExit(f"No app named {args.app!r} in apps.csv")
    return chosen


def unique(items: Iterable[str]) -> list[str]:
    """De-duplicate while keeping the original order."""
    return list(dict.fromkeys(items))
