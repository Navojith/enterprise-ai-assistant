---
title: "End-of-Day Batch Recovery Runbook"
department: core_banking
document_type: runbook
access_level: internal
created_date: 2025-09-12
---

# End-of-Day Batch Recovery Runbook

## Purpose

This runbook defines the standard operating procedure for the core banking on-call team to follow when responding to the failure mode covered by "End-of-Day Batch Recovery Runbook".

## Detection

The batch orchestrator alerts when a stage has not reported progress within its expected runtime window, visible on the End-of-Day Batch dashboard's stage timeline.

## Response Steps

1. Identify which batch stage stalled from the orchestrator's stage timeline.
2. Check that stage's logs for the specific record or partition that caused the failure.
3. Retry the stalled stage in isolation once the underlying data or resource issue is fixed.
4. Confirm downstream stages resume automatically once the stalled stage completes.
5. Verify the batch's completion report matches the expected record counts before close-of-business.

## Escalation

If the batch cannot complete before the regulatory cutoff time, escalate to the core banking engineering lead and notify operations that reporting will be delayed.
