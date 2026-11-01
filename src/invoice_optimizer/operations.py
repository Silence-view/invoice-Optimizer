"""Durable, bounded automation controls and pre-ingestion queue admission."""
from copy import deepcopy

from sqlalchemy import select, func, cast, Numeric

from .db import Blocked, Conflict, lock, replay, record_command
from .models import Batch, Job, RuntimeOption, Audit, now
from .review_history import actor_identity

AUTOMATION_KEY = 'automation.settings.v1'
BOOL_FIELDS = ('paused', 'monthly_preparation_enabled', 'dispatch_after_approval_enabled', 'email_delivery_enabled')
LIMITS = {'jobs_per_tick': (1, 100), 'emails_per_tick': (0, 100),
          'monthly_customers_per_cycle': (1, 20), 'dispatch_per_cycle': (1, 100),
          'queue_high_watermark': (100, 100000)}


class Backpressure(Blocked):
    retry_after = 5


def _defaults(settings):
    return {'paused': False, 'monthly_preparation_enabled': bool(settings.auto_close),
            'dispatch_after_approval_enabled': bool(settings.auto_dispatch), 'email_delivery_enabled': True,
            'jobs_per_tick': 10, 'emails_per_tick': 20, 'monthly_customers_per_cycle': 20,
            'dispatch_per_cycle': 10, 'queue_high_watermark': 10000}


def _validate_patch(patch):
    if not isinstance(patch, dict) or not patch or set(patch) - set(BOOL_FIELDS) - set(LIMITS):
        raise ValueError('Provide only recognized automation settings')
    for key, value in patch.items():
        if key in BOOL_FIELDS and type(value) is not bool:
            raise ValueError(key + ' must be true or false')
        if key in LIMITS:
            low, high = LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} must be an integer from {low} to {high}')


def _read(session, settings):
    stored = session.get(RuntimeOption, AUTOMATION_KEY)
    value = deepcopy(stored.value) if stored else {}
    config = {**_defaults(settings), **value.get('config', {})}
    _validate_patch(config)
    revision = value.get('revision', 0)
    if type(revision) is not int or revision < 0:
        raise ValueError('Stored automation revision is invalid')
    effective = {**config, 'revision': revision,
                 'processing_enabled': not config['paused'], 'training_enabled': not config['paused']}
    for key in ('monthly_preparation_enabled', 'dispatch_after_approval_enabled', 'email_delivery_enabled'):
        effective[key] = not config['paused'] and config[key]
    return {'revision': revision, 'config': config, 'effective': effective,
            'updated_at': value.get('updated_at'), 'updated_by': value.get('updated_by'),
            'limits': {key: {'min': low, 'max': high} for key, (low, high) in LIMITS.items()},
            'pause_scope': 'New automation claims only; in-flight work may complete. Intake and reads remain available.',
            'approval_policy': 'Human approval remains required before invoice issuance.'}


def get_effective_automation(session, settings):
    return _read(session, settings)['effective']


def get_automation(db):
    with db.transaction() as session:
        return _read(session, db.settings)


def update_automation(db, patch, expected_revision, request_id, actor, reason):
    _validate_patch(patch)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('A nonnegative expected_revision is required')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError('An automation change reason of 1 to 1000 characters is required')
    payload = {'patch': patch, 'expected_revision': expected_revision, 'actor': actor, 'reason': reason}
    with db.transaction() as session:
        identity = actor_identity(session, actor)
        if actor.startswith('user:'):
            from .operators import Operator
            if session.get(Operator, actor[5:]).role != 'admin':
                raise Blocked('Only an administrator can change automation settings')
        cached = replay(session, request_id, 'automation_settings', payload)
        if cached:
            return cached
        lock(session, AUTOMATION_KEY)
        before = _read(session, db.settings)
        if before['revision'] != expected_revision:
            raise Conflict('Automation settings changed; refresh before saving')
        config = {**before['config'], **patch}
        if config == before['config']:
            raise Conflict('Automation settings are unchanged')
        value = {'revision': expected_revision + 1, 'config': config,
                 'updated_at': now().isoformat(), 'updated_by': identity}
        option = session.get(RuntimeOption, AUTOMATION_KEY)
        if option is None:
            session.add(RuntimeOption(key=AUTOMATION_KEY, value=value))
        else:
            option.value = value
        event = Audit(actor=actor, action='automation_settings_changed', entity_id=AUTOMATION_KEY,
                      details={'before': before['config'], 'after': config, 'previous_revision': expected_revision,
                               'revision': expected_revision + 1, 'actor': identity, 'reason': reason,
                               'request_id': request_id, 'human_approval_required': True})
        session.add(event)
        session.flush()
        result = {**_read(session, db.settings), 'audit_event_id': event.id}
        return record_command(session, request_id, 'automation_settings', payload, result)


def check_ingestion_admission(session, settings, expected_rows):
    """Reject before creating a batch; retain replay and partially imported files.

    Declared deliveries reserve an upper bound for their remaining input chunks.
    Unmanifested inputs have no knowable size, so their initial admission uses one
    chunk. This is an admission watermark, not a hard end-to-end memory quota.
    """
    lock(session, 'ingestion:queue-admission')
    control = get_effective_automation(session, settings)
    watermark = control['queue_high_watermark']
    bounded = select(Job.id).where(Job.kind.in_(('ASSESS', 'SCORE')),
        Job.status.in_(('READY', 'RUNNING'))).limit(watermark).subquery()
    pending = select(func.count()).select_from(bounded).scalar_subquery()
    jobs_per_chunk = (settings.batch_size + settings.worker_batch_size - 1) // settings.worker_batch_size
    remaining = func.greatest(Batch.expected_rows - Batch.row_count, 0)
    reservations = select(func.coalesce(func.sum(func.ceil(cast(remaining, Numeric) / settings.batch_size) * jobs_per_chunk), 0)).where(
        Batch.status == 'RUNNING', Batch.expected_rows.is_not(None)).scalar_subquery()
    # One SQL statement gives a coherent snapshot while chunks commit progress.
    outstanding = int(session.scalar(select(pending + reservations)))
    requested = jobs_per_chunk if expected_rows is None else ((expected_rows + settings.batch_size - 1) // settings.batch_size) * jobs_per_chunk
    if outstanding >= watermark or outstanding + requested > watermark:
        raise Backpressure(f'Processing queue admission limit reached ({outstanding} pending/reserved jobs, '
                           f'{requested} requested, limit {watermark}); retry after workers drain the queue')
    return {'pending_and_reserved_jobs': outstanding, 'requested_jobs_upper_bound': requested,
            'queue_high_watermark': watermark}
