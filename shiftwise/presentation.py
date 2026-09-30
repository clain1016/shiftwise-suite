"""Presentation-only helpers for values stored in canonical database form."""
from datetime import datetime


def format_clock(value, time_format="24h"):
    """Format a stored HH:MM shift time without changing its database value."""
    if time_format != "12h":
        return value
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError):
        return value
    return f"{parsed.hour % 12 or 12}:{parsed.minute:02d} {'AM' if parsed.hour < 12 else 'PM'}"
