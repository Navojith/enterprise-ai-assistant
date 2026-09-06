---
title: "Payment Retry and Idempotency Specification"
department: payments
document_type: product_spec
access_level: internal
created_date: 2026-05-02
---

# Payment Retry and Idempotency Specification

## Problem Statement

Customers and internal stakeholders have identified a need addressed by "Payment Retry and Idempotency Specification". This specification defines the scope of the feature and the behavior it must implement.

## Requirements

Every client-initiated retry of a payment request must carry the same idempotency key as the original attempt, the system must return the original transaction's result rather than creating a duplicate when a retried key is recognized, and idempotency keys must be retained for at least 24 hours after the original request.

## Out of Scope

This specification does not cover retries initiated by the card network itself after settlement, which are handled by the reconciliation process instead.
