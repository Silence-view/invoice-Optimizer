"""Frozen Logistic Regression with chronological, synthetic-only evaluation."""

from __future__ import annotations

from functools import lru_cache
from contextlib import contextmanager
import fcntl
import math
import os
import re
import shutil
import tempfile
import time
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pyarrow.parquet as pq
import sklearn
from threadpoolctl import threadpool_limits
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_fscore_support, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FEATURE_DESCRIPTIONS, build_features, feature_raw_values, parse_event_at

MAX_TRAINING_ROWS = 100_000
POLICY_VERSION = "synthetic-validation-f2-v1"
TRAINING_RECIPE_VERSION = "standard-scaler-lr-lbfgs-explained-v3"
EXPLANATION_VERSION = "exact-linear-logodds-v1"


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, scores >= threshold, average="binary", zero_division=0
    )
    return {
        "rows": int(len(labels)), "positive_rows": int(labels.sum()),
        "average_precision": float(average_precision_score(labels, scores)),
        "roc_auc": float(roc_auc_score(labels, scores)),
        "brier_score": float(brier_score_loss(labels, scores)),
        "precision": float(precision), "recall": float(recall), "f1": float(f1),
        "flagged_rows": int((scores >= threshold).sum()),
        "false_positive_rows": int(((scores >= threshold) & (labels == 0)).sum()),
        "false_negative_rows": int(((scores < threshold) & (labels == 1)).sum()),
    }


def _choose_threshold(labels: np.ndarray, scores: np.ndarray) -> tuple[float, float]:
    """O(n log n) validation-only F2 selection, ties prefer fewer alerts."""
    order = np.argsort(-scores, kind="stable")
    sorted_scores, sorted_labels = scores[order], labels[order]
    true_positives = np.cumsum(sorted_labels)
    # Evaluate only at score-group boundaries because >= flags an entire tie.
    ends = np.r_[np.flatnonzero(sorted_scores[:-1] != sorted_scores[1:]), len(scores) - 1]
    tp = true_positives[ends]
    predicted_positive = ends + 1
    fp = predicted_positive - tp
    fn = int(labels.sum()) - tp
    f2 = 5 * tp / (5 * tp + 4 * fn + fp)
    best = int(np.argmax(f2))  # descending thresholds -> highest wins exact ties
    return float(sorted_scores[ends[best]]), float(f2[best])


def train_model(training_path: Path, output_dir: Path, *, activate: bool = True) -> dict:
    """Train once on past synthetic rows and freeze the entire serving pipeline.

    Inputs must explicitly identify synthetic rows and label availability. The
    bounded baseline rejects >100k training rows; it never loads serving volumes
    into memory. Feature scaling and model fitting use the training split only.
    The test split is inspected only after selecting the validation threshold.
    """
    started = time.perf_counter()
    if type(activate) is not bool:
        raise ValueError("activate must be a boolean")
    training_path, output_dir = Path(training_path), Path(output_dir)
    training_hash = _digest_file(training_path)
    parquet = pq.ParquetFile(training_path)
    if not 100 <= parquet.metadata.num_rows <= MAX_TRAINING_ROWS:
        raise ValueError(f"training data must contain 100..{MAX_TRAINING_ROWS} rows")
    rows = parquet.read().to_pylist()
    if any(row.get("synthetic") is not True for row in rows):
        raise ValueError("this demo training entry point accepts explicitly synthetic rows only")
    for row in rows:
        if type(row.get("label")) is not int or row["label"] not in (0, 1):
            raise ValueError("training labels must be integer 0 or 1")
        row["_event"] = parse_event_at(row.get("event_at"))
        row["_label_available"] = parse_event_at(row.get("label_available_at"))
        if row["_label_available"] < row["_event"]:
            raise ValueError("label availability cannot precede the movement event")
    rows.sort(key=lambda row: row["_event"])
    event_times = [row["_event"] for row in rows]
    validation_start = event_times[int(len(rows) * 0.6)]
    test_start = event_times[int(len(rows) * 0.8)]
    if validation_start <= event_times[0] or test_start <= validation_start:
        raise ValueError("insufficient distinct timestamps for chronological splits")
    partitions = {
        "train": [row for row in rows if row["_event"] < validation_start and row["_label_available"] < validation_start],
        "validation": [row for row in rows if validation_start <= row["_event"] < test_start and row["_label_available"] < test_start],
        "test": [row for row in rows if row["_event"] >= test_start],
    }
    arrays = {}
    split_evidence = {}
    for name, subset in partitions.items():
        if not subset or {row["label"] for row in subset} != {0, 1}:
            raise ValueError(f"{name} split must contain both classes after label-availability filtering")
        features = [build_features(row) for row in subset]
        arrays[name] = (
            np.asarray([[feature[key] for key in FEATURE_NAMES] for feature in features], dtype=np.float64),
            np.asarray([row["label"] for row in subset], dtype=np.int8),
        )
        split_evidence[name] = {
            "rows": len(subset), "positive_rows": sum(row["label"] for row in subset),
            "event_start": min(row["_event"] for row in subset).isoformat(),
            "event_end": max(row["_event"] for row in subset).isoformat(),
            "last_label_available_at": max(row["_label_available"] for row in subset).isoformat(),
        }
    model = Pipeline([
        ("scale", StandardScaler()),
        ("classifier", LogisticRegression(max_iter=1000, solver="lbfgs", random_state=42)),
    ])
    fit_started = time.perf_counter()
    # A background training thread must not fan out into unrestricted native
    # BLAS/OpenMP pools while ordinary movement scoring continues.
    with threadpool_limits(limits=1):
        model.fit(*arrays["train"])
        fit_seconds = time.perf_counter() - fit_started
        if np.any(model.named_steps["classifier"].n_iter_ >= 1000):
            raise ValueError("training did not converge within the iteration budget")
        validation_scores = model.predict_proba(arrays["validation"][0])[:, 1]
        threshold, validation_f2 = _choose_threshold(arrays["validation"][1], validation_scores)
        test_scores = model.predict_proba(arrays["test"][0])[:, 1]
    if _digest_file(training_path) != training_hash:
        raise ValueError("training data changed during fitting")
    # Data identity + feature/policy/library versions determine this immutable
    # model version. A change in training data creates a new artifact directory.
    version_inputs = json.dumps({"data": training_hash, "features": FEATURE_SCHEMA_VERSION, "policy": POLICY_VERSION, "recipe": TRAINING_RECIPE_VERSION, "sklearn": sklearn.__version__}, sort_keys=True)
    model_version = "lr-" + hashlib.sha256(version_inputs.encode()).hexdigest()[:20]
    recipe = {"version": TRAINING_RECIPE_VERSION, "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_order": list(FEATURE_NAMES), "scaler": {"class": "StandardScaler", "with_mean": True, "with_std": True},
        "classifier": {"class": "LogisticRegression", "solver": "lbfgs", "max_iter": 1000, "C": 1.0,
            "random_state": 42, "class_weight": None}, "split": "Chronological 60/20/20 with label-availability exclusions",
        "threshold_policy": POLICY_VERSION, "sklearn_version": sklearn.__version__, "native_thread_limit": 1}
    information_cutoff = max(row["_label_available"] for row in partitions["train"] + partitions["validation"])
    metadata = {
        "model_version": model_version, "policy_version": POLICY_VERSION,
        "training_recipe_version": TRAINING_RECIPE_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION, "feature_names": list(FEATURE_NAMES),
        "algorithm": "StandardScaler + sklearn LogisticRegression (frozen batch inference)",
        "sklearn_version": sklearn.__version__, "synthetic_only": True,
        "approved_for": "SYNTHETIC_SIMULATION_ONLY", "threshold": threshold,
        "threshold_selection": {"partition": "validation", "objective": "F2", "value": validation_f2, "tie_break": "highest threshold"},
        "training_sha256": training_hash,
        "artifact_path": f"versions/{model_version}/model.joblib",
        "training_recipe": recipe,
        "training_recipe_sha256": hashlib.sha256(json.dumps(recipe,sort_keys=True).encode()).hexdigest(),
        "version_identity_sha256": hashlib.sha256(version_inputs.encode()).hexdigest(),
        "training_rows": len(rows), "feature_count": len(FEATURE_NAMES),
        "training_costs": {"fit_seconds": fit_seconds, "elapsed_seconds_before_publication": time.perf_counter()-started,
            "matrix_bytes": sum(x.nbytes+y.nbytes for x,y in arrays.values()),
            "max_training_rows": MAX_TRAINING_ROWS, "native_thread_limit": 1,
            "optimizer_iterations": model.named_steps["classifier"].n_iter_.tolist()},
        "explanation_version": EXPLANATION_VERSION,
        "coefficients": _parameters(model),
        "information_cutoff": information_cutoff.isoformat(),
        "validation_start": validation_start.isoformat(), "test_start": test_start.isoformat(),
        "excluded_pending_label_rows": len(rows) - sum(len(part) for part in partitions.values()),
        "splits": split_evidence,
        "validation_metrics": _metrics(arrays["validation"][1], validation_scores, threshold),
        "test_metrics": _metrics(arrays["test"][1], test_scores, threshold),
        "evaluation_scope": "Chronological held-out synthetic test; deliberately separable generated patterns. Not evidence of real-world accuracy, calibration, fraud, or compliance.",
        "limitations": [
            "Labels describe generated model-risk scenarios only, not deterministic reconciliation failures",
            "No online fitting; reviewed real labels, separate validation and explicit model approval are required before production",
            "predict_proba is a model output, not proof that a movement is genuine or a calibrated real-world probability",
            "Serving is unavailable for timestamps at or before the model information cutoff",
        ],
    }
    # Unique staging paths and a process-shared publication lock avoid both
    # partial versions and two concurrent trainers overwriting shared temp files.
    versions = output_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
    try:
        artifact = staging / "model.joblib"
        joblib.dump(model, artifact)
        metadata["artifact_sha256"] = _digest_file(artifact)
        serialized = json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n"
        _write_sync(staging / "metadata.json", serialized.encode())
        with artifact.open("rb") as stream:
            os.fsync(stream.fileno())
        with _publication_lock(output_dir):
            destination = versions / model_version
            if destination.exists():
                _, existing = _get_model(output_dir, model_version, verify_fresh=True)
                if existing["artifact_sha256"] != metadata["artifact_sha256"]:
                    raise ValueError("model version exists with different bytes; use a new training recipe version")
                # Wall-clock costs belong to the first immutable publication.
                candidate = {k:v for k,v in metadata.items() if k != "training_costs"}
                recorded = {k:v for k,v in existing.items() if k != "training_costs"}
                if candidate != recorded:
                    raise ValueError("immutable model metadata differs for the same version")
                metadata = existing
            else:
                os.rename(staging, destination)
                _sync_directory(versions)
            _get_model(output_dir, model_version, verify_fresh=True)
            if activate:
                _activate_unlocked(output_dir, metadata)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return metadata


def _parameters(model):
    scale, classifier = model.named_steps["scale"], model.named_steps["classifier"]
    return {"feature_order": list(FEATURE_NAMES), "training_mean": scale.mean_.tolist(),
        "training_std": np.sqrt(scale.var_).tolist(), "scaling_divisor": scale.scale_.tolist(),
        "coefficient": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
        "units": "log_odds", "baseline": "Training-split mean of each transformed feature"}


def _write_sync(path, content):
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".publication.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _activate_unlocked(directory, metadata):
    descriptor, temporary = tempfile.mkstemp(prefix=".active-", suffix=".json", dir=directory)
    path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False)+"\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, Path(directory) / "metadata.json")
        _sync_directory(directory)
    finally:
        path.unlink(missing_ok=True)


def activate_model(model_dir: Path, model_version: str) -> dict:
    """Validate a complete immutable version before switching the active pointer."""
    with _publication_lock(model_dir):
        _, metadata = _get_model(model_dir, model_version, verify_fresh=True)
        _activate_unlocked(Path(model_dir), metadata)
        return metadata


def _validate_pipeline(model):
    if type(model) is not Pipeline or list(model.named_steps) != ["scale", "classifier"]:
        raise ValueError("unsupported model pipeline")
    scale, classifier = model.named_steps["scale"], model.named_steps["classifier"]
    if type(scale) is not StandardScaler or type(classifier) is not LogisticRegression:
        raise ValueError("unsupported model estimator")
    if not scale.with_mean or not scale.with_std or list(classifier.classes_) != [0, 1]:
        raise ValueError("unsupported scaler or classes")
    size = len(FEATURE_NAMES)
    if model.n_features_in_ != size or classifier.coef_.shape != (1,size) or classifier.intercept_.shape != (1,):
        raise ValueError("model feature dimensions differ")
    for values in (scale.mean_, scale.var_, scale.scale_, classifier.coef_[0], classifier.intercept_):
        if not np.isfinite(values).all():
            raise ValueError("model parameters must be finite")
    if any(value.shape != (size,) for value in (scale.mean_,scale.var_,scale.scale_)) or np.any(scale.scale_ <= 0) or np.any(scale.var_ < 0):
        raise ValueError("invalid training scaler")
    if not np.allclose(scale.scale_, np.where(scale.var_ == 0, 1., np.sqrt(scale.var_)), rtol=1e-12, atol=1e-12):
        raise ValueError("training scale does not match variance")
    probe = np.asarray([scale.mean_,scale.mean_+scale.scale_])
    actual = model.predict_proba(probe)[:,1]
    logits = classifier.intercept_[0]+((probe-scale.mean_)/scale.scale_) @ classifier.coef_[0]
    if not np.allclose(actual, [_sigmoid(float(x)) for x in logits], rtol=1e-12, atol=1e-12):
        raise ValueError("model output does not match the logistic formula")


@lru_cache(maxsize=8)
def _load_artifact(directory: str, metadata_text: str, fingerprint: tuple):
    """Deserialize only trusted, checksum-verified local model files."""
    del fingerprint
    metadata = json.loads(metadata_text)
    artifact = Path(directory) / metadata["artifact_path"]
    if _digest_file(artifact) != metadata.get("artifact_sha256"):
        raise ValueError("model artifact checksum mismatch")
    model = joblib.load(artifact)
    _validate_pipeline(model)
    if "coefficients" in metadata and _parameters(model) != metadata["coefficients"]:
        raise ValueError("metadata coefficients differ from the frozen model")
    return model


def _get_model(model_dir, model_version=None, *, verify_fresh=False):
    root = Path(model_dir).resolve()
    active = None
    if model_version is None:
        active = json.loads((root / "metadata.json").read_text())
        model_version = active.get("model_version")
    if not isinstance(model_version,str) or re.fullmatch(r"lr-[a-f0-9]{20}",model_version) is None:
        raise ValueError("invalid frozen model version")
    path = root / "versions" / model_version / "metadata.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("metadata escapes the model directory")
    metadata_text = path.read_text()
    metadata = json.loads(metadata_text)
    if metadata.get("model_version") != model_version or (active is not None and active != metadata):
        raise ValueError("active and versioned metadata differ")
    identity = json.dumps({"data":metadata.get("training_sha256"),"features":metadata.get("feature_schema_version"),
        "policy":metadata.get("policy_version"),"recipe":metadata.get("training_recipe_version"),
        "sklearn":metadata.get("sklearn_version")},sort_keys=True)
    if "lr-"+hashlib.sha256(identity.encode()).hexdigest()[:20] != model_version:
        raise ValueError("model version does not match its declared training identity")
    if "training_recipe" in metadata and hashlib.sha256(json.dumps(metadata["training_recipe"],sort_keys=True).encode()).hexdigest()!=metadata.get("training_recipe_sha256"):
        raise ValueError("training recipe checksum differs")
    if metadata.get("feature_schema_version") != FEATURE_SCHEMA_VERSION or metadata.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("model feature schema mismatch")
    if metadata.get("approved_for") != "SYNTHETIC_SIMULATION_ONLY" or metadata.get("synthetic_only") is not True:
        raise ValueError("model is not approved for this simulation")
    if metadata.get("sklearn_version") != sklearn.__version__:
        raise ValueError("model library version mismatch")
    threshold = metadata.get("threshold")
    if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("invalid threshold")
    if not isinstance(metadata.get("policy_version"),str) or not metadata["policy_version"]:
        raise ValueError("missing policy version")
    parse_event_at(metadata["information_cutoff"])
    expected_path = f"versions/{model_version}/model.joblib"
    if metadata.get("artifact_path") != expected_path:
        raise ValueError("artifact does not belong to the frozen model version")
    artifact = (root / expected_path).resolve()
    if not artifact.is_relative_to(root):
        raise ValueError("artifact escapes the model directory")
    stat = artifact.stat()
    if verify_fresh and _digest_file(artifact) != metadata.get("artifact_sha256"):
        raise ValueError("model artifact checksum mismatch")
    fingerprint = (stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size,stat.st_ino)
    return _load_artifact(str(root),metadata_text,fingerprint), metadata


def model_readiness(model_dir: Path, model_version: str | None = None) -> dict:
    """Readiness requires actual hash, pipeline, and prediction verification."""
    try:
        _, metadata = _get_model(model_dir,model_version,verify_fresh=True)
        return {"status":"READY","reason":None,"metadata":metadata}
    except Exception as exc:
        return {"status":"UNAVAILABLE","reason":f"MODEL_UNAVAILABLE: {type(exc).__name__}","metadata":None}


def _sigmoid(value):
    if value >= 0:
        return 1. / (1. + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1. + exponential)


def _feature_vector(features):
    if not isinstance(features,dict) or set(features) != set(FEATURE_NAMES):
        raise ValueError("recorded features do not match the frozen schema")
    if any(type(features[name]) not in (int,float) or not math.isfinite(features[name]) for name in FEATURE_NAMES):
        raise ValueError("recorded features must be finite numbers")
    return np.asarray([features[name] for name in FEATURE_NAMES],dtype=np.float64)


def _explain(features, model, metadata, expected_score, *, payload=None, calculation_kind="RECORDED", numeric=None):
    vector = _feature_vector(features)
    scale, classifier = model.named_steps["scale"],model.named_steps["classifier"]
    if numeric is None:
        standardized = (vector-scale.mean_)/scale.scale_
        contributions = standardized*classifier.coef_[0]
    else:
        standardized,contributions = numeric
    intercept = float(classifier.intercept_[0])
    logit = intercept+math.fsum(float(x) for x in contributions)
    score = _sigmoid(logit)
    if not math.isfinite(logit) or type(expected_score) not in (int,float) or not math.isfinite(expected_score) or not 0<=expected_score<=1:
        raise ValueError("invalid recorded score or logistic output")
    if not math.isclose(score,expected_score,rel_tol=1e-12,abs_tol=1e-12):
        raise ValueError("recorded score differs from its frozen-model reconstruction")
    raw = feature_raw_values(payload) if payload is not None else {}
    factors=[]
    for index,name in enumerate(FEATURE_NAMES):
        contribution=float(contributions[index])
        effect="INCREASE" if contribution>0 else "DECREASE" if contribution<0 else "NEUTRAL"
        label=FEATURE_DESCRIPTIONS[name]["label"]
        explanation=(f"{label} {'increases' if contribution>0 else 'decreases' if contribution<0 else 'does not change'} "
                     f"the model log-odds relative to its training-mean baseline ({contribution:+.4f}).")
        factors.append({"feature":name,"label":label,"transformation":FEATURE_DESCRIPTIONS[name]["transformation"],
            "raw_value":raw.get(name),"raw_value_status":"RECORDED_INPUT" if name in raw else "NOT_RETAINED",
            "transformed_value":float(vector[index]),"training_mean":float(scale.mean_[index]),
            "training_std":float(math.sqrt(scale.var_[index])),"scaling_divisor":float(scale.scale_[index]),
            "standardized_value":float(standardized[index]),"coefficient":float(classifier.coef_[0,index]),
            "contribution":contribution,"effect":effect,"explanation":explanation})
    return {"status":"AVAILABLE","reason":None,"explanation_version":EXPLANATION_VERSION,
        "calculation_kind":calculation_kind,"model_version":metadata["model_version"],"policy_version":metadata["policy_version"],
        "feature_schema_version":FEATURE_SCHEMA_VERSION,"artifact_sha256":metadata["artifact_sha256"],
        "baseline_log_odds":intercept,"baseline_score":_sigmoid(intercept),"intercept":intercept,
        "logit":logit,"score":float(expected_score),"reconstructed_score":score,
        "threshold":metadata["threshold"],"flagged":bool(expected_score>=metadata["threshold"]),
        "contribution_units":"log_odds","baseline_definition":"Training-split mean of each transformed feature; not an average real customer.",
        "interpretation":"Exact additive explanation of a synthetic-risk model output. Contributions are log-odds, not probability percentages or causal effects. The score is not a real-world fraud probability.",
        "class_meaning":"Generated synthetic high-risk scenario (label 1)","features":factors,
        "top_reasons":sorted(factors,key=lambda item:abs(item["contribution"]),reverse=True)[:3],
        "reconstruction_error":abs(score-expected_score)}


def compact_explanation(explanation):
    """Small immutable math receipt; labels and model parameters stay versioned.

    The UI expands the exact recorded features against the frozen artifact. It
    must not mistake this compact receipt for the full explanatory response.
    """
    if not isinstance(explanation,dict) or explanation.get("status")!="AVAILABLE":
        return {"status":"UNAVAILABLE","reason":"No verified additive explanation available"}
    factors=explanation["features"]
    if [item["feature"] for item in factors]!=list(FEATURE_NAMES):
        raise ValueError("explanation feature order differs")
    contributions={item["feature"]:item["contribution"] for item in factors}
    baseline,logit,score=explanation["baseline_log_odds"],explanation["logit"],explanation["score"]
    numbers=[baseline,logit,score,explanation["threshold"],*contributions.values()]
    if any(type(x) not in (float,int) or not math.isfinite(x) for x in numbers):
        raise ValueError("explanation math must be finite")
    if not math.isclose(baseline+math.fsum(contributions.values()),logit,rel_tol=1e-12,abs_tol=1e-12) or not math.isclose(_sigmoid(logit),score,rel_tol=1e-12,abs_tol=1e-12):
        raise ValueError("explanation math does not reconstruct its score")
    return {"status":"AVAILABLE","schema_version":EXPLANATION_VERSION,
        "model_version":explanation["model_version"],"policy_version":explanation["policy_version"],
        "artifact_sha256":explanation["artifact_sha256"],"baseline_logit":baseline,"logit":logit,
        "threshold":explanation["threshold"],"score":score,"contribution_units":"log_odds",
        "contributions":contributions}


def explain_recorded(features, model_dir, model_version, expected_score, policy_version):
    """Reconstruct historical evidence using ONLY its specific frozen version."""
    try:
        if model_version is None:
            raise ValueError("recorded model version is required")
        model,metadata=_get_model(model_dir,model_version,verify_fresh=True)
        if metadata["policy_version"]!=policy_version:
            raise ValueError("recorded policy does not match the frozen version")
        return _explain(features,model,metadata,expected_score)
    except Exception as exc:
        return {"status":"UNAVAILABLE","reason":f"EXPLANATION_UNAVAILABLE: {type(exc).__name__}",
                "model_version":model_version,"policy_version":policy_version}


def explain_features(features, model_dir, model_version, policy_version, *, payload=None):
    """Read-only hypothetical prediction; never a recorded assessment or edit."""
    try:
        if model_version is None:
            raise ValueError("a frozen model version is required")
        model,metadata=_get_model(model_dir,model_version,verify_fresh=True)
        if metadata["policy_version"]!=policy_version:
            raise ValueError("policy does not match the frozen version")
        vector=_feature_vector(features)
        if payload is not None:
            if build_features(payload)!=features:
                raise ValueError("raw values differ from feature values")
            if parse_event_at(payload["event_at"])<=parse_event_at(metadata["information_cutoff"]):
                raise ValueError("hypothetical movement does not follow the model information cutoff")
        score=float(model.predict_proba(vector.reshape(1,-1))[0,1])
        return _explain(features,model,metadata,score,payload=payload,calculation_kind="HYPOTHETICAL")
    except Exception as exc:
        return {"status":"UNAVAILABLE","reason":f"EXPLANATION_UNAVAILABLE: {type(exc).__name__}",
                "model_version":model_version,"policy_version":policy_version}


def score_batch(payloads: list[dict], model_dir: Path) -> list[dict]:
    """Vectorized inference with exact additive evidence; unknown is never zero."""
    if not payloads:
        return []
    results=[]
    for payload in payloads:
        result={"status":"UNAVAILABLE","score":None,"flagged":False,"model_version":None,"policy_version":None,
                "feature_values":None,"feature_schema_version":FEATURE_SCHEMA_VERSION,"reason":None,"explanation":None}
        try:
            result["feature_values"]=build_features(payload)
        except (ValueError,TypeError,ArithmeticError) as exc:
            result["reason"]=f"FEATURES_UNAVAILABLE: {exc}"
        results.append(result)
    try:
        model,metadata=_get_model(model_dir)
        cutoff=parse_event_at(metadata["information_cutoff"])
    except Exception as exc:
        for result in results:
            if result["reason"] is None:
                result["reason"]=f"MODEL_UNAVAILABLE: {type(exc).__name__}"
        return results
    valid=[]
    for index,(payload,result) in enumerate(zip(payloads,results,strict=True)):
        result.update(model_version=metadata["model_version"],policy_version=metadata["policy_version"])
        if result["reason"] is not None:
            continue
        if parse_event_at(payload["event_at"])<=cutoff:
            result["reason"]="MODEL_UNAVAILABLE: movement does not follow the model information cutoff"
            continue
        valid.append(index)
    if valid:
        matrix=np.asarray([[results[index]["feature_values"][name] for name in FEATURE_NAMES] for index in valid],dtype=np.float64)
        try:
            scores=model.predict_proba(matrix)[:,1]
            scaler=model.named_steps["scale"]
            standardized=(matrix-scaler.mean_)/scaler.scale_
            contributions=standardized*model.named_steps["classifier"].coef_[0]
            if len(scores)!=len(valid) or not np.isfinite(scores).all() or ((scores<0)|(scores>1)).any():
                raise ValueError("invalid model output")
        except Exception as exc:
            for index in valid:results[index]["reason"]=f"INFERENCE_UNAVAILABLE: {type(exc).__name__}"
        else:
            for position,(index,score) in enumerate(zip(valid,scores,strict=True)):
                try:
                    explanation=_explain(results[index]["feature_values"],model,metadata,float(score),payload=payloads[index],
                        calculation_kind="ASSESSMENT",numeric=(standardized[position],contributions[position]))
                    results[index].update(status="SCORED",score=float(score),flagged=bool(score>=metadata["threshold"]),explanation=explanation,
                        reason="MODEL_THRESHOLD_EXCEEDED" if score>=metadata["threshold"] else "BELOW_MODEL_THRESHOLD")
                except (ValueError,ArithmeticError) as exc:
                    results[index]["reason"]=f"EXPLANATION_UNAVAILABLE: {type(exc).__name__}"
    return results
