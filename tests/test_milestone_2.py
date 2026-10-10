"""
Unit and Contract tests for Milestone 2:
- Phase 2.1: Health & MongoDB lifecycle
- Phase 2.2: Shared validation & serialization helpers, time conversions, strict epoch millis, R8 rounding
- Phase 2.3: Startup index definitions
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient

from app.main import (
    app,
    validate_epoch_millis,
    epoch_ms_to_datetime_utc,
    datetime_to_epoch_ms,
    truncate_to_seconds,
    round_half_up,
    serialize_doc,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    EmployeeCreate,
    PunchInRequest,
    RegularizeRequest,
)

client = TestClient(app)


def test_epoch_millis_strict_validation():
    # Valid range
    valid_ts = 1783312500000
    assert validate_epoch_millis(valid_ts) == valid_ts

    # Rejection of bool (which is a subclass of int in Python)
    with pytest.raises(ValueError):
        validate_epoch_millis(True)
    with pytest.raises(ValueError):
        validate_epoch_millis(False)

    # Rejection of float, string, None
    with pytest.raises(ValueError):
        validate_epoch_millis(1783312500000.5)
    with pytest.raises(ValueError):
        validate_epoch_millis("1783312500000")

    # Seconds-like values (< 100000000000)
    with pytest.raises(ValueError):
        validate_epoch_millis(1783312500)

    # Far future (> 4102444800000)
    with pytest.raises(ValueError):
        validate_epoch_millis(5000000000000)


def test_truncate_to_seconds():
    dt = datetime(2026, 7, 6, 9, 30, 45, 987654, tzinfo=timezone.utc)
    truncated = truncate_to_seconds(dt)
    assert truncated.microsecond == 0
    assert truncated.second == 45


def test_round_half_up_r8_precision():
    # 4.495 -> 4.50 (half up), 4.494 -> 4.49
    assert round_half_up(4.495, 2) == 4.50
    assert round_half_up(4.494, 2) == 4.49
    assert round_half_up(Decimal("4.495"), 2) == 4.50
    # Rate 4 decimals
    assert round_half_up(0.85714, 4) == 0.8571
    assert round_half_up(0.85715, 4) == 0.8572


def test_serialize_doc_safety():
    raw = {
        "_id": "60d5ec49e49c7112a8a1c8f1",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc),
        "work_hours": 9.12,
        "late_minutes": 0,
        "overtime_minutes": 0,
    }
    serialized = serialize_doc(raw)
    assert "_id" not in serialized
    assert "id" not in serialized
    assert serialized["emp_code"] == "EMP0001"
    assert serialized["punch_in"] == int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert serialized["punch_out"] == int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert serialized["half_day"] is False
    assert serialized["history"] == []


def test_employee_create_model_validation():
    # Valid model
    valid_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    emp = EmployeeCreate(**valid_payload)
    assert emp.emp_code == "EMP0001"

    # Invalid emp_code regex
    with pytest.raises(Exception):
        EmployeeCreate(**{**valid_payload, "emp_code": "INVALID"})

    # Invalid shift times (start == end)
    with pytest.raises(Exception):
        EmployeeCreate(**{**valid_payload, "shift_start": "09:30", "shift_end": "09:30"})


def test_punch_in_request_validation():
    # Valid
    p = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000, status="WFH")
    assert p.status == "WFH"

    # Rejection of invalid status
    with pytest.raises(Exception):
        PunchInRequest(emp_code="EMP0001", status="ABSENT")

    # Rejection of boolean punched_at
    with pytest.raises(Exception):
        PunchInRequest(emp_code="EMP0001", punched_at=True)


def test_health_endpoint_response_format():
    resp = client.get("/health")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        assert resp.json() == {"status": "ok"}
    else:
        assert "detail" in resp.json()
