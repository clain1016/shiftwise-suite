#!/usr/bin/env python3
"""ShiftWise Live-Week Concurrency Gauntlet ("liveweek").

Simulates one real scheduling week with 15 employees and 2 managers driving
the app like production traffic: real HTTP requests against a real threaded
WSGI server, on an isolated SQLite database. Employees pick shifts, swap,
call in sick, request vacations and days off, exchange swap invites and
answer them; managers rebuild the schedule, add/delete shifts, triage the
request queue, edit the roster and assign overrides.

Goal: surface database, concurrency, and collision issues — WITHOUT fixing
them. Everything is recorded as agent-ready artifacts:

    tools/liveweek-artifacts/run-<ts>/
      REPORT.md         human/agent-readable issue index + coverage matrix
      summary.json      machine-readable issues + coverage + config
      eventlog.jsonl    every client request (actor, method, path, status, ms)
      serverlog.jsonl   every server response + unhandled server tracebacks
      issues/ISSUE-*.md one file per issue: evidence, traceback, code locations
      snapshots/*.html  HTML page snapshots taken at issue time
      snapshots/*.png   rendered screenshots (when a chrome binary exists)
      liveweek.db       database state at end of run

Usage:
    .venv/bin/python tools/liveweek.py [--duration 120] [--seed N]
        [--out DIR] [--strict] [--no-screenshots]

Exit codes: 0 = harness completed (findings are the product, not failures);
1 = harness itself broke; 2 = --strict and CRITICAL/HIGH findings present.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import unescape as html_unescape
from http.cookiejar import CookieJar
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ---------------------------------------------------------------- constants
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_DAYS_OFF = 2
ABSENT = ("sick", "swap_requested")
ASSIGNMENT_STATUSES = {
    "proposed", "notified", "confirmed", "switch_fixed", "manager_fixed",
    "sick", "swap_requested", "swap_invited", "coverage_fixed",
}
REQUEST_STATUSES = {
    "approved", "approved_ok", "denied", "superseded", "overridden_by_manager",
}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
FLASH_RE = re.compile(r'class="flash">([^<]*)<')

EXTRA_EMPLOYEES = [  # added to the mock-employee roster
    ("noah", "front"), ("lena", "front"), ("omar", "front"),
    ("ruby", "back"), ("theo", "back"),
]
TRANSIENT_HIRES = [("paul", "front"), ("zack", "back")]  # added+deleted mid-run
EXTRA_NAMES = {u for u, _ in EXTRA_EMPLOYEES} | {u for u, _ in TRANSIENT_HIRES}


def default_password(username: str) -> str:
    """Seed convention: mock employees use their username as password."""
    return (f"liveweek-hire-{username}-pw" if username in
            {u for u, _ in TRANSIENT_HIRES} else
            f"liveweek-pass-{username}" if username in EXTRA_NAMES else
            username)


MANAGER_SEED_PASSWORDS = {"manager": "manager",
                          "ops2": "liveweek-manager-pass"}


def seed_password(username: str) -> str:
    """Password the seed world assigned to this username.

    Managers don't follow the employee seed convention (seed_world sets
    ops2's password explicitly), so fresh_session must not derive theirs
    via default_password — otherwise the second manager can never log in
    and drills like double_review silently race with one manager.
    """
    return MANAGER_SEED_PASSWORDS.get(username, default_password(username))

HOTSPOTS = {  # category -> suspected code locations for fixing agents
    "http_500": ["see the server traceback inside this issue; the deepest "
                 "shiftwise/ frame is the failing call site"],
    "orphan_assignment": [
        "shiftwise/routes/roster.py:delete_employee (manual cascade, no FK "
        "constraints in schema -> races with concurrent writers)",
        "shiftwise/routes/conflicts.py:unassign",
        "shiftwise/scheduler/engine.py:run_scheduler (assignment deletes)",
    ],
    "orphan_row": [
        "shiftwise/routes/roster.py:delete_employee (manual cascade, no FK "
        "constraints in schema -> races with concurrent writers)",
        "shiftwise/routes/manager.py:delete_shift (supersedes requests "
        "referencing the deleted shift but leaves dangling shift_id/"
        "target_shift_id references that persist forever)",
        "shiftwise/db.py:SCHEMA (no FOREIGN KEY declarations despite "
        "PRAGMA foreign_keys=ON)",
    ],
    "capacity_violation": [
        "shiftwise/routes/conflicts.py:manager_assign",
        "shiftwise/routes/employee.py:swap (DELETE/INSERT not atomic)",
        "shiftwise/routes/manager.py:approve_request (swap branch: INSERT "
        "without conflict re-check under concurrency)",
        "shiftwise/scheduler/coverage.py:coverage_plan",
    ],
    "status_invalid": [
        "shiftwise/routes/employee.py + shiftwise/routes/manager.py "
        "(request state transitions are scattered, no single writer)",
    ],
    "duplicate_invite": [
        "shiftwise/routes/employee.py:request_employee_swap (pending-dup "
        "check is read-then-insert, not atomic)",
    ],
    "invite_inconsistent": [
        "shiftwise/routes/employee.py:swap/respond_to_swap vs "
        "shiftwise/scheduler/engine.py:run_scheduler (swap_invited handling)",
    ],
    "dayoff_violated": [
        "shiftwise/scheduler/engine.py:run_scheduler (fixed statuses are "
        "kept even when a day-off got approved afterwards)",
        "shiftwise/routes/conflicts.py:manager_assign (override path)",
    ],
    "vacation_violated": [
        "shiftwise/scheduler/engine.py:run_scheduler (fixed statuses are "
        "kept even when vacation got approved afterwards)",
    ],
    "hours_cap": [
        "shiftwise/scheduler/engine.py:run_scheduler (over-cap backfill "
        "allowed for vacation-gap slots)",
    ],
    "days_off": [
        "shiftwise/scheduler/engine.py:run_scheduler (MIN_DAYS_OFF guard)",
    ],
    "rank_collision": [
        "shiftwise/routes/employee.py:pick (DELETE+INSERT of all picks is "
        "one transaction but two concurrent submits interleave scheduler "
        "rebuilds)",
    ],
    "integrity": [
        "shiftwise/db.py (connection/pragma handling)",
    ],
    "behavior_mismatch": [
        "the flash/status expectation in this issue names the route; read "
        "that route's guard order under the concurrent actors listed",
    ],
    "harness_exception": [
        "often an unexpected app response shape (missing flash/element); "
        "check the attached event context before assuming a harness bug",
    ],
    "latency": [
        "shiftwise/db.py:db() (fresh connection per call, busy_timeout 15s)",
        "shiftwise/scheduler/engine.py:run_scheduler (BEGIN IMMEDIATE "
        "serializes every rebuild; called from many routes)",
    ],
    "server_error": [
        "see serverlog.jsonl around the timestamp",
    ],
}


# ---------------------------------------------------------------- recorder
class Recorder:
    """Thread-safe issue + event store."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.lock = threading.Lock()
        self.issues: list[dict] = []
        self.fp_index: dict[str, dict] = {}
        self.coverage: dict[str, dict] = defaultdict(
            lambda: {"attempts": 0, "skips": 0, "mismatch": 0})
        self._seq = 0

    def event(self, **kw):
        kw["ts"] = round(time.time(), 3)
        line = json.dumps(kw, default=str)
        with self.lock:
            with open(self.run_dir / "eventlog.jsonl", "a") as f:
                f.write(line + "\n")

    def server_event(self, **kw):
        kw["ts"] = round(time.time(), 3)
        line = json.dumps(kw, default=str)
        with self.lock:
            with open(self.run_dir / "serverlog.jsonl", "a") as f:
                f.write(line + "\n")

    def coverage_tick(self, name: str, skip: bool = False, mismatch: bool = False):
        with self.lock:
            c = self.coverage[name]
            c["attempts" if not skip else "skips"] += 1
            if mismatch:
                c["mismatch"] += 1

    def issue(self, severity: str, category: str, title: str, detail: dict,
              actor: str = "-", phase: str = "-", drill: str = "-",
              fingerprint_extra: str = "") -> dict:
        fp = category + "|" + (fingerprint_extra or title)
        with self.lock:
            if fp in self.fp_index:
                dup = self.fp_index[fp]
                dup["count"] += 1
                dup["last_seen"] = time.time()
                return dup
            self._seq += 1
            issue = {
                "id": f"ISSUE-{self._seq:03d}",
                "ts": time.time(),
                "severity": severity,
                "category": category,
                "title": title,
                "detail": detail,
                "actor": actor,
                "phase": phase,
                "drill": drill,
                "count": 1,
                "confirmed": False,
            }
            self.fp_index[fp] = issue
            self.issues.append(issue)
        print(f"    [{severity}] {issue['id']}: {title}", flush=True)
        return issue


# ---------------------------------------------------------------- session
class Response:
    __slots__ = ("status", "html", "url", "ms", "err")

    def __init__(self, status, html, url, ms, err):
        self.status, self.html, self.url, self.ms, self.err = (
            status, html, url, ms, err)

    @property
    def flash(self):
        m = FLASH_RE.search(self.html)
        return html_unescape(m.group(1)) if m else None


class WSession:
    """One signed-in browser tab: cookie jar + CSRF token + event logging."""

    def __init__(self, base: str, rec: Recorder, actor: str, phase: str):
        self.base = base
        self.rec = rec
        self.actor = actor
        self.phase = phase
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None

    def request(self, method, path, data=None, timeout=45) -> Response:
        url = self.base + path
        hdrs = {"User-Agent": "liveweek/1.0"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            if self.csrf:
                hdrs["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        t0 = time.monotonic()
        status, html, final_url, err = 0, "", url, None
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.code)
                html = resp.read().decode("utf-8", "replace")
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                html = e.read().decode("utf-8", "replace")
            except Exception:
                html = ""
            final_url = e.geturl() or url
        except Exception as e:  # connection-level failure
            err = f"{type(e).__name__}: {e}"
        ms = (time.monotonic() - t0) * 1000.0
        m = CSRF_RE.search(html)
        if m:
            self.csrf = m.group(1)
        rec_event = {"actor": self.actor, "kind": "http", "method": method,
                     "path": path, "status": status, "ms": round(ms, 1),
                     "phase": self.phase, "final_url": final_url}
        if err:
            rec_event["error"] = err
            self.rec.issue("HIGH", "server_error",
                           f"Connection failure for {method} {path}",
                           {"error": err}, actor=self.actor, phase=self.phase)
        self.rec.event(**rec_event)
        if status >= 500:
            self.rec.issue("CRITICAL", "http_500",
                           f"{self.actor}: {method} {path} -> {status}",
                           {"actor": self.actor, "method": method, "path": path,
                            "status": status, "phase": self.phase},
                           actor=self.actor, phase=self.phase,
                           fingerprint_extra=f"{method}|{path}")
        if ms > 15000:
            self.rec.issue("HIGH", "latency",
                           f"{self.actor}: {method} {path} took {ms:.0f}ms "
                           "(>= 15s, matches busy_timeout ceiling)",
                           {"ms": round(ms), "path": path},
                           actor=self.actor, phase=self.phase,
                           fingerprint_extra=path)
        elif ms > 3000:
            self.rec.issue("MEDIUM", "latency",
                           f"{self.actor}: {method} {path} took {ms:.0f}ms",
                           {"ms": round(ms), "path": path},
                           actor=self.actor, phase=self.phase,
                           fingerprint_extra=path)
        return Response(status, html, final_url, ms, err)

    def get(self, path) -> Response:
        return self.request("GET", path)

    def post(self, path, data=None) -> Response:
        return self.request("POST", path, data=data or {})

    def login(self, username: str, password: str) -> bool:
        self.get("/login")
        r = self.post("/login", {"username": username, "password": password})
        ok = "Log out" in r.html
        if not ok and r.flash and "Wrong username" not in r.flash:
            self.rec.issue("MEDIUM", "behavior_mismatch",
                           f"login {username}: unexpected flash '{r.flash}'",
                           {"status": r.status, "url": r.url},
                           actor=username, phase=self.phase,
                           fingerprint_extra=f"login|{username}")
        return ok

    def expect(self, resp: Response, want_flash: str | None, name: str,
               forbidden_flash: str | None = None):
        ok = resp.err is None and resp.status < 500
        if ok and want_flash:
            ok = resp.flash is not None and want_flash in resp.flash
        if ok and forbidden_flash and resp.flash:
            ok = forbidden_flash not in resp.flash
        if not ok:
            self.rec.issue("MEDIUM", "behavior_mismatch",
                           f"{self.actor}: {name} unexpected result",
                           {"want_flash": want_flash, "got_flash": resp.flash,
                            "status": resp.status, "url": resp.url,
                            "err": resp.err},
                           actor=self.actor, phase=self.phase,
                           fingerprint_extra=name)
        self.rec.coverage_tick(name, mismatch=not ok)
        return ok


# ---------------------------------------------------------------- world
class World:
    """Read-only DB helpers + mutable credential map (password changes)."""

    def __init__(self, db_path: Path, rec: Recorder):
        self.db_path = db_path
        self.rec = rec
        self.lock = threading.Lock()
        self.passwords: dict[str, str] = {}

    def q(self, sql, args=()):
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def usernames(self) -> list[str]:
        return [r["username"] for r in self.q(
            "SELECT username FROM users WHERE role='employee' ORDER BY id")]

    def uid(self, username: str):
        r = self.one("SELECT id FROM users WHERE username=?", (username,))
        return r["id"] if r else None

    def station(self, username: str) -> str:
        r = self.one("SELECT station FROM users WHERE username=?", (username,))
        return r["station"] if r else "front"

    def password(self, username: str, default: str) -> str:
        with self.lock:
            return self.passwords.get(username, default)

    def set_password(self, username: str, pw: str):
        with self.lock:
            self.passwords[username] = pw

    def week_shifts(self, week: str, area: str):
        return self.q("SELECT * FROM shifts WHERE week_start=? AND area=? "
                      "ORDER BY id", (week, area))

    def staffed(self, shift_id: int) -> int:
        r = self.one("SELECT COUNT(*) c FROM assignments WHERE shift_id=? "
                     "AND status NOT IN ('sick','swap_requested')", (shift_id,))
        return r["c"]

    def my_ok_assignments(self, uid_: int):
        return self.q(
            "SELECT a.shift_id, a.status, s.day, s.start_time, s.end_time, "
            "s.area, s.week_start FROM assignments a JOIN shifts s "
            "ON s.id=a.shift_id WHERE a.user_id=? AND a.status NOT IN "
            "('sick','swap_requested','manager_fixed','swap_invited')",
            (uid_,))

    def peers(self, uid_: int, station: str):
        return self.q("SELECT id, name FROM users WHERE role='employee' "
                      "AND station=? AND id!=?", (station, uid_))

    def pending_requests(self):
        return self.q(
            "SELECT id, user_id, kind, shift_id, target_shift_id, "
            "target_user_id, day, week_start, vacation_start, vacation_end "
            "FROM requests WHERE status='approved'")

    def targeted_invites_for(self, uid_: int):
        return self.q(
            "SELECT * FROM requests WHERE kind='swap' AND target_user_id=? "
            "AND status='approved'", (uid_,))

    def wait_for(self, fn, timeout=10.0, interval=0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(interval)
        return None


# ---------------------------------------------------------------- audit
def run_audit(world: World, rec: Recorder, phase: str, final: bool = False):
    """Recompute invariants from the DB; each violation becomes an issue.
    Reuses the app's own rule helpers where possible."""
    findings: list[tuple[str, str, str, str]] = []  # sev, category, key, note
    try:
        con = sqlite3.connect(world.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        integrity = con.execute("PRAGMA integrity_check").fetchall()
        if any(r[0] != "ok" for r in integrity):
            findings.append(("CRITICAL", "integrity", "integrity_check",
                             "; ".join(str(r[0]) for r in integrity)))
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            findings.append(("CRITICAL", "integrity", "foreign_key_check",
                             f"{len(fk)} violations"))
        users = {r["id"]: r for r in con.execute("SELECT * FROM users")}
        shifts = {r["id"]: r for r in con.execute("SELECT * FROM shifts")}

        # over-capacity
        staffed = defaultdict(int)
        for r in con.execute("SELECT shift_id, COUNT(*) c FROM assignments "
                             "WHERE status NOT IN ('sick','swap_requested') "
                             "GROUP BY shift_id"):
            staffed[r["shift_id"]] = r["c"]
        for sid, n in staffed.items():
            s = shifts.get(sid)
            if s and n > s["slots"]:
                findings.append(("CRITICAL", "capacity_violation",
                                 f"shift:{sid}",
                                 f"{s['day']} {s['start_time']}-{s['end_time']} "
                                 f"({s['area']}) staffed {n} > slots {s['slots']}"))

        # orphans
        for table, cols in (("assignments", ("user_id", "shift_id")),
                            ("picks", ("user_id", "shift_id")),
                            ("coverage_preferences", ("user_id", "shift_id")),
                            ("requests", ("user_id", "shift_id",
                                          "target_shift_id", "target_user_id")),
                            ("notifications", ("user_id",))):
            for col in cols:
                ph = ("target_user_id" if col == "target_user_id" else col)
                rows = con.execute(
                    f"SELECT rowid AS rid, {col} c FROM {table} WHERE {col} IS NOT NULL "
                    f"AND {col} NOT IN (SELECT id FROM "
                    f"{'users' if col.endswith('user_id') else 'shifts'})").fetchall()
                for r in rows:
                    sev = "MEDIUM" if table == "notifications" else "HIGH"
                    findings.append((sev, "orphan_assignment" if table ==
                                     "assignments" else "orphan_row",
                                     f"{table}.{ph}:{r['c']}",
                                     f"row {table}#{r['rid']} references missing "
                                     f"{'user' if col.endswith('user_id') else 'shift'} "
                                     f"{r['c']}"))

        # invalid statuses
        for r in con.execute("SELECT id, status FROM assignments"):
            if r["status"] not in ASSIGNMENT_STATUSES:
                findings.append(("HIGH", "status_invalid",
                                 f"assignment:{r['id']}",
                                 f"assignment #{r['id']} has invalid status "
                                 f"'{r['status']}'"))
        for r in con.execute("SELECT id, status, kind FROM requests"):
            if r["status"] not in REQUEST_STATUSES:
                findings.append(("HIGH", "status_invalid",
                                 f"request:{r['id']}",
                                 f"request #{r['id']} ({r['kind']}) has invalid "
                                 f"status '{r['status']}'"))

        # duplicate active swap invites for same (requester, source shift)
        dup = con.execute(
            "SELECT user_id, shift_id, COUNT(*) c FROM requests WHERE "
            "kind='swap' AND target_user_id IS NOT NULL AND status='approved' "
            "GROUP BY user_id, shift_id HAVING c>1").fetchall()
        for r in dup:
            findings.append(("MEDIUM", "duplicate_invite",
                             f"invite:{r['user_id']}:{r['shift_id']}",
                             f"{r['c']} pending swap invites for same "
                             f"(user {r['user_id']}, shift {r['shift_id']})"))

        # invite <-> swap_invited assignment consistency
        invited = {(r["shift_id"], r["user_id"]) for r in con.execute(
            "SELECT shift_id, user_id FROM assignments WHERE status='swap_invited'")}
        open_invites = {(r["shift_id"], r["user_id"]) for r in con.execute(
            "SELECT shift_id, user_id FROM requests WHERE kind='swap' AND "
            "target_user_id IS NOT NULL AND status='approved'")}
        for key in invited - open_invites:
            findings.append(("MEDIUM", "invite_inconsistent",
                             f"invite_assign:{key[0]}:{key[1]}",
                             f"assignment swap_invited (shift {key[0]}, user "
                             f"{key[1]}) has no matching pending invite request"))
        for key in open_invites - invited:
            findings.append(("MEDIUM", "invite_inconsistent",
                             f"invite_request:{key[0]}:{key[1]}",
                             f"pending swap invite (shift {key[0]}, user "
                             f"{key[1]}) but no swap_invited assignment"))

        # approved day_off honored?
        for r in con.execute(
                "SELECT r.id, r.user_id, r.day, r.week_start FROM requests r "
                "WHERE r.kind='day_off' AND r.status='approved_ok'"):
            hit = con.execute(
                "SELECT a.id, a.status FROM assignments a JOIN shifts s ON "
                "s.id=a.shift_id WHERE a.user_id=? AND s.week_start=? AND "
                "s.day=? AND a.status NOT IN ('sick','swap_requested') "
                "AND a.status!='manager_fixed'", (r["user_id"], r["week_start"],
                                                  r["day"])).fetchone()
            if hit:
                findings.append(("MEDIUM", "dayoff_violated",
                                 f"dayoff:{r['id']}",
                                 f"approved day-off ({r['day']}) for user "
                                 f"{r['user_id']} but assignment #{hit['id']} "
                                 f"({hit['status']}) still on that day"))

        # approved vacation honored?
        for r in con.execute(
                "SELECT id, user_id, vacation_start, vacation_end FROM requests "
                "WHERE kind='vacation' AND status='approved_ok'"):
            hit = con.execute(
                "SELECT a.id, a.status, s.day, s.week_start FROM assignments a "
                "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND "
                "a.status NOT IN ('sick','swap_requested') AND "
                "a.status!='manager_fixed'", (r["user_id"],)).fetchall()
            for a in hit:
                d = date.fromisoformat(a["week_start"]) + timedelta(
                    days=DAYS.index(a["day"]))
                if r["vacation_start"] and \
                        date.fromisoformat(r["vacation_start"]) <= d <= \
                        date.fromisoformat(r["vacation_end"]):
                    findings.append(("MEDIUM", "vacation_violated",
                                     f"vacation:{r['id']}",
                                     f"approved vacation {r['vacation_start']}.."
                                     f"{r['vacation_end']} for user {r['user_id']} "
                                     f"but assignment #{a['id']} ({a['status']}) "
                                     f"on {a['day']}"))

        # weekly hours caps + MIN_DAYS_OFF (current week only)
        week = min((s["week_start"] for s in shifts.values()), default=None)
        if week:
            hours = defaultdict(float)
            days = defaultdict(set)
            for r in con.execute(
                    "SELECT a.user_id, s.id, s.day, s.start_time, s.end_time, "
                    "a.status FROM assignments a JOIN shifts s ON s.id=a.shift_id "
                    "WHERE s.week_start=? AND a.status NOT IN "
                    "('sick','swap_requested')", (week,)):
                sh, sm = map(int, r["start_time"].split(":"))
                eh, em = map(int, r["end_time"].split(":"))
                h = (eh * 60 + em - sh * 60 - sm) / 60.0
                hours[r["user_id"]] += h
                days[r["user_id"]].add(r["day"])
            for uid_, h in hours.items():
                u = users.get(uid_)
                if not u:
                    continue
                cap = u["weekly_hours"] or 40
                if h > cap + 4:
                    findings.append(("HIGH", "hours_cap", f"hours:{uid_}",
                                     f"user {u['username']} scheduled {h:.1f}h "
                                     f"> cap {cap}h (+4h tolerance)"))
                elif h > cap + 0.01:
                    findings.append(("MEDIUM", "hours_cap", f"hours:{uid_}",
                                     f"user {u['username']} scheduled {h:.1f}h "
                                     f"> cap {cap}h (may be legal vacation-gap "
                                     f"backfill)"))
            for uid_, ds in days.items():
                if len(ds) > 7 - MIN_DAYS_OFF:
                    u = users.get(uid_)
                    findings.append(("HIGH", "days_off", f"days:{uid_}",
                                     f"user {u['username'] if u else uid_} works "
                                     f"{len(ds)} days (min days off "
                                     f"{MIN_DAYS_OFF})"))

        # duplicate ranks per user
        dup = con.execute(
            "SELECT user_id, rank, COUNT(*) c FROM picks GROUP BY user_id, rank "
            "HAVING c>1").fetchall()
        for r in dup:
            findings.append(("MEDIUM", "rank_collision",
                             f"rank:{r['user_id']}:{r['rank']}",
                             f"user {r['user_id']} has {r['c']} picks at rank "
                             f"{r['rank']} (concurrent submits interleaved)"))
        con.close()
    except sqlite3.Error as exc:
        findings.append(("CRITICAL", "integrity", "audit_sql",
                         f"audit query failed: {exc}"))

    for sev, category, key, note in findings:
        rec.issue(sev, category, f"{category}: {key}", {"detail": note},
                  phase=phase, drill="audit", fingerprint_extra=key)
    return findings


# ---------------------------------------------------------------- behaviors
def b_view_pages(ctx):
    ctx.sess.get("/")
    ctx.sess.get("/my-requests")
    ctx.sess.get(f"/calendar?week={ctx.week}")


def b_full_picks(ctx):
    """Rank every shift in own house + coverage willingness."""
    shifts = ctx.world.week_shifts(ctx.week, ctx.station)
    if not shifts:
        ctx.rec.coverage_tick("full_picks", skip=True)
        return
    n = len(shifts)
    ranks = list(range(1, n + 1))
    ctx.rng.shuffle(ranks)
    data = {}
    for s, rk in zip(shifts, ranks):
        data[f"rank_{s['id']}"] = str(rk)
        data[f"cover_{s['id']}"] = ctx.rng.choice(["yes", "no"])
    r = ctx.sess.post("/pick", data)
    ctx.sess.expect(r, "Preferences saved", "full_picks")


def b_partial_picks(ctx):
    """Invalid submission: miss one rank; app must reject with a flash."""
    shifts = ctx.world.week_shifts(ctx.week, ctx.station)
    if len(shifts) < 2:
        ctx.rec.coverage_tick("partial_picks", skip=True)
        return
    n = len(shifts)
    ranks = list(range(1, n))
    ctx.rng.shuffle(ranks)
    data = {}
    for s, rk in zip(shifts[:-1], ranks):
        data[f"rank_{s['id']}"] = str(rk)
    r = ctx.sess.post("/pick", data)
    ok = r.flash and ("Rank all seven days" in r.flash or "Use each rank" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: partial pick submit was NOT rejected "
                      f"(flash: {r.flash!r})",
                      {"status": r.status}, actor=ctx.sess.actor,
                      phase=ctx.phase, fingerprint_extra="partial_picks")
    ctx.rec.coverage_tick("partial_picks", mismatch=not ok)


def b_day_off(ctx):
    day = ctx.rng.choice(DAYS)
    r = ctx.sess.post("/request/day_off", {"day": day})
    ok = "Day off requested" in (r.flash or "") or \
        "already have an active request" in (r.flash or "")
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: day_off '{day}' unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="day_off")
    ctx.rec.coverage_tick("day_off", mismatch=not ok)


def b_vacation(ctx):
    start = date.today() + timedelta(days=ctx.rng.randint(1, 9))
    end = start + timedelta(days=ctx.rng.randint(1, 3))
    r = ctx.sess.post("/request/vacation", {"vac_start": start.isoformat(),
                                            "vac_end": end.isoformat()})
    ctx.sess.expect(r, "Vacation requested", "vacation")


def b_sick(ctx):
    mine = ctx.world.my_ok_assignments(ctx.uid)
    if not mine:
        ctx.rec.coverage_tick("sick", skip=True)
        return
    a = ctx.rng.choice(mine)
    r = ctx.sess.post(f"/request/sick/{a['shift_id']}")
    ctx.sess.expect(r, "Sick call logged", "sick")


def b_self_swap(ctx):
    mine = [a for a in ctx.world.my_ok_assignments(ctx.uid)]
    if not mine:
        ctx.rec.coverage_tick("self_swap", skip=True)
        return
    a = ctx.rng.choice(mine)
    r = ctx.sess.post(f"/swap/{a['shift_id']}")
    ok = r.flash and ("Swap arranged" in r.flash or
                      "Swap requested, but nobody" in r.flash or
                      "can't be swapped" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: self-swap unexpected flash",
                      {"got_flash": r.flash, "status": r.status,
                       "shift": a["shift_id"]},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="self_swap")
    ctx.rec.coverage_tick("self_swap", mismatch=not ok)


def b_switch(ctx):
    mine = ctx.world.my_ok_assignments(ctx.uid)
    if not mine:
        ctx.rec.coverage_tick("switch", skip=True)
        return
    src = ctx.rng.choice(mine)
    others = [s for s in ctx.world.week_shifts(ctx.week, ctx.station)
              if s["id"] != src["shift_id"]]
    if not others:
        ctx.rec.coverage_tick("switch", skip=True)
        return
    tgt = ctx.rng.choice(others)
    r = ctx.sess.post(f"/request/switch/{src['shift_id']}",
                      {"target_shift": str(tgt["id"])})
    ctx.sess.expect(r, "Switch request sent", "switch")


def b_swap_invite(ctx):
    """Invite a same-house coworker: my source shift <-> their shift."""
    mine = ctx.world.my_ok_assignments(ctx.uid)
    peers = ctx.world.peers(ctx.uid, ctx.station)
    if not mine or not peers:
        ctx.rec.coverage_tick("swap_invite", skip=True)
        return
    peer = ctx.rng.choice(peers)
    theirs = ctx.world.my_ok_assignments(peer["id"])
    if not theirs:
        ctx.rec.coverage_tick("swap_invite", skip=True)
        return
    src = ctx.rng.choice(mine)
    tgt = ctx.rng.choice(theirs)
    if src["shift_id"] == tgt["shift_id"]:
        ctx.rec.coverage_tick("swap_invite", skip=True)
        return
    r = ctx.sess.post("/request/swap", {
        "shift_id": str(src["shift_id"]),
        "target_assignment": f"{peer['id']}:{tgt['shift_id']}"})
    ok = r.flash and ("Swap request sent" in r.flash or
                      "Choose valid shifts" in r.flash or
                      "already have a pending request" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: swap invite unexpected flash",
                      {"got_flash": r.flash, "status": r.status,
                       "src": src["shift_id"], "tgt": tgt["shift_id"],
                       "peer": peer["name"]},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="swap_invite")
    ctx.rec.coverage_tick("swap_invite", mismatch=not ok)


def b_respond_invite(ctx):
    invites = ctx.world.targeted_invites_for(ctx.uid)
    if not invites:
        ctx.rec.coverage_tick("respond_invite", skip=True)
        return
    req = ctx.rng.choice(invites)
    accept = ctx.rng.random() < 0.6
    data = {"decision": "accept" if accept else "reject"}
    if not accept:
        data["reason"] = "Liveweek: plans changed."
    r = ctx.sess.post(f"/request/{req['id']}/respond", data)
    ok = r.flash and ("Swap accepted" in r.flash or "Swap declined" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: swap respond unexpected result",
                      {"got_flash": r.flash, "status": r.status,
                       "req": req["id"], "decision": data["decision"]},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="respond_invite")
    ctx.rec.coverage_tick("respond_invite", mismatch=not ok)


def b_double_pick(ctx):
    """Same form twice back-to-back. The second submit may legitimately be
    rejected if a manager added/deleted a shift between the two submits
    (rank set went stale) — only a 5xx or silent corruption is a failure."""
    shifts = ctx.world.week_shifts(ctx.week, ctx.station)
    if not shifts:
        ctx.rec.coverage_tick("double_pick", skip=True)
        return
    n = len(shifts)
    ranks = list(range(1, n + 1))
    ctx.rng.shuffle(ranks)
    data = {f"rank_{s['id']}": str(rk) for s, rk in zip(shifts, ranks)}
    r1 = ctx.sess.post("/pick", data)
    r2 = ctx.sess.post("/pick", data)
    ok = all(r.status < 500 and r.flash for r in (r1, r2))
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: double pick submit anomalous",
                      {"flash1": r1.flash, "flash2": r2.flash,
                       "status1": r1.status, "status2": r2.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="double_pick")
    ctx.rec.coverage_tick("double_pick", mismatch=not ok)


def b_authz_probe(ctx):
    """Employee touches manager-only routes + posts without CSRF."""
    r1 = ctx.sess.post("/manager/run")
    ok1 = r1.status == 403
    if r1.status >= 500:
        pass  # http_500 already recorded by session
    elif r1.status != 403:
        ctx.rec.issue("HIGH", "behavior_mismatch",
                      f"{ctx.sess.actor}: employee POST to /manager/run was "
                      f"not rejected with 403 (got {r1.status})",
                      {"flash": r1.flash, "status": r1.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="authz_probe_run")
    r3 = ctx.sess.get("/manager")
    if r3.status == 200:
        ctx.rec.issue("HIGH", "behavior_mismatch",
                      f"{ctx.sess.actor}: employee GET /manager rendered "
                      "the manager dashboard (expected redirect or 403)",
                      {"status": r3.status}, actor=ctx.sess.actor,
                      phase=ctx.phase, fingerprint_extra="authz_probe_dash")
    # missing CSRF: send raw without token header
    saved = ctx.sess.csrf
    ctx.sess.csrf = None
    r2 = ctx.sess.post("/request/day_off", {"day": "Mon"})
    ctx.sess.csrf = saved
    ok2 = r2.status == 400
    if not ok2:
        ctx.rec.issue("HIGH", "behavior_mismatch",
                      f"{ctx.sess.actor}: missing-CSRF POST was not rejected "
                      f"with 400 (got {r2.status})",
                      {"flash": r2.flash}, actor=ctx.sess.actor,
                      phase=ctx.phase, fingerprint_extra="csrf_probe")
    ctx.rec.coverage_tick("authz_probe",
                          mismatch=not (ok1 and ok2 and r3.status != 200))


def b_password_rotate(ctx, max_rotations=2):
    with ctx.world.lock:
        already = ctx.world.passwords
        if len(already) >= max_rotations or ctx.sess.actor in already:
            ctx.rec.coverage_tick("password_rotate", skip=True)
            return
    old = ctx.base_password
    new = f"liveweek-rotated-{ctx.sess.actor}-pw"
    r = ctx.sess.post("/account/password", {"old_password": old,
                                            "new_password": new})
    if "Password changed" in (r.flash or ""):
        ctx.world.set_password(ctx.sess.actor, new)
        ok = ctx.sess.login(ctx.sess.actor, new)
        if not ok:
            ctx.rec.issue("HIGH", "behavior_mismatch",
                          f"{ctx.sess.actor}: re-login after password change "
                          "failed", {"actor": ctx.sess.actor},
                          actor=ctx.sess.actor, phase=ctx.phase,
                          fingerprint_extra="password_rotate")
    else:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: password change unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="password_rotate")
    ctx.rec.coverage_tick("password_rotate", mismatch=False)


def b_bad_login(ctx):
    r = ctx.sess.post("/login", {"username": ctx.sess.actor,
                                 "password": "definitely-wrong-pw"})
    ok = "Wrong username or password" in (r.flash or "")
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: bad login unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="bad_login")
    ctx.rec.coverage_tick("bad_login", mismatch=not ok)
    # recover: proper login clears failure counters
    ctx.sess.login(ctx.sess.actor, ctx.world.password(
        ctx.sess.actor, ctx.base_password))


def b_login_logout(ctx):
    ctx.sess.get("/logout")
    ok = ctx.sess.login(ctx.sess.actor, ctx.world.password(
        ctx.sess.actor, ctx.base_password))
    if not ok:
        ctx.rec.issue("HIGH", "behavior_mismatch",
                      f"{ctx.sess.actor}: re-login after logout failed",
                      {}, actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="login_logout")
    ctx.rec.coverage_tick("login_logout", mismatch=not ok)


EMPLOYEE_BEHAVIORS = [
    b_view_pages, b_full_picks, b_partial_picks, b_day_off, b_vacation,
    b_sick, b_self_swap, b_switch, b_swap_invite, b_respond_invite,
    b_double_pick, b_authz_probe, b_password_rotate, b_login_logout,
]


def m_view_pages(ctx):
    ctx.sess.get("/manager")
    ctx.sess.get("/manager/requests")
    ctx.sess.get(f"/manager/conflicts?week={ctx.week}")
    ctx.sess.get("/manager/conflicts?week=not-a-date")  # must not 500
    ctx.sess.get("/manager/roster")


def m_rebuild(ctx):
    r = ctx.sess.post("/manager/run")
    ctx.sess.expect(r, "Scheduler ran over", "m_rebuild")


def m_review_queue(ctx):
    pending = ctx.world.pending_requests()
    if not pending:
        ctx.rec.coverage_tick("m_review_queue", skip=True)
        return
    req = ctx.rng.choice(pending)
    deny = ctx.rng.random() < 0.4
    path = (f"/manager/requests/{req['id']}/deny" if deny else
            f"/manager/requests/{req['id']}/approve")
    data = {"reason": "Liveweek triage: cannot cover this."} if deny else {}
    r = ctx.sess.post(path, data)
    ok = r.flash and ("Request approved" in r.flash or "Swap covered" in
                      r.flash or "Request denied" in r.flash or
                      "already handled" in r.flash or
                      "can't be approved until coverage" in r.flash or
                      "No eligible coverer" in r.flash or
                      "reviewed" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: triage of request #{req['id']} "
                      f"({req['kind']}) unexpected flash",
                      {"got_flash": r.flash, "status": r.status,
                       "action": "deny" if deny else "approve"},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="m_review_queue")
    ctx.rec.coverage_tick("m_review_queue", mismatch=not ok)


def m_add_shift(ctx):
    day = ctx.rng.choice(DAYS)
    area = ctx.rng.choice(["front", "back"])
    start, end = ctx.rng.choice([("09:00", "17:00"), ("11:00", "19:00"),
                                 ("06:00", "14:00"), ("12:00", "20:00")])
    r = ctx.sess.post("/manager/shift/add",
                      {"area": area, "day": day, "start": start, "end": end,
                       "slots": str(ctx.rng.randint(1, 3)), "note": "liveweek"})
    ctx.sess.expect(r, "Shift added", "m_add_shift")


def m_delete_shift(ctx):
    shifts = ctx.world.week_shifts(ctx.week, "front") + \
        ctx.world.week_shifts(ctx.week, "back")
    if not shifts:
        ctx.rec.coverage_tick("m_delete_shift", skip=True)
        return
    # prefer shifts with zero staff to keep the week playable
    empties = [s for s in shifts if ctx.world.staffed(s["id"]) == 0]
    s = ctx.rng.choice(empties or shifts)
    r = ctx.sess.post(f"/manager/shift/delete/{s['id']}")
    if r.status >= 500:
        return  # recorded as http_500
    ctx.rec.coverage_tick("m_delete_shift")


def m_unassign(ctx):
    week_shifts = ctx.world.week_shifts(ctx.week, "front") + \
        ctx.world.week_shifts(ctx.week, "back")
    rows = ctx.world.q(
        "SELECT a.shift_id, a.user_id, a.status FROM assignments a JOIN "
        "shifts s ON s.id=a.shift_id WHERE s.week_start=? AND a.status "
        "NOT IN ('sick','swap_requested')", (ctx.week,))
    live = [r for r in rows if any(s["id"] == r["shift_id"] for s in week_shifts)]
    if not live:
        ctx.rec.coverage_tick("m_unassign", skip=True)
        return
    a = ctx.rng.choice(live)
    r = ctx.sess.post(f"/manager/unassign/{a['shift_id']}/{a['user_id']}")
    if r.status >= 500:
        return
    ctx.rec.coverage_tick("m_unassign")


def m_assign(ctx):
    week_shifts = ctx.world.week_shifts(ctx.week, "front") + \
        ctx.world.week_shifts(ctx.week, "back")
    free = [s for s in week_shifts if ctx.world.staffed(s["id"]) < s["slots"]]
    if not free:
        ctx.rec.coverage_tick("m_assign", skip=True)
        return
    s = ctx.rng.choice(free)
    emps = ctx.world.q("SELECT id, station FROM users WHERE role='employee' "
                       "AND station=?", (s["area"],))
    if not emps:
        ctx.rec.coverage_tick("m_assign", skip=True)
        return
    e = ctx.rng.choice(emps)
    r = ctx.sess.post(f"/manager/assign/{s['id']}/{e['id']}")
    ok = r.flash and ("Override:" in r.flash or "Can't override" in r.flash or
                      "own house" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: manager assign unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="m_assign")
    ctx.rec.coverage_tick("m_assign", mismatch=not ok)


def m_roster_edit(ctx):
    emps = ctx.world.q("SELECT * FROM users WHERE role='employee'")
    if not emps:
        ctx.rec.coverage_tick("m_roster_edit", skip=True)
        return
    data = {}
    for e in emps:
        data[f"type_{e['id']}"] = e["employment_type"]
        data[f"hired_{e['id']}"] = e["hired_on"] or ""
        data[f"cap_{e['id']}"] = str(e["weekly_hours"] or 40)
        data[f"station_{e['id']}"] = e["station"]
        data[f"email_{e['id']}"] = e["email"] or f"{e['username']}@example.com"
        data[f"phone_{e['id']}"] = e["phone"] or "+15550009999"
    # tweak one cap (valid range) so the write actually changes something
    data[f"cap_{emps[0]['id']}"] = str(max(1, (emps[0]["weekly_hours"] or 40)))
    r = ctx.sess.post("/manager/roster", data)
    ctx.sess.expect(r, "Roster updated", "m_roster_edit")


def m_add_employee(ctx):
    if ctx.hires_added:
        ctx.rec.coverage_tick("m_add_employee", skip=True)
        return
    username, station = TRANSIENT_HIRES[ctx.hire_index % len(TRANSIENT_HIRES)]
    ctx.hire_index += 1
    r = ctx.sess.post("/manager/roster/add", {
        "username": username, "name": username.title() + " Liveweek",
        "password": default_password(username),
        "employment_type": "part_time", "hired_on": date.today().isoformat(),
        "weekly_hours": "20", "email": f"{username}@example.com",
        "phone": "+15550007777"})
    ok = "Employee added" in (r.flash or "") or "already in use" in (r.flash or "")
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: roster add unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="m_add_employee")
    if "Employee added" in (r.flash or ""):
        ctx.hires_added.append(username)
    ctx.rec.coverage_tick("m_add_employee", mismatch=not ok)


def m_send_link(ctx):
    emps = ctx.world.q("SELECT id FROM users WHERE role='employee'")
    if not emps:
        ctx.rec.coverage_tick("m_send_link", skip=True)
        return
    e = ctx.rng.choice(emps)
    r = ctx.sess.post(f"/manager/roster/{e['id']}/send-link", {"channel": "email"})
    ok = r.flash and ("SHIFTWISE_PUBLIC_URL" in r.flash or
                      "Schedule link sent" in r.flash or
                      "could not be sent" in r.flash or
                      "Add this employee" in r.flash)
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: send-link unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="m_send_link")
    ctx.rec.coverage_tick("m_send_link", mismatch=not ok)


def m_reset_password(ctx):
    emps = ctx.world.q("SELECT id, username FROM users WHERE role='employee'")
    if not emps:
        ctx.rec.coverage_tick("m_reset_password", skip=True)
        return
    e = ctx.rng.choice(emps)
    pw = f"liveweek-reset-{e['username']}-pw"
    r = ctx.sess.post(f"/manager/roster/{e['id']}/password",
                      {"password": pw, "confirm_password": pw})
    ok = "Employee password reset" in (r.flash or "")
    if not ok:
        ctx.rec.issue("MEDIUM", "behavior_mismatch",
                      f"{ctx.sess.actor}: password reset unexpected flash",
                      {"got_flash": r.flash, "status": r.status},
                      actor=ctx.sess.actor, phase=ctx.phase,
                      fingerprint_extra="m_reset_password")
    else:
        # world credential map must track the reset or the employee's own
        # session can no longer re-authenticate
        ctx.world.set_password(e["username"], pw)
    ctx.rec.coverage_tick("m_reset_password", mismatch=not ok)


MANAGER_BEHAVIORS = [
    m_view_pages, m_rebuild, m_review_queue, m_add_shift, m_delete_shift,
    m_unassign, m_assign, m_roster_edit, m_add_employee, m_send_link,
    m_reset_password,
]


# ---------------------------------------------------------------- drills
def ensure_pending_invite(ctx):
    """Return (request_row, requester_session, invitee_session) for a live
    coworker swap invite, creating one synchronously if none is pending."""
    existing = ctx.world.q(
        "SELECT * FROM requests WHERE kind='swap' AND target_user_id IS NOT "
        "NULL AND status='approved'")
    for req in existing:
        for s in ctx.emp_sessions:
            if s.uid == req["target_user_id"]:
                for r in ctx.emp_sessions:
                    if r.uid == req["user_id"]:
                        return req, r, s
    # engineer a fresh invite: requester hands their first assignment to a
    # peer in exchange for the peer's first assignment
    for s in ctx.emp_sessions:
        mine = ctx.world.my_ok_assignments(s.uid)
        if not mine:
            continue
        for peer in ctx.world.peers(s.uid, s.station):
            theirs = ctx.world.my_ok_assignments(peer["id"])
            if not theirs:
                continue
            src, tgt = mine[0], theirs[0]
            if src["shift_id"] == tgt["shift_id"]:
                continue
            r = s.post("/request/swap", {
                "shift_id": str(src["shift_id"]),
                "target_assignment": f"{peer['id']}:{tgt['shift_id']}"})
            if r.flash and "Swap request sent" in r.flash:
                row = ctx.world.wait_for(
                    lambda: ctx.world.one(
                        "SELECT * FROM requests WHERE kind='swap' AND "
                        "user_id=? AND shift_id=? AND status='approved'",
                        (s.uid, src["shift_id"])), timeout=5)
                if row:
                    return row, s, next((x for x in ctx.emp_sessions
                                         if x.uid == peer["id"]), None)
    return None, None, None


def ensure_transient_hire(ctx):
    """Return username of a live transient hire, adding one via the real
    roster route if none exists yet."""
    for username in ctx.hires_added:
        if ctx.world.uid(username):
            return username
    mgr = ctx.mgr_sessions[0]
    for username, _ in TRANSIENT_HIRES:
        if not ctx.world.uid(username):
            r = mgr.post("/manager/roster/add", {
                "username": username, "name": username.title() + " Liveweek",
                "password": default_password(username),
                "employment_type": "part_time",
                "hired_on": date.today().isoformat(), "weekly_hours": "20",
                "email": f"{username}@example.com", "phone": "+15550007777"})
            if "Employee added" in (r.flash or ""):
                ctx.hires_added.append(username)
                return username
    return ctx.hires_added[0] if ctx.hires_added else None


def drill_double_review(ctx):
    """Two managers triage the SAME request simultaneously."""
    pending = ctx.world.pending_requests()
    if not pending:
        ctx.rec.coverage_tick("drill:double_review", skip=True)
        return
    req = ctx.rng.choice(pending)
    bar = threading.Barrier(2)
    m0 = fresh_session(ctx.rec, ctx.base, ctx.world,
                       ctx.mgr_sessions[0].actor, ctx.phase)
    m1 = fresh_session(ctx.rec, ctx.base, ctx.world,
                       ctx.mgr_sessions[1].actor, ctx.phase)

    def act(sess, deny):
        bar.wait()
        path = (f"/manager/requests/{req['id']}/deny" if deny else
                f"/manager/requests/{req['id']}/approve")
        data = {"reason": "liveweek drill deny"} if deny else {}
        return sess.post(path, data)

    t_results = {}
    def run(idx, sess, deny):
        t_results[idx] = act(sess, deny)
    ths = [
        threading.Thread(target=run, args=(0, m0, False)),
        threading.Thread(target=run, args=(1, m1,
                                           ctx.rng.random() < 0.3)),
    ]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    row = ctx.world.one("SELECT status FROM requests WHERE id=?", (req["id"],))
    terminal = row and row["status"] in ("approved_ok", "denied", "superseded")
    if not terminal:
        ctx.rec.issue("HIGH", "status_invalid",
                      f"drill double_review: request #{req['id']} left in "
                      f"status {row['status'] if row else 'GONE'}",
                      {"kind": req["kind"]}, phase=ctx.phase,
                      drill="double_review", fingerprint_extra="double_review")
    ctx.rec.coverage_tick("drill:double_review", mismatch=not terminal)


def drill_sick_vs_swap(ctx):
    """Same employee calls in sick AND self-swaps the same shift at once."""
    donor = next((s for s in ctx.emp_sessions
                  if ctx.world.my_ok_assignments(s.uid)), None)
    if not donor:
        ctx.rec.coverage_tick("drill:sick_vs_swap", skip=True)
        return
    # act on fresh sessions for this user: the ambient worker owns the
    # original session's flash queue, and CookieJar isn't thread-safe, so
    # each racing thread gets its own session
    sess_sick = fresh_session(ctx.rec, ctx.base, ctx.world, donor.actor,
                              ctx.phase)
    sess_swap = fresh_session(ctx.rec, ctx.base, ctx.world, donor.actor,
                              ctx.phase)
    sess = sess_sick  # same user; used for DB queries below
    mine = ctx.world.my_ok_assignments(sess.uid)
    a = ctx.rng.choice(mine)
    bar = threading.Barrier(2)
    out = {}

    def sick():
        bar.wait()
        out["sick"] = sess_sick.post(f"/request/sick/{a['shift_id']}")

    def swap():
        bar.wait()
        out["swap"] = sess_swap.post(f"/swap/{a['shift_id']}")

    ths = [threading.Thread(target=sick), threading.Thread(target=swap)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    dup = ctx.world.q(
        "SELECT COUNT(*) c FROM requests WHERE user_id=? AND shift_id=? AND "
        "kind IN ('swap','sick')", (sess.uid, a["shift_id"]))
    n_dup = dup[0]["c"] if dup else 0
    if n_dup > 1:
        ctx.rec.issue("HIGH", "behavior_mismatch",
                      "drill sick_vs_swap: both the sick call and the swap "
                      f"created requests for the same shift ({n_dup} rows)",
                      {"shift": a["shift_id"], "actor": sess.actor},
                      actor=sess.actor, phase=ctx.phase, drill="sick_vs_swap",
                      fingerprint_extra="sick_vs_swap_dup")
    ok500 = all(r.status < 500 for r in out.values() if r)
    if not ok500:
        ctx.rec.issue("CRITICAL", "http_500",
                      "drill sick_vs_swap produced a 500",
                      {"shift": a["shift_id"], "actor": sess.actor},
                      actor=sess.actor, phase=ctx.phase, drill="sick_vs_swap",
                      fingerprint_extra="sick_vs_swap")
    ctx.rec.coverage_tick("drill:sick_vs_swap",
                          mismatch=not ok500 or n_dup > 1)


def drill_delete_shift_vs_accept(ctx):
    """Manager deletes a shift that is the TARGET of a pending swap invite,
    while the invitee accepts it."""
    req, requester, invitee = ensure_pending_invite(ctx)
    if not req or not invitee:
        ctx.rec.coverage_tick("drill:delete_vs_accept", skip=True)
        return
    target_shift = req["target_shift_id"] or req["shift_id"]
    invitee = fresh_session(ctx.rec, ctx.base, ctx.world, invitee.actor,
                            ctx.phase)
    bar = threading.Barrier(2)
    out = {}

    def accept():
        bar.wait()
        out["accept"] = invitee.post(
            f"/request/{req['id']}/respond", {"decision": "accept"})

    def delete():
        bar.wait()
        out["delete"] = ctx.mgr_sessions[0].post(
            f"/manager/shift/delete/{target_shift}")

    ths = [threading.Thread(target=accept), threading.Thread(target=delete)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    ghost = ctx.world.q(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=?", (target_shift,))
    ok = ghost and ghost[0]["c"] == 0
    if not ok:
        ctx.rec.issue("CRITICAL", "orphan_assignment",
                      f"drill delete_vs_accept: shift {target_shift} deleted "
                      f"but assignments remain",
                      {"remaining": ghost[0]["c"] if ghost else "?"},
                      phase=ctx.phase, drill="delete_vs_accept",
                      fingerprint_extra=f"ghost:{target_shift}")
    ctx.rec.coverage_tick("drill:delete_vs_accept", mismatch=not ok)


def drill_pick_storm(ctx, all_sessions=None):
    """Every employee submits their full picks simultaneously."""
    sessions = all_sessions or [
        fresh_session(ctx.rec, ctx.base, ctx.world, s.actor, ctx.phase)
        for s in ctx.emp_sessions]
    bar = threading.Barrier(len(sessions))
    lat = {}

    def fire(i, sess):
        bar.wait()
        shifts = ctx.world.week_shifts(ctx.week, sess.station)
        n = len(shifts)
        if not n:
            return
        ranks = list(range(1, n + 1))
        ctx.rng.shuffle(ranks)
        data = {f"rank_{s['id']}": str(rk) for s, rk in zip(shifts, ranks)}
        r = sess.post("/pick", data)
        lat[sess.actor] = (r.ms, r.status)

    ths = [threading.Thread(target=fire, args=(i, s))
           for i, s in enumerate(sessions)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    if lat:
        slowest = max(lat.items(), key=lambda kv: kv[1][0])
        errs = {a: v for a, v in lat.items() if v[1] >= 500}
        if errs:
            ctx.rec.issue("CRITICAL", "http_500",
                          f"pick storm: {len(errs)} requests returned 5xx",
                          {"errors": {a: s for a, (m, s) in errs.items()}},
                          phase=ctx.phase, drill="pick_storm",
                          fingerprint_extra="pick_storm")
        ctx.rec.issue("INFO", "latency",
                      f"pick storm slowest: {slowest[0]} {slowest[1][0]:.0f}ms "
                      f"over {len(lat)} concurrent pick submissions",
                      {"slowest_ms": round(slowest[1][0]),
                       "n": len(lat), "errors": len(errs)},
                      phase=ctx.phase, drill="pick_storm",
                      fingerprint_extra="storm_stats")
    ctx.rec.coverage_tick("drill:pick_storm")


def drill_roster_delete_vs_activity(ctx):
    """Employee submits a vacation while the manager deletes their account."""
    target = ensure_transient_hire(ctx)
    if not target:
        ctx.rec.coverage_tick("drill:roster_delete", skip=True)
        return
    uid_ = ctx.world.uid(target)
    sess = WSession(ctx.base, ctx.rec, target, ctx.phase)
    if not sess.login(target, ctx.world.password(target,
                                                 default_password(target))):
        ctx.rec.coverage_tick("drill:roster_delete", skip=True)
        return
    start = date.today() + timedelta(days=3)
    out = {}
    bar = threading.Barrier(2)
    collide = threading.Barrier(2)

    def vacation():
        bar.wait()
        # second barrier: both threads fire at the same instant, a genuine
        # collision with no sleep-based ordering
        collide.wait()
        out["vac"] = sess.post("/request/vacation",
                               {"vac_start": start.isoformat(),
                                "vac_end": (start + timedelta(days=2)).isoformat()})

    def delete():
        bar.wait()
        collide.wait()
        out["del"] = ctx.mgr_sessions[0].post(
            f"/manager/roster/{uid_}/delete", {"confirm": "yes"})

    ths = [threading.Thread(target=vacation), threading.Thread(target=delete)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    orphan = ctx.world.q("SELECT id FROM requests WHERE user_id=?", (uid_,))
    if orphan:
        ctx.rec.issue("HIGH", "orphan_row",
                      f"drill roster_delete: requests remain for deleted "
                      f"user {target} (id {uid_})",
                      {"remaining": [r["id"] for r in orphan]},
                      phase=ctx.phase, drill="roster_delete",
                      fingerprint_extra=f"orphan_user:{uid_}")
    if target in ctx.hires_added:
        ctx.hires_added.remove(target)
    ctx.rec.coverage_tick("drill:roster_delete",
                          mismatch=bool(orphan))


def drill_double_vacation(ctx):
    """Same employee fires two overlapping vacation requests at once."""
    donor = ctx.rng.choice(ctx.emp_sessions)
    s1 = fresh_session(ctx.rec, ctx.base, ctx.world, donor.actor, ctx.phase)
    s2 = fresh_session(ctx.rec, ctx.base, ctx.world, donor.actor, ctx.phase)
    start = date.today() + timedelta(days=ctx.rng.randint(10, 20))
    bar = threading.Barrier(2)
    out = {}

    def v1():
        bar.wait()
        out[0] = s1.post("/request/vacation",
                         {"vac_start": start.isoformat(),
                          "vac_end": (start + timedelta(days=2)).isoformat()})

    def v2():
        bar.wait()
        out[1] = s2.post("/request/vacation",
                         {"vac_start": (start + timedelta(days=1)).isoformat(),
                          "vac_end": (start + timedelta(days=4)).isoformat()})

    ths = [threading.Thread(target=v1), threading.Thread(target=v2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    dup = ctx.world.q(
        "SELECT COUNT(*) c FROM requests WHERE user_id=? AND kind='vacation' "
        "AND vacation_start<=? AND vacation_end>=? AND "
        "status IN ('approved','approved_ok')",
        (donor.uid, (start + timedelta(days=4)).isoformat(), start.isoformat()))
    if dup and dup[0]["c"] > 1:
        ctx.rec.issue("MEDIUM", "duplicate_invite",
                      f"{donor.actor}: {dup[0]['c']} overlapping vacation "
                      "requests accepted (no duplicate guard)",
                      {"start": start.isoformat()},
                      actor=donor.actor, phase=ctx.phase,
                      drill="double_vacation",
                      fingerprint_extra=f"dup_vac:{donor.uid}")
    ctx.rec.coverage_tick("drill:double_vacation")


def drill_unassign_vs_accept(ctx):
    """Manager unassigns the SOURCE of a swap invite while invitee accepts."""
    req, requester, invitee = ensure_pending_invite(ctx)
    if not req or not invitee:
        ctx.rec.coverage_tick("drill:unassign_vs_accept", skip=True)
        return
    source = req["shift_id"]
    holder = req["user_id"]
    inv = fresh_session(ctx.rec, ctx.base, ctx.world, invitee.actor, ctx.phase)
    bar = threading.Barrier(2)
    out = {}

    def accept():
        bar.wait()
        out["a"] = inv.post(f"/request/{req['id']}/respond",
                            {"decision": "accept"})

    def unassign():
        bar.wait()
        out["u"] = ctx.mgr_sessions[1].post(
            f"/manager/unassign/{source}/{holder}")

    ths = [threading.Thread(target=accept), threading.Thread(target=unassign)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    ghost = ctx.world.q("SELECT COUNT(*) c FROM assignments WHERE shift_id=? "
                        "AND user_id=?", (source, holder))
    if ghost and ghost[0]["c"] > 0:
        ctx.rec.issue("HIGH", "orphan_assignment",
                      f"drill unassign_vs_accept: unassigned assignment "
                      f"(shift {source}, user {holder}) re-appeared",
                      {}, phase=ctx.phase, drill="unassign_vs_accept",
                      fingerprint_extra=f"ghost:{source}:{holder}")
    ctx.rec.coverage_tick("drill:unassign_vs_accept",
                          mismatch=bool(ghost and ghost[0]["c"] > 0))


DRILLS = [
    ("double_review", drill_double_review),
    ("sick_vs_swap", drill_sick_vs_swap),
    ("delete_vs_accept", drill_delete_shift_vs_accept),
    ("pick_storm", drill_pick_storm),
    ("roster_delete", drill_roster_delete_vs_activity),
    ("double_vacation", drill_double_vacation),
    ("unassign_vs_accept", drill_unassign_vs_accept),
]


# ---------------------------------------------------------------- run ctx
def fresh_session(rec, base, world, username, phase):
    """A separately-logged-in session for the same user. Drills must act on
    their own session so concurrent ambient actions of the same user cannot
    interleave flash queues (per-session server state)."""
    s = WSession(base, rec, username, phase)
    if not s.login(username, world.password(username, seed_password(username))):
        rec.issue("HIGH", "behavior_mismatch",
                  f"{username}: fresh_session login failed; the drill will "
                  "run unauthenticated",
                  {"username": username}, actor=username, phase=phase,
                  fingerprint_extra=f"fresh_session_login:{username}")
    s.uid = world.uid(username)
    s.station = world.station(username)
    return s


class Ctx:
    """Everything a behavior/drill needs."""

    def __init__(self, sess, world, rec, rng, week, base_password):
        self.sess = sess
        self.base = getattr(sess, "base", "")
        self.world = world
        self.rec = rec
        self.rng = rng
        self.week = week
        self.base_password = base_password
        self.phase = phase_name
        self.uid = sess.uid if hasattr(sess, "uid") else None
        self.station = world.station(sess.actor) if sess.actor else "front"
        self.emp_sessions: list[WSession] = []
        self.mgr_sessions: list[WSession] = []
        self.hires_added: list = []
        self.hire_index = 0


def worker_loop(kind, session, world, rec, behaviors, stop, rng, base_password):
    """Round-robin over the behavior matrix until stopped."""
    uid_ = world.uid(session.actor)
    cycle = behaviors * 3  # repeat matrix; guarantees coverage
    i = 0
    while not stop.is_set():
        fn = cycle[i % len(cycle)]
        i += 1
        ctx = Ctx(session, world, rec, rng, world_week, base_password)
        ctx.uid = uid_
        ctx.emp_sessions = worker_loop.emp_sessions
        ctx.mgr_sessions = worker_loop.mgr_sessions
        name = getattr(fn, "__name__", "behavior")
        try:
            fn(ctx)
        except Exception:
            tb = traceback.format_exc()
            rec.issue("MEDIUM", "harness_exception",
                      f"{session.actor}: {name} raised",
                      {"traceback": tb}, actor=session.actor,
                      phase=phase_name, fingerprint_extra=f"{name}")
        # pacing: behaviors take a few seconds of wall time each
        stop.wait(rng.uniform(0.5, 2.0))


world_week = ""  # set in main
phase_name = "ambient"


class StopEvent:
    """Event + sleep that can be interrupted."""

    def __init__(self):
        self._ev = threading.Event()

    def is_set(self):
        return self._ev.is_set()

    def set(self):
        self._ev.set()

    def wait(self, t):
        self._ev.wait(t)


def auditor_loop(world, rec, stop, interval=5.0):
    while not stop.is_set():
        run_audit(world, rec, phase_name)
        stop.wait(interval)


# ---------------------------------------------------------------- snapshots
def take_snapshots(base, run_dir, mgr_session, emp_session, rec, limit=8):
    """Save HTML snapshots + rendered PNGs for the worst issues."""
    snap_dir = run_dir / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    chrome = shutil.which("google-chrome-stable") or \
        shutil.which("google-chrome") or shutil.which("chromium") or \
        shutil.which("chromium-browser")
    issues = sorted(rec.issues, key=lambda i: (
        {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}[i["severity"]],
        -i["ts"]))[:limit]
    for n, issue in enumerate(issues, 1):
        sess = mgr_session if issue["category"] not in (
            "behavior_mismatch",) else emp_session
        for label, path in (("manager", "/manager"), ("employee", "/")):
            if sess is None:
                continue
            r = sess.get(path)
            if not r.html:
                continue
            html_file = snap_dir / f"issue-{n:03d}-{label}-{path.strip('/') or 'dash'}.html"
            html_file.write_text(r.html)
            if chrome:
                port = urllib.parse.urlparse(base).port
                fixed = re.sub(r'(href|src)="/static/',
                               rf'\1="http://127.0.0.1:{port}/static/', r.html)
                tmp = snap_dir / (html_file.name + ".tmp")
                tmp.write_text(fixed)
                png = html_file.with_suffix(".png")
                try:
                    subprocess.run(
                        [chrome, "--headless=new", "--no-sandbox",
                         "--disable-gpu", "--disable-dev-shm-usage",
                         "--hide-scrollbars", "--window-size=1440,2600",
                         "--virtual-time-budget=6000",
                         f"--screenshot={png}", tmp.as_uri()],
                        capture_output=True, timeout=60)
                except Exception:
                    pass
                tmp.unlink(missing_ok=True)
                issue.setdefault("snapshots", []).append(
                    {"png": str(png), "html": str(html_file),
                     "page": label + ":" + path})
            else:
                issue.setdefault("snapshots", []).append(
                    {"html": str(html_file), "page": label + ":" + path})


# ---------------------------------------------------------------- reporter
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}


def write_reports(rec, run_dir, args, cfg):
    issues = sorted(rec.issues, key=lambda i: (SEV_ORDER.get(i["severity"], 9),
                                               -i["count"]))
    issue_dir = run_dir / "issues"
    issue_dir.mkdir(exist_ok=True)
    for issue in issues:
        lines = [
            f"# {issue['id']}: {issue['title']}",
            "",
            f"- **Severity:** {issue['severity']}  **Category:** `{issue['category']}`",
            f"- **Detected:** {datetime.fromtimestamp(issue['ts']).isoformat()} "
            f"(phase: {issue['phase']}, drill: {issue['drill']}, actor: {issue['actor']})",
            f"- **Occurrences:** {issue['count']}  "
            f"**Confirmed in final state:** {'YES' if issue['confirmed'] else 'no (transient / mid-run only)'}",
            "",
            "## Detail",
            "```json",
            json.dumps(issue["detail"], indent=2, default=str),
            "```",
            "",
            "## Suspected code locations",
        ]
        for hint in HOTSPOTS.get(issue["category"], []):
            lines.append(f"- {hint}")
        server_hits = []
        sl = run_dir / "serverlog.jsonl"
        if sl.exists():
            for line in sl.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("error") and abs(ev.get("ts", 0) - issue["ts"]) < 2.0:
                    server_hits.append(ev)
        if server_hits:
            lines += ["", "## Server-side errors near this timestamp", "```"]
            for ev in server_hits[:5]:
                lines.append(f"{ev.get('ts')} {ev.get('method')} "
                             f"{ev.get('path')} -> {ev.get('status')}")
                if ev.get("error"):
                    lines.append(ev["error"])
            lines.append("```")
        snaps = issue.get("snapshots") or []
        if snaps:
            lines += ["", "## Snapshots"]
            for s in snaps:
                lines.append(f"- `{s.get('png', s.get('html'))}` ({s['page']})")
        lines += ["", "## Notes for the fixing agent",
                  "- Re-run: `python tools/liveweek.py --seed "
                  f"{cfg['seed']}` (events in `eventlog.jsonl` are "
                  "time-ordered actor actions).",
                  "- Fix NOTHING here: this issue is a report of observed "
                  "app behavior; locate the race/missing guard, add a "
                  "regression test in `tests/`, then fix the code."]
        (issue_dir / f"{issue['id']}.md").write_text("\n".join(lines) + "\n")

    cov = rec.coverage
    cov_rows = "".join(
        f"| {name} | {c['attempts']} | {c['skips']} | {c['mismatch']} |\n"
        for name, c in sorted(cov.items()))
    crit = sum(1 for i in issues if i["severity"] == "CRITICAL")
    high = sum(1 for i in issues if i["severity"] == "HIGH")
    med = sum(1 for i in issues if i["severity"] == "MEDIUM")
    info = sum(1 for i in issues if i["severity"] == "INFO")
    report = f"""# ShiftWise Live-Week Report

Run: `{run_dir.name}` | seed `{cfg['seed']}` | {cfg['employees']} employees + \
{cfg['managers']} managers | duration {cfg['duration']}s | \
DB `{run_dir / 'liveweek.db'}`

## Result

| Severity | Count |
|---|---|
| CRITICAL | {crit} |
| HIGH | {high} |
| MEDIUM | {med} |
| INFO | {info} |

{'**No app-level issues were recorded — run is clean.**' if not issues else ''}

## Issues (see `issues/*.md` for evidence + code locations)

| ID | Sev | Category | Title | Count | Confirmed |
|---|---|---|---|---|---|
""" + "\n".join(
        f"| {i['id']} | {i['severity']} | `{i['category']}` | {i['title']} | "
        f"{i['count']} | {'yes' if i['confirmed'] else 'no'} |" for i in issues
    ) + f"""

## Scenario coverage matrix

| behavior | attempts | skipped | unexpected |
|---|---|---|---|
{cov_rows}
## What this run exercised

- Warm-up: every employee ranked the full week; manager rebuilt the schedule.
- Ambient phase ({cfg['duration']}s): {cfg['employees']} employee threads + 2 manager threads \
cycling the full behavior matrix (picks, swaps, invites, sick calls, vacations, \
day-offs, switches, roster edits, triage, probes).
- Collision drills: {', '.join(d[0] for d in DRILLS)}.
- Final audit: DB integrity + cross-table invariants recomputed from \
`liveweek.db`.

## For agents

1. Read `issues/ISSUE-*.md` files top-down (CRITICAL first).
2. Cross-reference actor actions in `eventlog.jsonl` around each issue ts.
3. Server tracebacks: `serverlog.jsonl` (`error` field).
4. Reproduce: `.venv/bin/python tools/liveweek.py --seed {cfg['seed']}` \
(artifacts land in a fresh `run-*` dir; `--strict` makes CI fail on findings).
"""
    (run_dir / "REPORT.md").write_text(report)
    summary = {
        "config": cfg,
        "issues": [{k: v for k, v in i.items() if k != "detail"} for i in issues],
        "coverage": dict(rec.coverage),
        "counts": {"CRITICAL": crit, "HIGH": high, "MEDIUM": med, "INFO": info},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2,
                                                     default=str))
    return issues


# ---------------------------------------------------------------- boot
def boot_server(db_path: Path, run_dir: Path):
    """Import the app with the harness DB, install capture hooks, start a
    real threaded WSGI server on an ephemeral port."""
    os.environ.setdefault("SHIFTWISE_DB_PATH", str(db_path))
    import app as appmod  # noqa: PLC0415  (side-effect import on purpose)
    appmod.DB_PATH = db_path
    rec = boot_server.rec

    def _after(resp):
        try:
            from flask import request
            rec.server_event(method=request.method, path=request.path,
                             status=resp.status_code, error=None)
        except Exception:
            pass
        return resp

    def _on_exc(sender, exception, **kw):
        tb = "".join(traceback.format_exception(
            type(exception), exception, exception.__traceback__))
        try:
            from flask import request
            rec.server_event(method=request.method, path=request.path,
                             status=500, error=tb)
        except Exception:
            rec.server_event(error=tb)

    appmod.app.after_request(_after)
    from flask import got_request_exception
    got_request_exception.connect(_on_exc, appmod.app)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, appmod.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return appmod, srv, f"http://127.0.0.1:{srv.server_port}"


def seed_world(appmod, db_path: Path, rec):
    """mock roster (10 employees) + 5 extra employees + 1 extra manager."""
    import mock_seed
    appmod.DB_PATH = db_path
    appmod.init_db(seed_demo=True)
    mock_seed.seed(appmod)
    appmod.init_db(seed_demo=True)  # migration/hash pass, like tools/gauntlet.py
    con = appmod.db()
    con.execute(
        "INSERT INTO users (username, password, name, role, weekly_hours, "
        "employment_type, hired_on, station, email, phone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ops2", appmod.generate_password_hash("liveweek-manager-pass"),
         "Ops Manager 2", "manager", 40, "full_time", "2019-01-01",
         "back", "ops2@example.com", "+15550000000"))
    for i, (username, station) in enumerate(EXTRA_EMPLOYEES):
        con.execute(
            "INSERT INTO users (username, password, name, role, weekly_hours, "
            "employment_type, hired_on, station, email, phone) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (username,
             appmod.generate_password_hash(f"liveweek-pass-{username}"),
             username.title() + " Liveweek", "employee", 20 + i,
             "part_time", "2026-01-05", station,
             f"{username}@example.com", f"+1555000001{i}"))
    con.commit()
    con.close()


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    global world_week, phase_name
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--duration", type=int, default=120,
                    help="ambient simulation seconds (default 120)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when CRITICAL/HIGH findings exist")
    ap.add_argument("--no-screenshots", action="store_true")
    args = ap.parse_args(argv)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(
        2 ** 31)
    rng = random.Random(seed)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) if args.out else \
        ROOT_DIR / "tools" / "liveweek-artifacts" / f"run-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "liveweek.db"

    rec = Recorder(run_dir)
    boot_server.rec = rec  # hook wiring needs the recorder before import

    print(f"liveweek: artifacts -> {run_dir}")
    print(f"liveweek: seed={seed}")
    try:
        appmod, srv, base = boot_server(db_path, run_dir)
    except Exception:
        print("harness failed to boot server:\n" + traceback.format_exc())
        return 1
    try:
        seed_world(appmod, db_path, rec)
        world = World(db_path, rec)
        world_week = appmod.monday_of(date.today()).isoformat()
        import mock_seed
        expected_count = len(mock_seed.PREFS) + len(EXTRA_EMPLOYEES)
        usernames = world.usernames()
        if len(usernames) != expected_count:
            print(f"harness seed error: expected {expected_count} employees, got "
                  f"{len(usernames)}: {usernames}")
            return 1
        print(f"liveweek: server {base}, week {world_week}, "
              f"{len(usernames)} employees: {', '.join(usernames)}")

        # --- sessions
        emp_sessions: list[WSession] = []
        for u in usernames:
            s = WSession(base, rec, u, "warmup")
            pw = world.password(u, default_password(u))
            if not s.login(u, pw):
                rec.issue("HIGH", "behavior_mismatch",
                          f"warmup: login failed for {u}", {},
                          actor=u, phase="warmup", fingerprint_extra=f"login:{u}")
            s.uid = world.uid(u)
            s.station = world.station(u)
            emp_sessions.append(s)
        mgr_sessions = []
        for u, pw in (("manager", "manager"), ("ops2", "liveweek-manager-pass")):
            s = WSession(base, rec, u, "warmup")
            if not s.login(u, pw):
                rec.issue("HIGH", "behavior_mismatch",
                          f"warmup: manager login failed for {u}", {},
                          actor=u, phase="warmup", fingerprint_extra=f"login:{u}")
            s.uid = world.uid(u)
            s.station = world.station(u)
            mgr_sessions.append(s)

        # --- warm-up: everyone picks, manager rebuilds
        print("liveweek: warm-up (full-week picks + scheduler rebuild)")
        for s in emp_sessions:
            ctx = Ctx(s, world, rec, rng, world_week,
                      world.password(s.actor, default_password(s.actor)))
            b_full_picks(ctx)
        warm_ctx = Ctx(mgr_sessions[0], world, rec, rng, world_week, "manager")
        m_rebuild(warm_ctx)
        run_audit(world, rec, "warmup")

        # --- ambient + drills
        stop = StopEvent()
        worker_loop.emp_sessions = emp_sessions
        worker_loop.mgr_sessions = mgr_sessions
        threads = []
        for s in emp_sessions:
            th = threading.Thread(
                target=worker_loop,
                args=("employee", s, world, rec, EMPLOYEE_BEHAVIORS, stop,
                      random.Random(rng.randrange(2 ** 31)),
                      world.password(s.actor, default_password(s.actor))),
                daemon=True)
            threads.append(th)
        for s in mgr_sessions:
            th = threading.Thread(
                target=worker_loop,
                args=("manager", s, world, rec, MANAGER_BEHAVIORS, stop,
                      random.Random(rng.randrange(2 ** 31)), "manager"),
                daemon=True)
            threads.append(th)
        aud = threading.Thread(target=auditor_loop, args=(world, rec, stop),
                               daemon=True)
        threads.append(aud)
        for th in threads:
            th.start()

        phase_name = "drills"
        drills_at = time.monotonic() + args.duration * 0.4
        deadline = time.monotonic() + args.duration
        # invitee for drills: pick a mid-roster employee session
        drill_ctx = Ctx(emp_sessions[0], world, rec, rng, world_week, None)
        drill_ctx.emp_sessions = emp_sessions
        drill_ctx.mgr_sessions = mgr_sessions
        while time.monotonic() < deadline:
            if time.monotonic() >= drills_at:
                print("liveweek: collision drills")
                for name, fn in DRILLS:
                    if stop.is_set():
                        break
                    phase_name = f"drill:{name}"
                    print(f"  drill: {name}")
                    try:
                        fn(drill_ctx)
                    except Exception:
                        tb = traceback.format_exc()
                        rec.issue("MEDIUM", "harness_exception",
                                  f"drill {name} raised",
                                  {"traceback": tb}, phase=phase_name,
                                  drill=name, fingerprint_extra=f"drill:{name}")
                    run_audit(world, rec, phase_name)
                    time.sleep(1.0)
                drills_at = time.monotonic() + 10 ** 9  # once per run
            time.sleep(0.2)

        # --- final storm: everyone + both managers at once
        phase_name = "storm"
        print(f"liveweek: final storm (all {len(usernames)} picks + 2 rebuilds, concurrent)")
        storm_sessions = [fresh_session(rec, base, world, s.actor, "storm")
                          for s in emp_sessions]
        storm_mgrs = [fresh_session(rec, base, world, s.actor, "storm")
                      for s in mgr_sessions]
        storm_conds = [
            ("pick", s, lambda s=s: s.post("/pick", storm_pick_data(world,
             world_week, s.station, rng)))
            for s in storm_sessions
        ] + [("run", s, lambda s=s: s.post("/manager/run"))
             for s in storm_mgrs]
        bar = threading.Barrier(len(storm_conds))
        storm_out = []

        def do(cond):
            bar.wait()
            storm_out.append(cond[2]())

        ths = [threading.Thread(target=do, args=(c,)) for c in storm_conds]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        fives = [r for r in storm_out if r.status >= 500]
        if fives:
            rec.issue("CRITICAL", "http_500",
                      f"final storm: {len(fives)} requests returned 5xx",
                      {"statuses": [r.status for r in fives]},
                      phase="storm", drill="final_storm",
                      fingerprint_extra="final_storm")

        # --- final audit, snapshots, reports
        stop.set()
        for th in threads:
            th.join(timeout=10)
        phase_name = "final"
        final_findings = run_audit(world, rec, "final")
        # findings whose fingerprint persists in the final DB snapshot are
        # real, persistent issues; everything else was transient mid-run
        INVARIANT_CATEGORIES = {
            "orphan_assignment", "orphan_row", "capacity_violation",
            "status_invalid", "duplicate_invite", "invite_inconsistent",
            "dayoff_violated", "vacation_violated", "hours_cap", "days_off",
            "rank_collision", "integrity",
        }
        final_keys = {key for _, _, key, _ in final_findings}
        for issue in rec.issues:
            if issue["category"] in INVARIANT_CATEGORIES:
                key = issue["title"].split(": ", 1)[-1]
                issue["confirmed"] = key in final_keys

        if not args.no_screenshots:
            print("liveweek: snapshots")
            take_snapshots(base, run_dir, mgr_sessions[0], emp_sessions[0], rec)

        issues = write_reports(rec, run_dir, args, {
            "seed": seed, "duration": args.duration, "employees": len(usernames),
            "managers": 2, "base": base, "week": world_week,
            "strict": args.strict})
        n_crit = sum(1 for i in issues if i["severity"] == "CRITICAL")
        n_high = sum(1 for i in issues if i["severity"] == "HIGH")
        print(f"liveweek: done -> {run_dir / 'REPORT.md'} "
              f"({n_crit} critical, {n_high} high, "
              f"{sum(1 for i in issues if i['severity'] == 'MEDIUM')} medium)")
        if args.strict and (n_crit or n_high):
            return 2
        return 0
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass


def storm_pick_data(world, week, station, rng):
    shifts = world.week_shifts(week, station)
    n = len(shifts)
    if not n:
        return {}
    ranks = list(range(1, n + 1))
    rng.shuffle(ranks)
    return {f"rank_{s['id']}": str(rk) for s, rk in zip(shifts, ranks)}


if __name__ == "__main__":
    sys.exit(main())
