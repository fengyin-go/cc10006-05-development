"""原始层管理接口：维护原始表，覆盖提交校验、启用同步、停用同步等动作。

启停接口按来源通道归属鉴权：
- 请求头 ``X-Operator-Id`` 标识操作者，``X-Operator-Source`` 标识操作来源；
- 越权请求返回 403 并携带归属信息，同步进程与下游清洗队列不发生任何变化；
- 所有裁决（含拒绝）进入只追加审计台账，可按操作来源追溯责任。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bronze import bronze_service
from app.services.iam import iam_service

router = APIRouter(prefix="/api/bronze", tags=["原始层管理"])

service = bronze_service

LIST_FIELDS = ["表编号", "来源通道", "目标路径", "保留策略", "分区规则", "文件格式", "压缩类型", "数据校验"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]
TOGGLE_ACTIONS = {"启用同步", "停用同步"}


def _require_operator(operator_id: str | None) -> str:
    if not operator_id:
        raise HTTPException(status_code=401, detail="缺少操作者身份头 X-Operator-Id")
    if iam_service.get_user(operator_id) is None:
        raise HTTPException(status_code=401, detail=f"操作者 {operator_id} 不存在或已注销")
    return operator_id


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
) -> PageResult[dict]:
    """按表编号与状态过滤原始表；携带操作者身份时按其只读范围裁剪字段。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        operator_id=x_operator_id, keyword=keyword, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/requests/list")
def list_requests(status: str | None = None) -> dict[str, Any]:
    """查看异步裁决请求队列。"""
    return {"items": service.list_requests(status=status)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(
    entry_id: int,
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
) -> dict:
    """读取单条原始表明细；携带身份时返回该角色可见范围的视图。"""
    entry = service.get_entry(entry_id, operator_id=x_operator_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"原始表 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/permission", response_model=dict)
def get_permission(
    entry_id: int,
    x_operator_id: str = Header(alias="X-Operator-Id"),
) -> dict[str, Any]:
    """一次性返回来源通道归属、启停权限与只读范围判定结果。"""
    _require_operator(x_operator_id)
    decision = service.permission_view(entry_id, x_operator_id)
    if not decision.get("found"):
        raise HTTPException(status_code=404, detail=decision.get("reason"))
    return decision


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条原始表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="原始表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
    x_operator_source: str = Header(default="web", alias="X-Operator-Source"),
) -> ActionResult:
    """提交校验、启用同步、停用同步。

    启停动作强制鉴权：越权返回 403 + 归属信息，且不改变同步状态或下游待办。
    """
    action = str(payload.values.get("action") or "").strip()
    if action in TOGGLE_ACTIONS:
        operator_id = _require_operator(x_operator_id)
        entry, message, fact = service.toggle(
            entry_id, action, operator_id, source=x_operator_source
        )
        if entry is None:
            # 越权拒绝：把完整归属信息随 403 返回，便于前端解释原因
            decision = service.permission_view(entry_id, operator_id)
            raise HTTPException(
                status_code=403,
                detail={"message": message, "ownership": decision.get("ownership"),
                        "role_view": decision.get("role_view"),
                        "read_scope": decision.get("read_scope"),
                        "audit": fact.get("日志编号") if fact else None},
            )
        return ActionResult(ok=True, message=message, entry=entry)

    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/requests", response_model=ActionResult)
def submit_request(
    entry_id: int,
    payload: EntryPayload,
    x_operator_id: str = Header(alias="X-Operator-Id"),
    x_operator_source: str = Header(default="api-queue", alias="X-Operator-Source"),
) -> ActionResult:
    """异步受理启停请求（提交与裁决分离），用于撤权晚于提交等顺序场景。"""
    operator_id = _require_operator(x_operator_id)
    action = str(payload.values.get("action") or "").strip()
    submitted_at = payload.values.get("submitted_at")
    request, message = service.submit_request(
        entry_id, action, operator_id,
        source=x_operator_source, submitted_at=submitted_at,
    )
    if request is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=request)


@router.post("/requests/{request_id}/decide", response_model=ActionResult)
def decide_request(
    request_id: str,
    x_operator_id: str = Header(alias="X-Operator-Id"),
) -> ActionResult:
    """按裁决时刻的身份处理待裁决请求；裁决人身份仅用于留痕，权限看提交人现身份。"""
    _require_operator(x_operator_id)
    entry, message, _ = service.decide_request(request_id)
    if entry is None and "不存在" in message:
        raise HTTPException(status_code=404, detail=message)
    if entry is None:
        raise HTTPException(status_code=403, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/state-explanation")
def explain_state(entry_id: int) -> dict[str, Any]:
    """按操作时间重放审计事件，解释该原始表最终同步状态的形成过程。"""
    return service.explain_state(entry_id)


@router.get("/export")
def export_entries(
    x_operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
) -> dict[str, Any]:
    """导出原始层清单：携带身份时同样按只读范围脱敏。"""
    items, total = service.list_entries(operator_id=x_operator_id, page=1, size=10000)
    return {"module": "bronze", "total": total, "items": items}
