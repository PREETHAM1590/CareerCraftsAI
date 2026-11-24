"""The member's own agent results."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.models.db import User
from app.services.agent_metrics import agent_metrics

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/agent")
async def agent_results(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    return await agent_metrics(db, current_user.id)


@router.get("/what-worked")
async def what_worked(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Which resume variants, sources and statuses produced interviews — the
    outcome feedback the tailoring and outreach prompts learn from."""
    from app.services.what_worked import what_worked as compute

    return await compute(db, current_user.id)
