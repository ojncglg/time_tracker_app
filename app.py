"""
Flask-based shift scheduling & time‑off tracking app.

Overview
--------
A single-file Flask application used for patrol shift visibility (month/day views),
time‑off requests (vacation & sick), admin approvals, and a few agency‑specific
utilities (e.g., NCCPD accrual). Data is stored in simple JSON files so the app
remains easy to deploy and inspect without a database.

Non-functional notes
--------------------
* This file prefers small helpers + clear comments over clever abstractions.
* JSON I/O is intentionally forgiving; call sites should assume defaults.
* Soft-delete is implemented via `user['is_active']` to preserve history.
* Access control is route-level with a tiny `require_role()` decorator.

Recently added behavior
-----------------------
- Soft-delete (Archive/Unarchive) via `user['is_active']` flag
- Add User route and minimal form support
- Hides archived users from rosters and manage lists
- Blocks login for archived accounts
"""


from __future__ import annotations

# =============================================================================
# COMMENTING GUIDE (READ ME)
# -----------------------------------------------------------------------------
# This file is now annotated in three layers:
#  1) Section banners  → Explain the purpose of each major area.
#  2) Function docstrings → Summarize inputs/outputs and side‑effects.
#  3) Inline comments  → Call out non‑obvious lines and edge cases.
#
# Why not comment *every* line?  Python reads naturally; excessive per‑line
# narration becomes noise and hurts maintainability. Instead, comments are
# placed where a maintainer actually needs context (state, invariants, I/O,
# branching logic, and side‑effects). If you still want literal line‑by‑line
# comments for any single function, ping Friday and we can generate a hyper‑
# annotated version of that function on demand.
# =============================================================================


# =========================
# Standard Library Imports
# =========================
import json
import os
from calendar import monthrange
from datetime import datetime, timedelta, date
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from itertools import groupby as igroupby


# =========================
# Third-Party Imports
# =========================
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    flash,
    url_for,
    jsonify,
    Blueprint,
)


# =========================
# Local Modules
# =========================
from pending_store import list_pending, add_pending, remove_pending
import requests_data
import secrets
from request_log import request_log, log_request, get_request_log


# =========================
# Config & Constants
# =========================
# Resolve data files relative to this app.py so cwd changes don't break loads
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = BASE_DIR  # keep data files alongside app.py; change to subfolder if desired
# File paths for simple, human-auditable data stores (no DB needed)
USERS_FILE = os.path.join(DATA_DIR, "users.json")          # Persistent store for user profiles
SHIFTS_FILE = os.path.join(DATA_DIR, "shifts.json")        # Daily squad shift assignments
STATUS_FILE = os.path.join(DATA_DIR, "status_log.json")    # Per-date per-user availability overrides (e.g., Sick)

TRAINING_DAYS_FILE = os.path.join(DATA_DIR, "training_days.json")  # NCCPD training day assignments (separate store)
TOW_LOG_FILE = os.path.join(DATA_DIR, "tow_log.json")  # Public tow submissions (append-only)
TOW_COMPANIES_FILE = os.path.join(DATA_DIR, "tow_companies.json")  # Allow-list of valid tow companies (id -> {name, active})

# Role names used by @require_role() (string matching; keep stable)
ROLES: List[str] = ["user", "supervisor", "admin", "webmaster"]
VALID_SQUADS = {"A", "B", "C", "D"}
ALL_CHOICE = "All"

SUPERVISOR_RANKS = {"Senior Sergeant", "Sergeant", "Lieutenant", "Senior Lieutenant"}
SPECIAL_UNITS = ["K9", "SWAT", "EOD", "UAS", "CIT", "VRT", "CNT"]
STATUS_BUCKETS = ["Vacation", "Sick", "FMLA", "TDY", "Training", "Admin Leave", "Other"]
MAX_FORWARD_DAYS = 30  # supervisor tool forward-date cap (UI + server)
# =========================
# Flask App Setup (moved earlier so decorators can bind)
# =========================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")

# --- Blueprint: Tow Log ----------------------------------------------------
# Phase 1: Keep in this file to minimize risk; later we can move to blueprints/tow.py
# without changing behavior.
# (Blueprint removed; reverting to plain @app.route for Tow endpoints)

# =========================
# Access Control (moved earlier to satisfy decorator ordering)
# =========================
def require_role(*roles: str):
    """
    Decorator: require any of `roles` to access a route.
    Supervisors may access a small whitelist of admin tools for operational tasks.
    - On failure: flash + redirect to /login (consistent with current UX).
    """
    SUPERVISOR_ADMIN_WHITELIST = {
        "edit_user",       # Edit profile form
        "archive_user",    # Archive button
        "unarchive_user",  # Unarchive button
        "adjust_vacation", # Vacation balance adjust
        "adjust_sick",     # Sick balance adjust
        "set_password",    # Set/Reset password
        "admin_requests",  # Admin Requests page (list)
        "handle_request",  # Approve/Deny action
        "create_user",     # Create new user (alias to add_user)
    }

    def decorator(view_fn):
        @wraps(view_fn)
        def wrapped(*args, **kwargs):
            username = get_current_username()
            if not username:
                flash("Please log in.", "warning")
                return redirect(url_for("login"))
            try:
                all_users = load_users()
            except Exception:
                flash("Temporary error loading users. Please log in again.", "error")
                return redirect(url_for("login"))

            user = all_users.get(username) or {}
            user_role = user.get("role")

            # Direct allow if role is explicitly permitted
            if user_role in roles:
                return view_fn(*args, **kwargs)

            # Supervisor uplift: allow selected admin/webmaster endpoints
            if user_role == "supervisor" and (("admin" in roles) or ("webmaster" in roles)):
                endpoint = (request.endpoint or "").split(":")[-1]  # handle blueprints
                if endpoint in SUPERVISOR_ADMIN_WHITELIST:
                    return view_fn(*args, **kwargs)

            # Otherwise, block
            flash("Access denied. Insufficient privileges.", "error")
            return redirect(url_for("login"))

        return wrapped
    return decorator

@app.errorhandler(403)
def handle_403(e):
    """Normalize 403s to a friendly message + login redirect."""
    flash("Access denied.", "error")
    return redirect(url_for("login"))

# =========================
# Supervisor Tool: Set Day Status (supports clearing)
# =========================
# (Moved here after require_role and app setup)


# =========================
# Supervisor Tool: Set Day Status (supports clearing)
# =========================
@app.route("/supervisor/day-status", methods=["GET", "POST"], endpoint="day_status")
@require_role("supervisor", "admin", "webmaster")
def supervisor_day_status():
    """
    Supervisors set per-day status for officers in their own squad; Admin/Webmaster can set for anyone.
    - Allowed statuses (non-protected): FMLA, TDY, Training, Field Training, Admin Leave, Other.
    - Clearing sets a day back to 'Available' (but never clears Vacation/Sick).
    - Date scope: future only, within next MAX_FORWARD_DAYS.
    """
    # Load context
    all_users = load_users()
    actor_username = get_current_username()
    if not actor_username:
        return redirect(url_for("login"))
    actor = all_users.get(actor_username, {})
    actor_role = actor.get("role", "user")
    actor_squad = actor.get("squad")
    allowed_statuses = ["FMLA", "TDY", "Training", "Field Training", "Admin Leave", "Other"]
    # Officer choices (squad-scoped for supervisors)
    officers = []
    for uname, u in all_users.items():
        if not is_user_active(u):
            continue
        if actor_role == "supervisor" and u.get("squad") != actor_squad:
            continue
        label = f"{u.get('last_name','')}, {u.get('first_name','')} — {uname}"
        officers.append({"username": uname, "label": label})
    officers.sort(key=lambda x: x["label"].lower())

    if request.method == "GET":
        return render_template(
            "supervisor_day_status.html",
            officers=officers,
            allowed_statuses=allowed_statuses,
            max_forward_days=MAX_FORWARD_DAYS,
        )

    # POST
    username = (request.form.get("username") or "").strip()
    start_str = (request.form.get("date") or "").strip()
    end_str = (request.form.get("end_date") or "").strip()
    status_req = (request.form.get("status") or "").strip()
    note = (request.form.get("note") or "").strip()

    if not username or not start_str:
        flash("Please select an officer and a start date.", "error")
        return redirect(url_for("day_status"))

    target_user = all_users.get(username)
    if not target_user:
        flash("Selected officer not found.", "error")
        return redirect(url_for("day_status"))
    if actor_role == "supervisor" and target_user.get("squad") != actor_squad:
        flash("Access denied: supervisors can only update their own squad.", "error")
        return redirect(url_for("day_status"))

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
    except Exception:
        flash("Invalid start date.", "error")
        return redirect(url_for("day_status"))

    if end_str:
        try:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        except Exception:
            end_dt = start_dt
    else:
        end_dt = start_dt
    if end_dt < start_dt:
        end_dt = start_dt

    today = date.today()
    max_day = today + timedelta(days=int(MAX_FORWARD_DAYS))
    if start_dt < today or end_dt > max_day:
        flash(f"Dates must be between {today.isoformat()} and {max_day.isoformat()}.", "error")
        return redirect(url_for("day_status"))

    if status_req != "Available" and status_req not in allowed_statuses:
        flash("Invalid status selection.", "error")
        return redirect(url_for("day_status"))

    # Build date list
    dates = []
    cur = start_dt
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    status_log = load_status_log()
    PROTECTED = {"Vacation", "Sick"}
    updated = 0
    skipped = 0

    for ds in dates:
        cur_map = status_log.setdefault(ds, {})
        current = (cur_map.get(username) or "Available").strip()
        if current in PROTECTED:
            skipped += 1
            continue
        if status_req == "Available":
            if current != "Available":
                cur_map[username] = "Available"
                updated += 1
            else:
                skipped += 1
            continue
        if current == status_req:
            skipped += 1
            continue
        cur_map[username] = status_req
        updated += 1

    save_status_log(status_log)

    try:
        audit_append(
            all_users,
            target_username=username,
            action="supervisor_day_status_update",
            details={
                "range": {"start": start_str, "end": end_str or start_str},
                "status": status_req,
                "updated": updated,
                "skipped": skipped,
                "note": note,
            },
            save_immediately=True,
        )
    except Exception:
        pass

    if updated and not skipped:
        flash(f"Updated {updated} day(s).", "success")
    elif updated and skipped:
        flash(f"Updated {updated} day(s). Skipped {skipped} due to conflicts or no change.", "success")
    else:
        flash("No changes applied (all days were protected or already matched).", "warning")

    return redirect(url_for("day_status"))

# =========================
# Supervisor Tool: Live Preview (JSON)
# =========================
@app.route("/day-status/preview")
def day_status_preview():
    """
    Return a JSON preview of what would change for a proposed supervisor day-status update.
    Query params: username, start_date, end_date (optional), status
    Output: list of {date, current_status, will_change: bool, reason: 'vacation'|'sick'|'none'|'same'}
    """
    all_users = load_users()
    actor_username = get_current_username()
    actor = all_users.get(actor_username, {}) if actor_username else {}
    actor_role = actor.get("role", "user")
    actor_squad = actor.get("squad")
    if not actor_username or actor_role not in {"supervisor", "admin", "webmaster"}:
        return jsonify([])

    username = (request.args.get("username") or "").strip()
    start_str = (request.args.get("start_date") or "").strip()
    end_str = (request.args.get("end_date") or "").strip() or start_str
    status_req = (request.args.get("status") or "").strip() or "Available"

    target = all_users.get(username)
    if not target:
        return jsonify([])
    if actor_role == "supervisor" and target.get("squad") != actor_squad:
        return jsonify([])

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
    except Exception:
        return jsonify([])

    if end_dt < start_dt:
        end_dt = start_dt

    today = date.today()
    max_day = today + timedelta(days=int(MAX_FORWARD_DAYS))
    if start_dt < today:
        start_dt = today
    if end_dt > max_day:
        end_dt = max_day

    allowed_statuses = {"FMLA", "TDY", "Training", "Field Training", "Admin Leave", "Other", "Available"}
    if status_req not in allowed_statuses:
        status_req = "Available"
    PROTECTED = {"Vacation", "Sick"}

    status_log = load_status_log()
    out = []
    cur = start_dt
    while cur <= end_dt:
        ds = cur.strftime("%Y-%m-%d")
        current = (status_log.get(ds, {}).get(username) or "Available").strip()
        reason = "none"
        will_change = False

        if current in PROTECTED:
            reason = current.lower()
            will_change = False
        else:
            if status_req == "Available":
                will_change = (current != "Available")
                if not will_change:
                    reason = "same"
            else:
                will_change = (current != status_req)
                if not will_change:
                    reason = "same"

        out.append({
            "date": ds,
            "current_status": current if current else "Available",
            "will_change": bool(will_change),
            "reason": reason,
        })
        cur += timedelta(days=1)

    return jsonify(out)

# Numeric user fields we parse/serialize as floats in forms
USER_NUMERIC_FIELDS = {"vacation_left", "vacation_used_today", "sick_left", "sick_used_ytd"}

 # (app setup moved earlier to satisfy decorator ordering)


 
# =========================
# JSON Helpers
# =========================
def _read_json(path: str, default: Any) -> Any:
    """Read JSON file at `path`; return `default` if missing/corrupt."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: str, payload: Any) -> None:
    """
    Write `payload` to `path` with indent. Swallow OS errors (best-effort).
    """
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        # Intentional: the app favors continuity over crashes in read-mostly flows.
        # Consider structured logging in production to capture these events.
        pass


def load_users() -> Dict[str, Dict[str, Any]]:
    """Load all user profiles keyed by username."""
    return _read_json(USERS_FILE, default={})


def save_users(all_users: Dict[str, Dict[str, Any]]) -> None:
    """Persist all user profiles to disk."""
    _write_json(USERS_FILE, all_users)


def save_users_atomic(all_users: Dict[str, Dict[str, Any]]) -> None:
    """
    Atomically persist all user profiles to disk to avoid partial writes.
    Writes to USERS_FILE + '.tmp' then replaces the original.
    """
    tmp_path = USERS_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(all_users, f, indent=2)
    os.replace(tmp_path, USERS_FILE)


def load_shifts() -> Dict[str, Dict[str, str]]:
    """Load daily shift assignments: {YYYY-MM-DD: {squad: label}}."""
    return _read_json(SHIFTS_FILE, default={})


def load_status_log() -> Dict[str, Dict[str, str]]:
    """Load per-date per-user status overrides (e.g., Sick)."""
    return _read_json(STATUS_FILE, default={})


def save_status_log(status_log: Dict[str, Dict[str, str]]) -> None:
    """Persist status overrides to disk."""
    _write_json(STATUS_FILE, status_log)


def load_training_days() -> List[Dict[str, Any]]:
    """Load training day entries list (each entry is a dict)."""
    return _read_json(TRAINING_DAYS_FILE, default=[])

def save_training_days(entries: List[Dict[str, Any]]) -> None:
    """Persist training day entries to disk."""
    _write_json(TRAINING_DAYS_FILE, entries)


def load_tow_log() -> List[Dict[str, Any]]:
    """Load Tow Log entries (append-only list)."""
    return _read_json(TOW_LOG_FILE, default=[])


def save_tow_log(entries: List[Dict[str, Any]]) -> None:
    """Persist Tow Log entries to disk."""
    _write_json(TOW_LOG_FILE, entries)


def load_tow_companies() -> Dict[str, Dict[str, Any]]:
    """Load allow-listed tow companies keyed by numeric id as string. Missing file -> {}."""
    try:
        data = _read_json(TOW_COMPANIES_FILE, default={})
        if isinstance(data, dict):
            return {str(k): (v or {}) for k, v in data.items()}
        return {}
    except Exception:
        return {}


 
# =========================
# Utility Helpers
# =========================
def get_current_username() -> Optional[str]:
    """Return username from session or None if not logged in."""
    return session.get("username")


def get_current_user(all_users: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the current user dict from `all_users` using session username."""
    uname = get_current_username()
    return all_users.get(uname) if uname else None


def is_on(shift_label: Optional[str]) -> bool:
    """True if shift label represents an 'on' day (i.e., not empty/Off)."""
    return bool(shift_label) and str(shift_label).strip().lower() != "off"


def safe_parse_hhmm(value: str, fallback: str = "07:00") -> Optional[datetime]:
    """Parse 'HH:MM' to datetime (date part ignored). Return None on failure."""
    try:
        return datetime.strptime(value or fallback, "%H:%M")
    except (TypeError, ValueError):
        return None  # Invalid or empty input → caller must handle gracefully


def compute_end_time_str(start_dt: Optional[datetime]) -> str:
    """Compute end time = start + 11h15m for display, or 'Unknown' if invalid."""
    if not start_dt:
        return "Unknown"  # Missing/invalid start; avoid raising in templates
    return (start_dt + timedelta(hours=11, minutes=15)).strftime("%H:%M")


def zone_of(call_sign: str) -> str:
    """Derive patrol zone from first digit of call sign; else 'Other'."""
    if not call_sign:
        return "Other"
    first = str(call_sign).strip()[:1]
    return first if first.isdigit() else "Other"


def rotation_label(date_str: str, squad: Optional[str]) -> Optional[str]:
    """
    Return a human label for Squad A rotation based on a 28‑day cadence:
      - Days for 14 consecutive days (Weeks 1–2)
      - Nights for 14 consecutive days (Weeks 1–2)
    Only shown for Squad A; return None for others.
    Adjust 'anchor' to the first day of a 'Days/Week 1' block in your real schedule.
    """
    if squad != "A":
        return None  # Only Squad A shows a rotation badge
    try:
        # Parse the YYYY-MM-DD string to a date; invalid input → no label
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    # Anchor marks day 0 of the rotation cycle (Days/Week 1). Update once and
    # all math follows. Keeping it as a date (not datetime) avoids TZ issues.
    anchor = date(2025, 8, 13)

    cycle_len = 28  # Days(14) + Nights(14)
    delta_days = (target - anchor).days  # Signed distance from anchor
    offset = delta_days % cycle_len      # Position within the 28-day window [0..27]

    # First 14 days are Days, next 14 are Nights
    phase = "Days" if offset < 14 else "Nights"

    # Within each 14-day block, the first 7 are Week 1, next 7 are Week 2
    week_in_phase = 1 if (offset % 14) < 7 else 2

    return f"Squad A Rotation: {phase} (Week {week_in_phase}/2)"



def month_bounds(year: int, month: int) -> Tuple[datetime, int, int]:
    """
    Return (first_day, num_days, start_weekday) for a given month.
    - start_weekday: 0..6, Sunday=0 (template expects this mapping)
    """
    first_day = datetime(year, month, 1)  # 1st of requested month
    _, num_days = monthrange(year, month) # number of days in month
    # Python weekday(): Monday=0..Sunday=6 → convert so Sunday=0..Saturday=6
    start_weekday = (first_day.weekday() + 1) % 7
    return first_day, num_days, start_weekday

# =========================
# NCCPD ONLY: Accrual helpers (vacation entitlement, carryover, min-use)
# =========================

def _parse_iso_date(s: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None

def _years_of_service_at(seniority: Optional[date], at_day: date) -> int:
    """
    NCCPD ONLY:
    Whole years of service as of `at_day`. If seniority is None, returns 0.
    """
    if not seniority:
        return 0
    years = at_day.year - seniority.year
    if (at_day.month, at_day.day) < (seniority.month, seniority.day):
        years -= 1
    return max(years, 0)

def nccpd_entitlement_days_for_year(seniority_date_iso: Optional[str], year: int) -> int:
    """
    NCCPD ONLY:
    Compute annual vacation ENTITLEMENT in DAYS for a given calendar year.
    Rule summary (contract):
      - <1 year by Dec 31 → 0 days
      - 1–<5 years → 10 days
      - 5–<10 years → 15 days
      - 10–<15 years → 20 days
      - 15 years → 25 days
      - >15 years → 25 + 1 per additional year
    'Milestone within year' clause is satisfied by evaluating at Dec 31 of 'year'.
    """
    seniority = _parse_iso_date(seniority_date_iso)
    dec31 = date(year, 12, 31)
    yrs = _years_of_service_at(seniority, dec31)

    if yrs < 1:
        return 0
    if 1 <= yrs < 5:
        return 10
    if 5 <= yrs < 10:
        return 15
    if 10 <= yrs < 15:
        return 20
    # 15 or more:
    if yrs == 15:
        return 25
    # > 15:
    return 25 + (yrs - 15)

def nccpd_apply_carryover_and_min_use(
    prior_vacation_left_hours: float,
    over_cap_approved: bool
) -> Tuple[float, int, bool]:
    """
    NCCPD ONLY:
    Apply carryover cap & derive min-use requirement.
      - Max carryover (without special approval) = 560 hours.
      - If carryover ≤ 240  → min-use = 40 hours this year.
      - If 240 < carryover ≤ 560 → min-use = 80 hours this year.
      - If carryover > 560 and not approved → clamp to 560 and flag supervisor.
    Returns: (carryover_out_hours, min_required_hours, supervisor_flag_required)
    """
    hours = float(prior_vacation_left_hours or 0.0)
    supervisor_flag = False

    if hours > 560.0 and not over_cap_approved:
        hours = 560.0
        supervisor_flag = True  # needs supervisor attention (exceeded without approval)

    if hours <= 240.0:
        min_required = 40
    elif hours <= 560.0:
        min_required = 80
    else:
        # (Shouldn't happen unless approved; if approved > 560, no special min-use rule in spec)
        min_required = 80

    return (hours, min_required, supervisor_flag)

def nccpd_accrual_for_user(user: Dict[str, Any], target_year: int) -> Dict[str, Any]:
    """
    NCCPD ONLY:
    Compute & APPLY accrual to a single user dict (in-memory).
      - Uses fields:
          * seniority_date (YYYY-MM-DD)        [string, optional]
          * hours_per_day_default               [float, default 8.0]
          * vacation_left                       [float, current balance before rollover]
          * vacation_over_cap_approved          [bool, default False]
      - Writes/updates fields:
          * vacation_entitlement_days_year
          * vacation_entitlement_hours_year
          * vacation_carryover_hours
          * vacation_min_required_hours
          * vacation_left  (new year = carryover_out + entitlement_hours)
          * nccpd_supervisor_alert (bool) if over-cap without approval
          * audit (append entry)
    Returns the updated user dict (mutated).
    """
    # Read current fields with safe defaults
    seniority_date_iso = str(user.get("seniority_date", "") or "").strip()
    hpd = float(user.get("hours_per_day_default", 8.0) or 8.0)
    prior_vac_left = float(user.get("vacation_left", 0.0) or 0.0)
    over_cap_approved = bool(user.get("vacation_over_cap_approved", False))

    # 1) Entitlement (days → hours) for THIS calendar year
    ent_days = nccpd_entitlement_days_for_year(seniority_date_iso, target_year)
    ent_hours = float(ent_days) * hpd

    # 2) Carryover from prior balance & min-use
    carry_out, min_use, needs_flag = nccpd_apply_carryover_and_min_use(
        prior_vacation_left_hours=prior_vac_left,
        over_cap_approved=over_cap_approved,
    )

    # 3) New-year opening balance
    new_balance = carry_out + ent_hours

    # 4) Persist back to user
    user["vacation_entitlement_days_year"] = ent_days
    user["vacation_entitlement_hours_year"] = round(ent_hours, 2)
    user["vacation_carryover_hours"] = round(carry_out, 2)
    user["vacation_min_required_hours"] = int(min_use)
    user["vacation_left"] = round(new_balance, 2)

    # Supervisor flag if exceeded cap without approval
    if needs_flag:
        user["nccpd_supervisor_alert"] = True
    else:
        # clear old flag if previously set
        if "nccpd_supervisor_alert" in user:
            user["nccpd_supervisor_alert"] = False

    # 5) Audit entry
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    entry = {
        "ts": ts,
        "action": "nccpd_accrual",
        "year": target_year,
        "entitlement_days": ent_days,
        "entitlement_hours": round(ent_hours, 2),
        "carryover_in": round(prior_vac_left, 2),
        "carryover_out": round(carry_out, 2),
        "min_required": int(min_use),
        "over_cap_approved": bool(over_cap_approved),
        "supervisor_alert": bool(needs_flag),
    }
    audit = user.get("audit") or []
    if not isinstance(audit, list):
        audit = []
    audit.append(entry)
    user["audit"] = audit

    return user



# --- Holiday helpers ---------------------------------------------------------
# Pure helpers; safe to keep in this file or move to holidays.py later.

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """
    weekday: Monday=0 ... Sunday=6
    n: 1=first, 2=second, ...
    """
    d = date(year, month, 1)
    days_ahead = (weekday - d.weekday()) % 7
    d = d + timedelta(days=days_ahead)
    return d + timedelta(weeks=n - 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    days_back = (d.weekday() - weekday) % 7
    return d - timedelta(days=days_back)


def _easter_date(year: int) -> date:
    """
    Western (Gregorian) Easter: Anonymous Gregorian algorithm (Meeus/Jones/Butcher).
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _election_day(year: int) -> date:
    """
    U.S. Election Day: first Tuesday after the first Monday in November
    (i.e., between Nov 2 and Nov 8).
    """
    first = date(year, 11, 1)
    first_monday = first + timedelta(days=(0 - first.weekday()) % 7)
    return first_monday + timedelta(days=1)


def get_holidays_map(year: int, election_base_year: int = 2025) -> dict:
    """
    Returns a dict: 'YYYY-MM-DD' -> Holiday Name.

    Election Day appears in years where (year - election_base_year) % 2 == 0.
    (You asked to treat 2025 as an election year and every other year thereafter.)
    """
    easter = _easter_date(year)
    good_friday = easter - timedelta(days=2)

    days = {
        date(year, 1, 1): "New Year’s Day",
        _nth_weekday_of_month(year, 1, 0, 3): "Martin Luther King’s Birthday",   # 3rd Mon
        date(year, 2, 12): "Lincoln’s Birthday",
        _nth_weekday_of_month(year, 2, 0, 3): "Washington’s Birthday",           # 3rd Mon
        good_friday: "Good Friday",
        _last_weekday_of_month(year, 5, 0): "Memorial Day",                      # last Mon
        date(year, 7, 4): "Independence Day",
        _nth_weekday_of_month(year, 9, 0, 1): "Labor Day",                       # 1st Mon
        _nth_weekday_of_month(year, 10, 0, 2): "Columbus Day",                   # 2nd Mon
        date(year, 11, 11): "Veterans’ Day",
        _nth_weekday_of_month(year, 11, 3, 4): "Thanksgiving Day",               # 4th Thu
        _nth_weekday_of_month(year, 11, 3, 4) + timedelta(days=1): "Day after Thanksgiving Day",
        date(year, 12, 24): "Christmas Eve Day (4 hours)",
        date(year, 12, 25): "Christmas Day",
    }

    # Election Day (every other year starting 2025)
    if (year - election_base_year) % 2 == 0:
        days[_election_day(year)] = "Election Day"

    return {d.isoformat(): name for d, name in days.items()}



 
# =========================
# User Defaults & Archive Helpers
# =========================
def default_user(username: str) -> Dict[str, Any]:
    """Create a new user skeleton (keep fields aligned with your schema)."""
    return {
        "first_name": "",
        "last_name": "",
        "rank": "",
        "squad": "",
        "call_sign": "",
        "sector": "",
        "skills": [],
        "start_time": "07:00",
        "role": "user",
        "password": "",
        "vacation_left": 0.0,
        "vacation_used_today": 0.0,
        "sick_left": 0.0,
        "sick_used_ytd": 0.0,
        "is_active": True,  # soft-delete flag
    }


def is_user_active(u: Dict[str, Any]) -> bool:
    """Consider missing flag as active for backward compatibility."""
    return bool(u.get("is_active", True))

# =========================
# NCCPD-ONLY: Audit Trail Helpers
# =========================
def _now_iso() -> str:
    """UTC-ish stamp in ISO for audit entries."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _actor() -> Dict[str, str]:
    """
    Who is performing the action?
    Returns dict with username/role for the audit log.
    """
    return {
        "username": session.get("username") or "anonymous",
        "role": session.get("role") or "unknown",
    }

def audit_append(all_users: Dict[str, Dict[str, Any]], target_username: str,
                 action: str, details: Dict[str, Any], save_immediately: bool = True) -> None:
    """
    Append a single audit event to users[target]['audit'].
    Each event: { ts, action, actor, details }
    - action: short verb like 'request_submit', 'vacation_approve', 'cancel_request', 'profile_update'
    - details: arbitrary dict; keep it lean (dates, hours, fields_changed, etc.)
    """
    user = all_users.get(target_username)
    if not user:
        return  # target user not found; nothing to write

    # ensure audit list exists
    if "audit" not in user or not isinstance(user["audit"], list):
        user["audit"] = []

    event = {
        "ts": _now_iso(),
        "action": action,
        "actor": _actor(),     # {username, role}
        "details": details or {}
    }
    user["audit"].append(event)

    # Optional ring-buffer to avoid unlimited growth (keep last 500)
    if len(user["audit"]) > 500:
        user["audit"] = user["audit"][-500:]

    if save_immediately:
        save_users(all_users)

def diff_fields(before: Dict[str, Any], after: Dict[str, Any], fields: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Return {field: {'from': X, 'to': Y}} for changed fields only.
    Useful for profile updates.
    """
    out = {}
    for f in fields:
        if (before.get(f) != after.get(f)):
            out[f] = {"from": before.get(f), "to": after.get(f)}
    return out



 
# (require_role and 403 handler defined earlier to satisfy decorator ordering)


@app.before_request
def _ensure_csrf_token():
    # create once per session
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)


 
# =========================
# New Year Sick Reset
# =========================
@app.before_first_request
def reset_sick_usage_if_needed() -> None:
    """
    On first request after server start: if today is Jan 1, reset sick_used_ytd.
    (Keeps previous year’s totals from carrying over.)
    """
    today = datetime.today()
    if today.month == 1 and today.day == 1:
        all_users = load_users()
        for user_data in all_users.values():
            user_data["sick_used_ytd"] = 0
        save_users(all_users)

@app.context_processor
def _inject_csrf_token():
    # exposes csrf_token() to templates
    def csrf_token():
        return session.get('csrf_token', '')
    return {'csrf_token': csrf_token}
 
# =========================
# Archive Enforcement
# =========================

# --- Register blueprints ----------------------------------------------------
# Keep registration near app setup so it’s obvious and runs early.
# app.register_blueprint(tow_bp)  # moved to end (after tow routes are defined)

@app.before_request
def _enforce_active_user():
    """
    If a logged-in user has been archived (is_active=False),
    immediately end their session and send them to login.
    """
    # Allow reaching login/logout without a loop
    if request.endpoint in {"login", "logout"}:
        return  # Skip enforcement for auth endpoints

    uname = session.get("username")
    if not uname:
        return  # Not logged in; nothing to enforce

    users = load_users()               # Read the latest archive flags
    u = users.get(uname)
    # Treat missing flag as active by default; only block explicit False
    if u and (u.get("is_active", True) is False):
        session.clear()                # Kill session immediately
        flash("Your account is inactive. Please contact an administrator.", "error")
        return redirect(url_for("login"))


 
# =========================
# Calendar: Month View
# =========================
@app.route("/calendar")
def calendar_view():
    """
    Render monthly calendar:
      - Supports ?year=YYYY&month=1..12
      - Highlights the current user's squad if on duty for a day.
    """
    today = datetime.today()
    # Robust query parsing with fallback to current month/year
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        if month < 1 or month > 12:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month

    first_day, num_days, start_weekday = month_bounds(year, month)
    shifts = load_shifts()

    # Determine viewer's squad (if logged in) to compute highlights
    user_squad = None
    username = get_current_username()
    if username:
        try:
            all_users = load_users()  # needed only to look up viewer's squad
            user_squad = all_users.get(username, {}).get("squad")
        except Exception:
            user_squad = None

    # Build day cells, padding with leading blanks for starting weekday
    cells: List[Optional[Dict[str, Any]]] = [None] * start_weekday
    for day in range(1, num_days + 1):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        day_shifts = shifts.get(date_str, {})
        # If viewer has a squad and it’s ON that day, mark highlight
        is_user_squad = False
        user_shift_label = None
        if user_squad:
            label = day_shifts.get(user_squad)
            if is_on(label):
                is_user_squad = True
                user_shift_label = label
        cells.append(
            {
                "date": date_str,
                "day": day,
                "shifts": day_shifts,
                "is_user_squad": is_user_squad,
                "user_shift_label": user_shift_label,
            }
        )
    # --- Tag holidays on cells (visual-only) ---
    holiday_map = get_holidays_map(year)  # {'YYYY-MM-DD': 'Holiday Name', ...}
    for c in cells:
        if not c:
            continue
        name = holiday_map.get(c["date"])
        if name:
            c["holiday_name"] = name
    # --- Tag "today" (visual-only) ---
    today_str = date.today().isoformat()
    for c in cells:
        if not c:
            continue
        c["is_today"] = (c["date"] == today_str)



    # Compute prev/next month links
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_name=first_day.strftime("%B"),
        cells=cells,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        user_squad=user_squad,
    )

 
# =========================
# Calendar: Day View
# =========================
@app.route("/calendar/<date>")
def view_day(date: str):
    """
    Render a single day’s roster with flexible squad selection.

    Behavior:
      - Accepts ?squad=A|B|C|D|All
      - If query param is missing/invalid or the squad is Off:
          * Prefer the current user's squad if ON that day
          * Else pick the first ON squad alphabetically
          * Else show "All"
      - Hides archived users (is_active=False) everywhere in roster
      - Computes supervisor lists, zone groupings, specialty counts, etc.
    """
    # Fresh data pulls
    selected_squad = request.args.get("squad")
    all_users = load_users()
    shifts = load_shifts()
    status_log = load_status_log()

    # Shifts for the day (default to "Off" for missing squads)
    raw_day = shifts.get(date, {}) or {}
    day_shifts = {squad: raw_day.get(squad, "Off") for squad in ["A", "B", "C", "D"]}

    # Determine initial selected squad validity
    valid_choices = set(VALID_SQUADS) | {ALL_CHOICE}
    # Validate the query param strictly; anything else is treated as missing
    if selected_squad not in valid_choices:
        selected_squad = None

    # Helper: find the first ON squad (A..D)
    def first_on_squad() -> Optional[str]:
        for s in ["A", "B", "C", "D"]:
            if is_on(day_shifts.get(s)):
                return s
        return None

    # If missing/invalid/Off: prefer viewer's ON squad, else first ON, else "All"
    if (not selected_squad) or (
        selected_squad != ALL_CHOICE and not is_on(day_shifts.get(selected_squad))
    ):
        viewer = get_current_user(all_users) or {}
        viewer_squad = viewer.get("squad")
        if viewer_squad in VALID_SQUADS and is_on(day_shifts.get(viewer_squad)):
            selected_squad = viewer_squad
        else:
            selected_squad = first_on_squad() or ALL_CHOICE

    # Build roster of members for squads that are ON
    roster: Dict[str, List[Dict[str, Any]]] = {s: [] for s in ["A", "B", "C", "D"]}
    for uname, udata in all_users.items():
        # NEW: hide archived users
        if not is_user_active(udata):
            continue

        squad = udata.get("squad")
        if squad not in VALID_SQUADS:
            continue

        shift_type = day_shifts.get(squad, "Off")
        if not is_on(shift_type):
            continue

        # Compute end time from start_time (11h15m later)
        start_dt = safe_parse_hhmm(udata.get("start_time", "07:00"), fallback="07:00")
        end_time = compute_end_time_str(start_dt)

        # Status for this user on this date (e.g., Sick)
        user_status = status_log.get(date, {}).get(uname, "Available")

        row = dict(udata)
        row.update(
            {
                "username": uname,
                "shift_type": shift_type,
                "end_time": end_time,
                "status": user_status,
            }
        )
        roster[squad].append(row)

    # Apply squad filter unless "All"
    if selected_squad != ALL_CHOICE:
        roster = {k: (v if k == selected_squad else []) for k, v in roster.items()}

    # Flatten for counting/sorting buckets
    scheduled: List[Dict[str, Any]] = []
    for members in roster.values():
        scheduled.extend(members)

    # Classify into supervisors, zones, categories, specialty counts
    supervisors: List[Dict[str, Any]] = []
    categories: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STATUS_BUCKETS}
    by_zone: Dict[str, List[Dict[str, Any]]] = {}
    available_count = 0

    unit_counts = {key: 0 for key in SPECIAL_UNITS}
    # Normalize skill aliases to canonical unit keys
    aliases = {
        "k9": "K9",
        "k-9": "K9",
        "k 9": "K9",
        "swat": "SWAT",
        "eod": "EOD",
        "uas": "UAS",
        "cit": "CIT",
        "vrt": "VRT",
        "cnt": "CNT",
    }

    for u in scheduled:
        status = (u.get("status") or "Available").strip()
        normalized = status.title()

        # Supervisors listed separately
        if u.get("rank") in SUPERVISOR_RANKS:
            supervisors.append(u)
        else:
            z = zone_of(u.get("call_sign", ""))
            by_zone.setdefault(z, []).append(u)

        # Non-available buckets
        if normalized in categories:
            categories[normalized].append(u)
        else:
            if normalized != "Available":
                categories["Other"].append(u)

        # Available headcount + specialties
        if normalized == "Available":
            available_count += 1
            skills_lower = [s.strip().lower() for s in (u.get("skills") or [])]
            for s in skills_lower:
                if s in aliases:
                    unit_counts[aliases[s]] += 1

    # Weekday name (for template header)
    try:
        parsed_dt = datetime.strptime(date, "%Y-%m-%d")
        weekday_name = parsed_dt.strftime("%A")
    except ValueError:
        weekday_name = ""

    # Current viewer's squad (for subtle highlight)
    current_user_squad = None
    viewer_username = get_current_username()
    if viewer_username:
        current_user = all_users.get(viewer_username)
        if current_user:
            current_user_squad = current_user.get("squad")

    # Rotation badge for Squad A (if selected)
    rotation_info = rotation_label(date, selected_squad)

    return render_template(
        "day_view.html",
        date=date,
        weekday_name=weekday_name,
        roster=roster,
        day_shifts=day_shifts,
        current_user_squad=current_user_squad,
        selected_squad=selected_squad,
        supervisors=supervisors,
        by_zone=by_zone,
        categories=categories,
        available_count=available_count,
        unit_counts=unit_counts,
        rotation_info=rotation_info,
    )


 
# =========================
# My Requests: Summary (YTD balances) + Flat List
# =========================
@app.route("/my-requests")
def my_requests():
    """
    Officer self-serve page: balances + personal request history.

    Summary cards:
      - Vacation used YTD: sum of APPROVED vacation entries in the current year (from request log)
      - Vacation remaining: user["vacation_left"]
      - Sick used YTD: user["sick_used_ytd"]
      - Sick remaining: user["sick_left"]

    Table:
      - One row per request-day (pending/approved/denied vacation; logged sick)
      - Current year only, newest first
    """
    # --- Auth guard ---
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    # --- Load user safely ---
    all_users = load_users()
    user = all_users.get(username)
    if not user:
        flash("User not found. Please log in again.", "error")
        return redirect(url_for("login"))

    # --- Determine current year scope ---
    today = datetime.today()
    current_year = today.year
    current_date = date.today().isoformat()

    # --- Pull full log once (approved/denied/ logged entries) ---
    try:
        log_data = get_request_log()
    except Exception:
        log_data = []

    # Helper: accept only rows matching the current calendar year
    def is_current_year(d: str) -> bool:
        try:
            return datetime.strptime(d, "%Y-%m-%d").year == current_year
        except Exception:
            return False

    # Sum APPROVED vacation hours from the request log (YTD)
    vacation_used_ytd = 0.0
    for entry in log_data:
        if (
            entry.get("user") == username
            and (entry.get("type") or "").lower() == "vacation"
            and (entry.get("status") or "").lower() == "approved"
            and is_current_year(entry.get("date", ""))
        ):
            try:
                vacation_used_ytd += float(entry.get("hours", 0))
            except (TypeError, ValueError):
                pass

    # Sick usage is tracked live on the user profile
    sick_used_ytd = float(user.get("sick_used_ytd", 0) or 0)
    sick_left = float(user.get("sick_left", 0) or 0)

    # Vacation remaining comes from the profile (deducted on approval)
    vacation_left = float(user.get("vacation_left", 0) or 0)

    # --- Build a flat list of the user's requests (pending + history) ---
    rows: List[Dict[str, Any]] = []

    # Pending vacation (from persistent store and in-memory queue for backward compatibility)
    pending_items = []
    try:
        pending_items.extend(list_pending())
    except Exception:
        # persistent pending store may be unavailable in older deployments
        pass

    # Also include in-memory queue entries if present (legacy path)
    try:
        mem_pending = getattr(requests_data, "requests", [])
        for r in mem_pending:
            if (r.get("status") or "pending") == "pending":
                pending_items.append(r)
    except Exception:
        pass

    for r in pending_items:
        if r.get("user") != username:
            continue
        rows.append(
            {
                "start_date": r.get("date"),
                "end_date": r.get("date"),
                "type": (r.get("type") or "vacation").title(),
                "hours_per_day": float(r.get("hours", 0) or 0),
                "status": (r.get("status") or "pending").title(),
            }
        )

    # History from log (approved/denied vacation; logged sick). Current year only.
    for entry in log_data:
        if entry.get("user") != username:
            continue
        d = entry.get("date", "")
        if not is_current_year(d):
            continue

        t = (entry.get("type") or "").lower()
        s = (entry.get("status") or "").lower()

        if t == "sick" and s == "logged":
            rows.append(
                {
                    "start_date": d,
                    "end_date": d,
                    "type": "Sick",
                    "hours_per_day": float(entry.get("hours", 0) or 0),
                    "status": "Logged",
                }
            )
        elif t == "vacation" and s in {"approved", "denied", "cancelled"}:
            rows.append(
                {
                    "start_date": d,
                    "end_date": d,
                    "type": "Vacation",
                    "hours_per_day": float(entry.get("hours", 0) or 0),
                    "status": s.title(),  # Approved / Denied / Cancelled
                }
            )

    # --- Optional filters for personal history (GET params) ---
    f_type = (request.args.get("type") or "").strip().lower()      # vacation|sick
    f_status = (request.args.get("status") or "").strip().lower()  # pending|approved|denied|cancelled|logged
    f_from = _parse_iso_date(request.args.get("date_from"))
    f_to = _parse_iso_date(request.args.get("date_to"))

    def _row_date_ok(ds: str) -> bool:
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            return False
        if f_from and d < f_from:
            return False
        if f_to and d > f_to:
            return False
        return True

    def _row_ok(r: Dict[str, Any]) -> bool:
        if f_type and (r.get("type", "").strip().lower() != f_type):
            return False
        if f_status and (r.get("status", "").strip().lower() != f_status):
            return False
        if (f_from or f_to) and (not _row_date_ok(r.get("start_date", ""))):
            return False
        return True

    if f_type or f_status or f_from or f_to:
        rows = [r for r in rows if _row_ok(r)]
    # Sort newest first
    def _key(row: Dict[str, Any]) -> datetime:
        """
        Sort key for the flat request list.
        We parse the row's `start_date` as YYYY-MM-DD; if anything goes wrong,
        we return `datetime.min` so malformed/missing dates sink to the bottom
        when we `reverse=True` (newest first) below.
        """
        try:
            return datetime.strptime(row["start_date"], "%Y-%m-%d")
        except Exception:
            # Defensive fallback: keep bad data visible but at the end
            return datetime.min

    rows.sort(key=_key, reverse=True)

    return render_template(
        "my_requests.html",
        # Summary card fields:
        vacation_used_ytd=round(vacation_used_ytd, 2),
        vacation_left=round(vacation_left, 2),
        sick_used_ytd=round(sick_used_ytd, 2),
        sick_left=round(sick_left, 2),
        current_year=current_year,
        # Flat list for the table:
        current_date=current_date,
        rows=rows,
        # Echo filters back for form stickiness
        q_type=f_type,
        q_status=f_status,
        q_date_from=(request.args.get("date_from") or ""),
        q_date_to=(request.args.get("date_to") or ""),
    )


 
@app.route("/tow-log", methods=["GET", "POST"], endpoint="tow_log")
def tow_log():
    """
    Public Tow Log submission page for tow operators.
    Expected form fields (template):
      - company_id (required, integer; must exist & be active in allow-list)
      - location (required)
      - time_iso (optional; default now if blank)
      - make, model (optional)
      - tag (required; 'NO TAG' accepted; stored uppercase)
      - vin (optional; max 17)
      - reason (optional; <=300 chars)
      - website (honeypot; if filled, ignore submission)
    """
    if request.method == "POST":
        # CSRF (presence check consistent with app pattern)
        _ = request.form.get("_csrf")

        # Honeypot for bots
        if (request.form.get("website") or "").strip():
            flash("Submission ignored.", "warning")
            return render_template("tow_log.html")

        # ---- Extract & normalize ----
        company_id_raw = (request.form.get("company_id") or "").strip()
        location = (request.form.get("location") or "").strip()
        time_iso = (request.form.get("time_iso") or "").strip()
        make = (request.form.get("make") or "").strip()
        model = (request.form.get("model") or "").strip()
        tag_raw = (request.form.get("tag") or "").strip()
        state_raw = (request.form.get("state") or "").strip()
        vin = (request.form.get("vin") or "").strip()
        reason = (request.form.get("reason") or "").strip()

        # Required: company_id must be integer and in allow-list (active)
        try:
            company_id_int = int(company_id_raw)
        except (TypeError, ValueError):
            flash("Company ID must be a number.", "error")
            return render_template("tow_log.html")
        company_id = str(company_id_int)

        companies = load_tow_companies()
        company_info = companies.get(company_id)
        if not company_info or not company_info.get("active", False):
            flash("Company ID not recognized or inactive. Contact admin.", "error")
            return render_template("tow_log.html")
        company_name = str(company_info.get("name", "")).strip()

        # Required: location
        if not location:
            flash("Please enter the location of the tow.", "error")
            return render_template("tow_log.html")

        # Required: tag (accept 'NO TAG')
        if not tag_raw:
            flash("Please enter the tag (or 'NO TAG' if none).", "error")
            return render_template("tow_log.html")
        tag = "NO TAG" if tag_raw.strip().lower() == "no tag" else tag_raw.strip().upper()
        # Optional: validate two-letter US state abbreviation (blank allowed)
        state = state_raw.upper()
        if state:
            _states = {"AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"}
            if state not in _states:
                flash("Please select a valid state abbreviation.", "error")
                return render_template("tow_log.html")

        # time: default to now if blank
        if not time_iso:
            time_iso = datetime.now().isoformat(timespec="minutes")

        # vin: trim and hard limit (defensive; template already has maxlength)
        if len(vin) > 17:
            vin = vin[:17]

        # reason: cap at 300
        if len(reason) > 300:
            flash("Reason must be 300 characters or fewer.", "error")
            return render_template("tow_log.html")

        # ---- Append clean record ----
        payload = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            "company_id": company_id,
            "company_name": company_name,
            "location": location,
            "time_iso": time_iso,
            "make": make,
            "model": model,
            "tag": tag,
            "vin": vin,
            "reason": reason,
            "state": state,
        }
        entries = load_tow_log()
        entries.append(payload)
        save_tow_log(entries)
        flash("Tow entry submitted. Thank you.", "success")

    return render_template("tow_log.html")
@app.route("/")
def home():
    return "Hello, world!"


@app.route("/landing")
def landing():
    username = get_current_username()
    if not username:
        return redirect(url_for("login"))
    all_users = load_users()
    user = all_users.get(username)
    if not user:
        return redirect(url_for("login"))
    return render_template("landing.html", user=user)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        all_users = load_users()
        user = all_users.get(username)
        if user:
            # Secure password verification (hashed or legacy plaintext with one-time migration)
            try:
                from werkzeug.security import check_password_hash, generate_password_hash
            except Exception:
                check_password_hash = None
                generate_password_hash = None

            ok = False
            ph = user.get("password_hash")
            if ph and check_password_hash:
                # Preferred path: verify against hashed password
                ok = check_password_hash(ph, password)
            else:
                # Legacy path: compare plaintext, then migrate to hash on success
                ok = (user.get("password") == password)
                if ok and generate_password_hash:
                    user["password_hash"] = generate_password_hash(password)
                    user.pop("password", None)
                    # Persist migration immediately
                    all_users[username] = user
                    try:
                        save_users_atomic(all_users)  # prefer atomic if available
                    except NameError:
                        try:
                            save_users(all_users)       # fallback to non-atomic saver if defined
                        except NameError:
                            import os, json
                            os.makedirs('data', exist_ok=True)
                            with open(os.path.join('data','users.json'), 'w') as f:
                                json.dump(all_users, f, indent=2)
                    # Optional: audit password migration
                    (user.setdefault("audit", [])).append({
                        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                        "actor": username,
                        "action": "password_migrated",
                        "details": {"method": "on_login"}
                    })
            # deny if password invalid
            if not ok:
                return "Invalid username or password", 401
            # NEW: archived accounts cannot log in
            if not is_user_active(user):
                return "Account is inactive. Contact an admin.", 403
            session["username"] = username
            # Normalize and enforce role from user record (no accidental elevation)
            role = (user.get("role") or "user")
            role = role.strip().lower()
            if role not in ("user", "supervisor", "admin", "webmaster"):
                role = "user"
            session["role"] = role
            return redirect(url_for("landing"))
        else:
            return "Invalid username or password", 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


 
# =========================
# Time Off
# =========================
@app.route("/request-time-off")
def request_time_off():
    """
    Render the time-off request form.

    Fixes:
    - Do NOT depend on a `?confirm=` query param for duplicate confirmation.
      We rely on session flags set by /submit-request, then immediately
      clear those flags so refresh doesn't re-trigger the confirm.
    - Always pass balances + today's date to the template.
    """
    # --- Auth guard ---
    if "username" not in session:
        return redirect(url_for("login"))

    # --- Balances for header card ---
    username = session.get("username")
    all_users = load_users()
    u = all_users.get(username, {})
    vacation_left = float(u.get("vacation_left", 0) or 0)
    sick_left = float(u.get("sick_left", 0) or 0)

    # --- Today for min= and default value ---
    current_date = date.today().isoformat()

    # --- Read once from session (set by /submit-request) ---
    confirm_needed = bool(session.get("confirm_needed")) and bool(session.get("resubmit_payload"))
    resubmit_payload = session.get("resubmit_payload")
    duplicate_dates = session.get("duplicate_dates") or []

    # --- IMPORTANT: clear the flags so refresh doesn’t re-prompt ---
    session.pop("confirm_needed", None)
    session.pop("duplicate_dates", None)
    # NOTE: we intentionally keep resubmit_payload until the JS decides:
    # - If user confirms: we POST the hidden form (payload consumed there).
    # - If user cancels: we redirect back here without flags and nothing happens.
    #   To avoid it lingering forever, the next non-confirm load clears it:
    if not confirm_needed:
        session.pop("resubmit_payload", None)

    return render_template(
        "request_time_off.html",
        vacation_left=vacation_left,
        sick_left=sick_left,
        current_date=current_date,
        confirm_needed=confirm_needed,
        resubmit_payload=resubmit_payload,
        duplicate_dates=duplicate_dates,
    )

# ...

@app.route("/submit-request", methods=["POST"])
def submit_request():
    """
    Create a time-off request (vacation or sick).

    Flow:
    - Parse: type, start, end (optional), range_mode, hours, note, force.
    - Build list of dates (single or inclusive range).
    - If not 'force', check duplicates vs. history + pending:
        * If duplicates exist, stash original payload and duplicate dates
          in session, set a 'confirm_needed' flag, and redirect back to
          /request-time-off where a native confirm() will be shown.
    - If 'force' or no duplicates:
        * For each date:
            - Log an entry (vacation=pending, sick=logged).
            - For sick: deduct immediately and mark status_log for that day.
        * Save users + status_log.
        * Flash success and redirect to landing.
    """
    # --- Auth guard ---
    username = get_current_username()
    if not username:
        return redirect(url_for("login"))

    # --- Load user (balances source) ---
    all_users = load_users()
    user = all_users.get(username)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    # --- Extract fields as submitted ---
    request_type = (request.form.get("type") or "").strip().lower()   # vacation|sick
    date_str     = (request.form.get("date") or "").strip()           # start date
    end_date_str = (request.form.get("end_date") or "").strip()       # optional end
    range_mode   = (request.form.get("range_mode") or "single").lower()
    note         = request.form.get("note", "")
    hours_raw    = (request.form.get("hours") or "").strip()
    force        = bool(request.form.get("force"))  # set when user confirms duplicate override

    # --- Basic validation ---
    if request_type not in {"vacation", "sick"}:
        flash("Invalid request type.", "error")
        return redirect(url_for("request_time_off"))

    try:
        hours = float(hours_raw)
    except (TypeError, ValueError):
        flash("Hours must be a valid number.", "error")
        return redirect(url_for("request_time_off"))

    if not (0 < hours <= 24):
        flash("Hours must be between 0 and 24.", "error")
        return redirect(url_for("request_time_off"))

    # --- Build the list of requested dates (single or inclusive range) ---
    def build_requested_dates() -> List[str]:
        if range_mode == "multi" and end_date_str:
            try:
                start_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                end_dt   = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                if end_dt < start_dt:
                    end_dt = start_dt  # clamp accidental inversions
                cur = start_dt
                out: List[str] = []
                while cur <= end_dt:
                    out.append(cur.strftime("%Y-%m-%d"))
                    cur += timedelta(days=1)
                return out
            except Exception:
                return [date_str] if date_str else []
        return [date_str] if date_str else []

    requested_dates = build_requested_dates()
    if not requested_dates:
        flash("Please select a valid date.", "error")
        return redirect(url_for("request_time_off"))

    # --- Duplicate detection (only when not forced) ---
    if not force:
        try:
            log_data = get_request_log()  # immutable audit/history
        except Exception:
            log_data = []

        pending_list = getattr(requests_data, "requests", [])

        # Collect all dates already on file for this user
        existing_dates = set()
        for e in log_data:
            if e.get("user") == username:
                d = e.get("date")
                if d:
                    existing_dates.add(d)
        for r in pending_list:
            if r.get("user") == username:
                d = r.get("date")
                if d:
                    existing_dates.add(d)

        dup_dates = [d for d in requested_dates if d in existing_dates]
        if dup_dates:
            # Preserve exact original input so the confirm re‑POST matches intent
            session["resubmit_payload"] = {
                "type": request_type,
                "date": date_str,
                "hours": hours_raw,
                "note": note,
            }
            if range_mode == "multi" and end_date_str:
                session["resubmit_payload"]["end_date"] = end_date_str
                session["resubmit_payload"]["range_mode"] = "multi"

            session["confirm_needed"] = True
            session["duplicate_dates"] = dup_dates
            return redirect(url_for("request_time_off"))  # UI will prompt

    # --- If here: no duplicates OR user confirmed override ('force') ---
    if request_type == "sick":
        # Sick must have enough hours for *all* selected days
        total_needed = hours * len(requested_dates)
        if total_needed > float(user.get("sick_left", 0) or 0):
            flash("Not enough sick hours remaining for the selected dates.", "error")
            return redirect(url_for("request_time_off"))

    # --- Persist per day ---
    display_name = f"{user.get('last_name','')} ({user.get('rank','')})"
    status_log = load_status_log()
    created_count = 0

    for d in requested_dates:
        status = "pending" if request_type == "vacation" else "logged"

        # Immutable log append (source of truth for history)
        log_entry = {
            "user": username,
            "name": f"{user.get('first_name','')} {user.get('last_name','')}",
            "call_sign": user.get("call_sign", ""),
            "sector": user.get("sector", ""),
            "date": d,
            "hours": hours,
            "status": status,
            "handled_by": display_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": request_type,
            "note": note,
        }
        log_request(log_entry)

        if request_type == "vacation":
            # Vacation is pending approval; balance is adjusted on approval only
            requests_data.requests.append({
                "user": username,
                "date": d,
                "type": "vacation",
                "hours": hours,
                "note": note,
                "status": "pending",
                "handled_by": "",
            })
        else:
            # Sick: deduct immediately and mark the status for that day
            user["sick_left"] = float(user.get("sick_left", 0) or 0) - hours
            user["sick_used_ytd"] = float(user.get("sick_used_ytd", 0) or 0) + hours
            status_log.setdefault(d, {})[username] = "Sick"

        created_count += 1

    # Save changes (users + per‑day status overrides)
    save_users(all_users)
    save_status_log(status_log)

    # --- Success toasts ---
    if request_type == "vacation":
        flash(
            "Vacation request submitted and is pending approval." if created_count == 1
            else f"Vacation requests submitted for {created_count} days — pending approval.",
            "success",
        )
    else:
        flash(
            "Sick time logged and deducted." if created_count == 1
            else f"Sick time logged for {created_count} days and deducted.",
            "success",
        )

    # --- NCCPD AUDIT: record submission summary ---
    submitted_summary = {
        "type": request_type,
        "dates": requested_dates,
        "hours_per_day": hours,
        "note": note,
        "mode": range_mode,
        "status_effect": ("pending" if request_type == "vacation" else "logged"),
    }
    audit_append(all_users, username, "request_submit", submitted_summary, save_immediately=True)

    return redirect(url_for("landing"))

 
# =========================
# Admin
# =========================
@app.route("/admin/requests")
@require_role("admin", "webmaster")
def admin_requests():
    """
    Show pending vacation requests grouped by (user, type, hours_per_day) and
    contiguous date ranges. Actions remain per‑day (approve/deny via /handle-request).
    """
    all_users = load_users()
    pending = list(getattr(requests_data, "requests", []))

    # ---- helpers ------------------------------------------------------------
    def _norm_type(t: Optional[str]) -> str:
        return (t or "").strip().lower()

    def _safe_hours(h: Any) -> float:
        try:
            return float(h)
        except Exception:
            return 0.0

    def _parse_date(s: Optional[str]) -> Optional[date]:
        try:
            return datetime.strptime((s or ""), "%Y-%m-%d").date()
        except Exception:
            return None

    # ---- normalize + filter to pending vacation ----------------------------
    enriched = []
    for r in pending:
        t = _norm_type(r.get("type"))
        if t != "vacation":
            continue  # sick never appears here (it’s logged immediately)
        if (r.get("status") or "pending") != "pending":
            continue

        dt = _parse_date(r.get("date"))
        enriched.append({
            **r,
            "_type": t,
            "_hours": _safe_hours(r.get("hours")),
            "_dt": dt,                  # parsed date object for sorting/contiguity
            "_ds": r.get("date", ""),   # original string
        })

    # Sort for grouping: by user, type, hours/day, then date
    enriched.sort(key=lambda r: (r.get("user",""), r["_type"], r["_hours"], r["_dt"] or date.min))

    # ---- group by (user, type, hours/day) and split into contiguous ranges --
    groups = []
    for (uname, typ, hrs), bucket in igroupby(enriched, key=lambda r: (r.get("user",""), r["_type"], r["_hours"])):
        seq = []          # current contiguous run
        last_dt = None    # last date in current run

        def flush_seq():
            """Emit the current contiguous sequence into groups (stable order)."""
            if not seq:
                return
            # Sort within the run to be safe (inputs were already sorted globally)
            seq_sorted = sorted(seq, key=lambda x: x["_dt"] or date.min)
            start_ds = seq_sorted[0]["_ds"]
            end_ds   = seq_sorted[-1]["_ds"]
            days = [{
                "date": row["_ds"],
                "hours": row["_hours"],
                "note": row.get("note", ""),
                "status": row.get("status", "pending"),
            } for row in seq_sorted]

            u = all_users.get(uname, {}) or {}
            groups.append({
                "user": uname,
                "user_label": f"{u.get('last_name','')}, {u.get('first_name','')}".strip(", "),
                "rank": u.get("rank",""),
                "call_sign": u.get("call_sign",""),
                "sector": u.get("sector",""),
                "type": typ.title(),            # e.g., "Vacation"
                "hours_per_day": float(hrs),
                "start_date": start_ds,
                "end_date": end_ds,
                "days": days,                    # emitted as per‑day actions in the UI
            })

        # Walk the rows for this (user, type, hours) and split on gaps
        for row in bucket:
            dt = row["_dt"]
            if last_dt is None:
                seq = [row]
            else:
                # contiguous if today == last + 1 day
                if dt and last_dt and (dt - last_dt).days == 1:
                    seq.append(row)
                else:
                    flush_seq()
                    seq = [row]
            last_dt = dt
        flush_seq()

    # Render grouped structure; `users` still available if needed by template
    # --- Squad scoping for supervisors (listing) ---
    actor = all_users.get(session.get("username"), {})
    if actor.get("role") == "supervisor":
        actor_squad = actor.get("squad")
        if actor_squad:
            groups = [g for g in groups if (all_users.get(g.get("user"), {}).get("squad") == actor_squad)]
    return render_template("admin_requests.html", groups=groups, users=all_users)



@app.route("/handle-request", methods=["POST"])
@require_role("admin", "webmaster")
def handle_request():
    username = get_current_username()
    try:
        users = load_users()
    except Exception:
        return "Server error", 500

    admin_user = users.get(username)
    if not admin_user or admin_user.get("role") not in ["admin", "webmaster", "supervisor"]:
        return redirect(url_for("login"))

    target = request.form.get("user")
    date_str = request.form.get("date")
    action = request.form.get("action")
    # --- Squad scoping for supervisors (POST guard) ---
    if admin_user.get("role") == "supervisor":
        target_user = users.get(target)
        if not target_user or target_user.get("squad") != admin_user.get("squad"):
            flash("Access denied: supervisors can only act on their own squad.", "error")
            return redirect(url_for("admin_requests"))

    handled = False
    # Find the exact pending vacation request that matches user + date
    for idx, req in enumerate(list(requests_data.requests)):
        if (
            req.get("user") == target
            and req.get("date") == date_str
            and req.get("type") == "vacation"
        ):
            req["status"] = "approved" if action == "approve" else "denied"
            target_user = users.get(target)
            if not target_user:
                return f"Target user {target} not found", 400
            hours = float(req.get("hours", 8))
            if action == "approve":
                target_user["vacation_left"] = target_user.get("vacation_left", 0) - hours  # deduct on approval only
                target_user["vacation_used_today"] = target_user.get(
                    "vacation_used_today", 0
                ) + hours

            # Audit trail: record the decision in the immutable request log
            log_entry = {
                "user": target,
                "name": f"{target_user.get('first_name','')} {target_user.get('last_name','')}",
                "call_sign": target_user.get("call_sign", ""),
                "sector": target_user.get("sector", ""),
                "date": date_str,
                "hours": hours,
                "status": req["status"],
                "handled_by": f"{admin_user.get('last_name','')} ({admin_user.get('rank','')})",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "vacation",
            }
            log_request(log_entry)
            # --- NCCPD AUDIT: record decision for this date ---
            audit_append(
                users,
                target,
                "vacation_decision",
                {
                    "date": date_str,
                    "hours": hours,
                    "decision": req["status"],      # 'approved' or 'denied'
                    "by": admin_user.get("last_name", "") or username,
                },
                save_immediately=True,
            )

            save_users(users)
            del requests_data.requests[idx]
            handled = True
            break

    if not handled:
        flash("No matching pending request found or already handled.")
    return redirect(url_for("admin_requests"))


@app.route("/admin/history")
@require_role("admin", "webmaster")
def admin_history():
    if "username" not in session:
        return redirect(url_for("login"))
    log_data = get_request_log()
    # --- Filters (admin history): date range, user, status, type ---
    q_user = (request.args.get("user") or "").strip()
    q_status = (request.args.get("status") or "").strip().lower()  # approved|denied|logged|cancelled
    q_type = (request.args.get("type") or "").strip().lower()      # vacation|sick
    q_from = _parse_iso_date(request.args.get("date_from"))
    q_to = _parse_iso_date(request.args.get("date_to"))

    def _in_range(ds: str) -> bool:
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            return False
        if q_from and d < q_from:
            return False
        if q_to and d > q_to:
            return False
        return True

    filtered_log = []
    for e in log_data:
        # Only show decisions/logs that are meaningful to admins by default
        st = (e.get("status") or "").lower()
        tp = (e.get("type") or "").lower()
        if q_status and st != q_status:
            continue
        if q_type and tp != q_type:
            continue
        if q_user and (e.get("user") != q_user):
            continue
        ds = e.get("date") or ""
        if (q_from or q_to) and (not _in_range(ds)):
            continue
        filtered_log.append(e)
    # Provide users map and echo filters back to template for form stickiness
    users_map = load_users()
    return render_template(
        "admin_history.html",
        request_log=filtered_log,
        users=users_map,
        q_user=q_user,
        q_status=q_status,
        q_type=q_type,
        q_date_from=(request.args.get("date_from") or ""),
        q_date_to=(request.args.get("date_to") or "")
    )



@app.route("/admin/tow-log", endpoint="admin_tow_log")
@require_role("admin", "webmaster", "supervisor")
def admin_tow_log():
    """Admin/Supervisor review of Tow Log submissions with simple filters."""
    entries = load_tow_log()

    # --- Filters ---
    q_company = (request.args.get("company_id") or "").strip()
    q_from = (request.args.get("date_from") or "").strip()  # YYYY-MM-DD
    q_to = (request.args.get("date_to") or "").strip()      # YYYY-MM-DD

    def _parse_date_ymd(s: str):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    d_from = _parse_date_ymd(q_from) if q_from else None
    d_to = _parse_date_ymd(q_to) if q_to else None

    def _entry_date(e: dict):
        # Prefer time_iso (YYYY-MM-DDTHH:MM) then ts (YYYY-MM-DD HH:MM:SS)
        t = (e.get("time_iso") or "").strip()
        if t:
            try:
                return datetime.fromisoformat(t)
            except Exception:
                pass
        ts = (e.get("ts") or "").strip()
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min

    # Apply filters
    filtered = []
    for e in entries:
        if q_company and str(e.get("company_id", "")).strip() != q_company:
            continue
        if d_from or d_to:
            dtv = _entry_date(e)
            dt = dtv.date() if dtv != datetime.min else None
            if not dt:
                continue
            if d_from and dt < d_from:
                continue
            if d_to and dt > d_to:
                continue
        filtered.append(e)

    # Sort newest-first
    filtered.sort(key=_entry_date, reverse=True)

    return render_template(
        "admin_tow_log.html",
        entries=filtered,
        q_company=q_company,
        q_date_from=q_from,
        q_date_to=q_to,
        total=len(filtered)
    )


@app.route("/admin/sick-history")
@require_role("admin", "webmaster")
def admin_sick_history():
    if "username" not in session:
        return redirect(url_for("login"))
    log_data = get_request_log()
    sick_log = [e for e in log_data if e.get("type") == "sick" and e.get("status") == "logged"]
    return render_template("admin_history.html", request_log=sick_log)


# ---- Helper: append to a user's audit trail ---------------------------------
def _audit_append(user_obj: dict, actor_username: str, action: str, details: dict) -> None:
    """
    Append a structured audit entry to the user's 'audit' list.
    Safe on older records that don't yet have an 'audit' key.
    """
    try:
        audit_list = user_obj.setdefault("audit", [])
        audit_list.append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": actor_username,
            "action": action,
            "details": details,
        })
    except Exception:
        # Never block the flow on audit failure
        pass


# =========================
# Admin: Adjust Vacation Balance (inline from Edit User)
# =========================
# Admin inline balance tool:
# Adjusts balances in place and writes a minimal audit trail (never negative).
@app.route("/admin/adjust-vacation", methods=["POST"], endpoint="adjust_vacation")
@require_role("admin", "webmaster")
def adjust_vacation():
    """
    Adjust a user's vacation_left balance by +/- hours.
    - Form fields: username, direction (add|subtract), hours (float), note (optional)
    - Writes an audit entry with before/after + note.
    - Clamps result to >= 0 (no negative balances).
    - Redirects back to edit_user page with a toast.
    """
    admin_username = get_current_username() or "system"

    target = (request.form.get("username") or "").strip()
    direction = (request.form.get("direction") or "").strip().lower()
    note = (request.form.get("note") or "").strip()

    # Parse hours
    try:
        hours = float(request.form.get("hours", "0") or 0)
    except (TypeError, ValueError):
        hours = 0.0

    if not target:
        flash("Missing target username.", "error")
        return redirect(url_for("landing"))

    if direction not in {"add", "subtract"}:
        flash("Invalid adjustment direction.", "error")
        return redirect(url_for("edit_user", username=target))

    if hours <= 0:
        flash("Hours must be greater than 0.", "error")
        return redirect(url_for("edit_user", username=target))

    # Load and validate target user
    users = load_users()
    u = users.get(target)
    if not u:
        flash(f"User '{target}' not found.", "error")
        return redirect(url_for("landing"))

    before = float(u.get("vacation_left", 0) or 0)
    delta = hours if direction == "add" else -hours
    after = max(0.0, before + delta)  # clamp to >= 0

    u["vacation_left"] = round(after, 2)

    # Audit entry
    _audit_append(
        u,
        actor_username=admin_username,
        action="vacation_balance_adjust",
        details={
            "direction": direction,
            "hours": round(hours, 2),
            "before": round(before, 2),
            "after": round(after, 2),
            "note": note,
        },
    )

    # Persist and toast
    save_users(users)
    verb = "added to" if direction == "add" else "subtracted from"
    flash(f"{hours:.2f} hrs {verb} {target}'s vacation balance.", "success")

    return redirect(url_for("edit_user", username=target))


# =========================
# Admin: Adjust Sick Balance (inline from Edit User)
# =========================
# Admin inline balance tool:
# Adjusts balances in place and writes a minimal audit trail (never negative).
@app.route("/admin/adjust-sick", methods=["POST"], endpoint="adjust_sick")
@require_role("admin", "webmaster")
def adjust_sick():
    """
    Manually adjust a user's Sick balance (Sick Left).
    - Expects: username, op ("add"|"subtract"), delta (float), note (str).
    - Does NOT modify sick_used_ytd. This is a balance correction only.
    - Logs an audit entry: time, actor, action, before/after, delta, note.
    """
    actor = get_current_username() or "system"
    target = (request.form.get("username") or "").strip()
    op = (request.form.get("op") or "add").strip().lower()
    note = (request.form.get("note") or "").strip()

    # Parse delta (hours) safely
    try:
        delta = float(request.form.get("delta") or 0)
    except (TypeError, ValueError):
        flash("Invalid sick hours delta.", "error")
        return redirect(url_for("edit_user", username=target))

    if delta < 0:
        # We expect a positive number; sign is controlled by op
        delta = abs(delta)

    # Compute signed delta
    signed_delta = delta if op == "add" else -delta

    # Load & validate target user
    users = load_users()
    user = users.get(target)
    if not user:
        flash(f"User '{target}' not found.", "error")
        return redirect(url_for("manage_users"))

    before = float(user.get("sick_left", 0) or 0)
    after = round(before + signed_delta, 2)
    # Keep sick_left non-negative (admin can always re-add if needed)
    if after < 0:
        after = 0.0

    # Apply change
    user["sick_left"] = after

    # --- Audit trail append (ensure list exists) ---
    audit_list = user.setdefault("audit", [])
    audit_list.append({
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "actor": actor,
        "action": "adjust_sick",
        "details": {
            "op": op,
            "delta": delta,            # raw input magnitude
            "signed_delta": signed_delta,
            "before": before,
            "after": after,
            "note": note,
        }
    })

    # Persist and toast
    save_users(users)
    flash(f"Sick balance updated for {target}: {before:.2f} → {after:.2f} hrs.", "success")
    return redirect(url_for("edit_user", username=target))


 
# =========================
# Admin: Edit User (single)
# =========================
@app.route("/admin/edit-user/<username>", methods=["GET", "POST"])
@require_role("admin", "webmaster")
def edit_user(username: str):
    all_users = load_users()
    user = all_users.get(username)
    if not user:
        return f"User {username} not found", 404

    numeric_fields = {
        "vacation_left",
        "vacation_used_today",
        "sick_left",
        "sick_used_ytd",
        # NEW:
        "hours_per_day_default",
        "vacation_accrual_rate",
    }
    list_fields = {"skills"}

    if request.method == "POST":
        before = dict(user)  # shallow snapshot before changes
        for field in list(user.keys()):
            if field not in request.form:
                continue
            raw = request.form.get(field, "")

            if field in list_fields:
                user[field] = [s.strip() for s in raw.split(",") if s.strip()]
            elif field in numeric_fields:
                try:
                    user[field] = float(raw) if raw != "" else 0.0
                except ValueError:
                    pass
            else:
                # allow updating is_active too if present in form
                if field == "is_active":
                    user[field] = str(raw).lower() in {"true", "1", "yes", "on"}
                else:
                    user[field] = raw

        # Optional: normalize date for seniority_date (store raw YYYY-MM-DD; validate elsewhere)
        if "seniority_date" in request.form:
            sd_raw = (request.form.get("seniority_date", "") or "").strip()
            user["seniority_date"] = sd_raw

        save_users(all_users)
        # --- NCCPD AUDIT: profile update (only log actual changes) ---
        track_fields = list(user.keys())
        # If you want to ignore noisy keys, you can prune track_fields here.
        changes = diff_fields(before, user, track_fields)
        if changes:
            audit_append(
                all_users,
                username,  # target being edited (route param)
                "profile_update",
                {"fields": changes},
                save_immediately=True,
            )
        flash(
            "Updated %s %s's profile."
            % (user.get("first_name", ""), user.get("last_name", ""))
        )
        return redirect(url_for("edit_user", username=username))

    user_choices = []
    for uname, u in all_users.items():
        label = f"{u.get('last_name','')}, {u.get('first_name','')} — {uname}"
        user_choices.append({"username": uname, "label": label})
    user_choices.sort(key=lambda x: x["label"].lower())

    return render_template(
        "edit_user.html",
        user=user,
        roles=ROLES,
        squads=["", "A", "B", "C", "D"],
        username=username,
        user_choices=user_choices,
    )


 
# =========================
# Admin: Manage Users (bulk)
# =========================
@app.route("/admin/manage-users", methods=["GET", "POST"], endpoint="manage_users")
@require_role("admin", "webmaster")
def manage_users():
    all_users = load_users()

    if request.method == "POST":
        # Apply bulk edits; sanitize inputs and normalize blank/"None" squads
        valid_squads = VALID_SQUADS | {"None", ""}
        for uname, u in all_users.items():
            form_squad = (request.form.get(f"squad[{uname}]", u.get("squad", "")) or "").strip()
            if form_squad not in valid_squads:
                form_squad = "None"
            u["squad"] = "" if form_squad in ("None", "") else form_squad

            u["rank"] = (request.form.get(f"rank[{uname}]", u.get("rank", "")) or "").strip()
            u["call_sign"] = (request.form.get(f"call_sign[{uname}]", u.get("call_sign", "")) or "").strip()
            u["sector"] = (request.form.get(f"sector[{uname}]", u.get("sector", "")) or "").strip()

            form_skills = request.form.get(f"skills[{uname}]", "")
            u["skills"] = [s.strip() for s in (form_skills or "").split(",") if s.strip()]

            # NEW: allow bulk toggle of is_active if you add checkboxes in the template (optional)
            active_val = request.form.get(f"is_active[{uname}]")
            if active_val is not None:
                u["is_active"] = active_val in {"on", "true", "1", "yes"}

        save_users(all_users)
        flash("User updates saved.", "success")
        return redirect(url_for("manage_users"))

    # NEW: hide archived by default, expose with ?show=all
    show_archived = request.args.get("show") == "all"
    users_list = []
    for uname, u in all_users.items():
        if not show_archived and not is_user_active(u):
            continue
        users_list.append({"username": uname, **u})

    users_list.sort(key=lambda x: (x.get("squad", "~"), x.get("last_name", ""), x.get("first_name", "")))
    supervisor_ranks = list(SUPERVISOR_RANKS)
    return render_template(
        "manage_users.html",
        users=users_list,
        supervisor_ranks=supervisor_ranks,
        show_archived=show_archived,
    )

# =========================
# Admin: Vacation Bidding (open/close per officer)
# =========================
@app.route("/admin/vacation-bidding", methods=["GET"])
@require_role("admin", "webmaster")
def vacation_bidding():
    """
    Admin view to manage vacation bidding windows per officer.
    - Filters by squad (?squad=A|B|C|D; default A)
    - Sorts officers by seniority_date ascending (earliest = most senior)
    - Displays whether each officer's bidding window is open/closed
    """
    # Load users
    all_users = load_users()

    # Resolve selected squad (default to 'A' if missing/invalid)
    selected_squad = (request.args.get("squad") or "A").strip().upper()
    if selected_squad not in {"A", "B", "C", "D"}:
        selected_squad = "A"

    # Build a lightweight view model per officer with safe defaults
    officers = []
    for uname, u in all_users.items():
        # Only active users in this squad
        if u.get("squad") != selected_squad or (u.get("is_active", True) is False):
            continue

        # Ensure fields exist with safe defaults
        u.setdefault("first_name", "")
        u.setdefault("last_name", "")
        u.setdefault("rank", "")
        u.setdefault("call_sign", "")
        u.setdefault("seniority_date", None)     # "YYYY-MM-DD" or None
        u.setdefault("bidding_open", False)      # bool flag
        u.setdefault("audit", [])                # list of audit entries
        u["username"] = uname                    # convenience for the template

        officers.append(u)

    # Sort by earliest seniority date, then last/first name for stability
    def _seniority_key(u):
        sd = u.get("seniority_date") or ""
        try:
            # Parse YYYY-MM-DD; pad invalid/empty as far future so they sort last
            dt = datetime.strptime(sd, "%Y-%m-%d")
        except Exception:
            dt = datetime.max
        return (dt, u.get("last_name", ""), u.get("first_name", ""))

    officers.sort(key=_seniority_key)

    # Count open windows
    total_open = sum(1 for u in officers if u.get("bidding_open"))

    return render_template(
        "admin_vacation_bidding.html",
        squads=["A", "B", "C", "D"],
        selected_squad=selected_squad,
        officers=officers,
        total_open=total_open,
    )


@app.route("/admin/vacation-bidding/toggle", methods=["POST"], endpoint="toggle_bidding")
@require_role("admin", "webmaster")
def toggle_bidding():
    """
    POST handler to open/close an officer's bidding window.
    Form inputs:
      - username: target officer username
      - action: 'open' or 'close'
      - squad: to return user to same filtered view
    Side effects:
      - set user['bidding_open'] True/False
      - append audit entry into user['audit']
      - flash a success message
    """
    target = (request.form.get("username") or "").strip()
    action = (request.form.get("action") or "").strip().lower()
    selected_squad = (request.form.get("squad") or "A").strip().upper()

    all_users = load_users()
    user = all_users.get(target)
    if not user:
        flash(f"User '{target}' not found.", "error")
        return redirect(url_for("vacation_bidding", squad=selected_squad))

    # Normalize fields
    if "bidding_open" not in user:
        user["bidding_open"] = False
    if "audit" not in user:
        user["audit"] = []

    # Flip the flag based on action
    if action == "open":
        user["bidding_open"] = True
        verb = "opened"
    elif action == "close":
        user["bidding_open"] = False
        verb = "closed"
    else:
        flash("Invalid action.", "error")
        return redirect(url_for("vacation_bidding", squad=selected_squad))

    # Record audit entry
    admin_uname = get_current_username() or "system"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user["audit"].append(
        {
            "timestamp": ts,
            "actor": admin_uname,
            "action": f"bidding_{verb}",
            "details": {"from_ui": "vacation-bidding", "new_state": user["bidding_open"]},
        }
    )

    save_users(all_users)
    flash(f"Bidding {verb} for {user.get('last_name','')}, {user.get('first_name','')}.", "success")
    return redirect(url_for("vacation_bidding", squad=selected_squad))


# =========================
# NCCPD ONLY: Training Day (Supervisor Tab Placeholder)
# =========================
@app.route("/admin/training-day", methods=["GET", "POST"], endpoint="training_day_create")
@require_role("supervisor", "admin", "webmaster")
def training_day_create():
    """
    NCCPD ONLY: Render the Create a Training Day form.
    - Supervisors: see only officers in their own squad.
    - Admin/Webmaster: see all active users.
    """
    all_users = load_users()
    actor = all_users.get(session.get("username"), {})
    role = actor.get("role")
    actor_squad = actor.get("squad")

    def _is_active(u: dict) -> bool:
        return bool(u.get("is_active", True))

    def _same_squad(u: dict) -> bool:
        return u.get("squad") == actor_squad

    # Build officer list based on role
    if role == "supervisor" and actor_squad:
        pool = {k: v for k, v in all_users.items() if _is_active(v) and _same_squad(v)}
    else:
        pool = {k: v for k, v in all_users.items() if _is_active(v)}

    officers = [
        {
            "username": uname,
            "name": f"{u.get('last_name','')}, {u.get('first_name','')}",
            "squad": u.get("squad"),
        }
        for uname, u in pool.items()
    ]
    officers.sort(key=lambda x: x["name"].lower())

    # --- POST: create training day entries (single date, multiple officers) ---
    if request.method == "POST":
        sel_officers = request.form.getlist("officers")
        date_str = (request.form.get("date") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        # Basic validation
        day = _parse_iso_date(date_str)
        if not day:
            flash("Please provide a valid date.", "error")
            return redirect(url_for("training_day_create"))
        if not sel_officers:
            flash("Please select at least one officer.", "error")
            return redirect(url_for("training_day_create"))

        # Enforce squad scope for supervisors via `pool`
        allowed = set((pool or {}).keys())
        filtered = [u for u in sel_officers if u in allowed] if allowed else sel_officers
        filtered_out = [u for u in sel_officers if u not in allowed] if allowed else []
        if not filtered:
            flash("No valid officers selected for your scope.", "error")
            return redirect(url_for("training_day_create"))

        # Load existing entries; de-duplicate by (date, officer)
        entries = load_training_days()
        existing_keys = { (e.get("date"), e.get("officer")) for e in entries }

        created = 0
        skipped = 0
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        actor_uname = session.get("username") or "system"

        for uname in filtered:
            key = (date_str, uname)
            if key in existing_keys:
                skipped += 1
                continue
            target = all_users.get(uname, {})
            entry = {
                "id": f"td_{date_str}_{uname}",
                "date": date_str,
                "officer": uname,
                "notes": notes,
                "squad": target.get("squad"),
                "created_by": actor_uname,
                "created_at": ts,
                "updated_at": ts,
            }
            entries.append(entry)
            existing_keys.add(key)
            created += 1

            # Audit on the target user's record (best-effort)
            try:
                audit_append(all_users, uname, "training_day_create", {
                    "date": date_str,
                    "notes": notes,
                    "squad": target.get("squad"),
                }, save_immediately=False)
            except Exception:
                pass

        save_training_days(entries)
        save_users(all_users)

        msg = f"Created {created} training assignment(s)."
        if skipped:
            msg += f" Skipped {skipped} duplicate(s)."
        if filtered_out:
            msg += f" Ignored {len(filtered_out)} out-of-scope selection(s)."
        flash(msg, "success" if created else "warning")
        return redirect(url_for("training_day_create"))

    # Load existing entries & apply squad scoping for display
    entries = load_training_days()

    def _by_date_desc_then_name(e):
        ds = e.get("date", "")
        try:
            k1 = datetime.strptime(ds, "%Y-%m-%d")
        except Exception:
            k1 = datetime.min
        u = all_users.get(e.get("officer"), {})
        lname = (u.get("last_name", "") or "").lower()
        fname = (u.get("first_name", "") or "").lower()
        return (-k1.timestamp(), lname, fname)

    if role == "supervisor" and actor_squad:
        entries = [e for e in entries if e.get("squad") == actor_squad]
    try:
        entries = sorted(entries, key=_by_date_desc_then_name)
    except Exception:
        pass
                # --- POST: create training day entries (one date, multiple officers) ---
    if request.method == "POST":
        sel_officers = request.form.getlist("officers")
        date_str = (request.form.get("date") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        # Basic validation
        day = _parse_iso_date(date_str)
        if not day:
            flash("Please provide a valid date.", "error")
            return redirect(url_for("training_day_create"))
        if not sel_officers:
            flash("Please select at least one officer.", "error")
            return redirect(url_for("training_day_create"))

        # Build allowed officer pool based on role/squad
        entries = load_training_days()
        allowed_usernames = set()
        all_users = load_users()
        if role == "supervisor" and actor_squad:
            for uname, u in all_users.items():
                if u.get("is_active", True) and u.get("squad") == actor_squad:
                    allowed_usernames.add(uname)
        else:
            for uname, u in all_users.items():
                if u.get("is_active", True):
                    allowed_usernames.add(uname)

        filtered = [u for u in sel_officers if u in allowed_usernames]
        filtered_out = [u for u in sel_officers if u not in allowed_usernames]
        if not filtered:
            flash("No valid officers selected for your scope.", "error")
            return redirect(url_for("training_day_create"))

        existing_keys = {(e.get("date"), e.get("officer")) for e in entries}
        created = 0
        skipped = 0
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        actor_uname = session.get("username") or "system"

        for uname in filtered:
            key = (date_str, uname)
            if key in existing_keys:
                skipped += 1
                continue
            target = all_users.get(uname, {})
            entry = {
                "id": f"td_{date_str}_{uname}",
                "date": date_str,
                "officer": uname,
                "notes": notes,
                "squad": target.get("squad"),
                "created_by": actor_uname,
                "created_at": ts,
                "updated_at": ts,
            }
            entries.append(entry)
            existing_keys.add(key)
            created += 1

            # Best-effort audit append on the officer record
            try:
                audit_append(all_users, uname, "training_day_create", {
                    "date": date_str,
                    "notes": notes,
                    "squad": target.get("squad"),
                }, save_immediately=False)
            except Exception:
                pass

        save_training_days(entries)
        save_users(all_users)

        msg = f"Created {created} training assignment(s)."
        if skipped:
            msg += f" Skipped {skipped} duplicate(s)."
        if filtered_out:
            msg += f" Ignored {len(filtered_out)} out-of-scope selection(s)."
        flash(msg, "success" if created else "warning")
        return redirect(url_for("training_day_create"))

    # --- Load entries for display (squad-scoped for supervisors) ---
    entries = load_training_days()
    try:
        def _key_sort(e):
            ds = e.get("date", "")
            try:
                t = datetime.strptime(ds, "%Y-%m-%d").timestamp()
            except Exception:
                t = 0
            # last, first for stable officer ordering
            u = all_users.get(e.get("officer"), {}) if 'all_users' in locals() else load_users().get(e.get("officer"), {})
            name_key = f"{u.get('last_name','')},{u.get('first_name','')}".lower()
            return (-t, name_key)
        if role == "supervisor" and actor_squad:
            entries = [e for e in entries if e.get("squad") == actor_squad]
        entries = sorted(entries, key=_key_sort)
    except Exception:
        pass
    
    return render_template(
        "training_day_create.html",
        officers=officers,
        actor=actor,
        actor_squad=actor_squad,
        role=role,
        entries=entries,
    )


# =========================
# Delete Training Day Entry
# =========================
@app.route("/admin/training-day/delete", methods=["POST"], endpoint="training_day_delete")
@require_role("supervisor", "admin", "webmaster")
def training_day_delete():
    """Delete a single training day entry by id (or by date+officer fallback)."""
    all_users = load_users()
    actor = all_users.get(session.get("username"), {})
    role = actor.get("role")
    actor_squad = actor.get("squad")

    entry_id = (request.form.get("id") or "").strip()
    date_str = (request.form.get("date") or "").strip()
    officer = (request.form.get("officer") or "").strip()

    entries = load_training_days()

    def _matches(e):
        if entry_id:
            return e.get("id") == entry_id
        return (e.get("date") == date_str) and (e.get("officer") == officer)

    # Enforce supervisor scope
    def _allowed(e):
        if role == "supervisor" and actor_squad:
            return e.get("squad") == actor_squad
        return True

    new_entries = []
    removed = 0
    for e in entries:
        if _matches(e) and _allowed(e):
            removed += 1
            continue
        new_entries.append(e)

    if removed:
        save_training_days(new_entries)
        flash(f"Deleted {removed} training assignment(s).", "success")
    else:
        flash("No matching entry found or not permitted.", "warning")

    return redirect(url_for("training_day_create"))

# =========================
# NCCPD ONLY: Admin button to run accrual
# =========================
@app.route("/admin/nccpd-accrual", methods=["POST"])
@require_role("admin", "webmaster")
def nccpd_run_accrual():
    """
    NCCPD ONLY:
    One-click admin action that:
      - Computes entitlement for the current calendar year per user.
      - Applies carryover cap and min-use.
      - Updates balances and writes an audit entry.
      - Flags 'nccpd_supervisor_alert' if user exceeded 560 without approval.
    """
    # Which year to credit? We use 'today.year' so it can be run any time.
    # If you want to simulate Jan 1 of next year, change to today.year + 1.
    today = datetime.today()
    target_year = today.year

    users = load_users()
    total = 0
    flagged = 0  # supervisor alerts set
    for uname, u in users.items():
        # Skip archived users
        if u.get("is_active", True) is False:
            continue

        # Run accrual for this user
        before_flag = bool(u.get("nccpd_supervisor_alert", False))
        nccpd_accrual_for_user(u, target_year)
        after_flag = bool(u.get("nccpd_supervisor_alert", False))

        if after_flag and not before_flag:
            flagged += 1
        elif after_flag and before_flag:
            flagged += 1  # still flagged is fine to count

        total += 1

    save_users(users)

    if flagged > 0:
        flash(f"NCCPD accrual complete for {total} users. {flagged} require supervisor attention (over-cap).", "warning")
    else:
        flash(f"NCCPD accrual complete for {total} users.", "success")

    return redirect(url_for("landing"))
# =========================


# User‑initiated cancellation: removes from pending queue and appends a
# 'cancelled' event to the immutable request log for history/audit.
@app.route("/cancel-request", methods=["POST"])
def cancel_request():
    """
    Allow the logged-in user to cancel ONE pending vacation request for a given date.
    - Only affects pending vacation requests in `requests_data.requests`.
    - Appends a 'cancelled' entry to the request log for audit/history.
    - Does NOT modify balances (nothing was deducted yet).
    """
    username = get_current_username()
    if not username:
        return redirect(url_for("login"))

    date_str = (request.form.get("date") or "").strip()

    # Find the matching pending vacation request for this user/date
    found_idx = None
    hours_val = 0.0
    note_val = ""
    for idx, req in enumerate(list(requests_data.requests)):
        if (
            req.get("user") == username
            and (req.get("type") or "").lower() == "vacation"
            and (req.get("status") or "").lower() == "pending"
            and (req.get("date") or "") == date_str
        ):
            found_idx = idx
            try:
                hours_val = float(req.get("hours", 0) or 0)
            except (TypeError, ValueError):
                hours_val = 0.0
            note_val = req.get("note", "")
            break

    if found_idx is None:
        flash("No matching pending vacation request to cancel.", "warning")
        return redirect(url_for("my_requests"))

    # Remove from the pending queue
    del requests_data.requests[found_idx]
    # --- NCCPD AUDIT: user-initiated cancel of a pending request ---
    users = load_users()
    audit_append(
        users,
        username,
        "cancel_request",
        {"date": date_str, "type": "vacation"},
        save_immediately=True,
    )

    # Append a 'cancelled' entry to the log for audit/history
    u = users.get(username, {})
    display_name = f"{u.get('last_name','')} ({u.get('rank','')})"
    log_entry = {
        "user": username,
        "name": f"{u.get('first_name','')} {u.get('last_name','')}",
        "call_sign": u.get("call_sign", ""),
        "sector": u.get("sector", ""),
        "date": date_str,
        "hours": hours_val,
        "status": "cancelled",     # <- key change
        "handled_by": display_name,  # who initiated the action (user display)
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": "vacation",
        "note": note_val,
    }
    log_request(log_entry)

    flash("Vacation request cancelled.", "success")
    return redirect(url_for("my_requests"))

 
# =========================
# NEW: Add / Archive / Unarchive routes

# --- Archive helpers (deep clear to neutralize operational data) ---
ARCHIVE_FIELDS_TO_CLEAR = [
    "call_sign", "sector", "squad", "skills", "start_time", "role",
    "agency", "vacation_accrual_rate", "carryover_caps", "seniority_date"
]
ARCHIVE_NUMERIC_ZERO_FIELDS = [
    "vacation_left", "vacation_used_today", "sick_left", "sick_used_ytd"
]

def _deep_clear_for_archive(u: dict) -> None:
    """Neutralize fields so archived users disappear from operational views."""
    u["is_active"] = False
    # Preserve identity; mark rank for clarity
    u["rank"] = "Archived"
    for key in ARCHIVE_FIELDS_TO_CLEAR:
        if key == "skills":
            u[key] = []
        else:
            u[key] = ""
    for key in ARCHIVE_NUMERIC_ZERO_FIELDS:
        u[key] = 0.0

def _normalize_on_unarchive(u: dict) -> None:
    """Restore minimal sane defaults when bringing a user back."""
    u["is_active"] = True
    if (u.get("rank") or "").lower() == "archived":
        u["rank"] = "Officer"  # safe default; can be edited on the page

@app.route("/admin/archive-user", methods=["POST"], endpoint="archive_user")
@require_role("admin", "webmaster", "supervisor")
def archive_user():
    """
    Archive a user: set is_active=False and deep-clear operational fields.
    Form fields: username
    """
    target = (request.form.get("username") or "").strip()
    if not target:
        flash("Missing target username.", "error")
        return redirect(url_for("landing"))

    users = load_users()
    u = users.get(target)
    if not u:
        flash(f"User '{target}' not found.", "error")
        return redirect(url_for("landing"))

#    _deep_clear_for_arch<truncated__content/>

# =============================================
# =============================================
# CSRF: decorator and token bootstrap
# =============================================
def require_csrf(view_fn):
    """
    Lightweight CSRF decorator used for canary routes.
    - On state-changing methods, compares a submitted token (form field '_csrf'
      or header 'X-CSRF-Token') with the session token.
    - Returns HTTP 400 on mismatch.
    """
    @wraps(view_fn)
    def _wrapped(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            sent = (request.form.get('_csrf') or request.headers.get('X-CSRF-Token') or '').strip()
            want = session.get('csrf_token') or ''
            # Use constant-time compare to avoid timing leaks
            if not sent or not want or not secrets.compare_digest(sent, want):
                return 'CSRF token missing or invalid', 400
        return view_fn(*args, **kwargs)
    return _wrapped


@app.before_request
def _ensure_csrf_token():
    """
    Ensure every session has a CSRF token.
    (Idempotent; safe to call on every request.)
    """
    try:
        if 'csrf_token' not in session:
            # 32 hex bytes = 64 chars; plenty of entropy for a per-session token
            session['csrf_token'] = secrets.token_hex(32)
    except Exception:
        # Never block a request if the session backend hiccups
        pass

# B2: Enable CSRF on a single route as a canary (/admin/set-password)
# We wrap the existing Flask view function after routes are registered, so
# we don't depend on decorator order in the file.
try:
    app.view_functions['set_password'] = require_csrf(app.view_functions['set_password'])
except Exception:
    # If the endpoint name changes, adjust 'set_password' accordingly.
    # Keeping this silent to avoid breaking dev startup; we'll spot issues in tests.
    pass
# =============================================
# Global CSRF guard (allow-list rollout)
# =============================================
# We only enforce on endpoints we KNOW have templates posting a _csrf field.
# Expand this set as we add tokens to more forms, one file at a time.
ENFORCE_CSRF_ENDPOINTS = {
    'set_password',         # Set/Reset Password form
    'archive_user',         # Archive button on Edit User
    'unarchive_user',       # Unarchive button on Edit User
    'adjust_vacation',      # Vacation adjust form on Edit User
    'adjust_sick',          # Sick adjust form on Edit User
    'edit_user',            # Profile Save form on Edit User
    'handle_request',       # Admin approve/deny (admin_requests.html)
    'manage_users',         # Bulk manage users (manage_users.html)
    'toggle_bidding',       # Vacation bidding toggle (admin_vacation_bidding.html)
    'nccpd_run_accrual',    # Run accrual (button posts)
    'cancel_request',       # User cancels pending vacation (my_requests.html)
    'submit_request',       # Time-off submission (request_time_off.html)
    'login',                # Login POST (login.html)
    'rehash_legacy',        # Bulk rehash preview form POST
}

@app.before_request
def _csrf_global_enforcer():
    # Only enforce on state-changing methods
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return

    # Skip if the endpoint isn't in our allow-list yet
    endpoint = (request.endpoint or '').split(':')[-1]  # handle blueprints if any
    if endpoint not in ENFORCE_CSRF_ENDPOINTS:
        return

    # Pull submitted token from form or header and compare to session token
    sent = (request.form.get('_csrf') or request.headers.get('X-CSRF-Token') or '').strip()
    want = session.get('csrf_token') or ''
    if not sent or not want or not secrets.compare_digest(sent, want):
        return 'CSRF token missing or invalid', 400
if __name__ == "__main__":
    app.run(debug=True)
# --- Register blueprints (after routes are attached) -----------------------
# Idempotent: only register if not already registered.
if "tow.admin_tow_log" not in app.view_functions:
    pass  # removed: reverting to plain @app.route
# --- Register blueprints (ensure after all tow routes) ---------------------
# Only register if the endpoint doesn't exist yet (prevents double-reg)
try:
    vf_keys = set(getattr(app, "view_functions", {}).keys())
except Exception:
    vf_keys = set()
if "tow.admin_tow_log" not in vf_keys or "tow.tow_log" not in vf_keys:
    pass  # removed: reverting to plain @app.route

# --- DEBUG: list endpoints (temporary; remove after verifying) -------------
# (Removed: temporary /_debug/routes endpoint)
