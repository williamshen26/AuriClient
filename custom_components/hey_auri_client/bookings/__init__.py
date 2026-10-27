"""Stays from the host's booking calendar (Airbnb's export link, or any iCal link).

calendar.py reads the calendar, schedule.py picks the stay Guest Settings
should hold, and sync.py keeps Guest Settings in step. The first two don't
import Home Assistant, so they can be tested on their own. automatic_mode.py
tells the backend about the stay (context.stay), and guest_name.py keeps the
name the guests give Auri when the booking has none.
"""
