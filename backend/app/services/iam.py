"""身份与来源通道归属服务。

核心是 :meth:`IAMService.evaluate` —— 对一张原始表、一个操作者，一次性完成
「来源通道归属 → 角色 → 启停权限 → 只读范围」的完整判定，返回结构化结论；
任何启停动作都只能依据这份结论决定放行或拒绝，业务层不再自行拼权限逻辑。
"""
from __future__ import annotations

from typing import Any

from app.clock import clock
from app.store import store

USERS = "users"
CHANNELS = "channels"
DELEGATIONS = "delegations"

# 各角色对原始表的只读字段范围（最小授权）
FULL_VIEW = "full"          # 负责人/受托人：全字段，含连接参数账户名
GROUP_VIEW = "group"        # 同组值班员：目标路径、校验结果等，不含连接参数
TEMP_VIEW = "temp"          # 临时协作：仅原始表路径与最近一次校验时间
CROSS_VIEW = "cross"        # 跨部门：业务字段只读，不含连接参数

TEMP_VISIBLE_FIELDS = ["id", "表编号", "来源通道", "目标路径", "最近校验时间"]


class PermissionDenied(Exception):
    """权限判定不通过时抛出，携带可直接返回给调用方的归属信息。"""

    def __init__(self, message: str, decision: dict[str, Any]) -> None:
        super().__init__(message)
        self.decision = decision


class IAMService:
    # ---------- 基础查询 ----------
    def list_users(self) -> list[dict[str, Any]]:
        return store.rows(USERS)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return store.find_by(USERS, "用户ID", user_id)

    def list_channels(self) -> list[dict[str, Any]]:
        return store.rows(CHANNELS)

    def get_channel(self, channel_code: str) -> dict[str, Any] | None:
        return store.find_by(CHANNELS, "来源通道", channel_code)

    def channel_of_bronze(self, bronze_id: int) -> dict[str, Any] | None:
        bronze = store.find("bronze", bronze_id)
        if bronze is None:
            return None
        return self.get_channel(str(bronze.get("来源通道")))

    def active_delegation(self, channel_code: str) -> dict[str, Any] | None:
        """返回当前时刻仍有效的委托；到期委托自动视为失效（懒收回）。"""
        now = clock.now()
        for item in store.rows(DELEGATIONS):
            if item.get("来源通道") != channel_code or item.get("状态") != "生效中":
                continue
            if clock.parse(str(item["到期时间"])) >= now:
                return item
        return None

    def list_delegations(self, channel_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(DELEGATIONS)
        if channel_code:
            rows = [row for row in rows if row.get("来源通道") == channel_code]
        return rows

    # ---------- 一次性权限判定 ----------
    def evaluate(self, bronze_id: int, user_id: str) -> dict[str, Any]:
        """对原始表 + 操作者做完整归属/权限/只读范围判定。

        返回字段固定，业务层与接口层共用同一份结论，保证“一次判断完成”。
        """
        bronze = store.find("bronze", bronze_id)
        if bronze is None:
            return {"found": False, "allowed": False, "can_toggle": False,
                    "reason": f"原始表 {bronze_id} 不存在或已归档",
                    "ownership": None, "read_scope": None}
        channel = self.get_channel(str(bronze.get("来源通道")))
        user = self.get_user(user_id)
        if user is None:
            return {"found": True, "allowed": False, "can_toggle": False,
                    "reason": f"操作者 {user_id} 不存在或已注销",
                    "ownership": self._ownership_info(channel),
                    "read_scope": None, "user": None}

        delegation = self.active_delegation(str(channel["来源通道"])) if channel else None
        is_owner = channel is not None and channel.get("负责人ID") == user_id
        is_delegate = delegation is not None and delegation.get("受托人ID") == user_id
        same_group = channel is not None and user.get("值班组") == channel.get("值班组")
        is_temp = bool(user.get("临时账号"))

        if is_owner:
            role_view, scope, can_toggle = "通道负责人", FULL_VIEW, True
        elif is_delegate:
            role_view, scope, can_toggle = "受托负责人", FULL_VIEW, True
        elif is_temp:
            role_view, scope, can_toggle = "临时协作", TEMP_VIEW, False
        elif same_group:
            role_view, scope, can_toggle = "同组值班员", GROUP_VIEW, False
        else:
            role_view, scope, can_toggle = "跨部门用户", CROSS_VIEW, False

        return {
            "found": True,
            "allowed": True,
            "can_toggle": can_toggle,
            "role_view": role_view,
            "read_scope": scope,
            "user": {"用户ID": user["用户ID"], "姓名": user["姓名"],
                     "部门": user["部门"], "值班组": user["值班组"],
                     "临时账号": is_temp},
            "ownership": self._ownership_info(channel, delegation),
            "delegation": (
                {"委托ID": delegation["委托ID"], "受托人ID": delegation["受托人ID"],
                 "受托人": delegation["受托人"], "到期时间": delegation["到期时间"]}
                if delegation else None
            ),
            "reason": "具备启停权限" if can_toggle else "仅具备只读权限，启停需来源通道负责人",
        }

    def require_toggle(self, bronze_id: int, user_id: str) -> dict[str, Any]:
        """启停动作的统一闸门：判定不通过直接抛 PermissionDenied。"""
        decision = self.evaluate(bronze_id, user_id)
        if not decision.get("found"):
            raise PermissionDenied(decision["reason"], decision)
        if not decision.get("can_toggle"):
            raise PermissionDenied(
                f"越权拒绝：{decision['user']['姓名']}（{decision['role_view']}）"
                f"不是来源通道 {decision['ownership']['来源通道']} 的负责人，"
                "仅可查看，不能启用/停用同步",
                decision,
            )
        return decision

    # ---------- 视图裁剪 ----------
    # 账户/口令类键名：无论嵌套在哪一层，值都整体脱敏，绝不保留任何片段
    _SECRET_KEYWORDS = ("账户名", "账号", "用户名", "user", "username", "account",
                        "密码", "口令", "password", "secret", "token")
    _MASK = "***"

    def mask_connection(self, connection: str | None, can_view_secret: bool) -> str | None:
        """连接参数中的账户名只对负责人/受托人可见，其余角色整体脱敏（不保留片段）。"""
        if connection is None:
            return None
        if can_view_secret:
            return connection
        try:
            import json

            payload = json.loads(connection)
        except (ValueError, TypeError):
            # 非 JSON 文本：用正则按「键:值」整体替换，避免只替前缀造成泄露
            import re

            def _blank(match: "re.Match[str]") -> str:
                return f"{match.group(1)}\"{self._MASK}\""

            return re.sub(
                r"(\"[^\"]*(?:" + "|".join(self._SECRET_KEYWORDS) + r")[^\"]*\"\s*:\s*)\"[^\"]*\"",
                _blank,
                connection,
                flags=re.IGNORECASE,
            )
        self._redact(payload)
        return json.dumps(payload, ensure_ascii=False)

    def _redact(self, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(key, str) and self._is_secret_key(key):
                    node[key] = self._MASK
                else:
                    self._redact(value)
        elif isinstance(node, list):
            for item in node:
                self._redact(item)

    @classmethod
    def _is_secret_key(cls, key: str) -> bool:
        low = key.lower()
        return any(kw.lower() in low for kw in cls._SECRET_KEYWORDS)

    def project_bronze(self, bronze: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        """按判定出的只读范围裁剪原始表字段，一次调用给出该用户可见的完整视图。"""
        scope = decision.get("read_scope")
        if scope == TEMP_VIEW:
            view = {key: bronze.get(key) for key in TEMP_VISIBLE_FIELDS}
            view["read_scope"] = TEMP_VIEW
            return view
        view = dict(bronze)
        channel = self.get_channel(str(bronze.get("来源通道")))
        if channel is not None:
            view["通道负责人"] = channel.get("负责人")
            view["所属部门"] = channel.get("所属部门")
            view["连接参数"] = self.mask_connection(
                channel.get("连接参数"), can_view_secret=scope == FULL_VIEW
            )
        view["read_scope"] = scope
        view["can_toggle"] = bool(decision.get("can_toggle"))
        view["role_view"] = decision.get("role_view")
        return view

    # ---------- 归属变更：转交与委托 ----------
    def transfer_ownership(self, channel_code: str, new_owner_id: str,
                           operator_id: str) -> tuple[dict[str, Any] | None, str]:
        """永久转交通道负责人；只有现任负责人可操作。历史审计不受影响（快照留痕）。"""
        channel = self.get_channel(channel_code)
        new_owner = self.get_user(new_owner_id)
        if channel is None:
            return None, f"来源通道 {channel_code} 不存在"
        if new_owner is None:
            return None, f"新负责人 {new_owner_id} 不存在"
        if channel.get("负责人ID") != operator_id:
            return None, (f"越权拒绝：仅现任负责人 {channel.get('负责人')} "
                          f"可转交通道 {channel_code} 的归属")
        old_owner = channel.get("负责人")
        channel["负责人ID"] = new_owner_id
        channel["负责人"] = new_owner["姓名"]
        return channel, f"通道 {channel_code} 负责人已由 {old_owner} 转交为 {new_owner['姓名']}"

    def create_delegation(self, channel_code: str, delegate_id: str,
                          expires_at: str, operator_id: str,
                          reason: str = "") -> tuple[dict[str, Any] | None, str]:
        """负责人请假时委托启停权；到期后权限自动收回（裁决时按当前时刻判定）。"""
        channel = self.get_channel(channel_code)
        delegate = self.get_user(delegate_id)
        if channel is None:
            return None, f"来源通道 {channel_code} 不存在"
        if delegate is None:
            return None, f"受托人 {delegate_id} 不存在"
        if channel.get("负责人ID") != operator_id:
            return None, (f"越权拒绝：仅现任负责人 {channel.get('负责人')} "
                          f"可委托通道 {channel_code} 的启停权")
        try:
            expiry = clock.parse(expires_at)
        except ValueError:
            return None, f"到期时间格式非法：{expires_at}（需 ISO 8601）"
        if expiry <= clock.now():
            return None, "委托到期时间必须晚于当前时间"
        delegation = {
            "id": store.next_id(DELEGATIONS),
            "委托ID": f"DLG-{store.next_id(DELEGATIONS):04d}",
            "来源通道": channel_code,
            "委托人ID": operator_id,
            "委托人": channel.get("负责人"),
            "受托人ID": delegate_id,
            "受托人": delegate["姓名"],
            "生效时间": clock.now_iso(),
            "到期时间": expiry.isoformat(timespec="seconds"),
            "状态": "生效中",
            "委托原因": reason,
        }
        store.rows(DELEGATIONS).append(delegation)
        return delegation, (f"通道 {channel_code} 启停权已委托给 {delegate['姓名']}，"
                            f"到期自动收回：{delegation['到期时间']}")

    # ---------- 内部工具 ----------
    @staticmethod
    def _ownership_info(channel: dict[str, Any] | None,
                        delegation: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if channel is None:
            return None
        info = {
            "来源通道": channel["来源通道"],
            "通道名称": channel.get("通道名称"),
            "所属部门": channel.get("所属部门"),
            "值班组": channel.get("值班组"),
            "负责人ID": channel.get("负责人ID"),
            "负责人": channel.get("负责人"),
        }
        if delegation:
            info["当前受托人"] = delegation["受托人"]
            info["委托到期时间"] = delegation["到期时间"]
        return info


iam_service = IAMService()
