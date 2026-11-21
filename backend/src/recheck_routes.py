from fastapi import APIRouter, Depends
from sqlalchemy import select, func

from .database import session
from .auth import current, editor, admin
from .recheck_models import (RecheckOrder, RecheckBatch, RecheckBooking,
                             RecheckReceipt, RecheckOutcome)
from .recheck_schemas import (RecheckOrderInput, RecheckBatchInput,
                              RecheckScheduleInput, RecheckReceiptInput)
from .models import ArrayBlock
from . import recheck_service

router = APIRouter()


def _dump(obj, **extra):
    """显式取列值，避免把 SQLAlchemy 实例状态带进 JSON。"""
    if obj is None:
        return None
    data = {col.name: getattr(obj, col.name) for col in obj.__table__.columns}
    data.update(extra)
    return data


def _block_codes(db):
    return {row.id: row.code for row in db.scalars(select(ArrayBlock)).all()}


@router.get('/rechecks/orders')
def orders(u=Depends(current), db=Depends(session)):
    rows = db.scalars(select(RecheckOrder).order_by(RecheckOrder.id.desc())).all()
    codes = _block_codes(db)
    return [_dump(row, block_code=codes.get(row.block_id)) for row in rows]


@router.post('/rechecks/orders')
def create_order(b: RecheckOrderInput, u=Depends(editor), db=Depends(session)):
    return _dump(recheck_service.create_order(db, b, u))


@router.get('/rechecks/batches')
def batches(u=Depends(current), db=Depends(session)):
    rows = db.scalars(select(RecheckBatch).order_by(RecheckBatch.id.desc())).all()
    usage = dict(db.execute(
        select(RecheckBooking.batch_id, func.count()).where(
            RecheckBooking.state == 'booked').group_by(RecheckBooking.batch_id)).all())
    return [_dump(row, booked=usage.get(row.id, 0), free=row.capacity - usage.get(row.id, 0)) for row in rows]


@router.post('/rechecks/batches')
def create_batch(b: RecheckBatchInput, u=Depends(admin), db=Depends(session)):
    return _dump(recheck_service.create_batch(db, b, u))


@router.post('/rechecks/batches/{key}/schedule')
def schedule(key: int, b: RecheckScheduleInput, u=Depends(admin), db=Depends(session)):
    result = recheck_service.schedule(db, key, b, u)
    return {
        'batch': _dump(result['batch']),
        'bookings': [_dump(row) for row in result['bookings']],
        'evidence': result['evidence'],
    }


@router.get('/rechecks/bookings')
def bookings(u=Depends(current), db=Depends(session)):
    rows = db.scalars(select(RecheckBooking).order_by(RecheckBooking.id.desc())).all()
    return [_dump(row) for row in rows]


@router.get('/rechecks/receipts')
def receipts(u=Depends(current), db=Depends(session)):
    rows = db.scalars(select(RecheckReceipt).order_by(RecheckReceipt.id.desc())).all()
    return [_dump(row) for row in rows]


@router.get('/rechecks/orders/{key}/rationale')
def rationale(key: int, u=Depends(current), db=Depends(session)):
    result = recheck_service.rationale(db, key)
    return {
        'order': _dump(result['order']),
        'booking': _dump(result['booking']),
        'receipts': [_dump(row) for row in result['receipts']],
        'array_stats': result['array_stats'],
        'condition_outcomes': result['condition_outcomes'],
        'model_evidence': result['model_evidence'],
    }


@router.post('/rechecks/orders/{key}/receipts')
def receipt(key: int, b: RecheckReceiptInput, u=Depends(editor), db=Depends(session)):
    return _dump(recheck_service.submit_receipt(db, key, b, u))


@router.get('/rechecks/risk')
def risk(u=Depends(current), db=Depends(session)):
    return recheck_service.risk_overview(db)


@router.get('/rechecks/outcomes')
def outcomes(u=Depends(current), db=Depends(session)):
    rows = db.scalars(select(RecheckOutcome).order_by(RecheckOutcome.id.desc())).all()
    return [_dump(row) for row in rows]


@router.post('/rechecks/outcomes/{key}/verify')
def verify(key: int, u=Depends(admin), db=Depends(session)):
    return _dump(recheck_service.verify_outcome(db, key, u))
