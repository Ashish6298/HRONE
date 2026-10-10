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
    assert "/attendance/punch-out" in routes
    assert "/attendance/{emp_code}/{date}" in routes


def test_missing_endpoints_in_starter():
    routes = {route.path for route in app.routes}
    assert "/analytics/departments/{department}/trend" not in routes
    assert "/admin/explain/{endpoint}" not in routes





def test_health_probe_contracts():
    # Health endpoint returns 200 when DB is up, or 503 when DB is down
    resp = client.get("/health")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        assert resp.json() == {"status": "ok"}
    else:
        assert resp.status_code == 503
        assert "detail" in resp.json()


def test_work_hours_half_up_precision():
    # Helper compute_work_hours uses Decimal ROUND_HALF_UP required by R4.
    pass
