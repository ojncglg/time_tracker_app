'''
Module: requests_data.py

Maintains the in-memory list of pending time-off requests.
Other parts of the application import this list to append new requests
and to render current pending requests for admin review.

Each request should be a dict with keys:
    - user (str): Officer's unique ID
    - date (str, YYYY-MM-DD): Requested date
    - type (str): 'vacation' or 'sick'
    - hours (float): Number of hours requested
    - note (str): Optional note from the user
    - status (str): 'pending' for vacation, 'logged' for sick
    - handled_by (str): Admin ID assigned to handle the request
'''

# The global list of pending requests; append new entries here

requests = []
