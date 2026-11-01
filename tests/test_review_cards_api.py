"""Case cards use retained evidence, exact money and bounded read-only SQL."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from invoice_optimizer import ml
from invoice_optimizer.api import create_app
from invoice_optimizer.models import (Assessment, Audit, Command, Job, Movement,
    MovementVersion, Outbox, ReviewCase)
from invoice_optimizer.review_history import ReviewEvent
from invoice_optimizer.review_cards import case_card
from test_bulk_review import case_factory


@pytest.fixture
def client(db):
    with TestClient(create_app(db.settings, db)) as value:
        value.headers['Authorization'] = 'Bearer ' + db.settings.api_token
        yield value


def receipt(*, score=.92, threshold=.5):
    return {'status': 'AVAILABLE', 'schema_version': 'exact-linear-logodds-v1',
            'model_version': 'fixture-model', 'policy_version': 'fixture-policy',
            'artifact_sha256': 'a' * 64, 'score': score, 'threshold': threshold}


def set_receipt(db, name, value, **changes):
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).where(
            Assessment.version_id == 'version-' + name + '-1', Assessment.kind == 'MODEL'))
        assessment.explanation = value
        for key, value in changes.items():
            setattr(assessment, key, value)


def get_card(client, identifier):
    response = client.get('/api/cases?limit=100')
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item['id'] == identifier)


def snapshot(db):
    with db.transaction() as session:
        return {model.__tablename__: [dict(row) for row in session.execute(
            select(model.__table__).order_by(model.__table__.c.id)).mappings()]
            for model in (Movement, MovementVersion, Assessment, ReviewCase, Audit,
                          ReviewEvent, Job, Outbox)} | {
            'command_count': session.scalar(select(func.count()).select_from(Command))}


def test_card_is_bound_to_cases_version_preserves_large_decimal_and_both_source_currencies(db, case_factory, client):
    case = case_factory('historical', current=2, case_version=1, status='SUPERSEDED')
    amount = '9007199254740993.01'  # Above the exact integer range of a JavaScript Number.
    with db.transaction() as session:
        old = session.get(MovementVersion, 'version-historical-1')
        payload = deepcopy(old.payload)
        payload.update(amount=amount, description='Original retained description', supplier_id='OLD-SUPPLIER')
        payload['source_values']['A'] = {**payload['source_values']['A'], 'amount': amount, 'currency': 'GBP'}
        payload['source_values']['B'] = {**payload['source_values']['B'], 'amount': '9007199254740993.02', 'currency': 'USD'}
        old.amount = Decimal(amount)
        old.payload = payload
        newer = session.get(MovementVersion, 'version-historical-2')
        newer.amount = Decimal('999.99')
        newer.payload = {**deepcopy(payload), 'amount': '999.99', 'description': 'Newer version must not leak into this card'}
        session.add(Assessment(version_id=newer.id, kind='MODEL', status='SCORED', score=.01,
            flagged=False, reasons=['NEW_VERSION_ONLY'], model_version='new-model', policy_version='new-policy'))
        session.add(Assessment(version_id=newer.id, kind='RULE', status='PASS', reasons=['NEW_RULE_ONLY']))
        rule = session.scalar(select(Assessment).where(Assessment.version_id == old.id, Assessment.kind == 'RULE'))
        rule.status = 'FAIL'
        rule.reasons = ['CURRENCY_MISMATCH', 'AMOUNT_MISMATCH']
    set_receipt(db, 'historical', receipt())
    card = get_card(client, case['case_id'])
    assert card['version_no'] == 1 and card['current_version'] == 2 and not card['release_eligible']
    assert card['transaction']['amount'] == card['transaction']['source_a_amount'] == amount
    assert type(card['transaction']['amount']) is str
    assert card['transaction']['source_b_amount'] == '9007199254740993.02'
    assert card['transaction']['source_a_currency'] == card['transaction']['currency'] == 'GBP'
    assert card['transaction']['source_b_currency'] == 'USD'
    assert card['transaction']['description'] == 'Original retained description'
    assert card['transaction']['supplier_id'] == 'OLD-SUPPLIER'
    assert card['transaction']['event_at'] == '2026-09-01T12:00:00+00:00'
    assert card['model']['recorded_score'] == .92 and card['model']['model_version'] == 'fixture-model'
    assert card['model']['threshold'] == .5 and card['model']['threshold_status'] == 'RECORDED_RECEIPT'
    assert card['model']['recorded_reasons'] == ['MODEL_RISK']
    assert card['rule'] == {'status': 'FAIL', 'recorded_reasons': ['CURRENCY_MISMATCH', 'AMOUNT_MISMATCH']}


def test_zero_is_available_and_unavailable_or_missing_results_are_not_zero(db, case_factory, client):
    zero = case_factory('zero', score=0., flagged=True)
    unavailable = case_factory('unavailable', model_status='UNAVAILABLE', score=0., case_reason='MODEL_UNAVAILABLE')
    missing = case_factory('missing', model_status=None, rule_status=None)
    quarantine = case_factory('quarantine', quarantine=True)
    set_receipt(db, 'zero', receipt(score=0., threshold=0.))
    set_receipt(db, 'unavailable', receipt(score=0., threshold=0.))
    zero_card = get_card(client, zero['case_id'])
    assert zero_card['model']['recorded_score'] == 0.
    assert zero_card['model']['threshold'] == 0.
    assert zero_card['model']['threshold_status'] == 'RECORDED_RECEIPT'
    unavailable_card = get_card(client, unavailable['case_id'])
    assert unavailable_card['model']['status'] == 'UNAVAILABLE'
    assert unavailable_card['model']['recorded_score'] is None
    assert unavailable_card['model']['threshold'] is None
    for item in (missing, quarantine):
        card = get_card(client, item['case_id'])
        assert card['model']['status'] == card['rule']['status'] == 'NOT_RECORDED'
        assert card['model']['recorded_score'] is None and card['model']['flagged'] is None
        assert card['model']['threshold'] is None
        assert card['model']['recorded_reasons'] == card['rule']['recorded_reasons'] == []
    q_card = get_card(client, quarantine['case_id'])
    assert q_card['version_no'] is None and q_card['current_version'] is None
    assert all(value is None for value in q_card['transaction'].values())


@pytest.mark.parametrize('key,value', [
    ('status', 'UNAVAILABLE'), ('model_version', 'another-model'), ('policy_version', 'another-policy'),
    ('score', .91), ('score', True), ('score', '0.92'), ('score', 10 ** 400),
    ('threshold', True), ('threshold', '0.5'), ('threshold', None),
    ('threshold', -.01), ('threshold', 1.01), ('threshold', 10 ** 400),
])
def test_incoherent_or_mistyped_persisted_receipt_cannot_supply_a_threshold(db, case_factory, client, key, value):
    case = case_factory('bad-receipt')
    evidence = receipt()
    evidence[key] = value
    set_receipt(db, 'bad-receipt', evidence)
    card = get_card(client, case['case_id'])
    assert card['model']['recorded_score'] == .92  # Stored assessment remains independently visible.
    assert card['model']['threshold'] is None
    assert card['model']['threshold_status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('evidence,changes', [
    (None, {}), ({}, {}), (receipt(), {'flagged': False}),
    (receipt(), {'model_version': None}), (receipt(), {'policy_version': None}),
])
def test_missing_receipt_or_conflicting_assessment_has_no_threshold(db, case_factory, client, evidence, changes):
    case = case_factory('missing-provenance')
    set_receipt(db, 'missing-provenance', evidence, **changes)
    card = get_card(client, case['case_id'])
    assert card['model']['threshold'] is None and card['model']['threshold_status'] == 'UNAVAILABLE'


def test_card_bounds_exposed_text_and_reasons_without_substituting_invalid_source_values(db, case_factory, client):
    case = case_factory('bounded')
    with db.transaction() as session:
        version = session.get(MovementVersion, 'version-bounded-1')
        version.payload = {'description': 'D' * 500, 'event_at': 'T' * 100, 'supplier_id': 'S' * 500,
            'source_values': {'A': {'amount': 1.25, 'currency': 'USD'}, 'B': {'amount': 'Infinity', 'currency': 'EUR'}}}
        model = session.scalar(select(Assessment).where(Assessment.version_id == version.id, Assessment.kind == 'MODEL'))
        model.reasons = ['R' * 800, None, 4] + [f'reason-{index}' for index in range(10)]
        rule = session.scalar(select(Assessment).where(Assessment.version_id == version.id, Assessment.kind == 'RULE'))
        rule.reasons = {'unexpected': 'not a reason list'}
    card = get_card(client, case['case_id'])
    transaction = card['transaction']
    assert len(transaction['description']) == 240 and len(transaction['event_at']) == 64
    assert len(transaction['supplier_id']) == 160
    assert transaction['amount'] == '10.00'
    assert transaction['source_a_amount'] is None and transaction['source_b_amount'] is None
    assert transaction['source_a_currency'] == 'USD' and transaction['source_b_currency'] == 'EUR'
    assert len(card['model']['recorded_reasons']) == 5 and len(card['model']['recorded_reasons'][0]) == 500
    assert card['rule']['recorded_reasons'] == []


def test_no_model_loading_active_threshold_fallback_or_mutation_and_query_count_is_constant(db, case_factory, client, monkeypatch):
    for index in range(50):
        case_factory(f'page-{index:02d}')
        if index % 2:
            set_receipt(db, f'page-{index:02d}', receipt())
    # A different active policy must neither fill missing receipts nor replace a retained threshold.
    (db.settings.model_dir / 'metadata.json').write_text(json.dumps({'model_version': 'other-active-model', 'threshold': .001}))
    def forbidden(*args, **kwargs):
        raise AssertionError('List cards must never load, score or explain a model')
    for name in ('_get_model', 'model_readiness', 'score_batch', 'explain_recorded'):
        monkeypatch.setattr(ml, name, forbidden)
    monkeypatch.setattr(ml.joblib, 'load', forbidden)
    original_open = Path.open
    def checked_open(path, *args, **kwargs):
        if path.is_relative_to(db.settings.model_dir):
            raise AssertionError('List cards must not read active or frozen model files')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', checked_open)
    before = snapshot(db)
    captured = []
    def record(connection, cursor, statement, parameters, context, executemany):
        captured.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        one = client.get('/api/cases?limit=1')
        first = list(captured)
        captured.clear()
        fifty = client.get('/api/cases?limit=50')
        second = list(captured)
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert one.status_code == fifty.status_code == 200
    assert len(one.json()) == 1 and len(fifty.json()) == 50
    first_selects = [sql for sql in first if sql.lstrip().upper().startswith('SELECT')]
    second_selects = [sql for sql in second if sql.lstrip().upper().startswith('SELECT')]
    assert len(first_selects) == len(second_selects) <= 12
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'CREATE')) for sql in first + second)
    cards = fifty.json()
    assert sum(item['model']['threshold'] == .5 for item in cards) == 25
    assert sum(item['model']['threshold'] is None for item in cards) == 25
    assert all(item['model']['model_version'] == 'fixture-model' for item in cards)
    assert snapshot(db) == before


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), True, '0.9', 10 ** 400])
def test_probability_projection_rejects_nonfinite_or_wrong_types_without_throwing(value):
    # PostgreSQL constrains persisted assessment scores. This pure boundary
    # additionally protects a future adapter from nonfinite or mistyped values.
    from types import SimpleNamespace
    model = SimpleNamespace(status='SCORED', score=value, flagged=True,
        explanation=receipt(), model_version='fixture-model', policy_version='fixture-policy', reasons=[])
    result = case_card(None, None, model)
    assert result['model']['recorded_score'] is None
    assert result['model']['threshold'] is None
