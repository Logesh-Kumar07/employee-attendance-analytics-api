from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture

def real_api(monkeypatch: pytest.MonkeyPatch):
    uri = os.getenv("MONGO_TEST_URI")
    if not uri:
        pytest.skip("set MONGO_TEST_URI to run MongoDB integration tests")
    mongo = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3_000)
    mongo.admin.command("ping")
    database_name = f"candidate_kit_test_{uuid.uuid4().hex}"
    database = mongo[database_name]
    monkeypatch.setattr(main, "client", mongo)
    monkeypatch.setattr(main, "db", database)
    try:
        with TestClient(main.app) as test_client:
            yield test_client, database
    finally:
        # The name is random and dedicated to this test run; no supplied database is dropped.
        mongo.drop_database(database_name)
        mongo.close()


def instant(day: str, hour: int, minute: int) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour, minute), tzinfo=IST).astimezone(timezone.utc)


def employee(code: str, department: str, joined_on: str) -> dict:
    return {
        "emp_code": code,
        "name": f"Name {code}",
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": instant(joined_on, 9, 0),
    }


def log(code: str, day: str, status: str, late: int = 0, hours: float | None = None,
        half_day: bool = False, overtime: int = 0) -> dict:
    punch_in = instant(day, 10, 5) if status in ("PRESENT", "WFH", "ON_DUTY") else None
    return {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": punch_in,
        "punch_out": instant(day, 18, 30) if hours is not None else None,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    }


def seed_analytics_data(database) -> None:
    database.employees.insert_many([
        employee("EMP0001", "Engineering", "2026-07-06"),
        employee("EMP0002", "Engineering", "2026-07-08"),
        employee("EMP0005", "Engineering", "2026-07-09"),
        employee("EMP0004", "Sales", "2026-07-01"),  # no attendance logs, still headcount
        employee("EMP0003", "Engineering", "2026-08-01"),  # not joined by July month-end
    ])
    database.attendance_logs.insert_many([
        log("EMP0001", "2026-07-06", "PRESENT", late=20, hours=9.0, overtime=10),
        log("EMP0001", "2026-07-07", "WFH", late=10, hours=4.49, half_day=True),
        log("EMP0001", "2026-07-11", "PRESENT", late=5, hours=8.0, overtime=40),  # weekend
        log("EMP0002", "2026-07-10", "LEAVE"),
        log("EMP0002", "2026-07-08", "ON_DUTY", late=20, hours=8.5),
        log("EMP0005", "2026-07-09", "PRESENT", late=20, hours=8.0),
        log("EMP9999", "2026-07-06", "PRESENT", late=100, hours=8.0),  # missing employee ignored
    ])


def test_monthly_department_leaderboard_and_trend_aggregations(real_api):
    client, database = real_api
    seed_analytics_data(database)

    monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert monthly.status_code == 200
    assert set(monthly.json()) == {
        "emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
        "total_late_minutes", "total_overtime_minutes", "attendance_pct",
    }
    assert monthly.json() == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 20,
        "present_days": 1.5, "leave_days": 0, "late_count": 3,
        "total_late_minutes": 35, "total_overtime_minutes": 50, "attendance_pct": 7.5,
    }
    joiner = client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"})
    assert joiner.status_code == 200
    assert joiner.json()["working_days"] == 18
    assert joiner.json()["present_days"] == 1
    assert joiner.json()["leave_days"] == 1
    assert client.get("/analytics/employees/MISSING/monthly", params={"month": "2026-07"}).status_code == 404

    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert summary.status_code == 200
    assert [item["department"] for item in summary.json()["items"]] == ["Engineering", "Sales"]
    rows = {item["department"]: item for item in summary.json()["items"]}
    assert set(rows["Engineering"]) == {
        "department", "headcount", "present_days", "avg_work_hours", "late_count",
        "total_late_minutes", "leave_count", "on_duty_count",
    }
    assert rows["Engineering"] == {
        "department": "Engineering", "headcount": 3, "present_days": 3.5,
        "late_count": 5, "total_late_minutes": 75, "leave_count": 1,
        "on_duty_count": 1, "avg_work_hours": 7.6,
    }
    assert rows["Sales"] == {
        "department": "Sales", "headcount": 1, "present_days": 0,
        "late_count": 0, "total_late_minutes": 0, "leave_count": 0,
        "on_duty_count": 0, "avg_work_hours": None,
    }
    filtered = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"})
    assert [row["department"] for row in filtered.json()["items"]] == ["Sales"]

    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2, "department": "Engineering"})
    assert leaderboard.status_code == 200
    assert set(leaderboard.json()) == {"month", "items"}
    assert set(leaderboard.json()["items"][0]) == {
        "rank", "emp_code", "name", "department", "total_late_minutes", "late_count",
    }
    assert [(row["emp_code"], row["rank"], row["total_late_minutes"]) for row in leaderboard.json()["items"]] == [
        ("EMP0001", 1, 35), ("EMP0002", 2, 20), ("EMP0005", 2, 20),
    ]
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()["items"] == []

    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert trend.status_code == 200
    rows = trend.json()["items"]
    assert set(rows[0]) == {
        "date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d",
    }
    assert len(rows) == 7
    assert [row["headcount"] for row in rows] == [1, 1, 2, 3, 3, 3, 3]
    assert [row["present_count"] for row in rows] == [1, 0.5, 1, 1, 0, 1, 0]
    assert [row["attendance_rate"] for row in rows] == [1.0, 0.5, 0.5, 0.3333, 0.0, None, None]
    assert [row["moving_avg_7d"] for row in rows] == [1.0, 0.75, 0.6667, 0.5833, 0.4667, 0.4667, 0.4667]
    assert rows[5]["is_working_day"] is False
    assert rows[5]["late_count"] == 1  # weekend records still count as late


def test_trend_validation_and_explain_routes_against_mongodb(real_api):
    client, database = real_api
    seed_analytics_data(database)
    assert client.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422

    calls = [
        ("attendance_list", {} , "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "employees"),
        ("department_summary", {"month": "2026-07"}, "employees"),
        ("late_leaderboard", {"month": "2026-07", "limit": 2}, "attendance_logs"),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}, "employees"),
    ]
    for endpoint, params, collection in calls:
        response = client.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"endpoint", "collection", "explain"}
        assert body["endpoint"] == endpoint
        assert body["collection"] == collection
        assert isinstance(body["explain"], dict)


def test_real_mongodb_concurrent_employee_punch_and_correction_writes(real_api):
    client, database = real_api
    payload = {
        "emp_code": "EMP0001", "name": "Concurrency Test", "email": "concurrency@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }

    def create_employee(_: int):
        return client.post("/employees", json=payload).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        create_results = list(pool.map(create_employee, range(8)))
    assert create_results.count(201) == 1
    assert create_results.count(409) == 7

    punch_in_ms = int(instant("2026-07-06", 9, 30).timestamp() * 1_000)

    def punch_in(_: int):
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in_ms}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        punch_in_results = list(pool.map(punch_in, range(8)))
    assert punch_in_results.count(201) == 1
    assert punch_in_results.count(409) == 7

    punch_out_ms = int(instant("2026-07-06", 18, 30).timestamp() * 1_000)

    def punch_out(_: int):
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_ms}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        punch_out_results = list(pool.map(punch_out, range(8)))
    assert punch_out_results.count(200) == 1
    assert punch_out_results.count(409) == 7

    def correct(index: int):
        corrected_in = int(instant("2026-07-06", 9, 30 + index).timestamp() * 1_000)
        return client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "WFH" if index % 2 == 0 else "ON_DUTY",
            "punch_in": corrected_in,
            "reason": "concurrent edit",
            "regularized_by": f"tester{index}",
        }).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        correction_results = list(pool.map(correct, range(8)))
    assert all(status in (200, 409) for status in correction_results)
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert len(stored["history"]) == correction_results.count(200)


def plan_stages(value):
    if isinstance(value, dict):
        stage = value.get("stage")
        if isinstance(stage, str):
            yield stage
        for key, child in value.items():
            if key not in ("rejectedPlans", "allPlansExecution"):
                yield from plan_stages(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_stages(child)


def test_execution_plans_use_indexes_on_100k_records(real_api):
    if os.getenv("MONGO_TEST_LARGE") != "1":
        pytest.skip("set MONGO_TEST_LARGE=1 to build a 100,000-record explain dataset")
    client, database = real_api
    employees = []
    for index in range(100):
        employees.append(employee(f"EMP{index:04d}", "Engineering" if index < 10 else "Support", "2024-01-01"))
    database.employees.insert_many(employees)

    first_day = date(2024, 1, 1)
    attendance = []
    for offset in range(1_000):
        day = (first_day + timedelta(days=offset)).isoformat()
        for index in range(100):
            attendance.append({
                "emp_code": f"EMP{index:04d}",
                "date": day,
                "status": "PRESENT",
                "punch_in": instant(day, 10, 0),
                "punch_out": instant(day, 18, 0),
                "work_hours": 8.0,
                "late_minutes": index % 7 + 1,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            })
    database.attendance_logs.insert_many(attendance)

    explain_requests = [
        ("attendance_list", {"date_from": "2026-07-06", "date_to": "2026-07-06", "status": "PRESENT"}),
        ("employee_monthly", {"emp_code": "EMP0000", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}),
        ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 10}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}),
    ]
    for endpoint, params in explain_requests:
        response = client.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        stages = list(plan_stages(response.json()["explain"]))
        assert "IXSCAN" in stages, f"{endpoint} had no index scan: {stages}"
        assert "COLLSCAN" not in stages, f"{endpoint} used a collection scan: {stages}"
