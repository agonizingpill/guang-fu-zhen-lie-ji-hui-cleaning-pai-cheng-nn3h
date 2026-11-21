from pydantic import Field, field_validator
from .revisit_schemas import Inputs


class RecheckOrderInput(Inputs):
    survey_id: int
    code: str = Field(min_length=3, max_length=60)
    # 约定工况允许现场按同工况补测；留空则按原巡测条件自动生成包络。
    irradiance_tolerance_pct: float = Field(default=15, ge=0, le=50)
    module_temp_tolerance: float = Field(default=6, ge=0, le=25)

    @field_validator('code')
    @classmethod
    def trim(cls, v):
        if not v.strip():
            raise ValueError('补测单号不能为空')
        return v.strip()


class RecheckBatchInput(Inputs):
    code: str = Field(min_length=3, max_length=60)
    scheduled_date: str = Field(min_length=8, max_length=10)
    capacity: int = Field(ge=1, le=200)

    @field_validator('code')
    @classmethod
    def trim(cls, v):
        if not v.strip():
            raise ValueError('批次号不能为空')
        return v.strip()


class RecheckScheduleInput(Inputs):
    # 可指定候选巡测；留空表示对全部待办单自动排单。
    survey_ids: list[int] = Field(default_factory=list, max_length=200)


class RecheckReceiptInput(Inputs):
    receipt_no: str = Field(min_length=3, max_length=60)
    block_id: int
    irradiance: float = Field(ge=350, le=1400)
    module_temp: float = Field(ge=-20, le=85)
    dust: float = Field(ge=0, le=10)
    rain_days: int = Field(ge=0, le=180)
    observed_kw: float = Field(ge=0, le=10000)
    deviation_note: str = Field(default='', max_length=300)

    @field_validator('receipt_no')
    @classmethod
    def trim(cls, v):
        if not v.strip():
            raise ValueError('回执号不能为空')
        return v.strip()
