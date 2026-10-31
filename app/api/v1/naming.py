from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.naming import NamingInput, NamingRunOut
from app.services import naming_service

router = APIRouter(prefix="/naming", tags=["naming"])


@router.post("/analyze", response_model=NamingRunOut)
async def analyze_naming(data: NamingInput, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        record = await naming_service.run(db, current_user.id, data.model_dump(), "analyze")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return naming_service.to_output(record)


@router.post("/score", response_model=NamingRunOut)
async def score_naming(data: NamingInput, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not data.name:
        raise HTTPException(status_code=422, detail="评分模式必须提供 name")
    try:
        record = await naming_service.run(db, current_user.id, data.model_dump(), "score")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return naming_service.to_output(record)


@router.get("/runs/{run_id}", response_model=NamingRunOut)
async def get_naming_run(run_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    record = await naming_service.get_mine(db, current_user.id, run_id)
    if not record:
        raise HTTPException(status_code=404, detail="命名记录不存在")
    return naming_service.to_output(record)
