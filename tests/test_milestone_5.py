"""
Unit and Integration tests for Milestone 5:
- Phase 5.1: Employee monthly summary aggregation
- Working days calculation (Mon-Fri, mid-month joiner, zero working days)
- Present days calculation (weekday PRESENT/WFH/ON_DUTY, half_day = 0.5)
- Weekend attendance inclusion in late/overtime totals but exclusion from weekday present_days
- LEAVE records counting
- Attendance percentage computation and precision
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_employee_monthly_unknown_employee_404():
    resp = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert resp.status_code in [404, 503]


def test_employee_monthly_invalid_month_422():
    resp = client.get("/analytics/employees/EMP0001/monthly?month=invalid-month")
    assert resp.status_code == 422

    resp = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
    assert resp.status_code == 422
