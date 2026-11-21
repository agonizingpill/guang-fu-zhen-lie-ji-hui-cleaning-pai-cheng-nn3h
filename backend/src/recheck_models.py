from datetime import date, datetime
from sqlalchemy import String, Float, ForeignKey, JSON, CheckConstraint, Date, DateTime, Integer, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class RecheckOrder(Base):
    """补测单：一张需要补测的巡测读数，按约定工况等待补测车名额。"""
    __tablename__ = 'recheck_orders'
    __table_args__ = (CheckConstraint('estimated_loss>=0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey('soiling_surveys.id'))
    block_id: Mapped[int] = mapped_column(ForeignKey('array_blocks.id'))
    condition_key: Mapped[str] = mapped_column(String(80))
    condition_snapshot: Mapped[dict] = mapped_column(JSON)
    agreed_condition: Mapped[dict] = mapped_column(JSON)
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    estimated_loss: Mapped[float] = mapped_column(Float, default=0)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default='pending')
    # 一条巡测同时在办只允许一个补测单；结清后置空，释放自然键。
    active_survey_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RecheckBatch(Base):
    """一次补测车出车批次，容量即名额数。"""
    __tablename__ = 'recheck_batches'
    __table_args__ = (CheckConstraint('capacity>0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    scheduled_date: Mapped[date] = mapped_column(Date)
    capacity: Mapped[int] = mapped_column()
    state: Mapped[str] = mapped_column(String(24), default='planned')
    evidence: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(120), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RecheckBooking(Base):
    """排单占用：一条巡测只占一个名额。"""
    __tablename__ = 'recheck_bookings'
    __table_args__ = (CheckConstraint('slot_no>0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('recheck_batches.id'))
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'))
    slot_no: Mapped[int] = mapped_column()
    agreed_condition: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default='booked')
    # 活跃名额的唯一自然键：并发排单下同一巡测不可能占两个名额。
    # 结清或存档后置空；MySQL 唯一索引允许多个 NULL。
    active_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    active_survey_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RecheckReceipt(Base):
    """补测回执：回执号幂等，绑定具体排单，旧回执不能结后来重约的单。"""
    __tablename__ = 'recheck_receipts'
    __table_args__ = (CheckConstraint('irradiance>0 AND observed_kw>=0 AND dust>=0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_no: Mapped[str] = mapped_column(String(60), unique=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey('recheck_bookings.id'))
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'))
    irradiance: Mapped[float] = mapped_column(Float)
    module_temp: Mapped[float] = mapped_column(Float)
    dust: Mapped[float] = mapped_column(Float)
    rain_days: Mapped[int] = mapped_column()
    observed_kw: Mapped[float] = mapped_column(Float)
    condition_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    deviation_note: Mapped[str] = mapped_column(String(300), default='')
    measured_condition: Mapped[dict] = mapped_column(JSON)
    reference_survey_id: Mapped[int | None] = mapped_column(ForeignKey('soiling_surveys.id'), nullable=True)
    new_survey_id: Mapped[int | None] = mapped_column(ForeignKey('soiling_surveys.id'), nullable=True)
    state: Mapped[str] = mapped_column(String(24))
    submitted_by: Mapped[str] = mapped_column(String(120), default='')
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RecheckOutcome(Base):
    """已核实的补测结果：误判风险与按阵列漏判率的训练样本。"""
    __tablename__ = 'recheck_outcomes'
    __table_args__ = (CheckConstraint('estimated_loss>=0 AND actual_loss>=0'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey('recheck_orders.id'), nullable=True)
    block_id: Mapped[int] = mapped_column(ForeignKey('array_blocks.id'))
    condition_key: Mapped[str] = mapped_column(String(80))
    estimated_loss: Mapped[float] = mapped_column(Float)
    actual_loss: Mapped[float] = mapped_column(Float)
    irradiance: Mapped[float] = mapped_column(Float)
    module_temp: Mapped[float] = mapped_column(Float)
    dust: Mapped[float] = mapped_column(Float)
    rain_days: Mapped[int] = mapped_column()
    slope: Mapped[float] = mapped_column(Float)
    capacity_kw: Mapped[float] = mapped_column(Float)
    misjudged: Mapped[bool] = mapped_column(Boolean, default=False)
    missed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(24), default='receipt')
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str] = mapped_column(String(120), default='')
    verified_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
