---
title: "Ledger Database Failover Runbook"
department: core_banking
document_type: runbook
access_level: internal
created_date: 2025-09-26
---

# Ledger Database Failover Runbook

## Purpose

This runbook defines the standard operating procedure for the core banking on-call team to follow when responding to the failure mode covered by "Ledger Database Failover Runbook".

## Detection

An alert fires when the ledger primary fails its health check for two consecutive intervals, or replication lag on the standby exceeds the configured threshold.

## Response Steps

1. Confirm the primary ledger database is genuinely unreachable, not a transient network blip, by checking connectivity from two independent hosts.
2. Promote the most caught-up standby replica to primary using the documented promotion script.
3. Repoint the ledger service's connection string to the newly promoted primary and restart affected pods.
4. Verify write traffic resumes and the ledger's transaction-per-second metric returns to baseline.
5. Rebuild a new standby replica from the promoted primary to restore redundancy.

## Escalation

If promotion fails, or the standby's replication lag means data loss is possible, escalate immediately to the database engineering on-call rotation before proceeding further.
