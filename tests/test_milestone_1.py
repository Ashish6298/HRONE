"""
Baseline and contract tests for Milestone 1 / Phase 1.1 and 1.2
Tests starter behavior, route presence, and defect reproductions.
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app, compute_late_minutes, compute_work_hours, compute_overtime

client = TestClient(app)


def test_starter_routes_present():
    routes = {route.path for route in app.routes}
    assert "/health" in routes
    assert "/employees" in routes
    assert "/attendance/punch-in" in routes
    assert "/attendance" in routes


def test_missing_endpoints_in_starter():
    routes = {route.path for route in app.routes}
    assert "/attendance/punch-out" not in routes
    assert "/attendance/{emp_code}/{date}" not in routes
    assert "/analytics/employees/{emp_code}/monthly" not in routes
    assert "/analytics/departments/summary" not in routes
    assert "/analytics/leaderboard/late" not in routes
    assert "/analytics/departments/{department}/trend" not in routes
    assert "/admin/explain/{endpoint}" not in routes


def test_defect_reproduction_health_always_ok_without_ping():
    # Health endpoint currently returns {"status": "ok"} without actually checking MongoDB
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_defect_reproduction_work_hours_bankers_rounding():
    # Helper compute_work_hours uses built-in round() which does banker's rounding (round-half-to-even)
    # instead of Decimal ROUND_HALF_UP required by R4.
    pass
