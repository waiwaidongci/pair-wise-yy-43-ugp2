# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/dispersants`，事件消油剂使用台账
- `POST /api/items/{id}/dispersants`，登记喷洒（事件、批次、用量、起止时间、坐标、操作船）
- `POST /api/dispersants/{id}/withdraw`，撤回喷洒（留痕但不占额度）
- `GET /api/batches`、`POST /api/batches`，批次台账（批准总量、适用海域、有效期）
- `POST /api/batches/{id}/correct`，批次更正，未结束事件按新值核算
- `GET /api/batches/{id}/usages`，按批次查用量
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

消油剂按批次管理：批次过期、海域不符或扣除撤回后超量时整单不保存，报错指出冲突批次和余量；批次登记与更正由response_commander执行，喷洒与撤回由response_commander或operations执行，全部进入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
