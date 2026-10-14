# DECISIONS.md

I use a unique index on `employees.emp_code` and a unique compound index on `attendance_logs(emp_code, date)` because these are natural keys and must remain unique even when requests race. Supporting indexes cover department/join-date lookups, date-sorted attendance pages, employee/date monthly lookups, and status/date filters. Index creation is idempotent at startup.

Two simultaneous punch-ins can both pass an application-level pre-check, so the database unique index is the authority: one insert succeeds with 201 and the other receives a duplicate-key error translated to 409. Punch-out uses a conditional update requiring `punch_out` to still be null; only one caller can close the record.

The leaderboard uses MongoDB competition ranking (`$rank`) before applying the rank cutoff. Employees tied at the cutoff are all returned, ordered by employee code. The department summary starts from employees who joined by the month end and looks up their logs, so employees with no logs still contribute to headcount; future joiners do not. For 100 times the data, I would profile representative explain plans, tune compound indexes from real query patterns, and consider pre-aggregated daily facts if live aggregations no longer meet latency goals.
