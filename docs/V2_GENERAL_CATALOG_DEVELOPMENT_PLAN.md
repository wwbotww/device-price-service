# 范围与开发指南

本文是当前开发基准，不再按阶段叠加进度。验收和待办见[项目状态](PROJECT_STATUS.md)，旧路线与分阶段实施计划见[归档](archive/README.md)。

## 1. 目标与边界

系统面向理赔服务提供可追溯的价格数据，只负责采集、规范化、校验、审计和 MySQL 持久化。已接入政府生鲜和五品牌官方设备，保留全品类的通用结构；不提供理赔计算、对话应用、价格推荐或查询 API。

| 维度 | 当前约定 |
| --- | --- |
| 来源 | 商务部、上海发改委政府公开数据；Apple、华为、小米、OPPO、vivo 中国大陆官方商城 |
| 品类 | 政府生鲜精确白名单；手机、平板、笔记本、台式机、手表 |
| 地区与币种 | 中国大陆、CNY；只保存来源明确的全国/地区范围，不选择理赔适用地 |
| 价格 | 政府零售/批发发布值分开；设备具体规格的官方直接无条件价 |
| 原价与费用 | 原价须有明确证据，缺失为空；不附加运费、另收税费和额外包装费，不反推商品内含税或固有包装成本 |
| 时间 | 点时事实；政府使用来源数据日期，动态设备使用抓取时点；下载与价格时间分开 |
| 更新 | 默认按需手工重采；设备可显式使用已有轻量调度，政府不自动调度 |

不接入京东、淘宝、拼多多、盒马、叮咚等商业平台或个人站；相关枚举和字段只是结构能力，不表示连接器已实现。不增加 Redis、消息队列、PDF/OCR、分库分表、动态规则引擎或运行监控平台。政府品种、单位和地区映射以[生鲜规则](V2_FRESH_FOOD_RULES.md)为准。

## 2. 架构与代码入口

```text
CLI（catalog / db / 可选 scheduler）
  → 来源连接器：产品多 SKU 或政府批量文档
  → 共用 HTTP / 浏览器抓取 + 原始证据存储
  → 共用行准备、品类规范化、价格质量门禁
  → 完整产品 / 完整文档事务
  → 13 张 V2 表；只读 replay / audit 反查证据
```

| 职责 | 实现入口 |
| --- | --- |
| 命令与运行时装配 | [cli.py](../src/device_price_service/cli.py)、[runtime.py](../src/device_price_service/runtime.py) |
| 产品/数据集契约、注册 | [catalog.py](../src/device_price_service/crawlers/catalog.py)、[catalog_builtin.py](../src/device_price_service/crawlers/catalog_builtin.py) |
| 站点解析 | [crawlers](../src/device_price_service/crawlers)；品牌/网站差异只留在本层 |
| 来源无关的品类规则 | [normalization](../src/device_price_service/normalization) |
| 唯一静态门禁 | [catalog_preparation.py](../src/device_price_service/services/catalog_preparation.py)、[catalog_price_policy.py](../src/device_price_service/validation/catalog_price_policy.py) |
| 产品/文档流水线与设备保护 | [catalog_crawl_pipeline.py](../src/device_price_service/services/catalog_crawl_pipeline.py)、[catalog_device_guards.py](../src/device_price_service/services/catalog_device_guards.py) |
| 模型、持久化和迁移 | [catalog_models.py](../src/device_price_service/db/catalog_models.py)、[catalog_repositories.py](../src/device_price_service/db/catalog_repositories.py)、[migrations](../migrations) |
| 证据、只读重放和审计 | [artifact_store.py](../src/device_price_service/services/artifact_store.py)、[replay_service.py](../src/device_price_service/services/replay_service.py)、[database_audit.py](../src/device_price_service/services/database_audit.py) |

继续使用 [pyproject.toml](../pyproject.toml) 中已有的 Python、httpx、Playwright、xlrd、Pydantic、SQLAlchemy、Alembic、Typer、APScheduler 和 pytest。政府 HTML/XLS 走 HTTP；华为、小米的浏览器抓取需要 Chromium。无需为来源新增一套写库流程。

运行时只注册 13 张 `v2_` 表。完整 Alembic 历史链还会创建 10 张 V1 表，但当前业务不读写它们；不新增 V1 包装层、双写、表改名或删旧表迁移。模型、约束和事务细节见[数据库设计](V2_GENERAL_CATALOG_DATABASE_DESIGN.md)。

## 3. 本地验证

本地初始化见[根 README](../README.md#本地快速开始)。以下测试不会访问真实来源；集成测试会清空专用 `device_price_test`，禁止连接公司实例或其他业务库，也不能用 SQLite 替代 MySQL 验证。

```bash
make lint
make test-unit

# MySQL 8.4，等待容器 healthy 后执行
make db-up
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3307 TEST_MYSQL_DATABASE=device_price_test make test-integration

# MySQL 5.7.36，同样仅使用本地专用测试库
make mysql57-up
RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_HOST=127.0.0.1 \
  TEST_MYSQL_PORT=3308 TEST_MYSQL_DATABASE=device_price_test make test-integration
```

账号默认使用 Docker 示例账号；不要继承指向公司环境的 `TEST_MYSQL_*`。涉及容器、入口或运行权限时补充：

```bash
docker build -t device-price-service:local .
docker run --rm --network none device-price-service:local catalog sources
```

停止本地实例可用 `make db-down`、`make mysql57-down`；这些命令不删除命名数据卷。只有文档变更时检查本地链接、命令参数和代码一致性，不把未执行的数据库或真实来源测试写成通过。

## 4. 后续改动顺序

1. 明确当前问题、调用方和验收方式；先看相关代码、测试、文档，避免无关重构。
2. 新增来源先确认访问授权、公开证据、稳定身份、地区、时间、计价性质和单位，再按[连接器契约](V2_CONNECTOR_CONTRACT.md)实现。
3. 新增生鲜品种优先扩展精确白名单和规则 fixture；有真实新边界才增加规则或连接器，不为每个品种建表。
4. 使用脱敏 fixture 验证缺字段、未知单位、条件价、重复身份、证据损坏、失败和重放；真实原文件不得提交。
5. 涉及持久化时验证幂等、乱序、同点冲突、规格切换、失败回滚、当前价和历史一致性；模型、迁移、Repository 或锁变更必须通过两种 MySQL。
6. 在获得授权且配置满足门禁后做低频 smoke；写公司库前另行确认目标、备份和影响范围。不规避登录、验证码或签名限制。
7. 同步更新对应指南；实际验收和遗留项更新到项目状态，详细过程才写归档报告。

数据正确性优先于数量：未知规格、价格或库存不猜测；异常批次不覆盖可信数据；原始证据、事实和批次之间始终能追溯。人工复核、跨平台智能匹配和自动修复并非当前已交付能力。
