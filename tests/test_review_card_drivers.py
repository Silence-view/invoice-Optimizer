"""Frozen-model card explanations: exact math, honest provenance, bounded reads.

Integration fixtures train an actual logistic model and use disposable PostgreSQL
schemas. Constructed feature vectors below are explicitly mathematical fixtures;
they must never acquire invented raw-source values.
"""
from copy import deepcopy
from decimal import Decimal
import math
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from invoice_optimizer import ml, review_card_drivers as drivers
from invoice_optimizer.api import create_app
from invoice_optimizer.features import FEATURE_NAMES
from invoice_optimizer.models import Assessment, MovementVersion, ReviewCase
from invoice_optimizer.synthetic import generate_training_data
from test_bulk_review import case_factory
from test_model_ops_api import actual_model
from test_review_cards_api import snapshot


@pytest.fixture
def client(db):
    with TestClient(create_app(db.settings, db)) as value:
        value.headers['Authorization'] = 'Bearer ' + db.settings.api_token
        yield value


@pytest.fixture
def driver_case(db, case_factory, actual_model):
    def create(name='drivers', *, payload_changes=None, **case_options):
        item = case_factory(name, **case_options)
        number = case_options.get('case_version', 1)
        with db.transaction() as session:
            version = session.get(MovementVersion, f'version-{name}-{number}')
            payload = {**version.payload, 'customer_age_days': 200,
                'supplier_age_days': 300, 'cross_border': 0,
                'event_at': '2026-09-10T12:00:00+00:00', **(payload_changes or {})}
            result = ml.score_batch([payload], db.settings.model_dir)[0]
            assert result['status'] == 'SCORED'
            version.payload = payload
            version.amount = Decimal(payload['amount'])
            version.currency = payload['currency']
            assessment = session.scalar(select(Assessment).where(
                Assessment.version_id == version.id, Assessment.kind == 'MODEL'))
            assessment.score = result['score']
            assessment.features = result['feature_values']
            assessment.model_version = result['model_version']
            assessment.policy_version = result['policy_version']
            assessment.feature_schema_version = result['feature_schema_version']
            assessment.flagged = result['flagged']
            assessment.explanation = ml.compact_explanation(result['explanation'])
        return item['case_id']
    return create


def get_driver(client, case_id):
    response = client.get('/api/case-drivers', params=[('case_id', case_id)])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['read_only'] is True and not data['missing_case_ids']
    return data['items'][0]


def edit_assessment(db, case_id, **changes):
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).join(
            MovementVersion, MovementVersion.id == Assessment.version_id).where(
            MovementVersion.movement_id == 'movement-' + case_id.removeprefix('case-'),
            Assessment.kind == 'MODEL'))
        for key, value in changes.items():
            setattr(assessment, key, value)


def set_mathematical_vector(db, case_id, vector, model, metadata):
    """Store a genuine frozen-model output for a labelled mathematical fixture."""
    features = dict(zip(FEATURE_NAMES, map(float, vector), strict=True))
    score = float(model.predict_proba(np.asarray([vector]))[0, 1])
    explanation = ml._explain(features, model, metadata, score)
    edit_assessment(db, case_id, features=features, score=score,
        flagged=score >= metadata['threshold'], explanation=ml.compact_explanation(explanation))
    return explanation


def test_exact_six_feature_bars_reconstruct_real_score_and_keep_raw_evidence(db, driver_case, client, actual_model):
    identifier = driver_case()
    before = snapshot(db)
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['rescored'] is False
    assert result['raw_binding'] == 'VERIFIED_SOURCE_SNAPSHOT'
    assert result['model_version'] == actual_model['model_version']
    assert result['threshold'] == actual_model['threshold']
    assert result['artifact_sha256'] == actual_model['artifact_sha256']
    assert result['contribution_units'] == 'log_odds'
    assert 'not a probability' in result['percent_meaning']
    factors = result['features']
    assert [factor['feature'] for factor in factors] == list(FEATURE_NAMES)
    assert len(factors) == 6
    total = math.fsum(abs(factor['contribution']) for factor in factors)
    assert result['absolute_contribution_total'] == total
    assert math.fsum(factor['share_percent'] for factor in factors) == pytest.approx(100.)
    for factor in factors:
        assert factor['share_percent'] == pytest.approx(abs(factor['contribution']) / total * 100)
        assert factor['raw_value_status'] == 'RECORDED_INPUT'
        assert factor['effect'] == ('INCREASE' if factor['contribution'] > 0 else
                                    'DECREASE' if factor['contribution'] < 0 else 'NEUTRAL')
    logit = result['baseline_log_odds'] + math.fsum(factor['contribution'] for factor in factors)
    assert result['logit'] == pytest.approx(logit, abs=1e-12)
    assert ml._sigmoid(logit) == pytest.approx(result['recorded_score'], abs=1e-12)
    assert result['reconstruction_error'] < 1e-12
    raw = {factor['feature']: factor['raw_value'] for factor in factors}
    assert raw == {'log_amount': '10.00',
        'amount_to_expected': {'amount': '10.00', 'expected_amount': '10.00'},
        'log_customer_age_days': 200, 'log_supplier_age_days': 300,
        'night_hour': {'event_at': '2026-09-10T12:00:00+00:00', 'hour_utc': 12}, 'cross_border': 0}
    assert snapshot(db) == before


def test_signed_and_neutral_contributions_use_absolute_shares_without_fabricated_raw_values(db, driver_case, client, actual_model):
    identifier = driver_case()
    model, metadata = ml._get_model(db.settings.model_dir, actual_model['model_version'])
    scale, classifier = model.named_steps['scale'], model.named_steps['classifier']
    desired = np.asarray([-1., 1., 0., -.25, .5, 0.])
    assert np.all(classifier.coef_[0] != 0)
    vector = scale.mean_ + scale.scale_ * desired / classifier.coef_[0]
    set_mathematical_vector(db, identifier, vector, model, metadata)
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['raw_binding'] == 'UNAVAILABLE'
    assert [factor['effect'] for factor in result['features']] == [
        'DECREASE', 'INCREASE', 'NEUTRAL', 'DECREASE', 'INCREASE', 'NEUTRAL']
    assert [factor['contribution'] for factor in result['features']] == pytest.approx(desired)
    assert math.fsum(factor['share_percent'] for factor in result['features']) == pytest.approx(100.)
    assert all(factor['raw_value'] is None and factor['raw_value_status'] == 'NOT_RETAINED'
               for factor in result['features'])


def test_training_mean_vector_has_six_real_zero_contributions_not_unavailable(db, driver_case, client, actual_model):
    identifier = driver_case()
    model, metadata = ml._get_model(db.settings.model_dir, actual_model['model_version'])
    set_mathematical_vector(db, identifier, model.named_steps['scale'].mean_, model, metadata)
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['absolute_contribution_total'] == 0.
    assert result['logit'] == result['baseline_log_odds']
    assert all(factor['contribution'] == factor['share_percent'] == 0.
               and factor['effect'] == 'NEUTRAL' for factor in result['features'])
    assert result['raw_binding'] == 'UNAVAILABLE'


def test_real_frozen_model_output_of_exactly_zero_is_available_not_missing(db, driver_case, client, actual_model):
    identifier = driver_case()
    model, metadata = ml._get_model(db.settings.model_dir, actual_model['model_version'])
    scale, classifier = model.named_steps['scale'], model.named_steps['classifier']
    vector = scale.mean_.copy()
    vector[0] += scale.scale_[0] * (-1000. - classifier.intercept_[0]) / classifier.coef_[0, 0]
    explanation = set_mathematical_vector(db, identifier, vector, model, metadata)
    assert explanation['score'] == 0.
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['recorded_score'] == 0.
    assert len(result['features']) == 6 and result['threshold'] == actual_model['threshold']
    assert result['features'][0]['effect'] == 'DECREASE'
    assert result['features'][0]['share_percent'] == pytest.approx(100.)


@pytest.mark.parametrize('legacy_receipt', [None, {}])
def test_legacy_missing_receipt_is_reconstructed_only_from_exact_frozen_evidence(db, driver_case, client, legacy_receipt):
    identifier = driver_case()
    original = get_driver(client, identifier)
    edit_assessment(db, identifier, explanation=legacy_receipt)
    result = get_driver(client, identifier)
    assert result == original


@pytest.mark.parametrize('change', [
    {'feature_schema_version': 'wrong-schema'}, {'policy_version': 'wrong-policy'},
    {'model_version': None}, {'score': .5}, {'features': None}, {'features': {}},
])
def test_mismatched_recorded_evidence_cannot_show_bars(db, driver_case, client, change):
    identifier = driver_case()
    edit_assessment(db, identifier, **change)
    result = get_driver(client, identifier)
    assert result['status'] == 'UNAVAILABLE' and result['reason']
    assert result['features'] == [] and result['threshold'] is None


def test_mismatched_flag_cannot_show_bars(db, driver_case, client):
    identifier = driver_case()
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).where(Assessment.kind == 'MODEL'))
        assessment.flagged = not assessment.flagged
    assert get_driver(client, identifier)['status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('field,value', [
    ('score', .5), ('threshold', -1.), ('threshold', True), ('threshold', 10 ** 400),
    ('model_version', 'another-model'), ('policy_version', 'another-policy'),
    ('artifact_sha256', 'a' * 64), ('schema_version', 'not-the-recorded-schema'),
    ('baseline_logit', 999.), ('logit', 999.), ('contribution_units', 'probability'),
    ('contributions', {'log_amount': 999.}),
])
def test_contradictory_compact_receipt_is_not_silently_ignored(db, driver_case, client, field, value):
    identifier = driver_case()
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).where(Assessment.kind == 'MODEL'))
        receipt = deepcopy(assessment.explanation)
        receipt[field] = value
        assessment.explanation = receipt
    result = get_driver(client, identifier)
    assert result['status'] == 'UNAVAILABLE', f'Contradictory {field} was accepted: {result}'
    assert result['features'] == [] and result['threshold'] is None


@pytest.mark.parametrize('field,value', [
    ('baseline_logit', float('nan')), ('logit', float('inf')),
    ('contribution', float('nan')), ('contribution', float('-inf')),
    ('contribution', True), ('contributions', []), ('contributions', None),
    ('entire_receipt', []), ('entire_receipt', 'invalid receipt'),
])
def test_nonfinite_or_malformed_receipt_values_are_unavailable(db, driver_case, actual_model, field, value):
    identifier = driver_case()
    model, metadata = ml._get_model(db.settings.model_dir, actual_model['model_version'])
    with db.transaction() as session:
        case = session.get(ReviewCase, identifier)
        version = session.get(MovementVersion, case.version_id)
        assessment = session.scalar(select(Assessment).where(
            Assessment.version_id == case.version_id, Assessment.kind == 'MODEL'))
    # PostgreSQL rejects NaN/Infinity in JSON itself. Test the helper's boundary
    # with detached objects instead of weakening that storage constraint.
    receipt = deepcopy(assessment.explanation)
    if field == 'entire_receipt':
        receipt = value
    elif field == 'contribution':
        receipt['contributions'][FEATURE_NAMES[0]] = value
    else:
        receipt[field] = value
    assessment.explanation = receipt
    result = drivers.feature_bars(case, version, assessment, lambda _: (model, metadata))
    assert result['status'] == 'UNAVAILABLE' and result['features'] == []
    assert result['threshold'] is None


@pytest.mark.parametrize('change', ['valid', 'contribution', 'feature', 'transformed_value', 'effect', 'missing', 'order', 'count'])
def test_full_legacy_explanation_checks_each_supplied_feature_against_frozen_model(db, driver_case, client, actual_model, change):
    identifier = driver_case()
    model, metadata = ml._get_model(db.settings.model_dir, actual_model['model_version'])
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).where(Assessment.kind == 'MODEL'))
        full = ml._explain(assessment.features, model, metadata, assessment.score)
        if change == 'contribution':
            full['features'][0]['contribution'] += 1.
        elif change == 'feature':
            full['features'][0]['feature'] = 'invented_feature'
        elif change == 'transformed_value':
            full['features'][0]['transformed_value'] = True
        elif change == 'effect':
            full['features'][0]['effect'] = 'INCREASE'  # Actual retained amount decreases log-odds.
        elif change == 'missing':
            del full['features'][0]['contribution']
        elif change == 'order':
            full['features'].reverse()
        elif change == 'count':
            full['features'].pop()
        assessment.explanation = full
    result = get_driver(client, identifier)
    assert result['status'] == ('AVAILABLE' if change == 'valid' else 'UNAVAILABLE')
    if change != 'valid':
        assert result['features'] == [] and result['threshold'] is None


@pytest.mark.parametrize('value', [True, '1.0', None, 10 ** 400, {'value': 1}])
def test_invalid_recorded_feature_types_and_large_integers_fail_closed(db, driver_case, client, value):
    identifier = driver_case()
    with db.transaction() as session:
        assessment = session.scalar(select(Assessment).where(Assessment.kind == 'MODEL'))
        assessment.features = {**assessment.features, FEATURE_NAMES[0]: value}
    result = get_driver(client, identifier)
    assert result['status'] == 'UNAVAILABLE' and result['features'] == []


@pytest.mark.parametrize('value', [True, '0', None, 10 ** 400, float('nan'), float('inf'), -.1, 1.1])
def test_invalid_score_types_are_unavailable_without_loading_any_model(value):
    def forbidden_load(*args, **kwargs):
        pytest.fail('Invalid score must be rejected before model IO')
    result = drivers.feature_bars(SimpleNamespace(id='case', version_id='version'),
        SimpleNamespace(version_no=1), SimpleNamespace(id='assessment', status='SCORED', score=value), forbidden_load)
    assert result['status'] == 'UNAVAILABLE' and result['features'] == []
    assert result['threshold'] is None


@pytest.mark.parametrize('change', ['features', 'amount', 'currency'])
def test_unbound_source_snapshot_keeps_exact_math_but_never_fabricates_raw_values(db, driver_case, client, change):
    identifier = driver_case()
    original = get_driver(client, identifier)
    with db.transaction() as session:
        version = session.get(MovementVersion, 'version-drivers-1')
        if change == 'features':
            version.payload = {**version.payload, 'customer_age_days': 999}
        elif change == 'amount':
            version.amount = Decimal('11.00')
        else:
            version.currency = 'USD'
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['raw_binding'] == 'UNAVAILABLE'
    assert result['raw_binding_reason']
    assert result['recorded_score'] == original['recorded_score']
    assert [factor['contribution'] for factor in result['features']] == [factor['contribution'] for factor in original['features']]
    assert all(factor['raw_value'] is None and factor['raw_value_status'] == 'NOT_RETAINED'
               for factor in result['features'])


def test_exact_large_money_in_list_and_bound_raw_input_is_not_rounded_or_swapped_with_source_b(db, driver_case, client):
    amount = '9007199254740993.01'
    identifier = driver_case(payload_changes={'amount': amount, 'expected_amount': amount,
        'source_values': {'A': {'amount': amount, 'currency': 'GBP'},
                          'B': {'amount': '9007199254740993.02', 'currency': 'USD'}}})
    card = next(item for item in client.get('/api/cases').json() if item['id'] == identifier)
    assert card['transaction']['amount'] == card['transaction']['source_a_amount'] == amount
    assert card['transaction']['source_b_amount'] == '9007199254740993.02'
    assert card['transaction']['source_a_currency'] == 'GBP' and card['transaction']['source_b_currency'] == 'USD'
    result = get_driver(client, identifier)
    assert result['status'] == 'AVAILABLE' and result['raw_binding'] == 'VERIFIED_SOURCE_SNAPSHOT'
    assert result['features'][0]['raw_value'] == amount
    assert result['features'][1]['raw_value'] == {'amount': amount, 'expected_amount': amount}
    assert result['features'][0]['raw_value'] == card['transaction']['amount']


def test_historical_case_uses_its_own_retained_version_even_after_new_active_model(db, driver_case, client, actual_model, tmp_path):
    identifier = driver_case('historical-driver', current=2, case_version=1, status='SUPERSEDED')
    original = get_driver(client, identifier)
    with db.transaction() as session:
        newer = session.get(MovementVersion, 'version-historical-driver-2')
        newer.payload = {**newer.payload, 'amount': '777.00'}
        newer.amount = Decimal('777.00')
        session.add(Assessment(version_id=newer.id, kind='MODEL', status='SCORED', score=.99,
            flagged=True, model_version='must-not-be-used', features={}))
    training = tmp_path / 'newer-driver-model.parquet'
    generate_training_data(training, rows=2000, seed=764, serving_period='2026-09')
    newer_model = ml.train_model(training, db.settings.model_dir)
    assert newer_model['model_version'] != actual_model['model_version']
    result = get_driver(client, identifier)
    assert result == original and result['version_no'] == 1
    assert result['version_id'] == 'version-historical-driver-1'
    assert result['model_version'] == actual_model['model_version']
    # An unavailable original artifact cannot silently fall back to the active one.
    (db.settings.model_dir / actual_model['artifact_path']).unlink()
    missing = get_driver(client, identifier)
    assert missing['status'] == 'UNAVAILABLE' and missing['features'] == []
    assert ml.model_readiness(db.settings.model_dir)['status'] == 'READY'


def test_missing_model_quarantine_and_missing_ids_are_explicit_and_input_order_is_preserved(db, case_factory, client, monkeypatch):
    missing = case_factory('missing-driver', model_status=None)['case_id']
    unavailable = case_factory('unavailable-driver', model_status='UNAVAILABLE', score=None)['case_id']
    quarantine = case_factory('quarantine-driver', quarantine=True)['case_id']
    def forbidden_load(*args, **kwargs):
        pytest.fail('Missing or unavailable assessment must not load a model')
    monkeypatch.setattr(drivers, '_get_model', forbidden_load)
    requested = ['missing-id-first', quarantine, missing, 'missing-id-last', unavailable]
    response = client.get('/api/case-drivers', params=[('case_id', key) for key in requested])
    assert response.status_code == 200, response.text
    data = response.json()
    assert [item['case_id'] for item in data['items']] == [quarantine, missing, unavailable]
    assert data['missing_case_ids'] == ['missing-id-first', 'missing-id-last']
    assert all(item['status'] == 'UNAVAILABLE' and item['features'] == []
               and item['threshold'] is None for item in data['items'])
    assert data['items'][0]['version_id'] is None and data['items'][0]['version_no'] is None


def test_one_and_fifty_cards_use_one_sql_query_and_one_fresh_model_verification_without_mutation(db, driver_case, client, monkeypatch):
    identifiers = [driver_case(f'batch-driver-{index:02}') for index in range(50)]
    baseline = snapshot(db)
    loads, statements = [], []
    real_load = drivers._get_model
    def load(*args, **kwargs):
        loads.append((args[1], kwargs))
        return real_load(*args, **kwargs)
    def forbidden_scoring(*args, **kwargs):
        pytest.fail('A read endpoint must not train or record new model predictions')
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    monkeypatch.setattr(drivers, '_get_model', load)
    monkeypatch.setattr(ml, 'score_batch', forbidden_scoring)
    monkeypatch.setattr(ml, 'train_model', forbidden_scoring)
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        for requested in (identifiers[:1], identifiers):
            loads.clear(); statements.clear()
            response = client.get('/api/case-drivers', params=[('case_id', key) for key in requested])
            assert response.status_code == 200, response.text
            assert len(response.json()['items']) == len(requested)
            assert all(item['status'] == 'AVAILABLE' for item in response.json()['items'])
            assert len(loads) == 1 and loads[0][0] is not None
            assert loads[0][1] == {'verify_fresh': True}
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith('SELECT ')
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert snapshot(db) == baseline


def test_batch_verifies_once_for_each_distinct_frozen_model_and_caches_failure(db, driver_case, client, actual_model, tmp_path, monkeypatch):
    old_ids = [driver_case('old-batch-' + str(index)) for index in range(2)]
    training = tmp_path / 'second-batch-model.parquet'
    generate_training_data(training, rows=2000, seed=765, serving_period='2026-09')
    newer_model = ml.train_model(training, db.settings.model_dir)
    new_ids = [driver_case('new-batch-' + str(index)) for index in range(2)]
    loads = []
    real_load = drivers._get_model
    def load(*args, **kwargs):
        loads.append(args[1])
        return real_load(*args, **kwargs)
    monkeypatch.setattr(drivers, '_get_model', load)
    params = [('case_id', key) for key in old_ids + new_ids]
    response = client.get('/api/case-drivers', params=params)
    assert response.status_code == 200 and all(item['status'] == 'AVAILABLE' for item in response.json()['items'])
    assert sorted(loads) == sorted([actual_model['model_version'], newer_model['model_version']])
    (db.settings.model_dir / actual_model['artifact_path']).write_bytes(b'Deliberately corrupted test model')
    loads.clear()
    response = client.get('/api/case-drivers', params=params)
    assert response.status_code == 200
    assert [item['status'] for item in response.json()['items']] == ['UNAVAILABLE', 'UNAVAILABLE', 'AVAILABLE', 'AVAILABLE']
    assert sorted(loads) == sorted([actual_model['model_version'], newer_model['model_version']])


def test_endpoint_requires_authentication_and_bounded_unique_ids(db, case_factory):
    identifier = case_factory('driver-auth', model_status=None)['case_id']
    with TestClient(create_app(db.settings, db)) as client:
        assert client.get('/api/case-drivers', params={'case_id': identifier}).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + db.settings.api_token
        assert client.get('/api/case-drivers').status_code == 422
        assert client.get('/api/case-drivers', params=[('case_id', str(index)) for index in range(51)]).status_code == 422
        for values in ([identifier, identifier], [''], ['x' * 65]):
            response = client.get('/api/case-drivers', params=[('case_id', key) for key in values])
            assert response.status_code in (400, 422), response.text
        response = client.get('/api/case-drivers', params=[('case_id', str(index)) for index in range(50)])
        assert response.status_code == 200 and len(response.json()['missing_case_ids']) == 50
