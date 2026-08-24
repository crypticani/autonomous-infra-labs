"""Append-only JSONL, written before the action.

The fsync is the whole module. A buffered write plus a crash between deciding and acting
leaves no record that a decision was made, and afterwards "never ran" and "ran and died"
have to be distinguishable.

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
    """One line per event, flushed and fsynced before returning."""
    line = json.dumps({"ts": time.time(), "event": event, **fields}, default=str)
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    logger.info(f"audit: {line}")
