# Device Price Service

中国大陆价格数据的采集、规范化、质量校验、证据审计与 MySQL 持久化项目，为理赔服务提供价格数据基础；不包含对话应用、查询 API 或理赔计算。

当前使用统一 V2 链路：政府生鲜以商务部 15 个批发品种为主、上海 8 个零售均价品种为补充；电子设备接入 Apple、华为、小米、OPPO、vivo 官方商城。13 张通用业务表保留全品类扩展能力，不代表已覆盖所有品类或全部在售型号。

**阶段性交付已完成，阶段 M 最终验收仍有遗留项。** 最近一次公司验收发现 6 组 iMac 重复身份待授权修复，另有 3 个 Apple Watch 组合总价采集缺口。数据规模、验证时点和后续事项统一见[项目状态](docs/PROJECT_STATUS.md)。

## 文档入口

完整导航见 [docs/README.md](docs/README.md)。

- 运行与验收：[运行手册](docs/OPERATIONS_RUNBOOK.md)。
- 继续开发：[范围与开发指南](docs/V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md)。
- 理解数据：[数据库设计](docs/V2_GENERAL_CATALOG_DATABASE_DESIGN.md)、[连接器契约](docs/V2_CONNECTOR_CONTRACT.md)、[生鲜规则](docs/V2_FRESH_FOOD_RULES.md)。
- 追溯旧方案：[历史归档](docs/archive/README.md)，不作为当前操作指南。

## 本地快速开始

需要 Python 3.11～3.13、uv 和 Docker。下列初始化命令只用于本地开发库；已有 `.env` 时不要覆盖，先核对 `MYSQL_*` 指向 `127.0.0.1:3307/device_price`。公司凭据不得写入 `.env` 或仓库。

```bash
# 仅在尚无 .env 时复制本地示例
cp -n .env.example .env
make setup
make db-up
# 等待 docker compose ps 显示 mysql 为 healthy 后再执行
make migrate
make seed-v2-devices
make seed-v2-government
make db-check
make catalog-sources
make lint
make test-unit
```

政府种子已包含生鲜品类，无需再执行 `seed-v2-fresh`；种子不采集价格，新来源默认禁用。华为、小米等浏览器采集需先执行 `make browser-install`。仅查看来源列表不需要 MySQL 或浏览器。

真实访问默认关闭。需要抓取时，先按[运行手册](docs/OPERATIONS_RUNBOOK.md)确认授权、来源开关和目标库，再执行 `catalog smoke/crawl`。项目默认手工运行，不自动启动调度，也不保证政府发布频率或现有价格实时有效。

MySQL 5.7/8.x 专用库测试、镜像和部署步骤见[开发指南](docs/V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md#3-本地验证)与[兼容说明](docs/MYSQL_57_COMPATIBILITY.md)。V1 业务入口已删除，历史物理表保留但不参与当前采集；不要使用归档中的旧命令。
