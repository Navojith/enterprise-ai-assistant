---
title: "Mobile Release Rollback Runbook"
department: product
document_type: runbook
access_level: internal
created_date: 2025-12-25
---

# Mobile Release Rollback Runbook

## Purpose

This runbook defines the standard operating procedure for the product on-call team to follow when responding to the failure mode covered by "Mobile Release Rollback Runbook".

## Detection

The crash-reporting service alerts when the crash-free session rate for the newly released version drops below the release-health threshold.

## Response Steps

1. Confirm the elevated crash rate is specific to the new release version, not a device- or OS-level issue affecting all versions.
2. Halt the phased rollout so no additional users receive the affected version.
3. Trigger a server-side rollback to the previous stable version where the release supports it, or submit an expedited store rollback otherwise.
4. Monitor the crash-free session rate to confirm it recovers to baseline for users on the prior version.
5. File a release-blocking defect describing the crash signature for engineering to fix before the next release attempt.

## Escalation

If the crash affects a payment or authentication flow, escalate immediately to the payments or security on-call rotation in addition to the mobile platform team.
