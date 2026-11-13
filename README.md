# 数据湖仓一体管理平台

面向数据湖仓一体架构下的多源异构接入、原始文件编目、湖内分区治理、流批一体转换、版本回溯与数据编目检索的管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据编目 | `catalog` | 数据目录 | 编目编号、数据集名称、存储路径 |
| 多源接入 | `ingest` | 接入通道 | 通道编号、源端类型、连接参数 |
| 原始层管理 | `bronze` | 原始表 | 表编号、来源通道、目标路径 |
| 清洗层管理 | `silver` | 清洗表 | 表编号、上游原始表、清洗脚本 |
| 聚合层管理 | `gold` | 聚合视图 | 视图编号、上游清洗表、聚合维度 |
| 湖区分区 | `lakezone` | 分区方案 | 方案编号、目标表名、分区字段 |
| 流式处理 | `stream` | 流处理作业 | 作业编号、输入Topic、处理逻辑 |
| 批量转换 | `batch` | 批处理任务 | 任务编号、输入路径、处理脚本 |
| 版本快照 | `snapshot` | 快照记录 | 快照编号、目标表名、快照时间 |
| 湖仓治理 | `govern` | 治理规则 | 规则编号、治理对象、治理类型 |
| 元数据中心 | `meta` | 元数据条目 | 条目编号、对象类型、对象名称 |
| 血缘图谱 | `lineage` | 血缘节点 | 节点编号、节点类型、节点名称 |
| 质量监控 | `quality` | 质量指标 | 指标编号、监控对象、指标类型 |
| 编排调度 | `schedule` | 编排计划 | 计划编号、任务列表、依赖图定义 |
| 历史回填 | `backfill` | 回填作业 | 作业编号、目标表名、回填起始 |
| 数据检索 | `search` | 检索索引 | 索引编号、索引名称、覆盖表名 |
| 资源计量 | `resource` | 计量记录 | 记录编号、计算引擎、作业名称 |
| 操作审计 | `audit` | 审计日志 | 日志编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 原始表启停的来源通道归属控制

原始表的启用/停用同步不再对所有值班人员开放，而是按「来源通道归属」鉴权：

- **一次判定完成**：`IAMService.evaluate` 对原始表+操作者一次性给出归属、角色、
  启停权限与只读范围（`full`/`group`/`cross`/`temp`），启停统一走
  `require_toggle` 闸门；越权返回 403 并携带归属信息，同步进程与下游队列零变化。
- **只读范围**：同组值班员可浏览目标路径与校验结果；跨部门用户业务字段只读；
  临时协作账号仅能读取原始表路径与最近一次校验时间；连接参数中的账户名/口令对
  非负责人整体脱敏为 `***`。
- **下游联动**：停用后依赖该原始表的清洗表进入待处理清单
  （`/api/access/silver-pending`），重新启用时按入队前状态恢复并出队。
- **归属变更可追溯**：负责人转交、请假委托（到期自动收回）均留只追加审计；
  历史操作按当时人员快照归属，新动作才记在新负责人/受托人名下。
- **异步裁决**：请求提交与裁决分离，按裁决时刻的身份判定，覆盖「撤权晚于提交」
  与「角色临时升级后回退」两种顺序。
- **冲突可解释**：30 秒内的启用/停用两个请求都会保留为独立审计事实，
  `/api/bronze/{id}/state-explanation` 按操作时间重放事件并解释最终状态。
- **按来源追责**：审计台账支持按操作来源（`X-Operator-Source`）、操作对象、
  操作人查询责任记录。

相关接口：`POST /api/bronze/{id}/actions`（请求头 `X-Operator-Id`）、
`GET /api/bronze/{id}/permission`、`POST /api/bronze/{id}/requests` 与
`/api/bronze/requests/{编号}/decide`、`/api/access/*`。
测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

领域：数据工程
子方向：数仓与数据湖
