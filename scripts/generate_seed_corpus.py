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


def _random_date(rng: random.Random, *, earliest_days_ago: int, latest_days_ago: int) -> date:
    days_ago = rng.randint(earliest_days_ago, latest_days_ago)
    return _TODAY - timedelta(days=days_ago)


def _non_payment_incidents(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, topic) in enumerate(_NON_PAYMENT_INCIDENT_TOPICS, start=1):
        created = _random_date(rng, earliest_days_ago=10, latest_days_ago=300)
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
                    (
                        "Impact",
                        "Impact was contained to internal operations and a subset of "
                        "customer-facing functionality for the duration of the incident.",
                    ),
                    (
                        "Resolution",
                        "The team applied the relevant runbook, restored normal operation, "
                        "and scheduled a follow-up review to assess whether additional "
                        "safeguards were warranted.",
                    ),
                ],
            )
        )
    return docs


def _runbooks(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_RUNBOOKS, start=1):
        created = _random_date(rng, earliest_days_ago=60, latest_days_ago=500)
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
                    (
                        "Detection",
                        "This condition is typically surfaced by an automated alert tied to "
                        "an elevated error rate, latency, or queue-depth threshold specific to "
                        "the affected system.",
                    ),
                    (
                        "Response Steps",
                        "1. Acknowledge the alert and confirm the affected system.\n"
                        "2. Check the system's current health dashboard for corroborating "
                        "signals before taking action.\n"
                        "3. Apply the documented mitigation for this failure mode.\n"
                        "4. Confirm recovery against the same signal that triggered the alert.\n"
                        "5. Open a follow-up ticket for any remediation that could not be "
                        "completed during the incident.",
                    ),
                    (
                        "Escalation",
                        "If the mitigation does not restore normal operation within the "
                        "runbook's expected recovery window, escalate to the platform "
                        "engineering on-call rotation.",
                    ),
                ],
            )
        )
    return docs


def _architecture_docs(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_ARCHITECTURE_DOCS, start=1):
        created = _random_date(rng, earliest_days_ago=90, latest_days_ago=600)
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
                    (
                        "Components",
                        "The system is composed of a request-handling service layer, a "
                        "persistence layer backed by a managed relational database, and an "
                        "asynchronous event stream used to propagate state changes to "
                        "downstream consumers.",
                    ),
                    (
                        "Reliability Considerations",
                        "The system is designed to degrade gracefully under partial failure: "
                        "downstream dependencies are called with bounded timeouts and circuit "
                        "breakers, and critical paths have a documented fallback behavior "
                        "rather than failing the entire request.",
                    ),
                    (
                        "Related Runbooks",
                        "Operational procedures for responding to failures in this system are "
                        "maintained separately in the corresponding runbook documents.",
                    ),
                ],
            )
        )
    return docs


def _product_specs(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title) in enumerate(_PRODUCT_SPECS, start=1):
        created = _random_date(rng, earliest_days_ago=30, latest_days_ago=400)
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
                    (
                        "Requirements",
                        "The feature must be available across supported platforms, must "
                        "degrade gracefully when a dependent service is unavailable, and must "
                        "be observable enough to diagnose issues in production without "
                        "requiring a code change.",
                    ),
                    (
                        "Out of Scope",
                        "This specification does not cover changes to unrelated systems; "
                        "any dependency identified during implementation should be raised as "
                        "a separate specification.",
                    ),
                ],
            )
        )
    return docs


def _policies(rng: random.Random) -> list[GeneratedDocument]:
    docs = []
    for index, (department, title, access_level) in enumerate(_POLICIES, start=1):
        created = _random_date(rng, earliest_days_ago=120, latest_days_ago=700)
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
                    (
                        "Policy Statement",
                        "All personnel must comply with the requirements set out in this "
                        "document. Exceptions require documented approval from the relevant "
                        "department lead and must be time-bound and reviewed periodically.",
                    ),
                    (
                        "Enforcement",
                        "Violations of this policy are handled according to the bank's "
                        "standard disciplinary process and may be escalated to compliance or "
                        "legal depending on severity.",
                    ),
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
                    (
                        "Action Items",
                        "Action items were assigned with owners and target dates, to be "
                        "followed up on in the next recurring sync.",
                    ),
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
