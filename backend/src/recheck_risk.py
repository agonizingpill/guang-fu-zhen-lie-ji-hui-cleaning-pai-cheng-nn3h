"""补测误判风险模型与工况覆盖统计。

训练样本只取「已核实」的补测结果（RecheckOutcome）。未通过原阵列按约实测的
回执（参考读数、工况偏离）不会写入结果表，因此不会污染风险学习。
"""
import hashlib
import json
import math

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

from sqlalchemy import select

from .recheck_models import RecheckOutcome

FEATURES = ['estimated_loss', 'irradiance', 'module_temp', 'dust', 'rain_days', 'slope', 'capacity_kw']
RARE_MIN_OUTCOMES = 3      # 少于该样本数的工况视为少见工况
LEAK_THRESHOLD = 0.35      # 阵列漏判率达到该阈值时为其保留名额
HOLDOUT_RATIO = 0.25
RANDOM_STATE = 42

_IRR_BANDS = [(350, 500, '低照'), (500, 700, '中照'), (700, 900, '高照'), (900, 1050, '满照'), (1050, 1400, '超照')]
_TEMP_BANDS = [(-20, 28, '低温'), (28, 40, '常温'), (40, 65, '高温'), (65, 85, '极温')]


def _band(value, bands):
    for low, high, label in bands:
        if low <= value < high:
            return label
    return bands[-1][2] if value >= bands[-1][1] else bands[0][2]


def condition_key(irradiance, module_temp):
    """把辐射与温度归入一个可比较的工况格，用于覆盖统计与名额预留。"""
    return f'{_band(float(irradiance), _IRR_BANDS)}·{_band(float(module_temp), _TEMP_BANDS)}'


def condition_envelope(irradiance, module_temp, irr_tolerance_pct=15, temp_tolerance=6):
    irr = float(irradiance)
    low = max(350.0, irr * (1 - irr_tolerance_pct / 100))
    high = min(1400.0, irr * (1 + irr_tolerance_pct / 100))
    return {
        'irradiance_min': round(low, 1),
        'irradiance_max': round(high, 1),
        'module_temp_min': round(float(module_temp) - temp_tolerance, 1),
        'module_temp_max': round(float(module_temp) + temp_tolerance, 1),
        'irradiance_tolerance_pct': irr_tolerance_pct,
        'module_temp_tolerance': temp_tolerance,
    }


def within_envelope(envelope, irradiance, module_temp):
    if not envelope:
        return True
    return (envelope.get('irradiance_min', 0) <= irradiance <= envelope.get('irradiance_max', 1e9)
            and envelope.get('module_temp_min', -1e9) <= module_temp <= envelope.get('module_temp_max', 1e9))


def classify_outcome(estimated_loss, actual_loss):
    """给出已核实结果的两个标签：误判（高估）与漏判（低估）。"""
    estimated_loss = max(0.0, float(estimated_loss))
    actual_loss = max(0.0, float(actual_loss))
    misjudged = estimated_loss >= 1.0 and actual_loss <= max(0.5, estimated_loss * 0.3)
    missed = actual_loss >= estimated_loss * 1.5 + 0.5
    return bool(misjudged), bool(missed)


def _outcome_features(row):
    return [float(getattr(row, name)) for name in FEATURES]


def array_stats(rows):
    """按阵列统计漏判率（报漏率），用 Laplace 平滑避免小样本极端值。"""
    buckets = {}
    for row in rows:
        item = buckets.setdefault(row.block_id, {'block_id': row.block_id, 'n': 0, 'missed': 0, 'misjudged': 0})
        item['n'] += 1
        item['missed'] += 1 if row.missed else 0
        item['misjudged'] += 1 if row.misjudged else 0
    result = {}
    for block_id, item in buckets.items():
        n = item['n']
        item['missed_rate'] = round((item['missed'] + 1) / (n + 2), 4)
        item['misjudge_rate'] = round((item['misjudged'] + 1) / (n + 2), 4)
        result[block_id] = item
    return result


def condition_counts(rows):
    counts = {}
    for row in rows:
        counts[row.condition_key] = counts.get(row.condition_key, 0) + 1
    return counts


def load_rows(db):
    """仅使用已核实结果，避免未核补测污染风险学习。"""
    return db.scalars(
        select(RecheckOutcome).where(RecheckOutcome.verified.is_(True)).order_by(RecheckOutcome.id)
    ).all()


def train(db):
    """在已核实结果上训练误判分类器。返回 (model, evidence, arrays, cond_counts)。"""
    rows = load_rows(db)
    arrays = array_stats(rows)
    conds = condition_counts(rows)
    payload = [{name: float(getattr(row, name)) for name in FEATURES} | {'misjudged': row.misjudged, 'missed': row.missed} for row in rows]
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    evidence = {
        'samples': len(rows),
        'features': FEATURES,
        'rare_condition_min': RARE_MIN_OUTCOMES,
        'leak_threshold': LEAK_THRESHOLD,
        'random_state': RANDOM_STATE,
        'sha256': sha,
        'method': 'RandomForestClassifier',
    }
    if len(rows) < 10:
        evidence['status'] = 'insufficient_samples'
        evidence['model'] = None
        return None, evidence, arrays, conds
    x = np.array([_outcome_features(row) for row in rows], dtype=float)
    y = np.array([1 if (row.misjudged or row.missed) else 0 for row in rows], dtype=int)
    classes, counts = np.unique(y, return_counts=True)
    if len(classes) < 2:
        evidence['status'] = 'single_class'
        evidence['model'] = None
        return None, evidence, arrays, conds
    # 小样本或某类过少时不做分层，避免 train_test_split 抛错。
    stratify = y if counts.min() >= 2 and len(rows) >= 8 else None
    train_x, test_x, train_y, test_y = train_test_split(
        x, y, test_size=HOLDOUT_RATIO, random_state=RANDOM_STATE, stratify=stratify)
    model = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=2,
                                   random_state=RANDOM_STATE, n_jobs=1)
    model.fit(train_x, train_y)
    pred = model.predict(test_x)
    evidence.update({
        'status': 'trained',
        'train_rows': int(len(train_x)),
        'holdout_rows': int(len(test_x)),
        'holdout_accuracy': round(float(accuracy_score(test_y, pred)), 4),
        'feature_importance': dict(zip(FEATURES, model.feature_importances_.tolist())),
    })
    try:
        proba = model.predict_proba(test_x)[:, 1]
        evidence['holdout_auc'] = round(float(roc_auc_score(test_y, proba)), 4)
    except ValueError:
        evidence['holdout_auc'] = None
    evidence['positive_rate'] = round(float(y.mean()), 4)
    return model, evidence, arrays, conds


def heuristic_probability(order):
    snapshot = order.source_snapshot or {}
    cap = max(1.0, float(snapshot.get('capacity_kw') or 100))
    est = max(0.0, float(order.estimated_loss or 0))
    dust = float(snapshot.get('dust') or 0)
    rain = float(snapshot.get('rain_days') or 0)
    value = 0.4 * min(1.0, dust / 10.0) + 0.3 * min(1.0, rain / 60.0) + 0.3 * min(1.0, est / (cap * 0.1))
    return round(min(1.0, max(0.0, value)), 4)


def score_orders(db, orders):
    """为待办补测单打分：模型概率与所在阵列漏判率混合。"""
    model, evidence, arrays, conds = train(db)
    scored = []
    for order in orders:
        snapshot = order.source_snapshot or {}
        block_id = order.block_id
        stats = arrays.get(block_id, {'n': 0, 'missed_rate': 0.5, 'missed': 0})
        leak = stats['missed_rate']
        if model is not None:
            features = np.array([[
                float(order.estimated_loss or 0),
                float(snapshot.get('irradiance') or 0),
                float(snapshot.get('module_temp') or 0),
                float(snapshot.get('dust') or 0),
                float(snapshot.get('rain_days') or 0),
                float(snapshot.get('slope') or 0),
                float(snapshot.get('capacity_kw') or 0),
            ]], dtype=float)
            prob = float(model.predict_proba(features)[0][1])
            risk = round(0.65 * prob + 0.35 * leak, 4)
        else:
            prob = heuristic_probability(order)
            risk = round(0.5 * prob + 0.5 * leak, 4)
        scored.append({
            'order': order,
            'risk': risk,
            'model_probability': round(prob, 4),
            'array_missed_rate': leak,
            'array_samples': stats['n'],
            'condition_key': order.condition_key,
            'condition_outcomes': conds.get(order.condition_key, 0),
            'rare_condition': conds.get(order.condition_key, 0) < RARE_MIN_OUTCOMES,
        })
    scored.sort(key=lambda item: item['risk'], reverse=True)
    return scored, evidence, arrays, conds
