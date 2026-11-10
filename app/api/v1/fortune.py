from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.fortune import FortuneInput, FortuneInterpretInput
from app.fortune.service import analyze, FortuneUnavailable
from app.fortune.interpretation import interpret

router = APIRouter(prefix="/fortune", tags=["fortune"])


@router.post("/analyze")
async def analyze_fortune(data: FortuneInput, current_user: User = Depends(get_current_user)):
    try:
        return await analyze(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FortuneUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/interpret")
async def interpret_fortune(data: FortuneInterpretInput, current_user: User = Depends(get_current_user)):
    # Recompute trusted facts; the client cannot replace chart data in the AI prompt.
    try:
        result = await analyze(data.birth)
        return await interpret(result, data.question)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FortuneUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
