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
- `GET /api/trials/{id}/summary`：中心级汇总、审计记录和链状态（条目数、链头摘要、最近校验时间与结果）。
- `POST /api/trials/{id}/audit/verify`：核验该试验审计链，返回被改写、漏记或缺失的断点位置。
- `POST /api/audit/verify`：核验全部试验的审计链（含全局链）。仅协调员和监查员可核验。

## 审计摘要链

审计记录为只可追加的摘要链：每次随机分配、方案变更、揭盲等动作按发生顺序追加一条记录，
`digest = SHA-256(前一条摘要 ‖ 试验 ‖ 操作者 ‖ 动作 ‖ 明细 ‖ 时间)`，链位 `seq` 由数据库唯一索引
保证不重复。审计条目与业务写入在同一事务中提交：两名值班员同时提交时由 SQLite 写锁串行化，
不会写出同一链位；写盘失败整体回滚，重试不会留下半条链。

旧库升级：服务启动时自动为 `audit_log` 补充 `seq/prev_digest/digest` 列，并按既有 `id` 顺序回填
摘要（回填时间记录在 `audit_chain_meta.backfilled_at`），已回填的条目不会被改写。

核验入口会重算整条链并报告断点：`tampered`（内容与摘要不符，记录被改写）、
`gap`（链位缺失或漏记）、`broken_link`（前置摘要与上一条不符）、`unsealed`（缺少摘要）、
`tail_tampered`（链尾与链元数据不符，尾部条目被删除）。链头摘要同时保存在独立的
`audit_chain_meta` 表中交叉核对，核验结果和校验时间也写入该表，并在试验摘要的 `chain` 字段中展示。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
