# Employee Attendance & Analytics API

FastAPI and synchronous PyMongo backend for the HROne trainee assignment. Application code is kept in [app/main.py](app/main.py), follows [openapi.yaml](openapi.yaml), and stores employee and attendance records in MongoDB collections described in [DATA_MODEL.md](DATA_MODEL.md). MongoDB is the source of truth for records and analytics; aggregation endpoints calculate results in the database.

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB 7 is also supported)
- PowerShell on Windows, or a shell on macOS/Linux

## Install from a clean clone

From the repository root (`candidate_kit`):

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

If PowerShell blocks activation, use the interpreter directly at `.venv\Scripts\python.exe` and `.venv\Scripts\uvicorn.exe`.

### macOS / Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

The local URI above is a safe example without credentials. For Atlas or another secured server, set its URI in your shell or an untracked local `.env`; never commit credentials. `python-dotenv` reads `.env` for convenience, and real environment variables take precedence. Both `MONGO_URI` and `MONGO_DB` must be set; neither connection target is hard-coded in the application.

## Start MongoDB and load sample data

Start a local MongoDB 6+ service, or optionally run a disposable local MongoDB 7 container:

```sh
docker run --name attendance-mongo -p 27017:27017 -d mongo:7
```

After setting `MONGO_URI` and `MONGO_DB`, load the supplied sample documents from the repository root:

```powershell
python sample_seed.py
```

**Warning:** the supplied seeder deletes and reloads both collections. Run it only against a disposable development database, never against a shared or production database. The sample data demonstrates document shapes; it does not cover the hidden edge cases.

## Run the API

From the repository root, with the virtual environment active and MongoDB configured:

```sh
uvicorn app.main:app --port 8000
```

The app creates its indexes idempotently during startup. `GET http://127.0.0.1:8000/health` returns `{"status":"ok"}` only when MongoDB answers a ping; otherwise it returns HTTP 503. Interactive Swagger UI is at `http://127.0.0.1:8000/docs`.

If MongoDB is not configured or is unavailable, the process can still start for orchestration, but `/health` and database-backed routes return 503. No connection string is embedded in application code.

## Example requests

Examples use `curl` (use `curl.exe` in Windows PowerShell). The epoch values below represent 2026-07-06 09:30 and 18:30 IST.

Create an employee:

```sh
curl -X POST http://127.0.0.1:8000/employees -H "Content-Type: application/json" -d '{"emp_code":"EMP9000","name":"Demo Employee","email":"demo@example.com","department":"Engineering","joined_on":"2026-07-01"}'
```

Punch in and out; API timestamps are integer epoch milliseconds. Each attendance date can have only one punch-in record for an employee.

```sh
curl -X POST http://127.0.0.1:8000/attendance/punch-in -H "Content-Type: application/json" -d '{"emp_code":"EMP9000","punched_at":1783310400000}'
curl -X POST http://127.0.0.1:8000/attendance/punch-out -H "Content-Type: application/json" -d '{"emp_code":"EMP9000","punched_at":1783342800000}'
```

List filtered/paginated attendance and request analytics:

```sh
curl "http://127.0.0.1:8000/attendance?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20"
curl "http://127.0.0.1:8000/analytics/employees/EMP9000/monthly?month=2026-07"
curl "http://127.0.0.1:8000/analytics/departments/summary?month=2026-07"
curl "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=10"
curl "http://127.0.0.1:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07"
curl "http://127.0.0.1:8000/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=20"
```

The full request and response contract, including correction requests and all error cases, is in [openapi.yaml](openapi.yaml); use Swagger UI or the tests to explore them.

## Index and aggregation notes

Startup creates unique indexes on `employees.emp_code` and `(attendance_logs.emp_code, attendance_logs.date)` for identity and race-safe inserts. Supporting employee indexes cover department listing, department/join-date selection, and unfiltered join-date selection. Attendance indexes support date/code ordering, status/date filtering, date/late ranking, and finding an employee's latest punch-in. Analytics use MongoDB pipelines, employee records as authoritative for department/headcount, and stored derived attendance values. MongoDB Decimal128 expressions implement half-up rounding for analytics outputs. Trend rows use aggregation date arithmetic and `$setWindowFields`, not a Python date loop. `/admin/explain/{endpoint}` runs execution-stats explain on the endpoint's corresponding query or pipeline.

## Tests and verification

Run the automated suite from the repository root:

```sh
python -m pytest -q
```

Verified in the current environment after the final contract checks: **18 passed, 4 skipped**, with one upstream Starlette/httpx TestClient deprecation warning. The skipped tests are MongoDB integration checks, including aggregation execution, actual concurrent writes, explain responses, and an opt-in 100,000-record plan check; they require a real MongoDB server and were not run. To run the regular integration checks against a disposable local MongoDB, set `MONGO_TEST_URI` (for example `mongodb://localhost:27017`) and run:

```powershell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
python -m pytest -q tests/test_mongo_integration.py
```

Integration tests create and drop a uniquely named `candidate_kit_test_*` database only. The unit suite uses `mongomock` and covers CRUD behavior, validation, time boundaries, corrections, and concurrency logic. Real MongoDB race behavior still requires the integration test run above.

For the larger explain-plan check, also set `MONGO_TEST_LARGE=1` and run:

```powershell
$env:MONGO_TEST_URI = "mongodb://localhost:27017"
$env:MONGO_TEST_LARGE = "1"
python -m pytest -q tests/test_mongo_integration.py::test_execution_plans_use_indexes_on_100k_records
```

## Known verification limits

MongoDB and Docker were not available in the implementation environment (`localhost:27017` ping timed out). The app did start with `uvicorn app.main:app --port 8000`; the live `/health` request returned HTTP 503 as expected without a MongoDB connection. Real-server analytics, startup index creation, concurrency races, and `IXSCAN`/`COLLSCAN` plans on a 100,000-record dataset could not be verified here. The explain endpoint is implemented but its real plan must be checked in the integration environment. No credentials, `.env`, virtual environment, or database dump belong in the submission; local environment and test caches are ignored by [.gitignore](.gitignore).
