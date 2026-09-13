# 全品类价格数据库设计与字段参考

本文解释现有 13 张 V2 业务表及其设计约束；字段与约束的实现依据为 [SQLAlchemy 模型](../src/device_price_service/db/catalog_models.py)、[Repository](../src/device_price_service/db/catalog_repositories.py)和 [Alembic 迁移](../migrations/versions)。公司数据与验收不在本文重复维护，见[项目状态](PROJECT_STATUS.md)。

快速定位：[设计概览](#1-设计概览) · [数据口径](#2-已确认的数据口径) · [核心不变量](#3-核心不变量) · [表关系](#4-总体关系) · [通用类型](#5-通用字段与数据库约定) · [标准商品字段](#6-标准商品维度) · [来源字段](#7-来源商品维度) · [价格字段](#8-价格事实) · [审计字段](#9-采集审计) · [查询路径](#10-主要查询路径) · [事务](#12-写入事务)。

## 1. 设计概览

数据库保存商品身份、来源、卖家、地区、点时价格、匹配结论和采集证据，不保存理赔案件或个人信息，也不计算赔偿金额。

| 分组 | 物理表 |
| --- | --- |
| 标准商品（4） | `v2_brand`、`v2_category`、`v2_catalog_item`、`v2_item_variant` |
| 来源商品（5） | `v2_source_channel`、`v2_merchant`、`v2_source_listing`、`v2_listing_revision`、`v2_listing_match` |
| 价格事实（2） | `v2_price_observation`、`v2_price_current` |
| 采集审计（2） | `v2_crawl_run`、`v2_crawl_record` |

下文为简洁省略 `v2_` 前缀；实际 SQL 必须使用物理表名，避免误查同名 V1 表。`alembic_version` 不计入业务表。运行时只依赖 13 张 V2 表，完整历史迁移仍创建 V1 10 表和 V2 13 表；历史表默认保留，不改名、不迁入、不参与当前业务。

三项必要的分离是：平台不等于卖家（merchant）、同一链接可能换规格（listing_revision）、来源商品不等于标准商品（listing_match）。设备会自动建立标准型号、规格和精确规则匹配；政府数据先保存来源价格，不自动生成标准 item/variant/match。

通用结构中的人工复核、其他来源类型等字段是扩展能力，不表示已有操作入口或连接器。当前不增加别名表、地区维表、EAV 属性表、独立证据表或任务队列表；长尾属性使用 JSON，证据文件正文外置。

## 2. 已确认的数据口径

### 2.1 点时价格

- 动态商品页以实际抓取时刻作为价格时点，每次成功观察都新增一条 `price_observation`；
- 带明确数据日期的政府文件以来源日期作为价格时点；重复下载同一份数据保持幂等，不把下载时间伪装成新价格时点；
- 历史事实只追加，不用 `valid_from/valid_to` 合并成时间区间；
- 持久化层允许较旧的采集观察进入历史，但不得覆盖较新的 `price_current`；当前 `catalog replay` 是只读验算，不提供回写历史入口；
- `price_current` 只是指向最新可信点时记录的查询投影，不是第二份价格事实。

### 2.2 地区

- 地区是价格观察的组成部分，但本库不判断理赔应使用寄件地、收件地还是其他地点；
- 全国统一价格使用 `region_scope=NATIONAL`、`region_code=CN`；
- 区域价格保存省、市、区县或平台配送区域代码；
- 商务部省级市场数据使用明确的省级行政区代码；新疆生产建设兵团没有被伪装成省份代码，使用来源范围码 `XJ_CORPS`；
- 不保存精确收货地址、姓名、手机号等个人信息；
- `price_current` 的唯一业务键必须包含地区，防止一个地区的价格覆盖另一个地区。

### 2.3 金额

- `current_price` 表示来源直接展示的商品价或发布值，其具体性质由 `price_nature` 和 `pricing_basis` 解释；
- `original_price` 只有在页面明确表达划线价、建议零售价或原价时才保存；
- 不把运费、另收税费、服务费或额外包装费加进商品价格；
- 消费者页面没有拆分商品内含税时，不反推税前价；
- 商品固有包装和销售规格仍属于商品身份，例如“5kg 礼盒装”不能擅自减去礼盒成本；
- 无法拆分额外费用、只显示条件价或价格语义不明时，可以保存解析结果用于审计，但不得进入 `price_current`。

### 2.4 范围边界

- 币种固定为人民币 `CNY`，地区限定中国大陆；
- 结构允许官方商城、大型电商、超市、个人商城和公共价格数据成为不同等级的数据源；当前实现两个政府来源和五品牌官方商城，种子默认禁用，真实访问须显式启用并满足门禁；
- 本库保存价格证据，不输出理赔金额，不保存理赔单号和客户隐私数据；
- 其他品类和来源尚无真实接入；枚举或字段可表达，不代表当前连接器和规则能可靠处理。

## 3. 核心不变量

下列规则是后续模型、迁移和 Repository 必须共同保证的行为：

1. `price_observation` 是追加式事实，正常业务流程不得修改价格、来源、地区和观察时间；
2. `price_current` 只能指向 `quality_status=ACCEPTED` 的价格观察；
3. `price_current` 的来源商品、来源版本、地区和观察时间必须与其指向的观察一致；
4. 来源商品身份切换到新版本时，旧版本的当前投影立即失效，但历史观察不删除；
5. 乱序数据可以进入历史，只有 `observed_at` 更新的可信记录才能推进当前投影；
6. 来源商品每次发生影响可比性的规格变化，都生成新的 `listing_revision`；只有身份质量合格的版本才能成为当前版本；
7. 一个来源商品版本同时最多有一个生效的 `ACCEPTED` 标准商品匹配；
8. 匹配失败不得阻止来源价格留痕，但未匹配价格不能冒充某个标准商品的价格；
9. 价格、数量和单位换算不得使用浮点数；
10. 原始证据正文不直接存入 MySQL，只保存受控文件位置、哈希和元数据；
11. 数据质量失败、页面异常和解析失败不得覆盖同一来源版本的上一条可信当前价格；
12. 任何影响价格解释的字段都必须能追溯到采集批次、页面记录和解析版本。

## 4. 总体关系

```text
category 1 ── N catalog_item N ── 0..1 brand
catalog_item 1 ── N item_variant

source_channel 1 ── N merchant
source_channel 1 ── N source_listing N ── 1 merchant
source_listing 1 ── N listing_revision
source_listing 1 ── 0..1 current listing_revision
listing_revision N ── N item_variant     （通过 listing_match）

source_listing 1 ── N price_observation
listing_revision 1 ── N price_observation
source_listing 1 ── N price_current      （每个地区最多一条）
listing_revision 1 ── N price_current
price_current N ── 1 price_observation

source_channel 1 ── N crawl_run
crawl_run 1 ── N crawl_record
crawl_record 1 ── N price_observation
```

这套结构刻意分离三种身份：

- `item_variant`：平台无关、供查询使用的标准可比商品；
- `source_listing`：平台和卖家赋予的稳定商品身份；
- `listing_revision`：某个来源商品在一段时间内实际展示的规格身份。

价格挂在来源商品和来源版本上，而不是直接挂在标准商品上。这样即使自动匹配后来被纠正，原始价格事实仍然不丢失。

## 5. 通用字段与数据库约定

### 5.1 类型

| 用途 | 类型/规则 |
| --- | --- |
| 主键和外键 | `BIGINT UNSIGNED` |
| 商品总价 | `DECIMAL(18,2)` |
| 数量和单位价 | `DECIMAL(20,6)` |
| 置信度 | `DECIMAL(5,4)`，范围 0～1 |
| 业务时间 | UTC `DATETIME(3)` |
| 哈希 | 小写十六进制 SHA-256，`CHAR(64)`，ASCII 字符集 |
| 币种 | `CHAR(3)`，V2 固定 `CNY` |
| 地区代码 | `VARCHAR(32)`；可容纳行政区划和平台配送区域代码 |
| 扩展属性 | MySQL `JSON`，由应用层提供 `{}`，不依赖默认值 |
| 枚举 | `VARCHAR`；应用枚举和数据库校验共同约束，不使用 MySQL `ENUM` |

除字段表明确标为“可为空”或说明发现阶段可为空外，目标字段均为 `NOT NULL`。

### 5.2 公共审计字段

可变的维度和投影表使用：

```text
created_at DATETIME(3) NOT NULL
updated_at DATETIME(3) NOT NULL
```

追加式事实表只使用 `created_at`。所有时间写入 UTC，读取时由应用层转换时区。

### 5.3 删除策略

- 所有业务外键默认 `ON DELETE RESTRICT`；
- 品牌、品类、商品、来源和卖家使用状态字段停用，不物理删除；
- 点时价格原则上不可删除；原始证据清理须另行设计并确认，不把删文件视为正常业务步骤；
- 不使用级联删除，避免误操作破坏证据链。

## 6. 标准商品维度

### 6.1 `brand`：规范品牌

品牌不是所有品类的必填项；散装果蔬、普通农产品可以没有品牌。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `code` | VARCHAR(64) | UNIQUE；稳定规范代码 |
| `name_zh` | VARCHAR(128) | 中文规范名 |
| `name_en` | VARCHAR(128) | 可为空 |
| `status` | VARCHAR(16) | `ACTIVE/INACTIVE` |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

设计作用：保持电子产品品牌查询能力，同时允许无品牌商品；不新增 `product_alias`，来源页面里的品牌写法保留在来源属性中，由规范化词典处理。

### 6.2 `category`：全品类树

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `parent_id` | BIGINT UNSIGNED | 自关联 FK，可为空 |
| `code` | VARCHAR(64) | UNIQUE；ASCII 稳定代码 |
| `name_zh` | VARCHAR(128) | 中文名 |
| `name_en` | VARCHAR(128) | 可为空 |
| `level` | TINYINT UNSIGNED | 根节点为 0 |
| `path` | VARCHAR(512) | 物化路径，例如 `/FOOD/FRESH/FRUIT/APPLE/` |
| `is_leaf` | BOOLEAN | 是否允许直接挂商品 |
| `default_measure_type` | VARCHAR(16) | `WEIGHT/VOLUME/COUNT/...`，可为空 |
| `attribute_profile_code` | VARCHAR(64) | 对应代码中的品类规则包 |
| `attribute_profile_version` | VARCHAR(32) | 当前规则版本 |
| `enabled` | BOOLEAN | 是否允许新数据进入 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

索引：`parent_id`、`path` 前缀索引、`enabled`。

设计作用：MySQL 5.7 没有递归 CTE，使用物化路径可以直接查询某一级品类的全部后代；属性规则只保存版本引用，具体校验规则留在可测试、可版本控制的代码中，不把数据库做成难维护的动态规则引擎。

### 6.3 `catalog_item`：平台无关的标准商品/商品族

该表描述“是什么商品”，不描述某个平台的销售链接和包装价格。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `category_id` | BIGINT UNSIGNED | FK → `category.id` |
| `brand_id` | BIGINT UNSIGNED | FK → `brand.id`，可为空 |
| `canonical_key` | CHAR(64) | UNIQUE；规范身份指纹 |
| `item_type` | VARCHAR(24) | `MODEL/COMMODITY/GENERIC_GOOD` |
| `name` | VARCHAR(255) | 规范名称 |
| `series_name` | VARCHAR(128) | 系列，可为空 |
| `model_number` | VARCHAR(128) | 型号，可为空 |
| `base_attributes` | JSON | 商品族公共属性 |
| `record_origin` | VARCHAR(16) | `MANUAL/RULE/IMPORT/AUTO` |
| `status` | VARCHAR(24) | `ACTIVE/INACTIVE/REVIEW_REQUIRED` |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

索引：`(category_id, status)`、`brand_id`、`model_number`。

例子：

- 电子产品：`Apple iPhone 16`；
- 生鲜：`红富士苹果`；
- 肉类：`猪五花肉`；
- 包装食品：某品牌某系列牛奶。

设计作用：同一种商品可以对应多个平台和卖家；不会把某个卖家的标题当作全局标准名称。

### 6.4 `item_variant`：可比较的标准规格

该表是价格查询和可比性判断的最小标准单元。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `catalog_item_id` | BIGINT UNSIGNED | FK → `catalog_item.id` |
| `supersedes_variant_id` | BIGINT UNSIGNED | 自关联 FK；纠正规范规格时使用，可为空 |
| `variant_key` | CHAR(64) | 规范规格指纹 |
| `name` | VARCHAR(255) | 规范规格展示名 |
| `gtin` | VARCHAR(32) | 条码/GTIN，可为空；普通索引而非强制唯一 |
| `manufacturer_part_number` | VARCHAR(128) | 厂商部件号，可为空 |
| `condition_code` | VARCHAR(24) | `NEW/USED/REFURBISHED/UNKNOWN` |
| `measure_type` | VARCHAR(16) | `WEIGHT/VOLUME/COUNT/LENGTH/AREA/SET/OTHER` |
| `quantity_value` | DECIMAL(20,6) | 精确标准数量，可为空 |
| `quantity_min` | DECIMAL(20,6) | 浮动规格下限，可为空 |
| `quantity_max` | DECIMAL(20,6) | 浮动规格上限，可为空 |
| `base_unit` | VARCHAR(16) | `KG/L/PIECE/M/...` |
| `package_count` | INT UNSIGNED | 包装内件数，可为空 |
| `attributes` | JSON | 品种、产地、等级、部位、保鲜形态等 |
| `identity_fingerprint` | CHAR(64) | 由影响可比性的规范字段计算 |
| `status` | VARCHAR(24) | `ACTIVE/INACTIVE/REVIEW_REQUIRED/SUPERSEDED` |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

唯一约束：`(catalog_item_id, variant_key)`；索引：`gtin`、`manufacturer_part_number`、`identity_fingerprint`。

数量约束：

- 精确规格使用 `quantity_value`；
- 浮动重量使用 `quantity_min/quantity_max`；
- 两种表达不能同时存在；
- 所有非空数量必须大于 0，且 `quantity_max >= quantity_min`；
- 不能可靠换算时，标准数量可为空并进入人工复核，禁止猜测。

已经被价格和匹配引用的身份字段不得原地修改。`supersedes_variant_id` 和 `SUPERSEDED` 可表达规范规格的替代关系；当前没有自动纠正或人工写入命令，具体修复须先验证影响并获得授权。

设计作用：固定字段承载跨品类高频查询条件，JSON 承载品类长尾属性，避免为每个细分生鲜品类增加一组新列，也避免纯 EAV 模型导致查询和约束失控。

## 7. 来源商品维度

### 7.1 `source_channel`：平台、商城或公共数据源

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `code` | VARCHAR(64) | UNIQUE，例如 `JD_CN_WEB` |
| `name` | VARCHAR(128) | 来源名称 |
| `source_type` | VARCHAR(32) | `OFFICIAL_MALL/MAJOR_ECOMMERCE/SUPERMARKET/PERSONAL_SITE/PUBLIC_DATA` |
| `business_mode` | VARCHAR(24) | `SELF_OPERATED/MARKETPLACE/HYBRID/WHOLESALE` |
| `access_mode` | VARCHAR(16) | `API/HTTP/BROWSER/FILE/MIXED` |
| `base_url` | VARCHAR(512) | 基础地址 |
| `allowed_domains` | JSON | 允许访问的精确域名 |
| `region_mode` | VARCHAR(16) | `NATIONAL/REGIONAL/MIXED` |
| `currency` | CHAR(3) | 固定 `CNY` |
| `connector_code` | VARCHAR(64) | 适配器注册代码 |
| `enabled` | BOOLEAN | 是否启用采集 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

设计作用：移除 V1 对“品牌官方直营”的硬编码，让同一平台承载不同品牌和不同卖家；访问方式只是能力描述，不意味着允许绕过登录、验证码或访问控制。

### 7.2 `merchant`：来源内卖家

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `source_channel_id` | BIGINT UNSIGNED | FK → `source_channel.id` |
| `merchant_key_hash` | CHAR(64) | 适配器生成的稳定卖家键哈希 |
| `external_merchant_id` | VARCHAR(128) | 平台卖家 ID，可为空 |
| `name` | VARCHAR(255) | 页面展示名 |
| `seller_type` | VARCHAR(32) | `PLATFORM_SELF/BRAND_OFFICIAL/THIRD_PARTY/INDIVIDUAL/PUBLIC_MARKET/UNKNOWN` |
| `verification_status` | VARCHAR(24) | `VERIFIED/UNVERIFIED/UNKNOWN` |
| `status` | VARCHAR(16) | `ACTIVE/INACTIVE` |
| `first_seen_at` | DATETIME(3) | 首次发现 |
| `last_seen_at` | DATETIME(3) | 最近确认 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

唯一约束：`(source_channel_id, merchant_key_hash)`；索引：`external_merchant_id`、`seller_type`。

对于没有卖家概念的官方数据源，建立一个来源专用的合成卖家记录，不使用空外键。

设计作用：查询端可以单独选择平台自营、品牌官方或普通第三方价格，防止把平台信誉和卖家信誉混为一谈。

### 7.3 `source_listing`：来源中的可销售商品/SKU

一条记录代表“某来源、某卖家、某可选择规格”的稳定外部身份，而不是单纯 URL。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `source_channel_id` | BIGINT UNSIGNED | FK → `source_channel.id` |
| `merchant_id` | BIGINT UNSIGNED | FK → `merchant.id` |
| `listing_key` | VARCHAR(512) | 适配器输出的原始稳定键，不建立长索引 |
| `listing_key_hash` | CHAR(64) | 稳定键 SHA-256 |
| `external_product_id` | VARCHAR(128) | 来源商品 ID，可为空 |
| `external_sku_id` | VARCHAR(128) | 来源 SKU ID，可为空 |
| `price_nature` | VARCHAR(32) | 该来源序列固定的价格性质 |
| `canonical_url` | VARCHAR(1024) | 规范化证据页 URL |
| `url_hash` | CHAR(64) | 规范 URL 哈希 |
| `current_revision_id` | BIGINT UNSIGNED | FK → `listing_revision.id`，发现阶段可为空 |
| `lifecycle_status` | VARCHAR(24) | `ACTIVE/INACTIVE/UNKNOWN` |
| `consecutive_misses` | INT UNSIGNED | 连续未发现次数 |
| `first_seen_at` | DATETIME(3) | 首次发现 |
| `last_seen_at` | DATETIME(3) | 最近成功确认 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 更新时间 |

唯一约束：`(source_channel_id, merchant_id, listing_key_hash)`；索引：`(source_channel_id, external_product_id)`、`(source_channel_id, external_sku_id)`、`url_hash`、`(lifecycle_status, last_seen_at)`。

`current_revision_id` 是当前页面商品身份的投影。由于它与 `listing_revision` 形成建表顺序上的循环引用，迁移时先创建两张表，再补充该外键；应用和兼容触发器必须保证该版本属于当前 `source_listing`。

同一 `source_listing` 只表达一种 `price_nature`。一个页面或数据集同时发布零售均价、批发均价和指数时，必须拆成不同的来源序列，并让 `listing_key` 包含序列标识。政府数据的稳定键至少包含来源、市场层级、品种、规格和报价单位。

`external_sku_id` 允许为空，但稳定规格身份不能缺失。官方设备优先用真实 SKU ID；小米无可靠 SKU、Apple 只有配置容器且具备完整显式配置时，使用既有规格指纹 `listing_key`，不把容器号伪装成 SKU 或制造商号。Apple 字段门禁见[连接器稳定身份契约](V2_CONNECTOR_CONTRACT.md#3-稳定身份)。

设计作用：同一页面的不同 SKU 和不同价格序列不会互相覆盖；URL 变化不一定生成新商品；同一外部 ID 被不同卖家使用时仍然可以区分。

### 7.4 `listing_revision`：来源商品的不可变身份版本

只有影响商品可比性的规格发生变化时才建立新版本。价格变化不建立新版本。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `source_listing_id` | BIGINT UNSIGNED | FK → `source_listing.id` |
| `first_crawl_record_id` | BIGINT UNSIGNED | FK → `crawl_record.id`；形成该版本的首份证据 |
| `revision_no` | INT UNSIGNED | 来源商品内部递增版本号 |
| `source_title` | VARCHAR(512) | 首次形成该版本时的标题 |
| `source_category_path` | VARCHAR(512) | 来源分类文本，可为空 |
| `source_attributes` | JSON | 页面原始结构化属性 |
| `normalized_attributes` | JSON | 规范化后的品类属性 |
| `condition_code` | VARCHAR(24) | 商品状态 |
| `measure_type` | VARCHAR(16) | 原商品计量维度 |
| `quantity_value` | DECIMAL(20,6) | 精确销售数量，可为空 |
| `quantity_min` | DECIMAL(20,6) | 浮动数量下限，可为空 |
| `quantity_max` | DECIMAL(20,6) | 浮动数量上限，可为空 |
| `quantity_unit` | VARCHAR(16) | 页面规范化后的原单位 |
| `base_quantity_value` | DECIMAL(20,6) | 换算到基本单位的精确数量，可为空 |
| `base_quantity_min` | DECIMAL(20,6) | 基本单位下限，可为空 |
| `base_quantity_max` | DECIMAL(20,6) | 基本单位上限，可为空 |
| `base_unit` | VARCHAR(16) | `KG/L/PIECE/...` |
| `package_count` | INT UNSIGNED | 包装内件数，可为空 |
| `identity_fingerprint` | CHAR(64) | 价格可比属性指纹 |
| `normalizer_version` | VARCHAR(64) | 规范化规则版本 |
| `quality_status` | VARCHAR(24) | `ACCEPTED/REVIEW_REQUIRED/REJECTED` |
| `rejection_code` | VARCHAR(64) | 身份解析拒绝原因，可为空 |
| `reviewed_by` | VARCHAR(64) | 内部角色或服务账号，可为空 |
| `reviewed_at` | DATETIME(3) | 身份复核时间，可为空 |
| `first_observed_at` | DATETIME(3) | 首次观察 |
| `last_observed_at` | DATETIME(3) | 最近仍观察到该身份 |
| `created_at` | DATETIME(3) | 创建时间 |

唯一约束：`(source_listing_id, revision_no)`、`(source_listing_id, identity_fingerprint)`；索引：`(source_listing_id, last_observed_at)`。

标题、规格、数量和属性等身份字段不可更新；仅 `last_observed_at` 和受审计的质量复核字段可以变化。标题的标点、宣传词等非身份变化不建立新版本；净重、包装数量、等级、型号、产地、保鲜状态等可比属性变化必须建立新版本。`source_listing.current_revision_id` 只能指向 `quality_status=ACCEPTED` 的版本；待复核或拒绝版本不会使可信商品身份发生跳变。

采集链由 Repository 统一选择当前版本：新规格必须有可信观察，且本次可信时点晚于现当前规格的最新可信事实；相同时点不同规格拒绝，旧时点只留历史。`last_observed_at` 包括仅能审计的拒绝价格，不能拿它判断当前版本新旧。无价状态也是可信观察，采用同一时序规则，不借用旧金额。

设计作用：解决“链接没变但商品已经换规格”的历史串货问题。旧价格继续引用旧版本，新价格引用新版本，两者不会被错误地认为是同一商品。

### 7.5 `listing_match`：来源版本与标准规格的匹配决策

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `listing_revision_id` | BIGINT UNSIGNED | FK → `listing_revision.id` |
| `item_variant_id` | BIGINT UNSIGNED | FK → `item_variant.id` |
| `match_status` | VARCHAR(24) | `CANDIDATE/ACCEPTED/REJECTED/REVIEW_REQUIRED/SUPERSEDED` |
| `match_method` | VARCHAR(24) | `GTIN/MODEL/RULE/MANUAL/ML` |
| `confidence` | DECIMAL(5,4) | 0～1 |
| `matched_fields` | JSON | 支持匹配的字段及值 |
| `mismatch_fields` | JSON | 冲突、缺失和排除原因 |
| `matcher_version` | VARCHAR(64) | 匹配规则/模型版本 |
| `effective_from` | DATETIME(3) | 决策生效时间 |
| `effective_to` | DATETIME(3) | 被替代时间，可为空 |
| `reviewed_by` | VARCHAR(64) | 内部角色或服务账号，可为空；不存个人隐私 |
| `reviewed_at` | DATETIME(3) | 人工复核时间，可为空 |
| `created_at` | DATETIME(3) | 创建时间 |

唯一约束：`(listing_revision_id, item_variant_id, matcher_version)`；索引：`(item_variant_id, match_status)`、`(listing_revision_id, match_status, effective_to)`。

同一个 `listing_revision_id` 同时最多有一个 `effective_to IS NULL` 的 `ACCEPTED` 记录。MySQL 5.7 不支持部分唯一索引，该规则由带行锁的事务、兼容触发器和集成测试共同保证。

设计作用：

- 设备以明确官方身份建立精确 RULE 匹配；其他匹配方法枚举不表示已有自动算法；
- 可以记录“为什么认为相同”和“哪些字段冲突”；
- 后续纠正可关闭旧匹配并新增匹配，不篡改价格事实；当前没有人工复核 CLI；
- 未匹配来源商品仍可保存价格，等待后续补充属性或人工处理。

## 8. 价格事实

### 8.1 `price_observation`：点时价格观察

这是核心事实表。设备新抓取时点即使同价也追加；相同证据/时点的幂等重复不新增。政府使用来源日期，重复下载未变化行不会制造新事实。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `observation_key` | CHAR(64) | UNIQUE；幂等键 |
| `supersedes_observation_id` | BIGINT UNSIGNED | 自关联 FK；政府同日修订使用，可为空 |
| `source_listing_id` | BIGINT UNSIGNED | FK → `source_listing.id` |
| `listing_revision_id` | BIGINT UNSIGNED | FK → `listing_revision.id` |
| `crawl_record_id` | BIGINT UNSIGNED | FK → `crawl_record.id` |
| `region_scope` | VARCHAR(24) | `NATIONAL/PROVINCE/CITY/DISTRICT/DELIVERY_ZONE/UNKNOWN` |
| `region_code` | VARCHAR(32) | `CN`、行政区代码或平台区域代码 |
| `currency` | CHAR(3) | 固定 `CNY` |
| `original_price` | DECIMAL(18,2) | 可为空 |
| `original_price_type` | VARCHAR(32) | `CROSSED_OUT/MSRP/EXPLICIT_ORIGINAL/NONE` |
| `current_price` | DECIMAL(18,2) | 可为空；可信金额必须大于 0；`AVAILABILITY_ONLY` 必须为空 |
| `price_nature` | VARCHAR(32) | `RETAIL_OFFER/WHOLESALE_OFFER/RETAIL_AVERAGE/WHOLESALE_AVERAGE/MARKET_AVERAGE/UNKNOWN` |
| `price_type` | VARCHAR(32) | `DIRECT_UNCONDITIONAL/PUBLISHED_VALUE/AVAILABILITY_ONLY/MEMBER/COUPON/SUBSIDY/STARTING/INSTALLMENT/DEPOSIT/BUNDLE/UNKNOWN` |
| `pricing_basis` | VARCHAR(24) | `PACKAGE_TOTAL/UNIT_QUOTED/VARIABLE_ESTIMATE/UNKNOWN` |
| `promotion_label` | VARCHAR(128) | 秒杀、限时促销等页面说明，可为空 |
| `availability` | VARCHAR(24) | `ON_SALE/OUT_OF_STOCK/PRE_SALE/OFF_SHELF/UNKNOWN` 等 |
| `unit_price` | DECIMAL(20,6) | 可为空；可精确换算时的单位价 |
| `unit_price_unit` | VARCHAR(16) | `CNY_PER_KG/CNY_PER_L/...`，可为空 |
| `fee_status` | VARCHAR(32) | `ITEM_ONLY/SEPARATE_FEES_EXCLUDED/NOT_APPLICABLE/INSEPARABLE/UNKNOWN` |
| `quality_status` | VARCHAR(24) | `ACCEPTED/REVIEW_REQUIRED/REJECTED` |
| `rejection_code` | VARCHAR(64) | 稳定拒绝原因，可为空 |
| `reviewed_by` | VARCHAR(64) | 内部角色或服务账号，可为空 |
| `reviewed_at` | DATETIME(3) | 价格语义复核时间，可为空 |
| `displayed_price_text` | VARCHAR(255) | 页面短文本证据，可为空 |
| `source_hash` | CHAR(64) | 单商品页面的原文哈希，或批量数据集内该价格行的规范证据哈希 |
| `observed_at` | DATETIME(3) | 价格事实时间：来源明确给出的观测时间/数据日期，否则为实际抓取时间 |
| `created_at` | DATETIME(3) | 入库时间 |

推荐索引：

```text
UNIQUE (observation_key)
INDEX (source_listing_id, region_scope, region_code, observed_at)
INDEX (listing_revision_id, observed_at)
INDEX (quality_status, observed_at)
INDEX (crawl_record_id)
INDEX (supersedes_observation_id)
INDEX (observed_at)
```

`observation_key` 由以下稳定字段计算：

```text
source_listing_id
+ listing_revision_id
+ region_scope
+ region_code
+ observed_at
+ source_hash
+ price_nature
+ price_type
+ pricing_basis
+ 规范化价格载荷哈希
+ parser/policy version
```

该键保证同一证据、同一价格候选和同一解析版本的重试或重放不会重复写入，同时允许同一页面的一条无条件价格和一条会员价格分别留痕，也不阻止不同来源时点观察到相同价格。批量政府文件使用来源行的规范证据哈希，完整文件字节哈希和路径由关联的 `crawl_record` 保存；因此一行变价只修订一行，页脚、脚本或其他行变化不会制造伪价格历史。政府文件的系统下载时间只写入 `crawl_record.fetched_at`，不参与制造新的来源价格时点。

政府文件同日修订通过 `supersedes_observation_id` 指向旧观察。修正记录必须保持来源商品、来源版本、地区和观察时间一致，但官方替换文件时证据哈希可以不同。旧记录不删除、不改金额；默认历史查询排除已被可信新观察明确替代的解析结果。普通重复采集和设备产品事实不能使用该字段；设备同刻非幂等冲突应拒绝，不建立政府式修订链。`catalog replay` 只呈现当前解析与历史事实，不因解析器更新而写入修正观察。

价格、来源、地区、证据和观察时间不可修改。复核字段保留在通用结构中，但本版本没有人工复核写入命令；不能用审计或重放命令原地改值，或将待复核价格升级为可信事实。

可信金额只有同时满足以下条件才可推进 `price_current`；可信无价状态使用下文独立的严格分支，不降低金额门禁：

- `quality_status=ACCEPTED`；
- 零售或批发报价必须是 `price_type=DIRECT_UNCONDITIONAL`；政府零售/批发均价必须是可验证的 `PUBLISHED_VALUE`；
- `price_nature` 不能为 `UNKNOWN`，不同性质价格不得在查询中混合；
- `pricing_basis` 为 `PACKAGE_TOTAL` 或能精确换算的 `UNIT_QUOTED`；
- `region_scope` 不是 `UNKNOWN`；只有已验证为全国价格的来源才能写成 `NATIONAL/CN`；
- `current_price > 0`；
- 零售或批发报价的 `fee_status` 为 `ITEM_ONLY` 或 `SEPARATE_FEES_EXCLUDED`；公开市场均价可以是 `NOT_APPLICABLE`；
- 价格和原价语义校验通过；
- `observed_at` 不早于当前投影指向的观察时间。

面向所有用户且无需领券、会员或特殊资格的限时直降仍可归入 `DIRECT_UNCONDITIONAL`，促销说明放入 `promotion_label`。政府明确发布且单位为人民币的零售均价和批发均价分别使用 `RETAIL_AVERAGE`、`WHOLESALE_AVERAGE`；旧 `MARKET_AVERAGE` 只为既有 fixture 兼容，不用于新政府连接器。没有人民币价格含义的指数值不进入本价格表。“前一日价格”是另一个历史时点，不是 `original_price`；第一版只采当日值。会员、优惠券、补贴、定金、分期、起售价、估算总价和不可拆分费用可以作为被拒绝的解析事实留存，但绝不能进入最新可信价格。

金额约束：所有非空金额必须大于 0；`original_price` 为空时 `original_price_type` 必须为 `NONE`，反之亦然；`unit_price` 与 `unit_price_unit` 必须同时为空或同时非空。`current_price` 表示 `pricing_basis` 声明的销售单位价格，不能把“每月”“定金”或“预计整件金额”伪装为包装总价。

可信无价状态使用 `price_type=AVAILABILITY_ONLY`，且必须同时满足：

- `quality_status=ACCEPTED`、`price_nature=RETAIL_OFFER`、地区已明确；
- `availability` 仅允许 `OFF_SHELF/OUT_OF_STOCK/COMING_SOON`；
- `current_price/original_price/unit_price/unit_price_unit` 均为 `NULL`，`original_price_type=NONE`；
- `pricing_basis=UNKNOWN`、`fee_status=NOT_APPLICABLE`、`promotion_label=NULL`。

此分支解决“商品明确下架或缺货但不再展示价格时，上一条在售价格仍留在当前投影”的问题；历史金额继续保留，当前投影可以指向已证实的无价状态，重新在售后再由新可信价格推进。请求失败、解析失败、发现列表缺失、`UNKNOWN` 或政府均价不能使用这个例外。流水线已接入证据判断和缺失复核；仅完整范围批次累积缺失，达到阈值后仍需明确详情证据。重放与审计也核对无价状态，不能借用旧金额生成新时点报价。

迁移 `b72c910e4f31` 不改动既有价格字段。若已经存在 `PRODUCT` 证据或 `AVAILABILITY_ONLY` 观察，降级会在任何 DDL 前拒绝；回退应采用经批准的备份恢复方案，不删除或篡改事实以强行降级。

### 8.2 `price_current`：每个来源商品、每个地区的最新可信价格/状态指针

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `source_listing_id` | BIGINT UNSIGNED | FK → `source_listing.id` |
| `listing_revision_id` | BIGINT UNSIGNED | FK → `listing_revision.id`；必须是来源商品当前版本 |
| `region_scope` | VARCHAR(24) | 与观察记录一致 |
| `region_code` | VARCHAR(32) | 与观察记录一致 |
| `price_observation_id` | BIGINT UNSIGNED | UNIQUE，FK → `price_observation.id` |
| `observed_at` | DATETIME(3) | 冗余排序字段，必须等于观察时间 |
| `created_at` | DATETIME(3) | 创建时间 |
| `updated_at` | DATETIME(3) | 投影更新时间 |

唯一约束：`(source_listing_id, region_scope, region_code)`；索引：`(listing_revision_id, region_scope, region_code)`、`(region_scope, region_code, observed_at)`。

本表不重复保存价格金额，只保存身份和观察指针，查询时关联一条 `price_observation`。`source_listing_id`、`listing_revision_id`、地区和 `observed_at` 是为了快速查询而保留的投影字段，必须与指向的观察一致；这样历史事实和当前价格不会出现两份金额不一致的问题。

写入规则：

1. 先插入不可变 `price_observation`；
2. 锁定对应来源商品和地区的 `price_current`；
3. 只有新观察更晚且质量合格时才更新指针；
4. 观察时间相同时，只有政府来源显式、可信的修正记录可以替代其指向的旧解析；设备只允许严格幂等，不允许同刻非幂等替换；
5. `price_observation.listing_revision_id` 必须等于当次采集确认的来源版本；
6. 来源商品切换到新 `current_revision_id` 时，删除旧版本的 `price_current` 投影；新版本价格质量合格后再建立投影；
7. 乱序、拒绝或待复核观察保留在历史中，但不修改指针；
8. 事务失败时观察和投影一起回滚。

设计作用：查询最新价格无需扫描或排序整个历史，同时保持单一事实来源。

## 9. 采集审计

### 9.1 `crawl_run`：采集批次

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `source_channel_id` | BIGINT UNSIGNED | FK → `source_channel.id` |
| `region_scope` | VARCHAR(24) | 本批次地区范围；批量接口可使用 `MULTI` |
| `region_code` | VARCHAR(32) | 默认地区代码；`MULTI` 时使用 `*` |
| `category_scope` | JSON | 本批次品类范围 |
| `run_type` | VARCHAR(24) | `DISCOVERY/PRICE/FULL/REPLAY` |
| `trigger_type` | VARCHAR(24) | `SCHEDULED/MANUAL/QUERY_RECRAWL` |
| `status` | VARCHAR(24) | `RUNNING/SUCCEEDED/PARTIAL/FAILED/CANCELLED` |
| `adapter_version` | VARCHAR(64) | 适配器代码版本/Git SHA |
| `policy_version` | VARCHAR(64) | 价格和质量规则版本 |
| `started_at` | DATETIME(3) | 开始时间 |
| `finished_at` | DATETIME(3) | 可为空 |
| `discovered_count` | INT UNSIGNED | 发现来源商品数 |
| `fetched_count` | INT UNSIGNED | 成功取得响应数 |
| `accepted_count` | INT UNSIGNED | 可信观察数；设备接入后包括合法无价状态候选，不等同于新增事实数 |
| `review_count` | INT UNSIGNED | 待复核数 |
| `rejected_count` | INT UNSIGNED | 规则拒绝数 |
| `failed_count` | INT UNSIGNED | 抓取或解析失败数 |
| `error_summary` | JSON | 稳定错误码汇总 |
| `created_at` | DATETIME(3) | 创建时间 |

索引：`(source_channel_id, started_at)`、`(status, started_at)`、`(region_code, started_at)`。

设计作用：能够判断一段时间没有新价格究竟是价格没变、商品缺货、平台失败、解析器漂移，还是任务根本没执行。

### 9.2 `crawl_record`：单请求/单实体证据与处理结果

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `crawl_run_id` | BIGINT UNSIGNED | FK → `crawl_run.id` |
| `source_listing_id` | BIGINT UNSIGNED | FK → `source_listing.id`，发现阶段可为空 |
| `replayed_from_record_id` | BIGINT UNSIGNED | 自关联 FK，可为空；当前只读 replay 不新建记录或填写此字段 |
| `entity_type` | VARCHAR(32) | `CATEGORY/SEARCH/PRODUCT/LISTING/SKU/OFFER/PUBLIC_PRICE` |
| `entity_key` | VARCHAR(255) | 外部 ID 或规范 URL 指纹 |
| `request_url` | VARCHAR(1024) | 脱敏请求 URL |
| `final_url` | VARCHAR(1024) | 重定向后 URL |
| `fetch_method` | VARCHAR(16) | `API/HTTP/BROWSER/FILE/REPLAY` |
| `http_status` | SMALLINT UNSIGNED | 可为空 |
| `fetch_status` | VARCHAR(24) | 抓取状态 |
| `parse_status` | VARCHAR(24) | 解析状态 |
| `validation_status` | VARCHAR(24) | 质量校验状态 |
| `error_code` | VARCHAR(64) | 稳定错误码，可为空 |
| `error_message` | TEXT | 脱敏错误摘要，可为空 |
| `raw_hash` | CHAR(64) | 主响应 SHA-256，可为空 |
| `raw_path` | VARCHAR(1024) | 对象存储或挂载卷相对路径，可为空 |
| `artifact_manifest` | JSON | 截图、结构化响应等证据清单及哈希 |
| `content_type` | VARCHAR(128) | 响应媒体类型，可为空 |
| `raw_size_bytes` | BIGINT UNSIGNED | 原始证据大小，可为空 |
| `duration_ms` | INT UNSIGNED | 请求耗时 |
| `fetched_at` | DATETIME(3) | 抓取时间 |
| `created_at` | DATETIME(3) | 创建时间 |

索引：`(crawl_run_id, parse_status)`、`(source_listing_id, fetched_at)`、`(entity_type, entity_key)`、`fetched_at`、`raw_hash`。

设计作用：价格记录不存完整网页，仍能通过 `crawl_record → raw_path/raw_hash → 原始证据` 重放和证明来源；一个页面产出多个 SKU 价格时，多条观察可以指向同一个采集记录。

`PRODUCT` 表示产品级共享证据，`source_listing_id` 可为空，由该产品各 SKU 的价格观察关联同一个记录；不能为填充外键任意挑选其中一个 SKU。发现产品、请求范围、品牌/渠道及主响应证据元数据已持久化；需要多个请求的产品使用可追溯的复合证据，变价复核另保留首次和确认响应。

`catalog replay --record-id` 使用原记录时点和发现上下文，重放全部 SKU 或政府文档行，分别输出当前静态解析与历史校验/已存事实；不联网、不写库，也不把本次执行当作新报价。旧政府记录可从保存的文章 URL、发布日期和商务部品种 ID 确定恢复上下文；缺少必要证据则明确失败，不回读后来变化的 listing 猜测或补抓。

`artifact_manifest` 保存证据元数据与重放上下文，不参与商品查询；当前不另建证据文件表。

### 9.3 跨表一致性约束

单列外键只能证明记录存在，不能自动证明几张表里的来源身份完全相同。Repository、MySQL 5.7 兼容触发器和数据库审计必须共同检查：

1. `source_listing.merchant_id` 所属渠道等于 `source_listing.source_channel_id`；
2. `source_listing.current_revision_id` 所属来源商品就是该 `source_listing`；
3. `source_listing.current_revision_id` 只能指向身份质量为 `ACCEPTED` 的版本；
4. `listing_revision.first_crawl_record_id` 若已绑定来源商品，则必须与该版本一致；批量响应的采集记录可以不绑定单一来源商品；
5. `price_observation.listing_revision_id` 所属来源商品等于 `price_observation.source_listing_id`；
6. `price_observation.price_nature` 等于 `source_listing.price_nature`；
7. `price_observation.crawl_record_id` 所在批次渠道与来源商品渠道一致；
8. 普通单地区批次中，观察地区必须与 `crawl_run` 地区一致；多地区接口批次应明确使用 `MULTI/*`，每条观察仍必须写入具体地区；
9. `price_current` 的来源商品、来源版本、地区、时间必须和 `price_observation_id` 指向的记录完全一致；
10. `price_current.listing_revision_id` 必须等于 `source_listing.current_revision_id`。

这些检查不能为了提高写入成功率而放宽。发现不一致时整笔事务回滚并写入稳定错误码；`price_current` 是可重建投影，必要时可由已接受的点时观察重新生成。

`db check` 仅检查 MySQL 兼容性和 13 张必需 V2 表；缺少 V1 表不阻止运行。`db audit` 只读核对 V2 当前指针、观察、身份、标准匹配、证据引用、无价状态与批次终态，使用 `--check-artifacts` 可追加本地文件/哈希检查；它不自动修复、不读取 V1 数据，也不对未运行的禁用来源发出缺采警告。

## 10. 主要查询路径

### 10.1 查询某标准规格的当前价格

```text
item_variant
→ 生效的 listing_match
→ listing_revision
→ source_listing
→ 要求 source_listing.current_revision_id = listing_revision.id
→ price_current（按地区）
→ price_observation
→ merchant + source_channel
```

同时要求 `price_current` 指向的观察也引用同一个 `listing_revision`。结果天然包含来源、卖家、地区、价格性质、观察时间、匹配置信度和证据引用。查询端必须按 `price_nature` 分组，并可以根据卖家类型、价格时效和匹配置信度进一步筛选。版本不一致时返回“当前无可信价格”，不能回退到该链接旧规格的价格。

### 10.2 查询某标准规格在时间点附近的历史价格

```text
item_variant
→ listing_match
→ listing_revision
→ price_observation
WHERE observed_at <= :target_time
ORDER BY observed_at DESC
```

数据库返回观察事实，不推断两个采样点之间价格一直有效。应用端如果需要“事故时最近价”，应明确允许的最大时间差。

### 10.3 查询来源商品原始价格轨迹

直接按 `source_listing_id + region + observed_at` 查询 `price_observation`，不依赖标准商品匹配。这使错误匹配被纠正后，原始来源轨迹仍然完整。

## 11. 痛点与设计取舍

| 痛点 | 现有设计如何解决 |
| --- | --- |
| 同名、同链接但规格不同 | 标准型号/规格分层；来源规格变化新建不可变 revision，价格继续引用当时身份 |
| 生鲜品种与单位细碎 | 核心字段 + 长尾 JSON + 可测试的品类规则；只做有依据的精确单位换算 |
| 地区、零售和批发混价 | 地区进入观察与当前投影键；每条来源序列固定 price_nature，查询时分开使用 |
| 重采重复、旧数据覆盖新数据 | 点时幂等键；current 指针按可信时间推进；政府同日修订显式关联旧观察 |
| 页面失败或条件价污染结果 | 质量门禁与失败审计分离；异常不覆盖上一条可信当前价 |
| 无法复现、匹配有误 | 保留原文件及批次/记录；匹配与价格事实分离，纠正匹配不改原价格 |
| 历史增加导致当前查询变慢 | current 只存最新可信观察指针，金额只有 observation 一份事实 |

## 12. 写入事务

一次可信价格写入建议遵守以下事务边界：

1. 创建或锁定 `source_listing`；
2. 创建 `crawl_record` 并登记证据位置和哈希；
3. 根据影响可比性的规范字段取得或新建 `listing_revision`，新建时引用首份 `crawl_record`；
4. 对该来源商品已有的 `price_current` 投影加行锁；
5. 来源身份变化、身份质量合格且本次可信观察较当前规格的可信事实更新时，使旧版本当前投影失效并推进 `source_listing.current_revision_id`；只有拒绝价格的新规格不切换当前版本；
6. 生成 `observation_key`，幂等插入 `price_observation`；
7. 校验来源版本、价格质量和观察时间；
8. 满足推进条件时更新或新建对应地区的 `price_current`；
9. 提交事务；任何一步失败全部回滚。

匹配标准商品不是价格写入的前置条件。来源价格可以先保存，后续建立 `listing_match`；这避免为了提高覆盖率而强行猜测标准商品。

上述是逐行逻辑，实际事务边界为完整产品或完整政府文档：先创建一条共享证据，再逐行处理 listing/revision/价格，全部成功后提交。官方设备在同一事务中建立标准型号、规格及精确匹配，政府不强制建档。任何持久化错误回滚该产品/文档后，另记失败证据，不通过审计初始化动作更新当前商品元数据。

设备单产品持久化事务使用 `READ COMMITTED`，避免空匹配范围的间隙锁阻塞不同来源；父 revision 锁、匹配当前读和唯一约束仍保证同一身份的互斥。批次启动在已有来源/地区命名锁下查询同范围旧批次，不再增加冗余范围锁。事务提交/回滚并归还连接后恢复默认隔离级别，政府事务保持原样。此为局部事务策略，不是数据库全局配置或表结构变更；运行要求见[MySQL 兼容说明](MYSQL_57_COMPATIBILITY.md#3-设备事务隔离)。

## 13. 数据量与保留边界

设备事实量随“来源规格数 × 实际地区数 × 采样次数”增长；政府同一来源日未变化的价格行幂等复用。全国价只保存 `NATIONAL/CN` 一份，不机械复制到每个行政区。

当前不做分区、分库分表或自动归档服务。原始证据正文外置，数据库保存引用；证据保留期限和历史清理方案须单独确认，不可只删文件而不处理可追溯性。备份和恢复见[运行手册](OPERATIONS_RUNBOOK.md#5-部署备份与回退)。

## 14. 实现与迁移

结构变更通过模型和 Alembic 共同演进，验证真实 MySQL 5.7 与 8.x；版本门禁、触发器、局部隔离和降级限制统一见[兼容说明](MYSQL_57_COMPATIBILITY.md)。

`source_listing.current_revision_id` 存在受控循环引用，迁移先建相关表再补外键。普通运行连接不安装触发器、不修改全局环境；后续自动生成迁移只管理 `v2_` 表。不得因运行时不再依赖 V1 而自动生成删旧表操作。

## 15. 政府数据入库示例

假设上海发布某日“鸡蛋平均零售价 5.76 元/500 克”：

| 实际使用表 | 内容 |
| --- | --- |
| category | `FRESH_MONITORED_COMMODITY`，引用 government-fresh 规则 |
| source_channel / merchant | 上海政府来源 / 来源专用合成公共市场 |
| source_listing / listing_revision | 上海零售序列 + 鸡蛋 + 来源规格和报价单位；保存原名称、规范属性及身份指纹 |
| price_observation | `CITY/310100`、`RETAIL_AVERAGE`、5.76、`PUBLISHED_VALUE`、`UNIT_QUOTED`；原价为空，单位价 11.52 元/kg，时点为来源日期 |
| crawl_record | 实际下载时间、文章/附件引用、完整文件路径和哈希 |
| price_current | 指向该来源商品、地区的最新可信发布值 |

本例不自动建立 catalog_item、item_variant 或 listing_match。次日新报价形成新点时事实；重复同日相同行保持幂等，同日官方修订追加修正观察。批发报价属于另一性质的来源序列，不与本例零售均价混用。
