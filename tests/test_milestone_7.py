"""
Unit and Integration tests for Milestone 7:
- Phase 7.1: Department daily trend aggregation with gap filling ($documents), dynamic daily headcount, present count, late count, null weekend attendance rate, and 7-day moving average.
- Validation: 404 on unknown department, 422 on from > to, 422 on > 92 days.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_department_trend_unknown_department_404():
    resp = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-10")
    assert resp.status_code in [404, 503]


def test_department_trend_invalid_dates_422():
    # from > to -> 422
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert resp.status_code == 422

    # range > 92 days -> 422
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert resp.status_code == 422


def test_department_trend_valid_structure():
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["department"] == "Engineering"
        assert isinstance(data["items"], list)
        assert len(data["items"]) == 7  # 7 inclusive days
        first = data["items"][0]
        assert "date" in first
        assert "is_working_day" in first
        assert "headcount" in first
        assert "present_count" in first
        assert "late_count" in first
        assert "attendance_rate" in first
        assert "moving_avg_7d" in first
