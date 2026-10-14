# REVIEW.md

| # | Where (function / area) | What is wrong | How you'd notice it | How fixed |
|---|---|---|---|---|
| 1 | `health` | Returned `ok` without checking MongoDB | Stop database and call `/health` | Ping MongoDB; return 503 on failure |
| 2 | startup/config | No startup index creation | Inspect indexes / run explain | Idempotent indexes for employee key, department/join date, attendance natural key, date and status |
| 3 | `create_employee` | Check-then-insert race and no shift validation | Parallel duplicate requests; equal shift times | Unique index handles races; duplicate-key becomes 409; reject equal shifts |
| 4 | `list_employees` | `skip=page*page_size` skipped the first page and total ignored filters | Filter by department and request page 1 | Use `(page-1)*page_size`; count filtered query |
| 5 | time helpers | Used local machine time, wrong overnight shift day, naive datetimes | Set `punched_at` around midnight/overnight shift | Normalize instants to UTC, calculate attendance date in IST and account for overnight shifts |
| 6 | late calculation | Grace and minute rounding were incorrect | Check 09:40:00 and 09:40:01 for 09:30 shift | Apply strict >600 seconds grace; floor elapsed minutes |
| 7 | overtime | Did not handle overnight end and ignored 30-minute threshold | Punch out 20 minutes after shift end | Resolve next-day shift end and apply minimum 30-minute rule |
| 8 | work hours | Python `round` uses bankers' rounding | Test a half-way decimal case | Use `Decimal` with `ROUND_HALF_UP` |
| 9 | `punch_in` | Missing employee check, status validation and atomic duplicate handling | Unknown employee / parallel same-day punches | 404 unknown employee; restrict status; rely on unique natural-key index |
| 10 | `punch_out` | Missing entirely | Swagger route absent | Added atomic conditional update and derived-field calculation |
| 11 | `list_attendance` | Loaded all matching records into Python and did not validate date range | Large collection / reversed dates | Count, sort, skip and limit in MongoDB |
| 12 | regularization | Missing endpoint and audit trail | PATCH a record twice or concurrent PATCH requests | Validate changes, recompute derived fields, append history and use optimistic concurrency |
| 13 | analytics | Required analytics routes were absent | Swagger route list | Added MongoDB aggregation implementations for monthly, department, leaderboard and trend |
| 14 | explain | No explain endpoint | Call `/admin/explain/...` | Added endpoint returning MongoDB explain output |
| 15 | output serialization | Mongo `_id` could leak and timestamps were inconsistent | Inspect response JSON | Strip `_id`, expose `id`, serialize instants as epoch milliseconds |

Reviewed and retained: the natural employee code as the external identifier, status vocabulary from the contract, and `history` as a manual-correction-only audit trail.
