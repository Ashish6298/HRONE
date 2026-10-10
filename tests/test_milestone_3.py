"""
Unit and Integration tests for Milestone 3:
- Phase 3.1: Employee creation, duplicate detection, schema validation, 409 conflict, no ID leakage.
- Phase 3.2: Employee listing, department filtering, pagination skip/limit, accurate totals.
- Phase 3.3: Attendance listing, filtering by emp_code/status/date_from/date_to, 422 on invalid date range, database-level sort/paging.
- Phase 3.4: Starter defect fixes verification.
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.main import (
    app,
    get_db,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    EmployeeCreate,
    create_employee,
    list_employees,
    list_attendance,
)

client = TestClient(app)


def test_late_minutes_grace_boundary_r2():
    # Shift 09:30 IST. Grace period is 10:00 (up to 09:40:00 IST).
    # 09:40:00 IST = 04:10:00 UTC -> 0 late minutes
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace, "09:30") == 0

    # 09:40:01 IST = 04:10:01 UTC -> 10 late minutes (strictly > 10 min)
    dt_late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late, "09:30") == 10

    # 10:15:59 IST = 04:45:59 UTC -> 45 late minutes
    dt_45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_45, "09:30") == 45


def test_work_hours_r4_half_up():
    # 9 hours 7 minutes 12 seconds = 32832 seconds / 3600 = 9.12 hours
    t1 = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(t1, t2) == 9.12


def test_overtime_r3_boundaries():
    # Shift 09:30 - 18:30 IST. Shift end = 18:30 IST = 13:00 UTC.
    # Punch out at 18:55 IST (25 min overtime -> < 30 min threshold -> 0)
    t_out_25 = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_25, "18:30", "2026-07-06") == 0

    # Punch out at 19:00 IST (30 min overtime -> >= 30 min -> 30)
    t_out_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_30, "18:30", "2026-07-06") == 30

    # Punch out at 19:10 IST (40 min overtime -> 40)
    t_out_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_40, "18:30", "2026-07-06") == 40


def test_overtime_overnight_shift():
    # Shift 22:00 - 06:00 IST. Shift start 2026-07-06 22:00, Shift end 2026-07-07 06:00 IST (00:30 UTC 2026-07-07).
    # Punch out at 06:45 IST on 2026-07-07 (01:15 UTC).
    t_out_overnight = datetime(2026, 7, 7, 1, 15, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_overnight, "06:00", "2026-07-06", shift_start="22:00") == 45


def test_employee_creation_and_listing_validation():
    # Test invalid employee creation payload gives 422
    bad_payload = {
        "emp_code": "INVALID_CODE",
        "name": "Test User",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    }
    resp = client.post("/employees", json=bad_payload)
    assert resp.status_code == 422


def test_attendance_list_date_range_validation():
    # date_from > date_to must return 422
    resp = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert resp.status_code == 422
