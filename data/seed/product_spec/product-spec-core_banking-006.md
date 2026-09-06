---
title: "Multi-Currency Ledger Support Specification"
department: core_banking
document_type: product_spec
access_level: internal
created_date: 2025-09-15
---

# Multi-Currency Ledger Support Specification

## Problem Statement

Customers and internal stakeholders have identified a need addressed by "Multi-Currency Ledger Support Specification". This specification defines the scope of the feature and the behavior it must implement.

## Requirements

The ledger must record each transaction's original currency and the exchange rate used at the time of conversion, must support reporting account balances in a customer's preferred display currency without altering the underlying recorded currency, and must reject a transaction if no exchange rate is available for the required currency pair.

## Out of Scope

This specification does not cover hedging or exposure management for currency risk, which is handled by treasury operations outside this system.
