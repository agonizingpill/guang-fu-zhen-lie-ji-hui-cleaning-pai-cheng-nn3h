from datetime import datetime,timedelta
import random
from sqlalchemy import select,func
from src.database import Base,engine,Session
from src.models import User,ArrayBlock,SoilingSurvey,WashMission
from src.revisit_models import WaterDelivery,FollowupSurvey
from src.auth import hash_password
from src.operations_models import WashCrew
from src.recheck_models import RecheckOrder,RecheckBatch,RecheckOutcome
from src.recheck_risk import condition_key,condition_envelope,classify_outcome

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ['admin','planner','viewer']:
            email=role+'@qingling.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=hash_password('Solar2026!'),role=role))
        for code,minutes in [('CREW-SOUTH',480),('CREW-RIDGE',360)]:
            if not db.scalar(select(WashCrew).where(WashCrew.code==code)):db.add(WashCrew(code=code,max_daily_minutes=minutes))
        blocks=[('SL-A01','南坡一号','南坡',150,23,780,1500),('SL-A02','南坡二号','南坡',120,25,630,1200),('SL-B01','脊线一号','脊线',100,18,520,900),('SL-C01','西坡一号','西坡',80,30,420,600)]
        for code,name,sector,cap,slope,area,water in blocks:
            if not db.scalar(select(ArrayBlock).where(ArrayBlock.code==code)):db.add(ArrayBlock(code=code,name=name,sector=sector,capacity_kw=cap,slope=slope,area_m2=area,water_liters=water))
        db.flush();rows=db.scalars(select(ArrayBlock).order_by(ArrayBlock.id)).all()
        measurements=[('SV-1117-A01',rows[0],800,35,18,3.2,88),('SV-1117-A02',rows[1],820,33,12,2.1,76),('SV-CLOUD-01',rows[2],240,23,8,1.2,18)]
        for code,block,irr,temp,rain,dust,power in measurements:
            if not db.scalar(select(SoilingSurvey).where(SoilingSurvey.code==code)):db.add(SoilingSurvey(code=code,block_id=block.id,capacity_kw=block.capacity_kw,slope=block.slope,irradiance=irr,module_temp=temp,rain_days=rain,dust=dust,observed_kw=power,state='recorded'))
        db.flush()
        block=db.scalar(select(ArrayBlock).where(ArrayBlock.code=='SL-C01'))
        if not db.scalar(select(WaterDelivery).where(WaterDelivery.delivery_code=='WD-DEMO-01')):
            db.add(WaterDelivery(block_id=block.id,delivery_code='WD-DEMO-01',liters=500,source='西坡蓄水池 / 水车02'))
        historical=db.scalar(select(SoilingSurvey).where(SoilingSurvey.code=='SV-DEMO-CLEAN'))
        if not historical:
            historical=SoilingSurvey(code='SV-DEMO-CLEAN',block_id=block.id,capacity_kw=block.capacity_kw,slope=block.slope,irradiance=800,module_temp=35,rain_days=15,dust=2,observed_kw=50,clean_kw=62,predicted_loss=12,recommended_water=100,state='closed')
            db.add(historical);db.flush()
        mission=db.scalar(select(WashMission).where(WashMission.survey_id==historical.id))
        if not mission:
            mission=WashMission(survey_id=historical.id,water_liters=100,available_liters=700,status='accepted',post_kw=62,post_irradiance=800,normalized_kw=62,verified_gain=12,dispatched_at=datetime.utcnow()-timedelta(days=7),accepted_at=datetime.utcnow()-timedelta(days=7))
            db.add(mission);db.flush()
        if not db.scalar(select(FollowupSurvey).where(FollowupSurvey.mission_id==mission.id,FollowupSurvey.days_after==2)):
            db.add(FollowupSurvey(mission_id=mission.id,days_after=2,irradiance=800,observed_kw=61,normalized_kw=61,normalized_loss=100/62,state='stable',baseline_snapshot={'post_kw':62,'post_irradiance':800,'accepted_at':mission.accepted_at.isoformat(),'block_id':block.id,'block_code':block.code,'survey_id':historical.id,'survey_code':historical.code,'threshold_pct':5}))
        _seed_recheck(db,rows)
        db.commit();print({key:db.scalar(select(func.count()).select_from(model)) for key,model in [('users',User),('blocks',ArrayBlock),('surveys',SoilingSurvey)]})


def _seed_recheck(db,rows):
    """补测扩展只补缺项：已核实误判/漏判样本、少见工况读数与一批待排名额。"""
    by_code={b.code:b for b in rows}
    block_a01=by_code.get('SL-A01') or rows[0]
    block_a02=by_code.get('SL-A02') or rows[min(1,len(rows)-1)]
    block_b01=by_code.get('SL-B01') or rows[min(2,len(rows)-1)]
    block_c01=by_code.get('SL-C01') or rows[min(3,len(rows)-1)]
    if not db.scalar(select(RecheckOutcome).where(RecheckOutcome.source=='seed')):
        rng=random.Random(20261121)
        # 固定 seed 生成 24 条已核实误判/漏判样本，覆盖四个阵列与常规工况，供风险模型起步。
        # 样本温度均在 18–39°C、辐照 420–880，故「高温」工况全部缺失，留给少见工况补测。
        plans=[(block_a01,800,35,3.2,18,2),(block_a02,820,33,2.1,12,1),(block_b01,600,26,1.4,9,1),(block_c01,480,22,1.0,6,1)]
        for index in range(24):
            block,irr,temp,dust,rain,miss_bias=plans[index%4]
            jitter=lambda base,span:round(base+rng.uniform(-span,span),2)
            e_irr=int(jitter(irr,60));e_temp=round(jitter(temp,4),1);e_dust=round(max(0.1,jitter(dust,0.8)),1);e_rain=max(0,int(jitter(rain,3)))
            estimated=round(max(0.5,jitter(block.capacity_kw*0.11,2.5)),3)
            # 三分之一偏高估（误判）、三分之一偏低（漏判）、其余真值，形成可学习分布。
            bias=miss_bias if index%3==2 else 0
            actual=round(max(0.0,estimated*(0.25 if index%3==0 else (1.9+bias if bias else 1.0))+rng.uniform(-0.4,0.4)),3)
            misjudged,missed=classify_outcome(estimated,actual)
            db.add(RecheckOutcome(block_id=block.id,condition_key=condition_key(e_irr,e_temp),estimated_loss=estimated,actual_loss=actual,
                irradiance=e_irr,module_temp=e_temp,dust=e_dust,rain_days=e_rain,slope=block.slope,capacity_kw=block.capacity_kw,
                misjudged=misjudged,missed=missed,source='seed',verified=True,verified_by='seed@qingling.test'))
        db.flush()
    # 待排补测读数：名额少于待办，才能看出「风险 + 少见工况」的取舍。两条常规 + 两条少见高温。
    candidates=[
        ('SV-RC-HOT-01',block_b01,760,52,20,4.4,54,'少见高温'),
        ('SV-RC-HOT-02',block_a01,720,48,16,3.6,96,'少见高温'),
        ('SV-RC-01',block_a01,790,34,14,2.8,86,'常规'),
        ('SV-RC-02',block_a02,830,31,10,1.9,78,'常规'),
        ('SV-RC-03',block_c01,470,24,8,1.2,52,'常规'),
    ]
    for code,block,irr,temp,rain,dust,power,note in candidates:
        survey=db.scalar(select(SoilingSurvey).where(SoilingSurvey.code==code))
        if not survey:
            survey=SoilingSurvey(code=code,block_id=block.id,capacity_kw=block.capacity_kw,slope=block.slope,irradiance=irr,module_temp=temp,rain_days=rain,dust=dust,observed_kw=power,state='recorded')
            db.add(survey);db.flush()
        rc=code.replace('SV-','RC-',1)
        if not db.scalar(select(RecheckOrder).where(RecheckOrder.code==rc)):
            db.add(RecheckOrder(code=rc,survey_id=survey.id,block_id=survey.block_id,condition_key=condition_key(survey.irradiance,survey.module_temp),
                condition_snapshot={'irradiance':survey.irradiance,'module_temp':survey.module_temp},
                agreed_condition=condition_envelope(survey.irradiance,survey.module_temp),
                source_snapshot={'survey_code':survey.code,'block_code':block.code,'capacity_kw':survey.capacity_kw,'slope':survey.slope,'irradiance':survey.irradiance,'module_temp':survey.module_temp,'dust':survey.dust,'rain_days':survey.rain_days,'observed_kw':survey.observed_kw,'area_m2':block.area_m2,'state_at_creation':survey.state},
                estimated_loss=round(survey.capacity_kw*0.12,3),state='pending',active_survey_id=survey.id))
    if not db.scalar(select(RecheckBatch).where(RecheckBatch.code=='RB-DEMO-01')):
        db.add(RecheckBatch(code='RB-DEMO-01',scheduled_date=datetime.utcnow().date()+timedelta(days=1),capacity=3,state='planned',evidence={'status':'seed'},created_by='seed@qingling.test'))
if __name__=='__main__':seed()
