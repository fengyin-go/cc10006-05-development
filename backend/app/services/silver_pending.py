"""清洗表待处理队列：随上游原始表的停用/启用同步变化。

- 原始表停用：依赖它且处于「已启用」的清洗表进入待处理清单，记录入队前状态；
- 原始表启用：清单中由停用动作挂起的清洗表恢复到入队前状态并出队；
- 待处理是清洗业务面的待办，原始表未授权的启停请求绝不会触发本队列变化。
"""
from __future__ import annotations

from typing import Any

from app.clock import clock
from app.store import store

SILVER = "silver"
QUEUE = "silver_pending"
PENDING_STATUS = "待处理"


class SilverPendingService:
    def _silver_rows(self, bronze_code: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(SILVER)
            if row.get("上游原始表") == bronze_code
        ]

    def suspend_by_bronze(self, bronze: dict[str, Any], audit_id: str,
                          operator: str) -> list[dict[str, Any]]:
        """原始表停用后，挂起依赖它的清洗表并写入待处理清单。"""
        bronze_code = str(bronze.get("表编号"))
        queued: list[dict[str, Any]] = []
        existing = {item["清洗表编号"] for item in store.rows(QUEUE)}
        for silver in self._silver_rows(bronze_code):
            if silver.get("status") == PENDING_STATUS or silver["表编号"] in existing:
                continue
            previous = str(silver.get("status"))
            silver["status"] = PENDING_STATUS
            silver["pending"] = True
            silver["abnormal"] = True
            item = {
                "id": store.next_id(QUEUE),
                "清洗表编号": silver["表编号"],
                "上游原始表": bronze_code,
                "来源通道": bronze.get("来源通道"),
                "入队前状态": previous,
                "待处理原因": f"上游原始表 {bronze_code} 已停用同步",
                "触发审计": audit_id,
                "触发操作人": operator,
                "入队时间": clock.now_iso(),
            }
            store.rows(QUEUE).append(item)
            queued.append(item)
        return queued

    def resume_by_bronze(self, bronze: dict[str, Any], audit_id: str,
                         operator: str) -> list[dict[str, Any]]:
        """原始表恢复同步后，把挂起的清洗表恢复到入队前状态并出队。"""
        bronze_code = str(bronze.get("表编号"))
        resumed: list[dict[str, Any]] = []
        queue = store.rows(QUEUE)
        for item in list(queue):
            if item.get("上游原始表") != bronze_code:
                continue
            silver = store.find_by(SILVER, "表编号", item["清洗表编号"])
            if silver is not None:
                silver["status"] = item.get("入队前状态", "已启用")
                silver["pending"] = silver["status"] != "已启用"
                silver["abnormal"] = False
            item["出队时间"] = clock.now_iso()
            item["恢复后状态"] = silver["status"] if silver else None
            item["恢复触发审计"] = audit_id
            item["恢复操作人"] = operator
            resumed.append(item)
            queue.remove(item)
        return resumed

    def list_queue(self, bronze_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(QUEUE)
        if bronze_code:
            rows = [row for row in rows if row.get("上游原始表") == bronze_code]
        return rows


silver_pending_service = SilverPendingService()
