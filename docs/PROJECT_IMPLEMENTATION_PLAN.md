# 设备官方价格采集项目：构建及实施方案

> 文档状态：V1 基准方案；阶段 0～5 工程开发和最终验收已完成；Apple Watch 组合总价为已知限制
> 最后更新：2026-08-11
> 适用范围：项目设计、开发、测试、部署和验收
> 变更原则：价格口径、表结构、官方渠道范围发生变化时，代码修改前先更新本文档

## 1. 项目目标

建设一套可持续运行的设备价格采集程序，从中国大陆官方直营商城采集 Apple、华为、小米、OPPO、vivo 核心设备的 SKU 级价格及销售状态，规范化后写入 MySQL，并保留完整的价格变更历史和采集审计信息。

V1 的完成标准不是“能抓到页面上的一个价格”，而是：

1. 能稳定发现范围内的产品及其可销售 SKU；
2. 能明确区分产品、SKU、销售报价、原价和当前售价；
3. 能识别并排除国补、优惠券、会员、以旧换新等有条件价格；
4. 能幂等地写入当前价格，并记录价格变化历史；
5. 页面结构变化、抓取失败或数据异常时，不污染已有正确数据；
6. 每条价格都能追溯到官方页面、采集时间和采集批次。

## 2. V1 范围

### 2.1 包含范围

| 维度 | V1 约束 |
| --- | --- |
| 地区 | 中国大陆 |
| 币种 | 人民币 CNY |
| 品牌 | Apple、华为、小米、OPPO、vivo |
| 品类 | 手机、平板、笔记本电脑、台式电脑、手表 |
| 渠道 | 品牌中国大陆官方直营网页商城 |
| 粒度 | 可销售 SKU，例如容量、内存、颜色、网络版本组合 |
| 价格 | 页面明确展示的原价、当前无条件直接购买售价 |
| 状态 | 在售、缺货、预约、未开售、下架等 |
| 存储 | 公司环境 MySQL 5.7.36；同时保持 MySQL 8.x 兼容 |

V1 官方渠道白名单：

| 品牌 | 允许域名/路径 | 说明 |
| --- | --- | --- |
| Apple | `www.apple.com.cn/shop/` | Apple 中国大陆在线商店 |
| 华为 | `www.vmall.com/`、`m.vmall.com/`、`item.vmall.com/`、`openapi.vmall.com/` | 华为商城页面、商品详情与页面公开内容接口 |
| 小米 | `www.mi.com/shop/` | 小米中国大陆商城 |
| OPPO | `www.opposhop.cn/` | 只采集 OPPO 商城，不跟随京东、天猫等外链 |
| vivo | `shop.vivo.com.cn/` | vivo 中国大陆官方商城 |

域名白名单是程序的硬约束。重定向到白名单以外的 URL 时立即停止，不采集、不入库。

### 2.2 不包含范围

- 对话应用、自然语言检索和面向业务方的查询 API；
- 京东、淘宝、天猫、拼多多、苏宁等第三方平台，即使店铺名称包含“官方”；
- 线下门店价、教育优惠、企业采购价、员工价和地区门店价；
- 国补价、优惠券价、会员价、直播价、以旧换新价、分期月供和赠品折算价；
- 二手、官翻、展示机、合约机、运营商套餐和配件捆绑价；
- 用户评论、销量、账户、订单以及其他个人信息；
- `starting_price`、产品列表页“XX 元起”等不对应具体 SKU 的价格；
- Redis、消息队列和分布式任务系统。

### 2.3 子品牌处理

V1 按品牌名称严格限定为 Apple、华为、小米、OPPO、vivo，默认不采集荣耀、REDMI、iQOO、一加等独立展示的子品牌。官方商城中发现这些商品时记录为范围外并跳过。

如果后续决定纳入某个子品牌，应新增独立 `brand` 记录并复用现有架构，不应把其品牌名称改写为母品牌。此调整不需要改变价格表结构。

## 3. 核心概念和价格口径

### 3.1 产品与 SKU

- **产品（Product）**：市场上的一个型号，例如“iPhone 17 Pro”或“HUAWEI MateBook X Pro”。
- **SKU**：官方商城可选择和购买的具体配置组合，例如“256GB、黑色、无合约版”。价格必须归属到 SKU。
- **报价（Offer）**：某 SKU 在某个官方销售渠道中的销售记录。V1 通常一个 SKU 对应一个网页官方报价，但模型允许未来接入官方 App 等其他直营渠道。

颜色即使不影响价格，也属于 SKU 身份的一部分。不得为了减少数据量，把官方 SKU 不同但当前同价的配置合并。

### 3.2 原价 `original_price`

`original_price` 只接受以下官方页面明确展示的值：

- 划线价；
- 官方零售价；
- 建议零售价；
- 页面明确标注为“原价”的价格。

同时保存 `original_price_type`：

| 值 | 含义 |
| --- | --- |
| `CROSSED_OUT` | 页面划线价 |
| `MSRP` | 官方建议零售价/官方零售价 |
| `EXPLICIT_ORIGINAL` | 页面明确标注的原价 |
| `NONE` | 页面没有可确认的原价 |

页面没有原价时必须写 `NULL`，不得用当前售价补齐，也不得从新闻稿、搜索摘要或其他 SKU 推算。

### 3.3 当前售价 `current_price`

`current_price` 是在以下条件下，用户选择该 SKU 后页面展示的官方直接销售金额：

- 不需要领取优惠券；
- 不要求登录会员或达到会员等级；
- 不依赖国家/地方补贴资格；
- 不提交旧设备抵扣；
- 不以分期月供替代商品总价；
- 不把赠品、积分或服务权益折算成现金；
- 不包含可选保险、AppleCare、延保和配件价格。

页面只展示“到手价”“补贴价”而无法确认无条件销售价时，`current_price` 写 `NULL`，该记录进入数据质量告警，不得猜测。

### 3.4 预约、定金和组合销售

- 预约但尚未公布完整价格：状态记为 `RESERVATION`，价格为 `NULL`；
- 预售且明确展示商品总价：状态记为 `PRE_SALE`，保存商品总价，不把定金当作售价；
- 仅展示定金、尾款不明确：价格为 `NULL`；
- 强制绑定套餐或服务：V1 跳过；
- 可选套餐：只记录不含可选项的设备本体价格。

### 3.5 销售状态

统一状态值如下：

| 状态 | 含义 |
| --- | --- |
| `ON_SALE` | 可直接购买 |
| `OUT_OF_STOCK` | 商品在售但当前缺货 |
| `RESERVATION` | 仅预约，未公布或未开放购买 |
| `PRE_SALE` | 已公布总价的预售 |
| `COMING_SOON` | 即将开售 |
| `OFF_SHELF` | 官方确认下架 |
| `UNKNOWN` | 页面异常，不能确认；不得自动当作下架 |

价格和销售状态必须一起采集。缺货不等于价格为零，下架也不等于删除历史记录。

## 4. 总体架构

```text
官方分类页/sitemap
        │
        ▼
商品发现 Discovery
        │
        ▼
品牌适配器 Adapter ──► HTTP 抓取优先 ──► Playwright 兜底
        │
        ▼
原始数据解析 Parse
        │
        ▼
统一模型 Normalize
        │
        ▼
业务规则与数据质量 Validate
        │
        ▼
MySQL 事务写入 Persist
        ├── 当前价格
        ├── 价格历史
        └── 采集审计
```

V1 不建设 HTTP 业务服务。程序由命令行入口和常驻调度进程组成：

- 命令行用于本地开发、单品牌运行、重放和运维检查；
- APScheduler 常驻进程负责定时启动品牌任务；
- MySQL 命名锁防止同一品牌任务重叠运行；
- 单个任务内部使用受控异步并发，不依赖 Redis。

## 5. 推荐技术栈

| 领域 | 选择 | 用途 |
| --- | --- | --- |
| 语言 | Python | 采集、解析、调度和数据写入 |
| 普通请求 | `httpx` | 连接池、超时、重试和异步请求 |
| HTML 解析 | `selectolax` 或 `lxml` | DOM 解析 |
| 浏览器兜底 | Playwright | 必须执行 JavaScript 才能获得的公开商品数据 |
| 数据模型 | Pydantic | 统一采集 DTO 和校验 |
| ORM | SQLAlchemy 2.x | 数据库访问和事务 |
| 迁移 | Alembic | 数据库版本管理 |
| 调度 | APScheduler | 单进程定时任务 |
| CLI | Typer | 手工运行和调试 |
| 重试 | Tenacity | 对明确可重试错误进行退避重试 |
| 测试 | pytest | 单元、解析快照、数据库集成测试 |
| 代码质量 | Ruff + mypy | 格式、静态检查和类型检查 |
| 部署 | Docker Compose | MySQL 与 crawler-scheduler |

依赖版本在 `pyproject.toml` 中固定到兼容范围，锁文件提交仓库。Playwright 浏览器版本必须与 Python 包版本一致。

## 6. 计划目录结构

```text
device-price-service/
├── README.md
├── pyproject.toml
├── uv.lock                         # 或项目选定包管理器的锁文件
├── .env.example
├── .gitignore
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── migrations/
│   └── versions/
├── docs/
│   └── PROJECT_IMPLEMENTATION_PLAN.md
├── src/device_price_service/
│   ├── __init__.py
│   ├── config.py
│   ├── logging.py
│   ├── cli.py
│   ├── scheduler.py
│   ├── db/
│   │   ├── session.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── models.py
│   │   └── price_policy.py
│   ├── crawlers/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── apple.py
│   │   ├── huawei.py
│   │   ├── xiaomi.py
│   │   ├── oppo.py
│   │   └── vivo.py
│   ├── discovery/
│   │   └── service.py
│   ├── fetchers/
│   │   ├── http.py
│   │   └── browser.py
│   ├── normalization/
│   │   ├── product.py
│   │   ├── sku.py
│   │   └── money.py
│   ├── validation/
│   │   └── rules.py
│   └── services/
│       ├── crawl_service.py
│       └── persistence_service.py
├── tests/
│   ├── fixtures/                   # 脱敏后的官方页面/JSON 样本
│   ├── unit/
│   ├── parser/
│   └── integration/
└── var/
    └── raw/                        # 本地原始证据，不提交 Git
```

领域模型不得直接依赖某品牌页面字段。品牌差异必须止于 adapter 输出之前。

## 7. 品牌适配器设计

### 7.1 统一接口

每个适配器实现以下能力：

```python
class BrandAdapter(Protocol):
    brand_code: str

    async def discover(self) -> list[DiscoveredProduct]: ...
    async def fetch_product(self, item: DiscoveredProduct) -> RawProductPage: ...
    def parse_product(self, raw: RawProductPage) -> ParsedProduct: ...
    def normalize(self, parsed: ParsedProduct) -> NormalizedProduct: ...
```

`NormalizedProduct` 至少包含：

- 品牌、品类、产品名、系列名、官方产品 ID；
- 官方产品 URL；
- 所有可识别 SKU；
- SKU 官方 ID、配置属性和稳定指纹；
- 每个 SKU 的原价、当前售价、销售状态；
- 价格来源页面、采集时间和证据哈希。

### 7.2 数据获取优先级

每个站点按照以下顺序选择数据源：

1. 经公司确认可使用的官方数据接口或数据文件；
2. 官方 sitemap、分类页、商品页；
3. 页面内嵌 JSON、JSON-LD 或服务端渲染数据；
4. 页面公开加载且经合规确认可使用的商品接口；
5. Playwright 渲染后的 DOM 或网络响应。

禁止破解签名、绕过验证码、伪造登录态或规避访问控制。出现此类要求时停止该站点开发并升级处理。

### 7.3 品牌实施注意点

- **Apple**：从官方商城导航或 sitemap 发现产品；必须进入配置选择数据，产品列表“起售价”不入库。
- **华为**：区分标准价格、活动文案、优惠券和国补展示；保留官方商品与 SKU ID。
- **小米**：从核心设备分类发现商品；排除 REDMI 和生态链商品；详情页动态配置以 SKU 数据为准。
- **OPPO**：只允许 `opposhop.cn`；排除一加和外部官方旗舰店链接。
- **vivo**：只采集 vivo 主品牌；排除 iQOO 和商城中的第三方配件品牌。

具体 CSS selector、JSON 路径和页面样本写在对应 adapter 及测试 fixture 中，不写进本文档，避免页面改版导致设计文档频繁失效。

## 8. 数据库设计：共 10 张表

数据库使用 `utf8mb4` 字符集；金额使用 `DECIMAL(12,2)`，禁止使用 `FLOAT` 或 `DOUBLE`；业务时间以 UTC 写入 `DATETIME(3)`，展示时再转换时区。

所有主键建议使用 `BIGINT UNSIGNED` 自增。业务枚举采用 `VARCHAR` 并由应用层枚举和数据库 `CHECK` 共同约束，避免 MySQL `ENUM` 难以演进。

### 8.1 `brand`：品牌

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `code` | VARCHAR(32) | UNIQUE；`APPLE/HUAWEI/XIAOMI/OPPO/VIVO` |
| `name_zh` | VARCHAR(64) | 中文展示名 |
| `name_en` | VARCHAR(64) | 英文展示名 |
| `enabled` | BOOLEAN | 是否启用采集 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

### 8.2 `category`：品类

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `parent_id` | BIGINT UNSIGNED | 自关联，可为空 |
| `code` | VARCHAR(32) | UNIQUE；如 `PHONE/TABLET/COMPUTER/LAPTOP/DESKTOP/WATCH` |
| `name_zh` | VARCHAR(64) | 中文名 |
| `enabled` | BOOLEAN | 是否属于当前采集范围 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

### 8.3 `product`：产品型号

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `brand_id` | BIGINT UNSIGNED | FK → `brand.id` |
| `category_id` | BIGINT UNSIGNED | FK → `category.id` |
| `official_product_id` | VARCHAR(128) | 官方产品标识 |
| `name` | VARCHAR(255) | 规范产品名 |
| `series_name` | VARCHAR(128) | 系列，可为空 |
| `model_number` | VARCHAR(128) | 官方型号，可为空 |
| `official_url` | VARCHAR(1024) | 官方产品页规范 URL |
| `lifecycle_status` | VARCHAR(32) | `ACTIVE/INACTIVE/UNKNOWN` |
| `first_seen_at` | DATETIME(3) | 首次发现 |
| `last_seen_at` | DATETIME(3) | 最近确认存在 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

唯一约束：`(brand_id, official_product_id)`。如果网站没有稳定产品 ID，使用经过版本控制的规范 URL 指纹作为官方产品 ID，并在适配器测试中固定生成规则。

### 8.4 `sku`：可销售配置

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `product_id` | BIGINT UNSIGNED | FK → `product.id` |
| `official_sku_id` | VARCHAR(128) | 官方 SKU 标识 |
| `name` | VARCHAR(255) | SKU 展示名 |
| `color` | VARCHAR(128) | 颜色，可为空 |
| `capacity` | VARCHAR(64) | 容量，可为空，如 `256GB` |
| `memory` | VARCHAR(64) | 内存，可为空，如 `16GB` |
| `connectivity` | VARCHAR(64) | Wi-Fi/蜂窝/网络版本等 |
| `size` | VARCHAR(64) | 表壳、屏幕等尺寸 |
| `attributes` | JSON | 其余品牌/品类特有属性 |
| `spec_fingerprint` | CHAR(64) | 规范属性 SHA-256 |
| `status` | VARCHAR(32) | `ACTIVE/INACTIVE/UNKNOWN` |
| `first_seen_at` | DATETIME(3) | 首次发现 |
| `last_seen_at` | DATETIME(3) | 最近确认存在 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

优先使用唯一约束 `(product_id, official_sku_id)`。官方 SKU ID 缺失时，使用 `(product_id, spec_fingerprint)` 作为幂等键。

### 8.5 `sales_channel`：官方直营渠道

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `brand_id` | BIGINT UNSIGNED | FK → `brand.id` |
| `code` | VARCHAR(64) | UNIQUE，如 `APPLE_CN_WEB` |
| `name` | VARCHAR(128) | 渠道名 |
| `base_url` | VARCHAR(512) | 基础 URL |
| `allowed_domains` | JSON | 允许访问的精确域名列表 |
| `region_code` | CHAR(2) | 固定 `CN` |
| `currency` | CHAR(3) | 固定 `CNY` |
| `seller_type` | VARCHAR(32) | 固定 `OFFICIAL_DIRECT` |
| `enabled` | BOOLEAN | 是否启用 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

### 8.6 `official_offer`：SKU 官方报价载体

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `sku_id` | BIGINT UNSIGNED | FK → `sku.id` |
| `channel_id` | BIGINT UNSIGNED | FK → `sales_channel.id` |
| `official_offer_id` | VARCHAR(128) | 官方报价/商品 ID，可为空 |
| `source_url` | VARCHAR(1024) | 价格证据页 |
| `availability` | VARCHAR(32) | 统一销售状态 |
| `consecutive_misses` | INT UNSIGNED | 连续未发现次数 |
| `first_seen_at` | DATETIME(3) | 首次发现 |
| `last_seen_at` | DATETIME(3) | 最近成功确认 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

唯一约束：始终设置 `(sku_id, channel_id)`；当 `official_offer_id` 非空时，应用层还需保证 `(channel_id, official_offer_id)` 唯一。不得因一次发现任务缺失就立刻将其标记为下架。

### 8.7 `price_current`：当前价格投影

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `offer_id` | BIGINT UNSIGNED | UNIQUE，FK → `official_offer.id` |
| `currency` | CHAR(3) | 固定 `CNY` |
| `original_price` | DECIMAL(12,2) | 可为空 |
| `original_price_type` | VARCHAR(32) | 原价类型 |
| `current_price` | DECIMAL(12,2) | 可为空 |
| `observed_at` | DATETIME(3) | 本价格最近采集时间 |
| `source_hash` | CHAR(64) | 原始证据哈希 |
| `crawl_run_id` | BIGINT UNSIGNED | FK → `crawl_run.id` |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

`price_current` 只保存每个报价的最新可信状态，用于下游直接读取。价格暂时无法解析时，不得用 `NULL` 覆盖上一条可信价格；失败信息写入采集审计表。

### 8.8 `price_history`：价格变更历史

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `offer_id` | BIGINT UNSIGNED | FK → `official_offer.id` |
| `currency` | CHAR(3) | 固定 `CNY` |
| `original_price` | DECIMAL(12,2) | 可为空 |
| `original_price_type` | VARCHAR(32) | 原价类型 |
| `current_price` | DECIMAL(12,2) | 可为空 |
| `availability` | VARCHAR(32) | 当时销售状态 |
| `valid_from` | DATETIME(3) | 首次观察到该状态 |
| `last_observed_at` | DATETIME(3) | 最近仍观察到该状态 |
| `valid_to` | DATETIME(3) | 状态结束时间，当前记录为空 |
| `source_hash` | CHAR(64) | 首次观察证据哈希 |
| `crawl_run_id` | BIGINT UNSIGNED | FK → `crawl_run.id` |
| `created_at` | DATETIME(3) | 创建时间 |

索引：`(offer_id, valid_from)`、`(offer_id, valid_to)`。同一 `offer_id` 同时最多允许一条 `valid_to IS NULL` 的逻辑当前记录，该约束由事务和集成测试保证。

### 8.9 `crawl_run`：采集批次

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `channel_id` | BIGINT UNSIGNED | FK → `sales_channel.id` |
| `run_type` | VARCHAR(32) | `DISCOVERY/PRICE/FULL/REPLAY` |
| `trigger_type` | VARCHAR(32) | `SCHEDULED/MANUAL` |
| `status` | VARCHAR(32) | `RUNNING/SUCCEEDED/PARTIAL/FAILED/CANCELLED` |
| `adapter_version` | VARCHAR(64) | 代码版本或 Git SHA |
| `started_at` | DATETIME(3) | 开始时间 |
| `finished_at` | DATETIME(3) | 结束时间，可为空 |
| `discovered_count` | INT UNSIGNED | 发现数 |
| `success_count` | INT UNSIGNED | 成功数 |
| `skipped_count` | INT UNSIGNED | 跳过数 |
| `failed_count` | INT UNSIGNED | 失败数 |
| `error_summary` | JSON | 聚合错误信息 |
| `created_at` | DATETIME(3) | 创建时间 |

### 8.10 `crawl_record`：单页面/单实体采集审计

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `crawl_run_id` | BIGINT UNSIGNED | FK → `crawl_run.id` |
| `entity_type` | VARCHAR(32) | `CATEGORY/PRODUCT/SKU/OFFER` |
| `entity_key` | VARCHAR(255) | 官方 ID 或规范 URL 指纹 |
| `request_url` | VARCHAR(1024) | 请求 URL |
| `final_url` | VARCHAR(1024) | 重定向后 URL |
| `fetch_method` | VARCHAR(32) | `HTTP/BROWSER/REPLAY` |
| `http_status` | SMALLINT UNSIGNED | 可为空 |
| `fetch_status` | VARCHAR(32) | 抓取状态 |
| `parse_status` | VARCHAR(32) | 解析状态 |
| `error_code` | VARCHAR(64) | 稳定错误码，可为空 |
| `error_message` | TEXT | 脱敏错误摘要，可为空 |
| `raw_hash` | CHAR(64) | 原始响应 SHA-256，可为空 |
| `raw_path` | VARCHAR(1024) | 原始证据相对路径，可为空 |
| `duration_ms` | INT UNSIGNED | 耗时 |
| `fetched_at` | DATETIME(3) | 抓取时间 |
| `created_at` | DATETIME(3) | 创建时间 |

索引：`(crawl_run_id, parse_status)`、`(entity_type, entity_key)`、`(fetched_at)`。

### 8.11 表关系

```text
brand 1 ── N product N ── 1 category
brand 1 ── N sales_channel
product 1 ── N sku
sku 1 ── N official_offer N ── 1 sales_channel
official_offer 1 ── 1 price_current
official_offer 1 ── N price_history
sales_channel 1 ── N crawl_run
crawl_run 1 ── N crawl_record
```

## 9. 采集与写入流程

### 9.1 任务启动

1. 调度器根据渠道配置启动品牌任务；
2. 获取 MySQL 命名锁，例如 `device-price:APPLE_CN_WEB`；
3. 获取失败说明已有同品牌任务运行，本次记日志后跳过；
4. 创建 `crawl_run`，状态为 `RUNNING`；
5. 任务结束后更新为 `SUCCEEDED`、`PARTIAL` 或 `FAILED` 并释放锁。

进程意外退出后 MySQL 连接关闭，命名锁自动释放。启动时将超过合理运行时长仍为 `RUNNING` 的旧批次标记为 `FAILED`，错误码为 `STALE_RUN`。

### 9.2 商品发现

1. 读取 sitemap 或范围内分类入口；
2. 只保留白名单域名；
3. 过滤品牌和品类；
4. URL 去跟踪参数并规范化；
5. 根据官方产品 ID 或 URL 指纹去重；
6. 生成待抓取产品列表；
7. 为每个发现页面写入 `crawl_record`。

### 9.3 详情抓取和解析

1. 优先使用 HTTP 请求；
2. 检查状态码、内容类型、页面标题和基础结构；
3. 从内嵌结构化数据或 DOM 中解析产品及 SKU；
4. 只有普通请求无法得到公开商品信息时才使用浏览器兜底；
5. 保存原始响应哈希和本地证据文件；
6. 品牌适配器输出统一 DTO，不直接写数据库。

### 9.4 规范化

- 去除价格中的 `¥`、`￥`、`RMB`、千分位和无关空白；
- 所有金额解析为 `Decimal`；
- 容量统一为 `128GB/1TB` 等展示规范，但保留原始属性；
- 颜色保留官方命名，不做主观归并；
- 属性键排序、单位规范化后生成 `spec_fingerprint`；
- URL 删除广告跟踪参数，保留影响 SKU 选择的必要参数；
- 品牌、品类和状态映射到统一枚举。

### 9.5 校验

数据写入前至少执行以下规则：

- 币种必须为 CNY；
- `current_price` 和 `original_price` 若非空，必须大于 0；
- `original_price < current_price` 时不直接拒绝，但标记异常等待人工检查；
- 价格不得来自“起”“低至”“月供”“最高优惠”“以旧换新”等语义；
- SKU 必须有官方 ID 或稳定配置指纹；
- URL 必须属于渠道白名单；
- 单次价格变化超过配置阈值时进入异常确认；
- 解析得到的 SKU 数骤降时，本批次不得批量标记下架。

建议默认异常阈值：同一 SKU 当前售价变化超过 30%，进行一次独立复抓；两次结果一致才发布，否则保留旧可信价格并报警。阈值做成环境配置，不写死在 adapter。

### 9.6 事务写入

以单个产品为事务边界：

1. upsert `product`；
2. upsert 所有 `sku`；
3. upsert `official_offer`；
4. 锁定该报价的 `price_current`；
5. 第一次发现：插入 `price_current` 和一条开放的 `price_history`；
6. 价格或销售状态变化：关闭旧历史、插入新历史、更新当前投影；
7. 完全未变化：只更新 `price_current.observed_at` 和历史的 `last_observed_at`；
8. 提交事务后将对应 `crawl_record` 标记成功。

数据库死锁或临时连接错误允许有限重试；校验失败和唯一键设计错误不可盲目重试。

### 9.7 下架判定

为了避免分类页临时故障造成批量误下架：

- 一次未发现：`consecutive_misses + 1`，保持原状态；
- 连续三次成功的完整发现任务均未出现，再请求原详情页确认；
- 详情页明确下架、404，或官方返回停售状态后，才标记 `OFF_SHELF`；
- 抓取失败、403、429、验证码和解析错误均不得计入未发现次数；
- 下架只更新状态，不删除产品、SKU、当前价格或历史。

## 10. 调度、并发和重试

### 10.1 默认调度

| 任务 | 默认频率 | 说明 |
| --- | --- | --- |
| 完整商品发现 | 每 6 小时 | 发现新品、新 SKU 和下架候选 |
| 在售价格刷新 | 每 30 分钟 | 只刷新 `ACTIVE` 报价 |
| 缺货/预约商品 | 每 60 分钟 | 检查恢复销售或价格公布 |
| 下架候选复核 | 每天 1 次 | 确认生命周期状态 |
| 原始证据清理 | 每天 1 次 | 按保留策略清理本地文件 |

调度时间为各品牌加入固定偏移和随机抖动，避免整点同时访问全部商城。

### 10.2 并发控制

- 每域名默认并发：2；
- 请求间隔：带抖动的 0.8～2 秒；
- 浏览器页面并发：1～2；
- HTTP 连接、读取和总任务均设置超时；
- 遵守 `Retry-After`；
- 发现 403、429、验证码或异常重定向时，立即降低速率；持续出现则中止该品牌批次。

这些值必须可通过环境变量配置。生产值应以站点规则、公司合规意见和实际监控为准。

### 10.3 重试策略

仅对连接超时、连接重置、部分 5xx 和明确的临时错误重试，默认最多 3 次，采用指数退避加随机抖动。

以下情况不自动重试或只做一次验证：

- 400、401、403、404；
- 验证码或访问控制；
- selector/JSON 路径失效；
- 价格业务规则校验失败；
- 域名白名单失败。

## 11. 原始证据管理

原始 HTML/JSON 用于复现解析问题和证明价格来源，不作为业务数据表保存。V1 存放在挂载卷：

```text
var/raw/{brand}/{yyyy}/{mm}/{dd}/{crawl_run_id}/{sha256}.html.gz
var/raw/{brand}/{yyyy}/{mm}/{dd}/{crawl_run_id}/{sha256}.json.gz
```

`crawl_record` 只保存相对路径和哈希。默认保留 30 天；价格变化对应的证据建议保留 180 天。文件不得包含登录信息、Cookie、请求密钥或个人信息。

后续如部署到多实例环境，可把证据目录迁移到对象存储，数据库结构无需变化。

## 12. 配置和密钥

`.env.example` 应列出但不包含真实值：

```dotenv
APP_ENV=development
LOG_LEVEL=INFO
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=device_price
MYSQL_USER=device_price
MYSQL_PASSWORD=change-me
CRAWLER_USER_AGENT=DevicePriceTestBot/1.0
HTTP_CONCURRENCY_PER_DOMAIN=2
HTTP_MIN_DELAY_SECONDS=0.8
HTTP_MAX_DELAY_SECONDS=2.0
PRICE_CHANGE_CONFIRM_THRESHOLD=0.30
RAW_STORAGE_PATH=/app/var/raw
RAW_RETENTION_DAYS=30
```

生产密码通过部署环境的 secret 机制注入，不写入 Git、镜像或日志。Cookie 默认不启用；如站点依赖纯会话 Cookie，只能使用匿名会话并由合规负责人确认。

## 13. 日志和监控

V1 使用 JSON 结构化日志输出到标准输出，由容器平台收集。每条日志至少包含：

- `timestamp`、`level`、`event`；
- `brand_code`、`channel_code`；
- `crawl_run_id`；
- `product_id`/`sku_id`/`entity_key`（适用时）；
- `duration_ms`；
- 稳定 `error_code`，不依赖自由文本做统计。

首批需要监控的指标：

- 每品牌批次成功率和运行时长；
- 发现产品数、SKU 数及相对上次变化；
- HTTP 状态码分布；
- 解析成功率、价格缺失率；
- 浏览器兜底比例；
- 价格变化数量和异常变化数量；
- 当前价格数据新鲜度；
- 连续失败批次数。

V1 可以先通过 SQL 巡检和日志平台报警，不因缺少 Prometheus 而阻塞上线。

建议报警条件：

- 任一品牌连续 2 个价格批次失败；
- 批次解析成功率低于 95%；
- SKU 数相较最近成功批次下降超过 20%；
- 在售报价超过 90 分钟未更新；
- 403/429/验证码数量超过阈值；
- 出现待确认的大幅价格变化。

## 14. 测试方案

### 14.1 单元测试

- 金额字符串解析；
- URL 白名单和规范化；
- SKU 属性规范化及指纹稳定性；
- 原价/当前售价语义判定；
- 销售状态映射；
- 子品牌和范围外品类过滤；
- 下架计数规则。

### 14.2 解析快照测试

每个品牌至少准备以下脱敏 fixture：

- 正常在售、多 SKU；
- 有原价和促销售价；
- 只有当前售价、没有原价；
- 缺货；
- 预约/预售；
- 已下架；
- 含国补、优惠券或以旧换新文案；
- 页面结构异常或缺字段。

测试不得依赖实时官网，否则 CI 会受网络和商城变更影响。实时验证作为独立 smoke test，手工或定时运行。

### 14.3 数据库集成测试

使用独立 MySQL 测试实例验证：

- 首次写入；
- 同一数据重复抓取的幂等性；
- 价格变化时历史区间正确关闭和开启；
- 状态变化但价格不变；
- 事务回滚后当前表和历史表保持一致；
- 并发写入同一 offer；
- 连续未发现与下架确认；
- migration 从空库升级成功、回滚策略可执行。

### 14.4 品牌 smoke test

每个品牌选取少量固定产品，验证：

- 域名和重定向符合白名单；
- 至少解析一个产品和一个 SKU；
- 价格与人工打开官方页面一致；
- 原价缺失时不会被错误补齐；
- 促销条件价格不会覆盖当前无条件售价；
- 原始证据能够从 `crawl_record.raw_path` 找回并重放。

## 15. 开发和部署流程

### 15.1 本地开发

目标命令约定：

```bash
make setup
make db-up
make migrate
make test
make lint
make crawl BRAND=apple MODE=full
make replay RECORD_ID=123
make scheduler
```

所有 adapter 必须支持 fixture 重放，开发解析逻辑时优先使用本地证据，避免反复请求官网。

### 15.2 CI 检查

每次提交至少执行：

1. Ruff 格式和 lint；
2. mypy 类型检查；
3. 单元和解析快照测试；
4. MySQL 集成测试；
5. Alembic 空库升级测试；
6. Docker 镜像构建。

实时商城 smoke test 不放在每个 PR 的必跑链路中，使用受控频率的独立任务。

### 15.3 部署单元

Docker Compose 包含：

- `mysql`：MySQL 数据库；
- `crawler-scheduler`：调度器、adapter、Playwright 运行环境；
- 可选一次性 `migration` 容器。

不部署 Redis，不部署业务 API。生产数据库如果由公司统一提供，则 Compose 中的 MySQL 只用于本地和测试。

### 15.4 发布步骤

1. 在测试环境备份数据库；
2. 执行 Alembic migration；
3. 部署新 crawler 镜像但先关闭自动调度；
4. 每品牌运行小范围 smoke test；
5. 对比人工页面、旧数据量和解析成功率；
6. 开启价格任务；
7. 最后开启完整发现任务；
8. 观察至少两个调度周期后完成发布。

adapter 页面规则变更也按正式发布处理，不允许在生产容器内临时修改 selector。

## 16. 分阶段实施计划

### 阶段 0：范围和合规确认

交付物：

- 本文档评审通过；
- 五个官方渠道白名单确认；
- robots、商城条款和公司内部合规意见留档；
- 子品牌排除范围确认；
- 采集频率和匿名测试 User-Agent 确认。

退出条件：不存在会改变数据模型或采集授权的未决问题。

### 阶段 1：项目骨架和数据库

工作项：

- 建立 Python 工程、配置、日志、CLI、Docker Compose；
- 建立 10 张表的 SQLAlchemy model 和 Alembic migration；
- 初始化五品牌、品类和渠道种子数据；
- 实现 repository、事务写入和价格历史算法；
- 完成数据库集成测试。

退出条件：可用构造数据完成首次写入、重复写入和价格变更历史测试。

### 阶段 2：通用采集框架

工作项：

- adapter 协议与注册器；
- HTTP fetcher、浏览器 fetcher 和域名白名单；
- 规范化、价格策略、质量校验；
- 原始证据、重放工具和 `crawl_record`；
- APScheduler、MySQL 命名锁和批次状态管理；
- 通用单元测试。

退出条件：使用本地 fixture 可完整执行“发现 → 解析 → 校验 → 入库”。

### 阶段 3：Apple 与小米适配器

> 当前状态：adapter、fixture、CLI、双品牌 MySQL 入库和重放已完成；真实 smoke test、人工价格抽查和连续两个调度周期待阶段 0 授权。

先实现两个结构差异较大的站点，用于验证抽象是否合理：

- Apple：重点验证多配置 SKU 和列表起售价排除；
- 小米：重点验证动态 SKU、促销信息过滤和子品牌排除。

退出条件：两个品牌通过 fixture、smoke test 和人工价格抽查，连续运行两个调度周期无重复或历史污染。

### 阶段 4：华为、OPPO、vivo 适配器

> 当前状态：三个 adapter、脱敏 fixture、五品牌注册入口和三品牌 MySQL 双周期入库重放已完成；真实 smoke test、人工价格抽查和生产调度验收待阶段 0 授权。

逐品牌完成发现、详情解析、SKU 和价格策略；每完成一个品牌单独上线验证，不等待三个品牌一起发布。

重点：

- 华为的国补、优惠文案区分；
- OPPO 的外部商城链接与一加过滤；
- vivo 的 iQOO 和第三方配件过滤。

退出条件：五品牌均达到单品牌验收标准。

### 阶段 5：稳定性和正式验收

> 当前状态：稳定性保护、只读巡检、运行手册、双版本数据库验收、公司 `device_price` 部署和真实商城全量验收已完成；四品牌全量成功，Apple 手机、平板和电脑成功，Watch 完整组合总价待官方 summary 接口适配。

工作项：

- 完成异常复抓和大幅价格变化保护；
- 完成下架确认机制；
- 建立 SQL 巡检和报警；
- 完成故障演练、备份恢复演练和运行手册；
- 进行一次全量人工抽样。

建议投入：单人约 10～15 个有效开发日，站点访问限制、合规审批和页面复杂度不计入此估算。

## 17. 验收标准

### 17.1 功能验收

- 五个品牌均能独立执行完整发现和价格刷新；
- 只产生范围内品牌、品类和官方直营渠道数据；
- 所有价格归属到 SKU，不保存 `starting_price`；
- 原价缺失时保存 `NULL`；
- 国补、券后、会员、以旧换新和月供不进入 `current_price`；
- 价格变化生成连续、无重叠的历史区间；
- 任务重复执行不产生重复产品、SKU、offer 或开放历史记录；
- 单页失败不会覆盖已有可信价格；
- 下架商品不被物理删除。

### 17.2 数据质量验收

- 在抽样商品上，SKU 配置和价格与官方页面一致；
- 解析成功率不低于 95%；
- 正常在售数据在计划运行时段内 90 分钟以内更新；
- 100% 当前价格记录可追溯到 source URL、采集批次、时间和原始证据哈希；
- 价格金额不存在浮点精度问题；
- 范围外渠道和子品牌入库数量为 0。

### 17.3 工程验收

- 10 张表均由 Alembic 管理；
- 本地环境可按 README 从空库启动；
- 单元、解析和数据库集成测试通过；
- 镜像中不包含数据库密码、Cookie 或原始生产证据；
- 日志中无密钥和个人信息；
- 每个 adapter 至少包含本文第 14.2 节要求的关键 fixture。

## 18. 主要风险和应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 页面结构频繁变化 | 解析失败或错误价格 | 独立 adapter、fixture、结构变化告警、旧价格保护 |
| 动态接口带签名或访问限制 | 无法稳定采集 | 优先官方页面/授权接口；不绕过控制；必要时停止并协调官方数据源 |
| 促销价格语义复杂 | 原价/售价污染 | 中央价格策略、品牌规则、人工样本和异常复抓 |
| 分类页暂时缺商品 | 误判下架 | 三次缺失 + 详情页确认 |
| SKU ID 不稳定 | 重复数据 | 官方 ID 优先，规范属性指纹兜底，规则版本测试 |
| 无 Redis 的单点调度 | 进程停止时任务暂停 | 容器自动重启、MySQL 锁、批次审计；规模增长后再评估队列 |
| Playwright 资源消耗 | 任务变慢 | HTTP 优先、浏览器兜底、限制页面并发 |
| 合规或 robots 限制 | 不能上线某渠道 | 上线前逐域名审批，优先争取官方数据接口 |

## 19. 运维排查顺序

当某品牌没有更新时，按以下顺序检查：

1. `crawl_run` 是否创建、是否卡在 `RUNNING`；
2. `crawl_record` 的 HTTP 状态、错误码和解析状态；
3. 是否发生域名重定向、403、429 或验证码；
4. 商品发现数量是否骤降；
5. 使用对应 `raw_path` 执行 fixture 重放；
6. 人工访问官方页面确认页面结构和价格语义；
7. 修复 adapter 并新增回归 fixture；
8. 在测试库重放成功后再发布；
9. 不手工篡改生产价格来掩盖解析问题。

## 20. 后续版本候选项

以下内容不进入 V1，只有在数据量或运行要求证明有必要时再设计：

- Redis/Celery 或其他消息队列；
- 多机分布式调度；
- 原始证据对象存储；
- 面向业务方的查询 API；
- 条件促销价的结构化保存；
- 官方 App、微信小程序等额外直营渠道；
- REDMI、iQOO、一加等子品牌；
- 第三方电商价格；
- 管理后台和人工审核工作流。

任何候选项不得提前混入 V1 表结构或运行链路；确需加入时先提交设计变更并更新本文档。

## 21. 官方参考入口

- Apple 中国大陆商城：<https://www.apple.com.cn/shop/>
- Apple robots：<https://www.apple.com.cn/robots.txt>
- 华为商城：<https://www.vmall.com/>
- 小米商城：<https://www.mi.com/shop/>
- 小米 robots：<https://www.mi.com/robots.txt>
- OPPO 商城：<https://www.opposhop.cn/>
- OPPO 官方渠道说明：<https://www.oppo.com/cn/online_store/>
- vivo 官方商城：<https://shop.vivo.com.cn/>

robots 文件只用于表达站点爬虫规则，不等同于商业使用授权。正式采集前仍需检查服务条款并完成公司内部合规确认。
