"""Domain constants for ShiftWise scheduling."""

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_DAYS_OFF = 2  # every employee gets at least 2 days off per week

STATION_FRONT = "front"
STATION_BACK = "back"
STATIONS = (STATION_FRONT, STATION_BACK)

ASSIGNMENT_STATUS_PROPOSED = "proposed"
ASSIGNMENT_STATUS_NOTIFIED = "notified"
ASSIGNMENT_STATUS_CONFIRMED = "confirmed"
ASSIGNMENT_STATUS_SWITCH_FIXED = "switch_fixed"
ASSIGNMENT_STATUS_MANAGER_FIXED = "manager_fixed"
ASSIGNMENT_STATUS_SICK = "sick"
ASSIGNMENT_STATUS_SWAP_REQUESTED = "swap_requested"

REQUEST_KIND_DAY_OFF = "day_off"
REQUEST_KIND_VACATION = "vacation"
REQUEST_KIND_SICK = "sick"
REQUEST_KIND_SWAP = "swap"
REQUEST_KIND_SWITCH = "switch"
REQUEST_KIND_MANAGER_UNASSIGN = "manager_unassign"
