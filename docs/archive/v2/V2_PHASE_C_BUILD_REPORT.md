# V2 阶段 C 构建报告：通用采集契约

> 历史归档：本文件保留当时的方案、结果和限制，不作为当前操作指南；其中旧命令、待办或授权不可直接沿用。当前入口见[文档导航](../../README.md)与[项目状态](../../PROJECT_STATUS.md)。

> 完成日期：2026-08-22
> 状态：本地开发与 MySQL 5.7/8.x 专用数据库验收通过
> 生产影响：无；未连接或修改公司 MySQL，未访问真实电商平台

## 1. 本阶段结论

阶段 C 已完成。V2 不再只有可单独调用的 Repository，而是具备从来源发现一直写到点时价格和当前投影的通用采集路径。平台、品类和核心价格策略已经分离，后续增加生鲜规则或新来源不需要复制数据库事务。

## 2. 交付内容

### 2.1 连接器与领域契约

- `CatalogConnector`：统一来源发现、共享抓取和平台解析接口；
- `CatalogConnectorRegistry`：按来源渠道唯一注册连接器；
- `DiscoveredCatalogListing`：稳定来源商品、卖家、品类和价格性质；
- `ParsedCatalogListing`：页面标题、原始属性和价格候选；
- `NormalizedListingIdentity`：可比属性、数量、基本单位、质量和确定性指纹；
- `CatalogCollectionRequest`：显式地区与品类运行范围。

### 2.2 品类和价格边界

- `CategoryRuleRegistry` 按 `profile_code + version` 精确查找规则，不静默回退版本；
- `CatalogPricePolicy` 统一接受直接无条件价和人民币公开市场发布值；
- 条件价、不明费用、不明地区、估算计价和不一致单位价使用稳定原因拒绝；
- 包装数量可精确换算时，中央策略用 `Decimal` 派生六位小数单位价。

### 2.3 V2 流水线

`CatalogCrawlPipeline` 已实现：

- 来源/地区级 MySQL 命名锁；
- 数据库渠道与连接器代码一致性门禁；
- 域名白名单下复用现有 HTTP 与 Browser fetcher；
- 原始证据压缩保存和 `crawl_record` 索引；
- 卖家、来源商品和来源版本幂等写入；
- 价格候选追加、当前价推进和规格切换失效；
- 重复发现、越界品类、HTTP 异常和解析异常审计；
- 批次接受、待复核、拒绝和失败计数。

原始证据目录从“品牌代码”语义调整为“来源代码”，V1 调用方同步使用渠道代码；文件内容和完整性校验行为不变。

## 3. 验证结果

```text
ruff check .
  passed

mypy src
  passed (54 source files)

pytest -m "not integration"
  103 passed

RUN_MYSQL_INTEGRATION=1 pytest -m integration
  25 passed (MySQL 8.4)

RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_PORT=3308 pytest -m integration
  25 passed (MySQL 5.7.36)
```

新增端到端覆盖：

- 5kg 商品连续同价采集形成两条点时观察；
- 同一来源链接从 5kg 改为 10kg 后形成新版本，当前价指向新身份；
- 优惠券价进入拒绝事实但不替换当前可信价；
- 解析失败保留原始证据和失败记录，不产生价格；
- 单位价由包装总价和精确重量计算；
- 连接器提供的冲突单位价被中央门禁拒绝；
- 品类规则版本缺失时显式失败，不采用其他版本。

全部数据库测试只使用本机专用 `device_price_test`，未连接公司实例。

## 4. 当前边界与下一阶段

本阶段完成时尚未实现真实大型电商连接器、生产 CLI、通用调度注册、标准商品自动匹配或生鲜具体规则。生鲜具体规则随后已在阶段 D 完成；真实平台、生产调度和标准商品自动匹配仍未接入。匹配不是保存来源价格的前置条件，后续可以在不改价格事实的情况下建立。

下一阶段为阶段 D：实现首批生鲜规则包、数量/单位词法、四类脱敏 fixture 和重放验收。真实平台访问仍留到阶段 E，先确认许可和稳定性。
