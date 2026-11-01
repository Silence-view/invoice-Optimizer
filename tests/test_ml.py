from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from invoice_optimizer.features import FEATURE_NAMES, build_features
from invoice_optimizer.ml import score_batch, train_model
from invoice_optimizer.synthetic import generate_dataset


def _sources(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("ml-fixture")
    data, model = root / "data", root / "model"
    manifest = generate_dataset(data, customers=4, movements=400, seed=42)
    metadata = train_model(data / "training.parquet", model)
    return data, model, manifest, metadata


def test_synthetic_generation_is_repeatable_and_truth_stays_out_of_serving(trained, tmp_path):
    data, _, manifest, _ = trained
    replica = generate_dataset(tmp_path, customers=4, movements=400, seed=42)
    assert replica["files"] == manifest["files"]
    a, b = _sources(data / "source_a.csv"), _sources(data / "source_b.csv")
    assert len(a) == manifest["source_rows"]["A"]
    assert len(b) == manifest["source_rows"]["B"]
    assert len(pq.read_table(data / "source_a.parquet")) == len(a)
    assert {"label", "synthetic_risk_label", "scenario", "label_available_at"}.isdisjoint(a[0])
    assert sum(manifest["customer_movement_counts"].values()) == 400
    assert manifest["scenario_counts"]["HIGH_RISK"] > 0
    assert manifest["scenario_counts"]["CONFLICTING_DUPLICATE"] > 0
    historical = pq.read_table(data / "training.parquet").to_pylist()
    assert max(datetime.fromisoformat(row["label_available_at"]) for row in historical) < datetime.fromisoformat("2026-09-01T00:00:00+00:00")


def test_features_ignore_labels_identifiers_and_future_columns(trained):
    data, *_ = trained
    original = _sources(data / "source_a.csv")[0]
    adversarial = {**original, "label": 1, "future_review_outcome": "CONFIRMED_ISSUE", "scenario": "HIGH_RISK", "customer_id": "ANOTHER", "movement_id": "FUTURE"}
    assert build_features(adversarial) == build_features(original)
    assert tuple(build_features(original)) == FEATURE_NAMES
    # Derive hour from the timestamp in UTC, not its local wall clock.
    timezone_row = {**original, "event_at": "2026-09-02T07:30:00+02:00", "hour_utc": "5"}
    assert build_features(timezone_row)["night_hour"] == 1.0
    timezone_row["hour_utc"] = "7"
    with pytest.raises(ValueError, match="conflicts"):
        build_features(timezone_row)


@pytest.mark.parametrize("patch", [
    {"expected_amount": ""}, {"expected_amount": "0.00"}, {"amount": "NaN"},
    {"amount": 1.25}, {"amount": "-1.00"}, {"amount": "1.001"},
    {"customer_age_days": None}, {"supplier_age_days": "-1"}, {"cross_border": "2"},
    {"event_at": "2026-09-01T00:00:00"},
])
def test_unusable_features_are_unknown_never_a_zero_score(trained, patch):
    data, model, *_ = trained
    original = _sources(data / "source_a.csv")[0]
    result = score_batch([{**original, **patch}], model)[0]
    assert result["status"] == "UNAVAILABLE"
    assert result["score"] is None
    assert result["flagged"] is False
    assert result["reason"].startswith("FEATURES_UNAVAILABLE")
    assert result["model_version"] is not None


def test_missing_model_is_unknown_and_valid_batch_members_survive_bad_rows(trained, tmp_path):
    data, model, _, metadata = trained
    original = _sources(data / "source_a.csv")[0]
    missing = score_batch([original], tmp_path / "no-model")[0]
    assert missing["status"] == "UNAVAILABLE" and missing["score"] is None
    scores = score_batch([original, {**original, "amount": "bad"}, original], model)
    assert scores[0] == scores[2]
    assert scores[0]["status"] == "SCORED"
    assert 0 <= scores[0]["score"] <= 1
    assert scores[0]["model_version"] == metadata["model_version"]
    assert scores[1]["status"] == "UNAVAILABLE"


def test_model_does_not_score_before_its_information_cutoff(trained):
    data, model, _, metadata = trained
    original = _sources(data / "source_a.csv")[0]
    historical = {**original, "event_at": metadata["information_cutoff"]}
    historical["hour_utc"] = str(datetime.fromisoformat(historical["event_at"]).hour)
    result = score_batch([historical], model)[0]
    assert result["status"] == "UNAVAILABLE" and result["score"] is None
    assert "cutoff" in result["reason"]


def test_clean_customer_is_billable_and_generated_risk_can_be_flagged(trained):
    data, model, *_ = trained
    rows = _sources(data / "source_a.csv")
    clean = [row for row in rows if row["customer_id"] == "C0001"]
    clean_scores = score_batch(clean, model)
    assert clean_scores and all(result["status"] == "SCORED" and not result["flagged"] for result in clean_scores)
    labels = _sources(data / "scenario_labels.csv")
    high_risk_ids = {row["movement_id"] for row in labels if row["scenario"] == "HIGH_RISK"}
    risk_scores = score_batch([row for row in rows if row["movement_id"] in high_risk_ids], model)
    assert risk_scores and all(result["status"] == "SCORED" for result in risk_scores)
    assert any(result["flagged"] for result in risk_scores)


def test_train_validation_test_boundaries_include_label_availability(trained):
    _, _, _, metadata = trained
    train, validation, test = (metadata["splits"][key] for key in ("train", "validation", "test"))
    assert train["event_end"] < validation["event_start"] < test["event_start"]
    assert train["last_label_available_at"] < metadata["validation_start"]
    assert validation["last_label_available_at"] < metadata["test_start"]
    assert metadata["excluded_pending_label_rows"] > 0
    assert metadata["threshold_selection"]["partition"] == "validation"
    assert metadata["approved_for"] == "SYNTHETIC_SIMULATION_ONLY"
    assert "synthetic" in metadata["evaluation_scope"]


def test_test_labels_cannot_change_fitted_weights_or_threshold(trained, tmp_path):
    data, model, _, before = trained
    rows = pq.read_table(data / "training.parquet").to_pylist()
    for row in rows:
        if row["event_at"] >= before["test_start"]:
            row["label"] = 1 - row["label"]
    changed = tmp_path / "changed-test-labels.parquet"
    pq.write_table(pa.Table.from_pylist(rows), changed)
    after = train_model(changed, tmp_path / "model")
    assert after["threshold"] == before["threshold"]
    assert after["validation_metrics"] == before["validation_metrics"]
    first = joblib.load(model / before["artifact_path"])
    second = joblib.load(tmp_path / "model" / after["artifact_path"])
    np.testing.assert_array_equal(first.named_steps["classifier"].coef_, second.named_steps["classifier"].coef_)
    np.testing.assert_array_equal(first.named_steps["scale"].mean_, second.named_steps["scale"].mean_)
    assert after["test_metrics"]["average_precision"] != before["test_metrics"]["average_precision"]


def test_tampered_artifact_is_unavailable_before_deserialization(trained, tmp_path):
    data, _, _, _ = trained
    metadata = train_model(data / "training.parquet", tmp_path / "model")
    payload = _sources(data / "source_a.csv")[0]
    assert score_batch([payload], tmp_path / "model")[0]["status"] == "SCORED"
    artifact = tmp_path / "model" / metadata["artifact_path"]
    artifact.write_bytes(b"untrusted model content")
    result = score_batch([payload], tmp_path / "model")[0]
    assert result["score"] is None and result["status"] == "UNAVAILABLE"
    assert result["reason"].startswith("MODEL_UNAVAILABLE")


def test_identical_retraining_preserves_immutable_artifact(trained, tmp_path):
    data, *_ = trained
    before = train_model(data / "training.parquet", tmp_path / "model")
    artifact = tmp_path / "model" / before["artifact_path"]
    original_mtime = artifact.stat().st_mtime_ns
    after = train_model(data / "training.parquet", tmp_path / "model")
    assert before == after
    assert artifact.stat().st_mtime_ns == original_mtime


def test_training_refuses_unlabelled_real_data(trained, tmp_path):
    data, *_ = trained
    rows = pq.read_table(data / "training.parquet").to_pylist()
    rows[0]["synthetic"] = False
    changed = tmp_path / "not-synthetic.parquet"
    pq.write_table(pa.Table.from_pylist(rows), changed)
    with pytest.raises(ValueError, match="synthetic"):
        train_model(changed, tmp_path / "model")


def test_explicit_largest_customer_size_is_not_multiplied_by_customer_count(tmp_path):
    manifest = generate_dataset(tmp_path, customers=4, movements=120, largest_customer_movements=90)
    assert manifest["movements"] == 120
    assert manifest["customer_movement_counts"]["C0001"] == 90
    assert sum(manifest["customer_movement_counts"].values()) == 120


def test_explanation_is_exact_logistic_decomposition_and_preserves_raw_evidence(trained):
    import math
    from invoice_optimizer.ml import explain_recorded
    data,model,_,metadata=trained
    payloads=[row for row in _sources(data/'source_a.csv') if row['expected_amount'] and row['event_at']!='not-a-timestamp' and not row['amount'].startswith('-')][:80]
    results=score_batch(payloads,model)
    assert all(result['status']=='SCORED' for result in results)
    for payload,result in zip(payloads,results):
        explanation=result['explanation']
        assert explanation['status']=='AVAILABLE' and explanation['contribution_units']=='log_odds'
        assert [item['feature'] for item in explanation['features']]==list(FEATURE_NAMES)
        reconstructed=explanation['intercept']+math.fsum(item['contribution'] for item in explanation['features'])
        assert reconstructed==explanation['logit']
        assert 1/(1+math.exp(-reconstructed))==pytest.approx(result['score'],abs=1e-12)
        for item in explanation['features']:
            assert item['standardized_value']==pytest.approx((item['transformed_value']-item['training_mean'])/item['scaling_divisor'])
            assert item['contribution']==pytest.approx(item['standardized_value']*item['coefficient'])
            assert item['raw_value_status']=='RECORDED_INPUT'
        assert explanation['features'][0]['raw_value']==payload['amount']
        assert explanation['reconstruction_error']<=1e-12
        old=explain_recorded(result['feature_values'],model,result['model_version'],result['score'],result['policy_version'])
        assert old['score']==result['score'] and old['reconstruction_error']<=1e-12
        assert all(item['raw_value'] is None and item['raw_value_status']=='NOT_RETAINED' for item in old['features'])
    assert metadata['coefficients']['feature_order']==list(FEATURE_NAMES)
    assert metadata['training_costs']['fit_seconds']>0


def test_recorded_explanation_never_falls_back_to_active_version(trained,tmp_path):
    from invoice_optimizer.ml import explain_recorded,activate_model,model_readiness
    from invoice_optimizer.synthetic import generate_training_data
    data,_,_,_=trained
    first=train_model(data/'training.parquet',tmp_path/'model')
    payload=_sources(data/'source_a.csv')[0]
    recorded=score_batch([payload],tmp_path/'model')[0]
    old_metadata=(tmp_path/'model'/'versions'/first['model_version']/'metadata.json').read_bytes()
    generate_training_data(tmp_path/'independent.parquet',rows=1600,seed=723)
    second=train_model(tmp_path/'independent.parquet',tmp_path/'model',activate=False)
    assert second['model_version']!=first['model_version']
    assert model_readiness(tmp_path/'model')['metadata']['model_version']==first['model_version']
    activate_model(tmp_path/'model',second['model_version'])
    assert model_readiness(tmp_path/'model')['metadata']['model_version']==second['model_version']
    assert (tmp_path/'model'/'versions'/first['model_version']/'metadata.json').read_bytes()==old_metadata
    explanation=explain_recorded(recorded['feature_values'],tmp_path/'model',first['model_version'],recorded['score'],first['policy_version'])
    assert explanation['status']=='AVAILABLE' and explanation['score']==recorded['score']
    (tmp_path/'model'/first['artifact_path']).unlink()
    assert explain_recorded(recorded['feature_values'],tmp_path/'model',first['model_version'],recorded['score'],first['policy_version'])['status']=='UNAVAILABLE'
    assert model_readiness(tmp_path/'model')['status']=='READY'


@pytest.mark.parametrize('change',['score','policy','feature_missing','feature_extra','nan','model_missing','model_path'])
def test_explanation_rejects_mismatched_recorded_evidence(trained,change):
    from invoice_optimizer.ml import explain_recorded
    data,model,_,_=trained
    result=score_batch([_sources(data/'source_a.csv')[0]],model)[0]
    values=dict(result['feature_values']);score=result['score'];version=result['model_version'];policy=result['policy_version']
    if change=='score':score=.5
    elif change=='policy':policy='other-policy'
    elif change=='feature_missing':values.pop(FEATURE_NAMES[0])
    elif change=='feature_extra':values['label']=1
    elif change=='nan':values[FEATURE_NAMES[0]]=float('nan')
    elif change=='model_missing':version=None
    else:version='../metadata.json'
    explanation=explain_recorded(values,model,version,score,policy)
    assert explanation['status']=='UNAVAILABLE' and 'features' not in explanation


def test_preview_uses_frozen_model_and_does_not_mutate_recorded_inputs(trained):
    from copy import deepcopy
    from invoice_optimizer.ml import explain_features,explain_recorded
    data,model,_,metadata=trained
    payload=_sources(data/'source_a.csv')[0];result=score_batch([payload],model)[0]
    original=deepcopy(result)
    changed={**payload,'expected_amount':'1.00'};features=build_features(changed)
    preview=explain_features(features,model,metadata['model_version'],metadata['policy_version'],payload=changed)
    assert preview['status']=='AVAILABLE' and preview['calculation_kind']=='HYPOTHETICAL'
    assert preview['score']!=result['score'] and result==original
    assert preview['features'][1]['raw_value']['expected_amount']=='1.00'
    assert explain_features(features,model,metadata['model_version'],metadata['policy_version'],payload=payload)['status']=='UNAVAILABLE'
    assert explain_recorded(result['feature_values'],model,metadata['model_version'],result['score'],metadata['policy_version'])['calculation_kind']=='RECORDED'


def test_training_generation_is_independent_bounded_repeatable_and_no_overwrite(tmp_path):
    import hashlib
    from invoice_optimizer.synthetic import generate_training_data,PARQUET_BATCH_SIZE
    first=generate_training_data(tmp_path/'one'/'training.parquet',rows=9000,seed=20261027)
    second=generate_training_data(tmp_path/'two'/'training.parquet',rows=9000,seed=20261027)
    assert first==second
    path=tmp_path/'one'/'training.parquet'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==first['sha256']
    assert json.loads(path.with_suffix('.manifest.json').read_text())==first
    parquet=pq.ParquetFile(path)
    assert parquet.metadata.num_rows==9000 and first['positive_rows']==450
    assert parquet.metadata.num_row_groups==2
    assert all(parquet.metadata.row_group(i).num_rows<=PARQUET_BATCH_SIZE for i in range(parquet.metadata.num_row_groups))
    assert parquet.metadata.row_group(0).column(0).compression=='ZSTD'
    assert first['last_label_available_at']<'2026-09-01T00:00:00+00:00'
    assert {x.name for x in path.parent.iterdir()}=={'training.parquet','training.manifest.json'}
    with pytest.raises(ValueError,match='exists'):generate_training_data(path,rows=9000,seed=20261027)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==first['sha256']


def test_model_readiness_checks_frozen_pipeline_and_active_metadata(trained,tmp_path):
    import hashlib
    from invoice_optimizer.ml import model_readiness
    data,_,_,_=trained
    model=tmp_path/'model';metadata=train_model(data/'training.parquet',model)
    assert model_readiness(model)['status']=='READY'
    frozen=model/'versions'/metadata['model_version']/'metadata.json'
    original=frozen.read_text()
    altered={**metadata,'feature_names':list(reversed(FEATURE_NAMES))}
    frozen.write_text(json.dumps(altered));(model/'metadata.json').write_text(json.dumps(altered))
    assert model_readiness(model)['status']=='UNAVAILABLE'
    frozen.write_text(original);(model/'metadata.json').write_text(original)
    assert model_readiness(model)['status']=='READY'
    artifact=model/metadata['artifact_path']
    pipeline=joblib.load(artifact);pipeline.named_steps['classifier'].coef_[0,0]+=1
    joblib.dump(pipeline,artifact)
    altered={**metadata,'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}
    frozen.write_text(json.dumps(altered));(model/'metadata.json').write_text(json.dumps(altered))
    # Even a matching byte checksum cannot make a coefficient snapshot mismatch READY.
    assert model_readiness(model)['status']=='UNAVAILABLE'


def test_legacy_frozen_metadata_without_explanation_fields_still_explains(trained,tmp_path):
    import hashlib
    import shutil
    from invoice_optimizer.ml import explain_recorded,model_readiness
    data,original_model,_,metadata=trained
    legacy={k:v for k,v in metadata.items() if k not in {'training_costs','training_recipe','training_recipe_sha256','version_identity_sha256','coefficients','explanation_version','training_rows','feature_count'}}
    legacy['training_recipe_version']='standard-scaler-lr-lbfgs-v1'
    inputs=json.dumps({'data':legacy['training_sha256'],'features':legacy['feature_schema_version'],'policy':legacy['policy_version'],'recipe':legacy['training_recipe_version'],'sklearn':legacy['sklearn_version']},sort_keys=True)
    legacy['model_version']='lr-'+hashlib.sha256(inputs.encode()).hexdigest()[:20]
    legacy['artifact_path']='versions/'+legacy['model_version']+'/model.joblib'
    artifact=tmp_path/legacy['artifact_path'];artifact.parent.mkdir(parents=True)
    shutil.copyfile(original_model/metadata['artifact_path'],artifact)
    (artifact.parent/'metadata.json').write_text(json.dumps(legacy));(tmp_path/'metadata.json').write_text(json.dumps(legacy))
    assert model_readiness(tmp_path)['status']=='READY'
    result=score_batch([_sources(data/'source_a.csv')[0]],tmp_path)[0]
    assert explain_recorded(result['feature_values'],tmp_path,legacy['model_version'],result['score'],legacy['policy_version'])['status']=='AVAILABLE'


def test_parallel_trainers_publish_one_complete_immutable_version(trained,tmp_path):
    import subprocess
    import sys
    from invoice_optimizer.ml import model_readiness
    data,_,_,_=trained
    model=tmp_path/'parallel-model'
    command=[sys.executable,'-c','from pathlib import Path; import sys; from invoice_optimizer.ml import train_model; train_model(Path(sys.argv[1]),Path(sys.argv[2]))',str(data/'training.parquet'),str(model)]
    processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    for process in processes:
        _,error=process.communicate(timeout=30)
        assert process.returncode==0,error
    result=model_readiness(model)
    assert result['status']=='READY'
    assert len(list((model/'versions').iterdir()))==1
    frozen=model/'versions'/result['metadata']['model_version']/'metadata.json'
    before=frozen.read_bytes();before_mtime=frozen.stat().st_mtime_ns
    assert train_model(data/'training.parquet',model)==result['metadata']
    assert frozen.read_bytes()==before and frozen.stat().st_mtime_ns==before_mtime
    assert not list(model.glob('.active-*'))


def test_failed_activation_keeps_the_previous_active_pointer(trained,tmp_path,monkeypatch):
    import invoice_optimizer.ml as module
    from invoice_optimizer.synthetic import generate_training_data
    data,_,_,_=trained
    model=tmp_path/'model';first=train_model(data/'training.parquet',model)
    before=(model/'metadata.json').read_bytes()
    generate_training_data(tmp_path/'second.parquet',rows=1000,seed=321)
    second=train_model(tmp_path/'second.parquet',model,activate=False)
    original=module.os.replace
    def fail_replace(source,target):
        if Path(target)==model/'metadata.json':raise OSError('injected pointer failure')
        return original(source,target)
    monkeypatch.setattr(module.os,'replace',fail_replace)
    with pytest.raises(OSError):module.activate_model(model,second['model_version'])
    assert (model/'metadata.json').read_bytes()==before
    assert module.model_readiness(model)['metadata']['model_version']==first['model_version']
    assert module.model_readiness(model,second['model_version'])['status']=='READY'
    assert not list(model.glob('.active-*'))


def test_compact_math_receipt_is_bounded_and_exact(trained):
    import math
    from invoice_optimizer.ml import compact_explanation
    data,model,_,_=trained
    result=score_batch([_sources(data/'source_a.csv')[0]],model)[0]
    receipt=compact_explanation(result['explanation'])
    assert len(json.dumps(receipt).encode())<=1024
    assert set(receipt['contributions'])==set(FEATURE_NAMES)
    logit=receipt['baseline_logit']+math.fsum(receipt['contributions'].values())
    assert logit==receipt['logit']
    assert 1/(1+math.exp(-logit))==pytest.approx(result['score'],abs=1e-12)
    assert 'features' not in receipt and 'top_reasons' not in receipt
    assert compact_explanation(None)['status']=='UNAVAILABLE'
    damaged={**result['explanation'],'score':.5}
    with pytest.raises(ValueError):compact_explanation(damaged)


def test_batch_uses_one_vectorized_prediction(trained,monkeypatch):
    import invoice_optimizer.ml as module
    data,directory,_,_=trained
    model,_=module._get_model(directory)
    original=model.predict_proba;calls=[]
    def count(matrix):
        calls.append(len(matrix));return original(matrix)
    monkeypatch.setattr(model,'predict_proba',count)
    payload=_sources(data/'source_a.csv')[0]
    scored=score_batch([payload]*75,directory)
    assert calls==[75] and all(row['status']=='SCORED' for row in scored)


def test_training_limits_native_numeric_pools_and_restores_them(trained,tmp_path,monkeypatch):
    import invoice_optimizer.ml as module
    from threadpoolctl import threadpool_info
    data,*_=trained
    observed=[]
    before={item['prefix']:item['num_threads'] for item in threadpool_info()}
    original_fit=module.LogisticRegression.fit
    original_predict=module.LogisticRegression.predict_proba
    def capture_fit(self,*args,**kwargs):
        observed.append(('fit',threadpool_info()))
        return original_fit(self,*args,**kwargs)
    def capture_predict(self,*args,**kwargs):
        # Validation and test prediction each have hundreds of rows. The loader
        # independently probes two rows after the bounded training context.
        if len(args[0])>2:observed.append(('predict',threadpool_info()))
        return original_predict(self,*args,**kwargs)
    monkeypatch.setattr(module.LogisticRegression,'fit',capture_fit)
    monkeypatch.setattr(module.LogisticRegression,'predict_proba',capture_predict)
    metadata=train_model(data/'training.parquet',tmp_path/'bounded-model',activate=False)
    assert [name for name,_ in observed]==['fit','predict','predict']
    assert observed[0][1], 'A native numerical pool is expected in the installed sklearn runtime'
    assert all(pool['num_threads']==1 for _,pools in observed for pool in pools)
    assert metadata['training_costs']['native_thread_limit']==1
    assert {item['prefix']:item['num_threads'] for item in threadpool_info()}==before
