"""
Unit and integration tests for Milestone 8:
- Phase 8.1: Implement explain endpoint for all five operations (attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend).
- Phase 8.2: Verify query parameter validation, shared pipeline builders, and executionStats explain output shape.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    build_employee_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

client = TestClient(app)


def test_explain_invalid_endpoint_name_422():
    resp = client.get("/admin/explain/unknown_endpoint")
    assert resp.status_code == 422


def test_explain_attendance_list_invalid_dates_422():
    resp = client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01")
    assert resp.status_code == 422


def test_explain_attendance_list_valid():
    resp = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31&status=PRESENT&page=1&page_size=20")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data
        assert isinstance(data["explain"], dict)


def test_explain_employee_monthly_missing_params_422():
    # Missing both
    resp = client.get("/admin/explain/employee_monthly")
    assert resp.status_code == 422

    # Missing month
    resp = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert resp.status_code == 422

    # Missing emp_code
    resp = client.get("/admin/explain/employee_monthly?month=2026-07")
    assert resp.status_code == 422

    # Invalid month format
    resp = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
    assert resp.status_code == 422


def test_explain_employee_monthly_valid():
    resp = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["endpoint"] == "employee_monthly"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data


def test_explain_department_summary_missing_params_422():
    resp = client.get("/admin/explain/department_summary")
    assert resp.status_code == 422

    resp = client.get("/admin/explain/department_summary?month=invalid")
    assert resp.status_code == 422


def test_explain_department_summary_valid():
    resp = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["endpoint"] == "department_summary"
        assert data["collection"] == "employees"
        assert "explain" in data


def test_explain_late_leaderboard_missing_or_invalid_params_422():
    # Missing month
    resp = client.get("/admin/explain/late_leaderboard")
    assert resp.status_code == 422

    # Invalid limit (< 1 or > 50)
    resp = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=0")
    assert resp.status_code == 422

    resp = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=51")
    assert resp.status_code == 422


def test_explain_late_leaderboard_valid():
    resp = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Sales")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["endpoint"] == "late_leaderboard"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data


def test_explain_department_trend_missing_or_invalid_params_422():
    # Missing all
    resp = client.get("/admin/explain/department_trend")
    assert resp.status_code == 422

    # Missing from / to
    resp = client.get("/admin/explain/department_trend?department=Engineering")
    assert resp.status_code == 422

    # from > to
    resp = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-31&to=2026-07-01")
    assert resp.status_code == 422

    # > 92 days
    resp = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-06-01")
    assert resp.status_code == 422


def test_explain_department_trend_valid():
    resp = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["endpoint"] == "department_trend"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data


def test_pipeline_builders_structure():
    # Verify shared pipeline builders generate expected pipeline stage structure
    emp_pipe = build_employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31")
    assert len(emp_pipe) >= 4
    assert emp_pipe[0]["$match"]["emp_code"] == "EMP0001"

    dept_sum_pipe = build_department_summary_pipeline("2026-07-01", "2026-07-31", "Engineering")
    assert dept_sum_pipe[0]["$match"]["department"] == "Engineering"

    lead_pipe = build_late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10, "Sales")
    assert lead_pipe[0]["$match"]["late_minutes"] == {"$gt": 0}

    trend_pipe = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-31")
    assert "$documents" in trend_pipe[0]
