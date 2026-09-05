---
title: "Incident Report: Payment Processing Disruption (2025-12-10)"
department: payments
document_type: incident
access_level: internal
created_date: 2025-12-10
---

# Incident Report: Payment Processing Disruption (2025-12-10)

## Summary

On 2025-12-10, the payments platform experienced a SEV-2 incident lasting approximately 130 minutes, during which an estimated 28,881 transactions were declined or delayed. Customer-facing impact was elevated authorization failure rates on card and transfer payments.

## Root Cause

The root cause was third-party fraud-check API outage. The external fraud-scoring provider used to pre-screen high-value transfers returned 5xx errors for an extended window. Because the integration was configured to fail closed, every transaction requiring a fraud check was declined rather than falling back to a conservative default score.

## Timeline

Alerting fired within minutes of the error-rate threshold breach. The on-call payments engineer engaged the runbook for this failure class, escalated to the platform team once initial mitigation did not recover the error rate, and confirmed full recovery after the underlying resource was restored.

## Remediation

Immediate mitigation restored service within the incident window. Follow-up actions were opened to add a dedicated early-warning alert for this specific failure mode and to reduce the blast radius of a recurrence.
