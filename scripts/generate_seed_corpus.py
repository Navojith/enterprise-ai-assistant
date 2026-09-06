"""Generate the ~60-document synthetic corpus for a fictional commercial bank.

Writes one Markdown file per document to `data/seed/<document_type>/<document_id>.md`, each
with a YAML front-matter block carrying the metadata schema ASSESSMENT.md names
(`department`, `document_type`, `access_level`, `created_date`) plus a `title`, and a body
structured into `##` sections so `retrieval/chunking.py` has real section boundaries to split
on and attribute chunks to.

The corpus is synthetic — ASSESSMENT.md explicitly permits generated mock data — but the
payment-failure incidents are deliberately *not* random: they're drawn from a small, fixed
pool of five root causes that each recur two or three times across the last year, specifically
so the RLM research agent (Cycle 5) has real recurring patterns to find when asked ASSESSMENT.md's
own example question ("summarize outage reports related to payment failures... identify
recurring root causes") — a corpus where every incident has a unique cause would make that
demo impossible to give a meaningful answer to, however well the retrieval and RLM code works.

Run with `python -m scripts.generate_seed_corpus`. Deterministic (seeded), so re-running
regenerates byte-identical output — which is what makes ingestion's content-hash idempotency
check meaningful to demonstrate: a second `python -m scripts.ingest` run afterward should
re-embed nothing.
"""

from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from backend.app.retrieval.models import DEPARTMENTS, AccessLevel, DocumentType

_SEED = 20260905  # today's date, used only to make regeneration deterministic
_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "seed"
_TODAY = date(2026, 9, 5)


@dataclass
class GeneratedDocument:
    document_id: str
    title: str
    department: str
    document_type: DocumentType
    access_level: AccessLevel
    created_date: date
    sections: list[tuple[str, str]] = field(default_factory=list)  # (heading, body)

    def render(self) -> str:
        front_matter = textwrap.dedent(f"""\
            ---
            title: "{self.title}"
            department: {self.department}
            document_type: {self.document_type.value}
            access_level: {self.access_level.value}
            created_date: {self.created_date.isoformat()}
            ---
            """)
        body_parts = [f"# {self.title}\n"]
        for heading, text in self.sections:
            body_parts.append(f"## {heading}\n\n{text.strip()}\n")
        return front_matter + "\n" + "\n".join(body_parts)


# ---------------------------------------------------------------------------
# Payment-failure incidents: the fixed root-cause pool the RLM demo depends on.
# ---------------------------------------------------------------------------

_PAYMENT_ROOT_CAUSES = [
    (
        "card-network gateway timeout",
        "The outbound connection pool to the card-network gateway exhausted its configured "
        "limit under peak load, causing new authorization requests to queue past their "
        "client-side timeout and fail as declines even though the card network itself was "
        "healthy throughout.",
    ),
    (
        "expired TLS certificate on the payment gateway",
        "The payment gateway's client certificate, used for mutual TLS with the acquiring "
        "bank, expired without triggering the renewal alert because the alert threshold was "
        "configured against the wrong certificate in the chain. Every transaction routed "
        "through that gateway instance failed the handshake.",
    ),
    (
        "database connection pool exhaustion on the ledger service",
        "A slow query introduced by an unrelated reporting job held connections open on the "
        "ledger service's primary pool for far longer than expected, starving the payment "
        "posting path of connections and causing writes to queue and eventually time out.",
    ),
    (
        "idempotency-key race condition under retry storms",
        "When the payment gateway timeout above triggers client-side retries, a race between "
        "two concurrent requests sharing the same idempotency key occasionally let both reach "
        "the ledger write path before the first had recorded its key, producing a brief "
        "window of duplicate-charge risk that the reconciliation job later had to catch.",
    ),
    (
        "third-party fraud-check API outage",
        "The external fraud-scoring provider used to pre-screen high-value transfers returned "
        "5xx errors for an extended window. Because the integration was configured to fail "
        "closed, every transaction requiring a fraud check was declined rather than falling "
        "back to a conservative default score.",
    ),
]


def _payment_incidents(rng: random.Random) -> list[GeneratedDocument]:
    docs: list[GeneratedDocument] = []
    # Each root cause recurs 2-3 times, spread across the last ~13 months, so "recurring root
    # causes" is something a real analysis finds rather than something declared in a comment.
    occurrences: list[tuple[str, str]] = []
    for cause, explanation in _PAYMENT_ROOT_CAUSES:
        for _ in range(rng.choice([2, 3])):
            occurrences.append((cause, explanation))
    rng.shuffle(occurrences)

    for index, (cause, explanation) in enumerate(occurrences, start=1):
        days_ago = rng.randint(15, 395)
        created = _TODAY - timedelta(days=days_ago)
        severity = rng.choice(["SEV-1", "SEV-2", "SEV-2", "SEV-3"])
        duration_minutes = rng.randint(8, 140)
        affected = rng.randint(200, 48_000)
        doc_id = f"incident-payments-{index:03d}"
        docs.append(
            GeneratedDocument(
                document_id=doc_id,
                title=f"Incident Report: Payment Processing Disruption ({created.isoformat()})",
                department="payments",
                document_type=DocumentType.INCIDENT,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Summary",
                        f"On {created.isoformat()}, the payments platform experienced a "
                        f"{severity} incident lasting approximately {duration_minutes} minutes, "
                        f"during which an estimated {affected:,} transactions were declined or "
                        "delayed. Customer-facing impact was elevated authorization failure "
                        "rates on card and transfer payments.",
                    ),
                    (
                        "Root Cause",
                        f"The root cause was {cause}. {explanation}",
                    ),
                    (
                        "Timeline",
                        "Alerting fired within minutes of the error-rate threshold breach. "
                        "The on-call payments engineer engaged the runbook for this failure "
                        "class, escalated to the platform team once initial mitigation did not "
                        "recover the error rate, and confirmed full recovery after the "
                        "underlying resource was restored.",
                    ),
                    (
                        "Remediation",
                        "Immediate mitigation restored service within the incident window. "
                        "Follow-up actions were opened to add a dedicated early-warning alert "
                        "for this specific failure mode and to reduce the blast radius of a "
                        "recurrence.",
                    ),
                ],
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Everything else: templated but topically varied across departments.
# ---------------------------------------------------------------------------

_NON_PAYMENT_INCIDENT_TOPICS = [
    ("core_banking", "ledger reconciliation batch job stalling overnight"),
    ("core_banking", "core ledger read replica falling behind primary"),
    ("security", "elevated failed-login rate from a credential-stuffing attempt"),
    ("customer_support", "support ticketing system queue backlog during a product launch"),
    ("product", "mobile app crash loop after a configuration push"),
]

# Impact/Resolution content specific to each topic above — see `_RUNBOOK_DETAILS`'s docstring
# for why: identical boilerplate across every document of a type makes retrieval unable to
# tell them apart, no matter how the retrieval pipeline itself is tuned.
_NON_PAYMENT_INCIDENT_DETAILS: dict[str, dict[str, str]] = {
    "ledger reconciliation batch job stalling overnight": {
        "impact": (
            "Overnight reconciliation reports were delayed by several hours, so the finance "
            "team did not have the morning settlement summary available at the usual time; no "
            "customer-facing systems were affected."
        ),
        "resolution": (
            "The team identified a lock contention issue between the reconciliation job and a "
            "concurrent reporting query, killed the blocking query, and restarted the batch "
            "job from its last checkpoint. It completed successfully before the extended "
            "deadline."
        ),
    },
    "core ledger read replica falling behind primary": {
        "impact": (
            "Read-only reporting queries against the replica returned data up to 20 minutes "
            "stale. The primary ledger's write path and customer-facing balance checks, which "
            "read from the primary, were unaffected."
        ),
        "resolution": (
            "The team found a large batch export job saturating the replica's disk I/O, "
            "paused the export, and replication lag returned to normal within 30 minutes. The "
            "export was rescheduled to run outside business hours."
        ),
    },
    "elevated failed-login rate from a credential-stuffing attempt": {
        "impact": (
            "A spike in failed login attempts was observed against the customer login "
            "endpoint. No accounts were confirmed compromised, but the elevated load briefly "
            "increased login latency for legitimate customers."
        ),
        "resolution": (
            "The security team enabled the elevated rate-limiting profile on the login "
            "endpoint and blocked the source IP ranges at the edge; login latency returned to "
            "normal within 10 minutes. A password reset was forced for the small number of "
            "accounts that showed suspicious activity."
        ),
    },
    "support ticketing system queue backlog during a product launch": {
        "impact": (
            "The open-ticket queue grew to more than three times its normal depth during the "
            "launch window, and average first-response time increased from under 10 minutes "
            "to over an hour."
        ),
        "resolution": (
            "The team activated the overflow staffing plan and posted a status update to "
            "reduce duplicate tickets about the same known issue; the queue returned to its "
            "normal depth within four hours."
        ),
    },
    "mobile app crash loop after a configuration push": {
        "impact": (
            "A subset of mobile app users on the affected app version experienced a crash "
            "loop on startup after receiving the configuration update, temporarily preventing "
            "them from accessing the app."
        ),
        "resolution": (
            "The team identified the malformed configuration flag and rolled it back through "
            "the remote configuration service, confirming the crash-free session rate "
            "recovered to baseline within 15 minutes. No app store release was required since "
            "the configuration was server-controlled."
        ),
    },
}

_RUNBOOKS = [
    ("payments", "Payment Gateway Failover Runbook"),
    ("payments", "Card Network Reconciliation Runbook"),
    ("core_banking", "Ledger Database Failover Runbook"),
    ("core_banking", "End-of-Day Batch Recovery Runbook"),
    ("security", "Credential-Stuffing Response Runbook"),
    ("security", "Certificate Rotation Runbook"),
    ("customer_support", "Support Queue Overload Runbook"),
    ("product", "Mobile Release Rollback Runbook"),
    ("human_resources", "Employee Offboarding Access Revocation Runbook"),
    ("core_banking", "Database Connection Pool Exhaustion Runbook"),
]

# Detection/Response Steps/Escalation content, keyed by title, specific to each runbook's own
# failure mode. Earlier versions of this generator used one identical template for every
# runbook regardless of title — verified live (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off
# 26) that this made runbook-specific questions nearly unanswerable: ten documents sharing
# byte-identical "Response Steps" text gives retrieval nothing to tell them apart by, since
# the only distinguishing content was ever the title. Each entry below is deliberately as
# concrete as the payment-incident root causes above — a specific dashboard, threshold, or
# script a real on-call engineer would actually see — for the same reason: generic text is
# generic to embed, no matter how it's chunked.
_RUNBOOK_DETAILS: dict[str, dict[str, str]] = {
    "Payment Gateway Failover Runbook": {
        "detection": (
            "The Payments Gateway Health dashboard alerts when the primary gateway's "
            "authorization success rate drops below 95% over a 5-minute window, or its p99 "
            "latency exceeds 3 seconds."
        ),
        "response_steps": (
            "1. Acknowledge the gateway-health alert and confirm the primary gateway's error "
            "rate on the dashboard.\n"
            "2. Trigger the failover script to route new authorization traffic to the "
            "secondary gateway region.\n"
            "3. Confirm the secondary gateway is accepting traffic and the authorization "
            "success rate recovers above 98%.\n"
            "4. Monitor for 15 minutes to confirm the primary gateway's outage is not "
            "intermittent before declaring resolution.\n"
            "5. Open a follow-up ticket to investigate the primary gateway's root cause and "
            "plan fail-back."
        ),
        "escalation": (
            "If the secondary gateway also shows a degraded authorization rate, escalate "
            "immediately to the platform engineering on-call rotation and the card-network "
            "vendor's emergency support line."
        ),
    },
    "Card Network Reconciliation Runbook": {
        "detection": (
            "The nightly reconciliation job flags a mismatch when the settled transaction "
            "count or total value differs from the internal ledger by more than the "
            "configured tolerance."
        ),
        "response_steps": (
            "1. Pull the card network's settlement file and the internal ledger export for "
            "the affected settlement date.\n"
            "2. Run the reconciliation diff tool to isolate the specific transactions causing "
            "the mismatch.\n"
            "3. Classify each discrepancy as a timing difference, a declined-but-recorded "
            "transaction, or a genuine data error.\n"
            "4. Correct genuine data errors in the ledger and document timing differences "
            "expected to resolve on the next settlement cycle.\n"
            "5. Confirm the corrected totals reconcile before closing the ticket."
        ),
        "escalation": (
            "If the discrepancy exceeds the regulatory reporting threshold or cannot be "
            "explained within one business day, escalate to the payments finance team and "
            "start the incident disclosure process."
        ),
    },
    "Ledger Database Failover Runbook": {
        "detection": (
            "An alert fires when the ledger primary fails its health check for two "
            "consecutive intervals, or replication lag on the standby exceeds the configured "
            "threshold."
        ),
        "response_steps": (
            "1. Confirm the primary ledger database is genuinely unreachable, not a transient "
            "network blip, by checking connectivity from two independent hosts.\n"
            "2. Promote the most caught-up standby replica to primary using the documented "
            "promotion script.\n"
            "3. Repoint the ledger service's connection string to the newly promoted primary "
            "and restart affected pods.\n"
            "4. Verify write traffic resumes and the ledger's transaction-per-second metric "
            "returns to baseline.\n"
            "5. Rebuild a new standby replica from the promoted primary to restore redundancy."
        ),
        "escalation": (
            "If promotion fails, or the standby's replication lag means data loss is "
            "possible, escalate immediately to the database engineering on-call rotation "
            "before proceeding further."
        ),
    },
    "End-of-Day Batch Recovery Runbook": {
        "detection": (
            "The batch orchestrator alerts when a stage has not reported progress within its "
            "expected runtime window, visible on the End-of-Day Batch dashboard's stage "
            "timeline."
        ),
        "response_steps": (
            "1. Identify which batch stage stalled from the orchestrator's stage timeline.\n"
            "2. Check that stage's logs for the specific record or partition that caused the "
            "failure.\n"
            "3. Retry the stalled stage in isolation once the underlying data or resource "
            "issue is fixed.\n"
            "4. Confirm downstream stages resume automatically once the stalled stage "
            "completes.\n"
            "5. Verify the batch's completion report matches the expected record counts "
            "before close-of-business."
        ),
        "escalation": (
            "If the batch cannot complete before the regulatory cutoff time, escalate to the "
            "core banking engineering lead and notify operations that reporting will be "
            "delayed."
        ),
    },
    "Credential-Stuffing Response Runbook": {
        "detection": (
            "The fraud detection service alerts when failed-login attempts from a "
            "concentrated set of IP ranges exceed the baseline rate, visible on the Login "
            "Anomaly dashboard."
        ),
        "response_steps": (
            "1. Confirm the pattern is credential stuffing rather than a genuine traffic "
            "spike by checking the login endpoint's failure-to-success ratio and IP "
            "concentration.\n"
            "2. Enable the elevated rate-limiting and CAPTCHA challenge profile on the login "
            "endpoint.\n"
            "3. Block the confirmed malicious IP ranges at the edge/WAF layer.\n"
            "4. Force a password reset for any account with a successful login from a "
            "flagged IP range.\n"
            "5. Confirm the failed-login rate returns to baseline before standing down."
        ),
        "escalation": (
            "If any customer account is confirmed compromised, escalate to the fraud team and "
            "follow the incident disclosure policy for a potential customer data event."
        ),
    },
    "Certificate Rotation Runbook": {
        "detection": (
            "The certificate-expiry monitor alerts when a certificate has fewer than 14 days "
            "remaining before expiry, listed on the Certificate Inventory dashboard."
        ),
        "response_steps": (
            "1. Identify every service and integration that presents or validates the "
            "expiring certificate.\n"
            "2. Generate the replacement certificate through the internal certificate "
            "authority and validate its chain.\n"
            "3. Deploy the new certificate to a single instance first and confirm TLS "
            "handshakes succeed before a full rollout.\n"
            "4. Roll the new certificate out to the remaining instances and confirm the old "
            "certificate is no longer in use.\n"
            "5. Update the certificate inventory record with the new expiry date."
        ),
        "escalation": (
            "If a certificate has already expired and is causing active handshake failures, "
            "escalate immediately to the platform engineering on-call rotation rather than "
            "following the staged rollout."
        ),
    },
    "Support Queue Overload Runbook": {
        "detection": (
            "The support platform alerts when the open-ticket queue depth or average wait "
            "time exceeds the configured threshold, visible on the Support Queue dashboard."
        ),
        "response_steps": (
            "1. Confirm whether the backlog is driven by a genuine incident (a product outage "
            "generating duplicate tickets) or a staffing shortfall.\n"
            "2. If an incident is the cause, post a status-page update to reduce duplicate "
            "ticket volume.\n"
            "3. Activate the on-call overflow staffing plan to bring additional agents "
            "online.\n"
            "4. Triage the queue by priority so account-security and payment-related tickets "
            "are handled first.\n"
            "5. Confirm the queue depth returns below threshold before standing down the "
            "overflow plan."
        ),
        "escalation": (
            "If the backlog is caused by an ongoing product incident, escalate to that "
            "product line's incident commander rather than treating it as a support-capacity "
            "issue alone."
        ),
    },
    "Mobile Release Rollback Runbook": {
        "detection": (
            "The crash-reporting service alerts when the crash-free session rate for the "
            "newly released version drops below the release-health threshold."
        ),
        "response_steps": (
            "1. Confirm the elevated crash rate is specific to the new release version, not a "
            "device- or OS-level issue affecting all versions.\n"
            "2. Halt the phased rollout so no additional users receive the affected version.\n"
            "3. Trigger a server-side rollback to the previous stable version where the "
            "release supports it, or submit an expedited store rollback otherwise.\n"
            "4. Monitor the crash-free session rate to confirm it recovers to baseline for "
            "users on the prior version.\n"
            "5. File a release-blocking defect describing the crash signature for engineering "
            "to fix before the next release attempt."
        ),
        "escalation": (
            "If the crash affects a payment or authentication flow, escalate immediately to "
            "the payments or security on-call rotation in addition to the mobile platform "
            "team."
        ),
    },
    "Employee Offboarding Access Revocation Runbook": {
        "detection": (
            "The HR system generates an offboarding ticket automatically once a termination "
            "date is recorded, listed on the Offboarding Queue dashboard."
        ),
        "response_steps": (
            "1. Confirm the employee's last working day and required access-revocation time "
            "from the offboarding ticket.\n"
            "2. Disable the employee's single sign-on account at the scheduled revocation "
            "time.\n"
            "3. Revoke access to any system not covered by single sign-on, using the "
            "employee's system access list from the identity platform.\n"
            "4. Confirm the employee's physical badge access is deactivated with the "
            "facilities team.\n"
            "5. Mark the offboarding ticket complete once every access-list item is confirmed "
            "revoked."
        ),
        "escalation": (
            "If the offboarding is involuntary and flagged as high-risk, escalate to the "
            "security team to revoke access immediately rather than waiting for the scheduled "
            "time."
        ),
    },
    "Database Connection Pool Exhaustion Runbook": {
        "detection": (
            "The connection-pool monitor alerts when active connections reach the pool's "
            "configured ceiling for more than one minute, visible on the affected service's "
            "dashboard."
        ),
        "response_steps": (
            "1. Identify the affected service and confirm whether the exhaustion is caused by "
            "a connection leak or a genuine traffic increase.\n"
            "2. If a leak, identify and restart the specific instance holding stale "
            "connections rather than the whole fleet.\n"
            "3. If traffic-driven, temporarily raise the pool ceiling within the database's "
            "documented connection limit.\n"
            "4. Confirm queued requests drain and the pool's active-connection count returns "
            "below the alert threshold.\n"
            "5. File a follow-up ticket to fix the leak or right-size the pool permanently "
            "once the immediate pressure is relieved."
        ),
        "escalation": (
            "If raising the pool ceiling risks exceeding the database's total connection "
            "limit shared across services, escalate to the database engineering on-call "
            "rotation before making the change."
        ),
    },
}

_ARCHITECTURE_DOCS = [
    ("payments", "Payments Processing Platform Architecture"),
    ("payments", "Payment Gateway Integration Architecture"),
    ("core_banking", "Core Ledger System Architecture"),
    ("core_banking", "Batch Settlement Pipeline Architecture"),
    ("security", "Identity and Access Management Architecture"),
    ("security", "Fraud Detection Service Architecture"),
    ("customer_support", "Customer Support Platform Architecture"),
    ("product", "Mobile Banking Application Architecture"),
    ("human_resources", "Employee Directory and HR Systems Architecture"),
]

# Components/Reliability Considerations content specific to each architecture doc above — see
# `_RUNBOOK_DETAILS`'s docstring for why identical boilerplate defeats retrieval. "Related
# Runbooks" is deliberately *not* listed here: it's generated from `_RUNBOOKS` itself
# (`_architecture_docs` below), so it always names the department's actual runbooks by title
# rather than drifting out of sync with a hand-maintained duplicate.
_ARCHITECTURE_DETAILS: dict[str, dict[str, str]] = {
    "Payments Processing Platform Architecture": {
        "components": (
            "The platform is composed of the payment orchestration service, which validates "
            "and routes transactions; the ledger posting service, which records the financial "
            "effect of each transaction; and an outbound gateway adapter layer that "
            "translates requests into the format required by each downstream payment gateway."
        ),
        "reliability": (
            "The orchestration service enforces per-gateway circuit breakers so a single "
            "gateway's degradation does not exhaust connection pools shared with healthy "
            "gateways, and every transaction is written with an idempotency key so a "
            "client-side retry after a timeout cannot post the same payment twice."
        ),
    },
    "Payment Gateway Integration Architecture": {
        "components": (
            "The integration layer consists of a gateway adapter for each supported card "
            "network, a shared retry-and-timeout wrapper, and a webhook receiver that "
            "processes asynchronous settlement notifications from each gateway."
        ),
        "reliability": (
            "Each gateway adapter maintains its own connection pool and circuit breaker so "
            "one gateway's outage cannot exhaust connections needed by another, and every "
            "outbound request carries a bounded client-side timeout independent of the "
            "gateway's own advertised SLA."
        ),
    },
    "Core Ledger System Architecture": {
        "components": (
            "The ledger is built around an append-only transaction log, a materialized "
            "balance view rebuilt from that log, and a primary-replica database cluster that "
            "serves reads from replicas and all writes from the primary."
        ),
        "reliability": (
            "Every write to the transaction log is synchronously replicated to at least one "
            "standby before being acknowledged, so a primary failure cannot lose an "
            "already-confirmed transaction, and balance reads can fall back to the "
            "append-only log if the materialized view is temporarily unavailable."
        ),
    },
    "Batch Settlement Pipeline Architecture": {
        "components": (
            "The pipeline consists of a file-ingestion stage that validates incoming "
            "settlement files, a transformation stage that maps them to the internal ledger "
            "format, and a posting stage that applies the resulting entries to the ledger in "
            "order."
        ),
        "reliability": (
            "Each stage checkpoints its progress so a failure partway through a run can "
            "resume from the last completed record rather than reprocessing the entire "
            "batch, and the posting stage is idempotent so a retried record cannot be applied "
            "twice."
        ),
    },
    "Identity and Access Management Architecture": {
        "components": (
            "The system is composed of a central identity provider handling authentication, "
            "a policy engine evaluating role-based access decisions, and an audit log "
            "capturing every access grant and revocation."
        ),
        "reliability": (
            "The identity provider runs across multiple availability zones so a single zone "
            "failure does not block authentication, and access decisions are cached briefly "
            "at the policy engine so a transient outage does not immediately lock out users "
            "who already held a valid session."
        ),
    },
    "Fraud Detection Service Architecture": {
        "components": (
            "The service is composed of a real-time scoring engine evaluating each "
            "transaction against a rules and machine-learning model ensemble, a feature store "
            "supplying recent account history, and a case-management interface for manual "
            "review of flagged transactions."
        ),
        "reliability": (
            "The scoring engine is configured to fail closed for high-value transactions but "
            "fail open with a conservative default score for lower-risk transactions, so a "
            "scoring-engine outage degrades fraud coverage rather than blocking all payments "
            "outright."
        ),
    },
    "Customer Support Platform Architecture": {
        "components": (
            "The platform consists of a ticket-intake service accepting requests from chat, "
            "email, and phone channels, a routing engine assigning tickets to the appropriate "
            "queue, and an agent workspace surfacing customer and account context alongside "
            "each ticket."
        ),
        "reliability": (
            "The routing engine falls back to a single general queue if a specialized "
            "queue's assignment rules cannot be evaluated, so a routing-configuration error "
            "delays specialization rather than losing or blocking incoming tickets."
        ),
    },
    "Mobile Banking Application Architecture": {
        "components": (
            "The application is composed of a client app for iOS and Android, a "
            "backend-for-frontend API layer aggregating calls to core banking services, and a "
            "remote configuration service used to control feature rollouts without requiring "
            "an app store release."
        ),
        "reliability": (
            "The backend-for-frontend layer caches recent responses so a brief upstream "
            "outage can still serve a stale-but-usable view of account data, and the remote "
            "configuration service supports an immediate rollback of any flag without needing "
            "a new app build."
        ),
    },
    "Employee Directory and HR Systems Architecture": {
        "components": (
            "The system is composed of the core employee directory service, an integration "
            "layer synchronizing changes to downstream identity and payroll systems, and a "
            "self-service portal employees use to view and update their own records."
        ),
        "reliability": (
            "Directory changes are propagated to downstream systems through an at-least-once "
            "event stream with idempotent consumers, so a redelivered event updates a "
            "downstream system correctly rather than duplicating the change."
        ),
    },
}

_PRODUCT_SPECS = [
    ("product", "Instant Payments Feature Specification"),
    ("product", "Mobile Check Deposit Feature Specification"),
    ("product", "Real-Time Fraud Alerts Feature Specification"),
    ("payments", "Payment Retry and Idempotency Specification"),
    ("customer_support", "In-App Support Chat Feature Specification"),
    ("core_banking", "Multi-Currency Ledger Support Specification"),
    ("product", "Biometric Login Feature Specification"),
    ("security", "Step-Up Authentication Feature Specification"),
    ("human_resources", "Employee Self-Service Portal Specification"),
]

# Requirements/Out of Scope content specific to each spec above — see `_RUNBOOK_DETAILS`'s
# docstring for why identical boilerplate defeats retrieval.
_PRODUCT_SPEC_DETAILS: dict[str, dict[str, str]] = {
    "Instant Payments Feature Specification": {
        "requirements": (
            "The feature must confirm a transfer to the recipient's account within 10 "
            "seconds end to end, must operate outside normal banking hours including "
            "weekends, and must fall back to a standard next-business-day transfer if the "
            "instant-payment rail is unavailable."
        ),
        "out_of_scope": (
            "This specification does not cover cross-border instant transfers, which depend "
            "on a separate rail with different settlement guarantees and will be scoped "
            "separately."
        ),
    },
    "Mobile Check Deposit Feature Specification": {
        "requirements": (
            "The feature must extract the check amount and account number from a photo with "
            "sufficient confidence before submission, must hold deposited funds according to "
            "the bank's standard funds-availability schedule, and must flag a photo that "
            "fails quality checks for the customer to retake rather than submitting it for "
            "processing."
        ),
        "out_of_scope": (
            "This specification does not cover deposit limits for business banking accounts, "
            "which follow a separate approval workflow."
        ),
    },
    "Real-Time Fraud Alerts Feature Specification": {
        "requirements": (
            "The feature must notify the customer within 30 seconds of a transaction the "
            "fraud engine scores above the alerting threshold, must let the customer confirm "
            "or dispute the transaction directly from the notification, and must not alert on "
            "a transaction the customer has pre-authorized as trusted."
        ),
        "out_of_scope": (
            "This specification does not cover the fraud-scoring model itself, which is owned "
            "by the Fraud Detection Service and evolves independently of this notification "
            "feature."
        ),
    },
    "Payment Retry and Idempotency Specification": {
        "requirements": (
            "Every client-initiated retry of a payment request must carry the same "
            "idempotency key as the original attempt, the system must return the original "
            "transaction's result rather than creating a duplicate when a retried key is "
            "recognized, and idempotency keys must be retained for at least 24 hours after "
            "the original request."
        ),
        "out_of_scope": (
            "This specification does not cover retries initiated by the card network itself "
            "after settlement, which are handled by the reconciliation process instead."
        ),
    },
    "In-App Support Chat Feature Specification": {
        "requirements": (
            "The feature must preserve chat history across app restarts within the same "
            "support session, must route the customer to a human agent if the automated "
            "assistant cannot resolve the request within a configured number of turns, and "
            "must let the customer attach a screenshot to a message."
        ),
        "out_of_scope": (
            "This specification does not cover phone or email support channels, which are "
            "handled by the existing ticketing system independently of in-app chat."
        ),
    },
    "Multi-Currency Ledger Support Specification": {
        "requirements": (
            "The ledger must record each transaction's original currency and the exchange "
            "rate used at the time of conversion, must support reporting account balances in "
            "a customer's preferred display currency without altering the underlying "
            "recorded currency, and must reject a transaction if no exchange rate is "
            "available for the required currency pair."
        ),
        "out_of_scope": (
            "This specification does not cover hedging or exposure management for currency "
            "risk, which is handled by treasury operations outside this system."
        ),
    },
    "Biometric Login Feature Specification": {
        "requirements": (
            "The feature must only enable biometric login after the customer has completed "
            "at least one successful password-based login on that device, must fall back to "
            "password entry if biometric verification fails a configured number of times, and "
            "must disable biometric login automatically if the device's biometric enrollment "
            "changes."
        ),
        "out_of_scope": (
            "This specification does not cover biometric verification for high-value "
            "transaction approval, which is covered separately by the Step-Up Authentication "
            "specification."
        ),
    },
    "Step-Up Authentication Feature Specification": {
        "requirements": (
            "The feature must require a second authentication factor for any transaction "
            "above the configured value threshold, must accept either a one-time code or "
            "biometric confirmation as the second factor, and must expire an unused step-up "
            "challenge after a short, configurable timeout."
        ),
        "out_of_scope": (
            "This specification does not cover step-up requirements for internal employee "
            "tools, which follow the separate internal access policy."
        ),
    },
    "Employee Self-Service Portal Specification": {
        "requirements": (
            "The portal must let an employee update their own contact information and "
            "banking details for payroll without HR intervention, must require "
            "re-authentication before a sensitive field like banking details can be changed, "
            "and must notify HR of any change to a field subject to compliance review."
        ),
        "out_of_scope": (
            "This specification does not cover manager-initiated changes such as compensation "
            "adjustments, which remain in the existing HR system's approval workflow."
        ),
    },
}

_POLICIES = [
    ("security", "Access Control Policy", AccessLevel.CONFIDENTIAL),
    ("security", "Password and Multi-Factor Authentication Policy", AccessLevel.INTERNAL),
    ("human_resources", "Data Retention and Deletion Policy", AccessLevel.INTERNAL),
    ("human_resources", "Employee Code of Conduct Policy", AccessLevel.PUBLIC),
    ("security", "Third-Party Vendor Risk Management Policy", AccessLevel.CONFIDENTIAL),
    ("payments", "Incident Disclosure and Regulatory Reporting Policy", AccessLevel.CONFIDENTIAL),
    ("customer_support", "Customer Complaint Handling Policy", AccessLevel.PUBLIC),
    ("core_banking", "Change Management and Release Approval Policy", AccessLevel.INTERNAL),
    ("human_resources", "Remote Work and Device Security Policy", AccessLevel.INTERNAL),
]

# Policy Statement/Enforcement content specific to each policy above — see
# `_RUNBOOK_DETAILS`'s docstring for why identical boilerplate defeats retrieval.
_POLICY_DETAILS: dict[str, dict[str, str]] = {
    "Access Control Policy": {
        "statement": (
            "Access to any system or data classified above the public level must be granted "
            "according to the principle of least privilege, tied to a specific business "
            "justification, and reviewed at least quarterly by the resource owner. Access "
            "must be revoked immediately upon role change or termination rather than at the "
            "next scheduled review."
        ),
        "enforcement": (
            "An unreviewed or unjustified access grant found during an audit is revoked "
            "immediately, and repeated or willful violations are escalated to the security "
            "team and may result in disciplinary action up to termination."
        ),
    },
    "Password and Multi-Factor Authentication Policy": {
        "statement": (
            "Every employee account must be protected by multi-factor authentication, "
            "passwords must meet the bank's minimum complexity and rotation requirements, and "
            "a shared or generic account password must never be used for an individual "
            "employee's access."
        ),
        "enforcement": (
            "An account found without multi-factor authentication enabled is suspended until "
            "it is configured, and sharing credentials is treated as a security incident "
            "subject to the standard incident response process."
        ),
    },
    "Data Retention and Deletion Policy": {
        "statement": (
            "Employee personal data must be retained only for as long as required by legal "
            "or operational need, must be deleted or anonymized once that need ends, and any "
            "extended retention beyond the standard schedule requires documented approval "
            "from legal and HR leadership."
        ),
        "enforcement": (
            "Data found retained past its scheduled deletion date without approval is "
            "deleted immediately upon discovery, and the responsible system owner must "
            "document why the automated deletion did not run."
        ),
    },
    "Employee Code of Conduct Policy": {
        "statement": (
            "Employees are expected to act with honesty and professionalism, avoid conflicts "
            "of interest, and treat colleagues and customers with respect regardless of role "
            "or seniority."
        ),
        "enforcement": (
            "Reported violations are investigated by HR, and outcomes range from coaching "
            "for a minor first occurrence to termination for serious or repeated misconduct, "
            "consistent with the bank's standard disciplinary process."
        ),
    },
    "Third-Party Vendor Risk Management Policy": {
        "statement": (
            "Any third-party vendor with access to customer data or critical systems must "
            "complete a security assessment before onboarding, must be reassessed at least "
            "annually, and any finding rated high risk must be remediated or formally "
            "accepted by a designated risk owner before the engagement proceeds."
        ),
        "enforcement": (
            "A vendor with an overdue reassessment has its access suspended until the "
            "assessment is completed, and unremediated high-risk findings are escalated to "
            "the vendor risk committee."
        ),
    },
    "Incident Disclosure and Regulatory Reporting Policy": {
        "statement": (
            "Any incident meeting the regulatory definition of a reportable event must be "
            "disclosed to the relevant regulator within the mandated timeframe, and affected "
            "customers must be notified in line with applicable consumer protection "
            "requirements."
        ),
        "enforcement": (
            "A missed regulatory reporting deadline is escalated immediately to legal and "
            "compliance leadership, and the incident commander for the underlying event must "
            "document the cause of the delay."
        ),
    },
    "Customer Complaint Handling Policy": {
        "statement": (
            "Every customer complaint must be acknowledged within one business day, "
            "investigated by the appropriate team, and resolved or escalated within the "
            "timeframe committed to the customer."
        ),
        "enforcement": (
            "A complaint that breaches its resolution timeframe is automatically escalated "
            "to the customer support manager, and repeated breaches for the same root cause "
            "are raised to the relevant product or engineering team."
        ),
    },
    "Change Management and Release Approval Policy": {
        "statement": (
            "Any change to a production core banking system must be peer-reviewed, tested "
            "in a non-production environment, and approved by a designated release approver "
            "before deployment; emergency changes require retroactive approval within one "
            "business day."
        ),
        "enforcement": (
            "A change deployed without required approval is treated as a policy violation "
            "subject to review by engineering leadership, regardless of whether the change "
            "itself caused any incident."
        ),
    },
    "Remote Work and Device Security Policy": {
        "statement": (
            "Any device used to access bank systems remotely must have disk encryption and "
            "up-to-date endpoint security software enabled, and remote access to internal "
            "systems must go through the bank's managed VPN rather than a direct connection."
        ),
        "enforcement": (
            "A device found out of compliance has its access suspended until the required "
            "security controls are confirmed in place, and repeated non-compliance is "
            "escalated to the employee's manager and IT security."
        ),
    },
}

_MEETING_NOTES = [
    ("payments", "Payments Platform Incident Postmortem Review"),
    ("core_banking", "Core Banking Architecture Review Meeting Notes"),
    ("security", "Quarterly Security Posture Review Meeting Notes"),
    ("product", "Mobile Banking Sprint Retrospective Notes"),
    ("customer_support", "Customer Support Escalations Weekly Sync Notes"),
    ("human_resources", "HR Systems Vendor Evaluation Meeting Notes"),
    ("payments", "Payment Gateway Vendor Renewal Meeting Notes"),
    ("core_banking", "Ledger Migration Planning Meeting Notes"),
    ("product", "Fraud Alerts Feature Kickoff Meeting Notes"),
]

# Action Items content specific to each meeting above — see `_RUNBOOK_DETAILS`'s docstring for
# why identical boilerplate defeats retrieval.
_MEETING_NOTES_DETAILS: dict[str, str] = {
    "Payments Platform Incident Postmortem Review": (
        "The team agreed to add an explicit alert for the specific failure signal that "
        "delayed detection during the incident, to update the relevant runbook with the "
        "mitigation steps that proved effective, and to schedule a follow-up review in one "
        "month to confirm the alert catches the condition reliably."
    ),
    "Core Banking Architecture Review Meeting Notes": (
        "The team agreed to document the current replication topology for the ledger "
        "database, to evaluate whether the batch settlement pipeline needs an additional "
        "standby region, and to revisit the review findings at the next quarterly "
        "architecture sync."
    ),
    "Quarterly Security Posture Review Meeting Notes": (
        "The team agreed to close out the remaining findings from the last vendor risk "
        "assessment, to schedule the next round of access reviews for confidential systems, "
        "and to report progress on both items at the following quarter's review."
    ),
    "Mobile Banking Sprint Retrospective Notes": (
        "The team agreed to add automated crash-rate monitoring to the release checklist, to "
        "shorten the phased rollout window for low-risk changes, and to revisit whether the "
        "current rollback runbook still matches the team's actual release process."
    ),
    "Customer Support Escalations Weekly Sync Notes": (
        "The team agreed to review the tickets that breached their resolution timeframe this "
        "week, to identify whether any share a common root cause worth escalating to "
        "engineering, and to check back on open escalations at next week's sync."
    ),
    "HR Systems Vendor Evaluation Meeting Notes": (
        "The team agreed to request a follow-up security assessment from the shortlisted "
        "vendor, to confirm the vendor's data retention practices align with the bank's "
        "policy, and to bring a recommendation to the next meeting."
    ),
    "Payment Gateway Vendor Renewal Meeting Notes": (
        "The team agreed to request updated SLA terms from the gateway vendor ahead of "
        "renewal, to confirm the failover runbook still reflects the vendor's current "
        "failover process, and to finalize the renewal decision before the current "
        "contract's expiry."
    ),
    "Ledger Migration Planning Meeting Notes": (
        "The team agreed to draft a rollback plan for the ledger migration, to schedule a "
        "dry run in the staging environment before the production cutover, and to confirm "
        "the reconciliation process for validating the migrated data."
    ),
    "Fraud Alerts Feature Kickoff Meeting Notes": (
        "The team agreed on the initial alerting threshold to launch with, to confirm the "
        "notification feature's dependency on the fraud-scoring engine's output format, and "
        "to schedule a design review before implementation begins."
    ),
}


def _random_date(rng: random.Random, *, earliest_days_ago: int, latest_days_ago: int) -> date:
    days_ago = rng.randint(earliest_days_ago, latest_days_ago)
    return _TODAY - timedelta(days=days_ago)


def _non_payment_incidents(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, topic) in enumerate(_NON_PAYMENT_INCIDENT_TOPICS, start=1):
        created = _random_date(rng, earliest_days_ago=10, latest_days_ago=300)
        details = _NON_PAYMENT_INCIDENT_DETAILS[topic]
        docs.append(
            GeneratedDocument(
                document_id=f"incident-{department}-{index:03d}",
                title=f"Incident Report: {topic.capitalize()}",
                department=department,
                document_type=DocumentType.INCIDENT,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Summary",
                        f"On {created.isoformat()}, the {department.replace('_', ' ')} team "
                        f"responded to an incident involving {topic}. The issue was detected "
                        "by automated monitoring and triaged within the team's standard "
                        "response window.",
                    ),
                    ("Impact", details["impact"]),
                    ("Resolution", details["resolution"]),
                ],
            )
        )
    return docs


def _runbooks(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_RUNBOOKS, start=1):
        created = _random_date(rng, earliest_days_ago=60, latest_days_ago=500)
        details = _RUNBOOK_DETAILS[title]
        docs.append(
            GeneratedDocument(
                document_id=f"runbook-{department}-{index:03d}",
                title=title,
                department=department,
                document_type=DocumentType.RUNBOOK,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Purpose",
                        f"This runbook defines the standard operating procedure for the "
                        f"{department.replace('_', ' ')} on-call team to follow when "
                        f'responding to the failure mode covered by "{title}".',
                    ),
                    ("Detection", details["detection"]),
                    ("Response Steps", details["response_steps"]),
                    ("Escalation", details["escalation"]),
                ],
            )
        )
    return docs


def _related_runbooks_text(department: str) -> str:
    """Name this department's actual runbooks by title, derived from `_RUNBOOKS` itself rather
    than hand-authored per architecture doc — so it can never drift out of sync with the real
    runbook titles, and so it carries real, department-specific vocabulary instead of the
    generic "see the corresponding runbook documents" every architecture doc used to share."""
    titles = [
        runbook_title
        for runbook_department, runbook_title in _RUNBOOKS
        if runbook_department == department
    ]
    if not titles:
        return (
            "No dedicated operational runbook exists yet for this system; incidents are "
            "handled ad hoc by the owning team until one is written."
        )
    if len(titles) == 1:
        return f'Operational procedures for this system are covered by the "{titles[0]}".'
    joined = "; ".join(f'"{title}"' for title in titles)
    return (
        f"Operational procedures for this system are covered by the following runbooks: {joined}."
    )


def _architecture_docs(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_ARCHITECTURE_DOCS, start=1):
        created = _random_date(rng, earliest_days_ago=90, latest_days_ago=600)
        details = _ARCHITECTURE_DETAILS[title]
        docs.append(
            GeneratedDocument(
                document_id=f"architecture-{department}-{index:03d}",
                title=title,
                department=department,
                document_type=DocumentType.ARCHITECTURE,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Overview",
                        f"This document describes the architecture of the system covered by "
                        f'"{title}", owned by the {department.replace("_", " ")} team, '
                        "including its major components and how they interact.",
                    ),
                    ("Components", details["components"]),
                    ("Reliability Considerations", details["reliability"]),
                    ("Related Runbooks", _related_runbooks_text(department)),
                ],
            )
        )
    return docs


def _product_specs(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_PRODUCT_SPECS, start=1):
        created = _random_date(rng, earliest_days_ago=30, latest_days_ago=400)
        details = _PRODUCT_SPEC_DETAILS[title]
        docs.append(
            GeneratedDocument(
                document_id=f"product-spec-{department}-{index:03d}",
                title=title,
                department=department,
                document_type=DocumentType.PRODUCT_SPEC,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Problem Statement",
                        f"Customers and internal stakeholders have identified a need "
                        f'addressed by "{title}". This specification defines the scope of '
                        "the feature and the behavior it must implement.",
                    ),
                    ("Requirements", details["requirements"]),
                    ("Out of Scope", details["out_of_scope"]),
                ],
            )
        )
    return docs


def _policies(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title, access_level) in enumerate(_POLICIES, start=1):
        created = _random_date(rng, earliest_days_ago=120, latest_days_ago=700)
        details = _POLICY_DETAILS[title]
        docs.append(
            GeneratedDocument(
                document_id=f"policy-{department}-{index:03d}",
                title=title,
                department=department,
                document_type=DocumentType.POLICY,
                access_level=access_level,
                created_date=created,
                sections=[
                    (
                        "Purpose",
                        f'This policy, "{title}", establishes the bank\'s requirements in '
                        f"this area and applies to all employees and contractors of the "
                        f"{department.replace('_', ' ')} organization and any team handling "
                        "related data or systems.",
                    ),
                    ("Policy Statement", details["statement"]),
                    ("Enforcement", details["enforcement"]),
                ],
            )
        )
    return docs


def _meeting_notes(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_MEETING_NOTES, start=1):
        created = _random_date(rng, earliest_days_ago=5, latest_days_ago=250)
        docs.append(
            GeneratedDocument(
                document_id=f"meeting-{department}-{index:03d}",
                title=title,
                department=department,
                document_type=DocumentType.MEETING_NOTES,
                access_level=AccessLevel.INTERNAL,
                created_date=created,
                sections=[
                    (
                        "Attendees",
                        f"Representatives from the {department.replace('_', ' ')} team and "
                        "relevant stakeholders attended this meeting.",
                    ),
                    (
                        "Discussion",
                        f'The group reviewed the current status related to "{title}", '
                        "discussed open risks, and agreed on next steps.",
                    ),
                    ("Action Items", _MEETING_NOTES_DETAILS[title]),
                ],
            )
        )
    return docs


def generate_all(rng: random.Random) -> list[GeneratedDocument]:
    docs: list[GeneratedDocument] = []
    docs.extend(_payment_incidents(rng))
    docs.extend(_non_payment_incidents(rng))
    docs.extend(_runbooks(rng))
    docs.extend(_architecture_docs(rng))
    docs.extend(_product_specs(rng))
    docs.extend(_policies(rng))
    docs.extend(_meeting_notes(rng))
    assert {d.department for d in docs} <= set(DEPARTMENTS), "generated an unknown department"
    return docs


def write_corpus(output_root: Path = _OUTPUT_ROOT) -> list[Path]:
    rng = random.Random(_SEED)
    documents = generate_all(rng)
    written: list[Path] = []
    for doc in documents:
        doc_dir = output_root / doc.document_type.value
        doc_dir.mkdir(parents=True, exist_ok=True)
        path = doc_dir / f"{doc.document_id}.md"
        path.write_text(doc.render(), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    written = write_corpus()
    by_type: dict[str, int] = {}
    for path in written:
        by_type[path.parent.name] = by_type.get(path.parent.name, 0) + 1
    print(f"Wrote {len(written)} documents to {_OUTPUT_ROOT}:")
    for doc_type, count in sorted(by_type.items()):
        print(f"  {doc_type}: {count}")


if __name__ == "__main__":
    main()
