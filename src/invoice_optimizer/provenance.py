"""Bounded file references and a once-per-snapshot SQL provenance projection."""
from sqlalchemy import select, text, func
from .db import lock, Conflict
from .models import (Batch, SourceRecord, MovementVersion, Quarantine,
                     InvoiceSourceFile, InvoiceSourceManifest)

PREVIEW_LIMIT = 6


def file_reference(batch):
    return {'id': batch.id, 'source': batch.source, 'period': batch.period,
            'original_name': batch.original_name, 'sha256': batch.sha256,
            'status': batch.status, 'superseded_by': batch.superseded_by,
            'detail_url': f'/api/batches/{batch.id}',
            'download_url': f'/api/batches/{batch.id}/file'}


def case_sources(session, cases):
    """Page-sized lookups only. Origins refer to first accepted source records."""
    versions = dict(session.execute(select(MovementVersion.id, MovementVersion.source_refs).where(
        MovementVersion.id.in_([c.version_id for c in cases if c.version_id]))).all())
    refs = {r for values in versions.values() for r in values}
    origins = dict(session.execute(select(SourceRecord.id, SourceRecord.batch_id).where(SourceRecord.id.in_(refs))).all())
    qids = [c.dedup_key.removeprefix('quarantine:') for c in cases if not c.movement_id]
    quarantines = dict(session.execute(select(Quarantine.id, Quarantine.batch_id).where(Quarantine.id.in_(qids))).all())
    ids = set(origins.values()) | set(quarantines.values())
    files = {b.id: file_reference(b) for b in session.scalars(select(Batch).where(Batch.id.in_(ids)))}
    result = {}
    for case in cases:
        missing = 0
        if case.version_id:
            source_refs = versions.get(case.version_id, [])
            batch_ids = {origins[r] for r in source_refs if r in origins}
            missing = sum(r not in origins for r in source_refs) or (0 if source_refs else 1)
        else:
            batch_id = quarantines.get(case.dedup_key.removeprefix('quarantine:'))
            batch_ids = {batch_id} if batch_id else set()
            missing = 0 if batch_id else 1
        result[case.id] = {'source_files': [files[b] for b in sorted(batch_ids) if b in files],
                           'provenance_status': 'INCOMPLETE' if missing else 'COMPLETE',
                           'missing_reference_count': missing}
    return result


def ensure_invoice_sources(session, invoice):
    """Compute once in PostgreSQL, never materialize movement rows in Python."""
    manifest = session.get(InvoiceSourceManifest, invoice.id)
    if manifest:
        if manifest.snapshot_hash != invoice.snapshot_hash:
            raise Conflict('Source manifest does not match the frozen snapshot')
        return manifest
    lock(session, 'invoice-sources:' + invoice.id)
    manifest = session.get(InvoiceSourceManifest, invoice.id)
    if manifest:
        return manifest
    session.execute(text('''
        WITH origins AS MATERIALIZED (
          SELECT sr.batch_id, count(*) AS reference_count
          FROM invoice_members im
          JOIN movement_versions mv ON mv.id = im.version_id
          LEFT JOIN LATERAL jsonb_array_elements_text(mv.source_refs) AS ref(id) ON true
          LEFT JOIN source_records sr ON sr.id = ref.id
          WHERE im.invoice_id = :invoice_id
          GROUP BY sr.batch_id
        ), saved AS (
          INSERT INTO invoice_source_files(invoice_id, batch_id)
          SELECT :invoice_id, batch_id FROM origins WHERE batch_id IS NOT NULL
          RETURNING batch_id
        )
        INSERT INTO invoice_source_manifests
          (invoice_id, snapshot_hash, file_count, missing_reference_count, created_at)
        SELECT :invoice_id, :snapshot_hash, (SELECT count(*) FROM saved),
          COALESCE((SELECT sum(reference_count) FROM origins WHERE batch_id IS NULL), 0), now()
    '''), {'invoice_id': invoice.id, 'snapshot_hash': invoice.snapshot_hash})
    return session.get(InvoiceSourceManifest, invoice.id)


def invoice_previews(session, invoices):
    """Polling reads only compact projections; old snapshots load on demand."""
    ids = [inv.id for inv in invoices]
    manifests = {m.invoice_id: m for m in session.scalars(select(InvoiceSourceManifest).where(InvoiceSourceManifest.invoice_id.in_(ids)))}
    ranked = select(InvoiceSourceFile.invoice_id, InvoiceSourceFile.batch_id,
        func.row_number().over(partition_by=InvoiceSourceFile.invoice_id,
                               order_by=InvoiceSourceFile.batch_id).label('position')).where(InvoiceSourceFile.invoice_id.in_(ids)).subquery()
    files = {}
    for invoice_id, batch in session.execute(select(ranked.c.invoice_id, Batch).join(Batch, Batch.id == ranked.c.batch_id).where(ranked.c.position <= PREVIEW_LIMIT)):
        files.setdefault(invoice_id, []).append(file_reference(batch))
    result = {}
    for inv in invoices:
        manifest = manifests.get(inv.id)
        valid = manifest and manifest.snapshot_hash == inv.snapshot_hash
        result[inv.id] = {'source_files': files.get(inv.id, []) if valid else [],
            'sources_url': f'/api/invoices/{inv.id}/sources',
            'provenance_status': ('INCOMPLETE' if manifest.missing_reference_count else 'COMPLETE') if valid else 'NOT_COMPUTED',
            'source_file_count': manifest.file_count if valid else None,
            'missing_reference_count': manifest.missing_reference_count if valid else None,
            'truncated': bool(valid and manifest.file_count > PREVIEW_LIMIT)}
    return result


def invoice_sources_page(session, invoice, limit=100, offset=0):
    manifest = ensure_invoice_sources(session, invoice)
    files = list(session.scalars(select(Batch).join(InvoiceSourceFile, InvoiceSourceFile.batch_id == Batch.id)
        .where(InvoiceSourceFile.invoice_id == invoice.id).order_by(Batch.id).limit(limit).offset(offset)))
    return {'source_files': [file_reference(b) for b in files], 'source_file_count': manifest.file_count,
            'provenance_status': 'INCOMPLETE' if manifest.missing_reference_count else 'COMPLETE',
            'missing_reference_count': manifest.missing_reference_count,
            'offset': offset, 'limit': limit, 'truncated': offset + len(files) < manifest.file_count,
            'sources_url': f'/api/invoices/{invoice.id}/sources'}
