'''
Module: request_log.py

Defines the global in-memory log for all handled time-off requests.
Other parts of the application import this list to append audit entries
and render historical request data.
'''

# The request_log list stores dictionaries representing each processed request event.
# Each entry should include keys like: user, date, hours, status, handled_by, timestamp.
request_log = []  # type: list[dict]
