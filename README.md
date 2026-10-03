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

## 排班策略（可解释）

服务单提交时可携带 `event_starts_at`（场次开场时间）与 `required_skill`（所需技能）。领取顺序不再只看静态优先级，而是按以下整数分值综合排序，领取响应与 `GET /api/compute/queue/preview` 都会返回逐单的打分明细：

- 基础紧急度 `priority`（0-100）；
- 等待补偿：自最近一次入队（`queue_rank_at`）起每分钟 +1，长期未被领取的普通订单会逐步追上高优先级订单；
- 临近开场：开场前 120 分钟窗口内线性加分，超过开场时间后按每分钟 +2 加速累计；
- 技能匹配：显式声明 `required_skill` 且被工作者持有时 +15；不具备所需技能的订单不会进入候选集；未声明技能时沿用模板 `algorithm` 的资格匹配。

总分相同时按入队时间、订单 id 两级稳定排序。人工临时加权走 `POST /api/compute/tasks/{id}/boost`：必须填写原因（写入审计），单次不超过 40 分、最长 2 小时自动失效，因此不会变成永久高优先级。重新排队（失败重试、租约恢复、人工 retry）都会把排队基准重置为重新入队时刻；领取使用条件更新兜底，取消或重排不会造成重复领取。每次领取在 `compute_claim_events` 留存总分与打分明细，订单详情接口同时返回加权记录与领取事件。

