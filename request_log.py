import json
import os
from typing import List, Dict, Any

REQUEST_LOG_PATH = "request_log.json"

# Load existing log on import (safe fallback to empty list)
if os.path.exists(REQUEST_LOG_PATH):
    try:
        with open(REQUEST_LOG_PATH) as f:
            request_log: List[Dict[str, Any]] = json.load(f)
    except (json.JSONDecodeError, OSError):
        request_log = []
else:
    request_log = []


def save_request_log() -> None:
    """Persist the in-memory request_log to disk with pretty formatting."""
    with open(REQUEST_LOG_PATH, "w") as f:
        json.dump(request_log, f, indent=2)


def log_request(entry: Dict[str, Any]) -> None:
    """
    Append a single request entry to the in-memory log and persist immediately.
    Expected entry keys include:
      user, name, call_sign, sector, date, hours, status, handled_by, timestamp
    """
    request_log.append(entry)
    save_request_log()


def get_request_log() -> List[Dict[str, Any]]:
    """Return a shallow copy of the current in-memory log for read-only use."""
    return list(request_log)
