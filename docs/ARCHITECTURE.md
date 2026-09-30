# Architecture & System Design
> Status: current · Last verified: 2026-09-29

## 1. System Overview

ShiftWise is an automated employee scheduling and shift-management system tailored for environments with distinct operational divisions (such as Front-of-House and Back-of-House in restaurants and hospitality).

The core operational workflow is:
1. **Manager defines shift slots:** Specifies week start date, weekday, time range, capacity (slots), and house area (`front` or `back`).
2. **Employees rank preferred shifts:** Staff submit 1-to-N ranked preferences for open shifts within their assigned station.
3. **Automated Scheduler assigns shifts:** The algorithm allocates shifts by round and priority lineup, honoring weekly hours caps, minimum days off, and house separation rules.
4. **Self-Service & Coverage:** Staff can file day-off requests, vacation periods, sick calls, and shift swaps. They can opt in or out of covering each shift and directly invite a coworker to exchange assigned shifts; the recipient accepts or declines, and accepted exchanges must satisfy schedule constraints. Coverage planning automatically identifies qualified, willing, same-house replacements.
5. **Manager Overrides:** Managers retain ultimate control, able to manually place staff with sticky assignments (`manager_fixed`) that survive subsequent automated recomputations.

```
       +------------------------------------------------+
       |                  Web Browser                   |
       |  (Employees & Managers via Responsive HTML/CSS)|
       +-----------------------+------------------------+
                               | HTTP
       +-----------------------v------------------------+
       |                     Caddy                      |
       |         (Reverse Proxy, TLS, Headers)          |
       +-----------------------+------------------------+
                               | Proxy (Port 5000)
       +-----------------------v------------------------+
       |                    Gunicorn                    |
       |             (WSGI Multi-worker)                |
       +-----------------------+------------------------+
                               |
       +-----------------------v------------------------+
       |                  Flask App                     |
       |  (Auth, Routes, Scheduler Engine, Coverage)    |
       +-----------------------+------------------------+
                               | SQLite WAL
       +-----------------------v------------------------+
       |                 scheduler.db                   |
       +------------------------------------------------+
```

---

## 2. Front-of-House & Back-of-House Separation

A foundational architectural invariant is the strict separation between Front-of-House (`front`) and Back-of-House (`back`):

- **User Model:** Every employee belongs to a `station` (`front` or `back`).
- **Shift Model:** Every shift belongs to an `area` (`front` or `back`).
- **Isolation Rules:**
  - An employee can only submit picks for shifts where `shift.area == user.station`.
  - The auto-scheduler never assigns an employee across house boundaries.
  - Coverage planning (`coverage_plan`) only searches for candidates within the shift's house area.
  - Switch and swap requests cannot target cross-house shifts.
  - Manager overrides validate that the chosen employee belongs to the shift's house area.

---

## 3. Auto-Scheduler Engine Mechanics

The auto-scheduler (`run_scheduler(week_start)`) assigns shifts across a seven-day week (Mon–Sun) starting on the specified date.

### Priority Lineup

When multiple employees compete for the same shift slot, priority is determined by `priority_key`:
1. **Station Order:** `front` before `back` (shifts are partitioned by house).
2. **Employment Type:** Full-time (`full_time`) takes precedence over part-time (`part_time`).
3. **Seniority:** Earlier hire date (`hired_on`) wins among employees of the same employment type.

$$\text{priority\_key} = (0 \text{ if front else } 1, \ 0 \text{ if full\_time else } 1, \ -\text{seniority\_days})$$

### Invariants & Constraint Checks

The helper `assignment_block_reason(conn, uid, shift)` validates four constraints before assigning an employee:

1. **Availability:** Employee must not have an active absence request (`vacation`, `day_off`) or pending `swap` on that shift.
2. **Weekly Hours Cap:** Total assigned hours plus the new shift's duration must not exceed `users.weekly_hours` (default 40 for full-time). One documented exception: a slot that a *pending vacation* is holding open may be backfilled by an over-cap coverer, so the manager can approve the vacation knowing the cover exists.
3. **Days-Off Rule:** Every employee must receive at least 2 days off per week (`MIN_DAYS_OFF = 2`; max 5 working days). A double shift on the same day counts as 1 working day. The same pending-vacation exception applies.
4. **Time Overlap:** An employee cannot hold two shifts that overlap in time on the same day.

### Round-Based Allocation

1. The scheduler collects all employee picks for the week.
2. In Round 1, each employee attempts to claim their Rank 1 pick.
3. If a slot is contested, the candidate with the highest priority claims it. Displaced candidates fall back to their Rank 2 pick in subsequent rounds.
4. Employees unable to get their preferred picks receive in-app notifications explaining why (e.g., slot filled, hours cap reached, or days-off limit met).

### Rebuild Semantics

When `run_scheduler` executes:
- Existing auto-assignments (`proposed`, `notified`) are cleared and re-evaluated against the latest picks.
- Fixed assignments (`manager_fixed` from manager overrides, `confirmed`, `switch_fixed` from approved switches, `coverage_fixed` from arranged cover, `swap_invited` from a pending employee→coworker swap invite) are preserved and count against the employee's hours and days.
- Pending `swap_requested` and `sick` assignments leave room open for other staff.

---

## 4. Coverage Engine & Absence Management

When an assigned shift is vacated, `coverage_plan(conn, week, out_shift_id, out_uid)` finds the optimal replacement:

1. **Unfilled Pickers First:** Evaluates other employees who ranked this shift but did not receive it, sorted by the priority lineup.
2. **Least-Loaded Backup:** If no unfilled pickers are eligible, falls back to any eligible employee in the same station, prioritizing whoever has the fewest assigned hours.
3. **Notification:** The new coverer receives an in-app assignment notification; if no legal coverer exists, the manager is alerted.

Slots that a **pending vacation** is holding open are the only gaps that may be
backfilled past an employee's hours or days-off limit (`_vacated_by_pending_vacation`
in `shiftwise/scheduler/engine.py`); gaps from sick calls, unfilled picks, or
manager unassignments stay inside the caps. That is the coverage the requests
page flags as "Review proposed coverage".

---

## 5. Request Lifecycle & State Machine

Employees submit 5 types of requests through the web interface:

| Request Kind | Target | Immediate Action | Manager Action | Resulting Status |
|---|---|---|---|---|
| `day_off` | Day of week | Drops shifts on that day; rebuilds schedule | Approve / Deny | `approved_ok` / `denied` |
| `vacation` | Date range | Drops shifts in range; rebuilds schedule | Approve / Deny (blocked while a shift in range is understaffed) | `approved_ok` / `denied` |
| `sick` | Specific shift | Calls `apply_sick()`; finds auto-cover | Informational | `approved` / row marked `sick` |
| `swap` | Specific shift | Calls `coverage_plan()`; releases requester if covered | Informational | `approved_ok` (covered) or `pending` |
| `switch` | Shift A $\rightarrow$ Shift B | Requires manager approval; validates target capacity | Approve / Deny | `approved` / `denied` |

---

## 6. Planned Extensions

The modular architecture is the foundation for these planned capabilities (no implementation yet):

1. **Employee-to-Employee Shift Trade Board:** Employees post open shifts to a peer board; qualified same-house colleagues accept directly, with manager one-click approval.
2. **Pluggable Notification Adapters:** Abstract `notify()` into backends (in-app SQLite, email via SMTP, SMS via Twilio).
3. **iCalendar / Webcal Schedule Feeds:** Signed subscriber URLs (`/schedule/<token>.ics`) so staff can sync published shifts to Google/Apple/Outlook calendars.
4. **Multi-Week Scheduling & Templates:** Manager-saved repeating shift templates with schedules published 2–4 weeks ahead.
