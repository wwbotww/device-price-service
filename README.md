# Device Price Service

中国大陆价格数据采集与 MySQL 持久化项目。

V1 已完成 Apple、华为、小米、OPPO、vivo 中国大陆官方直营商城设备价格的 demo。当前 V2 保留全品类通用数据框架，但开发范围已收敛为**政府网站公开的生鲜食品价格数据**。项目不提供对话应用、理赔计算或面向业务方的查询 API。

当前开发先阅读：[V2 生鲜政府价格数据开发与实施计划](docs/V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md)。V1 历史基线见[设备官方价格采集项目方案](docs/PROJECT_IMPLEMENTATION_PLAN.md)。

V2 的 13 张通用表和设计取舍见 [V2 全品类价格采集数据库设计](docs/V2_GENERAL_CATALOG_DATABASE_DESIGN.md)。通用结构继续保留，当前只实现 `PUBLIC_DATA` 政府来源、零售/批发均价和生鲜品类规则，不为尚未接入的全品类来源增加实现复杂度。

阶段 A～H 的通用结构、重采决策、生鲜规则、两个政府来源和公司库验收均已完成。商务部“百家日报”是当前主来源，支持 15 个跨地区批发品种；上海市发展改革委 8 个主副食品零售均价作为补充。详见[阶段 G 报告](docs/V2_PHASE_G_BUILD_REPORT.md)和[阶段 H 公司库验收报告](docs/V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md)。本项目按 demo 交付，V2 只提供高质量手工采集，不建设常驻调度、告警平台或连续运行监控。连接器边界见 [V2 通用来源连接器契约](docs/V2_CONNECTOR_CONTRACT.md)，生鲜口径见 [V2 生鲜品类规则说明](docs/V2_FRESH_FOOD_RULES.md)。

公司实例为 MySQL 5.7.36，版本门禁、UTC 会话和约束兼容策略见[MySQL 5.7 兼容说明](docs/MYSQL_57_COMPATIBILITY.md)。

## 当前进度

- 阶段 0：技术范围和显式开关门禁已建立；项目负责人已授权匿名测试 User-Agent 的真实公开页面测试，详见[阶段 0 基线](docs/PHASE_0_BASELINE.md)。
- 阶段 1：项目骨架、10 张表、迁移、种子数据、Repository 和价格历史事务已完成，详见[阶段 1 构建报告](docs/PHASE_1_BUILD_REPORT.md)。
- 阶段 2：通用采集框架、白名单抓取、证据重放、质量校验、批次流水线和调度器已完成，详见[阶段 2 构建报告](docs/PHASE_2_BUILD_REPORT.md)。
- 阶段 3：Apple 与小米 adapter、脱敏 fixture、运行入口和双品牌入库重放已完成；生产 smoke test 和两个调度周期仍受阶段 0 门禁约束，详见[阶段 3 构建报告](docs/PHASE_3_BUILD_REPORT.md)。
- 阶段 4：华为、OPPO、vivo adapter、五品牌注册入口、脱敏 fixture 和三品牌双周期入库重放已完成；五品牌生产验收仍受阶段 0 门禁约束，详见[阶段 4 构建报告](docs/PHASE_4_BUILD_REPORT.md)。
- 阶段 5：最终开发、匿名真实商城采集、公司专用库验收和双版本测试已完成；四品牌全量成功，Apple 手机/平板/电脑成功，Apple Watch 组合总价作为已知限制保留，详见[阶段 5 验收报告](docs/PHASE_5_ACCEPTANCE_REPORT.md)。
- V2 阶段 A：13 张全品类影子表、MySQL 5.7 兼容约束、来源版本、点时价格与当前投影事务已完成；未回填 V1、未接入新平台、未操作公司数据库。
- V2 阶段 B：V1 已确认为可重建 demo，取消一次性回填器；影子期保留 V1 作为回退，切换后从授权来源重新采集。
- V2 阶段 C：通用连接器 DTO、品类规则接口、中央价格门禁和 V2 端到端采集流水线已完成；MySQL 5.7/8.x 专用库验证通过。
- V2 阶段 D：红富士苹果、包装鸡蛋、明确部位猪肉和公共市场均价的规则、单位换算、脱敏 fixture 与双周期重放已完成。
- V2 阶段 E：来源路线决策已完成。大型电商 HTML 入口未通过门禁，当前阶段不再等待平台 API；生鲜数据源正式收敛为政府公开数据。
- V2 阶段 F：上海市主要主副食品价格 `.xls` 已完成来源日期、零售均价、批量数据集解析、同日修订和本地 MySQL 闭环；真实只读 smoke 与双版本数据库验收通过。
- V2 阶段 G：商务部“百家日报”连接器已完成跨地区市场批发价解析、稳定市场 ID、来源日期、行级证据与精准同日修订；首批 5 个真实页面解析 429 条价格。
- V2 阶段 H：商务部白名单扩展为 15 个品种；公司 `device_price` 在完整备份后重建至当前 head，写入商务部 1,354 条批发价和上海 8 条零售均价；重复采集、证据完整性、其他 schema 不变及 MySQL 5.7 约束均验收通过，未启动 V2 scheduler。

当前 Alembic head 和 `device-price db check` 以 V1 10 张表与 V2 13 张表同时存在为前提。公司库已经重建为该结构：V1 表保留但数据为空，实际政府价格只写 `v2_` 表；这是现有迁移链的最小实现，不增加删表迁移或表改名。公司实例仍暂用 root，凭据不写入仓库或 `.env`。

## 本地开发

公司 MySQL 不可用时，使用 Docker MySQL 8.4 和独立的 `device_price_test` 数据库验证，不能用 SQLite 替代 MySQL 兼容性测试。

```bash
cp .env.example .env
make setup
make browser-install
make db-up
make migrate
make seed
make seed-v2-fresh
make seed-v2-government
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

V2 政府来源命令：

```bash
make catalog-sources
LIVE_CRAWL_ENABLED=true make catalog-smoke CHANNEL=SH_FGW_FRESH_RETAIL

# 商务部：默认依次验证 15 个白名单品种，也可用 COMMODITY=CUCUMBER 只跑一个
LIVE_CRAWL_ENABLED=true make catalog-smoke \
  CHANNEL=MOFCOM_FRESH_WHOLESALE COMMODITY=ALL

# 首次初始化时显式启用来源；公司库已完成该步骤
uv run device-price db seed-v2-government --enable
LIVE_CRAWL_ENABLED=true make catalog-crawl CHANNEL=SH_FGW_FRESH_RETAIL
LIVE_CRAWL_ENABLED=true make catalog-crawl \
  CHANNEL=MOFCOM_FRESH_WHOLESALE COMMODITY=CUCUMBER
```

`db seed-v2-government` 默认只建立 8 条生鲜品类/规则引用和两个禁用的政府来源，不访问网络。`catalog smoke` 不写 MySQL；`catalog crawl` 只有在门禁开启且数据库来源已显式启用时才会写入 V2 表。商务部省略 `--commodity` 时依次运行 15 个独立数据集；每个页面分别保存证据，不合并成虚假大文件。V2 不注册常驻调度任务。

`crawl`、`smoke` 和 `scheduler` 默认被 `LIVE_CRAWL_ENABLED=false` 禁用。真实公开页面测试必须由操作者显式开启；默认匿名标识为 `DevicePriceTestBot/1.0`，不要求填写公司或个人联系方式。

部署、报警、备份、恢复和故障处理见[运行与故障处理手册](docs/OPERATIONS_RUNBOOK.md)。
