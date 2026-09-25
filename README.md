# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。事件页同时维护**消油剂使用台账**：批次批准量、适用海域、效期集中登记，喷洒逐单核销，撤回留痕不占额度。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和喷洒冲突判定。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：事件、批次台账、喷洒登记/撤回和批次用量查询页。
- `tests/`：完整流程、规则、失败和消油剂台账测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

事件：

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

消油剂批次台账（`dispersant_batches`）：

- `POST /api/batches`：登记批次，字段 `batch_no`、`approved_total`、`unit`、`allowed_areas[]`、`valid_from`(可空)、`valid_until`。仅`response_commander`。
- `GET /api/batches`：批次列表，含 `used_total`（有效喷洒合计）和 `remaining`（余量）。
- `GET /api/batches/{id}`
- `GET /api/batches/{id}/usage`：按批次查用量，含全部喷洒/撤回明细与计数。
- `POST /api/batches/{id}/correct`：批次更正（批准总量、适用海域、效期），可选 `expected_version` 乐观锁；返回 `recalculation`，按新值对未结束事件逐单重算并列出冲突单。仅`response_commander`。

喷洒与撤回（`spray_usages`）：

- `POST /api/items/{id}/sprays`：登记喷洒，字段 `batch_id`、`amount`、`started_at`、`ended_at`（ISO8601 UTC）、`area`、`latitude`、`longitude`、`vessel`、`external_ref`(可空)。`response_commander`、`operations`。
- `POST /api/sprays/{id}/withdraw`：撤回，提交 `reason`；记录保留（`status=withdrawn`），额度立即释放，不可重复撤回。
- `GET /api/items/{id}/sprays`：事件的喷洒列表。
- `GET /api/sprays?batch_id=&item_id=`：可按批次或事件过滤。

### 喷洒保存规则

登记前按批次校验，**任一冲突整单不保存**（409，响应体 `details.conflicts` 指出冲突批次和余量）：

- `batch_expired` / `batch_not_effective`：喷洒起止时间超出批次有效期；
- `area_mismatch`：海域不在批次 `allowed_areas` 内；
- `over_quota`：用量超过「批准总量 − 有效喷洒合计」，返回当前 `remaining`。

撤回不占额度；批次更正后，未结束事件按新批准量/海域/效期重新核算，已关闭事件冻结（不能再登记或撤回）。喷洒、撤回、批次登记/更正均写入审计链。

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
