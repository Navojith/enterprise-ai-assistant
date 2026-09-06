---
title: "Payment Gateway Failover Runbook"
department: payments
document_type: runbook
access_level: internal
created_date: 2025-05-10
---

# Payment Gateway Failover Runbook

## Purpose

This runbook defines the standard operating procedure for the payments on-call team to follow when responding to the failure mode covered by "Payment Gateway Failover Runbook".

## Detection

The Payments Gateway Health dashboard alerts when the primary gateway's authorization success rate drops below 95% over a 5-minute window, or its p99 latency exceeds 3 seconds.

## Response Steps

1. Acknowledge the gateway-health alert and confirm the primary gateway's error rate on the dashboard.
2. Trigger the failover script to route new authorization traffic to the secondary gateway region.
3. Confirm the secondary gateway is accepting traffic and the authorization success rate recovers above 98%.
4. Monitor for 15 minutes to confirm the primary gateway's outage is not intermittent before declaring resolution.
5. Open a follow-up ticket to investigate the primary gateway's root cause and plan fail-back.

## Escalation

If the secondary gateway also shows a degraded authorization rate, escalate immediately to the platform engineering on-call rotation and the card-network vendor's emergency support line.
