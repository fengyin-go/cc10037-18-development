# ETL编排与数据质量中心

面向数据仓库ETL作业场景，提供流水线编排、质量规则、依赖调度和回刷补偿的ETL治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流水线定义 | `pipeline_def` | ETL流水线 | 流水线名称、处理阶段、输入源表 |
| 任务节点 | `task_node` | 处理节点 | 节点名称、所属管线、节点类型 |
| 质量规则 | `quality_rule` | 校验规则 | 规则名称、目标表、校验字段 |
| 质量执行 | `validation_run` | 校验批次 | 批次标识、目标表、校验分区 |
| 依赖调度 | `depend_scheduler` | 调度配置 | 配置名称、目标管线、触发方式 |
| 滑动窗口 | `sliding_window` | 窗口配置 | 窗口名称、目标表、窗口字段 |
| 回刷补偿 | `rerun_compensation` | 重跑任务 | 任务名称、目标管线、重跑范围 |
| 水位追踪 | `watermark_tracker` | 水位记录 | 管线标识、分区字段、当前水位 |
| 资源池 | `resource_pool` | 计算资源 | 池名称、总核数、总内存 |
| 血缘图谱 | `lineage_graph` | 血缘边 | 源节点、目标节点、传递字段 |
| 异常捕获 | `exception_capture` | 异常记录 | 发生时间、所属管线、异常类型 |
| 模板仓库 | `template_store` | 处理模板 | 模板名称、处理类型、输入Schema |
| 分区扫描 | `partition_scan` | 扫描报告 | 报告时间、目标表、分区总数 |
| 配置迁移 | `config_migration` | 迁移任务 | 任务名称、源环境、目标环境 |
| Schema演进 | `schema_evolution` | 变更记录 | 变更时间、目标表、变更类型 |
| 回填追踪 | `backfill_tracker` | 回填进度 | 追踪ID、回填任务、当前分区 |
| 成本分析 | `cost_analysis` | 成本快照 | 快照日期、管线标识、计算时长 |
| 告警策略 | `alert_policy` | 告警规则 | 规则名称、监控对象、告警条件 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 费用期分区费用核对

成本快照原本只按管线汇总，预算人员拿不到可下载的分区级分摊文件。
现在由回填回执提供事实、成本快照提供金额，生成可分段的成本核对文件。

**链路**

```text
回填回执 (backfill_receipt)          每条 = 回填任务 + 分区日期 + 一次执行尝试
   │  已确认 → 有效写入量；已拒绝 = 失败尝试；待补交 = 尚未交出
   ▼
有效分区写入量 (GET /api/cost/write-throughput)
   │
   ▼
分区费用分摊 (backend/app/engine/cost_allocation.py)
   │  金额只取费用期快照 (cost_period → cost_analysis)
   ▼
成本核对文件 (GET /api/cost/periods/{period}/export)
   │  已确认明细 / 暂未分摊 / 排除回执 / 费用合计 四段
   ▼
版本留档 (cost_revision)  旧文件按版本号原样取回，重算写入修订依据
```

**口径**

- 分摊分母 = `max(预期写入量, 已确认有效写入量)`，已确认量超预期时也不会摊超快照。
- 金额按分（cent）用最大余数法分配，`已分摊 + 暂未分摊 == 快照金额` 严格成立。
- 失败尝试（`已拒绝`）、待补交、`as_of` 导出时点之后到达的回执都不进分母，
  只在核对文件的「排除回执」段留原因。
- 迟到回执不改已结转期的金额：按「回执到达日期 ≤ 费用期截止日」重新归期，
  原期只记录「跨月补交，已归入 X 期」。
- 跨月更正先校验回执与账期关联，通过后才改写入量并重算计算费/存储费；
  校验不通过直接拒绝，不会产生「先算金额、后改归期」的中间态。

**接口**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/cost/periods` | 费用期列表：快照金额、已分摊、待核、核对结论 |
| GET | `/api/cost/periods/{period}?as_of=YYYY-MM-DD` | 分区费用核对详情 |
| GET | `/api/cost/periods/{period}/export?as_of=&reason=` | 下载并登记新版本核对文件 |
| GET | `/api/cost/periods/{period}/revisions` | 已发出文件版本和修订依据 |
| GET | `/api/cost/periods/{period}/revisions/{version}` | 原样取回历史文件 |
| GET | `/api/cost/tasks/{task}/allocation` | 按回填任务看跨期分摊 |
| GET | `/api/cost/write-throughput` | 有效写入量对账（确认/失败/待补交） |
| GET/POST | `/api/cost/receipts` | 回执明细 / 登记回执 |
| POST | `/api/cost/receipts/{id}/correct` | 跨月更正（先校验账期） |
| POST | `/api/cost/receipts/{id}/confirm` `/reject` | 确认有效 / 标记失败并重算 |

**种子场景**（费用期 2026-11 截止 2026-11-20，快照 450 = 计算 360 + 存储 90）

| 回执 | 分区 | 写入量 | 到达 | 归属期 | 状态 |
| --- | --- | --- | --- | --- | --- |
| RC-1118-001A | 2026-11-14 | 180 GB | 11-18 | 2026-11 | 已确认 |
| RC-1119-002A/B/C | 11-13/14/15 | 240/120/60 GB | 11-19 | 2026-11 | 已确认 |
| RC-1119-002X | 2026-11-13 | 96 GB | 11-19 | 2026-11 | 已拒绝（失败尝试） |
| RC-1118-001B | 2026-11-15 | 150 GB | 11-21 | 2026-12 | 已确认（跨月补交） |
| RC-1119-003P | 2026-11-16 | 未交出 | 11-19 | 2026-11 | 待补交 |

- `as_of=2026-11-18`：只看得到 180GB，已分摊 108，待核 342。
- `as_of=2026-11-19`：四条确认到齐 600GB，已分摊 360，待核 90。
- 2026-12 期：迟到回执 150GB 分摊 125，待核 25（快照 150）。
- 测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`

领域：数据工程
子方向：数仓与数据湖
