---
title: "Fraud Detection Service Architecture"
department: security
document_type: architecture
access_level: internal
created_date: 2025-07-12
---

# Fraud Detection Service Architecture

## Overview

This document describes the architecture of the system covered by "Fraud Detection Service Architecture", owned by the security team, including its major components and how they interact.

## Components

The service is composed of a real-time scoring engine evaluating each transaction against a rules and machine-learning model ensemble, a feature store supplying recent account history, and a case-management interface for manual review of flagged transactions.

## Reliability Considerations

The scoring engine is configured to fail closed for high-value transactions but fail open with a conservative default score for lower-risk transactions, so a scoring-engine outage degrades fraud coverage rather than blocking all payments outright.

## Related Runbooks

Operational procedures for this system are covered by the following runbooks: "Credential-Stuffing Response Runbook"; "Certificate Rotation Runbook".
