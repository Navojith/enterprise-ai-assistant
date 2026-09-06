---
title: "Mobile Check Deposit Feature Specification"
department: product
document_type: product_spec
access_level: internal
created_date: 2025-10-17
---

# Mobile Check Deposit Feature Specification

## Problem Statement

Customers and internal stakeholders have identified a need addressed by "Mobile Check Deposit Feature Specification". This specification defines the scope of the feature and the behavior it must implement.

## Requirements

The feature must extract the check amount and account number from a photo with sufficient confidence before submission, must hold deposited funds according to the bank's standard funds-availability schedule, and must flag a photo that fails quality checks for the customer to retake rather than submitting it for processing.

## Out of Scope

This specification does not cover deposit limits for business banking accounts, which follow a separate approval workflow.
