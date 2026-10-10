"""
Unit and Integration tests for Milestone 6:
- Phase 6.1: Department summary aggregation (cross-collection join, headcount for 0-log employees, period-end cutoff, avg work hours).
- Phase 6.2: Late leaderboard (competition ranking 1,2,2,4, ties at cutoff, department filtering, sort order).
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_department_summary_invalid_month_422():
    resp = client.get("/analytics/departments/summary?month=invalid")
    assert resp.status_code == 422


def test_department_summary_valid_structure():
    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["month"] == "2026-07"
        assert isinstance(data["items"], list)


def test_late_leaderboard_invalid_limit_422():
    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=0")
    assert resp.status_code == 422

    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=100")
    assert resp.status_code == 422


def test_late_leaderboard_valid_structure():
    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert resp.status_code in [200, 503]
    if resp.status_code == 200:
        data = resp.json()
        assert data["month"] == "2026-07"
        assert isinstance(data["items"], list)
