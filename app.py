'''
Main Flask application for the Time Tracker App.
Handles user authentication, landing and calendar views,
and submission and administration of time off requests.
'''

### Imports ###
import json
import os
from calendar import monthrange
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    session, flash
)

# Application-specific data modules
from users import users              # Static user data loaded as a dict
from requests_data import requests   # List of pending time off requests
from request_log import request_log  # List of past handled requests

# ------------------------
# Role Definitions
# ------------------------
ROLES = ["user", "supervisor", "admin", "webmaster"] # Define all valid user roles for reference



### Application Setup ###
app = Flask(__name__)
app.secret_key = "supersecretkey"  # Securely sign session cookies


### Utility Functions ###

def load_users():
    """
    Load and return the full users dictionary from users.json.
    """
    with open("users.json") as f:
        return json.load(f)


def load_shifts():
    """
    Load and return the shifts schedule dictionary from shifts.json.
    """
    with open("shifts.json") as f:
        return json.load(f)


### Decorators ###

from functools import wraps

def require_role(*roles):
    """
    Decorator to enforce that a logged-in user has one of the specified roles.
    Redirects to /login if not authenticated,
    or returns 403 if the user lacks a required role.
    """
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapped(*args, **kwargs):
            username = session.get("username")
            if not username:
                return redirect("/login")

            # Reload users to get latest role updates
            try:
                with open("users.json") as f:
                    all_users = json.load(f)
            except Exception as e:
                print("[DEBUG] Failed to load users.json:", e)
                return "Internal server error", 500

            user = all_users.get(username)
            print(f"[DEBUG] session username: {username}")
            print(f"[DEBUG] user object: {user}")

            if not user or user.get("role") not in roles:
                print(f"[DEBUG] Access denied for role: {user.get('role')}")
                return "Access denied", 403

            return view_fn(*args, **kwargs)
        return wrapped
    return decorator
### End Decorators ###


### Yearly Reset Function ###

def reset_sick_usage_if_needed():
    """
    On January 1st, reset sick_used_ytd for all users to 0.
    Ensures yearly sick leave tracking starts fresh.
    """
    today = datetime.today()
    if today.month == 1 and today.day == 1:
        all_users = load_users()
        for user_data in all_users.values():
            user_data["sick_used_ytd"] = 0

        # Persist changes
        with open("users.json", "w") as f:
            json.dump(all_users, f, indent=2)
        print("✅ sick_used_ytd reset for all users.")


### Routes: Calendar Views ###

@app.route("/calendar")
def calendar_view():
    """
    Display a monthly calendar grid annotated with shift assignments.
    Pre-builds empty cells for alignment and injects shift data per date.
    """
    shifts = load_shifts()

    # Hardcoded for now; could be made dynamic
    year, month = 2025, 6
    first_day = datetime(year, month, 1)
    _, num_days = monthrange(year, month)

    # weekday(): 0=Monday; shift so Sunday==0 for display
    start_weekday = (first_day.weekday() + 1) % 7

    calendar_cells = []
    # Leading empty cells for calendar alignment
    for _ in range(start_weekday):
        calendar_cells.append(None)

    # Fill each day cell with date & shift info
    for day in range(1, num_days + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        calendar_cells.append({
            "date": date_str,
            "shifts": shifts.get(date_str, {})
        })

    # Pass grid cells to template
    return render_template(
        "calendar.html",
        cells=calendar_cells,
        year=year,
        month=month
    )


@app.route('/calendar/<date>')
def view_day(date):
    """
    Show detailed shift roster and individual statuses for a given date.
    Builds per-squad listings including computed end times and availability.
    """
    all_users = load_users()
    shifts = load_shifts()
    with open("status_log.json") as f:
        status_log = json.load(f)

    day_data = shifts.get(date, {})
    day_shifts = {squad: day_data.get(squad, "Off") for squad in ['A','B','C','D']}

    # Organize roster by squad
    roster = {squad: [] for squad in day_shifts}
    for uname, udata in all_users.items():
        squad = udata.get("squad")
        shift_type = day_shifts.get(squad)
        if squad and shift_type and shift_type != "Off":
            # Compute end time based on start_time + 11h15m shift length
            try:
                start_dt = datetime.strptime(udata.get("start_time","07:00"), "%H:%M")
                end_time = (start_dt + timedelta(hours=11, minutes=15)).strftime("%H:%M")
            except ValueError:
                end_time = "Unknown"

            user_status = status_log.get(date, {}).get(uname, "Available")
            roster[squad].append({
                **udata,
                "username": uname,
                "shift_type": shift_type,
                "end_time": end_time,
                "status": user_status
            })

    return render_template(
        "day_view.html",
        date=date,
        roster=roster,
        day_shifts=day_shifts
    )


### Routes: Authentication & Landing ###

@app.route("/")
def home():
    """
    Simple health-check route to confirm the app is up.
    """
    return "Hello, world!"


@app.route("/landing")
def landing():
    """
    User landing page; requires login and displays personalized info.
    """
    username = session.get("username")
    if not username:
        return redirect("/login")

    all_users = load_users()
    user = all_users.get(username)
    if not user:
        # Session may be stale or user removed
        return redirect("/login")

    return render_template("landing.html", user=user)


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Handles user login:
    - GET: render login form
    - POST: verify credentials and start session
    """
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        print(f"[DEBUG] login attempt → {username}")

        all_users = load_users()
        user = all_users.get(username)
        if user and user.get("password") == password:
            # Successful login
            session["username"] = username
            session["role"] = user.get("role", "user")
            return redirect("/landing")
        else:
            # Invalid credentials
            return "Invalid username or password", 401

    # Render form for GET
    return render_template("login.html")


### Routes: Time Off Requests ###

@app.route("/request-time-off")
def request_time_off():
    """
    Display the time off request form; user must be authenticated.
    """
    if "username" not in session:
        return redirect("/login")
    return render_template("request_time_off.html")


@app.route("/submit-request", methods=["POST"])
def submit_request():
    """
    Process a submitted time off request:
    - Validate user
    - Deduct hours for sick leave immediately
    - Log vacation requests as pending
    - Update status_log for sick days
    """
    username = session.get("username")
    if not username:
        return redirect("/login")

    # Work on the shared `users` dict imported at module load
    user = users.get(username)
    request_type = request.form.get("type")
    date_str = request.form.get("date")
    hours = float(request.form.get("hours"))
    note = request.form.get("note", "")

    # Prevent sick leave overdraw
    if request_type == "sick" and hours > user.get("sick_left", 0):
        return "Not enough sick hours remaining", 400

    status = "pending" if request_type == "vacation" else "logged"
    new_req = {
        "user": username,
        "date": date_str,
        "type": request_type,
        "hours": hours,
        "note": note,
        "status": status,
        "handled_by": "mbingnear"
    }
    requests.append(new_req)

    # Immediate adjustments for sick leave
    if request_type == "sick":
        user["sick_left"] -= hours
        user["sick_used_ytd"] = user.get("sick_used_ytd", 0) + hours

        # Load or init status log
        try:
            with open("status_log.json") as f:
                status_log = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            status_log = {}

        status_log.setdefault(date_str, {})[username] = "Sick"

        # Persist user data and status log
        with open("users.json", "w") as uf:
            json.dump(users, uf, indent=2)
        with open("status_log.json", "w") as sf:
            json.dump(status_log, sf, indent=2)

    return redirect("/landing")


### Routes: Admin Request Review ###

@app.route("/admin/requests")
@require_role("admin", "webmaster")
def admin_requests():
    """
    Admin overview of all pending requests.
    Accessible only to users with admin/webmaster roles.
    """
    all_users = load_users()
    return render_template(
        "admin_requests.html",
        requests=requests,
        users=all_users
    )


@app.route("/handle-request", methods=["POST"])
def handle_request():
    """
    Approve or deny a specific vacation request,
    update user balances, and log the action.
    """
    # Get the username of the currently logged-in user from the session
    username = session.get("username")

    # Load the most recent version of users.json to ensure accuracy
    try:
        with open("users.json") as f:
            users = json.load(f)
    except Exception as e:
        print("[ERROR] Failed to load users.json:", e)
        return "Server error", 500

    # Get the admin user's data from the freshly loaded user dictionary
    admin_user = users.get(username)

    # If the user is not found or doesn't have the right role, redirect to login
    if not admin_user or admin_user.get("role") not in ["admin", "webmaster"]:
        print(f"[DEBUG] Access denied for user: {username}, role: {admin_user.get('role') if admin_user else 'N/A'}")
        return redirect("/login")

    # Extract the form data submitted by the admin
    target = request.form.get("user")         # Who the request is for
    date_str = request.form.get("date")       # What date the request is for
    action = request.form.get("action")       # 'approve' or 'deny'

    # Loop through all pending requests to find the matching one
    for req in requests:
        if req["user"] == target and req["date"] == date_str and req["type"] == "vacation":
            req["status"] = "approved" if action == "approve" else "denied"

            # Load the affected user's profile
            user = users.get(target)
            if not user:
                return f"Target user {target} not found", 400

            hours = req.get("hours", 8)

            # If approved, subtract vacation hours and track usage
            if action == "approve":
                user["vacation_left"] -= hours
                user["vacation_used_today"] += hours

            # Create a log entry for this action
            log_entry = {
                "user": target,
                "name": f"{user['first_name']} {user['last_name']}",
                "call_sign": user["call_sign"],
                "sector": user["sector"],
                "date": date_str,
                "hours": hours,
                "status": req["status"],
                "handled_by": f"{admin_user['last_name']} ({admin_user['rank']})",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            # Load existing request log if it exists
            if os.path.exists("request_log.json"):
                with open("request_log.json") as lf:
                    log_data = json.load(lf)
            else:
                log_data = []

            # Append the new log entry and save it
            log_data.append(log_entry)
            with open("request_log.json", "w") as lf:
                json.dump(log_data, lf, indent=4)

            # Persist updated user balances
            with open("users.json", "w") as f:
                json.dump(users, f, indent=2)

            break  # Stop after finding and handling the first matching request

    # After processing, return the admin back to the pending requests page
    return redirect("/admin/requests")
### Routes: Admin History ###

@app.route("/admin/history")
@require_role("admin", "webmaster")
def admin_history():
    """
    Display full history of handled requests for auditing.
    """
    if "username" not in session:
        return redirect("/login")

    with open("request_log.json") as lf:
        log_data = json.load(lf)

    return render_template("admin_history.html", request_log=log_data)


### Routes: User Profile Editing ###

@app.route("/admin/edit-user/<username>", methods=["GET", "POST"])
@require_role("admin", "webmaster")
def edit_user(username):
    """
    Allow admins to edit certain profile fields for a user.
    GET  → show the edit form
    POST → update JSON and redirect
    """
    all_users = load_users()
    user = all_users.get(username)
    if not user:
        return f"User {username} not found", 404

    if request.method == "POST":
        # Update editable fields
        user['call_sign'] = request.form.get('call_sign', user['call_sign'])
        user['start_time'] = request.form.get('start_time', user['start_time'])
        user['sector'] = request.form.get('sector', user['sector'])

        # Parse comma-separated skills
        skills_raw = request.form.get('skills', '')
        user['skills'] = [s.strip() for s in skills_raw.split(',') if s.strip()]

        # Persist and notify
        with open('users.json', 'w') as uf:
            json.dump(all_users, uf, indent=2)
        flash(f"Updated {user['first_name']} {user['last_name']}'s profile.")
        return redirect('/landing')

    return render_template('edit_user.html', user=user)
### End of User Profile Editing Routes ###

### Startup Tasks ###

# Reset sick usage if it's New Year's Day
reset_sick_usage_if_needed()

@app.route("/logout")
def logout():
    """
    Logs the user out by clearing session data
    and redirecting to the login page.
    """
    # Clear the session (removes username, role, etc.)
    session.clear()

    # Optional: Flash a message if you’re using Flask's flash system
    # flash("You have been logged out.")

    # Redirect to login page
    return redirect("/login")
### End of Startup Tasks ###


### Application Entry Point ###
if __name__ == '__main__':
    # Launch Flask dev server with debugging enabled
    app.run(debug=True)
