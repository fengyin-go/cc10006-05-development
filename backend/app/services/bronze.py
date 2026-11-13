"""原始层管理业务规则。

在原有的登记/校验/筛选能力之上，启停同步接入「来源通道归属」控制：

1. 每次启停先经 :class:`IAMService` 一次性判定归属、角色、权限与只读范围；
2. 越权请求抛 PermissionDenied，记「越权拒绝」审计事实，同步状态与下游队列零变化；
3. 授权动作才落状态，并联动清洗表待处理队列（停用挂起、启用恢复）；
4. 所有裁决写只追加审计，操作人按当时身份快照留痕，后续权限变更不改历史；
5. 支持提交/裁决分离的异步队列，覆盖撤权晚于提交、临时升级后回退两种顺序。
"""
from __future__ import annotations

from typing import Any

from app.clock import clock
from app.services.access_audit import (
    FACT_DELEGATE,
    FACT_DENIED,
    FACT_QUEUE_APPLY,
    FACT_SUBMIT,
    FACT_TOGGLE,
    FACT_TRANSFER,
    audit_service,
)
from app.services.iam import PermissionDenied, iam_service
from app.services.silver_pending import silver_pending_service
from app.store import store

MODULE = "bronze"
REQUIRED_FIELDS = ["表编号", "来源通道", "目标路径"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
TOGGLE_ACTIONS = {"启用同步": "已启用", "停用同步": "已停用"}
REQUESTS = "toggle_requests"


class BronzeService:
    # ---------- 列表/明细（带操作者视角的只读裁剪） ----------
    def list_entries(
        self,
        *,
        operator_id: str | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        if operator_id:
            projected = []
            for row in page_rows:
                decision = iam_service.evaluate(int(row["id"]), operator_id)
                projected.append(iam_service.project_bronze(row, decision))
            page_rows = projected
        return page_rows, total

    def get_entry(self, entry_id: int, operator_id: str | None = None) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None or operator_id is None:
            return entry
        decision = iam_service.evaluate(entry_id, operator_id)
        return iam_service.project_bronze(entry, decision)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ---------- 归属/权限视图 ----------
    def permission_view(self, entry_id: int, operator_id: str) -> dict[str, Any]:
        """一次性返回归属、权限与只读范围的完整判定结果。"""
        decision = iam_service.evaluate(entry_id, operator_id)
        entry = store.find(MODULE, entry_id)
        if entry is not None and decision.get("found"):
            decision["view"] = iam_service.project_bronze(entry, decision)
        return decision

    # ---------- 同步启停（主路径） ----------
    def toggle(
        self,
        entry_id: int,
        action: str,
        operator_id: str,
        *,
        source: str = "web",
        request_id: str | None = None,
        submitted_at: str | None = None,
        fact_type: str = FACT_TOGGLE,
        denied_fact_type: str = FACT_DENIED,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """授权后执行启用/停用。返回 (更新后的表, 说明, 审计事实)。

        越权时表状态与下游队列保持不变，仅落一条拒绝事实
        （同步路径记「越权拒绝」，异步裁决路径记「异步裁决/已拒绝」）。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"原始表 {entry_id} 不存在或已归档", {}
        if action not in TOGGLE_ACTIONS:
            return None, f"动作「{action}」不属于原始表启停范围", {}

        decided_at = clock.now_iso()
        op_time = submitted_at or decided_at
        try:
            decision = iam_service.require_toggle(entry_id, operator_id)
        except PermissionDenied as exc:
            fact = audit_service.append(
                denied_fact_type,
                操作类型=action,
                操作对象=entry.get("表编号"),
                原始表ID=entry_id,
                来源通道=entry.get("来源通道"),
                操作人ID=operator_id,
                操作人=(exc.decision.get("user") or {}).get("姓名", operator_id),
                操作来源=source,
                提交时间=op_time,
                裁决时间=decided_at,
                操作时间=op_time,
                裁决结果="已拒绝",
                最终状态=entry.get("status"),
                归属信息=exc.decision.get("ownership"),
                角色视图=exc.decision.get("role_view"),
                拒绝原因=str(exc),
                关联请求=request_id,
            )
            return None, str(exc), fact

        target_status = TOGGLE_ACTIONS[action]
        operator_name = decision["user"]["姓名"]
        before_status = entry.get("status")
        affected: list[dict[str, Any]] = []
        if target_status != before_status:
            entry["status"] = target_status
            entry["pending"] = target_status != "已启用"
            entry["abnormal"] = target_status == "已停用"
            if target_status == "已停用":
                affected = silver_pending_service.suspend_by_bronze(
                    entry, audit_id="pending", operator=operator_name
                )
            else:
                affected = silver_pending_service.resume_by_bronze(
                    entry, audit_id="pending", operator=operator_name
                )

        fact = audit_service.append(
            fact_type,
            操作类型=action,
            操作对象=entry.get("表编号"),
            原始表ID=entry_id,
            来源通道=entry.get("来源通道"),
            操作人ID=operator_id,
            操作人=operator_name,
            操作来源=source,
            提交时间=op_time,
            裁决时间=decided_at,
            操作时间=op_time,
            裁决结果=target_status,
            变更前状态=before_status,
            最终状态=target_status,
            下游影响=[item["清洗表编号"] for item in affected],
            归属信息=decision.get("ownership"),
            委托依据=decision.get("delegation"),
            关联请求=request_id,
        )
        # 待办项回填真实审计编号
        for item in affected:
            if item.get("触发审计") == "pending":
                item["触发审计"] = fact["日志编号"]
            if item.get("恢复触发审计") == "pending":
                item["恢复触发审计"] = fact["日志编号"]
        message = f"原始表 {entry['表编号']} 已{action}"
        if affected:
            message += f"，下游清洗表 {len(affected)} 张已" + (
                "进入待处理队列" if target_status == "已停用" else "恢复并移出待处理队列"
            )
        elif target_status == before_status:
            message += f"（当前已是{target_status}，状态无变化）"
        return entry, message, fact

    # ---------- 异步提交/裁决（撤权晚于提交、临时升级后回退） ----------
    def submit_request(
        self,
        entry_id: int,
        action: str,
        operator_id: str,
        *,
        source: str = "api-queue",
        submitted_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """只受理入队，不做权限裁决；裁决权以实际处理时刻的身份为准。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"原始表 {entry_id} 不存在或已归档"
        if action not in TOGGLE_ACTIONS:
            return None, f"动作「{action}」不属于原始表启停范围"
        submitted = submitted_at or clock.now_iso()
        request = {
            "id": store.next_id(REQUESTS),
            "请求编号": f"REQ-{store.next_id(REQUESTS):04d}",
            "原始表ID": entry_id,
            "表编号": entry.get("表编号"),
            "动作": action,
            "提交人ID": operator_id,
            "操作来源": source,
            "提交时间": submitted,
            "状态": "待裁决",
        }
        store.rows(REQUESTS).append(request)
        user = iam_service.get_user(operator_id)
        audit_service.append(
            FACT_SUBMIT,
            操作类型=action,
            操作对象=entry.get("表编号"),
            原始表ID=entry_id,
            来源通道=entry.get("来源通道"),
            操作人ID=operator_id,
            操作人=user["姓名"] if user else operator_id,
            操作来源=source,
            提交时间=submitted,
            裁决时间=None,
            操作时间=submitted,
            裁决结果="待裁决",
            关联请求=request["请求编号"],
        )
        return request, f"请求 {request['请求编号']} 已受理，等待裁决"

    def decide_request(self, request_id: str) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """按裁决时刻的权限重新判定：撤权后提交的请求会被拒，升级回退后同样按现身份拒绝。"""
        request = store.find_by(REQUESTS, "请求编号", request_id)
        if request is None:
            return None, f"请求 {request_id} 不存在", {}
        if request["状态"] != "待裁决":
            return None, f"请求 {request_id} 已裁决（{request['状态']}），不可重复处理", {}
        entry, message, fact = self.toggle(
            int(request["原始表ID"]),
            str(request["动作"]),
            str(request["提交人ID"]),
            source=str(request["操作来源"]),
            request_id=request_id,
            submitted_at=str(request["提交时间"]),
            fact_type=FACT_QUEUE_APPLY,
            denied_fact_type=FACT_QUEUE_APPLY,
        )
        request["裁决时间"] = clock.now_iso()
        if entry is None:
            request["状态"] = "已拒绝"
            request["裁决说明"] = message
        else:
            request["状态"] = "已执行"
            request["裁决说明"] = message
        return entry, message, fact

    def list_requests(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(REQUESTS)
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return rows

    # ---------- 兼容旧动作入口（提交校验不走归属闸门） ----------
    def run_action(self, entry_id: int, action: str,
                   operator_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        if action in TOGGLE_ACTIONS:
            if not operator_id:
                return None, "启停同步必须携带操作者身份（X-Operator-Id）"
            entry, message, _ = self.toggle(entry_id, action, operator_id)
            return entry, message
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"原始表 {entry_id} 不存在或已归档"
        if action != "提交校验":
            return None, f"动作「{action}」不属于原始层管理可执行范围"
        entry["status"] = "待校验"
        entry["pending"] = True
        return entry, "原始表已提交校验"

    # ---------- 归属变更（转交/委托）并留审计 ----------
    def transfer_ownership(self, channel_code: str, new_owner_id: str,
                           operator_id: str) -> tuple[dict[str, Any] | None, str]:
        channel, message = iam_service.transfer_ownership(
            channel_code, new_owner_id, operator_id
        )
        if channel is None:
            return None, message
        audit_service.append(
            FACT_TRANSFER,
            操作类型="权限转交",
            操作对象=channel_code,
            来源通道=channel_code,
            操作人ID=operator_id,
            操作人=iam_service.get_user(operator_id)["姓名"],
            操作来源="web",
            提交时间=clock.now_iso(),
            裁决时间=clock.now_iso(),
            操作时间=clock.now_iso(),
            裁决结果="归属已变更",
            变更内容=message,
            新负责人ID=new_owner_id,
        )
        return channel, message

    def create_delegation(self, channel_code: str, delegate_id: str,
                          expires_at: str, operator_id: str,
                          reason: str = "") -> tuple[dict[str, Any] | None, str]:
        delegation, message = iam_service.create_delegation(
            channel_code, delegate_id, expires_at, operator_id, reason
        )
        if delegation is None:
            return None, message
        audit_service.append(
            FACT_DELEGATE,
            操作类型="启停权委托",
            操作对象=channel_code,
            来源通道=channel_code,
            操作人ID=operator_id,
            操作人=iam_service.get_user(operator_id)["姓名"],
            操作来源="web",
            提交时间=clock.now_iso(),
            裁决时间=clock.now_iso(),
            操作时间=clock.now_iso(),
            裁决结果="委托生效",
            委托ID=delegation["委托ID"],
            受托人ID=delegate_id,
            到期时间=expires_at,
        )
        return delegation, message

    def explain_state(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"操作对象": str(entry_id), "最终状态": None, "说明": "原始表不存在"}
        return audit_service.explain_state(str(entry["表编号"]))


bronze_service = BronzeService()
