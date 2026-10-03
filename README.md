# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

## 排班策略

周末多场婚礼与追思仪式并行时，服务单的领取顺序不再由单一“高优先级”标签决定，而是按可解释的综合评分排序（实现见 `app/compute/scheduling.py`）：

| 组件 | 上限 | 说明 |
| --- | --- | --- |
| 临近开场 | 30 | 开场前 180 分钟内线性升高，开场时刻满分 |
| 服务紧急度 | 20 | 提交时 `urgency` 1~5 级线性映射 |
| 等待时长 | 10 | 入队后前 30 分钟线性计分 |
| 长期等待补偿 | 25 | 排队超过 15 分钟宽限后，每 10 分钟加 2 分，封顶 25 |
| 优先级标签 | 15 | 人工标签权重受限，不能永久挤压普通订单 |
| 技能匹配 | 5 | 领取者对所需技能（1~5 级）等级越高越靠前 |
| 临时加权 | 40 | 人工临时提权，必须填写原因并设置有效期，到期自动失效 |

同分订单按“开场时间升序 → 入队时间升序 → 订单 id 升序”稳定打破平局。失败重试、租约恢复或人工重新排队会重置等待补偿基准并作废旧的临时加权。

- 提交服务单：`POST /api/compute/tasks`，可携带 `urgency`、`start_at`、`required_skill`、`min_skill_level`。
- 领取：`POST /api/compute/tasks/claim`，请求体的 `skills` 声明领取者技能等级；“选候选 + 条件更新”在同一即时事务内完成，取消、退避或重新排队都不会造成重复领取。
- 临时提权：`POST /api/compute/tasks/{id}/boost`，原因必填，分值与有效期受限；同时写入人工干预记录与排班审计。
- 顺序预览（不改状态）：`POST /api/compute/queue/preview`，返回每张单的逐组件分值与中文解释。
- 排班审计：`GET /api/compute/schedule-events`，记录领取、重新排队、临时提权与标签调整的分值明细。

评分只依赖数据库内状态与可注入时钟，因此进程重启后队列顺序与领取结果保持一致。
