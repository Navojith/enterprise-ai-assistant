---
title: "Core Ledger System Architecture"
department: core_banking
document_type: architecture
access_level: internal
created_date: 2025-11-16
---

# Core Ledger System Architecture

## Overview

This document describes the architecture of the system covered by "Core Ledger System Architecture", owned by the core banking team, including its major components and how they interact.

## Components

The ledger is built around an append-only transaction log, a materialized balance view rebuilt from that log, and a primary-replica database cluster that serves reads from replicas and all writes from the primary.

## Reliability Considerations

Every write to the transaction log is synchronously replicated to at least one standby before being acknowledged, so a primary failure cannot lose an already-confirmed transaction, and balance reads can fall back to the append-only log if the materialized view is temporarily unavailable.

## Related Runbooks

Operational procedures for this system are covered by the following runbooks: "Ledger Database Failover Runbook"; "End-of-Day Batch Recovery Runbook"; "Database Connection Pool Exhaustion Runbook".
