# Development Guide
> Status: current · Last verified: 2026-09-29

## 1. Environment Setup

ShiftWise requires Python 3.12 or newer.

```sh
# 1. Clone repository and navigate to folder
git clone https://github.com/clain1016/shiftwise-suite.git
cd shiftwise-suite

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install runtime and development dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

---

## 2. Running Locally

### Development Server
To launch the primary application on `127.0.0.1:5000`:

```sh
export SHIFTWISE_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD="your-strong-manager-password"
.venv/bin/python app.py
```

Open `http://127.0.0.1:5000` and sign in with username `manager` and the password specified above.

### Running with Demo Data
To start with a pre-seeded roster and shifts:
```sh
export SHIFTWISE_DEMO_SEED=1
.venv/bin/python app.py
```

---

## 3. Running the Test Suite

Tests use isolated temporary SQLite databases and never alter working or production databases.

### Running All Tests via Script
```sh
for test in test_*.py; do
    echo "Running $test..."
    .venv/bin/python "$test"
done
```

### Running with Pytest
```sh
.venv/bin/pytest -v test_container.py test_review_fixes.py shiftwise-mock/test_lan_bind.py
```

### Running Mock Twin Tests
```sh
.venv/bin/python shiftwise-mock/test_mock.py
.venv/bin/python shiftwise-mock/test_lan_bind.py
```

---

## 4. End-to-End Simulation Harnesses

The repository includes three advanced simulation and chaos-testing scripts:

### 1. Gauntlet (`shiftwise-mock/gauntlet.py`)
An 11-phase deterministic stress test verifying:
- Full 10-person preference submissions
- Partial pick form rejection
- Multiple sick calls and instant auto-cover
- Swaps, vacation ranges, past-dated vacation rejection
- Full shift switch request auto-denial
- Manager unassignment and instant backfills
- Mid-week roster additions and re-ranking

```sh
.venv/bin/python shiftwise-mock/gauntlet.py
```

### 2. Scenario Demo (`shiftwise-mock/scenario_demo.py`)
A 10-phase sequential simulation illustrating conflict handling step-by-step:
```sh
.venv/bin/python shiftwise-mock/scenario_demo.py
```

### 3. House Chaos Simulation (`shiftwise-mock/scenario_random.py`)
A randomized property-based chaos harness:
- Randomly balances roster between FOH and BOH
- Plants invalid cross-house picks to verify isolation
- Injects 10 random lifecycle events (sick, swap, vacation, day off, station flips, cap changes)
- Audits invariants (no cross-house leaks, no over-cap hours)

```sh
# Random run
.venv/bin/python shiftwise-mock/scenario_random.py

# Replay a specific seed
.venv/bin/python shiftwise-mock/scenario_random.py <seed>
```

---

## 5. Development Principles

1. **Keep Invariants Intact:**
   Never bypass house boundaries (`station == area`), weekly hours limits, or the 2-days-off rule.
2. **Backward-Compatible Schema:**
   Add new columns with default values and update `init_db()` migration blocks.
3. **Verify Before Submitting:**
   Always run root tests, mock tests, and the gauntlet before creating PRs.
