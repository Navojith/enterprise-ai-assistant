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

The HR system generates an offboarding ticket automatically once a termination date is recorded, listed on the Offboarding Queue dashboard.

## Response Steps

1. Confirm the employee's last working day and required access-revocation time from the offboarding ticket.
2. Disable the employee's single sign-on account at the scheduled revocation time.
3. Revoke access to any system not covered by single sign-on, using the employee's system access list from the identity platform.
4. Confirm the employee's physical badge access is deactivated with the facilities team.
5. Mark the offboarding ticket complete once every access-list item is confirmed revoked.

## Escalation

If the offboarding is involuntary and flagged as high-risk, escalate to the security team to revoke access immediately rather than waiting for the scheduled time.
