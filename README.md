# 发热门诊周报隐私发布服务

本地 FastAPI + SQLite 服务，按**固定街道 × 年龄段**分组发布每周发热门诊人数，
用 ε=1 的 Laplace 机制加噪，并以全局差分隐私预算（默认 3）限制多次发布的累计泄露。

## 启动

```bash
docker compose up --build
```

API 入口：<http://localhost:8000>，交互式文档：<http://localhost:8000/docs>。
SQLite 文件位于命名卷 `fever-data` 内的 `/data/fevers.db`，容器重建后数据仍在。

本地（不用 Docker）：

```bash
pip install -r requirements.txt
DATABASE_PATH=./data/fevers.db uvicorn app.entrypoint:app --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/groups` | 固定公开分组、ε、敏感度、每次发布消耗 |
| `POST` | `/v1/imports` | 导入一周记录，返回不可改写版本号 |
| `GET` | `/v1/imports/{version}` | 查询导入元数据 |
| `POST` | `/v1/publications` | 为某导入版本发布（或重放）加噪计数 |
| `GET` | `/v1/publications/{version}` | 查询已发布结果 |
| `GET` | `/v1/publications` | 列出全部已发布结果 |
| `GET` | `/v1/budget` | 全局预算使用情况 |

### 导入

```bash
curl -s -X POST localhost:8000/v1/imports -H 'Content-Type: application/json' -d '{
  "week": "2026-W20",
  "records": [
    {"patient_id": "anon-001", "street": "东城街道", "age_band": "0-14"},
    {"patient_id": "anon-002", "street": "西城街道", "age_band": "60+"}
  ]
}'
# {"version":1,"week":"2026-W20","record_count":2}
```

- `week` 必须是 ISO 周（`YYYY-Www`），且只能导入一次；重复导入返回 409。
- 同一 `patient_id` 在一周内出现多次 → **整批拒绝**（409），不写入任何数据。
  错误响应不包含该编号。
- `street`、`age_band` 只接受固定公开分组，未知值返回 422。
- 导入成功即形成不可改写版本：SQLite 触发器和应用逻辑都禁止更新/删除。

### 发布

```bash
curl -s -X POST localhost:8000/v1/publications \
  -H 'Content-Type: application/json' -d '{"import_version": 1}'
```

```jsonc
{
  "version": 1,                 // 发布版本
  "counts": [                   // 5×5=25 格，全部返回，包括零计数
    {"street": "东城街道", "age_band": "0-14", "count": 1}
  ],
  "remaining_budget": 2
}
```

响应**只包含**加噪计数、发布版本和剩余预算——不返回真实计数、不加噪前的数据或患者编号。

## 隐私语义

- 固定公开分组：5 个街道 × 5 个年龄段，零计数组合也发布（避免分组本身泄露）。
- 每个格子：`count + Laplace(0, 1/ε)`，`ε = 1`，单人敏感度 1；四舍五入为整数
  （后处理，不改变 DP 保证），负值保留以免扭曲小计数格子的分布。
- 一次发布（无论多少格子）消耗全局预算 1；全局预算 3，即最多 3 个不同版本的发布。
- 重复发布同一导入版本**返回原结果**（逐字节相同的加噪计数），不消耗预算；即使
  预算已耗尽，重放仍然可用。
- 并发发布：预算检查、计数写入与预算扣减在同一个 `BEGIN IMMEDIATE` 事务中完成，
  并由 `spent + cost <= total_budget` 条件更新兜底，因此不会透支。
- 预算不足（409 `privacy_budget_exhausted`）或写入失败（503
  `storage_write_failed`）时事务回滚：不产生新结果、不消耗预算，既有结果仍可查询。
- 患者编号只在进程内用于批内查重，从不落库。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：批内重复整批拒绝、周重复导入、固定分组校验、零计数格、发布幂等与预算计数、
预算耗尽、写入失败回滚、并发 6 请求最多 3 次成功、数据库层不可改写、Laplace 分布。
