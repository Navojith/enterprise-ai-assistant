---
title: "Incident Report: Payment Processing Disruption (2025-11-11)"
department: payments
document_type: incident
access_level: internal
created_date: 2025-11-11
---

# Incident Report: Payment Processing Disruption (2025-11-11)

## Summary

On 2025-11-11, the payments platform experienced a SEV-2 incident lasting approximately 139 minutes, during which an estimated 4,511 transactions were declined or delayed. Customer-facing impact was elevated authorization failure rates on card and transfer payments.

## Root Cause

The root cause was expired TLS certificate on the payment gateway. The payment gateway's client certificate, used for mutual TLS with the acquiring bank, expired without triggering the renewal alert because the alert threshold was configured against the wrong certificate in the chain. Every transaction routed through that gateway instance failed the handshake.

## Timeline

Alerting fired within minutes of the error-rate threshold breach. The on-call payments engineer engaged the runbook for this failure class, escalated to the platform team once initial mitigation did not recover the error rate, and confirmed full recovery after the underlying resource was restored.

## Remediation

Immediate mitigation restored service within the incident window. Follow-up actions were opened to add a dedicated early-warning alert for this specific failure mode and to reduce the blast radius of a recurrence.
