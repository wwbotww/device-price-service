# V2 全品类价格采集数据库设计

> 状态：政府生鲜结构已部署公司库；设备 I～J 已实现 Apple 原生链路，本地验证；K～M 待实施
> 适用数据库：公司 MySQL 5.7.36，同时兼容 MySQL 8.x
> 最后更新：2026-09-11

## 1. 设计结论

V2 将现有“官方设备 SKU 价格库”扩展为“全品类来源商品点时价格证据库”。数据库只负责保存商品身份、来源、卖家、地区、价格快照、匹配结论和采集证据，不保存理赔案件、客户信息，也不计算赔偿金额。

目标结构包含 **13 张业务表**：

| 分组 | 表 |
| --- | --- |
| 标准商品维度 | `brand`、`category`、`catalog_item`、`item_variant` |
| 来源商品维度 | `source_channel`、`merchant`、`source_listing`、`listing_revision`、`listing_match` |
| 价格事实 | `price_observation`、`price_current` |
| 采集审计 | `crawl_run`、`crawl_record` |

此外仍有 Alembic 管理的 `alembic_version`，不计入业务表数量。

本文表名表示逻辑名称。由于其中 `brand`、`category`、`price_current`、`crawl_run` 和 `crawl_record` 与 V1 同名，当前实现继续使用统一的 `v2_` 物理前缀。公司库已从备份后完整重建；随后于 2026-09-10 单独重采了 V1 设备 demo，未迁移旧数据，V2 当前仍只承载政府价格。

下一阶段直接将五品牌官方设备采集接入现有 13 张表；电商、超市和个人站仍仅保留框架能力。执行边界见[V2 全品类实施计划](V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md)，设备细节见[原生采集改造计划](V2_DEVICE_NATIVE_COLLECTION_PLAN.md)。

阶段 I 已实现产品级证据 `entity_type=PRODUCT`、可信无金额状态 `price_type=AVAILABILITY_ONLY` 及配套约束；不新增业务表。领域校验、SQLAlchemy 模型、追加式 Alembic 迁移 `b72c910e4f31`、MySQL 5.7 触发器和 MySQL 8.x CHECK 已同步并验证。阶段 J 已接通 Apple 原生产品解析、标准建档和共用事务，没有追加新迁移。公司库最近确认的 head 仍为 `96524222b3ec`，本轮未迁移公司库；其余品牌和完整运行保护尚未接通，不能宣称五品牌 V2 验收完成。

V1 使用 10 张表；V2 增加到 13 张不是为了追求模型完整，而是为了解决全品类场景中新出现的三个关键边界：

1. **平台不等于卖家**：大型电商中平台自营、品牌旗舰店和普通第三方店铺必须区分；
2. **来源商品会换内容**：同一个商品链接或外部 ID 可能被商家修改规格，需要保存不可变的商品版本；
3. **来源商品不等于标准商品**：自动或人工匹配必须有独立记录、置信度和版本，不能直接把来源标题写成标准商品。

如果强行压缩到 9～10 张表，只能把卖家、来源版本或匹配关系合并进商品表，会失去防止串货、错误匹配和历史不可复现的能力。

## 2. 已确认的数据口径

### 2.1 点时价格

- 动态商品页以实际抓取时刻作为价格时点，每次成功观察都新增一条 `price_observation`；
- 带明确数据日期的政府文件以来源日期作为价格时点；重复下载同一份数据保持幂等，不把下载时间伪装成新价格时点；
- 历史事实只追加，不用 `valid_from/valid_to` 合并成时间区间；
- 较旧的补采、重放数据允许写入历史，但不得覆盖较新的 `price_current`；
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
- 结构允许官方商城、大型电商、超市、个人商城和公共价格数据成为不同等级的数据源；当前只启用政府公共价格数据；
- 本库保存价格证据，不输出理赔金额，不保存理赔单号和客户隐私数据；
- 二手、定制、收藏、活体等难标准化商品可以入库，但其匹配状态可保持 `REVIEW_REQUIRED`，不能假装精确匹配。

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
10. 原始证据正文不直接存入 MySQL，只保存脱敏文件位置、哈希和元数据；
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
- 点时价格原则上不可删除；依法或按保留策略清理原始证据时，保留证据哈希和清理状态；
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

已经被价格和匹配引用的身份字段不得原地修改。规范规格判断错误时，新建记录并通过 `supersedes_variant_id` 指向旧记录，再把旧记录标为 `SUPERSEDED`；这样旧的匹配决策仍可解释。

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

阶段 J 已将采集链的当前版本选择收敛到 Repository：新规格必须有可信观察，且本次可信时点晚于现当前规格的最新可信事实；相同时点不同规格拒绝，旧时点只留历史。`last_observed_at` 包括仅能审计的拒绝价格，不能拿它判断当前版本新旧。无价状态也是可信观察，采用同一时序规则，不借用旧金额。

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

- 自动算法只产生候选，不直接改写标准商品；
- 可以记录“为什么认为相同”和“哪些字段冲突”；
- 人工纠正通过关闭旧匹配并新增匹配实现，不篡改价格事实；
- 未匹配来源商品仍可保存价格，等待后续补充属性或人工处理。

## 8. 价格事实

### 8.1 `price_observation`：点时价格观察

这是 V2 数据量最大的核心事实表。每次成功解析都新增一条；即使价格不变也不合并。

| 字段 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | BIGINT UNSIGNED | PK |
| `observation_key` | CHAR(64) | UNIQUE；幂等键 |
| `supersedes_observation_id` | BIGINT UNSIGNED | 自关联 FK；修正旧解析时使用，可为空 |
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

同一份证据被新版解析器纠正，或官方在同一来源时点替换文件时，新观察通过 `supersedes_observation_id` 指向旧观察。修正记录必须保持来源商品、来源版本、地区和观察时间一致，但官方替换文件时证据哈希可以不同。旧记录不删除、不改金额；默认历史查询排除已被可信新观察明确替代的解析结果。普通的重复采集不能使用该字段。

价格、来源、地区、证据和观察时间不可修改；`REVIEW_REQUIRED` 的质量状态只有在记录复核账号和时间后才可变为 `ACCEPTED` 或 `REJECTED`。如果复核发现价格金额或语义字段本身错误，必须新增修正观察并使用 `supersedes_observation_id`，不能原地改值。

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

此分支解决“商品明确下架或缺货但不再展示价格时，上一条在售价格仍留在当前投影”的问题；历史金额继续保留，当前投影可以指向已证实的无价状态，重新在售后再由新可信价格推进。请求失败、解析失败、发现列表缺失、`UNKNOWN` 或政府均价不能使用这个例外。确认状态所需的证据判断和缺失复核调用方在阶段 K 接入；当前已验证的是领域/数据库约束、幂等和乱序投影行为。

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
4. 观察时间相同时，只有显式、可信的修正记录可以替代其指向的旧解析；
5. `price_observation.listing_revision_id` 必须等于当次采集确认的来源版本；
6. 来源商品切换到新 `current_revision_id` 时，删除旧版本的 `price_current` 投影；新版本价格质量合格后再建立投影；
7. 乱序、拒绝或待复核观察保留在历史中，但不修改指针；
8. 事务失败时观察和投影一起回滚。

设计作用：在亿级历史快照上查询最新价格时无需扫描或排序整个历史，同时保持单一事实来源。

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
| `replayed_from_record_id` | BIGINT UNSIGNED | 自关联 FK，可为空 |
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

`PRODUCT` 表示产品级共享证据，`source_listing_id` 可为空，由该产品各 SKU 的价格观察关联同一个记录；不能为填充外键任意挑选其中一个 SKU。阶段 J 已持久化发现产品、请求范围、品牌/渠道及主响应证据元数据；完整重放入口仍按阶段 L 开发。

`artifact_manifest` 只保存不参与业务查询的证据元数据，因此暂不单独增加 `evidence_artifact` 表。将来需要独立保留期限、逐文件权限或大量截图时，再无损拆表。

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

## 11. 痛点与对应设计

| 痛点 | 数据库设计 | 解决方式 |
| --- | --- | --- |
| 全品类属性细碎 | 固定核心字段 + `attributes` JSON + 品类规则版本 | 常用条件可索引，长尾属性无需频繁加列 |
| 同名商品并非同规格 | `catalog_item` 与 `item_variant` 分层 | 商品族与可比销售规格分离 |
| 页面链接不变但规格被商家替换 | 不可变 `listing_revision` | 新旧价格引用不同身份版本，避免历史串货 |
| 平台上存在多种卖家 | 独立 `merchant` | 能按自营、旗舰店、第三方分层使用价格 |
| 自动匹配会误判 | 独立、版本化 `listing_match` | 候选、置信度、冲突字段和人工复核均可追踪 |
| 生鲜单位混乱 | 数量、范围、计量类型、基本单位和单位价专列 | 可精确换算时统一比较，不能换算时明确为空 |
| 地区价格互相覆盖 | 地区进入观察和当前投影唯一键 | 全国价和区域价并存 |
| 每次采集都要留点 | 追加式 `price_observation` | 相同价格的不同采样时间仍有独立证据 |
| 历史表太大导致最新价慢 | 小型 `price_current` 指针表 | 最新查询不扫描历史，也不复制金额事实 |
| 补采旧数据覆盖新数据 | 历史允许乱序，投影按时间单向推进 | 重放和回补安全 |
| 条件价污染直接售价 | `price_type`、`pricing_basis`、`fee_status`、`quality_status` 门禁 | 只有无条件、计价基础和费用口径清晰的价格能成为当前可信价 |
| 批发均价被误当成零售价 | 独立 `price_nature` | 零售报价、批发报价、零售均价和批发均价分别查询和解释；无人民币含义的指数不入价格表 |
| 页面失败后可信价被清空 | 失败只进入审计，不推进当前指针 | 异常批次不会覆盖可信数据 |
| 无法证明价格来源 | `crawl_run`、`crawl_record`、证据路径与哈希 | 可重放、可定位适配器版本、可审计 |
| 个人站结构差异大 | `source_channel` 抽象来源类型，来源属性保留 JSON | 核心库不因站点差异改表 |
| 数据库量增长过快 | 全国价不复制地区；正文外置；当前与历史分离 | 控制重复数据和主库查询压力 |

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

## 13. 数据量与归档

点时快照的年数据量近似为：

```text
来源商品数 × 实际采样地区数 × 每日采样次数 × 365
```

例如 10 万个来源商品、3 个地区、每天 1 次即约 1.095 亿条/年。因此：

- 全国统一价格只保存一份 `NATIONAL/CN` 观察；
- 不为未验证存在价格差异的所有行政区机械复制数据；
- 原始网页、JSON 和截图外置，MySQL 只保留元数据；
- `price_current` 保持小表，业务查询避免直接扫描完整历史；
- 在掌握真实增长速度前，不提前做分库分表；
- 达到容量门槛后，优先采用按年度/季度归档表或独立历史库。

当前公司使用 MySQL 5.7。该版本中，分区 InnoDB 表与外键不兼容，因此 V2 初期不建议给带完整证据关系的 `price_observation` 直接增加原生分区；否则必须放弃数据库外键或改变归档方案。参考 MySQL 5.7 官方限制说明：[Partitioning Limitations Relating to Storage Engines](https://dev.mysql.com/doc/mysql-reslimits-excerpt/5.7/en/partitioning-limitations-storage-engines.html)。

## 14. MySQL 5.7 兼容策略

1. MySQL 5.7 会解析但不执行 `CHECK`，关键约束继续使用应用校验和 `BEFORE INSERT/UPDATE` 兼容触发器；参考 [MySQL 5.7 CREATE TABLE](https://dev.mysql.com/doc/refman/5.7/en/create-table.html)；
2. 不使用部分唯一索引；“每个来源版本只有一个开放的 ACCEPTED 匹配”通过事务、触发器和集成测试保证；
3. 不依赖递归 CTE，品类树使用 `path` 物化路径；
4. 常用查询字段使用普通列，JSON 只放长尾属性，不依赖 MySQL 8 的 JSON 索引能力；
5. 所有哈希和业务代码采用短 ASCII 字段，避免 `utf8mb4` 复合索引过长；
6. 保持 `DATETIME(3)` UTC、`DECIMAL` 金额和显式事务；
7. MySQL 5.7 与 8.x 的迁移和集成测试继续使用专用测试库，不在公司业务库执行破坏性测试。

为处理 `source_listing.current_revision_id` 的受控循环引用，推荐建表顺序为：标准商品表 → `source_channel` → `merchant` → 暂不带当前版本外键的 `source_listing` → `crawl_run` → `crawl_record` → `listing_revision` → 补充当前版本外键 → `listing_match` → `price_observation` → `price_current`。

## 15. 与 V1 表的映射

| V1 | V2 | 处理方式 |
| --- | --- | --- |
| `brand` | `brand` | 保留并扩宽语义，商品引用可为空 |
| `category` | `category` | 增加层级路径和品类规则版本 |
| `product` | `catalog_item` | 从“官方型号”改为平台无关商品族 |
| `sku` | `item_variant` + `listing_revision` | 标准规格与来源规格分离 |
| `sales_channel` | `source_channel` | 移除品牌和官方直营硬约束 |
| 无 | `merchant` | 新增卖家边界 |
| `official_offer` | `source_listing` | 改为任意来源、任意卖家的可销售规格，并增加当前身份版本指针 |
| 无 | `listing_revision` | 新增来源规格版本，防止链接换货 |
| 无 | `listing_match` | 新增标准化匹配决策 |
| `price_history` | `price_observation` | 从变化区间改为每次采集的点时快照 |
| `price_current` | `price_current` | 改为最新可信观察指针，地区进入唯一键 |
| `crawl_run` | `crawl_run` | 增加地区、策略版本和质量计数 |
| `crawl_record` | `crawl_record` | 增加来源商品、重放关系和证据清单 |

## 16. 已执行的重建与后续迁移原则

2026-08-25 经明确授权采用“完整备份后重建目标 schema”，而不是逐行迁移无价值的 V1 demo 数据：

1. 只删除并重建 `device_price`，重建前后核对其他 schema 对象清单；
2. 从 Alembic base 升级到 `96524222b3ec`，由权威迁移链创建 10 张 V1 和 13 张 V2 业务表；
3. V2 从空表初始化，不把 V1 区间历史转换成不存在的点时观察；
4. 新政府采集链路只写 V2，不增加 V1/V2 双写；
5. 两个来源先种子初始化，再显式启用并手工采集；
6. 关闭来源即可停止新增数据，既有点时事实不自动删除；
7. V1 表在 2026-08-25 重建后为空，已于 2026-09-10 单独重采恢复设备 demo；本轮保留静态数据，不为物理删表或去掉 `v2_` 前缀增加新的迁移复杂度。

后续数据库结构变更仍必须通过 SQLAlchemy 模型和 Alembic 共同演进，并先在专用 MySQL 5.7/8.x 验证。公司库重建过程和恢复点见 [V2 阶段 H 公司库验收报告](V2_PHASE_H_COMPANY_ACCEPTANCE_REPORT.md)。

## 17. 当前暂不增加的表

为控制复杂度，本阶段明确不增加：

- `product_alias`：来源名称通过 `listing_revision` 保留，规范化同义词放版本化配置；
- `starting_price`：起售价仅作为拒绝的价格类型，不形成可信价格；
- `region`：当前只需要地区变量，暂不维护行政区维表；
- `category_attribute_definition/value`：不采用纯 EAV 属性模型；
- `reference_price_snapshot`：市场区间、中位数等属于数据应用或聚合层，不混入原始价格事实；
- `claim`、`claim_item`、`customer`：理赔案件和个人信息不属于本项目；
- `evidence_artifact`：当前以 `crawl_record.artifact_manifest` 管理，出现独立保留和权限需求后再拆；
- 调度任务表和消息队列表：当前继续使用现有调度方案，数据库不提前承担分布式队列职责。

## 18. 生鲜数据示例

假设上海市发展改革委发布某日“鸡蛋平均零售价 5.76 元/500 克”，文章附带同日 `.xls`：

| 表 | 示例内容 |
| --- | --- |
| `category` | `/FOOD/FRESH/EGG/` |
| `catalog_item` | 鸡蛋，无强制品牌 |
| `item_variant` | 政府监测口径、500 克报价单位 |
| `source_channel` | 上海市主要主副食品价格信息，`source_type=PUBLIC_DATA` |
| `merchant` | 来源专用合成公共市场记录 |
| `source_listing` | 上海零售均价 + 鸡蛋 + 500 克形成稳定键 |
| `listing_revision` | 保存来源名称、规格、单位和身份指纹 |
| `price_observation` | `CITY/310100`、`RETAIL_AVERAGE`、5.76、`PUBLISHED_VALUE`、`UNIT_QUOTED`、原价为空、来源数据日期及证据哈希 |
| `crawl_record` | 保存实际下载时间、文章 URL、附件 URL、文件哈希和存储位置 |
| `price_current` | 指向上海该口径最新来源日期的可信发布值 |

第二天发布新数值时新增点时观察并推进当前指针。重复下载同一日同一附件不重复写入；同日附件被官方修订时新增修正观察，旧值不覆盖、不删除。这样查询端既能得到最新发布值，也能区分价格日期与系统获取日期。

## 19. 当前已确认与后置事项

当前阶段已经确认：

1. 保存质量门禁拒绝结果和稳定原因，但拒绝值不推进 `price_current`；
2. 地区只使用全国、省、市三级明确代码，不从市场名推断更细粒度；
3. 零售均价和批发均价分别保存、分别查询；
4. 政府数据的 `original_price` 默认为空，前一日价格和环比不映射为原价；
5. V1 数据不回填 V2；V1 已于 2026-09-10 重新采集设备 demo，V2 已从政府来源采集，设备 V2 将直接从官方来源重采；
6. 第一版继续使用 13 张表，不增加地区维表、别名表、调度表或证据文件表。

原始证据保留期限、大规模历史归档阈值和 V1 兼容表最终清理时间在真实运行产生容量数据后再决定，不影响当前 demo 使用。
