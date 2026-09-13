# 历史归档

这里保存决策依据、旧实施方案和阶段验收快照，不用于指导当前部署。文件中的“当前”“下一步”、表结构、命令和授权均限于原记录时点；最新结论见[项目状态](../PROJECT_STATUS.md)，可用命令见[运行手册](../OPERATIONS_RUNBOOK.md)。

归档不表示所有事项已关闭：阶段 M 的 iMac 身份问题仍在项目状态中列为待办，原报告保留精确映射和备份证据。V1 已退出运行时，不要按旧方案恢复 V1 写库链路。

## V1：官方设备历史 demo

| 材料 | 保留用途 |
| --- | --- |
| [原项目实施方案](v1/PROJECT_IMPLEMENTATION_PLAN.md) | 10 表、旧设备流程及早期设计取舍 |
| [阶段 0](v1/PHASE_0_BASELINE.md)、[阶段 1](v1/PHASE_1_BUILD_REPORT.md)、[阶段 2](v1/PHASE_2_BUILD_REPORT.md) | 初始范围、骨架与通用采集框架 |
| [阶段 3](v1/PHASE_3_BUILD_REPORT.md)、[阶段 4](v1/PHASE_4_BUILD_REPORT.md) | 五品牌旧实现与当时页面验证 |
| [阶段 5 验收](v1/PHASE_5_ACCEPTANCE_REPORT.md) | 旧 demo 公司库与工程验收 |
| [2026-09-10 重采](v1/V1_RECOLLECTION_20260910_REPORT.md) | 当前保留的 V1 静态数据从何而来 |

## V2：通用框架、政府生鲜与设备接入

| 材料 | 保留用途 |
| --- | --- |
| [A 数据库基础](v2/V2_PHASE_A_BUILD_REPORT.md)、[B 重采决策](v2/V2_PHASE_B_BUILD_REPORT.md) | 为什么不迁移 V1、如何建立通用点时模型 |
| [C 通用连接器](v2/V2_PHASE_C_BUILD_REPORT.md)、[D 生鲜规则](v2/V2_PHASE_D_BUILD_REPORT.md) | 连接器与规则的初始实现验证 |
| [E 来源可行性](v2/V2_PHASE_E_FEASIBILITY_REPORT.md) | 生鲜为何从商业平台路线收敛为政府公开数据；不是当前网站可用性报告 |
| [F 上海来源](v2/V2_PHASE_F_BUILD_REPORT.md)、[G 商务部来源](v2/V2_PHASE_G_BUILD_REPORT.md) | 两种政府来源的接入过程 |
| [H 公司库验收](v2/V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md) | 2026-08-25 获准重建、备份与政府价格验证；不是再次清库指令 |
| [设备原生接入计划](v2/V2_DEVICE_NATIVE_COLLECTION_PLAN.md) | 改造前状态、I～M 分阶段设计与退出 V1 的取舍 |
| [I 设备基础](v2/V2_PHASE_I_BUILD_REPORT.md)、[J Apple 接入](v2/V2_PHASE_J_BUILD_REPORT.md) | 产品多 SKU、标准身份和事务贯通 |
| [K 五品牌与保护](v2/V2_PHASE_K_BUILD_REPORT.md)、[L 统一运行时](v2/V2_PHASE_L_BUILD_REPORT.md) | 运行保护、只读重放/审计和旧代码退出 |
| [M 公司增量部署与验收](v2/V2_PHASE_M_ACCEPTANCE_REPORT.md) | 最近公司采集、隔离核验、备份恢复及尚未关闭的 iMac 身份问题 |

## 本次整理边界

2026-09-12 将 22 份历史材料移至本目录，正文只调整归档提示和相对链接，保留原始结论。主目录删除重复进度、V1 运行查询、已完成的分阶段待办及重复验收数字；数据库字段说明和现行安全规则继续保留。旧主目录文本可通过 Git 历史恢复，不额外保存同内容的计划副本。
