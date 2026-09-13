# 来源连接器开发契约

本文记录当前代码契约，不承载阶段进度。范围见[开发指南](V2_GENERAL_CATALOG_DEVELOPMENT_PLAN.md)，实测与遗留项见[项目状态](PROJECT_STATUS.md)。

连接器只处理来源差异；产品与数据集共用 `ParsedCatalogRow`、静态行准备和事务写入，由同一注册表管理。没有 V1 包装层或第二套写库管道。

## 1. 两种输入形态

项目只保留两种连接器形态：

1. 产品连接器：`CatalogConnector.discover_products/fetch_product/parse_product`，一个产品结果包含多个 SKU 来源行；当前实现为 Apple、华为、小米、OPPO、vivo 五个原生连接器；
2. 数据集连接器：一个 HTML、XLS 或公开接口响应包含多行价格，使用已实现的 `CatalogDatasetConnector`。

政府来源使用第二种形态：

    discover document
      → fetch once
      → store one crawl_record and evidence hash
      → parse many dataset rows
      → normalize each row with CategoryRule
      → evaluate each price with CatalogPricePolicy
      → persist listing_revision + price_observation + price_current

数据集只下载一次。多条价格观察可以引用同一个 crawl_record，不得为每一行重复请求同一附件，也不得另建一套 ETL 写库逻辑。

### 1.1 设备产品数据契约

- `DiscoveredCatalogProduct`：官方产品 ID、产品 URL、设备分类和发现元数据；
- `ParsedCatalogProduct`：产品 ID/分类、品牌、名称、系列/型号、基础属性，以及至少一行 `ParsedCatalogRow`；
- `ParsedCatalogRow`：`DiscoveredCatalogListing + ParsedCatalogListing`，政府和设备共用。

产品结果必须与发现的官方产品 ID 和分类一致；每行必须归属同一产品/分类，不能包含重复 listing 键或同商家内重复官方 SKU。发现列表含重复产品时，在抓取前整批失败，不能先提交第一份再发现身份歧义。`catalog sources` 当前列出两个政府连接器及五品牌设备；`catalog smoke/crawl/replay` 使用同一注册表，不提供可执行的 V1 采集入口。

五品牌均直接输出 V2 DTO。小米没有可靠官方 SKU 时使用完整规格键，华为保留款式/版本等原始配置维度，OPPO/vivo 保留必要的逐 SKU 详情请求；复合证据包包含各次原始响应及 URL、HTTP 状态、时间、哈希，逻辑产品时点取最后一次响应。任何已声明 SKU 缺少必要身份或有效价格/明确状态，都不能静默删去。

`CatalogConnector.product_not_found_is_definitive` 默认 false，只有整产品端点才可声明 true；仍须原请求/最终 URL 与发现 URL 相同，不能把 SKU 详情 404、跳转或子请求错误用来判整品下架。浏览器非 2xx 返回原始状态页面，不再等待商品选择器。站点特例在 connector，缺失计数、价差复抓与事务在共用服务层。

`services/catalog_preparation.py` 为 smoke 和正式采集提供唯一静态校验路径：来源 URL、请求范围、品类规格、地区及价格语义均在写库前验证。产品额外核对品牌、直营已验证卖家及发现归属；候选不得自行覆盖产品共享证据的时点或哈希。产品任一规格身份不可信时，整个产品不推进可信价；纯价格质量问题按每个 SKU/地区判定，不阻止同产品其他明确规格的可信价格在同一事务内保存。

## 2. 数据集连接器职责

`CatalogDatasetConnector` 负责：

- source channel、连接器代码和版本；
- 发现最新公开文章、附件或数据页；
- 使用共享 HTTP 抓取器和批准域名白名单；
- 验证标题日期、数据日期、附件日期和内容类型；
- 把一份响应解析为多条来源行；
- 输出稳定的来源商品键、品种、规格、单位、市场、地区和价格性质；
- 输出来源观测时间 source_observed_at；
- 对缺列、未知单位和非金额内容给出稳定错误码。

批量入口有意保持很窄：`discover_dataset` 发现一个文档，`fetch_dataset` 下载一次，`parse_dataset` 返回若干 `ParsedCatalogRow`。每行继续复用 `DiscoveredCatalogListing + ParsedCatalogListing`，不引入第二套领域模型或 ETL Repository。商务部一个品种对应一个 HTML 文档，CLI 的 `ALL` 只是顺序执行 15 个独立数据集，不把多个页面拼成不可追溯的合成证据。

连接器不得：

- 自行写 ORM 表或更新 price_current；
- 从市场名称猜测行政区代码；
- 把前一日价格、环比、指数或涨跌幅当作原价；
- 把零售均价和批发均价输出为同一种价格性质；
- 使用未公开接口、个人登录态、验证码绕过或外部聚合数据。

## 3. 稳定身份

数据库中的来源渠道与每一行的 `listing_key` 合并后至少表达：

    source channel
    + market level
    + published market/aggregate identity
    + normalized commodity name
    + explicit specification
    + quoted unit

当前价格、发布日期和附件 URL 不进入稳定键。上海实现使用市场层级、规范品种代码、来源规格和报价单位构成 `listing_key`；文章 URL 只更新 `canonical_url`。商务部实现使用 `commodityid + enterid + 报价单位`：`enterid` 来自页面走势图链接，不从可能变化的市场名称计算身份。同一 `enterid` 出现在两个来源地区时合并为一个 listing、保留两个地区价格。来源把“鸡蛋 500 克”改为“鸡蛋 1 千克”时形成新的来源序列或版本，不能与原规格混价；仅价格变化不建立新版本。

没有卖家概念的政府汇总数据使用来源专用合成 merchant。有明确批发市场的行以市场为 merchant，seller_type 使用 PUBLIC_MARKET。

设备稳定身份规则已在 `normalization/devices.py` 实现：标准产品键使用品牌、来源渠道和官方产品 ID；来源 listing 优先使用官方产品 ID + SKU ID，无官方 SKU 时使用官方产品 ID + 已证实的规格指纹，不虚构外部 SKU。渠道/商家范围仍由共用行和 Repository 维持。

`DeviceSpecification` 保留颜色、容量、内存、连接方式、尺寸、版本、厂商部件号，以及有明确标签的完整附加规格（如款式/版本、处理器、表壳和表带配置）。附加维度使用扁平字符串字段，不掺入价格、标题、库存或抓取时间；未知规格不猜测，完全缺失时拒绝建立可信身份。相同官方 SKU 的规格改变时 listing 键不变，管道据新规格指纹建立 variant/revision；只有标题或价格改变不会形成新规格。

Apple 的 `aosContainerPartNumber`（如 RO 配置容器）不是 SKU。同一容器可以对应多种配置；只有真实 `btrOrFdPartNumber` 才写入 `external_sku_id`。没有真实 SKU 时，必须由 `productConfiguration` 明确提供完整的 `processor/memory/storage` 及其他已声明配置，使用既有规格指纹形成 `listing_key`，`external_sku_id` 保持为空。容器与配置保存在属性中，不生成伪 SKU 或制造商号，不从容器推断配置或金额；重复完整身份仍拒绝，价格仍须由本配置的明确当前价证明。

标准产品按品牌/渠道/官方产品 ID 建档，具体规格与来源 revision 建立精确规则匹配。重采复用同一个匹配及首次 `effective_from`。只有可信观察才能推进当前规格；选择时比较本次可信时点与现当前规格最新可信事实，不能使用包含拒绝报价的 `revision.last_observed_at`。

## 4. 来源时间

价格事实时间和系统下载时间必须分开：

- price_observation.observed_at：来源明确给出的观测时间或数据日期；
- crawl_record.fetched_at：系统实际下载时间；
- 来源只有日期时按 Asia/Shanghai 当日 00:00:00 转 UTC，并记录 time_precision=DAY；
- 来源没有任何日期时才退回 fetched_at，同时记录 time_basis=FETCHED_AT。

同一来源日期、同一来源行和同一证据的重放必须幂等。批量数据集连接器应为每条价格候选生成 `evidence_hash`：只纳入来源日期、来源商品/市场/地区身份、报价单位和该行当日价格等受该候选直接证明的字段，不纳入页脚、脚本、其他品种或其他市场行。完整 HTML/XLS 的字节哈希仍写入 `crawl_record.raw_hash`，原文件仍是最终审计证据。没有提供行级哈希的普通单商品页面回退使用完整原文哈希。

官方同日修订使某一价格行的 `evidence_hash` 或解析载荷变化时，只为该行写入修正观察并关联 `supersedes_observation_id`；修正身份要求来源商品、版本、地区和来源时间一致。未变化行不得因同一文件的其他内容变化而增长修订链。

设备不套用政府同日修订机制：只允许同证据、同时间、同解析/策略及载荷的幂等重放；同一来源 SKU/地区/时点出现另一条非幂等可信事实，整产品事务拒绝，避免增量指针与重建结果不一致。新采集时点即使同价也正常追加。产品证据 manifest 保存当时的发现 DTO、请求范围、品牌/渠道和主响应路径/哈希/时点，不能从后续变化的 listing 猜回旧上下文。

### 4.1 只读重放

`uv run device-price catalog replay --record-id <V2记录ID>` 读取原始文件和已保存的发现上下文，使用当前解析器、品类规则和静态价格门禁处理完整产品的全部 SKU 或整份政府文档。输出分别展示当前静态解析结果与历史校验/已存事实；不联网、不新建批次、不写价格，不将重放时间当作报价时间。来源或品类停用不阻止读取历史证据；来源身份、域名、原始文件和上下文仍必须一致。

变价复核的首次/确认响应逐份校验并展示；静态解析通过不能把历史未确认变价升级为可信事实。明确 404/410 下架记录只展示经核对的原始无价状态事实，不重新构造旧金额。

已部署的旧政府记录可能只有 `source_page`（URL、发布日期）和 `index` 哈希，没有 `discovery_context`。商务部按已保存 URL 的品种 ID、发布日期恢复文档身份；上海按文章 URL、发布日期恢复，随后用原 HTML/XLS 验证。证据不足、身份不一致或文件损坏时明确失败，不从当前 listing 猜测、不补抓、不修改旧记录。历史只有哈希但没有文件路径的发现页面不被假装为可读的已归档文件。

## 5. 政府价格映射

| 来源语义 | 领域值 |
| --- | --- |
| 政府零售平均价 | RETAIL_AVERAGE + PUBLISHED_VALUE |
| 政府批发市场价/平均价 | WHOLESALE_AVERAGE + PUBLISHED_VALUE |
| 明确单位报价 | UNIT_QUOTED |
| 费用口径 | NOT_APPLICABLE |
| 原价 | NULL + NONE |
| 当前可用状态 | UNKNOWN；政府发布不证明商品库存 |

第一版只采“当日价格”。前一日价格即使出现在同一表中也不重复生成历史观察；历史由每日源文件自然形成。价格指数、环比和文字趋势不进入 price_observation。

## 6. 品类规则

CategoryRule 仍只负责来源无关的品种和单位规范化：

- 明确映射品种名称；
- 保存来源规格和报价单位；
- 精确换算 500 克、千克、斤、升和件数；
- 形成确定性身份指纹；
- 未知品种、规格或单位进入 REVIEW_REQUIRED。

规则不得访问网络或数据库，不得因来源名称不同而复制规则。新增品种必须有脱敏 fixture；只增加白名单和规则映射，不增加表。

设备使用独立 `DeviceCategoryRule`（`electronic-device@1`），只接受手机、平板、笔记本、台式机、手表的官方零售来源身份，并验证 `source_attributes.device_specification` 与 listing 键一致；规格以一台/件（`COUNT/PIECE`）规范化。未知规格、分类或稳定键冲突进入 `REVIEW_REQUIRED`。规则已用于五品牌正式管道及 smoke，政府品种/单位规则不受影响。

## 7. 中央质量门禁

政府发布值只有同时满足以下条件才能推进 price_current：

- 官方批准域名和可追溯证据；
- 人民币正数；
- 品种、规格、单位、地区和来源日期明确；
- price_nature 为 RETAIL_AVERAGE 或 WHOLESALE_AVERAGE；
- price_type 为 PUBLISHED_VALUE；
- pricing_basis 为 UNIT_QUOTED；
- 原价为空；
- 来源日期不晚于抓取时间允许的时钟误差；
- 日期、标题和附件内容没有冲突。

任何抓取、解析或规则失败只记录失败审计，不清空上一条可信价格。

设备金额仍须满足具体配置、直接无条件价格和费用口径规则。`AVAILABILITY_ONLY`，仅允许零售报价的已知地区及 `OFF_SHELF/OUT_OF_STOCK/COMING_SOON`，全部金额、单位价单位和促销字段均为空；不能将抓取失败、库存未知或政府无价行认定为合法状态。完整字段门禁见[数据库价格事实设计](V2_GENERAL_CATALOG_DATABASE_DESIGN.md#8-价格事实)。流水线已接入状态证据确认，不允许仅由发现缺失生成此类事实；已见产品少了 SKU 时降级批次，不更改遗漏 SKU。

Apple 同一个金额对象同时提供 `raw_amount` 和展示 `amount` 时，两者都必须经过条件价门禁且金额一致，当前价与原价均适用；不能只取裸数值而忽略展示值中的“起”“券后”等条件。比较仅限同一对象，不把旁边的分期、换新或比较价格当作该报价的另一种表示。

批次按有效结果判定：可信价格和可信状态均计入有效结果，全拒绝或全待复核不能成功，幂等重复写入新增零行仍可成功；同一规格有可信直售价和额外条件价时不误判失败。零售原价低于售价以 `ORIGINAL_BELOW_CURRENT` 留审计，不推进当前价。状态及退出码见[运行手册](OPERATIONS_RUNBOOK.md#结果判断)。

运行、审计和可选调度统一见[运行手册](OPERATIONS_RUNBOOK.md)，本节只约束连接器输出。

## 8. 新政府来源接入清单

1. 确认发布机关、公开入口、域名、访问规则、频率和文件格式；
2. 保存一份原始样本用于本地分析，再提交脱敏 fixture；
3. 定义 source_channel，默认禁用；
4. 明确零售/批发性质、地区、日期和单位；
5. 实现文档发现与批量行解析；
6. 覆盖重复重放、同日修订、缺列、未知单位和结构漂移；
7. 在专用 MySQL 5.7 与 8.x 重放；
8. 执行一次低频真实只读 smoke；
9. 政府 demo 由操作者按需执行单次采集，不加入设备的可选常驻调度。

## 9. 当前来源顺序

1. 主来源：商务部商务预报“百家日报”，15 个品种的 HTML 市场表格解析；
2. 补充来源：上海市发展改革委“主要主副食品品种价格信息表”，HTML 发现 + XLS 批量解析；
3. 其他政府来源在前两个来源稳定后逐个评估，不建设通用 PDF/OCR 或政府站点爬虫平台。

历史实测中，商务部旧站与 Python/OpenSSL 默认配置握手失败，TLS 1.2 的兼容密码套件可以访问。连接器显式选择 `TLS12_COMPAT`，仍校验证书链和主机名；共享 HTTP 抓取器的默认 TLS 策略以及上海来源均不受影响。本地 fixture 验证不等于来源当前可访问，发布前真实 smoke 仍须按门禁执行。

商业平台不在当前接入范围；历史路线决策见[归档](archive/README.md)。
