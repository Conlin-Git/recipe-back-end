from fastapi import APIRouter, Depends
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.gaokao import GaokaoAdmission, GaokaoCutoff, GaokaoScoreBand
from app.models.user import User
from app.schemas.gaokao import GaokaoGuideInput, GaokaoGuideOut
from app.services.gaokao_service import guide

router = APIRouter(prefix="/gaokao", tags=["gaokao"])


@router.post("/guide", response_model=GaokaoGuideOut)
async def get_guide(
    data: GaokaoGuideInput,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    return await guide(db, data)


@router.get("/options")
async def get_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    del current_user
    # 批次线/位次表在录取线导入前也应能驱动指南筛选器。
    provinces = set((await db.execute(select(distinct(GaokaoCutoff.province)))).scalars().all())
    provinces.update((await db.execute(select(distinct(GaokaoScoreBand.province)))).scalars().all())
    provinces.update((await db.execute(select(distinct(GaokaoAdmission.province)))).scalars().all())
    cutoff_years = set((await db.execute(select(distinct(GaokaoCutoff.year)))).scalars().all())
    score_band_years = set((await db.execute(select(distinct(GaokaoScoreBand.year)))).scalars().all())
    verified_admissions = GaokaoAdmission.verification_status == "verified"
    admission_years = set((await db.execute(select(distinct(GaokaoAdmission.year)).where(verified_admissions))).scalars().all())
    years = cutoff_years | score_band_years | admission_years
    tracks = set((await db.execute(select(distinct(GaokaoCutoff.track)))).scalars().all())
    tracks.update((await db.execute(select(distinct(GaokaoScoreBand.track)))).scalars().all())
    tracks.update((await db.execute(select(distinct(GaokaoAdmission.track)))).scalars().all())
    province_tracks: dict[str, set[str]] = {province: set() for province in provinces}
    for province, track in (await db.execute(select(GaokaoCutoff.province, GaokaoCutoff.track).distinct())).all():
        province_tracks.setdefault(province, set()).add(track)
    for province, track in (await db.execute(select(GaokaoScoreBand.province, GaokaoScoreBand.track).distinct())).all():
        province_tracks.setdefault(province, set()).add(track)
    for province, track in (await db.execute(select(GaokaoAdmission.province, GaokaoAdmission.track).distinct())).all():
        province_tracks.setdefault(province, set()).add(track)
    province_batches: dict[str, set[str]] = {province: set() for province in provinces}
    for province, batch in (await db.execute(select(GaokaoCutoff.province, GaokaoCutoff.batch).distinct())).all():
        province_batches.setdefault(province, set()).add(batch)
    for province, batch in (await db.execute(select(GaokaoAdmission.province, GaokaoAdmission.batch).distinct())).all():
        province_batches.setdefault(province, set()).add(batch)
    province_reference_batches: dict[str, dict[str, set[str]]] = {}
    for province, year, batch in (
        await db.execute(
            select(GaokaoAdmission.province, GaokaoAdmission.year, GaokaoAdmission.batch)
            .where(verified_admissions)
            .distinct()
        )
    ).all():
        province_reference_batches.setdefault(province, {}).setdefault(str(year), set()).add(batch)
    return {
        "provinces": sorted(provinces),
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
        "school_provinces": ["四川", "广东", "上海"],
    }
