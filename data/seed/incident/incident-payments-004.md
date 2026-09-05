---
title: "Incident Report: Payment Processing Disruption (2026-01-17)"
department: payments
document_type: incident
access_level: internal
created_date: 2026-01-17
---

# Incident Report: Payment Processing Disruption (2026-01-17)

## Summary

On 2026-01-17, the payments platform experienced a SEV-1 incident lasting approximately 17 minutes, during which an estimated 3,355 transactions were declined or delayed. Customer-facing impact was elevated authorization failure rates on card and transfer payments.

## Root Cause

The root cause was database connection pool exhaustion on the ledger service. A slow query introduced by an unrelated reporting job held connections open on the ledger service's primary pool for far longer than expected, starving the payment posting path of connections and causing writes to queue and eventually time out.

## Timeline

Alerting fired within minutes of the error-rate threshold breach. The on-call payments engineer engaged the runbook for this failure class, escalated to the platform team once initial mitigation did not recover the error rate, and confirmed full recovery after the underlying resource was restored.

## Remediation

Immediate mitigation restored service within the incident window. Follow-up actions were opened to add a dedicated early-warning alert for this specific failure mode and to reduce the blast radius of a recurrence.
