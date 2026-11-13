"""原始表启停归属控制的完整场景测试。

覆盖需求中的全部要点：
1. 同一来源通道三张原始表，负责人/同组值班员/跨部门用户的差异化权限与只读范围；
2. 权限转交后历史操作归属原负责人、新动作归属新负责人，停用联动两张清洗表入待办；
3. 临时协作账号只读路径+最近校验时间，看不到连接参数账户名；
4. 委托到期自动收回；委托期内两次停用分别归属原负责人与受托人；
5. 30 秒内启用+停用两个请求：两条审计事实都保留，最终状态按操作时间可解释；
6. 撤权晚于提交、角色临时升级后回退两种顺序；
7. 审计按操作来源查询责任记录；任何未授权请求都不改变同步进程或下游队列。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.clock import clock
from app.main import app
from app.store import store

OWNER = "U-OWNER-01"      # 王敏，CH-ORDER 通道负责人
DUTY = "U-DUTY-01"        # 李娟，同组值班员
DUTY2 = "U-DUTY-02"       # 孙浩，同组另一值班员
CROSS = "U-CROSS-01"      # 周凯，跨部门用户
TEMP = "U-TEMP-01"        # 临时协作账号
CHANNEL = "CH-ORDER"
BRONZE_IDS = [1, 2, 3]    # 同一来源通道 CH-ORDER 下三张原始表
SILVER_BY_BRONZE = {1: ["SILV-201"], 3: ["SILV-202", "SILV-203"]}


class AccessControlTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def setUp(self) -> None:
        store.reset()
        clock.reset()

    # ---------- 辅助 ----------
    def _headers(self, user_id: str, source: str = "web") -> dict[str, str]:
        return {"X-Operator-Id": user_id, "X-Operator-Source": source}

    def _toggle(self, user_id: str, bronze_id: int, action: str, source: str = "web"):
        return self.client.post(
            f"/api/bronze/{bronze_id}/actions",
            headers=self._headers(user_id, source),
            json={"values": {"action": action}},
        )

    def _facts(self, **params):
        return self.client.get("/api/access/audit-facts", params=params).json()["items"]

    def _silver_status(self, code: str) -> str:
        rows = [r for r in store.rows("silver") if r["表编号"] == code]
        return rows[0]["status"]

    # ---------- 场景一：三用户对三张表的归属判定与越权拒绝 ----------
    def test_01_owner_same_group_cross_user_permissions(self) -> None:
        # 1. 一次性判定：三类用户对表1的归属、权限、只读范围
        for user_id, can_toggle, scope, role in [
            (OWNER, True, "full", "通道负责人"),
            (DUTY, False, "group", "同组值班员"),
            (CROSS, False, "cross", "跨部门用户"),
        ]:
            resp = self.client.get(
                "/api/bronze/1/permission", headers=self._headers(user_id)
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["can_toggle"], can_toggle)
            self.assertEqual(data["read_scope"], scope)
            self.assertEqual(data["role_view"], role)
            self.assertEqual(data["ownership"]["来源通道"], CHANNEL)
            self.assertEqual(data["ownership"]["负责人"], "王敏")

        # 2. 跨部门用户停用请求必须被拒绝，并返回归属信息
        before = store.find("bronze", 1)["status"]
        resp = self._toggle(CROSS, 1, "停用同步", source="ops-console")
        self.assertEqual(resp.status_code, 403)
        detail = resp.json()["detail"]
        self.assertEqual(detail["ownership"]["来源通道"], CHANNEL)
        self.assertEqual(detail["ownership"]["负责人ID"], OWNER)
        self.assertEqual(detail["role_view"], "跨部门用户")
        self.assertIn("audit", detail)
        # 同步进程与下游队列零变化
        self.assertEqual(store.find("bronze", 1)["status"], before)
        self.assertEqual(store.rows("silver_pending"), [])

        # 3. 同组值班员仅允许浏览目标路径与校验结果，不能启停
        resp = self._toggle(DUTY, 1, "停用同步")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(store.find("bronze", 1)["status"], before)
        resp = self.client.get("/api/bronze/1", headers=self._headers(DUTY))
        view = resp.json()
        self.assertIn("目标路径", view)
        self.assertIn("数据校验", view)
        # 同组值班员看不到连接参数中的账户名（整体脱敏，不保留任何片段）
        self.assertNotIn("ingest_order", view["连接参数"])
        self.assertIn("***", view["连接参数"])

        # 4. 通道负责人可启用同步（表2当前为待校验）
        resp = self._toggle(OWNER, 2, "启用同步")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(store.find("bronze", 2)["status"], "已启用")

        # 5. 三张表归属同一通道，跨部门对三张表均无启停权
        for bronze_id in BRONZE_IDS:
            decision = self.client.get(
                f"/api/bronze/{bronze_id}/permission", headers=self._headers(CROSS)
            ).json()
            self.assertFalse(decision["can_toggle"])
            self.assertEqual(decision["ownership"]["来源通道"], CHANNEL)

        # 6. 越权尝试全部留痕，且未产生任何下游影响
        denied = self._facts(fact_type="越权拒绝")
        self.assertEqual(len(denied), 2)
        self.assertTrue(all(item["裁决结果"] == "已拒绝" for item in denied))
        self.assertEqual(store.rows("silver_pending"), [])

    # ---------- 场景二：转交后历史归属不变，新动作归新负责人，两清洗表入待办 ----------
    def test_02_transfer_keeps_history_and_silver_pending(self) -> None:
        # 1. 原负责人先做一次停用（表1），审计记在王敏名下
        resp = self._toggle(OWNER, 1, "停用同步", source="web")
        self.assertEqual(resp.status_code, 200)
        first_fact = self._facts(object_id="BRON-101", fact_type="启停裁决")[0]
        self.assertEqual(first_fact["操作人ID"], OWNER)
        self.assertEqual(first_fact["操作人"], "王敏")
        # 依赖表1的清洗表 SILV-201 进入待处理
        self.assertEqual(self._silver_status("SILV-201"), "待处理")
        queue = self.client.get("/api/access/silver-pending").json()["items"]
        self.assertIn("SILV-201", [item["清洗表编号"] for item in queue])

        # 2. 原负责人把通道权限转交给同组值班员孙浩
        resp = self.client.post(
            f"/api/access/channels/{CHANNEL}/transfer",
            headers=self._headers(OWNER),
            json={"values": {"new_owner_id": DUTY2}},
        )
        self.assertEqual(resp.status_code, 200)
        channel = self.client.get(
            "/api/access/channels", headers=self._headers(DUTY2)
        ).json()["items"][0]
        self.assertEqual(channel["负责人ID"], DUTY2)

        # 3. 转交后原负责人不再能启停
        resp = self._toggle(OWNER, 1, "启用同步")
        self.assertEqual(resp.status_code, 403)

        # 4. 历史操作仍归属原负责人
        history = self._facts(object_id="BRON-101", fact_type="启停裁决")
        self.assertEqual(history[0]["操作人ID"], OWNER)
        self.assertEqual(history[0]["操作人"], "王敏")

        # 5. 新负责人的启用动作记入新负责人名下
        resp = self._toggle(DUTY2, 1, "启用同步", source="mobile")
        self.assertEqual(resp.status_code, 200)
        latest = self._facts(object_id="BRON-101", fact_type="启停裁决")[-1]
        self.assertEqual(latest["操作人ID"], DUTY2)
        self.assertEqual(latest["操作人"], "孙浩")
        # 重新启用后 SILV-201 恢复为入队前状态并出队
        self.assertEqual(self._silver_status("SILV-201"), "已启用")
        self.assertEqual(
            self.client.get("/api/access/silver-pending",
                            params={"bronze_code": "BRON-101"}).json()["items"],
            [],
        )

        # 6. 新负责人先启用表3（种子为已停用），再停用：两张依赖清洗表随停用进入待处理队列
        self.assertEqual(self._toggle(DUTY2, 3, "启用同步").status_code, 200)
        resp = self._toggle(DUTY2, 3, "停用同步")
        self.assertEqual(resp.status_code, 200)
        queue = self.client.get("/api/access/silver-pending").json()["items"]
        queued_codes = {item["清洗表编号"] for item in queue}
        self.assertEqual(queued_codes, {"SILV-202", "SILV-203"})
        self.assertEqual(self._silver_status("SILV-202"), "待处理")
        self.assertEqual(self._silver_status("SILV-203"), "待处理")
        # 再启用后两张表恢复
        self._toggle(DUTY2, 3, "启用同步")
        self.assertEqual(self._silver_status("SILV-202"), "已启用")
        self.assertEqual(self._silver_status("SILV-203"), "已启用")

    # ---------- 场景三：临时协作账号的最小只读范围 ----------
    def test_03_temp_account_limited_view(self) -> None:
        # 临时账号只能看到路径与最近校验时间等白名单字段
        resp = self.client.get("/api/bronze/1", headers=self._headers(TEMP))
        self.assertEqual(resp.status_code, 200)
        view = resp.json()
        self.assertEqual(view["read_scope"], "temp")
        self.assertIn("目标路径", view)
        self.assertIn("最近校验时间", view)
        self.assertNotIn("连接参数", view)
        self.assertNotIn("保留策略", view)
        self.assertNotIn("ingest_order", str(view))

        # 通道清单同样不暴露账户名
        channels = self.client.get(
            "/api/access/channels", headers=self._headers(TEMP)
        ).json()["items"]
        self.assertTrue(all("ingest_" not in str(c["连接参数"]) for c in channels))

        # 临时账号启停被拒
        resp = self._toggle(TEMP, 1, "启用同步")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(store.find("bronze", 1)["status"], "已启用")

    # ---------- 场景四：委托到期自动收回，期间两次停用分别归属 ----------
    def test_04_delegation_expiry_and_attribution(self) -> None:
        base = datetime(2026, 11, 13, 9, 0, 0)
        clock.freeze(base)
        # 负责人王敏请假，把启停权委托给同组孙浩，2 小时后到期
        expires = (base + timedelta(hours=2)).isoformat(timespec="seconds")
        resp = self.client.post(
            f"/api/access/channels/{CHANNEL}/delegations",
            headers=self._headers(OWNER),
            json={"values": {"delegate_id": DUTY2, "expires_at": expires,
                             "reason": "负责人请假"}},
        )
        self.assertEqual(resp.status_code, 200)

        # 委托生效：受托人可停用，原负责人也保留权限
        clock.freeze(base + timedelta(minutes=10))
        resp = self._toggle(DUTY2, 1, "停用同步", source="delegate-console")
        self.assertEqual(resp.status_code, 200)
        delegate_fact = self._facts(object_id="BRON-101", fact_type="启停裁决")[-1]
        self.assertEqual(delegate_fact["操作人ID"], DUTY2)
        self.assertEqual(delegate_fact["操作人"], "孙浩")
        self.assertIsNotNone(delegate_fact["委托依据"])

        # 期间第二次停用（表3）由原负责人执行，记在原负责人名下
        clock.freeze(base + timedelta(minutes=20))
        resp = self._toggle(OWNER, 3, "停用同步", source="web")
        self.assertEqual(resp.status_code, 200)
        owner_fact = self._facts(object_id="BRON-103", fact_type="启停裁决")[-1]
        self.assertEqual(owner_fact["操作人ID"], OWNER)
        self.assertEqual(owner_fact["操作人"], "王敏")

        # 委托到期后权限自动收回：受托人再操作被拒
        clock.freeze(base + timedelta(hours=3))
        resp = self._toggle(DUTY2, 1, "启用同步")
        self.assertEqual(resp.status_code, 403)
        # 原负责人权限恢复可用
        resp = self._toggle(OWNER, 1, "启用同步")
        self.assertEqual(resp.status_code, 200)

        # 两次停用事实分别归属两人，均可按操作人检索
        sun_facts = self._facts(operator_id=DUTY2, fact_type="启停裁决")
        wang_facts = self._facts(operator_id=OWNER, fact_type="启停裁决")
        self.assertTrue(any(f["操作来源"] == "delegate-console" for f in sun_facts))
        self.assertTrue(any(f["操作来源"] == "web" for f in wang_facts))

    # ---------- 场景五：30 秒内启用+停用，两条事实保留且最终状态可解释 ----------
    def test_05_conflicting_requests_within_30_seconds(self) -> None:
        base = datetime(2026, 11, 13, 14, 0, 0)
        clock.freeze(base)
        # 表1当前已启用：先提交停用（14:00:00），再提交启用（14:00:20）
        resp1 = self._toggle(OWNER, 1, "停用同步", source="api")
        clock.freeze(base + timedelta(seconds=20))
        resp2 = self._toggle(OWNER, 1, "启用同步", source="api")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)

        # 两条审计事实都保留
        facts = self._facts(object_id="BRON-101", fact_type="启停裁决")
        self.assertEqual(len(facts), 2)
        self.assertEqual([f["裁决结果"] for f in facts], ["已停用", "已启用"])

        # 按操作时间解释：后发生的启用决定最终状态
        explanation = self.client.get(
            "/api/bronze/1/state-explanation"
        ).json()
        self.assertEqual(explanation["最终状态"], "已启用")
        self.assertEqual(explanation["事件总数"], 2)
        self.assertEqual(explanation["依据事件编号"], facts[-1]["日志编号"])
        self.assertEqual(store.find("bronze", 1)["status"], "已启用")

        # 反向顺序再验一次：启用后 10 秒内停用，最终为已停用
        clock.freeze(base + timedelta(seconds=40))
        self._toggle(OWNER, 1, "停用同步", source="api")
        clock.freeze(base + timedelta(seconds=45))
        self._toggle(OWNER, 1, "启用同步", source="api")
        clock.freeze(base + timedelta(seconds=50))
        self._toggle(OWNER, 1, "停用同步", source="api")
        explanation = self.client.get("/api/bronze/1/state-explanation").json()
        self.assertEqual(explanation["最终状态"], "已停用")
        self.assertEqual(explanation["事件总数"], 5)

    # ---------- 场景六：撤权晚于提交 ----------
    def test_06_revocation_after_submission(self) -> None:
        base = datetime(2026, 11, 13, 10, 0, 0)
        clock.freeze(base)
        # 提交时负责人身份有效，请求入队
        resp = self.client.post(
            "/api/bronze/1/requests",
            headers=self._headers(OWNER, "batch-api"),
            json={"values": {"action": "停用同步"}},
        )
        self.assertEqual(resp.status_code, 200)
        request_id = resp.json()["entry"]["请求编号"]
        self.assertEqual(
            self.client.get("/api/bronze/requests/list").json()["items"][0]["状态"],
            "待裁决",
        )

        # 裁决前撤权：负责人转交给孙浩
        clock.freeze(base + timedelta(minutes=5))
        self.client.post(
            f"/api/access/channels/{CHANNEL}/transfer",
            headers=self._headers(OWNER),
            json={"values": {"new_owner_id": DUTY2}},
        )

        # 裁决时刻按当前身份判定 → 原负责人请求被拒，状态与队列不变
        before = store.find("bronze", 1)["status"]
        resp = self.client.post(
            f"/api/bronze/requests/{request_id}/decide",
            headers=self._headers(DUTY),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(store.find("bronze", 1)["status"], before)
        self.assertEqual(store.rows("silver_pending"), [])

        # 受理事实与拒绝事实同时保留，可还原全过程
        facts = self._facts(object_id="BRON-101")
        self.assertIn("请求已受理", [f["事实类型"] for f in facts])
        denied = [f for f in facts if f["事实类型"] == "异步裁决" and f["裁决结果"] == "已拒绝"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["关联请求"], request_id)

    # ---------- 场景七：角色临时升级后回退 ----------
    def test_07_temporary_promotion_then_rollback(self) -> None:
        base = datetime(2026, 11, 13, 8, 0, 0)
        clock.freeze(base)
        expires = (base + timedelta(hours=1)).isoformat(timespec="seconds")
        # 临时升级：委托给同组李娟
        self.client.post(
            f"/api/access/channels/{CHANNEL}/delegations",
            headers=self._headers(OWNER),
            json={"values": {"delegate_id": DUTY, "expires_at": expires}},
        )
        # 升级期间李娟提交停用请求并入队
        resp = self.client.post(
            "/api/bronze/1/requests",
            headers=self._headers(DUTY, "shift-api"),
            json={"values": {"action": "停用同步"}},
        )
        request_id = resp.json()["entry"]["请求编号"]

        # 角色回退：委托到期
        clock.freeze(base + timedelta(hours=2))
        resp = self.client.post(
            f"/api/bronze/requests/{request_id}/decide",
            headers=self._headers(OWNER),
        )
        # 裁决时刻李娟已无启停权 → 拒绝，同步进程与下游队列不变
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(store.find("bronze", 1)["status"], "已启用")
        self.assertEqual(store.rows("silver_pending"), [])
        facts = self._facts(object_id="BRON-101")
        self.assertIn("请求已受理", [f["事实类型"] for f in facts])
        denied = [f for f in facts if f["事实类型"] == "异步裁决" and f["裁决结果"] == "已拒绝"]
        self.assertEqual(len(denied), 1)

        # 对照组：升级有效期内裁决则放行
        clock.freeze(base + timedelta(minutes=30))
        resp = self.client.post(
            "/api/bronze/1/requests",
            headers=self._headers(DUTY, "shift-api"),
            json={"values": {"action": "停用同步"}},
        )
        valid_req = resp.json()["entry"]["请求编号"]
        resp = self.client.post(
            f"/api/bronze/requests/{valid_req}/decide",
            headers=self._headers(OWNER),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(store.find("bronze", 1)["status"], "已停用")
        self.assertEqual(self._silver_status("SILV-201"), "待处理")

    # ---------- 场景八：按操作来源查询责任记录 + 未授权零副作用 ----------
    def test_08_query_by_source_and_no_side_effects(self) -> None:
        self._toggle(OWNER, 1, "停用同步", source="web")
        self._toggle(CROSS, 1, "启用同步", source="ops-console")
        self._toggle(DUTY, 3, "停用同步", source="mobile")

        web_facts = self._facts(source="web")
        console_facts = self._facts(source="ops-console")
        mobile_facts = self._facts(source="mobile")
        self.assertTrue(web_facts and all(f["操作来源"] == "web" for f in web_facts))
        self.assertTrue(all(f["操作来源"] == "ops-console" for f in console_facts))
        self.assertTrue(all(f["操作来源"] == "mobile" for f in mobile_facts))
        # 跨部门与同组值班员的请求均为拒绝
        self.assertTrue(all(f["裁决结果"] == "已拒绝" for f in console_facts + mobile_facts))

        # 未授权请求未改变任何同步状态：表3仍为种子状态「已停用」，且未新增队列项
        self.assertEqual(store.find("bronze", 3)["status"], "已停用")
        # 只有表1被负责人成功停用 → 仅 SILV-201 在队列
        self.assertEqual(
            {item["清洗表编号"] for item in store.rows("silver_pending")},
            {"SILV-201"},
        )

    # ---------- 场景九：缺少身份头与未知用户 ----------
    def test_09_identity_required(self) -> None:
        resp = self.client.post(
            "/api/bronze/1/actions", json={"values": {"action": "启用同步"}}
        )
        self.assertEqual(resp.status_code, 401)
        resp = self._toggle("U-NOBODY", 1, "启用同步")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
