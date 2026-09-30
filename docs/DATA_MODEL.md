# Data Model & Storage Schema
> Status: current · Last verified: 2026-09-29

## 1. Storage Configuration

ShiftWise uses SQLite with Write-Ahead Logging (WAL) and foreign keys enabled:
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=15000;
PRAGMA foreign_keys=ON;
```

The database file defaults to `scheduler.db` relative to `app.py`, configurable via the `SHIFTWISE_DB_PATH` environment variable.

---

## 2. Relational Schema

### `users`
Represents staff members and administrative accounts.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique user identifier. |
| `username` | `TEXT` | `UNIQUE NOT NULL` | Login username. |
| `password` | `TEXT` | `NOT NULL` | Password hash (`scrypt` or `pbkdf2`). |
| `name` | `TEXT` | `NOT NULL` | Display name of employee/manager. |
| `role` | `TEXT` | `NOT NULL DEFAULT 'employee'` | Role: `'employee'` or `'manager'`. |
| `weekly_hours` | `INTEGER` | `DEFAULT 40` | Maximum weekly hours cap. |
| `employment_type` | `TEXT` | `NOT NULL DEFAULT 'part_time'` | `'full_time'` or `'part_time'` (influences priority). |
| `hired_on` | `TEXT` | `NULL` | ISO date string (`YYYY-MM-DD`) establishing seniority. |
| `station` | `TEXT` | `NOT NULL DEFAULT 'front'` | House station: `'front'` (FOH) or `'back'` (BOH). |

---

### `shifts`
Represents scheduled work blocks within a specified week.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique shift identifier. |
| `week_start` | `TEXT` | `NOT NULL` | ISO date string of Monday of the schedule week. |
| `day` | `TEXT` | `NOT NULL` | Weekday: `'Mon'`, `'Tue'`, `'Wed'`, `'Thu'`, `'Fri'`, `'Sat'`, `'Sun'`. |
| `start_time` | `TEXT` | `NOT NULL` | Shift start time (`HH:MM`, 24-hour). |
| `end_time` | `TEXT` | `NOT NULL` | Shift end time (`HH:MM`, 24-hour). |
| `slots` | `INTEGER` | `NOT NULL DEFAULT 1` | Number of concurrent workers required. |
| `note` | `TEXT` | `NULL` | Optional description (e.g., "Dinner prep"). |
| `area` | `TEXT` | `NOT NULL DEFAULT 'front'` | Operational area: `'front'` (FOH) or `'back'` (BOH). |

---

### `picks`
Records an employee's ranked preference for a specific shift.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique preference identifier. |
| `user_id` | `INTEGER` | `NOT NULL` | Reference to `users.id`. |
| `shift_id` | `INTEGER` | `NOT NULL` | Reference to `shifts.id`. |
| `rank` | `INTEGER` | `NOT NULL` | Preference rank (`1` = highest choice). |

*Constraint:* `UNIQUE(user_id, shift_id)` guarantees one rank per shift per user.

---

### `coverage_preferences`
Stores the employee's yes/no willingness to cover each shift. Missing rows default to willing.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `user_id` | `INTEGER` | `NOT NULL` | Employee identifier. |
| `shift_id` | `INTEGER` | `NOT NULL` | Shift identifier. |
| `willing` | `INTEGER` | `NOT NULL DEFAULT 1` | `1` = willing to cover; `0` = opt out. |

*Constraint:* `PRIMARY KEY(user_id, shift_id)` keeps one coverage preference per employee and shift.

---

### `assignments`
Connects an employee to a shift slot and tracks its scheduling state.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique assignment identifier. |
| `shift_id` | `INTEGER` | `NOT NULL` | Reference to `shifts.id`. |
| `user_id` | `INTEGER` | `NOT NULL` | Reference to `users.id`. |
| `status` | `TEXT` | `NOT NULL DEFAULT 'proposed'` | Scheduling lifecycle state. |

*Constraint:* `UNIQUE(shift_id, user_id)` guarantees an employee cannot be double-assigned to the same shift.

#### Assignment Statuses
- `'proposed'`: Assigned by the auto-scheduler; subject to displacement or recalculation on rebuild.
- `'notified'`: Finalized automated assignment; notification sent to employee.
- `'confirmed'`: Explicitly confirmed assignment; preserved across auto-rebuilds.
- `'manager_fixed'`: Placed via manual manager override; permanently preserved across rebuilds.
- `'sick'`: Vacated due to sick call; row retained for tracking, slot opened for coverage.
- `'swap_requested'`: Employee requested coverage; slot available for coverage assignment.

---

### `requests`
Tracks employee absence, vacation, sick, swap, and switch requests.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique request identifier. |
| `user_id` | `INTEGER` | `NOT NULL` | Reference to `users.id`. |
| `kind` | `TEXT` | `NOT NULL` | Request category: `day_off`, `vacation`, `sick`, `swap`, `switch`, or `manager_unassign`. |
| `shift_id` | `INTEGER` | `NULL` | Target shift (sick shift, or shift being surrendered). |
| `day` | `TEXT` | `NULL` | Weekday for `day_off` request. |
| `target_shift_id` | `INTEGER` | `NULL` | Desired shift for `switch` request. |
| `target_user_id` | `INTEGER` | `NULL` | Coworker invited to a direct employee-to-employee swap. |
| `vacation_start` | `TEXT` | `NULL` | ISO date start of vacation (inclusive). |
| `vacation_end` | `TEXT` | `NULL` | ISO date end of vacation (inclusive). |
| `week_start` | `TEXT` | `NULL` | ISO date of week for `day_off` request. |
| `reason` | `TEXT` | `NULL` | Decision or rejection explanation shown in request history. |
| `status` | `TEXT` | `NOT NULL DEFAULT 'approved'` | Request status: `pending`, `approved`, `approved_ok`, `denied`. |
| `created_at` | `TEXT` | `NOT NULL` | ISO timestamp of request submission. |

---

### `notifications`
In-app communication delivery queue.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PRIMARY KEY` | Unique notification identifier. |
| `user_id` | `INTEGER` | `NOT NULL` | Reference to recipient in `users.id`. |
| `shift_id` | `INTEGER` | `NULL` | Associated shift (if applicable). |
| `kind` | `TEXT` | `NOT NULL` | Notification category: `'assignment'` or `'conflict'`. |
| `message` | `TEXT` | `NOT NULL` | Text content of notification. |
| `created_at` | `TEXT` | `NOT NULL` | ISO timestamp of notification creation. |
| `read` | `INTEGER` | `NOT NULL DEFAULT 0` | Read flag (`0` = unread, `1` = read). |
