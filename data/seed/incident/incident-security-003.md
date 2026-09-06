---
title: "Incident Report: Elevated failed-login rate from a credential-stuffing attempt"
department: security
document_type: incident
access_level: internal
created_date: 2026-02-12
---

# Incident Report: Elevated failed-login rate from a credential-stuffing attempt

## Summary

On 2026-02-12, the security team responded to an incident involving elevated failed-login rate from a credential-stuffing attempt. The issue was detected by automated monitoring and triaged within the team's standard response window.

## Impact

A spike in failed login attempts was observed against the customer login endpoint. No accounts were confirmed compromised, but the elevated load briefly increased login latency for legitimate customers.

## Resolution

The security team enabled the elevated rate-limiting profile on the login endpoint and blocked the source IP ranges at the edge; login latency returned to normal within 10 minutes. A password reset was forced for the small number of accounts that showed suspicious activity.
