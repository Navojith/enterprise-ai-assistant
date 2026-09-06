---
title: "Real-Time Fraud Alerts Feature Specification"
department: product
document_type: product_spec
access_level: internal
created_date: 2026-01-10
---

# Real-Time Fraud Alerts Feature Specification

## Problem Statement

Customers and internal stakeholders have identified a need addressed by "Real-Time Fraud Alerts Feature Specification". This specification defines the scope of the feature and the behavior it must implement.

## Requirements

The feature must notify the customer within 30 seconds of a transaction the fraud engine scores above the alerting threshold, must let the customer confirm or dispute the transaction directly from the notification, and must not alert on a transaction the customer has pre-authorized as trusted.

## Out of Scope

This specification does not cover the fraud-scoring model itself, which is owned by the Fraud Detection Service and evolves independently of this notification feature.
