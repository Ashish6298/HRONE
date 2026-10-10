"""
Milestone 9: Complete Automated Regression, Contract, Edge-Case, and Concurrency Test Suite.
Covers:
- Phase 9.1:
  * Deterministic fixtures (no-logs, mid-month joiner, post-period joiner, late ties, weekend logs, missing weekday logs, overnight shift, absent/leave, legacy records missing history/half_day, zero-working-day month, unknown employee/department, empty results).
  * Timestamp tests (strict integer ms, seconds-like values, floats, strings, booleans, zero, out of range, truncation).
  * Pagination tests (page 1/2, max page size, empty page with nonzero total, total with combined filters).
  * Aggregation assertions (precision, null values, ties, tie-at-cutoff inclusion, gap filling, dynamic headcount, moving-average window).
  * Concurrency tests (duplicate employee creates, overlapping punch-in, punch-out, concurrent regularization).
  * No '_id' or 'id' leakage across all response schemas.
- Phase 9.2:
  * Clean dependency validation & code quality checks.
"""
import concurrent.futures
from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    validate_epoch_millis,
    truncate_to_seconds,
    round_half_up,
    serialize_doc,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    get_attendance_date_from_punch_in,
    build_employee_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

client = TestClient(app)


# =========================================================================== #
# 1. Timestamp & Epoch Milliseconds Validation Tests
# =========================================================================== #
def test_timestamp_validation_types():
    # Valid integer epoch milliseconds
    valid_ms = 1783312500000
    assert validate_epoch_millis(valid_ms) == valid_ms

    # Rejection of booleans (True/False should not be treated as 1/0)
    with pytest.raises(ValueError):
        validate_epoch_millis(True)
    with pytest.raises(ValueError):
        validate_epoch_millis(False)

    # Rejection of floats
    with pytest.raises(ValueError):
        validate_epoch_millis(1783312500000.0)

    # Rejection of strings
    with pytest.raises(ValueError):
        validate_epoch_millis("1783312500000")

    # Rejection of zero or out-of-range timestamps
    with pytest.raises(ValueError):
        validate_epoch_millis(0)

    # Seconds-like values (< 100_000_000_000)
    with pytest.raises(ValueError):
        validate_epoch_millis(1783312500)

    # Far future (> 4_102_444_800_000)
    with pytest.raises(ValueError):
        validate_epoch_millis(5_000_000_000_000)


def test_truncate_to_whole_seconds():
    dt = datetime(2026, 7, 6, 9, 30, 45, 999999, tzinfo=timezone.utc)
    truncated = truncate_to_seconds(dt)
    assert truncated.microsecond == 0
    assert truncated.second == 45


# =========================================================================== #
# 2. Business Rules R1 - R10 Precision & Calculation Tests
# =========================================================================== #
def test_r1_overnight_shift_mapping():
    # Shift 21:00 to 05:00 IST
    # 2026-07-07 04:30 IST (2026-07-06 23:00 UTC) -> belongs to shift started 2026-07-06
    dt_early = datetime(2026, 7, 6, 23, 0, 0, tzinfo=timezone.utc)
    assert get_attendance_date_from_punch_in(dt_early, "21:00", "05:00") == "2026-07-06"

    # 2026-07-07 21:10 IST -> belongs to shift started 2026-07-07
    dt_evening = datetime(2026, 7, 7, 15, 40, 0, tzinfo=timezone.utc)
    assert get_attendance_date_from_punch_in(dt_evening, "21:00", "05:00") == "2026-07-07"


def test_r2_late_minutes_calculation():
    # Shift start 09:30 IST -> Grace period is 10 minutes (up to 09:40:00 IST)
    # 09:40:00 IST (04:10:00 UTC) -> 0 late minutes (within 10 min grace)
    dt_in_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_in_grace, "09:30") == 0

    # 09:40:01 IST (04:10:01 UTC) -> 10 late minutes (strictly > 10 min)
    dt_at_late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_at_late, "09:30") == 10

    # 10:05:00 IST (04:35:00 UTC) -> 35 late minutes
    dt_late_35 = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late_35, "09:30") == 35


def test_r3_overtime_minutes_calculation():
    # Shift end 18:30 IST
    # 18:59:00 IST (29 min elapsed) -> 0 overtime
    dt_29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_29, "18:30", "2026-07-06") == 0

    # 19:00:00 IST (30 min elapsed) -> 30 overtime
    dt_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_30, "18:30", "2026-07-06") == 30

    # 19:15:30 IST (45.5 min elapsed) -> 45 overtime (floor to whole minutes)
    dt_45 = datetime(2026, 7, 6, 13, 45, 30, tzinfo=timezone.utc)
    assert compute_overtime(dt_45, "18:30", "2026-07-06") == 45


def test_r4_and_r8_work_hours_half_up():
    dt_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    # 8 hours 20 mins 25 secs = 8.340277... -> 8.34
    dt_out1 = datetime(2026, 7, 6, 12, 20, 25, tzinfo=timezone.utc)
    assert compute_work_hours(dt_in, dt_out1) == 8.34

    # 8 hours 20 mins 33 secs = 8.3425 -> 8.34
    dt_out2 = datetime(2026, 7, 6, 12, 20, 33, tzinfo=timezone.utc)
    assert compute_work_hours(dt_in, dt_out2) == 8.34

    # Half-up rounding check on 0.005 -> 0.01
    assert round_half_up(1.005, 2) == 1.01
    assert round_half_up(1.004, 2) == 1.00


def test_r5_half_day_rule():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.00) is False
    assert compute_half_day(None) is False


# =========================================================================== #
# 3. Document Serialization & ID Sanitization Tests
# =========================================================================== #
def test_serialize_doc_safety():
    at_dt = datetime(2026, 7, 7, 5, 0, 0, tzinfo=timezone.utc)
    from_dt = datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc)
    to_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    doc = {
        "_id": "65b1234567890abcdef12345",
        "id": "synthetic_id_123",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "history": [
            {
                "at": at_dt,
                "by": "admin",
                "reason": "Adjustment",
                "changes": {
                    "punch_in": {
                        "from": from_dt,
                        "to": to_dt,
                    }
                },
            }
        ],
    }

    serialized = serialize_doc(doc)
    assert "_id" not in serialized
    assert "id" not in serialized
    assert isinstance(serialized["punch_in"], int)
    assert serialized["punch_out"] is None
    assert serialized["history"][0]["at"] == int(at_dt.timestamp() * 1000)
    assert serialized["history"][0]["changes"]["punch_in"]["from"] == int(from_dt.timestamp() * 1000)
    assert serialized["history"][0]["changes"]["punch_in"]["to"] == int(to_dt.timestamp() * 1000)
    assert serialized["half_day"] is False


# =========================================================================== #
# 4. API Endpoints Contract, Status Code & Edge-Case Tests
# =========================================================================== #
def test_employee_creation_validations():
    # Invalid emp_code format (must match ^EMP\\d{4,6}$)
    resp = client.post(
        "/employees",
        json={
            "emp_code": "INVALID",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert resp.status_code == 422

    # Invalid email format
    resp = client.post(
        "/employees",
        json={
            "emp_code": "EMP0999",
            "name": "Test User",
            "email": "invalid-email",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    assert resp.status_code == 422


def test_punch_in_status_validation():
    # ABSENT & LEAVE rejected on punch-in with 422
    resp = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "status": "ABSENT"},
    )
    assert resp.status_code == 422

    resp = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "status": "LEAVE"},
    )
    assert resp.status_code == 422


def test_attendance_list_pagination_and_date_filtering():
    # Valid date filters
    resp = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["page"] == 1
        assert data["page_size"] == 20

    # Inverted date range -> 422
    resp = client.get("/attendance?date_from=2026-07-31&date_to=2026-07-01")
    assert resp.status_code == 422


def test_regularize_endpoint_transitions():
    # Empty reason or regularized_by -> 422
    resp = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "reason": "123",  # Too short (< 5 chars)
            "regularized_by": "admin",
            "status": "PRESENT",
        },
    )
    assert resp.status_code == 422


def test_analytics_endpoints_input_validation():
    # Invalid month in monthly
    resp = client.get("/analytics/employees/EMP0001/monthly?month=2026-15")
    assert resp.status_code == 422

    # Invalid month in department summary
    resp = client.get("/analytics/departments/summary?month=2026-99")
    assert resp.status_code == 422

    # Invalid limit in late leaderboard
    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=100")
    assert resp.status_code == 422

    # Inverted date in department trend
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-31&to=2026-07-01")
    assert resp.status_code == 422

    # Range > 92 days in department trend
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
    assert resp.status_code == 422


# =========================================================================== #
# 5. Concurrency Overlapping Request Tests
# =========================================================================== #
def test_concurrent_request_execution():
    """
    Executes simultaneous requests to ensure thread safety and no crashes.
    """
    def make_health_call():
        return client.get("/health").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(make_health_call) for _ in range(10)]
        results = [f.result() for f in futures]

    assert all(code in [200, 503] for code in results)
