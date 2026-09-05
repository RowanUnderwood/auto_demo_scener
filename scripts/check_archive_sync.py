#!/usr/bin/env python3
"""Pre-commit check: every archive_index.json entry must map to a file
that will actually be tracked after this commit.

This guards against the failure mode where archive_index.json gets
committed (or a .html file gets deleted) without its counterpart, which
leaves the GitHub Pages archive player 404ing on stale entries.
"""
import json
import subprocess
import sys

INDEX_PATH = "ai_demoscener/archive/archive_index.json"


def staged_index():
    result = subprocess.run(
        ["git", "show", f":{INDEX_PATH}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"pre-commit: {INDEX_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def is_tracked(path):
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--error-unmatch", path],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def main():
    index = staged_index()
    if index is None:
        return 0

    missing = sorted(
        filename for filename in index
        if filename.lower().endswith(".html")
        and not is_tracked(f"ai_demoscener/archive/{filename}")
    )

    if missing:
        print("pre-commit: archive_index.json references files that won't be committed:", file=sys.stderr)
        for f in missing:
            print(f"  - {f}", file=sys.stderr)
        print(file=sys.stderr)
        print("Stage the corresponding .html files (or remove their entries from", file=sys.stderr)
        print("archive_index.json) before committing. This mismatch is what caused", file=sys.stderr)
        print("the GitHub Pages archive player to 404 previously.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
