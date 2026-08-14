"""Append-only JSONL, written before the action -- Day 18.

The fsync is the whole module. A buffered write plus a crash between deciding and
acting leaves no record that a decision was made, which is exactly the gap an audit
log exists to close: afterwards, "never ran" and "ran and died" have to be
distinguishable, and only a line already on disk distinguishes them.

Nothing here rewrites or deletes a line. A state machine that can edit its own history
is not evidence.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

AUDIT_PATH = os.getenv("SHA_AUDIT_PATH", "audit.jsonl")


def record(event: str, **fields) -> None:
    """One line per event, flushed and fsynced before returning.

    Deliberately no try/except. If the audit write fails, the caller's action must not
    happen -- an OSError here propagates and stops the approve handler before it
    touches the cluster. Fail-closed is the only safe direction for a log whose entire
    purpose is to exist before the thing it describes.
    """
    line = json.dumps({"ts": time.time(), "event": event, **fields}, default=str)
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    logger.info(f"audit: {line}")
