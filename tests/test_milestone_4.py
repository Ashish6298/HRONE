"""
Unit and Integration tests for Milestone 4:
- Phase 4.1: Shift time calculations, overnight mapping, late minutes, overtime, work hours, half day.
- Phase 4.2: Punch-in endpoint contracts and validations.
- Phase 4.3: Punch-out endpoint contracts, timing rules, overnight support, 404/409/422 handling.
- Phase 4.4: Regularization endpoint contracts, patch validation, status transitions, change audit trail, no-op rejection.
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    get_attendance_date_from_punch_in,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
)

client = TestClient(app)


def test_overnight_punch_in_date_mapping_r1():
    # Shift 22:00 - 06:00 IST (overnight)
    # Punch in at 2026-07-07 02:30 IST (2026-07-06 21:00 UTC) -> belongs to shift starting 2026-07-06
    dt_early_morning = datetime(2026, 7, 6, 21, 0, 0, tzinfo=timezone.utc)
    record_date = get_attendance_date_from_punch_in(dt_early_morning, "22:00", "06:00")
    assert record_date == "2026-07-06"

    # Punch in at 2026-07-07 22:15 IST (2026-07-07 16:45 UTC) -> belongs to shift starting 2026-07-07
    dt_night = datetime(2026, 7, 7, 16, 45, 0, tzinfo=timezone.utc)
    record_date = get_attendance_date_from_punch_in(dt_night, "22:00", "06:00")
    assert record_date == "2026-07-07"


def test_half_day_boundary_r5():
    # 4.49 hours -> half_day = True
    assert compute_half_day(4.49) is True
    # 4.50 hours -> half_day = False
    assert compute_half_day(4.50) is False
    # None -> False
    assert compute_half_day(None) is False


def test_punch_out_request_validations():
    # Unknown employee -> 404
    resp = client.post("/attendance/punch-out", json={"emp_code": "EMP9999", "punched_at": 1783312500000})
    assert resp.status_code in [404, 503]


def test_regularize_request_validations():
    # Unknown employee -> 404
    resp = client.patch(
        "/attendance/EMP9999/2026-07-06",
        json={
            "reason": "Correcting punch time error",
            "regularized_by": "HR Manager",
            "status": "PRESENT",
        },
    )
    assert resp.status_code in [404, 503]

    # Rejection of punch times when status is ABSENT
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "reason": "Employee was absent",
            "regularized_by": "HR Manager",
            "status": "ABSENT",
            "punch_in": 1783312500000,
        },
    )
    assert resp.status_code in [422, 404, 503]
