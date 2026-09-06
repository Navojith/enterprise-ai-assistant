---
title: "Database Connection Pool Exhaustion Runbook"
department: core_banking
document_type: runbook
access_level: internal
created_date: 2026-05-22
---

# Database Connection Pool Exhaustion Runbook

## Purpose

This runbook defines the standard operating procedure for the core banking on-call team to follow when responding to the failure mode covered by "Database Connection Pool Exhaustion Runbook".

## Detection

The connection-pool monitor alerts when active connections reach the pool's configured ceiling for more than one minute, visible on the affected service's dashboard.

## Response Steps

1. Identify the affected service and confirm whether the exhaustion is caused by a connection leak or a genuine traffic increase.
2. If a leak, identify and restart the specific instance holding stale connections rather than the whole fleet.
3. If traffic-driven, temporarily raise the pool ceiling within the database's documented connection limit.
4. Confirm queued requests drain and the pool's active-connection count returns below the alert threshold.
5. File a follow-up ticket to fix the leak or right-size the pool permanently once the immediate pressure is relieved.

## Escalation

If raising the pool ceiling risks exceeding the database's total connection limit shared across services, escalate to the database engineering on-call rotation before making the change.
