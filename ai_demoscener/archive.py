import csv
import hashlib
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_FILE = "archive_index.json"


def _archive_dir(cfg: dict) -> Path:
    d = Path(__file__).parent / cfg["archive"]["directory"]
    d.mkdir(exist_ok=True)
    return d


def _load_index(archive_dir: Path) -> dict:
    idx = archive_dir / INDEX_FILE
    if idx.exists():
        try:
            with open(idx, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_index(archive_dir: Path, index: dict) -> None:
    idx = archive_dir / INDEX_FILE
    tmp = idx.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    tmp.replace(idx)


def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:30]


def _hash4(html: str) -> str:
    return hashlib.md5(html.encode()).hexdigest()[:4]


def save(html: str, effect_slug: str, mode: str, cfg: dict, source_path: Path | None = None, model: str = "") -> Path:
    archive_dir = _archive_dir(cfg)
    index = _load_index(archive_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    slug = _slug(effect_slug)
    h = _hash4(html)

    if mode == "update" and source_path is not None:
        src_stem = source_path.stem
        base_stem = re.sub(r"_v\d+$", "", src_stem)
        existing_versions = []
        for f in archive_dir.glob(f"{base_stem}_v*.html"):
            m = re.search(r"_v(\d+)$", f.stem)
            if m:
                existing_versions.append(int(m.group(1)))
        next_v = max(existing_versions, default=1) + 1
        filename = f"{base_stem}_v{next_v}.html"
    else:
        filename = f"{today}_{slug}_{h}.html"
        counter = 0
        while (archive_dir / filename).exists():
            counter += 1
            filename = f"{today}_{slug}_{h}{counter}.html"

    dest = archive_dir / filename
    dest.write_text(html, encoding="utf-8")

    index[filename] = {
        "created": datetime.now().isoformat(),
        "mode": mode,
        "effect": effect_slug,
        "passed_validation": True,
        **({"model": model} if model else {}),
    }
    _save_index(archive_dir, index)
    log.info("Archived %s (%d chars)", filename, len(html))

    prune(cfg)
    return dest


def get_meta(filename: str, cfg: dict) -> dict:
    return _load_index(_archive_dir(cfg)).get(filename, {})


def get_all_meta(cfg: dict) -> dict:
    return _load_index(_archive_dir(cfg))


def update_meta(filename: str, updates: dict, cfg: dict) -> None:
    archive_dir = _archive_dir(cfg)
    index = _load_index(archive_dir)
    if filename in index:
        index[filename].update(updates)
        _save_index(archive_dir, index)


def clear_title_meta(cfg: dict) -> None:
    archive_dir = _archive_dir(cfg)
    index = _load_index(archive_dir)
    for entry in index.values():
        entry.pop("title", None)
        entry.pop("description", None)
    _save_index(archive_dir, index)


def list_files(cfg: dict) -> list[Path]:
    archive_dir = _archive_dir(cfg)
    return sorted(
        (f for f in archive_dir.glob("*.html")),
        key=lambda p: p.stat().st_mtime,
    )


def pick_random(cfg: dict) -> Path | None:
    files = list_files(cfg)
    return random.choice(files) if files else None


def pick_for_update(cfg: dict, max_input_tokens: int) -> Path | None:
    max_chars = max(4096, max_input_tokens) * 4
    candidates = [f for f in list_files(cfg) if f.stat().st_size <= max_chars]
    return random.choice(candidates) if candidates else None


def delete_file(filename: str, cfg: dict) -> bool:
    archive_dir = _archive_dir(cfg)
    path = archive_dir / filename
    if not path.exists():
        return False
    path.unlink()
    index = _load_index(archive_dir)
    index.pop(filename, None)
    _save_index(archive_dir, index)
    log.info("Deleted archive file: %s", filename)
    return True


def prune(cfg: dict) -> None:
    max_files = cfg["archive"]["max_files"]
    files = list_files(cfg)
    if len(files) <= max_files:
        return
    archive_dir = _archive_dir(cfg)
    index = _load_index(archive_dir)
    for oldest in files[: len(files) - max_files]:
        oldest.unlink(missing_ok=True)
        index.pop(oldest.name, None)
        log.info("Pruned %s (archive over limit)", oldest.name)
    _save_index(archive_dir, index)
