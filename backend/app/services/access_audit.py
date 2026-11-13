"""启停审计台账：只追加、不可变。

每条事实同时记录 *提交时间* 与 *裁决时间*、操作人快照与操作来源；
权限转交/委托后历史事实仍归属当时人员，因为操作人姓名与 ID 在写入时即固化。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "toggle_audit"

# 审计事实类型
FACT_TOGGLE = "启停裁决"           # 同步处理的启用/停用
FACT_DENIED = "越权拒绝"           # 未授权请求被拒绝（不改变任何状态）
FACT_SUBMIT = "请求已受理"         # 异步请求进入待裁决队列
FACT_QUEUE_APPLY = "异步裁决"      # 待裁决请求被处理
FACT_TRANSFER = "归属转交"
FACT_DELEGATE = "委托授权"


class AuditService:
    def append(self, fact_type: str, **fields: Any) -> dict[str, Any]:
        rows = store.rows(MODULE)
        next_id = store.next_id(MODULE)
        fact = {"id": next_id, "日志编号": f"AUTH-{next_id:04d}", "事实类型": fact_type}
        # 调用方若误传空编号，以服务端生成的只追加编号为准，保证审计编号永不丢失
        fields.pop("日志编号", None)
        fact.update(fields)
        rows.append(fact)
        return fact

    def list_facts(
        self,
        *,
        source: str | None = None,
        object_id: str | None = None,
        fact_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(MODULE))
        if source:
            rows = [row for row in rows if row.get("操作来源") == source]
        if object_id:
            rows = [row for row in rows if row.get("操作对象") == object_id]
        if fact_type:
            rows = [row for row in rows if row.get("事实类型") == fact_type]
        if operator_id:
            rows = [row for row in rows if row.get("操作人ID") == operator_id]
        # 默认按裁决时间、其次提交时间排序，保证时间线可解释
        return sorted(
            rows,
            key=lambda row: (str(row.get("裁决时间") or row.get("提交时间") or ""),
                             int(row.get("id", 0))),
        )

    def events_of(self, object_id: str) -> list[dict[str, Any]]:
        """重放某张原始表的启停事件，用于解释最终状态是如何一步步形成的。"""
        rows = [
            row for row in store.rows(MODULE)
            if row.get("操作对象") == object_id
            and row.get("事实类型") in {FACT_TOGGLE, FACT_QUEUE_APPLY}
            and row.get("裁决结果") in {"已启用", "已停用"}
        ]
        return sorted(rows, key=lambda row: (str(row.get("操作时间")), int(row.get("id", 0))))

    def explain_state(self, object_id: str) -> dict[str, Any]:
        """按操作时间重放事件：最后一条生效事件决定最终状态，全部事实仍保留。"""
        events = self.events_of(object_id)
        if not events:
            return {"操作对象": object_id, "最终状态": None, "依据事件": [],
                    "说明": "该表暂无生效的启停事件"}
        last = events[-1]
        return {
            "操作对象": object_id,
            "最终状态": last["裁决结果"],
            "依据事件编号": last["日志编号"],
            "依据操作时间": last["操作时间"],
            "依据操作人": last.get("操作人"),
            "事件总数": len(events),
            "说明": "按操作时间升序重放，最后一条生效事件决定最终状态；"
                    "两条相互冲突的请求都会保留为独立审计事实",
            "事件链": [
                {"日志编号": item["日志编号"], "操作时间": item["操作时间"],
                 "裁决结果": item["裁决结果"], "操作人": item.get("操作人"),
                 "操作来源": item.get("操作来源")}
                for item in events
            ],
        }


audit_service = AuditService()
