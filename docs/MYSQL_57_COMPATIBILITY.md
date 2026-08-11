# MySQL 5.7.36 兼容说明

> 建立日期：2026-08-11
> 目标实例：公司内网 MySQL 5.7.36
> 目标库：`device_price`
> 当前账号：暂用 root；不得把密码写入 Git、镜像或日志

## 1. 兼容范围

项目最低支持 MySQL 5.7.8，以确保原生 JSON 可用；公司实例为 5.7.36。MySQL 8.x 继续受支持。

连接建立时程序会：

1. 查询并校验服务端版本；
2. 拒绝低于 5.7.8 或无法识别的版本；
3. 执行 `SET SESSION time_zone = '+00:00'`；
4. 对 MySQL 5.7～8.0.15 使用触发器约束模式；
5. 对 MySQL 8.0.16+ 使用原生 `CHECK` 约束模式。

## 2. 数据约束

MySQL 5.7 解析但不执行 `CHECK`。迁移 `6f2a91d4c8b7` 会在旧版本上为以下表建立 `BEFORE INSERT/UPDATE` 触发器：

- `product`；
- `sales_channel`；
- `crawl_run`；
- `sku`；
- `official_offer`；
- `price_current`；
- `price_history`。

触发器覆盖状态枚举、CNY/CN/官方直营口径、非负计数、正金额、原价语义和价格历史时间区间。应用层 Pydantic、质量校验和 Repository 事务仍然保留。

## 3. 字符集与时间

- 字符集固定为 `utf8mb4`；
- 兼容排序规则使用 `utf8mb4_general_ci`；
- 业务和审计时间统一按 UTC 写入 `DATETIME(3)`；
- 不依赖服务器 `SYSTEM` 时区；每个物理连接都显式设置 UTC。

## 4. 测试

```bash
make mysql57-up
make test-integration-mysql57
make mysql57-down
```

MySQL 5.7 集成测试必须使用专用 `device_price_test`，禁止把 `TEST_MYSQL_*` 指向公司 `device_price` 或其他已有业务库。测试会执行建表、删表、迁移降级和数据写入。

默认 MySQL 8.4 测试继续保留，用于验证双版本兼容性。

## 5. 公司环境上线顺序

1. 确认 `device_price` 是空库；
2. 在隔离 MySQL 5.7.36 完成完整测试；
3. 通过内存注入凭据运行 `alembic upgrade head`；
4. 核对 10 张业务表、`alembic_version` 和 14 个兼容触发器；
5. 运行幂等种子；
6. 执行 `device-price db check`，确认 UTC 和兼容模式；
7. 生产采集仍须通过阶段 0 门禁。

暂用 root 只是一项部署决策，不改变“不落盘、不提交、不记录密码”的密钥规则。后续具备条件时仍应切换到仅授权 `device_price.*` 的账号。

## 6. 公司环境验收记录

2026-08-11 已按上述顺序完成首次部署验收：

- 服务端版本：MySQL `5.7.36-log`；
- 目标库：`device_price`，`utf8mb4_general_ci`；
- Alembic 版本：`6f2a91d4c8b7`；
- 业务表：10 张，另有 Alembic 版本表；
- MySQL 5.7 校验触发器：14 个；
- 应用连接会话时区：`+00:00`；
- 基础数据：5 个品牌、6 个分类、5 个官方直营渠道；
- 产品、SKU、offer、当前价格、价格历史、采集批次和采集记录：均为 0；
- 迁移前后对 50 个其他非系统 schema 逐库比较表数量，结果一致。

公司实例未运行会降级迁移、删除表或写入构造业务数据的集成测试；阶段 5 完成后的 79 项单元测试全部通过，13 项数据库集成测试分别在本机专用 MySQL 8.4 和 MySQL 5.7.36 容器全部通过。
