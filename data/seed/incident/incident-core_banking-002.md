---
title: "Incident Report: Core ledger read replica falling behind primary"
department: core_banking
document_type: incident
access_level: internal
created_date: 2026-02-05
---

# Incident Report: Core ledger read replica falling behind primary

## Summary

On 2026-02-05, the core banking team responded to an incident involving core ledger read replica falling behind primary. The issue was detected by automated monitoring and triaged within the team's standard response window.

## Impact

Read-only reporting queries against the replica returned data up to 20 minutes stale. The primary ledger's write path and customer-facing balance checks, which read from the primary, were unaffected.

## Resolution

The team found a large batch export job saturating the replica's disk I/O, paused the export, and replication lag returned to normal within 30 minutes. The export was rescheduled to run outside business hours.
