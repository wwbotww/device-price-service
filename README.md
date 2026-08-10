# Device Price Service

中国大陆官方商城设备价格采集项目。

V1 只负责从 Apple、华为、小米、OPPO、vivo 中国大陆官方直营商城采集核心设备的 SKU 级原价和当前售价，并写入 MySQL；不提供对话应用或面向业务方的查询 API。

开发前请先阅读：[项目构建及实施方案](docs/PROJECT_IMPLEMENTATION_PLAN.md)。该文档是本项目范围、数据口径、数据库设计、采集流程和验收标准的基准。

## 当前进度

- 阶段 0：技术范围和合规门禁已建立；生产采集仍需公司审批，详见[阶段 0 基线](docs/PHASE_0_BASELINE.md)。
- 阶段 1：项目骨架、10 张表、迁移、种子数据、Repository 和价格历史事务已完成，详见[阶段 1 构建报告](docs/PHASE_1_BUILD_REPORT.md)。
- 阶段 2：通用采集框架、白名单抓取、证据重放、质量校验、批次流水线和调度器已完成，详见[阶段 2 构建报告](docs/PHASE_2_BUILD_REPORT.md)。
- 阶段 3：Apple 与小米 adapter、脱敏 fixture、运行入口和双品牌入库重放已完成；生产 smoke test 和两个调度周期仍受阶段 0 门禁约束，详见[阶段 3 构建报告](docs/PHASE_3_BUILD_REPORT.md)。

## 本地开发

公司 MySQL 不可用时，使用 Docker MySQL 8.4 和独立的 `device_price_test` 数据库验证，不能用 SQLite 替代 MySQL 兼容性测试。

```bash
cp .env.example .env
make setup
make browser-install
make db-up
make migrate
make seed
make db-check
RUN_MYSQL_INTEGRATION=1 make test
```

停止本地数据库：

```bash
make db-down
```

构建并验证生产镜像：

```bash
docker build -t device-price-service:phase3 .
docker run --rm device-price-service:phase3 adapters
```

阶段 3 运维命令：

```bash
make adapters
make crawl BRAND=APPLE MODE=full
make replay RECORD_ID=123
make scheduler
```

`crawl` 和 `scheduler` 默认被 `LIVE_CRAWL_ENABLED=false` 禁用。只有阶段 0 的公司授权、User-Agent 和首次 smoke test 审批全部完成后才能开启。
