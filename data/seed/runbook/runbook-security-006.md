---
title: "Certificate Rotation Runbook"
department: security
document_type: runbook
access_level: internal
created_date: 2025-05-29
---

# Certificate Rotation Runbook

## Purpose

This runbook defines the standard operating procedure for the security on-call team to follow when responding to the failure mode covered by "Certificate Rotation Runbook".

## Detection

The certificate-expiry monitor alerts when a certificate has fewer than 14 days remaining before expiry, listed on the Certificate Inventory dashboard.

## Response Steps

1. Identify every service and integration that presents or validates the expiring certificate.
2. Generate the replacement certificate through the internal certificate authority and validate its chain.
3. Deploy the new certificate to a single instance first and confirm TLS handshakes succeed before a full rollout.
4. Roll the new certificate out to the remaining instances and confirm the old certificate is no longer in use.
5. Update the certificate inventory record with the new expiry date.

## Escalation

If a certificate has already expired and is causing active handshake failures, escalate immediately to the platform engineering on-call rotation rather than following the staged rollout.
