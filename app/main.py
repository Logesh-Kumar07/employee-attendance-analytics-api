"""Employee Attendance & Analytics API. All application code stays in this module."""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.requests import Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # By default existing environment variables take precedence over .env.

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
EXPLAIN_ENDPOINTS = (
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
)

mongo_uri = os.getenv("MONGO_URI")
mongo_db_name = os.getenv("MONGO_DB")
client: MongoClient[Any] | None = None
db: Any = None
if mongo_uri and mongo_db_name:
    client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=2_000, connectTimeoutMS=2_000)
    db = client[mongo_db_name]

# --------------------------------------------------------------------------- #
# Validation and time helpers
# --------------------------------------------------------------------------- #
def validation_error(message: str, location: tuple[str, ...] = ("query",)) -> None:
    """Raise a FastAPI-shaped 422 body for validation performed outside Pydantic."""
    raise HTTPException(
        status_code=422,
        detail=[{"loc": list(location), "msg": message, "type": "value_error"}],
    )


def parse_date(value: str, field: str = "date") -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        validation_error(f"{field} must use YYYY-MM-DD", ("query", field))
    try:
        return date.fromisoformat(value)
    except ValueError:
        validation_error(f"{field} is not a valid calendar date", ("query", field))


def parse_month(value: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        validation_error("month must use YYYY-MM", ("query", "month"))
    year, month = map(int, value.split("-"))
    if year == 0:
        validation_error("month year must be at least 0001", ("query", "month"))
    last = calendar.monthrange(year, month)[1]
    return value, f"{year:04d}-{month:02d}-01", date(year, month, last), date(year, month, 1)


def datetime_from_epoch_ms(value: int, field: str = "punched_at") -> datetime:
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        validation_error(
            f"{field} must be an integer epoch-millisecond value between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}",
            ("body", field),
        )
    # BSON stores milliseconds; truncating to seconds here also prevents subsecond rule drift.
    return datetime.fromtimestamp(value // 1_000, tz=UTC)


def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1_000


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def shift_times(employee: dict[str, Any]) -> tuple[time, time]:
    start_h, start_m = map(int, employee["shift_start"].split(":"))
    end_h, end_m = map(int, employee["shift_end"].split(":"))
    return time(start_h, start_m), time(end_h, end_m)


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = as_utc(punch_in).astimezone(IST)
    _, end = shift_times(employee)
    local_date = local.date()
    if end <= shift_times(employee)[0] and local.time().replace(tzinfo=None) < end:
        return local_date - timedelta(days=1)
    return local_date


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str | date | None = None) -> int:
    local = as_utc(punch_in).astimezone(IST)
    if record_date is None:
        shift_day = local.date()
    elif isinstance(record_date, str):
        shift_day = date.fromisoformat(record_date)
    else:
        shift_day = record_date
    h, m = map(int, shift_start.split(":"))
    start = datetime.combine(shift_day, time(h, m), tzinfo=IST)
    elapsed_seconds = int((local - start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def shift_end_datetime(shift_end: str, shift_start: str, record_date: str | date) -> datetime:
    day = date.fromisoformat(record_date) if isinstance(record_date, str) else record_date
    start_hm = tuple(map(int, shift_start.split(":")))
    end_hm = tuple(map(int, shift_end.split(":")))
    if end_hm <= start_hm:
        day += timedelta(days=1)
    return datetime.combine(day, time(*end_hm), tzinfo=IST)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3_600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    end = shift_end_datetime(shift_end, shift_start, date_str).astimezone(UTC)
    elapsed_seconds = int((as_utc(punch_out) - end).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds >= 1_800 else 0


def compute_derived(employee: dict[str, Any], record_date: str, punch_in: datetime | None,
                    punch_out: datetime | None, status: str) -> dict[str, Any]:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"], record_date)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, employee["shift_end"], record_date, employee["shift_start"])
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4.50,
    }


def get_database() -> Any:
    if db is None:
        raise HTTPException(503, "MongoDB is not configured or is unavailable")
    return db


def ensure_indexes(database: Any) -> None:
    """Idempotently create the unique keys and the query/aggregation support indexes."""
    index_specs = (
        ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uq_employee_emp_code"}),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_employee_department_code"}),
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "ix_employee_department_joined"}),
        ("employees", [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_employee_joined_department"}),
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_attendance_employee_date"}),
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_attendance_date_code"}),
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_attendance_status_date_code"}),
        ("attendance_logs", [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], {"name": "ix_attendance_date_late_code"}),
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "ix_attendance_employee_punchin"}),
    )
    for collection_name, keys, options in index_specs:
        database[collection_name].create_index(keys, **options)


def startup_indexes() -> None:
    if db is None:
        logger.warning("MONGO_URI and MONGO_DB must be configured; API will return 503 for database operations")
        return
    try:
        ensure_indexes(db)
    except PyMongoError:
        # Keep the process alive so /health reports 503 and the cause remains observable in logs.
        logger.exception("Could not create MongoDB indexes during startup")


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_exception_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    _ = request, exc
    return JSONResponse(status_code=503, content={"detail": "MongoDB is unavailable"})


# --------------------------------------------------------------------------- #
# Request models and API serializers
# --------------------------------------------------------------------------- #
class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeCreate(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must use YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def shift_is_nonzero(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(APIModel):
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punch_time(cls, value: int | None) -> int | None:
        if value is not None:
            datetime_from_epoch_ms(value)
        return value

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self) -> PunchInRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutRequest(APIModel):
    emp_code: str
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_punch_time(cls, value: int | None) -> int | None:
        if value is not None:
            datetime_from_epoch_ms(value)
        return value

    @model_validator(mode="after")
    def timestamp_cannot_be_null(self) -> PunchOutRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeRequest(APIModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_time(cls, value: int | None) -> int | None:
        if value is not None:
            datetime_from_epoch_ms(value)
        return value

    @model_validator(mode="after")
    def disallow_explicit_null_punch_time(self) -> RegularizeRequest:
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        for field_name in ("punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be an epoch-millisecond integer")
        return self


def employee_response(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": epoch_ms(document["created_at"]),
    }


def attendance_response(document: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in document.get("history", []):
        changes = {}
        for field_name, pair in entry.get("changes", {}).items():
            changes[field_name] = {
                key: epoch_ms(value) if field_name in ("punch_in", "punch_out") else value
                for key, value in pair.items()
            }
        history.append({
            "at": epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": epoch_ms(document.get("punch_in")),
        "punch_out": epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


def raise_if_duplicate(error: DuplicateKeyError, message: str) -> None:
    if error.code == 11000 or "duplicate key" in str(error).lower():
        raise HTTPException(409, message) from error
    raise error


# --------------------------------------------------------------------------- #
# Shared MongoDB query and aggregation builders
# --------------------------------------------------------------------------- #
def attendance_query(emp_code: str | None, date_from: str | None, date_to: str | None,
                     status: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds: dict[str, str] = {}
        if date_from is not None:
            bounds["$gte"] = date_from
        if date_to is not None:
            bounds["$lte"] = date_to
        query["date"] = bounds
    if status is not None:
        query["status"] = status
    return query


def mongo_half_up(value: Any, places: int) -> dict[str, Any]:
    """Round a non-negative Mongo numeric expression half-up using Decimal128 arithmetic."""
    scale = 10 ** places
    rounded = {
        "$divide": [
            {"$floor": {"$add": [
                {"$multiply": [{"$toDecimal": value}, Decimal128(str(scale))]},
                Decimal128("0.5"),
            ]}},
            Decimal128(str(scale)),
        ]
    }
    return {"$convert": {"input": rounded, "to": "double"}}


def working_days_expression(month_start: str, month_end: str) -> dict[str, Any]:
    start_date = {
        "$dateFromString": {
            "dateString": {"$cond": [{"$gt": ["$joined_on", month_start]}, "$joined_on", month_start]},
        }
    }
    end_date = {"$dateFromString": {"dateString": month_end}}
    day_offsets = {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_date, "endDate": end_date, "unit": "day"}}, 1]}]}
    weekdays = {
        "$map": {
            "input": day_offsets,
            "as": "offset",
            "in": {"$dayOfWeek": {"$dateAdd": {"startDate": start_date, "unit": "day", "amount": "$$offset"}}},
        }
    }
    return {
        "$cond": [
            {"$gt": ["$joined_on", month_end]},
            0,
            {"$size": {"$filter": {"input": weekdays, "as": "weekday", "cond": {"$in": ["$$weekday", [2, 3, 4, 5, 6]]}}}},
        ]
    }


def month_log_lookup(emp_code_expr: str, month_start: str, month_end: str) -> list[dict[str, Any]]:
    presence = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
    present_on_weekday = {"$and": [presence, {"$in": [weekday, [2, 3, 4, 5, 6]]}]}
    work_hours_eligible = {"$and": [presence, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", emp_code_expr]},
            {"$gte": ["$date", month_start]},
            {"$lte": ["$date", month_end]},
        ]}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [present_on_weekday, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "work_hours_sum": {"$sum": {"$cond": [work_hours_eligible, {"$toDecimal": "$work_hours"}, Decimal128("0")]}},
            "work_hours_count": {"$sum": {"$cond": [work_hours_eligible, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": month_log_lookup("$$employee_code", month_start, month_end),
            "as": "monthly_logs",
        }},
        {"$set": {"metrics": {"$ifNull": [{"$arrayElemAt": ["$monthly_logs", 0]}, {}]}}},
        {"$set": {
            "working_days": working_days_expression(month_start, month_end),
            "present_days": {"$ifNull": ["$metrics.present_days", 0]},
            "leave_days": {"$ifNull": ["$metrics.leave_days", 0]},
            "late_count": {"$ifNull": ["$metrics.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$metrics.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$metrics.total_overtime_minutes", 0]},
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": 1,
            "present_days": 1,
            "leave_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [
                {"$eq": ["$working_days", 0]},
                None,
                mongo_half_up({"$multiply": [
                    {"$divide": [{"$toDecimal": "$present_days"}, {"$toDecimal": "$working_days"}]},
                    Decimal128("100"),
                ]}, 2),
            ]},
        }},
    ]


def department_summary_pipeline(month_start: str, month_end: str, department: str | None) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    eligible_logs = month_log_lookup("$$employee_code", month_start, month_end)
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": eligible_logs,
            "as": "monthly_metrics",
        }},
        {"$unwind": {"path": "$monthly_metrics", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$monthly_metrics.present_days", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$monthly_metrics.leave_days", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$monthly_metrics.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$monthly_metrics.total_late_minutes", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$monthly_metrics.on_duty_count", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$monthly_metrics.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$monthly_metrics.work_hours_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": {"$cond": [
                {"$eq": ["$work_hours_count", 0]},
                None,
                mongo_half_up({"$divide": [
                    {"$toDecimal": "$work_hours_sum"},
                    {"$toDecimal": "$work_hours_count"},
                ]}, 2),
            ]},
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month_start: str, month_end: str, department: str | None,
                              limit: int) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # Logs for deleted/unknown employees are deliberately ignored.
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, first_day: str, last_day: str) -> list[dict[str, Any]]:
    """Create a calendar spine and join employee/day logs inside MongoDB (no Python day loop)."""
    start_date = {"$dateFromString": {"dateString": first_day}}
    end_date = {"$dateFromString": {"dateString": last_day}}
    days = {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_date, "endDate": end_date, "unit": "day"}}, 1]}]}
    return [
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {"$set": {"_trend_days": {"$map": {
            "input": days,
            "as": "offset",
            "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": start_date, "unit": "day", "amount": "$$offset"}}, "format": "%Y-%m-%d"}},
        }}}},
        {"$unwind": "$_trend_days"},
        {"$set": {
            "_trend_date": "$_trend_days",
            "_trend_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_trend_days"}}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code", "attendance_date": "$_trend_date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$eq": ["$date", "$$attendance_date"]},
                ]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "_log",
        }},
        {"$unwind": {"path": "$_log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_trend_date",
            "weekday": {"$first": "$_trend_weekday"},
            "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_trend_date"]}, 1, 0]}},
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$_log.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$ifNull": ["$_log.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_log.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$set": {
            "is_working_day": {"$in": ["$weekday", [2, 3, 4, 5, 6]]},
            "attendance_rate": {"$cond": [
                {"$and": [{"$in": ["$weekday", [2, 3, 4, 5, 6]]}, {"$gt": ["$headcount", 0]}]},
                mongo_half_up({"$divide": [
                    {"$toDecimal": "$present_count"},
                    {"$toDecimal": "$headcount"},
                ]}, 4),
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"_moving_avg": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {
            "moving_avg_7d": {"$cond": [
                {"$eq": [{"$ifNull": ["$_moving_avg", None]}, None]},
                None,
                mongo_half_up("$_moving_avg", 4),
            ]},
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    if client is None:
        raise HTTPException(503, "MongoDB is not configured")
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    database = get_database()
    document = body.model_dump()
    document["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        database.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise_if_duplicate(exc, "emp_code already exists")
    return employee_response(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    database = get_database()
    query = {"department": department} if department is not None else {}
    items = database.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_response(item) for item in items],
        "total": database.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    database = get_database()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record_day = attendance_date(punched, employee).isoformat()
    document = {
        "emp_code": body.emp_code,
        "date": record_day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched, employee["shift_start"], record_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise_if_duplicate(exc, "already punched in for this date")
    return attendance_response(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    database = get_database()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    current = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched}},
        sort=[("punch_in", DESCENDING)],
    )
    if current is None:
        latest = database.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if latest is not None and punched <= as_utc(latest["punch_in"]):
            validation_error("punched_at must be after punch_in", ("body", "punched_at"))
        raise HTTPException(404, "no eligible punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    punch_in_value = as_utc(current["punch_in"])
    elapsed = (punched - punch_in_value).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        validation_error("punched_at must be after punch_in and no more than 24 hours later", ("body", "punched_at"))
    derived = compute_derived(employee, current["date"], punch_in_value, punched, current["status"])
    update = {"punch_out": punched, **derived}
    result = database.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": current["date"], "punch_out": None},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record was concurrently punched out")
    return attendance_response({**current, **update})


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        validation_error("date_from must be on or before date_to", ("query", "date_from"))
    database = get_database()
    query = attendance_query(emp_code, date_from, date_to, status)
    cursor = database.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    cursor = cursor.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_response(item) for item in cursor],
        "total": database.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    record_day = parse_date(date, "date").isoformat()
    database = get_database()
    employee = database.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = database.attendance_logs.find_one({"emp_code": emp_code, "date": record_day})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    fields = body.model_fields_set
    final_status = body.status if "status" in fields else current["status"]
    final_in = datetime_from_epoch_ms(body.punch_in, "punch_in") if "punch_in" in fields else current.get("punch_in")
    final_out = datetime_from_epoch_ms(body.punch_out, "punch_out") if "punch_out" in fields else current.get("punch_out")
    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields or "punch_out" in fields:
            validation_error("ABSENT and LEAVE cannot be supplied with punch times", ("body", "status"))
        final_in = final_out = None
    else:
        if final_in is None:
            validation_error("presence statuses require a punch_in", ("body", "punch_in"))
        if attendance_date(final_in, employee).isoformat() != record_day:
            validation_error("punch_in must belong to the record date", ("body", "punch_in"))
        if final_out is not None:
            elapsed = (as_utc(final_out) - as_utc(final_in)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                validation_error("punch_out must follow punch_in and be within 24 hours", ("body", "punch_out"))

    derived = compute_derived(employee, record_day, final_in, final_out, final_status)
    before = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    after = {
        "status": final_status,
        "punch_in": final_in,
        "punch_out": final_out,
        **derived,
    }
    changes = {
        key: {"from": before[key], "to": value}
        for key, value in after.items()
        if before[key] != value
    }
    if not changes:
        validation_error("correction does not change the record", ("body",))
    history_item = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    old_history = current.get("history")
    optimistic_filter: dict[str, Any] = {
        "emp_code": emp_code,
        "date": record_day,
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
    }
    optimistic_filter["history"] = old_history if old_history is not None else {"$exists": False}
    result = database.attendance_logs.update_one(
        optimistic_filter,
        {"$set": after, "$push": {"history": history_item}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently; retry the correction")
    return attendance_response({**current, **after, "history": [*(old_history or []), history_item]})


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    month_value, first_day, last_day, _ = parse_month(month)
    pipeline = employee_monthly_pipeline(emp_code, first_day, last_day.isoformat())
    result = next(get_database().employees.aggregate(pipeline), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    return {"emp_code": result["emp_code"], "month": month_value, **{key: result[key] for key in (
        "working_days", "present_days", "leave_days", "late_count", "total_late_minutes",
        "total_overtime_minutes", "attendance_pct",
    )}}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None) -> dict[str, Any]:
    month_value, first_day, last_day, _ = parse_month(month)
    pipeline = department_summary_pipeline(first_day, last_day.isoformat(), department)
    items = list(get_database().employees.aggregate(pipeline))
    return {"month": month_value, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict[str, Any]:
    month_value, first_day, last_day, _ = parse_month(month)
    pipeline = late_leaderboard_pipeline(first_day, last_day.isoformat(), department, limit)
    return {"month": month_value, "items": list(get_database().attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)) -> dict[str, Any]:
    from_day, to_day = parse_date(from_, "from"), parse_date(to, "to")
    if to_day < from_day:
        validation_error("to must be on or after from", ("query", "to"))
    if (to_day - from_day).days + 1 > 92:
        validation_error("trend range cannot exceed 92 calendar days", ("query", "to"))
    database = get_database()
    if database.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat())
    items = list(database.employees.aggregate(pipeline))
    return {"department": department, "items": items}


def explain_command(database: Any, operation: dict[str, Any]) -> dict[str, Any]:
    raw = database.command("explain", operation, verbosity="executionStats")
    # Extended JSON makes any BSON values in explain serializable without dropping information.
    return json.loads(json_util.dumps(raw))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict[str, Any]:
    database = get_database()
    if month is not None:
        parse_month(month)
    for field_name, value in (("date_from", date_from), ("date_to", date_to), ("from", from_), ("to", to)):
        if value is not None:
            parse_date(value, field_name)
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            validation_error("date_from must be on or before date_to", ("query", "date_from"))
        operation = {"find": "attendance_logs", "filter": attendance_query(emp_code, date_from, date_to, status),
                     "projection": {"_id": 0}, "sort": {"date": -1, "emp_code": 1},
                     "skip": (page - 1) * page_size, "limit": page_size}
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            validation_error("emp_code and month are required", ("query",))
        _, first_day, last_day, _ = parse_month(month)
        operation = {"aggregate": "employees", "pipeline": employee_monthly_pipeline(emp_code, first_day, last_day.isoformat()), "cursor": {}}
        collection_name = "employees"
    elif endpoint == "department_summary":
        if month is None:
            validation_error("month is required", ("query", "month"))
        _, first_day, last_day, _ = parse_month(month)
        operation = {"aggregate": "employees", "pipeline": department_summary_pipeline(first_day, last_day.isoformat(), department), "cursor": {}}
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            validation_error("month is required", ("query", "month"))
        _, first_day, last_day, _ = parse_month(month)
        operation = {"aggregate": "attendance_logs", "pipeline": late_leaderboard_pipeline(first_day, last_day.isoformat(), department, limit), "cursor": {}}
        collection_name = "attendance_logs"
    else:
        if department is None or from_ is None or to is None:
            validation_error("department, from, and to are required", ("query",))
        from_day, to_day = date.fromisoformat(from_), date.fromisoformat(to)
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            validation_error("trend requires to >= from and a range of at most 92 days", ("query", "to"))
        operation = {"aggregate": "employees", "pipeline": department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat()), "cursor": {}}
        collection_name = "employees"
    return {"endpoint": endpoint, "collection": collection_name, "explain": explain_command(database, operation)}
