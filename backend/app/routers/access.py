"""来源通道归属与启停权限接口。

集中暴露：
- 用户/通道/委托查询；
- 负责人转交、请假委托（到期自动收回）；
- 清洗表待处理队列；
- 启停审计台账（可按操作来源、操作对象、操作人查询并解释最终状态）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.access_audit import audit_service
from app.services.bronze import bronze_service
from app.services.iam import iam_service
from app.services.silver_pending import silver_pending_service
from app.store import store

router = APIRouter(prefix="/api/access", tags=["来源通道归属与权限"])


def _require_operator(operator_id: str | None) -> str:
    if not operator_id:
        raise HTTPException(status_code=401, detail="缺少操作者身份头 X-Operator-Id")
    if iam_service.get_user(operator_id) is None:
        raise HTTPException(status_code=401, detail=f"操作者 {operator_id} 不存在或已注销")
    return operator_id


@router.get("/users")
def list_users() -> dict[str, Any]:
    """平台用户清单（前端切换操作者用）。"""
    return {"items": iam_service.list_users()}


@router.get("/channels")
def list_channels(
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
) -> dict[str, Any]:
    """来源通道清单；携带身份时连接参数按角色脱敏。"""
    bronze_rows = store.rows("bronze")
    items = []
    for channel in iam_service.list_channels():
        view = dict(channel)
        can_view_secret = False
        if x_operator_id and iam_service.get_user(x_operator_id):
            bronze = next(
                (row for row in bronze_rows
                 if row.get("来源通道") == channel["来源通道"]),
                None,
            )
            if bronze is not None:
                decision = iam_service.evaluate(int(bronze["id"]), x_operator_id)
                can_view_secret = decision.get("read_scope") == "full"
        view["连接参数"] = iam_service.mask_connection(channel.get("连接参数"), can_view_secret)
        view["生效委托"] = iam_service.active_delegation(channel["来源通道"])
        items.append(view)
    return {"items": items}


@router.get("/delegations")
def list_delegations(channel: str | None = None) -> dict[str, Any]:
    """委托记录（含已到期与生效中）。"""
    return {"items": iam_service.list_delegations(channel)}


@router.post("/channels/{channel_code}/transfer", response_model=ActionResult)
def transfer_ownership(
    channel_code: str,
    payload: EntryPayload,
    x_operator_id: str = Header(alias="X-Operator-Id"),
) -> ActionResult:
    """负责人永久转交启停权；仅现任负责人可操作，历史审计仍归属原负责人。"""
    operator_id = _require_operator(x_operator_id)
    new_owner_id = str(payload.values.get("new_owner_id") or "").strip()
    if not new_owner_id:
        return ActionResult(ok=False, message="缺少 new_owner_id")
    channel, message = bronze_service.transfer_ownership(
        channel_code, new_owner_id, operator_id
    )
    if channel is None:
        raise HTTPException(status_code=403, detail=message)
    return ActionResult(ok=True, message=message, entry=channel)


@router.post("/channels/{channel_code}/delegations", response_model=ActionResult)
def create_delegation(
    channel_code: str,
    payload: EntryPayload,
    x_operator_id: str = Header(alias="X-Operator-Id"),
) -> ActionResult:
    """负责人请假委托启停权，到期自动收回。"""
    operator_id = _require_operator(x_operator_id)
    delegate_id = str(payload.values.get("delegate_id") or "").strip()
    expires_at = str(payload.values.get("expires_at") or "").strip()
    reason = str(payload.values.get("reason") or "")
    if not delegate_id or not expires_at:
        return ActionResult(ok=False, message="缺少 delegate_id 或 expires_at")
    delegation, message = bronze_service.create_delegation(
        channel_code, delegate_id, expires_at, operator_id, reason
    )
    if delegation is None:
        raise HTTPException(status_code=403, detail=message)
    return ActionResult(ok=True, message=message, entry=delegation)


@router.get("/silver-pending")
def silver_pending(bronze_code: str | None = None) -> dict[str, Any]:
    """清洗业务面待处理清单：随原始表停用入队、启用恢复出队。"""
    return {"items": silver_pending_service.list_queue(bronze_code)}


@router.get("/audit-facts")
def audit_facts(
    source: str | None = Query(default=None, description="按操作来源查询责任记录"),
    object_id: str | None = Query(default=None, description="按操作对象（表编号）查询"),
    fact_type: str | None = Query(default=None, description="按事实类型查询"),
    operator_id: str | None = Query(default=None, description="按操作人ID查询"),
) -> dict[str, Any]:
    """启停审计台账查询：只追加事实，支持按操作来源追溯责任。"""
    return {"items": audit_service.list_facts(
        source=source, object_id=object_id, fact_type=fact_type, operator_id=operator_id
    )}


@router.get("/audit-facts/explain/{object_id}")
def explain(object_id: str) -> dict[str, Any]:
    """按操作时间重放事件，还原某张原始表的最终同步状态。"""
    return audit_service.explain_state(object_id)
