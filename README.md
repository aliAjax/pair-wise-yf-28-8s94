# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总、链状态（`chain.status`）、最近校验时间（`chain.last_verified_at`）和审计记录。
- `POST /api/trials/{id}/verify`：核验追加账，返回 `intact`/`broken` 状态及被改写、漏记、缺失、断链、孤儿等断点，并写入最近校验时间。
- `GET /api/trials/{id}/chain`：按链位顺序返回追加账条目（含前向哈希与条目哈希）。

## 追加账（审计链）

所有随机分配（`participant.enroll`）、方案变更（`trial.create`/`protocol.update`/`trial.start`）和揭盲（`unblinding.request`/`unblinding.approve.*`）在写入审计记录的同一事务内，按发生顺序向 `audit_ledger` 追加一条摘要：

- 每条摘要 = `sha256(版本, 审计记录ID, 试验ID, 操作人, 动作, 审计内容, 发生时间, 链位, 前一条摘要)`，前一条摘要即上链位的 `entry_hash`，首条链接版本化创世串。
- 链位由事务内 `SELECT MAX(chain_seq)+1` 决定，并由 `UNIQUE(trial_id,chain_seq)` 兜底；配合分配动作的 `BEGIN IMMEDIATE` 写锁，两名值班员并发提交不会写出同一链位。
- 摘要追加与审计记录、业务数据在同一事务提交；写盘失败整体回滚，重试时链仍从正确位置继续，不留半条链。
- 旧库升级时，`init_schema` 会对已有审计记录但尚无链的试验按现有 `id` 顺序回填摘要（回填在同一事务内完成，失败回滚、重试不产生半条链）。
- 核验（`POST /api/trials/{id}/verify`）重算每条摘要并与存储值比对，指出：`rewritten`（审计内容被改写）、`omitted`（审计记录未入链）、`missing`（链位缺失）、`broken_link`（前向链接断裂）、`orphan`（链引用的审计记录不存在）。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
