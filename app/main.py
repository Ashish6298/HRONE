"""
Employee Attendance & Analytics API - Main Application File

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment variables with optional local .env fallback)
"""
from __future__ import annotations

import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

# --------------------------------------------------------------------------- #
# Environment & MongoDB Lifecycle
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezone definitions
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# Global database client and db handle (managed via FastAPI lifespan)
mongo_client: Optional[MongoClient] = None
db: Optional[Database] = None


def get_mongo_client() -> MongoClient:
    global mongo_client
    if mongo_client is None:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            tz_aware=True,
        )
    return mongo_client


def get_db() -> Database:
    global db
    if db is None:
        client = get_mongo_client()
        db = client[MONGO_DB]
    return db


def init_indexes(database: Database) -> None:
    """
    Idempotently creates all required startup indexes.
    """
    # 1. Unique index on employees.emp_code
    database.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uniq_emp_code",
    )

    # 2. Index on employees.department to optimize department-filtered queries
    database.employees.create_index(
        [("department", ASCENDING)],
        name="idx_emp_department",
    )

    # 3. Unique compound index on attendance_logs (emp_code, date)
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_code_date",
    )

    # 4. Compound index on attendance_logs for listing and date sorting
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_emp",
    )

    # 5. Compound index on attendance_logs for employee punch-out & history lookups
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_emp_punch_in",
    )

    # 6. Index on attendance_logs (date) for date-based lookups and joins
    database.attendance_logs.create_index(
        [("date", ASCENDING)],
        name="idx_attendance_date",
    )

    # 7. Compound index on attendance_logs (date, late_minutes) for late leaderboard queries
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_attendance_date_late",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize MongoDB connection and indexes
    try:
        database = get_db()
        database.command("ping")
        init_indexes(database)
    except Exception:
        pass
    yield
    # Shutdown: Cleanly close Mongo client
    global mongo_client, db
    if mongo_client:
        mongo_client.close()
        mongo_client = None
        db = None


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ServerSelectionTimeoutError)
@app.exception_handler(ConnectionFailure)
async def mongo_connection_exception_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})



# --------------------------------------------------------------------------- #
# Domain Enums & Schema Constants
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Shared Time, Rounding & Conversion Helpers
# --------------------------------------------------------------------------- #
def validate_epoch_millis(val: Any) -> int:
    """
    Validates integer epoch milliseconds strictly.
    Rejects booleans, floats, strings, and values outside [100000000000, 4102444800000].
    """
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("Timestamp must be an integer count of epoch milliseconds")
    if val < EPOCH_MIN or val > EPOCH_MAX:
        raise ValueError(f"Epoch milliseconds out of range [{EPOCH_MIN}, {EPOCH_MAX}]")
    return val


def truncate_to_seconds(dt: datetime) -> datetime:
    """
    Truncates a datetime object to whole seconds (drops microseconds).
    """
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime_utc(epoch_ms: int) -> datetime:
    """
    Converts epoch milliseconds to a UTC-aware datetime truncated to whole seconds.
    """
    validate_epoch_millis(epoch_ms)
    seconds = epoch_ms // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a datetime (UTC or aware) to integer epoch milliseconds.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def round_half_up(value: Union[float, Decimal, int], decimals: int = 2) -> float:
    """
    R8 Rounding: Rounds using Decimal ROUND_HALF_UP (not banker's rounding).
    """
    if value is None:
        return None
    d = Decimal(str(value))
    target_exp = Decimal("1." + "0" * decimals) if decimals > 0 else Decimal("1")
    rounded = d.quantize(target_exp, rounding=ROUND_HALF_UP)
    return float(rounded)


def get_attendance_date_from_punch_in(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Rule:
    Determines attendance date YYYY-MM-DD from punch_in instant.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started on the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST) if punch_in_utc.tzinfo else punch_in_utc.replace(tzinfo=UTC).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = time(eh, em) <= time(sh, sm)
    if is_overnight and punch_in_ist.time() < time(eh, em):
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Serializes MongoDB documents for HTTP response:
    - Strips MongoDB '_id' and prevents synthetic 'id'
    - Converts BSON datetimes to epoch milliseconds
    - Normalizes legacy missing fields (half_day=False, history=[])
    """
    if not doc:
        return doc
    clean = {}
    for k, v in doc.items():
        if k == "_id" or k == "id":
            continue
        if isinstance(v, datetime):
            clean[k] = datetime_to_epoch_ms(v)
        elif k == "history" and isinstance(v, list):
            serialized_history = []
            for entry in v:
                h_entry = dict(entry)
                if isinstance(h_entry.get("at"), datetime):
                    h_entry["at"] = datetime_to_epoch_ms(h_entry["at"])
                if "changes" in h_entry and isinstance(h_entry["changes"], dict):
                    c_clean = {}
                    for field_name, change in h_entry["changes"].items():
                        c_dict = dict(change)
                        if isinstance(c_dict.get("from"), datetime):
                            c_dict["from"] = datetime_to_epoch_ms(c_dict["from"])
                        if isinstance(c_dict.get("to"), datetime):
                            c_dict["to"] = datetime_to_epoch_ms(c_dict["to"])
                        c_clean[field_name] = c_dict
                    h_entry["changes"] = c_clean
                serialized_history.append(h_entry)
            clean["history"] = serialized_history
        else:
            clean[k] = v

    if "half_day" not in clean and "status" in clean:
        clean["half_day"] = False
    if "history" not in clean and "status" in clean:
        clean["history"] = []
    return clean


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models (Exact OpenAPI Matching)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EmployeeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: List[EmployeeResponse]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_millis(v)
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_millis(v)
        return v


class HistoryChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_: Optional[Any] = Field(None, alias="from")
    to: Optional[Any] = None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Optional[Any]]]


class AttendanceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    date: str
    status: StatusEnum
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int = 0
    overtime_minutes: int = 0
    half_day: bool = False
    history: List[HistoryEntry] = Field(default_factory=list)


class AttendancePage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_punches(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_millis(v)
        return v


class EmployeeMonthly(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    department: str
    items: List[TrendItem]





# --------------------------------------------------------------------------- #
# Shift & Attendance Time Engine Helpers (R1-R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2 Late Rule:
    Grace period is 10 minutes. A punch-in is late only if strictly > 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    punch_in_ist = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=UTC).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = punch_in_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:  # Strictly greater than 10 minutes (600 seconds)
        return int(math.floor(elapsed_seconds / 60))
    return 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    """
    R4 Work Hours Rule:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if not punch_in or not punch_out:
        return None
    elapsed_seconds = (punch_out - punch_in).total_seconds()
    if elapsed_seconds < 0:
        return None
    raw_hours = elapsed_seconds / 3600.0
    return round_half_up(raw_hours, 2)


def compute_overtime(punch_out: Optional[datetime], shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    """
    R3 Overtime Rule:
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    Handles overnight shifts where shift_end falls on next calendar day.
    """
    if not punch_out:
        return 0
    punch_out_ist = punch_out.astimezone(IST) if punch_out.tzinfo else punch_out.replace(tzinfo=UTC).astimezone(IST)
    d = date.fromisoformat(date_str)
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    end_dt = datetime.combine(d, time(eh, em), tzinfo=IST)
    if time(eh, em) <= time(sh, sm):
        # Overnight shift: shift_end is on the next calendar day
        end_dt += timedelta(days=1)

    elapsed_seconds = (punch_out_ist - end_dt).total_seconds()
    if elapsed_seconds >= 1800:  # At least 30 minutes (1800 seconds)
        return int(math.floor(elapsed_seconds / 60))
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half Day Rule:
    After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """
    Readiness probe.
    Returns 200 {"status": "ok"} when MongoDB answers a ping, otherwise 503.
    """
    try:
        database = get_db()
        database.command("ping")
        return {"status": "ok"}
    except (PyMongoError, ServerSelectionTimeoutError, ConnectionFailure, Exception):
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=EmployeeResponse, tags=["employees"])
def create_employee(body: EmployeeCreate):
    database = get_db()
    if database.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc = body.model_dump()
    created_at = datetime.now(UTC).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_doc(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = get_db()
    q = {}
    if department:
        q["department"] = department

    total = database.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        ts_utc = datetime.now(UTC).replace(microsecond=0)

    # Determine attendance date using shared helper (R1)
    record_date = get_attendance_date_from_punch_in(ts_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts_utc, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status.value,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_out_utc = datetime.now(UTC).replace(microsecond=0)

    # Find the most recent attendance record for the employee with punch_in <= punch_out_utc
    record = database.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No eligible punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    # Validate timing rules: punched_at must be strictly > punch_in and <= 24 hours later
    elapsed_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if elapsed_seconds > 86400:  # 24 hours * 3600
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived values
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_mins = compute_overtime(
        punch_out_utc,
        emp["shift_end"],
        record["date"],
        shift_start=emp["shift_start"],
    )
    half_day = compute_half_day(work_hours)

    # Atomic conditional update: only close if punch_out is still null
    updated_doc = database.attendance_logs.find_one_and_update(
        {
            "emp_code": record["emp_code"],
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Attendance record already closed by another request")

    return serialize_doc(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Determine target status and punch times from request or current record
    fields_set = body.model_fields_set
    new_status_enum = body.status if "status" in fields_set else StatusEnum(record["status"])
    new_status = new_status_enum.value

    current_punch_in = record.get("punch_in")
    if current_punch_in and current_punch_in.tzinfo is None:
        current_punch_in = current_punch_in.replace(tzinfo=UTC)

    current_punch_out = record.get("punch_out")
    if current_punch_out and current_punch_out.tzinfo is None:
        current_punch_out = current_punch_out.replace(tzinfo=UTC)

    # Status / punch combinations validation
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(status_code=422, detail="Cannot supply punch times for ABSENT or LEAVE status")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if "punch_in" in fields_set:
            final_punch_in = epoch_ms_to_datetime_utc(body.punch_in) if body.punch_in is not None else None
        else:
            final_punch_in = current_punch_in

        if not final_punch_in:
            raise HTTPException(status_code=422, detail=f"{new_status} status requires a punch_in instant")

        # Verify punch_in remains on the record's attendance date (R1)
        expected_date = get_attendance_date_from_punch_in(final_punch_in, emp["shift_start"], emp["shift_end"])
        if expected_date != date_str:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in maps to attendance date {expected_date}, which does not match record date {date_str}",
            )

        if "punch_out" in fields_set:
            final_punch_out = epoch_ms_to_datetime_utc(body.punch_out) if body.punch_out is not None else None
        else:
            final_punch_out = current_punch_out

        if final_punch_out is not None:
            elapsed_seconds = (final_punch_out - final_punch_in).total_seconds()
            if elapsed_seconds <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if elapsed_seconds > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        final_late_mins = 0
        final_work_hours = None
        final_overtime_mins = 0
        final_half_day = False
    else:
        final_late_mins = compute_late_minutes(final_punch_in, emp["shift_start"])
        final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
        final_overtime_mins = compute_overtime(
            final_punch_out,
            emp["shift_end"],
            date_str,
            shift_start=emp["shift_start"],
        )
        final_half_day = compute_half_day(final_work_hours)

    # Track actual changes
    changes: Dict[str, Dict[str, Any]] = {}

    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}

    if current_punch_in != final_punch_in:
        changes["punch_in"] = {"from": current_punch_in, "to": final_punch_in}

    if current_punch_out != final_punch_out:
        changes["punch_out"] = {"from": current_punch_out, "to": final_punch_out}

    if record.get("work_hours") != final_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": final_work_hours}

    if record.get("late_minutes", 0) != final_late_mins:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": final_late_mins}

    if record.get("overtime_minutes", 0) != final_overtime_mins:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": final_overtime_mins}

    if record.get("half_day", False) != final_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in regularization request")

    # Prepare audit history entry
    correction_at = datetime.now(UTC).replace(microsecond=0)
    history_entry = {
        "at": correction_at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency / atomic update on matching history length / state
    current_history = record.get("history") or []
    current_history_len = len(current_history)

    updated_doc = database.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date_str,
            "$or": [
                {"history": {"$size": current_history_len}},
                {"history": {"$exists": False}} if current_history_len == 0 else {"_id": record["_id"]},
            ],
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_mins,
                "overtime_minutes": final_overtime_mins,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_doc(updated_doc)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    database = get_db()
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = database.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        database.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Milestone 5: Employee Monthly Analytics Endpoint
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    tags=["analytics"],
)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    month_start_date = f"{year:04d}-{m:02d}-01"
    if m == 12:
        next_month_start_date = f"{year+1:04d}-01-01"
        month_end_date = f"{year:04d}-12-31"
    else:
        next_month_start_date = f"{year:04d}-{m+1:02d}-01"
        # Find last day of month
        last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
        month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

    # Pipeline to compute employee monthly attendance metrics directly in MongoDB
    pipeline = build_employee_monthly_pipeline(emp_code, month_start_date, month_end_date)

    agg_result = list(database.attendance_logs.aggregate(pipeline))

    if agg_result:
        metrics = agg_result[0]
        present_days = float(metrics.get("present_days", 0.0))
        leave_days = int(metrics.get("leave_days", 0))
        late_count = int(metrics.get("late_count", 0))
        total_late_minutes = int(metrics.get("total_late_minutes", 0))
        total_overtime_minutes = int(metrics.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # Calculate working_days via MongoDB date expression: Monday-Friday from max(month_start, joined_on) to month_end
    effective_start = max(month_start_date, emp["joined_on"])
    if effective_start > month_end_date:
        working_days = 0
    else:
        working_days_pipeline = [
            {
                "$documents": [
                    {
                        "start": {"$dateFromString": {"dateString": effective_start, "format": "%Y-%m-%d"}},
                        "end": {"$dateFromString": {"dateString": month_end_date, "format": "%Y-%m-%d"}},
                    }
                ]
            },
            {
                "$project": {
                    "days": {
                        "$range": [
                            0,
                            {
                                "$add": [
                                    {
                                        "$dateDiff": {
                                            "startDate": "$start",
                                            "endDate": "$end",
                                            "unit": "day",
                                        }
                                    },
                                    1,
                                ]
                            },
                        ]
                    },
                    "start": 1,
                }
            },
            {"$unwind": "$days"},
            {
                "$project": {
                    "current_date": {
                        "$dateAdd": {
                            "startDate": "$start",
                            "unit": "day",
                            "amount": "$days",
                        }
                    }
                }
            },
            {
                "$match": {
                    "$expr": {
                        "$and": [
                            {"$gt": [{"$dayOfWeek": "$current_date"}, 1]},
                            {"$lt": [{"$dayOfWeek": "$current_date"}, 7]},
                        ]
                    }
                }
            },
            {"$count": "working_days"},
        ]
        wd_result = list(database.attendance_logs.aggregate(working_days_pipeline))
        working_days = wd_result[0]["working_days"] if wd_result else 0

    # Calculate attendance_pct
    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Milestone 6: Cross-Collection & Ranking Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    tags=["analytics"],
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    database = get_db()
    year, m = map(int, month.split("-"))
    month_start_date = f"{year:04d}-{m:02d}-01"
    if m == 12:
        month_end_date = f"{year:04d}-12-31"
    else:
        last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
        month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(month_start_date, month_end_date, department)

    items = list(database.employees.aggregate(pipeline))
    # Post-process precision using round_half_up (R8)
    formatted_items = []
    for item in items:
        avg_wh = item.get("avg_work_hours")
        formatted_items.append(
            {
                "department": item["department"],
                "headcount": item["headcount"],
                "present_days": round_half_up(item.get("present_days", 0.0), 2),
                "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
                "late_count": item.get("late_count", 0),
                "total_late_minutes": item.get("total_late_minutes", 0),
                "leave_count": item.get("leave_count", 0),
                "on_duty_count": item.get("on_duty_count", 0),
            }
        )

    return {
        "month": month,
        "items": formatted_items,
    }


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    tags=["analytics"],
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    database = get_db()
    year, m = map(int, month.split("-"))
    month_start_date = f"{year:04d}-{m:02d}-01"
    if m == 12:
        month_end_date = f"{year:04d}-12-31"
    else:
        last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
        month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start_date, month_end_date, limit, department)

    items = list(database.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Milestone 7: Department Daily Trend & Window Calculation Endpoint
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    tags=["analytics"],
)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="'from' date must be <= 'to' date")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    total_days = (d_to - d_from).days + 1
    if total_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 calendar days")

    database = get_db()
    # Check if department exists in employees collection (404 if unknown)
    dept_exists = database.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)

    rows = list(database.attendance_logs.aggregate(pipeline))

    # Apply R8 4-decimal half-up rounding to rates and 2-decimal rounding to counts
    formatted_items = []
    for r in rows:
        att_rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        formatted_items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": round_half_up(r["present_count"], 2),
                "late_count": r["late_count"],
                "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
                "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
            }
        )

    return {
        "department": department,
        "items": formatted_items,
    }


# --------------------------------------------------------------------------- #
# Milestone 8: Explain Endpoint & Performance Proof
# --------------------------------------------------------------------------- #
class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    collection: str
    explain: Dict[str, Any]


def build_employee_monthly_pipeline(emp_code: str, month_start_date: str, month_end_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start_date, "$lte": month_end_date},
            }
        },
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {"$dayOfWeek": "$date_obj"},
                "is_weekday": {
                    "$and": [
                        {"$gt": [{"$dayOfWeek": "$date_obj"}, 1]},
                        {"$lt": [{"$dayOfWeek": "$date_obj"}, 7]},
                    ]
                },
            }
        },
        {
            "$addFields": {
                "present_contribution": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_weekday",
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                },
                "is_leave": {
                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0],
                },
                "is_late": {
                    "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0],
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contribution"},
                "leave_days": {"$sum": "$is_leave"},
                "late_count": {"$sum": "$is_late"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month_start_date: str, month_end_date: str, department_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    match_pred: Dict[str, Any] = {"joined_on": {"$lte": month_end_date}}
    if department_filter:
        match_pred["department"] = department_filter

    return [
        {"$match": match_pred},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start_date]},
                                    {"$lte": ["$date", month_end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                        }
                    },
                    {
                        "$addFields": {
                            "dayOfWeek": {"$dayOfWeek": "$date_obj"},
                            "is_weekday": {
                                "$and": [
                                    {"$gt": [{"$dayOfWeek": "$date_obj"}, 1]},
                                    {"$lt": [{"$dayOfWeek": "$date_obj"}, 7]},
                                ]
                            },
                        }
                    },
                    {
                        "$addFields": {
                            "present_val": {
                                "$cond": [
                                    {
                                        "$and": [
                                            "$is_weekday",
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                            "is_late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
                            "is_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                            "is_on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                            "is_qualifying_work_record": {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": "$$l.present_val",
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": "$$l.is_late",
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": "$$l.is_leave",
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": "$$l.is_on_duty",
                        }
                    }
                },
                "qualifying_work_hours": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": "$$l.is_qualifying_work_record",
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$qualifying_work_hours"}, 0]},
                        {"$avg": "$qualifying_work_hours.work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


def build_late_leaderboard_pipeline(month_start_date: str, month_end_date: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    pipe: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start_date, "$lte": month_end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department:
        pipe.append({"$match": {"department": department}})

    pipe.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": limit},
                }
            },
            {
                "$sort": {
                    "total_late_minutes": DESCENDING,
                    "emp_code": ASCENDING,
                }
            },
        ]
    )
    return pipe


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$documents": [
                {
                    "start": {"$dateFromString": {"dateString": from_date, "format": "%Y-%m-%d"}},
                    "end": {"$dateFromString": {"dateString": to_date, "format": "%Y-%m-%d"}},
                }
            ]
        },
        {
            "$project": {
                "days": {
                    "$range": [
                        0,
                        {
                            "$add": [
                                {
                                    "$dateDiff": {
                                        "startDate": "$start",
                                        "endDate": "$end",
                                        "unit": "day",
                                    }
                                },
                                1,
                            ]
                        },
                    ]
                },
                "start": 1,
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "date_obj": {
                    "$dateAdd": {
                        "startDate": "$start",
                        "unit": "day",
                        "amount": "$days",
                    }
                }
            }
        },
        {
            "$project": {
                "date_obj": 1,
                "date_str": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}},
                "dayOfWeek": {"$dayOfWeek": "$date_obj"},
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "date_obj": 1,
                "is_working_day": {
                    "$and": [
                        {"$gt": ["$dayOfWeek", 1]},
                        {"$lt": ["$dayOfWeek", 7]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_info",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$cond": [
                        {"$gt": [{"$size": "$hc_info"}, 0]},
                        {"$arrayElemAt": ["$hc_info.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"current_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$current_day"],
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department,
                        }
                    },
                    {
                        "$project": {
                            "present_val": {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                            "is_late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": "$present_val"},
                            "late_count": {"$sum": "$is_late"},
                        }
                    },
                ],
                "as": "log_metrics",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$cond": [
                        {"$gt": [{"$size": "$log_metrics"}, 0]},
                        {"$arrayElemAt": ["$log_metrics.present_count", 0]},
                        0.0,
                    ]
                },
                "late_count": {
                    "$cond": [
                        {"$gt": [{"$size": "$log_metrics"}, 0]},
                        {"$arrayElemAt": ["$log_metrics.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    tags=["admin"],
)
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """
    Runs MongoDB explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, constructed with the exact filters, sort, and pagination.
    """
    database = get_db()
    endpoint_name = endpoint.value

    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status.value

        p = page or 1
        ps = page_size or 20
        skip = (p - 1) * ps

        try:
            cursor = (
                database.attendance_logs.find(q, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip(skip)
                .limit(ps)
            )
            explain_result = cursor.explain()
        except PyMongoError as e:
            explain_result = {"error": str(e), "verbosity": "executionStats"}

        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query parameter: emp_code")
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        year, m = map(int, month.split("-"))
        month_start_date = f"{year:04d}-{m:02d}-01"
        if m == 12:
            month_end_date = f"{year:04d}-12-31"
        else:
            last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
            month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

        pipeline = build_employee_monthly_pipeline(emp_code, month_start_date, month_end_date)
        try:
            explain_result = database.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
        except PyMongoError:
            try:
                explain_result = database.attendance_logs.aggregate(pipeline).explain()
            except PyMongoError as e:
                explain_result = {"error": str(e), "verbosity": "executionStats"}

        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        year, m = map(int, month.split("-"))
        month_start_date = f"{year:04d}-{m:02d}-01"
        if m == 12:
            month_end_date = f"{year:04d}-12-31"
        else:
            last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
            month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

        pipeline = build_department_summary_pipeline(month_start_date, month_end_date, department)
        try:
            explain_result = database.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
        except PyMongoError:
            try:
                explain_result = database.employees.aggregate(pipeline).explain()
            except PyMongoError as e:
                explain_result = {"error": str(e), "verbosity": "executionStats"}

        return {
            "endpoint": endpoint_name,
            "collection": "employees",
            "explain": explain_result,
        }

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        year, m = map(int, month.split("-"))
        month_start_date = f"{year:04d}-{m:02d}-01"
        if m == 12:
            month_end_date = f"{year:04d}-12-31"
        else:
            last_day = (date(year, m + 1, 1) - timedelta(days=1)).day
            month_end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

        lim = limit if limit is not None else 10
        pipeline = build_late_leaderboard_pipeline(month_start_date, month_end_date, lim, department)
        try:
            explain_result = database.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
        except PyMongoError:
            try:
                explain_result = database.attendance_logs.aggregate(pipeline).explain()
            except PyMongoError as e:
                explain_result = {"error": str(e), "verbosity": "executionStats"}

        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query parameter: department")
        if not from_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: from")
        if not to_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: to")
        if from_date > to_date:
            raise HTTPException(status_code=422, detail="'from' date must be <= 'to' date")

        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 calendar days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        try:
            explain_result = database.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
        except PyMongoError:
            try:
                explain_result = database.attendance_logs.aggregate(pipeline).explain()
            except PyMongoError as e:
                explain_result = {"error": str(e), "verbosity": "executionStats"}

        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": explain_result,
        }

    else:
        raise HTTPException(status_code=422, detail="Unsupported explain endpoint")



