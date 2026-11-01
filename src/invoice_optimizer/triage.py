"""Personal, version-bound review postponements; never financial decisions."""
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import defer
from sqlalchemy.exc import SQLAlchemyError

from .db import Blocked, Conflict, NotFound, audit, digest, lock, replay, record_command
from .models import Invoice, Movement, MovementVersion, ReviewCase, RuntimeOption, now
from .review_history import actor_identity


POSTPONE_HOURS = 24


def decision_reason(reason):
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
        raise ValueError('A decision reason of 5 to 2000 characters is required')
    return reason.strip()


def _key(actor, kind, identifier):
    return 'triage:' + digest([actor, kind, identifier])


def case_binding(case, version_no):
    return {'case_id': case.id, 'version_id': case.version_id, 'version_no': version_no}


def invoice_binding(invoice):
    return {'invoice_id': invoice.id, 'fingerprint': invoice.fingerprint}


def personal_postponements(session, actor, kind, items):
    """One lookup for a displayed page; another operator's choices never leak."""
    if not isinstance(actor, str) or not re.fullmatch(r'user:[a-f0-9]{32}', actor) or not items:
        return {}
    expected = {_key(actor, kind, identifier): (identifier, binding) for identifier, binding in items}
    values = session.scalars(select(RuntimeOption).where(RuntimeOption.key.in_(expected)))
    result = {}
    for option in values:
        value = option.value
        identifier, binding = expected[option.key]
        if not isinstance(value, dict) or value.get('actor') != actor or value.get('kind') != kind or value.get('id') != identifier or value.get('binding') != binding:
            continue
        try:
            until = datetime.fromisoformat(value['until'])
            if until.tzinfo is None or until <= now():
                continue
        except (KeyError, ValueError, TypeError):
            continue
        result[identifier] = {key: value.get(key) for key in ('until', 'reason', 'binding', 'actor', 'postponed_at')}
    return result


def _target(session, kind, identifier, expected_version, fingerprint):
    if kind == 'CASE':
        if fingerprint is not None:
            raise ValueError('Case postponement uses a movement version, not an invoice fingerprint')
        case = session.get(ReviewCase, identifier)
        if not case:
            raise NotFound('Case not found')
        if case.customer_id:
            lock(session, 'customer-period:' + case.customer_id + ':' + case.period)
        session.refresh(case, with_for_update=True)
        if case.status != 'OPEN':
            raise Conflict('Only an open case can be postponed or resumed')
        if not case.movement_id:
            if expected_version is not None or case.version_id is not None:
                raise Conflict('Quarantined evidence has no movement version')
            return case_binding(case, None)
        movement = session.get(Movement, case.movement_id, with_for_update=True)
        version = session.get(MovementVersion, case.version_id)
        if (not movement or not version or version.movement_id != movement.id
                or expected_version != movement.current_version or version.version_no != movement.current_version):
            raise Conflict('Case is for an old movement version; refresh before postponing')
        return case_binding(case, version.version_no)
    if expected_version is not None:
        raise ValueError('Invoice postponement uses its package fingerprint')
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise ValueError('An exact invoice package fingerprint is required')
    invoice = session.get(Invoice, identifier, options=[defer(Invoice.document)])
    if not invoice:
        raise NotFound('Invoice not found')
    lock(session, 'customer-period:' + invoice.customer_id + ':' + invoice.period)
    session.refresh(invoice, with_for_update=True)
    if invoice.issued_at is not None or invoice.status not in ('DRAFT', 'APPROVED'):
        raise Conflict('Only an unissued draft or approved package can be postponed or resumed')
    if invoice.fingerprint != fingerprint or digest(invoice.package) != fingerprint:
        raise Conflict('Invoice package fingerprint changed; refresh before postponing')
    return invoice_binding(invoice)


def _record_rejection(db, actor, action, identifier, payload, exc):
    try:
        with db.transaction() as session:
            reviewer = actor_identity(session, actor)
            if reviewer['identity_kind'] != 'REGISTERED_OPERATOR':
                return
            audit(session, actor, 'review_' + action + '_rejected', identifier,
                {**payload, 'reviewer': reviewer, 'error': str(exc)[:500],
                 'interpretation': 'Rejected personal workflow request; no financial decision was applied.'})
    except (ValueError, SQLAlchemyError):
        exc.add_note('The rejected personal workflow request could not be recorded.')


def _change(db, *, kind, id, expected_version=None, fingerprint=None, reason, request_id, actor, action):
    if kind not in ('CASE', 'INVOICE') or not isinstance(id, str) or not 1 <= len(id) <= 64:
        raise ValueError('Select a valid review case or invoice')
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise ValueError('Expected movement version must be a positive integer')
    reason = decision_reason(reason)
    payload = {'kind': kind, 'id': id, 'expected_version': expected_version, 'fingerprint': fingerprint,
               'reason': reason, 'actor': actor}
    try:
        with db.transaction() as session:
            reviewer = actor_identity(session, actor)
            if reviewer['identity_kind'] != 'REGISTERED_OPERATOR':
                raise Blocked('Personal review postponement requires an active operator account')
            cached = replay(session, request_id, 'review_' + action, payload)
            if cached:
                return cached
            binding = _target(session, kind, id, expected_version, fingerprint)
            key = _key(actor, kind, id)
            lock(session, key)
            option = session.get(RuntimeOption, key, with_for_update=True)
            stamp = now()
            if action == 'postpone':
                value = {'kind': kind, 'id': id, 'actor': actor, 'binding': binding, 'reason': reason,
                         'postponed_at': stamp.isoformat(), 'until': (stamp + timedelta(hours=POSTPONE_HOURS)).isoformat()}
                if option:
                    option.value = value
                else:
                    session.add(RuntimeOption(key=key, value=value))
                personal = {name: value[name] for name in ('until', 'reason', 'binding', 'actor', 'postponed_at')}
            else:
                if not option or not isinstance(option.value, dict) or option.value.get('binding') != binding or option.value.get('actor') != actor:
                    raise Conflict('No matching personal postponement exists for this version')
                session.delete(option)
                personal = None
            response = {'id': id, 'kind': kind, 'status': 'POSTPONED' if action == 'postpone' else 'RESUMED',
                'personal_postponement': personal, 'binding': binding, 'reviewer': reviewer,
                'decided_at': stamp.isoformat(), 'financial_status_changed': False}
            audit(session, actor, 'review_postponed' if action == 'postpone' else 'review_resumed', id,
                {**payload, 'binding': binding, 'request_id': request_id, 'reviewer': reviewer,
                 'until': personal['until'] if personal else None, 'financial_status_changed': False})
            return record_command(session, request_id, 'review_' + action, payload, response)
    except ValueError as exc:
        _record_rejection(db, actor, action, id, {**payload, 'request_id': request_id}, exc)
        raise


def postpone(db, **kwargs):
    return _change(db, action='postpone', **kwargs)


def resume(db, **kwargs):
    return _change(db, action='resume', **kwargs)
