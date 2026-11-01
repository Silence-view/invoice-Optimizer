from datetime import datetime, timezone
import os, time
from sqlalchemy import String, Integer, BigInteger, Boolean, DateTime, Numeric, Float, Text, ForeignKey, UniqueConstraint, Index, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    """32-hex UUIDv7: time-ordered for B-tree insert locality, still opaque."""
    rand=int.from_bytes(os.urandom(10),'big')
    value=(time.time_ns()//1_000_000&(1<<48)-1)<<80|0x7<<76|(rand>>62&0xfff)<<64|0b10<<62|rand&(1<<62)-1
    return f'{value:032x}'

def now(): return datetime.now(timezone.utc)

class Base(DeclarativeBase): pass

class Customer(Base):
    __tablename__='customers'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(240))
    address: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(320))
    alert_email: Mapped[str] = mapped_column(String(320))
    vat_number: Mapped[str] = mapped_column(String(50), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Batch(Base):
    __tablename__='ingestion_batches'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(1))
    period: Mapped[str] = mapped_column(String(7), index=True)
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    original_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='RUNNING', index=True)
    expected_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0)
    accepted_count: Mapped[int] = mapped_column(BigInteger, default=0)
    rejected_count: Mapped[int] = mapped_column(BigInteger, default=0)
    duplicate_count: Mapped[int] = mapped_column(BigInteger, default=0)
    declared_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(UniqueConstraint('source','period','sha256'),CheckConstraint("source IN ('A','B')"),)

class SourceRecord(Base):
    __tablename__='source_records'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey('ingestion_batches.id'))
    source: Mapped[str] = mapped_column(String(1))
    record_id: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer)
    movement_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(ForeignKey('customers.id'))
    period: Mapped[str] = mapped_column(String(7), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('source','record_id','revision'),)

class Quarantine(Base):
    __tablename__='quarantine'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey('ingestion_batches.id'))
    period: Mapped[str] = mapped_column(String(7), index=True)
    row_number: Mapped[int] = mapped_column(BigInteger)
    validated_customer_id: Mapped[str | None] = mapped_column(ForeignKey('customers.id'), nullable=True, index=True)
    resolved_by_batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Movement(Base):
    __tablename__='movements'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(160))
    customer_id: Mapped[str] = mapped_column(ForeignKey('customers.id'))
    period: Mapped[str] = mapped_column(String(7), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default='PENDING')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('customer_id','external_id'),Index('ix_movement_customer_period','customer_id','period'),)

class MovementVersion(Base):
    __tablename__='movement_versions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    movement_id: Mapped[str] = mapped_column(ForeignKey('movements.id'))
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    source_refs: Mapped[list] = mapped_column(JSONB)
    amount: Mapped[object] = mapped_column(Numeric(20,2))
    currency: Mapped[str] = mapped_column(String(3))
    origin: Mapped[str] = mapped_column(String(20), default='SOURCE')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('movement_id','version_no'),)

class Assessment(Base):
    __tablename__='assessments'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    version_id: Mapped[str] = mapped_column(ForeignKey('movement_versions.id'))
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(24))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    model_version: Mapped[str | None] = mapped_column(String(160), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(160), nullable=True)
    feature_schema_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(40), default='demo-contract-v1')
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    explanation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    feature_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('version_id','kind'),CheckConstraint('score IS NULL OR (score >= 0 AND score <= 1)'),)

class ReviewCase(Base):
    __tablename__='review_cases'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dedup_key: Mapped[str] = mapped_column(String(240), unique=True)
    movement_id: Mapped[str | None] = mapped_column(ForeignKey('movements.id'), nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(ForeignKey('movement_versions.id'), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey('customers.id'), nullable=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    reason: Mapped[str] = mapped_column(String(240))
    severity: Mapped[str] = mapped_column(String(20), default='HIGH')
    status: Mapped[str] = mapped_column(String(24), default='OPEN')
    owner: Mapped[str] = mapped_column(String(320))
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Job(Base):
    __tablename__='jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dedup_key: Mapped[str] = mapped_column(String(240), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default='READY')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Invoice(Base):
    __tablename__='invoices'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    number: Mapped[str] = mapped_column(String(80), unique=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey('customers.id'))
    period: Mapped[str] = mapped_column(String(7), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default='BUILDING', index=True)
    currency: Mapped[str] = mapped_column(String(3), default='GBP')
    net_total: Mapped[object] = mapped_column(Numeric(20,2))
    vat_total: Mapped[object] = mapped_column(Numeric(20,2))
    gross_total: Mapped[object] = mapped_column(Numeric(20,2))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    snapshot_key: Mapped[str] = mapped_column(Text)
    document: Mapped[dict] = mapped_column(JSONB)
    package: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pdf_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Derived from the frozen document at freeze time so lists never detoast it.
    # NULL line_format means a pre-migration row: readers fall back to document.
    line_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    movement_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    movement_vat_policy: Mapped[str | None] = mapped_column(String(160), nullable=True)
    __table_args__=(UniqueConstraint('customer_id','period','revision'),)

class InvoiceMember(Base):
    __tablename__='invoice_members'
    invoice_id: Mapped[str] = mapped_column(ForeignKey('invoices.id'), primary_key=True)
    movement_id: Mapped[str] = mapped_column(ForeignKey('movements.id'), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey('movement_versions.id'))
    amount: Mapped[object] = mapped_column(Numeric(20,2))

class InvoiceSourceManifest(Base):
    """Derived provenance; never part of or a mutation to an approved package."""
    __tablename__='invoice_source_manifests'
    invoice_id: Mapped[str] = mapped_column(ForeignKey('invoices.id'), primary_key=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    file_count: Mapped[int] = mapped_column(BigInteger)
    missing_reference_count: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class InvoiceSourceFile(Base):
    __tablename__='invoice_source_files'
    invoice_id: Mapped[str] = mapped_column(ForeignKey('invoices.id'), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey('ingestion_batches.id'), primary_key=True)

class Allocation(Base):
    __tablename__='issued_allocations'
    movement_id: Mapped[str] = mapped_column(ForeignKey('movements.id'), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey('invoices.id'), index=True)
    component: Mapped[str] = mapped_column(String(40), default='demo-service-fee')

class Outbox(Base):
    __tablename__='outbox'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dedup_key: Mapped[str] = mapped_column(String(240), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey('invoices.id'), nullable=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey('review_cases.id'), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default='READY', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class DeliveryAttempt(Base):
    __tablename__='delivery_attempts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    outbox_id: Mapped[str] = mapped_column(ForeignKey('outbox.id'), index=True)
    status: Mapped[str] = mapped_column(String(30), default='SUBMITTING')
    provider_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    eml_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class DeliveryEvent(Base):
    __tablename__='delivery_events'
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey('delivery_attempts.id'), index=True)
    event: Mapped[str] = mapped_column(String(30))
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Audit(Base):
    __tablename__='audit_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(160))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Command(Base):
    __tablename__='commands'
    request_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    action: Mapped[str] = mapped_column(String(80))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class RuntimeOption(Base):
    __tablename__='runtime_options'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)

class Counter(Base):
    __tablename__='operational_counters'
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)

class BatchRecord(Base):
    __tablename__='batch_records'
    batch_id: Mapped[str] = mapped_column(ForeignKey('ingestion_batches.id'), primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey('source_records.id'), primary_key=True)
