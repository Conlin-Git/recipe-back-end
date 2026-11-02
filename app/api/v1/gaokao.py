from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.agents.gaokao_planner import plan_gaokao
from app.gaokao.regions import CANDIDATE_REGIONS
from app.models.gaokao import GaokaoAdmission, GaokaoCutoff, GaokaoScoreBand
from app.models.user import User
from app.schemas.gaokao import GaokaoCatalogInput, GaokaoCatalogOut, GaokaoGuideInput, GaokaoGuideOut
from app.services.gaokao_service import SUPPORTED_SCHOOL_PROVINCES, catalog, coverage_snapshot, eligible_admissions

router = APIRouter(prefix="/gaokao", tags=["gaokao"])


@router.post("/guide", response_model=GaokaoGuideOut)
async def get_guide(
    data: GaokaoGuideInput,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    return await plan_gaokao(db, data)


@router.post("/catalog", response_model=GaokaoCatalogOut)
async def get_catalog(
    data: GaokaoCatalogInput,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    return await catalog(db, data)


@router.get("/catalog", response_model=GaokaoCatalogOut)
async def get_catalog_default(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """给直接打开接口地址的场景提供默认本科目录，避免出现裸 404。"""
    del current_user
    return await catalog(db, GaokaoCatalogInput())


@router.get("/coverage")
async def get_coverage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    return await coverage_snapshot(db)


@router.get("/options")
async def get_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    # 批次线/位次表在录取线导入前也应能驱动指南筛选器。
    provinces = CANDIDATE_REGIONS
    async def distinct_options(model, *fields, admissions=False):
        query=select(model.province, *fields).where(model.province.in_(provinces)).distinct()
        if admissions: query=query.where(eligible_admissions())
        return (await db.execute(query)).all()
    cuts=await distinct_options(GaokaoCutoff,GaokaoCutoff.year,GaokaoCutoff.track,GaokaoCutoff.batch)
    bands=await distinct_options(GaokaoScoreBand,GaokaoScoreBand.year,GaokaoScoreBand.track)
    admissions=await distinct_options(GaokaoAdmission,GaokaoAdmission.year,GaokaoAdmission.track,GaokaoAdmission.batch,admissions=True)
    province_tracks={p:set() for p in provinces}
    province_batches={p:set() for p in provinces}
    province_reference_batches={}
    for p,y,t,b in [*cuts,*admissions]:
        province_tracks[p].add(t)
        province_batches[p].add(b)
    for p,y,t,b in admissions:
        province_reference_batches.setdefault(p,{}).setdefault(str(y),set()).add(b)
    for p,y,t in bands: province_tracks[p].add(t)
    tracks=set().union(*province_tracks.values())
    admission_years={r[1] for r in admissions}
    score_band_years={r[1] for r in bands}
    years=admission_years | score_band_years | {r[1] for r in cuts}
    return {
        "provinces": list(provinces),
        "years": sorted(years, reverse=True),
        "reference_years": sorted(admission_years, reverse=True),
        "score_band_years": sorted(score_band_years, reverse=True),
        "tracks": sorted(tracks),
        "province_tracks": {key: sorted(value) for key, value in province_tracks.items()},
        "province_batches": {key: sorted(value) for key, value in province_batches.items()},
        "province_reference_batches": {
            province: {year: sorted(batches) for year, batches in years_map.items()}
            for province, years_map in province_reference_batches.items()
        },
        "school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        "coverage": await coverage_snapshot(db),
    }
