'''
Script: test_reset_sick_ytd.py

Purpose:
    Utility to reset the 'sick_used_ytd' counter for all users in users.json.
    Primarily used to verify the reset functionality works as expected.

Usage:
    Run this script to zero out the yearly sick leave usage for every user.
    This helps in testing the annual reset logic without waiting for January 1st.
'''

import json

### Load current user data ###
with open("users.json", "r") as f:
    users = json.load(f)

### Reset yearly sick usage for all users ###
for username, user_data in users.items():
    # Forcefully set sick_used_ytd to zero for test verification
    user_data["sick_used_ytd"] = 0

### Persist changes ###
with open("users.json", "w") as f:
    # Use indentation for readability
    json.dump(users, f, indent=2)

# Provide feedback to operator
print("✅ All users' 'sick_used_ytd' fields have been reset to 0.")
