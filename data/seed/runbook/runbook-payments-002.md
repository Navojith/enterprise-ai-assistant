---
title: "Card Network Reconciliation Runbook"
department: payments
document_type: runbook
access_level: internal
created_date: 2026-06-27
---

# Card Network Reconciliation Runbook

## Purpose

This runbook defines the standard operating procedure for the payments on-call team to follow when responding to the failure mode covered by "Card Network Reconciliation Runbook".

## Detection

The nightly reconciliation job flags a mismatch when the settled transaction count or total value differs from the internal ledger by more than the configured tolerance.

## Response Steps

1. Pull the card network's settlement file and the internal ledger export for the affected settlement date.
2. Run the reconciliation diff tool to isolate the specific transactions causing the mismatch.
3. Classify each discrepancy as a timing difference, a declined-but-recorded transaction, or a genuine data error.
4. Correct genuine data errors in the ledger and document timing differences expected to resolve on the next settlement cycle.
5. Confirm the corrected totals reconcile before closing the ticket.

## Escalation

If the discrepancy exceeds the regulatory reporting threshold or cannot be explained within one business day, escalate to the payments finance team and start the incident disclosure process.
