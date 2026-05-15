#!/usr/bin/env python3
"""Backfill missing 'model' field in archive_index.json."""

import json
import shutil
from pathlib import Path

INDEX = Path(__file__).parent / "ai_demoscener/archive/archive_index.json"
BACKFILL_MODEL = "google/gemma-4-26b-a4b"

data = json.loads(INDEX.read_text(encoding="utf-8"))

missing = [k for k, v in data.items() if "model" not in v]
print(f"Entries without model: {len(missing)} / {len(data)}")

if not missing:
    print("Nothing to do.")
    raise SystemExit(0)

# Backup
backup = INDEX.with_suffix(".json.bak")
shutil.copy2(INDEX, backup)
print(f"Backed up to {backup}")

for key in missing:
    data[key]["model"] = BACKFILL_MODEL

tmp = INDEX.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
tmp.replace(INDEX)

print(f"Updated {len(missing)} entries → model: '{BACKFILL_MODEL}'")
