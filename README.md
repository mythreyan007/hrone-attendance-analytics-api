# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the HROne Software Engineer Trainee assignment.

## Setup
Python 3.11+ and MongoDB 6+ (Atlas or local instance) are required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "your MongoDB connection URI"
$env:MONGO_DB = "attendance_db"
python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000/docs. Do not commit credentials or a `.env` file.

## Endpoints
Health, employee create/list, attendance punch-in/punch-out/list/regularization, monthly employee analytics, department summary, late leaderboard, department trend, and MongoDB explain.

## Important
This is a candidate implementation that must be run against MongoDB and validated with the assignment's edge cases before submission. In particular, verify aggregation pipelines and explain plans against the exact MongoDB version used by the grader, and test overnight shifts, concurrent requests, empty-log employees, ties, and date-range boundaries. Do not represent untested behavior as verified.
