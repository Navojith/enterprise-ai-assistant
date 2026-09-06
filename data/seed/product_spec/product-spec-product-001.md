---
title: "Instant Payments Feature Specification"
department: product
document_type: product_spec
access_level: internal
created_date: 2025-09-02
---

# Instant Payments Feature Specification

## Problem Statement

Customers and internal stakeholders have identified a need addressed by "Instant Payments Feature Specification". This specification defines the scope of the feature and the behavior it must implement.

## Requirements

The feature must confirm a transfer to the recipient's account within 10 seconds end to end, must operate outside normal banking hours including weekends, and must fall back to a standard next-business-day transfer if the instant-payment rail is unavailable.

## Out of Scope

This specification does not cover cross-border instant transfers, which depend on a separate rail with different settlement guarantees and will be scoped separately.
