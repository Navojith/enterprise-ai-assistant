---
title: "Incident Report: Payment Processing Disruption (2025-11-23)"
department: payments
document_type: incident
access_level: internal
created_date: 2025-11-23
---

# Incident Report: Payment Processing Disruption (2025-11-23)

## Summary

On 2025-11-23, the payments platform experienced a SEV-3 incident lasting approximately 34 minutes, during which an estimated 47,561 transactions were declined or delayed. Customer-facing impact was elevated authorization failure rates on card and transfer payments.

## Root Cause

The root cause was idempotency-key race condition under retry storms. When the payment gateway timeout above triggers client-side retries, a race between two concurrent requests sharing the same idempotency key occasionally let both reach the ledger write path before the first had recorded its key, producing a brief window of duplicate-charge risk that the reconciliation job later had to catch.

## Timeline

Alerting fired within minutes of the error-rate threshold breach. The on-call payments engineer engaged the runbook for this failure class, escalated to the platform team once initial mitigation did not recover the error rate, and confirmed full recovery after the underlying resource was restored.

## Remediation

Immediate mitigation restored service within the incident window. Follow-up actions were opened to add a dedicated early-warning alert for this specific failure mode and to reduce the blast radius of a recurrence.
