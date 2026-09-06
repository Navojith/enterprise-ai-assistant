---
title: "Incident Report: Ledger reconciliation batch job stalling overnight"
department: core_banking
document_type: incident
access_level: internal
created_date: 2025-12-20
---

# Incident Report: Ledger reconciliation batch job stalling overnight

## Summary

On 2025-12-20, the core banking team responded to an incident involving ledger reconciliation batch job stalling overnight. The issue was detected by automated monitoring and triaged within the team's standard response window.

## Impact

Overnight reconciliation reports were delayed by several hours, so the finance team did not have the morning settlement summary available at the usual time; no customer-facing systems were affected.

## Resolution

The team identified a lock contention issue between the reconciliation job and a concurrent reporting query, killed the blocking query, and restarted the batch job from its last checkpoint. It completed successfully before the extended deadline.
