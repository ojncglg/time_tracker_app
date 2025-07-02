'''
Utility script to update the users.json file with the correct sick leave fields.
Adds a yearly "sick_used_ytd" counter if missing and removes deprecated daily counters.
'''

import json

### Load existing users from the JSON data store ###
with open("users.json", "r") as f:
    users = json.load(f)


### Update each user record ###
for username, user_data in users.items():
    # Ensure yearly sick usage is tracked (new field)
    if "sick_used_ytd" not in user_data:
        user_data["sick_used_ytd"] = 0  # Initialize to zero for backwards compatibility

    # Clean up deprecated daily sick counter if still present
    if "sick_used_today" in user_data:
        del user_data["sick_used_today"]  # Remove outdated field


### Persist the cleaned and enhanced data back to disk ###
with open("users.json", "w") as f:
    json.dump(users, f, indent=2)

# Inform the operator of successful update
print("✅ All users updated: 'sick_used_ytd' added (if missing) and 'sick_used_today' removed.")
