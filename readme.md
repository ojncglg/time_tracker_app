# Time Tracker App - Developer Guide

## Overview
The Time Tracker App is a web-based scheduling and personnel management system built for law enforcement shift coordination. The system is designed to handle vacation requests, sick leave logging, shift visualization, and admin approvals with built-in user role management.

This project is actively under development and uses Flask (Python) on the backend, with JSON files for data persistence.

---

## Features Implemented

### User Management
- Users are stored in `users.json`
- Each user has fields:
  ```json
  {
    "username": "1234",
    "password": "password123",
    "first_name": "John",
    "last_name": "Doe",
    "rank": "Corporal",
    "squad": "A",
    "call_sign": "34A1",
    "sector": "Patrol",
    "skills": [],
    "start_time": "07:00",
    "role": "user",
    "vacation_left": 80,
    "vacation_used_today": 0,
    "sick_left": 40,
    "sick_used_ytd": 0
  }
  ```
- Roles: `user`, `admin`, `webmaster`

### Authentication
- Login system with session management
- Role-based access control using custom `@require_role` decorator

### Landing Page
- Personalized dashboard based on user role
- Webmaster has full access to all admin routes and management tools

### Time Off Requests
- Vacation and Sick leave request form at `/request-time-off`
- Vacation requests go into `requests` as `"pending"`
- Sick leave is logged immediately and:
  - Deducts from `sick_left`
  - Increments `sick_used_ytd`
  - Updates `status_log.json` with Sick status

### Admin Tools
- `/admin/requests` – View and approve pending vacation requests
- `/admin/history` – View full request history
- `/admin/users` – (Planned) Manage user details
- Requests and approvals are tracked in `request_log.json`

### Calendar View
- `/calendar` shows a monthly calendar with clickable days
- `/calendar/<date>` shows daily schedule including:
  - Shift (Day/Night/Off)
  - Start and end times
  - Current status (Available, Sick, Vacation, etc.) from `status_log.json`

---

## Development Progress Log

### Alpha (✅ Completed)
- Fixed `users.json` structure and ensured consistent fields

### Bravo (✅ Completed)
- Replaced `sick_used_today` with `sick_used_ytd`
- Implemented rolling reset plan (annual)

### Charlie (✅ Completed)
- Improved landing page with role-based navigation

### Delta (✅ Completed)
- Added inline documentation throughout codebase
- Developer chose to use a separate GPT instance to review/comment large files

---

## Next Tasks (Roadmap)

### Echo (Planned)
- Vacation approval workflow with buttons for `approve` / `deny`
- Approved vacation updates `status_log.json`
- Deducts from `vacation_left`

### Foxtrot (Planned)
- Notification and alert system
- Log when users submit requests
- Show pending request counts

### Golf (Planned)
- Mobile redesign using Tailwind CSS or Bootstrap
- Option 3: vertical timeline view for mobile

---

## Running the App (Dev Environment)
1. Activate your virtual environment
2. Run the Flask server:
   ```bash
   python app.py
   ```
3. Visit `http://127.0.0.1:5000` in your browser

---

## Notes
- Be sure to stop the Flask server cleanly to avoid caching issues
- When making changes to `users.json`, ensure proper JSON structure
- Restart the server if changes aren’t reflected

---

## Contact / Maintainer
- Lead Developer: Grigori Lopez-Garcia
- Role: Webmaster

---

We will continue to update this README as features and logic are added.

