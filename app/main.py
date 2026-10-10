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
from pymongo import ASCENDING, DESCENDING, MongoClient
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize MongoDB connection and indexes
    try:
        database = get_db()
        # Ping database with short timeout during startup
        database.command("ping")
        init_indexes(database)
    except Exception:
        # If MongoDB is unavailable at startup, log/pass and let /health report 503
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


# Allowed epoch milliseconds limits per openapi.yaml
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

    # Set safe defaults for optional/legacy fields if absent
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


# --------------------------------------------------------------------------- #
# Legacy Helper Wrappers (Refactored for correctness & reuse)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2 Late Rule:
    Grace period is 10 minutes. A punch-in is late only if strictly > 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    # punch_in is UTC datetime, convert to IST
    punch_in_ist = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=UTC).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = punch_in_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)

    # Elapsed seconds from shift_start
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:  # Strictly greater than 10 minutes (600 seconds)
        return int(math.floor(elapsed_seconds / 60))
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4 Work Hours Rule:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if not punch_in or not punch_out:
        return None
    elapsed_seconds = (punch_out - punch_in).total_seconds()
    raw_hours = elapsed_seconds / 3600.0
    return round_half_up(raw_hours, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
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

    # Convert to IST to determine attendance date (R1)
    ts_ist = ts_utc.astimezone(IST)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))

    is_overnight = time(eh, em) <= time(sh, sm)
    if is_overnight and ts_ist.time() < time(eh, em):
        # Belongs to shift that started previous calendar day
        record_date = (ts_ist.date() - timedelta(days=1)).isoformat()
    else:
        record_date = ts_ist.date().isoformat()

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
# End of Milestone 2 Foundation
# --------------------------------------------------------------------------- #
