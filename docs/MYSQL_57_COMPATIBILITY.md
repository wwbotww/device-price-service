# MySQL 兼容与迁移边界

本文说明实现约束，不重复公司验收流水。已核验的实例、数据规模和迁移时点见[项目状态](PROJECT_STATUS.md)，部署操作见[运行手册](OPERATIONS_RUNBOOK.md)。

## 1. 支持范围

项目版本门禁最低为 MySQL 5.7.8（原生 JSON）；实际兼容性测试使用 5.7.36 和 8.4。连接时校验版本，并设置 `SET SESSION time_zone = '+00:00'`，不安装触发器或改动全局环境。

| 数据库版本 | 约束实现 |
| --- | --- |
| 5.7.8～8.0.15 | 应用校验 + Alembic 安装的 BEFORE INSERT/UPDATE 触发器 |
| 8.0.16+ | 应用校验 + 原生 CHECK；跨表关系仍由 Repository、外键和审计核对 |

字符集为 `utf8mb4`，兼容排序规则为 `utf8mb4_general_ci`；时间使用 UTC `DATETIME(3)`，金额使用 `DECIMAL`。JSON 值由应用赋值，不依赖 5.7 不支持的 JSON 默认值；品类树使用物化路径，不依赖递归 CTE。

## 2. 运行表与历史迁移

- 当前 SQLAlchemy 元数据只注册 13 张 V2 表，业务不读写 V1。
- Alembic 完整历史链保留 10 张 V1 表；从 base 升级共创建 23 张业务表，另有版本表。运行表数量不等于完整迁移表数量。
- MySQL 5.7 完整历史链安装 40 个校验触发器（V1 14 个、V2 26 个）；不要据此在只含 V2 的环境补建 V1 表。
- `db check` 只要求 13 张 V2 表并核对版本/时区，不是完整的触发器或数据验收；`db audit` 不读取 V1。
- [schema_scope.py](../migrations/schema_scope.py) 将自动生成迁移的反射范围限定为 `v2_`，避免误删旧表或同库其他表。

当前代码 head 为 `b72c910e4f31`。它在旧 head `96524222b3ec` 上扩展产品级证据 `PRODUCT` 和可信无价状态 `AVAILABILITY_ONLY`：5.7 替换两个 V2 表上的 4 个校验触发器，8.0.16+ 替换对应 3 个 CHECK；不增删业务表。已有这些新类型事实时，降级在执行 DDL 前拒绝，不能删除证据强行回退。

实现依据：[mysql_compat.py](../src/device_price_service/db/mysql_compat.py)、[catalog_models.py](../src/device_price_service/db/catalog_models.py)和[迁移文件](../migrations/versions)。

## 3. 设备事务隔离

设备单产品持久化事务使用 `READ COMMITTED`，避免空匹配范围的间隙锁阻塞其他来源；仍保留父 revision 锁、匹配当前读和唯一约束。提交/回滚归还连接后恢复默认隔离级别，政府事务不变。

批次启动已有来源/地区命名锁，因此旧批次查询不再叠加范围 `FOR UPDATE`。这是局部写事务策略，不是全局修改为 RC。

运行前应只读检查 `@@global.binlog_format`、`@@session.binlog_format` 及隔离级别。设备写入要求相应日志格式为 `ROW` 或 `MIXED`；若为 `STATEMENT`，停止设备写入并确认环境方案，不得擅自 `SET GLOBAL`。这一部署前置检查需要操作者执行，当前 CLI 不会代为修改环境。

## 4. 增量部署与回退

1. 明确目标库、当前 revision 和正在运行的写入，暂停目标来源；公司实例不得作为集成测试目标。
2. 获准备份数据库和关联原始文件，在隔离的同版本库恢复核验并演练迁移。
3. 记录触发器定义、SQL_MODE、definer、字符集和排序规则；迁移连接须保留已有会话属性。
4. 使用 Alembic 增量升级，不清库、不改数据库版本，不以 `metadata.create_all` 代替触发器迁移。
5. 核对迁移前后业务数据、DDL、触发器和影响范围，再运行只读检查及证据重放。

MySQL DDL 不能按普通事务自动回滚。应用回退不自动降级数据库；失败迁移应保留现场，在备份副本上验证恢复方案。5.7 的 mysqldump 可能去掉触发器 SQL_MODE 中的 `NO_AUTO_CREATE_USER`：恢复演练须显式记录这一差异，不将其推广为正式迁移允许的属性变更。

双版本命令统一见[开发指南](V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md#3-本地验证)。专用集成测试覆盖真实迁移、约束、并发匹配、回滚后隔离恢复、V2-only 运行和 V1 共存；不得用 SQLite 或仅建 ORM 表来代替。
