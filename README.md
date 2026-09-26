# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 实训中断补偿

自然灾害或企业停产导致实训中断时，`app/interruption` 模块提供完整的补偿生命周期：中断登记 → 影响名单 → 方案生成 → 学生确认 → 方案调整 → 原活动恢复 → 结算。

- **缺口生成**：按能力要求减去原活动已完成部分得到缺口（避免重复计入），再从可用时段池中按开始时间确定性地切出补偿项；时段可声明来源方案与折算比例（跨方案折算），未覆盖的缺口记入 `unfilled`。
- **确认锁定**：学生确认本人方案后经原子条件更新锁定，并发确认只发生一次状态迁移，重复确认幂等。
- **恢复释放**：原活动恢复时只释放尚未开始的补偿项（跨越恢复点的按已完成部分拆分保留），保留部分的来源（原活动 / 替代时段 / 来源方案）在结算中完整保留。
- **结算**：汇总原活动已完成部分与保留的补偿部分，按能力逐项核对要求，结果持久化且幂等；每次状态变更都写入带指纹的审计轨迹。

主要接口（均位于 `/api` 下）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/interruptions` | 中断登记（幂等） |
| POST | `/interruptions/{id}/impacts` | 批量登记影响名单 |
| POST | `/interruptions/{id}/plans/generate` | 按能力缺口与可用时段生成补偿方案 |
| POST | `/interruptions/{id}/resume` | 原活动恢复，释放未开始的补偿 |
| POST | `/compensation-plans/{id}/confirm` | 学生确认并锁定方案 |
| POST | `/compensation-plans/{id}/adjust` | 调整补偿项时段或折算比例 |
| POST | `/compensation-plans/{id}/settle` | 结算并持久化结果 |
| GET | `/interruptions/{id}`、`/interruptions/{id}/impacts`、`/interruptions/{id}/plans`、`/compensation-plans/{id}`、`/compensation-plans/{id}/settlement` | 查询 |

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及中断补偿的批量影响、并发确认、跨方案折算与重启恢复；运行过程中不需要单独的数据库或网络服务。
