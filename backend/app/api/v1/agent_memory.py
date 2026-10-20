"""What the agents have remembered about the signed-in member, and the
controls to forget it. Memory rows are keyed by the member's id as text."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.models.db import User

router = APIRouter(prefix="/memory", tags=["memory"])

# kind -> fixed statements. Nothing from the request is ever interpolated
# into SQL; the request only picks a key of these dicts.
_LIST = {
    "learnings": text(
        "SELECT id, agent_type, learning AS text, success_rate, sample_count, created_at "
        "FROM agent_memory_learnings WHERE user_id = :uid ORDER BY created_at DESC LIMIT 100"
    ),
    "preferences": text(
        "SELECT id, preference_key AS label, preference_value AS text, created_at "
        "FROM agent_memory_preferences WHERE user_id = :uid ORDER BY created_at DESC LIMIT 100"
    ),
    "procedures": text(
        "SELECT id, agent_type, trigger_desc AS text, success_count, last_used_at, created_at "
        "FROM agent_memory_procedures WHERE user_id = :uid ORDER BY created_at DESC LIMIT 100"
    ),
    "episodes": text(
        "SELECT id, agent_type, task_type, strategy, success, context_summary, "
        "output_summary AS text, created_at "
        "FROM agent_memory_episodes WHERE user_id = :uid ORDER BY created_at DESC LIMIT 100"
    ),
}
_DELETE_ONE = {
    "learnings": text("DELETE FROM agent_memory_learnings WHERE user_id = :uid AND id = :id"),
    "preferences": text("DELETE FROM agent_memory_preferences WHERE user_id = :uid AND id = :id"),
    "procedures": text("DELETE FROM agent_memory_procedures WHERE user_id = :uid AND id = :id"),
    "episodes": text("DELETE FROM agent_memory_episodes WHERE user_id = :uid AND id = :id"),
}


@router.get("")
async def list_memory(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = str(current_user.id)
    out: dict[str, list[dict]] = {}
    for kind, statement in _LIST.items():
        rows = await db.execute(statement, {"uid": uid})
        out[kind] = [dict(row) for row in rows.mappings().all()]
    return out


@router.delete("")
async def forget_everything(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.memory_lifecycle import delete_memory_cache, delete_memory_rows

    owner = (
        await db.execute(
            select(User)
            .where(User.id == current_user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status_code=404, detail="Account not found")
    owner.memory_generation += 1
    await delete_memory_rows(db, owner.id)
    # Fail the request on cache failure; a retry repeats the whole operation.
    await delete_memory_cache(owner.id)
    await db.commit()
    return {"deleted": "all"}


@router.delete("/{kind}/{item_id}")
async def forget_one(
    kind: str,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statement = _DELETE_ONE.get(kind)
    if statement is None:
        raise HTTPException(status_code=404, detail="Unknown memory type")
    result = await db.execute(statement, {"uid": str(current_user.id), "id": item_id})
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": item_id}
