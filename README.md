# Employee Attendance & Analytics API

Production-ready, high-performance Employee Attendance & Analytics API built with FastAPI, PyMongo, and MongoDB 6.0+/7.0.

---

## 1. Requirements & Prerequisites
- **Python:** 3.11 or newer
- **MongoDB:** 6.0 or newer (tested against MongoDB 7.0 / Atlas)

---

## 2. Setup & Installation

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` in your environment or via a local `.env` file:
   ```bash
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

4. **(Optional) Seed Sample Data:**
   ```bash
   python sample_seed.py
   ```
   *Note: Indexes are initialized idempotently at application startup; manual index creation is not required.*

---

## 3. Running the Application

Launch the server using Uvicorn:
```bash
uvicorn app.main:app --port 8000
```
- **Readiness / Health Check:** `GET http://localhost:8000/health` (returns `200 {"status": "ok"}` when MongoDB is reachable, `503` when unreachable).
- **Interactive OpenAPI Documentation:** `http://localhost:8000/docs`

---

## 4. Running the Test Suite

Execute all regression, contract, and concurrency test suites via pytest:
```bash
pytest -v
```

---

## 5. Architectural Highlights
- **All Implementation in `app/main.py`**: Cleanly structured with domain validators, atomic write operations, and shared MongoDB aggregation pipeline builders.
- **MongoDB Aggregations**: All analytics (monthly summary, department summary, late leaderboard, daily trend with 7-day moving average, and explain endpoints) run entirely inside MongoDB pipelines ($group, $lookup, $documents, $setWindowFields, $rank) without in-memory Python calculations.
- **Concurrency & Atomicity**: Atomic conditional updates (`find_one_and_update`) and unique compound indexes guarantee race safety for punch-in, punch-out, and regularization audit history.
