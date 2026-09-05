---
title: "Incident Report: Payment Processing Disruption (2026-03-05)"
department: payments
document_type: incident
access_level: internal
created_date: 2026-03-05
---

# Incident Report: Payment Processing Disruption (2026-03-05)

## Summary

On 2026-03-05, the payments platform experienced a SEV-1 incident lasting approximately 32 minutes, during which an estimated 26,142 transactions were declined or delayed. Customer-facing impact was elevated authorization failure rates on card and transfer payments.

## Root Cause

The root cause was card-network gateway timeout. The outbound connection pool to the card-network gateway exhausted its configured limit under peak load, causing new authorization requests to queue past their client-side timeout and fail as declines even though the card network itself was healthy throughout.

## Timeline

Alerting fired within minutes of the error-rate threshold breach. The on-call payments engineer engaged the runbook for this failure class, escalated to the platform team once initial mitigation did not recover the error rate, and confirmed full recovery after the underlying resource was restored.

## Remediation

Immediate mitigation restored service within the incident window. Follow-up actions were opened to add a dedicated early-warning alert for this specific failure mode and to reduce the blast radius of a recurrence.
