# DECISIONS.md

1. **Indexes.**
We created unique indexes `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce business uniqueness and prevent concurrent duplicate writes. For query performance, we added `employees(department)` for department filtering, `attendance_logs(date, emp_code)` for attendance listing/sorting, `attendance_logs(emp_code, punch_in)` for atomic punch-outs, and `attendance_logs(date, late_minutes)` for late leaderboard aggregations. We considered and rejected an index on `attendance_logs(status)` because status has very low cardinality and offers negligible index selectivity on large datasets.

2. **Punch-in race.**
Both requests find the valid employee. Both compute the IST date and attempt `attendance_logs.insert_one()`. MongoDB's unique index `(emp_code, date)` guarantees atomicity: exactly one insert succeeds (returning 201 Created), while the concurrent racer fails with `DuplicateKeyError`, which our handler catches and returns HTTP 409 Conflict.

3. **Ties.**
We use `$setWindowFields` with standard competition ranking `$rank`. If two employees tie at the cutoff rank, both share the same rank (`rank <= limit`), and both are returned, preserving complete leaderboard accuracy without arbitrary truncation.

4. **Headcount.**
The aggregation pipeline starts from `employees` (filtering `joined_on <= month_end`), groups by department to establish total headcount, and uses `$lookup` to join attendance logs. This ensures employees with zero logs are fully counted in headcount and averages.

5. **One thing you would change for 100x data.**
Shard `attendance_logs` by a hashed `(emp_code, date)` key or pre-aggregate daily department metrics via time-series collections to eliminate expensive runtime lookups across hundreds of millions of records.
