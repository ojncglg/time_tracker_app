'''
Utility script to update the users.json file with the correct sick leave fields.
Adds a yearly "sick_used_ytd" counter if missing and removes deprecated daily counters.
'''

# ------------------------------
# Standard library imports
# ------------------------------
from __future__ import annotations  # enables forward references in type hints (safe for 3.7+)
import argparse                     # parses command-line flags/options for flexible usage
import json                         # read/write JSON files (our simple datastore format)
import os                           # used for atomic replace and file operations
from pathlib import Path            # robust filesystem paths (works cross-platform)
from typing import Dict, Any, Tuple # type hints for clarity and tooling


# ------------------------------
# Constants & Defaults
# ------------------------------
# Script directory (where this file resides); useful to default paths relative to it.
SCRIPT_DIR: Path = Path(__file__).resolve().parent

# Default users.json path resolves to the app's users.json sitting next to this script
DEFAULT_USERS_PATH: Path = SCRIPT_DIR / "users.json"


# ------------------------------
# File helpers (I/O + safety)
# ------------------------------
def load_users(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Load the users JSON file from disk.

    Args:
        path: Filesystem path to users.json

    Returns:
        Dict mapping username -> user record (dict of fields)
    """
    # Open the file in text mode with explicit UTF-8 encoding for portability
    with path.open("r", encoding="utf-8") as f:  # file handle is closed automatically by context manager
        return json.load(f)                      # parse JSON into Python dict


def atomic_write_json(path: Path, data: Any) -> None:
    """
    Write JSON data atomically: write to a temporary file and replace the target.

    This prevents corruption if the process crashes mid-write.
    """
    # Create a temp file path in the same directory to ensure os.replace is atomic on the same filesystem
    tmp_path = path.with_suffix(path.suffix + ".tmp")  # e.g., users.json.tmp

    # Write the JSON payload to the temp file with pretty indentation
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)  # human-friendly formatting for diffs/reviews

    # Atomically replace target with temp (no partial writes)
    os.replace(str(tmp_path), str(path))  # os.replace is atomic on POSIX and Windows


def backup_file(path: Path) -> Path:
    """
    Create a simple backup snapshot of the target file (path.bak).

    Returns:
        Path to the backup file created.
    """
    # Define backup path with .bak extension alongside original file
    backup_path = path.with_suffix(path.suffix + ".bak")  # e.g., users.json.bak

    # Copy bytes from source to backup using read/write (avoid external deps)
    with path.open("rb") as src, backup_path.open("wb") as dst:
        dst.write(src.read())  # simple copy; acceptable for small JSON stores

    return backup_path


# ------------------------------
# Domain update logic
# ------------------------------
def update_user_record(user: Dict[str, Any]) -> Tuple[bool, bool]:
    """
    Ensure a user record has the correct sick leave fields.

    Mutations performed:
      - add 'sick_used_ytd' (int) if missing, initialized to 0
      - remove deprecated 'sick_used_today' if present

    Returns:
      (added_ytd, removed_today)
      added_ytd: True if we created 'sick_used_ytd'
      removed_today: True if we deleted 'sick_used_today'
    """
    # Track whether we changed anything in this user dict
    added_ytd = False   # did we add the yearly counter?
    removed_today = False  # did we remove the deprecated daily counter?

    # Add yearly sick usage counter if missing
    if "sick_used_ytd" not in user:   # field absent → create with safe default 0
        user["sick_used_ytd"] = 0
        added_ytd = True

    # Remove deprecated field if still present
    if "sick_used_today" in user:     # legacy key still around → delete
        del user["sick_used_today"]
        removed_today = True

    # Return flags to summarize what changed
    return added_ytd, removed_today


def migrate_users(users: Dict[str, Dict[str, Any]]) -> Tuple[int, int]:
    """
    Apply update_user_record to all users and count changes.

    Returns:
      (count_added_ytd, count_removed_today)
    """
    # Initialize counters for reporting after migration
    added_total = 0     # how many users got sick_used_ytd added
    removed_total = 0   # how many users had sick_used_today removed

    # Iterate each username and their mutable user record
    for _, user_data in users.items():
        added, removed = update_user_record(user_data)  # mutate in place
        # Increment counters based on flags returned
        if added:
            added_total += 1
        if removed:
            removed_total += 1

    # Provide aggregate stats to caller
    return added_total, removed_total


# ------------------------------
# CLI (argument parsing)
# ------------------------------
def build_parser() -> argparse.ArgumentParser:
    """
    Construct an argparse parser for the command-line interface.
    """
    # Create the parser with a short description (shown in -h/--help)
    p = argparse.ArgumentParser(description="Migrate users.json sick leave fields safely.")

    # Path to users.json; defaults to sibling file next to this script
    p.add_argument(
        "-f", "--file",
        type=Path,
        default=DEFAULT_USERS_PATH,
        help=f"Path to users.json (default: {DEFAULT_USERS_PATH})",
    )

    # Dry-run mode: report what would change without writing to disk
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report changes without writing the file.",
    )

    # Backup toggle: create users.json.bak before writing
    p.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak backup before writing changes.",
    )

    # Return the configured parser object to the caller
    return p


# ------------------------------
# Entry point
# ------------------------------
def main() -> int:
    """
    Program entry point. Orchestrates load → migrate → (optional) write.

    Returns:
        Process exit code (0 = success, non-zero on failure).
    """
    # Build the CLI parser and parse incoming arguments from sys.argv
    parser = build_parser()
    args = parser.parse_args()

    # Resolve the target users.json path (could be relative at invocation)
    users_path: Path = args.file.resolve()

    # Validate that the file exists before attempting to open
    if not users_path.exists():
        print(f"❌ File not found: {users_path}")
        return 2  # use non-zero to indicate error

    try:
        # Load existing users as a dict[str, dict]
        users: Dict[str, Dict[str, Any]] = load_users(users_path)
    except Exception as e:
        # Surface the read/JSON error and exit; no writes attempted
        print(f"❌ Failed to read JSON from {users_path}: {e}")
        return 3

    # Apply in-memory migration and compute change counts
    added_count, removed_count = migrate_users(users)

    # Report a concise summary of what changed (or would change)
    summary = (
        f"Added 'sick_used_ytd' to {added_count} user(s); "
        f"removed 'sick_used_today' from {removed_count} user(s)."
    )

    # If this is a dry-run, print summary and exit success without writing
    if args.dry_run:
        print(f"🛈 DRY-RUN: {summary}\nNo changes written.")
        return 0

    try:
        # Optionally create a backup before overwriting the file
        if args.backup:
            backup_path = backup_file(users_path)
            print(f"🗂  Backup created: {backup_path}")

        # Persist updated users via atomic write (temp + replace)
        atomic_write_json(users_path, users)
    except Exception as e:
        # Surface write errors (permissions, disk full, etc.)
        print(f"❌ Failed to write changes to {users_path}: {e}")
        return 4

    # Inform the operator of successful update (as the original script did)
    print(f"✅ All users updated. {summary}")
    return 0  # success exit code


# Python standard entry-point pattern to allow import without side-effects
if __name__ == "__main__":
    raise SystemExit(main())  # run main() and exit with its return code
