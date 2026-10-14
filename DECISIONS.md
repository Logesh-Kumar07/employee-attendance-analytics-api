# Decisions

1. **Indexes.** Startup adds nine indexes: unique employee `emp_code`; department/code and department/join-date for employee queries; joined-date/department/code for monthly headcount; unique attendance `(emp_code, date)` for identity and race safety; date/code and status/date/code for attendance queries; date/late/code for ranking; and employee/punch-in for latest-punch lookup. I rejected a `work_hours` index: no endpoint filters on it, so it would add storage and write cost without helping these access paths.

2. **Punch-in race.** Both requests may validate the employee and build a document. The unique `(emp_code, date)` index permits one insert; that request returns 201. The other gets duplicate-key error 11000, mapped to 409. There is no check-then-insert.

3. **Ties.** `$rank` gives equal totals the same competition rank. The limit applies to rank, not row count, so all employees tied at rank `limit` are returned, ordered by total minutes and employee code.

4. **Headcount.** The department pipeline starts from employees joined by month-end and looks up their monthly logs. Preserving an empty lookup keeps employees without logs in headcount; later joiners are excluded.

5. **100x data.** I would benchmark explain plans and latency first, then consider time-based archival or partitioning. Sharding would be a later choice only if measurements justified its operational cost.
