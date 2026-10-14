from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture

def api(monkeypatch: pytest.MonkeyPatch):
    mongo = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(main, "client", mongo)
    monkeypatch.setattr(main, "db", mongo["unit_test"])
    with TestClient(main.app) as test_client:
        yield test_client, mongo["unit_test"]
    mongo.close()


def epoch_millis(day: str, hour: int, minute: int, second: int = 0, millis: int = 0) -> int:
    instant = datetime.combine(date.fromisoformat(day), time(hour, minute, second, millis * 1000), tzinfo=IST)
    return int(instant.timestamp() * 1000)


def employee_payload(emp_code: str = "EMP0001", **overrides: str) -> dict[str, str]:
    payload = {
        "emp_code": emp_code,
        "name": "Asha Rao",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    payload.update(overrides)
    return payload


def test_startup_indexes_and_health(api):
    client, database = api
    assert client.get("/health").status_code == 200
    assert "uq_employee_emp_code" in database.employees.index_information()
    assert "uq_attendance_employee_date" in database.attendance_logs.index_information()
    assert "ix_attendance_date_code" in database.attendance_logs.index_information()


def test_health_returns_503_when_mongodb_ping_fails(api, monkeypatch: pytest.MonkeyPatch):
    client, _ = api

    class UnavailableMongo:
        @property
        def admin(self):
            return self

        def command(self, command: str):
            raise main.PyMongoError("not reachable")

    monkeypatch.setattr(main, "client", UnavailableMongo())
    assert client.get("/health").status_code == 503


def test_employee_create_duplicate_and_filtered_pagination(api):
    client, _ = api
    for code in ("EMP0002", "EMP0001", "EMP0003"):
        response = client.post("/employees", json=employee_payload(code))
        assert response.status_code == 201
        assert set(response.json()) == {
            "emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at",
        }
        assert isinstance(response.json()["created_at"], int)
    client.post("/employees", json=employee_payload("EMP0004", department="Sales"))
    duplicate = client.post("/employees", json=employee_payload("EMP0001"))
    assert duplicate.status_code == 409

    page = client.get("/employees", params={"department": "Engineering", "page": 2, "page_size": 1})
    assert page.status_code == 200
    assert set(page.json()) == {"items", "total", "page", "page_size"}
    assert page.json()["total"] == 3
    assert [row["emp_code"] for row in page.json()["items"]] == ["EMP0002"]
    assert page.json()["page"] == 2
    assert client.get("/employees", params={"department": "Engineering"}).json()["total"] == 3
    assert client.get("/employees", params={"department": ""}).json()["total"] == 0


def test_employee_validation_and_pagination_limits(api):
    client, _ = api
    assert client.post("/employees", json=employee_payload("bad-code")).status_code == 422
    assert client.post("/employees", json=employee_payload(shift_start="09:30", shift_end="09:30")).status_code == 422
    assert client.post("/employees", json=employee_payload(email="not-an-email")).status_code == 422
    assert client.post("/employees", json=employee_payload(shift_start="9:30")).status_code == 422
    assert client.post("/employees", json=employee_payload(joined_on="2026-02-30")).status_code == 422
    missing = employee_payload()
    del missing["name"]
    assert client.post("/employees", json=missing).status_code == 422
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_late_rounding_timestamp_validation_and_duplicate(api):
    client, _ = api
    client.post("/employees", json=employee_payload())
    first = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 40, 1), "late_minutes": 999,
    })
    assert first.status_code == 201
    assert first.json()["date"] == "2026-07-06"
    assert first.json()["late_minutes"] == 10
    assert first.json()["punch_in"] == epoch_millis("2026-07-06", 9, 40, 1)
    assert first.json()["punch_out"] is None
    assert first.json()["work_hours"] is None
    assert first.json()["history"] == []
    assert set(first.json()) == {
        "emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
        "overtime_minutes", "half_day", "history",
    }
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 40),
    }).status_code == 409
    client.post("/employees", json=employee_payload("EMP0002"))
    truncated = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002", "punched_at": epoch_millis("2026-07-06", 9, 40, millis=999),
    })
    assert truncated.status_code == 201
    assert truncated.json()["punch_in"] == epoch_millis("2026-07-06", 9, 40)
    assert truncated.json()["late_minutes"] == 0

    for timestamp in (1_783_312_500, 4_102_444_800_001, 1_783_312_500.0, "1783312500000", None):
        response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
        assert response.status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN"}).status_code == 404


def test_late_grace_and_work_overtime_half_day_boundaries():
    def utc(hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 7, 6, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)

    assert main.compute_late_minutes(utc(9, 40), "09:30", "2026-07-06") == 0
    assert main.compute_late_minutes(utc(9, 40, 1), "09:30", "2026-07-06") == 10
    assert main.compute_late_minutes(utc(10, 15, 59), "09:30", "2026-07-06") == 45
    assert main.compute_work_hours(utc(9, 30), utc(13, 59, 24)) == 4.49
    assert main.compute_work_hours(utc(9, 30), utc(13, 59, 6)) == 4.49  # 4.485 hours rounds half-up
    assert main.compute_work_hours(utc(9, 30), utc(13, 59, 42)) == 4.50  # 4.495 hours rounds half-up
    assert main.compute_work_hours(utc(9, 30), utc(14, 0)) == 4.5
    assert main.compute_overtime(utc(18, 59, 59), "18:30", "2026-07-06") == 0
    assert main.compute_overtime(utc(19, 0), "18:30", "2026-07-06") == 30
    assert main.compute_overtime(utc(19, 0, 59), "18:30", "2026-07-06") == 30
    assert main.compute_derived(employee_payload(), "2026-07-06", utc(9, 30), utc(13, 59, 24), "PRESENT")["half_day"] is True
    assert main.compute_derived(employee_payload(), "2026-07-06", utc(9, 30), utc(13, 59, 42), "PRESENT")["half_day"] is False
    assert main.compute_derived(employee_payload(), "2026-07-06", utc(9, 30), utc(14, 0), "PRESENT")["half_day"] is False


def test_epoch_millisecond_schema_accepts_inclusive_endpoints():
    assert main.datetime_from_epoch_ms(100_000_000_000) == datetime(1973, 3, 3, 9, 46, 40, tzinfo=timezone.utc)
    assert main.datetime_from_epoch_ms(4_102_444_800_000) == datetime(2100, 1, 1, tzinfo=timezone.utc)
    for invalid in (99_999_999_999, 4_102_444_800_001, 1_000.0, "100000000000"):
        with pytest.raises(main.HTTPException) as error:
            main.datetime_from_epoch_ms(invalid)
        assert error.value.status_code == 422


def test_punch_out_and_attendance_filter_sort_pagination(api):
    client, _ = api
    client.post("/employees", json=employee_payload())
    client.post("/employees", json=employee_payload("EMP0002", department="Sales"))
    punch_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 30),
    })
    assert punch_in.status_code == 201
    closed = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 19, 0),
    })
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 9.5
    assert closed.json()["overtime_minutes"] == 30
    assert closed.json()["half_day"] is False
    assert client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 19, 1),
    }).status_code == 409

    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": epoch_millis("2026-07-07", 9, 30)})
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-07", 9, 30)})
    listed = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page": 1, "page_size": 1})
    assert listed.status_code == 200
    assert set(listed.json()) == {"items", "total", "page", "page_size"}
    assert listed.json()["total"] == 3
    assert listed.json()["items"][0]["date"] == "2026-07-07"
    assert set(listed.json()["items"][0]) == {
        "emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
        "overtime_minutes", "half_day", "history",
    }
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert client.get("/attendance", params={"status": "INVALID"}).status_code == 422
    all_rows = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 100}).json()["items"]
    assert [(item["date"], item["emp_code"]) for item in all_rows] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"),
    ]


def test_punch_out_rejects_bad_elapsed_time_and_unknown_record(api):
    client, _ = api
    client.post("/employees", json=employee_payload())
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "MISSING"}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 30)})
    for out_time in (epoch_millis("2026-07-06", 9, 29), epoch_millis("2026-07-07", 9, 31)):
        assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_time}).status_code == 422


def test_overnight_date_punch_out_and_overtime(api):
    client, _ = api
    client.post("/employees", json=employee_payload(shift_start="22:00", shift_end="06:00"))
    punched_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-07", 5, 59),
    })
    assert punched_in.status_code == 201
    assert punched_in.json()["date"] == "2026-07-06"
    closed = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-07", 6, 30),
    })
    assert closed.status_code == 200
    assert closed.json()["overtime_minutes"] == 30


def test_concurrent_punch_out_and_regularization_preserve_history(api):
    client, database = api
    client.post("/employees", json=employee_payload())
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 30),
    })

    def close_record(_: int):
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 18, 30),
        }).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        close_results = list(pool.map(close_record, range(6)))
    assert close_results.count(200) == 1
    assert close_results.count(409) == 5

    def correct(index: int):
        status = "WFH" if index % 2 == 0 else "ON_DUTY"
        return client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": status, "punch_in": epoch_millis("2026-07-06", 9, 30, index),
            "reason": "classification", "regularized_by": f"reviewer{index}",
        }).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        correction_results = list(pool.map(correct, range(6)))
    assert all(code in (200, 409) for code in correction_results)
    record = database.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert len(record["history"]) == correction_results.count(200)


def test_regularization_recomputes_and_appends_only_real_changes(api):
    client, _ = api
    client.post("/employees", json=employee_payload())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 10, 5)})
    corrected = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": epoch_millis("2026-07-06", 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin",
        "work_hours": 1234,
    })
    assert corrected.status_code == 200
    assert corrected.json()["late_minutes"] == 0
    assert len(corrected.json()["history"]) == 1
    assert set(corrected.json()["history"][0]["changes"]) == {"punch_in", "late_minutes"}
    entry = corrected.json()["history"][0]
    assert set(entry) == {"at", "by", "reason", "changes"}
    assert isinstance(entry["at"], int)
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch"
    assert entry["changes"]["punch_in"] == {
        "from": epoch_millis("2026-07-06", 10, 5),
        "to": epoch_millis("2026-07-06", 9, 28),
    }
    assert client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "no changes", "regularized_by": "hr.admin",
    }).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-09", json={
        "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr.admin",
    }).status_code == 404


def test_regularization_status_clearing_validation_and_not_found(api):
    client, database = api
    client.post("/employees", json=employee_payload())
    original = {
        "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": datetime.combine(date(2026, 7, 6), time(9, 30), IST).astimezone(timezone.utc),
        "punch_out": datetime.combine(date(2026, 7, 6), time(18, 30), IST).astimezone(timezone.utc),
        "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    database.attendance_logs.insert_one(original)
    assert client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "LEAVE", "punch_in": epoch_millis("2026-07-06", 9, 30),
        "reason": "approved leave", "regularized_by": "hr.admin",
    }).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr.admin",
    }).status_code == 200
    current = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert current["punch_in"] is None and current["punch_out"] is None
    assert current["history"][-1]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    assert current["history"][-1]["changes"]["punch_out"]["from"] is not None
    assert client.patch("/attendance/MISSING/2026-07-06", json={
        "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr.admin",
    }).status_code == 404


def test_concurrent_employee_creates_and_punch_ins_have_single_winner(api):
    client, _ = api

    def create_employee(_: int):
        return client.post("/employees", json=employee_payload()).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        employee_results = list(pool.map(create_employee, range(8)))
    assert employee_results.count(201) == 1
    assert employee_results.count(409) == 7

    def punch(_: int):
        return client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": epoch_millis("2026-07-06", 9, 30),
        }).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        punch_results = list(pool.map(punch, range(8)))
    assert punch_results.count(201) == 1
    assert punch_results.count(409) == 7


def test_analytics_pipeline_builders_keep_work_in_mongodb():
    monthly = main.employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31")
    summary = main.department_summary_pipeline("2026-07-01", "2026-07-31", "Engineering")
    leaderboard = main.late_leaderboard_pipeline("2026-07-01", "2026-07-31", None, 2)
    trend = main.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
    assert monthly[0] == {"$match": {"emp_code": "EMP0001"}}
    assert any("$lookup" in stage for stage in monthly)
    assert summary[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}, "department": "Engineering"}
    assert any("$lookup" in stage for stage in summary)
    assert any("$rank" in str(stage) for stage in leaderboard)
    assert any("$setWindowFields" in stage for stage in trend)
    assert any("$range" in str(stage) for stage in trend)


def test_explain_requires_contract_parameters_and_rejects_unknown_endpoint(api):
    client, _ = api
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/department_summary", params={"month": "2026-13"}).status_code == 422
    assert client.get("/admin/explain/attendance_list", params={"month": "2026-13"}).status_code == 422
    assert client.get("/admin/explain/attendance_list", params={"from": "2026-02-30"}).status_code == 422
    assert client.get("/admin/explain/not_allowed").status_code == 422


def test_analytics_calendar_query_validation_precedes_database_access(api):
    client, _ = api
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-2"}).status_code == 422
    assert client.get("/analytics/departments/summary", params={"month": "2026-00"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-02-30", "to": "2026-03-01"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-04-01"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422


def test_invalid_month_year_is_rejected():
    with pytest.raises(main.HTTPException) as error:
        main.parse_month("0000-01")
    assert error.value.status_code == 422
