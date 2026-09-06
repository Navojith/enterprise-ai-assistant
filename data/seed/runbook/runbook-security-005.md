---
title: "Credential-Stuffing Response Runbook"
department: security
document_type: runbook
access_level: internal
created_date: 2025-12-23
---

# Credential-Stuffing Response Runbook

## Purpose

This runbook defines the standard operating procedure for the security on-call team to follow when responding to the failure mode covered by "Credential-Stuffing Response Runbook".

## Detection

The fraud detection service alerts when failed-login attempts from a concentrated set of IP ranges exceed the baseline rate, visible on the Login Anomaly dashboard.

## Response Steps

1. Confirm the pattern is credential stuffing rather than a genuine traffic spike by checking the login endpoint's failure-to-success ratio and IP concentration.
2. Enable the elevated rate-limiting and CAPTCHA challenge profile on the login endpoint.
3. Block the confirmed malicious IP ranges at the edge/WAF layer.
4. Force a password reset for any account with a successful login from a flagged IP range.
5. Confirm the failed-login rate returns to baseline before standing down.

## Escalation

If any customer account is confirmed compromised, escalate to the fraud team and follow the incident disclosure policy for a potential customer data event.
