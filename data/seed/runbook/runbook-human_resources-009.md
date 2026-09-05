---
title: "Employee Offboarding Access Revocation Runbook"
department: human_resources
document_type: runbook
access_level: internal
created_date: 2025-06-09
---

# Employee Offboarding Access Revocation Runbook

## Purpose

This runbook defines the standard operating procedure for the human resources on-call team to follow when responding to the failure mode covered by "Employee Offboarding Access Revocation Runbook".

## Detection

This condition is typically surfaced by an automated alert tied to an elevated error rate, latency, or queue-depth threshold specific to the affected system.

## Response Steps

1. Acknowledge the alert and confirm the affected system.
2. Check the system's current health dashboard for corroborating signals before taking action.
3. Apply the documented mitigation for this failure mode.
4. Confirm recovery against the same signal that triggered the alert.
5. Open a follow-up ticket for any remediation that could not be completed during the incident.

## Escalation

If the mitigation does not restore normal operation within the runbook's expected recovery window, escalate to the platform engineering on-call rotation.
