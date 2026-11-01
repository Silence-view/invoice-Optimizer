"""Frozen monthly snapshots, approval and issuance before external transport."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_UP
from pathlib import Path
import tempfile, json, threading, weakref
from sqlalchemy import select, func, text, exists, insert, literal, or_
from .models import (Customer, Batch, Quarantine, Movement, MovementVersion, Assessment,
                     ReviewCase, Job, Invoice, InvoiceMember, Allocation, Outbox, BatchRecord, SourceRecord, Command, now, uid)
from .db import lock, audit, Blocked, Conflict, NotFound, replay, record_command, digest, canonical
from .storage import ObjectStore
from .movement_vat import MOVEMENT_VAT_POLICY, calculate_movement_vat
from .review_history import actor_identity
from .triage import decision_reason
from sqlalchemy.exc import SQLAlchemyError

MAX_ITEMIZED_MOVEMENTS = 10000
PACKAGE_PRESENTATION_KEYS = ('pack', 'renderer_version', 'pack_sha256', 'profile_id', 'profile_sha256', 'assets')


def _presentation(settings, store):
    """Resolve the configured format profile; its seller block and logo freeze with the draft."""
    from .formats import PackIntegrityError, ProfileError, build_presentation
    try:
        presentation, asset_paths = build_presentation(settings.profile_dir, settings.format_profile)
    except (ProfileError, PackIntegrityError) as exc:
        raise Blocked('Invoice format profile is not usable: ' + str(exc)) from None
    if not presentation['profile']['seller']['vat_number'].strip():
        raise Blocked('This billing adapter issues VAT invoices; the format profile seller needs a VAT number')
    for name, path in asset_paths.items():
        # Rendering reads only this content-addressed copy, never the profile file.
        if store.put_file(path) != presentation['assets'][name]:
            raise Blocked('Invoice format asset changed while freezing; retry')
    return presentation


def _line_format(value):
    if value not in ('SUMMARY', 'ITEMIZED'):
        raise ValueError('Invoice line format must be SUMMARY or ITEMIZED')
    return value


def _replay_compatible(session, request_id, action, payload, legacy_payloads=()):
    """Replay exact historical commands without silently applying a new policy."""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
        raise ValueError('A request ID of 1..120 characters is required')
    lock(session, 'command:' + request_id)
    old = session.get(Command, request_id)
    if old:
        allowed_hashes = {digest(payload), *(digest(item) for item in legacy_payloads)}
        if old.action != action or old.payload_hash not in allowed_hashes:
            raise Conflict('Request ID was already used for different content')
        return old.response
    return None


def _tax_point(period):
    try:
        year, month = map(int, period.split('-'))
        result = date(year, month, monthrange(year, month)[1])
        if result.strftime('%Y-%m') != period:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError):
        raise ValueError('Period must be YYYY-MM') from None


def _detail(row, period):
    for name in ('external_id', 'event_at', 'description', 'supplier_id'):
        value = getattr(row, name)
        if not isinstance(value, str) or not value.strip():
            raise Blocked('A frozen movement lacks itemized field: ' + name)
    try:
        event = datetime.fromisoformat(row.event_at.replace('Z', '+00:00'))
        if event.tzinfo is None or event.astimezone(timezone.utc).strftime('%Y-%m') != period:
            raise ValueError()
    except ValueError:
        raise Blocked('A frozen movement has an invalid itemized event timestamp') from None
    return {'movement_id': row.id, 'version_id': row.version_id, 'external_id': row.external_id,
            'event_at': row.event_at, 'description': row.description, 'supplier_id': row.supplier_id,
            'amount': str(row.amount), 'currency': row.currency}


# Positive coverage certificates per engine (one database). Key: the source,
# period, latest batch and every batch ID of that source-period. Source records
# of a source-period are written only by its own batches, superseded ones are
# terminal, and a non-terminal batch blocks above, so an unchanged key implies
# unchanged records and latest membership. Any new delivery changes the key.
_coverage = weakref.WeakKeyDictionary()
_coverage_guard = threading.Lock()


def _covered(s, source, period, latest, batch_ids):
    from sqlalchemy.orm import aliased
    engine = s.get_bind()
    key = (source, period, latest.id, batch_ids)
    with _coverage_guard:
        if key in _coverage.get(engine, ()):
            return True
    # A complete period snapshot must cover every previously seen source key,
    # including its latest revision. A partial delta is not a close certificate.
    # Members of a batch always share its source (record IDs digest the source).
    candidate=aliased(SourceRecord); membership=aliased(BatchRecord)
    covered=select(candidate.id).join(membership,membership.record_id==candidate.id).where(membership.batch_id==latest.id,candidate.source==source,candidate.record_id==SourceRecord.record_id,candidate.revision>=SourceRecord.revision).exists()
    if s.scalar(select(SourceRecord.id).where(SourceRecord.source==source,SourceRecord.period==period,~covered).limit(1)):
        return False
    with _coverage_guard:
        certificates = _coverage.setdefault(engine, set())
        if len(certificates) > 4096:
            certificates.clear()
        certificates.add(key)
    return True


def close_checks(s,customer_id,period):
    customer=s.get(Customer,customer_id)
    if not customer or not customer.active: raise Blocked('Customer is missing or inactive')
    every=list(s.scalars(select(Batch).where(Batch.period==period)))
    batches=[b for b in every if b.superseded_by is None]
    if any(b.status!='COMPLETE' for b in batches): raise Blocked('A source batch is incomplete or failed')
    for source in ('A','B'):
        candidates=[b for b in batches if b.source==source]
        latest=max(candidates,key=lambda b:b.created_at) if candidates else None
        if not latest or not latest.declared_complete or latest.expected_rows!=latest.row_count:
            raise Blocked('Latest source '+source+' delivery lacks a complete, count-verified period manifest')
        if not _covered(s,source,period,latest,tuple(sorted(b.id for b in every if b.source==source))):
            raise Blocked('Latest source '+source+' manifest does not cover all known period record versions')
    quarantined=s.scalar(select(func.count()).select_from(Quarantine).where(Quarantine.period==period,Quarantine.resolved_by_batch.is_(None),
        (Quarantine.validated_customer_id==customer_id) | (Quarantine.validated_customer_id.is_(None))))
    if quarantined: raise Blocked(f'{quarantined} quarantined rows affect this close')
    pending=s.scalar(select(func.count()).select_from(Job).where(Job.period==period,Job.kind.in_(['ASSESS','SCORE']),Job.status!='DONE'))
    if pending: raise Blocked(f'{pending} assessment jobs are unfinished in this period')
    counts=dict(s.execute(select(Movement.state,func.count()).where(Movement.customer_id==customer_id,Movement.period==period).group_by(Movement.state)).all())
    total=sum(counts.values())
    if not total: raise Blocked('No movements for this customer and period')
    if counts.get('ELIGIBLE',0)!=total: raise Blocked('Unresolved movement states: '+canonical(counts))
    open_cases=s.scalar(select(func.count()).select_from(ReviewCase).where(ReviewCase.period==period,ReviewCase.customer_id==customer_id,ReviewCase.status=='OPEN'))
    if open_cases: raise Blocked(f'{open_cases} review cases remain open')
    return customer,total


def _freeze_invoice_in_session(db, s, customer_id, period, actor, line_format, presentation=None):
    """One transaction freezes membership, accounting and optional presentation."""
    tax_point = _tax_point(period)
    store = ObjectStore(db.settings.data_dir / 'objects')
    if presentation is None:
        presentation = _presentation(db.settings, store)
    lock(s, 'period:' + period)
    lock(s, 'customer-period:' + customer_id + ':' + period)
    customer, count = close_checks(s, customer_id, period)
    # Fail before selecting JSON fields or constructing a list. SUMMARY keeps
    # the existing streaming path for much larger scopes; never silently omit
    # an ITEMIZED member to fit a PDF or a memory budget.
    if line_format == 'ITEMIZED' and count > MAX_ITEMIZED_MOVEMENTS:
        raise Blocked(f'ITEMIZED invoices support at most {MAX_ITEMIZED_MOVEMENTS:,} movements; '
                      f'this scope contains {count:,}. Choose SUMMARY explicitly or an agreed split workflow.')
    existing = s.scalar(select(Invoice).where(Invoice.customer_id == customer_id, Invoice.period == period,
        Invoice.status.notin_(['STALE', 'FAILED'])).order_by(Invoice.created_at.desc()).limit(1))
    if existing:
        raise Conflict('A current invoice already exists for this customer and period: ' + existing.id)
    if s.scalar(select(Allocation.movement_id).join(Movement, Movement.id == Allocation.movement_id).where(
            Movement.customer_id == customer_id, Movement.period == period).limit(1)):
        raise Blocked('This billing scope has already been issued. Use an authorized adjustment workflow.')
    columns = [Movement.id, MovementVersion.id.label('version_id'), MovementVersion.amount, MovementVersion.currency]
    if line_format == 'ITEMIZED':
        columns.extend([Movement.external_id, MovementVersion.payload['event_at'].astext.label('event_at'),
                        MovementVersion.payload['description'].astext.label('description'),
                        MovementVersion.payload['supplier_id'].astext.label('supplier_id')])
    query = select(*columns).join(MovementVersion, (MovementVersion.movement_id == Movement.id) &
        (MovementVersion.version_no == Movement.current_version)).where(
        Movement.customer_id == customer_id, Movement.period == period).order_by(Movement.id)
    money_context = Context(prec=64, rounding=ROUND_HALF_UP)
    total = Decimal('0')
    seen = 0
    detail = []
    snapshot_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=db.settings.data_dir, delete=False) as f:
            snapshot_path = Path(f.name)
            for row in s.execute(query.execution_options(yield_per=1000)):
                if row.currency != 'GBP':
                    raise Blocked('Mixed/unsupported currency')
                total = money_context.add(total, row.amount)
                seen += 1
                f.write(canonical({'movement_id': row.id, 'version_id': row.version_id,
                                   'amount': str(row.amount), 'currency': row.currency}) + '\n')
                if line_format == 'ITEMIZED':
                    if seen > MAX_ITEMIZED_MOVEMENTS:
                        raise Blocked('ITEMIZED movement limit exceeded; no partial invoice was created')
                    detail.append(_detail(row, period))
        if seen != count:
            raise Blocked('Snapshot coverage mismatch')
        snapshot_key = store.put_file(snapshot_path)
    finally:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
    if total >= Decimal('1000000000000000'):
        raise Blocked('Invoice total exceeds this renderer precision contract; split into an agreed billing structure')
    vat = money_context.quantize(money_context.multiply(total, Decimal('.20')), Decimal('.01'))
    gross = money_context.add(total, vat)
    if gross >= Decimal('1000000000000000'):
        raise Blocked('Gross invoice total exceeds this renderer precision contract')
    sequence = s.scalar(text("SELECT nextval('invoice_number_seq')"))
    number = f'DEMO-{tax_point.year}-{sequence:08d}'
    invoice_date = max(date.today(), tax_point)
    document = {'invoice_number': number, 'invoice_date': invoice_date.isoformat(), 'tax_point': tax_point.isoformat(),
        'due_date': (invoice_date + timedelta(days=30)).isoformat(), 'period': period, 'currency': 'GBP',
        'synthetic': True, 'vat_enabled': True, 'seller': presentation['profile']['seller'],
        'customer': {k: getattr(customer, k) for k in ('id', 'legal_name', 'address', 'email', 'vat_number')},
        'lines': [{'description': f'Monthly processing service package for {period}: {count:,} reconciled movements. Detailed membership retained in the snapshot.',
            'quantity': '1', 'unit_price': str(total), 'net_amount': str(total), 'vat_rate': '0.20',
            'vat_amount': str(vat), 'gross_amount': str(gross)}],
        'net_total': str(total), 'vat_total': str(vat), 'gross_total': str(gross),
        'payment_terms': '30 days from invoice date. Demonstration only - do not pay.', 'reference': number,
        'line_format': line_format, 'movement_count': count, 'presentation': presentation}
    if line_format == 'ITEMIZED':
        # The PDF is ordered by the source event and business ID, while the
        # content-addressed accounting snapshot keeps its established ID order.
        detail.sort(key=lambda item: (datetime.fromisoformat(item['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc),
                                      item['external_id'], item['movement_id']))
        row_vat, movement_vat = calculate_movement_vat([item['amount'] for item in detail],
            invoice_net_total=total, invoice_vat_total=vat, invoice_gross_total=gross, rate='0.20')
        document['movement_detail'] = [dict(row_no=index, **item, **vat_fields)
            for index, (item, vat_fields) in enumerate(zip(detail, row_vat, strict=True), 1)]
        document['movement_vat'] = movement_vat
    revision = (s.scalar(select(func.max(Invoice.revision)).where(
        Invoice.customer_id == customer_id, Invoice.period == period)) or 0) + 1
    inv = Invoice(id=uid(), number=number, customer_id=customer_id, period=period, revision=revision, status='BUILDING',
        currency='GBP', net_total=total, vat_total=vat, gross_total=gross,
        snapshot_hash=snapshot_key, snapshot_key=snapshot_key, document=document,
        line_format=line_format, movement_count=count,
        movement_vat_policy=document['movement_vat'].get('policy') if line_format == 'ITEMIZED' else None)
    s.add(inv)
    s.flush()
    member_query = select(literal(inv.id), Movement.id, MovementVersion.id, MovementVersion.amount).join(
        MovementVersion, (MovementVersion.movement_id == Movement.id) &
        (MovementVersion.version_no == Movement.current_version)).where(
        Movement.customer_id == customer_id, Movement.period == period)
    s.execute(insert(InvoiceMember).from_select(['invoice_id', 'movement_id', 'version_id', 'amount'], member_query))
    from .provenance import ensure_invoice_sources
    ensure_invoice_sources(s, inv)
    s.add(Job(dedup_key='render:' + inv.id, kind='RENDER', period=period, payload={'invoice_id': inv.id}))
    audit(s, actor, 'snapshot_frozen', inv.id, {'movement_count': count, 'net_total': str(total),
          'snapshot_sha256': snapshot_key, 'line_format': line_format,
          'movement_vat_policy': MOVEMENT_VAT_POLICY if line_format == 'ITEMIZED' else None,
          'presentation': {key: presentation[key] for key in PACKAGE_PRESENTATION_KEYS}})
    return inv


def create_invoice(db, customer_id, period, request_id, actor, *, line_format=None):
    """Prepare a draft through the normal flow; new omitted formats are ITEMIZED.

    A pre-format legacy close command may still replay when format is omitted.
    An explicit format is always bound in the command payload and cannot reuse a
    legacy command key to change an existing frozen package.
    """
    _tax_point(period)
    actual_format = _line_format('ITEMIZED' if line_format is None else line_format)
    previous_args = {'customer_id': customer_id, 'period': period, 'line_format': actual_format}
    policy = MOVEMENT_VAT_POLICY if actual_format == 'ITEMIZED' else None
    args = {**previous_args, 'movement_vat_policy': policy}
    with db.transaction() as s:
        legacy_payloads = [previous_args]
        if line_format is None:
            legacy_payloads.append({'customer_id': customer_id, 'period': period})
        cached = _replay_compatible(s, request_id, 'close', args, legacy_payloads)
        if cached:
            return cached
        inv = _freeze_invoice_in_session(db, s, customer_id, period, actor, actual_format)
        return record_command(s, request_id, 'close', args,
                              {'id': inv.id, 'number': inv.number, 'status': 'BUILDING', 'line_format': actual_format,
                               'movement_vat_policy': policy})


def regenerate_invoice_format(db, invoice_id, expected_fingerprint, line_format, request_id, actor):
    """Replace an unapproved draft atomically, retaining the complete old package.

    Only its presentation changes: the line format and/or the currently
    configured format profile. A failed validation/freezing transaction leaves
    the original DRAFT intact. Rendering remains a normal queued worker job, and
    the replacement requires its own later human approval.
    """
    actual_format = _line_format(line_format)
    previous_args = {'invoice_id': invoice_id, 'expected_fingerprint': expected_fingerprint,
                     'line_format': actual_format, 'actor': actor}
    policy = MOVEMENT_VAT_POLICY if actual_format == 'ITEMIZED' else None
    args = {**previous_args, 'movement_vat_policy': policy}
    from .review_history import actor_identity
    with db.transaction() as s:
        identity = actor_identity(s, actor)
        cached = _replay_compatible(s, request_id, 'regenerate_invoice_format', args, [previous_args])
        if cached:
            return cached
        presentation = _presentation(db.settings, ObjectStore(db.settings.data_dir / 'objects'))
        original = s.get(Invoice, invoice_id)
        if original is None:
            raise NotFound('Invoice not found')
        lock(s, 'period:' + original.period)
        lock(s, 'customer-period:' + original.customer_id + ':' + original.period)
        s.refresh(original, with_for_update=True)
        if (original.status != 'DRAFT' or original.approved_by is not None
                or original.approved_at is not None or original.issued_at is not None):
            raise Blocked('Only a current, unapproved DRAFT can change presentation')
        if (not expected_fingerprint or original.fingerprint != expected_fingerprint
                or not original.package or digest(original.package) != expected_fingerprint):
            raise Conflict('Package fingerprint changed; refresh the current draft')
        previous_format = original.document.get('line_format', 'SUMMARY')
        previous_vat = original.document.get('movement_vat')
        previous_policy = previous_vat.get('policy') if isinstance(previous_vat, dict) else None
        previous_details = original.document.get('movement_detail')
        legacy_itemized = (previous_format == 'ITEMIZED' and 'movement_vat' not in original.document
            and isinstance(previous_details, list) and bool(previous_details)
            and all(isinstance(row, dict) and not any(key in row for key in ('vat_rate', 'vat_amount', 'gross_amount'))
                    for row in previous_details))
        previous_presentation = original.document.get('presentation')
        if (previous_format == actual_format and previous_presentation == presentation
                and not (actual_format == 'ITEMIZED' and legacy_itemized)):
            raise Conflict('Invoice already uses the requested presentation or has unsupported VAT evidence')
        validate_snapshot(s, original)
        store = ObjectStore(db.settings.data_dir / 'objects')
        if (original.pdf_key != original.pdf_sha256 or original.snapshot_key != original.snapshot_hash
                or original.package.get('pdf_sha256') != original.pdf_sha256
                or original.package.get('snapshot_sha256') != original.snapshot_hash
                or original.package.get('invoice_id') != original.id):
            raise Conflict('Frozen invoice package references changed')
        store.path(original.pdf_key, verify=True)
        snapshot_path = store.path(original.snapshot_key, verify=True)
        query = select(InvoiceMember.movement_id, InvoiceMember.version_id, InvoiceMember.amount,
                       MovementVersion.currency).join(MovementVersion, MovementVersion.id == InvoiceMember.version_id).where(
                       InvoiceMember.invoice_id == original.id).order_by(InvoiceMember.movement_id)
        with snapshot_path.open(encoding='utf-8') as snapshot:
            for row in s.execute(query.execution_options(yield_per=1000)):
                expected = canonical({'movement_id': row.movement_id, 'version_id': row.version_id,
                                      'amount': str(row.amount), 'currency': row.currency}) + '\n'
                if snapshot.readline() != expected:
                    raise Conflict('Frozen snapshot bytes no longer match invoice membership')
            if snapshot.readline():
                raise Conflict('Frozen snapshot contains unexpected membership')
        original.status = 'STALE'
        s.flush()
        replacement = _freeze_invoice_in_session(db, s, original.customer_id, original.period, actor, actual_format,
                                                 presentation)
        if ((replacement.net_total, replacement.vat_total, replacement.gross_total, replacement.snapshot_hash) !=
                (original.net_total, original.vat_total, original.gross_total, original.snapshot_hash)):
            raise Conflict('Presentation regeneration must preserve every amount and frozen movement version')
        relation = {'old_invoice_id': original.id, 'new_invoice_id': replacement.id,
                    'old_invoice_number': original.number, 'new_invoice_number': replacement.number,
                    'old_fingerprint': original.fingerprint, 'old_pdf_sha256': original.pdf_sha256,
                    'snapshot_sha256': original.snapshot_hash, 'from_format': previous_format,
                    'to_format': actual_format, 'request_id': request_id, 'actor': identity,
                    'from_movement_vat_policy': previous_policy, 'to_movement_vat_policy': policy,
                    'net_total': str(original.net_total), 'vat_total': str(original.vat_total),
                    'gross_total': str(original.gross_total), 'human_approval_required': True,
                    'from_presentation': {key: previous_presentation.get(key) for key in ('pack', 'profile_id', 'profile_sha256')}
                                         if isinstance(previous_presentation, dict) else None,
                    'to_presentation': {key: presentation[key] for key in ('pack', 'profile_id', 'profile_sha256')}}
        # Store the link on both sides so either invoice's audit query can find it.
        audit(s, actor, 'invoice_format_superseded', original.id, relation)
        audit(s, actor, 'invoice_format_regenerated', replacement.id, relation)
        return record_command(s, request_id, 'regenerate_invoice_format', args,
            {'id': replacement.id, 'number': replacement.number, 'status': 'BUILDING',
             'line_format': actual_format, 'old_invoice_id': original.id, 'approval_required': True,
             'movement_vat_policy': policy})


def validate_snapshot(s, inv):
    close_checks(s,inv.customer_id,inv.period)
    mismatch=s.scalar(select(func.count()).select_from(InvoiceMember).join(Movement,Movement.id==InvoiceMember.movement_id).join(MovementVersion,MovementVersion.id==InvoiceMember.version_id).where(InvoiceMember.invoice_id==inv.id,or_(
        Movement.current_version!=MovementVersion.version_no, MovementVersion.movement_id!=Movement.id,
        Movement.customer_id!=inv.customer_id, Movement.period!=inv.period,
        InvoiceMember.amount!=MovementVersion.amount, MovementVersion.currency!=inv.currency)))
    members=s.scalar(select(func.count()).select_from(InvoiceMember).where(InvoiceMember.invoice_id==inv.id))
    current=s.scalar(select(func.count()).select_from(Movement).where(Movement.customer_id==inv.customer_id,Movement.period==inv.period))
    if mismatch or members!=current: raise Blocked('Snapshot no longer matches current eligible movements')


def render_job(db,job_id,claim_token=None):
    from .documents import render_invoice
    with db.transaction() as s:
        job=s.get(Job,job_id); inv=s.get(Invoice,job.payload['invoice_id'])
        if job.status=='DONE' or (claim_token is not None and job.claim_token!=claim_token): return
        if inv.status!='BUILDING': job.status='DONE'; return
        document=inv.document; invoice_id=inv.id
    store=ObjectStore(db.settings.data_dir/'objects')
    result=render_invoice(document,db.settings.data_dir/'documents'/invoice_id,db.settings.compiler,assets=store)
    key=store.put_file(result['pdf_path'])
    with db.transaction() as s:
        job=s.get(Job,job_id,with_for_update=True); inv=s.get(Invoice,invoice_id)
        if claim_token is not None and job.claim_token!=claim_token:return
        lock(s,'customer-period:'+inv.customer_id+':'+inv.period)
        if inv.status!='BUILDING': job.status='DONE'; return
        validate_snapshot(s,inv)
        message={'from':document['seller']['email'],'to':[document['customer']['email']],
            'subject':f"Synthetic invoice {inv.number} for {inv.period}",
            'body':f"Dear {document['customer']['legal_name']},\n\nPlease find attached invoice {inv.number} for {inv.period}. Total: GBP {inv.gross_total:.2f}.\n\nThis is a synthetic demonstration. No payment is due.\n\n{document['seller']['legal_name']}"}
        package={'invoice_id':inv.id,'invoice_number':inv.number,'pdf_sha256':key,'pdf_key':key,'message':message,'snapshot_sha256':inv.snapshot_hash}
        if 'presentation' in document:
            # Fingerprints then cover the reviewed pack, profile and asset hashes.
            package['presentation']={k:document['presentation'][k] for k in PACKAGE_PRESENTATION_KEYS}
        inv.pdf_key=key; inv.pdf_sha256=key; inv.package=package; inv.fingerprint=digest(package); inv.status='DRAFT'
        job.status='DONE'; job.lease_until=None
        audit(s,'worker','invoice_rendered',inv.id,{'pdf_sha256':key,'fingerprint':inv.fingerprint})


def _invoice_rejected_attempt(db, invoice_id, action, args, request_id, actor, error):
    try:
        with db.transaction() as session:
            reviewer=actor_identity(session,actor)
            audit(session,actor,'invoice_decision_rejected',invoice_id,
                {**args,'intended_action':action,'request_id':request_id,'reviewer':reviewer,
                 'error':str(error)[:500],'interpretation':'Rejected request; no invoice decision was applied.'})
    except (ValueError,SQLAlchemyError):
        error.add_note('The rejected invoice request could not be recorded.')


def approve_invoice(db,invoice_id,fingerprint,request_id,actor,reason=None):
    reason=decision_reason(reason if reason is not None else 'Legacy client approval: no additional review note was supplied.')
    # New receipts bind the initiator and note. A legacy receipt cannot prove
    # that identity and deliberately conflicts rather than being rewritten.
    args={'id':invoice_id,'fingerprint':fingerprint,'actor':actor,'reason':reason}
    try:
        with db.transaction() as s:
            reviewer=actor_identity(s,actor)
            cached=replay(s,request_id,'approve',args)
            if cached:return cached
            inv=s.get(Invoice,invoice_id)
            if not inv:raise NotFound('Invoice not found')
            lock(s,'period:'+inv.period);lock(s,'customer-period:'+inv.customer_id+':'+inv.period)
            s.refresh(inv,with_for_update=True)
            if inv.status!='DRAFT':raise Conflict('Only a current draft can be approved')
            if not fingerprint or fingerprint!=inv.fingerprint or digest(inv.package)!=fingerprint:raise Conflict('Package fingerprint changed')
            validate_snapshot(s,inv)
            ObjectStore(db.settings.data_dir/'objects').path(inv.pdf_key,verify=True)
            inv.status='APPROVED';inv.approved_by=actor;inv.approved_at=now()
            audit(s,actor,'invoice_approved',inv.id,{'fingerprint':fingerprint,'reason':reason,
                'reviewer':reviewer,'request_id':request_id,'approved_at':inv.approved_at.isoformat()})
            return record_command(s,request_id,'approve',args,{'id':inv.id,'status':inv.status,'fingerprint':fingerprint,
                'reviewer':reviewer,'reason':reason,'approved_at':inv.approved_at.isoformat()})
    except ValueError as exc:
        _invoice_rejected_attempt(db,invoice_id,'APPROVE',args,request_id,actor,exc)
        raise


def _package_decision(db,invoice_id,fingerprint,reason,request_id,actor,action):
    reason=decision_reason(reason)
    args={'id':invoice_id,'fingerprint':fingerprint,'reason':reason,'actor':actor}
    try:
        with db.transaction() as s:
            reviewer=actor_identity(s,actor)
            if reviewer['identity_kind']!='REGISTERED_OPERATOR':
                raise Blocked('A personal operator account is required for this invoice decision')
            cached=replay(s,request_id,'invoice_'+action,args)
            if cached:return cached
            inv=s.get(Invoice,invoice_id)
            if not inv:raise NotFound('Invoice not found')
            lock(s,'period:'+inv.period);lock(s,'customer-period:'+inv.customer_id+':'+inv.period)
            s.refresh(inv,with_for_update=True)
            if (inv.issued_at is not None or inv.status=='ISSUED'
                    or s.scalar(select(Allocation.movement_id).where(Allocation.invoice_id==inv.id).limit(1))
                    or s.scalar(select(Outbox.id).where(Outbox.invoice_id==inv.id,Outbox.kind=='INVOICE').limit(1))):
                raise Blocked('An issued invoice cannot be rejected or reopened; use an authorized adjustment workflow')
            allowed=('DRAFT','APPROVED') if action=='reject' else ('REJECTED',)
            if inv.status not in allowed:raise Conflict('Invoice status changed; refresh before this decision')
            if not fingerprint or fingerprint!=inv.fingerprint or digest(inv.package)!=fingerprint:
                raise Conflict('Package fingerprint changed')
            if action=='reopen':
                validate_snapshot(s,inv)
                ObjectStore(db.settings.data_dir/'objects').path(inv.pdf_key,verify=True)
            before={'status':inv.status,'approved_by':inv.approved_by,
                    'approved_at':inv.approved_at.isoformat() if inv.approved_at else None}
            inv.status='REJECTED' if action=='reject' else 'DRAFT'
            inv.approved_by=None;inv.approved_at=None;stamp=now().isoformat()
            audit(s,actor,'invoice_rejected' if action=='reject' else 'invoice_reopened',inv.id,
                {**args,'request_id':request_id,'reviewer':reviewer,'before':before,
                 'after':{'status':inv.status,'approved_by':None,'approved_at':None},
                 'snapshot_hash':inv.snapshot_hash,'pdf_sha256':inv.pdf_sha256,'decided_at':stamp})
            return record_command(s,request_id,'invoice_'+action,args,
                {'id':inv.id,'status':inv.status,'fingerprint':fingerprint,'reviewer':reviewer,
                 'reason':reason,'decided_at':stamp,'email_queued':False})
    except ValueError as exc:
        _invoice_rejected_attempt(db,invoice_id,action.upper(),args,request_id,actor,exc)
        raise


def reject_invoice(db,invoice_id,fingerprint,reason,request_id,actor):
    return _package_decision(db,invoice_id,fingerprint,reason,request_id,actor,'reject')


def reopen_invoice(db,invoice_id,fingerprint,reason,request_id,actor):
    return _package_decision(db,invoice_id,fingerprint,reason,request_id,actor,'reopen')


def revoke_invoice(db,invoice_id,reason,request_id,actor):
    if not reason.strip():raise ValueError('Reason required')
    args={'id':invoice_id,'reason':reason}
    with db.transaction() as s:
        cached=replay(s,request_id,'revoke',args)
        if cached:return cached
        inv=s.get(Invoice,invoice_id)
        if not inv:raise NotFound('Invoice not found')
        lock(s,'customer-period:'+inv.customer_id+':'+inv.period);s.refresh(inv)
        if inv.issued_at is not None:raise Blocked('Invoice already issued; approval cannot be retroactively revoked')
        if inv.status!='APPROVED':raise Conflict('Invoice is not approved')
        inv.status='DRAFT';inv.approved_by=None;inv.approved_at=None
        audit(s,actor,'approval_revoked',inv.id,{'reason':reason})
        return record_command(s,request_id,'revoke',args,{'id':inv.id,'status':inv.status})


def dispatch_invoice(db,invoice_id,request_id,actor):
    args={'id':invoice_id}
    with db.transaction() as s:
        cached=replay(s,request_id,'dispatch',args)
        if cached:return cached
        inv=s.get(Invoice,invoice_id)
        if not inv:raise NotFound('Invoice not found')
        lock(s,'period:'+inv.period);lock(s,'customer-period:'+inv.customer_id+':'+inv.period);s.refresh(inv)
        if inv.status!='APPROVED' or not inv.approved_at:raise Blocked('Exact current package must be approved before issuance')
        validate_snapshot(s,inv)
        if digest(inv.package)!=inv.fingerprint:raise Conflict('Approved package content changed')
        ObjectStore(db.settings.data_dir/'objects').path(inv.pdf_key,verify=True)
        # Primary key on stable movement identity prevents rebilling any version.
        s.execute(insert(Allocation).from_select(['movement_id','invoice_id','component'],select(InvoiceMember.movement_id,InvoiceMember.invoice_id,text("'demo-service-fee'")).where(InvoiceMember.invoice_id==inv.id)))
        inv.issued_at=now();inv.status='ISSUED'
        s.add(Outbox(dedup_key='invoice:'+inv.id,kind='INVOICE',invoice_id=inv.id,payload=inv.package))
        audit(s,actor,'invoice_issued',inv.id,{'fingerprint':inv.fingerprint})
        return record_command(s,request_id,'dispatch',args,{'id':inv.id,'status':'ISSUED','email_status':'QUEUED'})
