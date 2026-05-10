import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup(log_path: Path | None = None) -> logging.Logger:
    if log_path is None:
        log_path = Path(__file__).parent / "debug.log"

    root = logging.getLogger()
    if root.handlers:
        return root  # Already configured

    root.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(ch)

    return root
