from datetime import datetime, date

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from .models import ArrayBlock, SoilingSurvey, WashMission
from .recheck_models import (RecheckOrder, RecheckBatch, RecheckBooking,
                             RecheckReceipt, RecheckOutcome)
from .field_service import require, audit
from . import recheck_risk
from .power_model import train_reference


ACTIVE_ORDER_STATES = ('pending', 'scheduled')


def _survey_loss(survey, block):
    """补测单的估损基线：优先用已评估损失，否则按目测/容量给保守估计。"""
    if survey.predicted_loss is not None:
        return round(float(survey.predicted_loss), 3)
    ref_clean = _clean_reference(survey.capacity_kw, survey.irradiance, survey.module_temp, survey.slope)
    if ref_clean is not None:
        return round(max(0.0, ref_clean - float(survey.observed_kw)), 3)
    return round(float(block.capacity_kw) * float(survey.dust) / 100.0, 3)


def _clean_reference(capacity_kw, irradiance, module_temp, slope):
    try:
        model, _ = train_reference()
        value = float(model.predict([[capacity_kw, irradiance, module_temp, slope]])[0])
        return max(0.0, value)
    except Exception:
        return None


def create_order(db, body, user):
    survey = require(db, SoilingSurvey, body.survey_id, True)
    block = require(db, ArrayBlock, survey.block_id, True)
    if survey.state not in ('recorded', 'blocked'):
        raise HTTPException(409, '仅待评估或需重试的巡测可申请补测')
    if block.cleaning_state == 'washing':
        raise HTTPException(409, '阵列清洗执行中，不能排补测')
    if db.scalar(select(WashMission).where(WashMission.survey_id == survey.id)):
        raise HTTPException(409, '该巡测已有清洗任务，无需补测')
    if db.scalar(select(RecheckOrder).where(RecheckOrder.code == body.code)):
        raise HTTPException(409, '补测单号已存在')
    if db.scalar(select(RecheckOrder).where(RecheckOrder.active_survey_id == survey.id)):
        raise HTTPException(409, '该巡测已有在办补测单')
    if db.scalar(select(RecheckOrder).where(RecheckOrder.survey_id == survey.id, RecheckOrder.state == 'closed')):
        raise HTTPException(409, '该巡测已完成补测并结清，无需再次补测')
    snapshot = {
        'survey_code': survey.code,
        'block_code': block.code,
        'capacity_kw': survey.capacity_kw,
        'slope': survey.slope,
        'irradiance': survey.irradiance,
        'module_temp': survey.module_temp,
        'dust': survey.dust,
        'rain_days': survey.rain_days,
        'observed_kw': survey.observed_kw,
        'area_m2': block.area_m2,
        'state_at_creation': survey.state,
    }
    envelope = recheck_risk.condition_envelope(
        survey.irradiance, survey.module_temp,
        body.irradiance_tolerance_pct, body.module_temp_tolerance)
    order = RecheckOrder(
        code=body.code,
        survey_id=survey.id,
        block_id=block.id,
        condition_key=recheck_risk.condition_key(survey.irradiance, survey.module_temp),
        condition_snapshot={'irradiance': survey.irradiance, 'module_temp': survey.module_temp},
        agreed_condition=envelope,
        source_snapshot=snapshot,
        estimated_loss=_survey_loss(survey, block),
        state='pending',
        active_survey_id=survey.id,
    )
    db.add(order)
    audit(db, user, f'登记补测单 {body.code} 巡测{survey.code} 估损{order.estimated_loss}kW')
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该巡测已有在办补测单，请勿重复登记')
    return order


def create_batch(db, body, user):
    if db.scalar(select(RecheckBatch).where(RecheckBatch.code == body.code)):
        raise HTTPException(409, '批次号已存在')
    try:
        scheduled = date.fromisoformat(body.scheduled_date)
    except ValueError:
        raise HTTPException(422, '出车日期格式须为 YYYY-MM-DD')
    _, evidence, _, _ = recheck_risk.train(db)
    batch = RecheckBatch(
        code=body.code,
        scheduled_date=scheduled,
        capacity=body.capacity,
        state='planned',
        evidence={k: v for k, v in evidence.items() if k != 'feature_importance'},
        created_by=user.email,
    )
    db.add(batch)
    audit(db, user, f'登记补测批次 {body.code} 名额{body.capacity}')
    db.commit()
    return batch


def _select(scored, capacity):
    """兼顾风险、工况覆盖与按阵列漏判率的选单算法。返回 [(item, reason)]。"""
    if capacity <= 0:
        return []
    picks, chosen, conditions = [], set(), set()

    def take(item, reason):
        if item['order'].id in chosen or len(picks) >= capacity:
            return False
        picks.append((item, reason))
        chosen.add(item['order'].id)
        conditions.add(item['condition_key'])
        return True

    # 1) 先给最高误判风险单一个名额，保证风险底线。
    if scored:
        take(scored[0], '最高误判风险')

    # 2) 少见工况覆盖：每类少见工况留一个名额（最多半个批次），避免只挑同一类工况。
    rare_budget = max(1, capacity // 2)
    rare_used = 0
    rare_seen = set()
    for item in scored:
        if rare_used >= rare_budget:
            break
        if not item['rare_condition'] or item['condition_key'] in rare_seen:
            continue
        rare_seen.add(item['condition_key'])
        if item['condition_key'] in conditions:
            continue
        if take(item, '少见工况保留名额'):
            rare_used += 1

    # 3) 按阵列漏判率预留：高漏判阵列在风险排序中可能被埋没，单独补位。
    leak_seen = set()
    for item in sorted(scored, key=lambda x: (x['array_missed_rate'], x['risk']), reverse=True):
        if item['array_missed_rate'] < recheck_risk.LEAK_THRESHOLD or item['array_samples'] <= 0:
            continue
        block_id = item['order'].block_id
        if block_id in leak_seen:
            continue
        leak_seen.add(block_id)
        if not any(p[0]['order'].block_id == block_id for p in picks):
            take(item, '补齐阵列漏判率')

    # 4) 余下名额：优先未覆盖工况，再按风险从高到低。
    for item in sorted(scored, key=lambda x: (x['condition_key'] in conditions, -x['risk'])):
        if len(picks) >= capacity:
            break
        take(item, '风险排序补位')
    return picks


def schedule(db, batch_id, body, user):
    batch = require(db, RecheckBatch, batch_id, True)
    if batch.state != 'planned':
        raise HTTPException(409, '该批次已排定或已结束，不能重复排单')
    booked = db.scalar(select(func.count()).select_from(RecheckBooking).where(
        RecheckBooking.batch_id == batch.id, RecheckBooking.state == 'booked'))
    free = batch.capacity - int(booked or 0)
    if free <= 0:
        raise HTTPException(409, '该批次补测名额已用尽')
    query = select(RecheckOrder).where(RecheckOrder.state == 'pending')
    if body.survey_ids:
        query = query.where(RecheckOrder.survey_id.in_(body.survey_ids))
    orders = db.scalars(query.order_by(RecheckOrder.id)).all()
    if not orders:
        raise HTTPException(409, '没有可排的待办补测单')
    scored, evidence, arrays, conds = recheck_risk.score_orders(db, orders)
    picks = _select(scored, free)
    if not picks:
        raise HTTPException(409, '没有可排的待办补测单')
    # 名额位次按批次历史最大位次递增，避免回执释放后复用同一 slot。
    last_slot = db.scalar(select(func.max(RecheckBooking.slot_no)).where(
        RecheckBooking.batch_id == batch.id)) or 0
    created = []
    for offset, (item, reason) in enumerate(picks, start=1):
        order = item['order']
        slot_no = int(last_slot) + offset
        record = RecheckBooking(
            batch_id=batch.id,
            order_id=order.id,
            slot_no=slot_no,
            agreed_condition=order.agreed_condition,
            rationale={
                'reason': reason,
                'risk': item['risk'],
                'model_probability': item['model_probability'],
                'array_missed_rate': item['array_missed_rate'],
                'array_samples': item['array_samples'],
                'condition_key': item['condition_key'],
                'condition_outcomes': item['condition_outcomes'],
                'rare_condition': item['rare_condition'],
                'model_status': evidence.get('status'),
                'model_holdout_accuracy': evidence.get('holdout_accuracy'),
                'model_holdout_auc': evidence.get('holdout_auc'),
            },
            state='booked',
            active_order_id=order.id,
            active_survey_id=order.survey_id,
        )
        order.state = 'scheduled'
        db.add(record)
        created.append(record)
    audit(db, user, f'补测批次{batch.code}排单{len(created)}单，兼顾风险与工况覆盖')
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '并发排单冲突：该巡测已被其他批次占用，请刷新后重试')
    return {'batch': batch, 'bookings': created, 'evidence': evidence}


def _active_booking(db, order):
    return db.scalar(select(RecheckBooking).where(
        RecheckBooking.order_id == order.id, RecheckBooking.state == 'booked'))


def submit_receipt(db, order_id, body, user):
    order = require(db, RecheckOrder, order_id, True)
    block = require(db, ArrayBlock, body.block_id, True)
    existing = db.scalar(select(RecheckReceipt).where(RecheckReceipt.receipt_no == body.receipt_no))
    booking = _active_booking(db, order)

    # 回执重试：同号回执只认一次，不重复核销；但旧回执不能结掉后来重约的单。
    if existing:
        if existing.order_id != order.id:
            raise HTTPException(409, '该回执号已用于其他补测单，不能结掉当前单')
        if booking is None:
            # 无活跃名额：若本单已由该回执结清或存档，则幂等返回；否则视为旧回执。
            if existing.state in ('settled', 'archived'):
                return existing
            raise HTTPException(409, '该补测单已不在补测名额中，旧回执不能重复核销')
        if existing.booking_id == booking.id:
            return existing
        raise HTTPException(409, '该回执属于旧排单，不能结掉后来重约的补测单')

    if order.state != 'scheduled' or not booking:
        raise HTTPException(409, '该补测单尚未排入名额，不能提交回执')
    if body.observed_kw > float(block.capacity_kw) * 1.2:
        raise HTTPException(409, '补测功率超过该阵列额定容量120%，请核对电表口径')

    agreed = booking.agreed_condition or order.agreed_condition or {}
    condition_ok = recheck_risk.within_envelope(agreed, body.irradiance, body.module_temp)
    same_array = block.id == order.block_id
    measured = {
        'irradiance': body.irradiance,
        'module_temp': body.module_temp,
        'dust': body.dust,
        'rain_days': body.rain_days,
        'observed_kw': body.observed_kw,
        'condition_key': recheck_risk.condition_key(body.irradiance, body.module_temp),
        'block_id': block.id,
        'block_code': block.code,
    }
    receipt = RecheckReceipt(
        receipt_no=body.receipt_no,
        booking_id=booking.id,
        order_id=order.id,
        irradiance=body.irradiance,
        module_temp=body.module_temp,
        dust=body.dust,
        rain_days=body.rain_days,
        observed_kw=body.observed_kw,
        condition_ok=condition_ok,
        deviation_note=body.deviation_note.strip(),
        measured_condition=measured,
        state='submitted',
        submitted_by=user.email,
    )

    if not condition_ok:
        # 实测偏离约定工况：回执存档，原单退回待补，释放名额。
        receipt.state = 'archived'
        booking.state = 'released'
        booking.active_order_id = None
        booking.active_survey_id = None
        booking.closed_at = datetime.utcnow()
        order.state = 'pending'
        db.add(receipt)
        audit(db, user, f'补测回执{body.receipt_no}偏离约定工况，存档退回待补')
        db.commit()
        return receipt

    if not same_array:
        # 别的阵列同工况读数可作参考，但不结清本单；条件也不吻合则不收。
        if measured['condition_key'] != order.condition_key:
            raise HTTPException(409, '参考读数须与本单同工况；当前读数条件不符，未记录')
        receipt.state = 'reference'
        db.add(receipt)
        audit(db, user, f'补测回执{body.receipt_no}为{block.code}同工况参考读数，不结清{order.code}')
        db.commit()
        return receipt

    # 原阵列、工况达标：结清补测单，写入已核实结果供风险学习。
    if block.cleaning_state == 'washing':
        raise HTTPException(409, '阵列正在清洗，补测读数请在复测页登记')
    survey_code = f'SV-{body.receipt_no}'
    if db.scalar(select(SoilingSurvey).where(SoilingSurvey.code == survey_code)):
        raise HTTPException(409, '该回执已生成过补测巡测，请勿重复提交')
    fresh = SoilingSurvey(
        code=survey_code,
        block_id=block.id,
        capacity_kw=block.capacity_kw,
        slope=block.slope,
        irradiance=body.irradiance,
        module_temp=body.module_temp,
        rain_days=body.rain_days,
        dust=body.dust,
        observed_kw=body.observed_kw,
        state='recorded',
    )
    db.add(fresh)
    db.flush()
    clean = _clean_reference(order.source_snapshot.get('capacity_kw') or 0,
                             body.irradiance, body.module_temp,
                             order.source_snapshot.get('slope') or 0)
    actual_loss = round(max(0.0, (clean if clean is not None else 0.0) - float(body.observed_kw)), 3)
    misjudged, missed = recheck_risk.classify_outcome(order.estimated_loss, actual_loss)
    outcome = RecheckOutcome(
        order_id=order.id,
        block_id=order.block_id,
        # 以补测单约定工况分组，使少见工况计数与排单查询口径一致；实测条件另存在回执中。
        condition_key=order.condition_key,
        estimated_loss=order.estimated_loss,
        actual_loss=actual_loss,
        irradiance=body.irradiance,
        module_temp=body.module_temp,
        dust=body.dust,
        rain_days=body.rain_days,
        slope=order.source_snapshot.get('slope') or 0,
        capacity_kw=order.source_snapshot.get('capacity_kw') or 0,
        misjudged=misjudged,
        missed=missed,
        source='receipt',
        verified=False,
    )
    receipt.new_survey_id = fresh.id
    receipt.state = 'settled'
    booking.state = 'completed'
    booking.active_order_id = None
    booking.active_survey_id = None
    booking.closed_at = datetime.utcnow()
    order.state = 'closed'
    order.closed_at = datetime.utcnow()
    order.active_survey_id = None
    order.risk_score = None
    db.add(receipt)
    db.add(outcome)
    audit(db, user, f'补测回执{body.receipt_no}结清{order.code}，实际损失{actual_loss}kW'
                    f'{"（误判）" if misjudged else ""}{"（漏判）" if missed else ""}')
    db.commit()
    return receipt


def verify_outcome(db, outcome_id, user):
    """主管核实补测结果，方可用于风险学习与漏判率统计。"""
    outcome = require(db, RecheckOutcome, outcome_id, True)
    if outcome.verified:
        raise HTTPException(409, '该补测结果已核实，不可重复核验')
    outcome.verified = True
    outcome.verified_by = user.email
    outcome.verified_at = datetime.utcnow()
    audit(db, user, f'核实补测结果{outcome.id} 实际损失{outcome.actual_loss}kW')
    db.commit()
    return outcome


def rationale(db, order_id):
    order = require(db, RecheckOrder, order_id)
    booking = db.scalar(select(RecheckBooking).where(
        RecheckBooking.order_id == order.id).order_by(RecheckBooking.id.desc()))
    receipts = db.scalars(select(RecheckReceipt).where(
        RecheckReceipt.order_id == order.id).order_by(RecheckReceipt.id.desc())).all()
    _, evidence, arrays, conds = recheck_risk.train(db)
    return {
        'order': order,
        'booking': booking,
        'receipts': receipts,
        'array_stats': arrays.get(order.block_id),
        'condition_outcomes': conds.get(order.condition_key, 0),
        'model_evidence': evidence,
    }


def risk_overview(db):
    orders = db.scalars(select(RecheckOrder).where(
        RecheckOrder.state.in_(ACTIVE_ORDER_STATES)).order_by(RecheckOrder.id)).all()
    scored, evidence, arrays, conds = recheck_risk.score_orders(db, orders)
    rows = db.scalars(select(RecheckOutcome).order_by(RecheckOutcome.id.desc())).all()
    verified_rows = [row for row in rows if row.verified]
    return {
        'model_evidence': evidence,
        'array_stats': list(arrays.values()),
        'condition_counts': conds,
        'scored': [{
            'order_id': item['order'].id,
            'code': item['order'].code,
            'block_id': item['order'].block_id,
            'condition_key': item['condition_key'],
            'estimated_loss': item['order'].estimated_loss,
            'risk': item['risk'],
            'model_probability': item['model_probability'],
            'array_missed_rate': item['array_missed_rate'],
            'rare_condition': item['rare_condition'],
            'state': item['order'].state,
        } for item in scored],
        'outcome_count': len(verified_rows),
        'pending_outcome_count': len(rows) - len(verified_rows),
        'outcomes': [{
            'id': row.id,
            'block_id': row.block_id,
            'condition_key': row.condition_key,
            'estimated_loss': row.estimated_loss,
            'actual_loss': row.actual_loss,
            'misjudged': row.misjudged,
            'missed': row.missed,
            'verified': row.verified,
            'verified_at': row.verified_at,
        } for row in rows[:50]],
    }
