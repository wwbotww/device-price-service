# Device Price Service

中国大陆官方商城设备价格采集项目。

V1 只负责从 Apple、华为、小米、OPPO、vivo 中国大陆官方直营商城采集核心设备的 SKU 级原价和当前售价，并写入 MySQL；不提供对话应用或面向业务方的查询 API。

开发前请先阅读：[项目构建及实施方案](docs/PROJECT_IMPLEMENTATION_PLAN.md)。该文档是本项目范围、数据口径、数据库设计、采集流程和验收标准的基准。

公司实例为 MySQL 5.7.36，版本门禁、UTC 会话和约束兼容策略见[MySQL 5.7 兼容说明](docs/MYSQL_57_COMPATIBILITY.md)。

## 当前进度

- 阶段 0：技术范围和显式开关门禁已建立；项目负责人已授权匿名测试 User-Agent 的真实公开页面测试，详见[阶段 0 基线](docs/PHASE_0_BASELINE.md)。
- 阶段 1：项目骨架、10 张表、迁移、种子数据、Repository 和价格历史事务已完成，详见[阶段 1 构建报告](docs/PHASE_1_BUILD_REPORT.md)。
- 阶段 2：通用采集框架、白名单抓取、证据重放、质量校验、批次流水线和调度器已完成，详见[阶段 2 构建报告](docs/PHASE_2_BUILD_REPORT.md)。
- 阶段 3：Apple 与小米 adapter、脱敏 fixture、运行入口和双品牌入库重放已完成；生产 smoke test 和两个调度周期仍受阶段 0 门禁约束，详见[阶段 3 构建报告](docs/PHASE_3_BUILD_REPORT.md)。
- 阶段 4：华为、OPPO、vivo adapter、五品牌注册入口、脱敏 fixture 和三品牌双周期入库重放已完成；五品牌生产验收仍受阶段 0 门禁约束，详见[阶段 4 构建报告](docs/PHASE_4_BUILD_REPORT.md)。
- 阶段 5：最终开发、匿名真实商城采集、公司专用库验收和双版本测试已完成；四品牌全量成功，Apple 手机/平板/电脑成功，Apple Watch 组合总价作为已知限制保留，详见[阶段 5 验收报告](docs/PHASE_5_ACCEPTANCE_REPORT.md)。

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
uv run device-price db audit
RUN_MYSQL_INTEGRATION=1 make test
```

额外验证与公司环境一致的 MySQL 5.7.36：

```bash
make mysql57-up
make test-integration-mysql57
make mysql57-down
```

停止本地数据库：

```bash
make db-down
```

构建并验证生产镜像：

```bash
docker build -t device-price-service:final .
docker run --rm device-price-service:final adapters
```

运行与巡检命令：

```bash
make adapters
make crawl BRAND=HUAWEI MODE=full
make replay RECORD_ID=123
make scheduler
make db-audit
```

`crawl`、`smoke` 和 `scheduler` 默认被 `LIVE_CRAWL_ENABLED=false` 禁用。真实公开页面测试必须由操作者显式开启；默认匿名标识为 `DevicePriceTestBot/1.0`，不要求填写公司或个人联系方式。

部署、报警、备份、恢复和故障处理见[运行与故障处理手册](docs/OPERATIONS_RUNBOOK.md)。
