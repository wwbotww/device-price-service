# V2 通用来源连接器开发契约

> 适用阶段：C 及以后
> 当前目标：政府公开生鲜价格
> 原则：连接器只处理来源差异，品类规则、价格门禁和数据库事务保持共用

## 1. 两种输入形态

项目只保留两种连接器形态：

1. 商品页连接器：一个响应对应一个来源商品，继续使用现有 CatalogConnector；
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

批量入口有意保持很窄：`discover_dataset` 发现一个文档，`fetch_dataset` 下载一次，`parse_dataset` 返回若干 `ParsedCatalogDatasetRow`。每行继续复用 `DiscoveredCatalogListing + ParsedCatalogListing`，不引入第二套领域模型或 ETL Repository。商务部一个品种对应一个 HTML 文档，CLI 的 `ALL` 只是顺序执行 15 个独立数据集，不把多个页面拼成不可追溯的合成证据。

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

## 4. 来源时间

价格事实时间和系统下载时间必须分开：

- price_observation.observed_at：来源明确给出的观测时间或数据日期；
- crawl_record.fetched_at：系统实际下载时间；
- 来源只有日期时按 Asia/Shanghai 当日 00:00:00 转 UTC，并记录 time_precision=DAY；
- 来源没有任何日期时才退回 fetched_at，同时记录 time_basis=FETCHED_AT。

同一来源日期、同一来源行和同一证据的重放必须幂等。批量数据集连接器应为每条价格候选生成 `evidence_hash`：只纳入来源日期、来源商品/市场/地区身份、报价单位和该行当日价格等受该候选直接证明的字段，不纳入页脚、脚本、其他品种或其他市场行。完整 HTML/XLS 的字节哈希仍写入 `crawl_record.raw_hash`，原文件仍是最终审计证据。没有提供行级哈希的普通单商品页面回退使用完整原文哈希。

官方同日修订使某一价格行的 `evidence_hash` 或解析载荷变化时，只为该行写入修正观察并关联 `supersedes_observation_id`；修正身份要求来源商品、版本、地区和来源时间一致。未变化行不得因同一文件的其他内容变化而增长修订链。

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

## 8. 新政府来源接入清单

1. 确认发布机关、公开入口、域名、访问规则、频率和文件格式；
2. 保存一份原始样本用于本地分析，再提交脱敏 fixture；
3. 定义 source_channel，默认禁用；
4. 明确零售/批发性质、地区、日期和单位；
5. 实现文档发现与批量行解析；
6. 覆盖重复重放、同日修订、缺列、未知单位和结构漂移；
7. 在专用 MySQL 5.7 与 8.x 重放；
8. 执行一次低频真实只读 smoke；
9. demo 由操作者按需执行单次采集，不注册 V2 常驻调度。

## 9. 当前来源顺序

1. 主来源（阶段 G～H 已完成）：商务部商务预报“百家日报”，15 个品种的 HTML 市场表格解析；
2. 补充来源（阶段 F 已完成）：上海市发展改革委“主要主副食品品种价格信息表”，HTML 发现 + XLS 批量解析；
3. 其他政府来源在前两个来源稳定后逐个评估，不建设通用 PDF/OCR 或政府站点爬虫平台。

商务部旧站与 Python/OpenSSL 默认配置当前握手失败；实测 TLS 1.2 的兼容密码套件可以访问。连接器显式选择 `TLS12_COMPAT`，仍校验证书链和主机名；共享 HTTP 抓取器的默认 TLS 策略以及上海来源均不受影响。

大型电商评估已经结束，历史结论见 [V2 阶段 E 首轮可行性报告](V2_PHASE_E_FEASIBILITY_REPORT.md)。当前不以取得商业平台 API 作为政府来源阶段的前置条件。
