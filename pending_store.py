"""
Pending request store (JSON-backed).

Purpose
-------
A tiny persistence layer for **pending vacation requests**. Its shape matches
legacy `requests_data.requests` entries so the rest of the app can read/write
without caring where the data lives.

Design & invariants
-------------------
* Storage format: a JSON list of dicts (order preserved).
* Each entry looks like:
  { user, date, type='vacation', hours, note, status='pending', handled_by='' }
* Functions here are **best-effort**: they favor continuity over exceptions.
* Atomic writes ensure readers never observe partial files.
"""

# =============================================================================
# HOW TO USE (MAINTAINER NOTES)
# -----------------------------------------------------------------------------
# - This module is intentionally small and standard-library only.
# - All helpers are "best-effort": if the file is missing or corrupted, we
#   return safe defaults instead of raising, so the UI continues to function.
# - Callers should treat this as a persistence boundary and avoid catching
#   its internal exceptions, since most are already handled here.
# - Storage path is relative to the process working directory (cwd).
#   If you need app-relative storage, ensure cwd is set accordingly on startup.
# =============================================================================

# Standard library only; keep this helper dependency‑free
from __future__ import annotations
import json, os, tempfile  # json: serialize/deserialize; os: atomic replace; tempfile: safe tmp files
from typing import Any, Dict, List

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
PENDING_FILE = "pending.json"  # relative to current working directory (see notes above)


def _read_json(path: str, default: Any) -> Any:
    """Best‑effort JSON reader.
    Returns `default` on FileNotFoundError/JSONDecodeError/OSError so callers
    can assume a value and the app keeps running in read‑mostly flows.
    NOTE: We intentionally avoid raising to keep UI responsive even if the file
          is missing or has been hand-edited into an invalid state.
    """
    try:
        # Open text file (platform default encoding); small file expected
        with open(path, "r") as f:
            # Parse JSON payload into Python objects
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # On any expected read/parse error, fall back to provided default
        return default


def _atomic_write(path: str, payload: Any) -> None:
    """Write JSON atomically using a temp file + os.replace(...).
    This ensures readers never observe a half‑written file (POSIX/modern Windows).
    NOTE: We create the temp file in the same directory to keep the replace atomic
          on the same filesystem. Permissions inherit from the directory defaults.
    """
    # Resolve target directory (absolute) for the temp file sibling
    d = os.path.dirname(os.path.abspath(path)) or "."
    # Create a unique temp file in target dir; returns low-level file descriptor
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pending-", suffix=".json")
    try:
        # Wrap the descriptor in a Python file object for JSON dump and auto-close
        with os.fdopen(fd, "w") as f:
            # Pretty-print for human diffing; small files keep this cheap
            json.dump(payload, f, indent=2)
        # Atomic replace: either the old file stays or the new one fully appears
        os.replace(tmp, path)  # atomic on POSIX/modern Windows
    finally:
        # Best-effort cleanup: remove temp file if something failed before replace
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            # Best‑effort cleanup; safe to ignore if the temp file is already gone
            pass


def list_pending() -> List[Dict[str, Any]]:
    """Return all pending items as a list of dicts.

    Notes:
    - Order is preserved as stored in the file.
    - Defensive: filters out non‑dict entries if the file was manually edited.
    - Never raises on read errors; returns [] in those cases.
    """
    # Load the raw payload (or [] if file missing/corrupt)
    items = _read_json(PENDING_FILE, default=[])
    # Defensive normalization: only keep dict entries
    return [x for x in items if isinstance(x, dict)]


def add_pending(item: Dict[str, Any]) -> None:
    """
    Append one pending entry.

    Ensures:
    - `type` defaults to "vacation"
    - `status` is normalized to "pending" (single source of truth)
    - `handled_by` exists (empty string by default)
    """
    # Copy and normalize input to a plain dict (avoid mutating caller's object)
    item = dict(item or {})
    # Default missing type to "vacation" to match legacy shape
    item.setdefault("type", "vacation")
    # Single source of truth: status is always "pending" in this queue
    item["status"] = "pending"
    # Ensure key exists; empty string means "unassigned"
    item.setdefault("handled_by", "")
    # Read current queue (never raises; returns [] on issues)
    items = list_pending()
    # Append new entry preserving list order
    items.append(item)
    # Persist updated queue atomically
    _atomic_write(PENDING_FILE, items)


def remove_pending(user: str, date: str, typ: str = "vacation") -> bool:
    """
    Remove the first entry matching (user, date, type).

    Returns:
        True if an item was removed and the file updated; False otherwise.

    Intentional behavior: only the **first** matching entry is removed to
    preserve potential duplicates added by mistake; callers can loop if needed.
    """
    # Read current snapshot of the queue
    items = list_pending()
    # Prepare output list and a flag to indicate a single removal
    out, removed = [], False
    # Iterate in order, copying items unless the first match is found
    for it in items:
        # Match on user, date, and case-insensitive type
        if (not removed
            and it.get("user") == user
            and it.get("date") == date
            and (it.get("type") or "").lower() == (typ or "").lower()):
            # Mark that we've removed exactly one matching item
            removed = True
            continue
        out.append(it)
    if removed:
        # Write the pruned list back to disk atomically
        _atomic_write(PENDING_FILE, out)
    # Report whether we actually removed an entry
    return removed
