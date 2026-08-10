# 阶段 2 构建报告

> 完成日期：2026-08-08  
> 阶段状态：已完成  
> 真实商城访问：未进行

## 1. 阶段目标

阶段 2 建立与品牌无关的完整采集框架。使用 fixture 假适配器跑通：

```text
发现商品 → 安全抓取 → 原始证据 → 品牌解析 → 统一模型
         → 质量校验 → MySQL 事务入库 → 批次审计 → 离线重放
```

Apple、小米、华为、OPPO、vivo 的实际页面规则不属于本阶段。

## 2. 已完成能力

### 2.1 品牌适配器

- `BrandAdapter` 抽象接口；
- `discover`、`fetch_product`、`parse_product`、`normalize` 四段职责；
- `AdapterContext` 只提供受控 HTTP、浏览器和渠道白名单；
- `AdapterRegistry` 按渠道唯一注册，拒绝重复或不存在的 adapter。

### 2.2 抓取安全

- 只允许 HTTPS；
- 只允许精确域名，不隐式放行子域名；
- 禁止 URL 中携带用户名和密码；
- 禁止非 443 HTTPS 端口；
- HTTP 重定向在请求下一跳之前重新检查白名单；
- 浏览器拦截跳出白名单的主页面导航；
- 每域名并发、请求间隔和随机抖动；
- 明确连接、读取、响应大小和重定向上限；
- 429 和部分 5xx 使用有限退避重试；
- 不实现登录、验证码绕过或签名破解。

### 2.3 原始证据与重放

- 原始 HTML/JSON 使用 gzip 保存；
- 文件名使用响应内容 SHA-256；
- 同一内容幂等保存；
- `crawl_record` 保存相对路径和哈希；
- 读取时防止路径穿越并校验内容完整性；
- `ReplayService` 从数据库定位渠道和 adapter，离线重新解析、规范化和校验；
- 重放默认不改写业务数据。

### 2.4 统一模型与价格策略

- `DiscoveredProduct`、`ParsedProduct`、`NormalizedProduct/Sku/Offer`；
- 稳定的 SKU 属性 SHA-256 指纹；
- 文本、容量和属性键规范化；
- CNY 金额使用 `Decimal`；
- 原价类型和空值语义校验；
- 直接拒绝国补、补贴、券后、会员、换购、月供、起售价、到手价等有条件或不明确价格；
- 多金额文本视为歧义，不自行猜测。

### 2.5 数据质量

- 品牌和渠道必须与 adapter 一致；
- 产品页和报价页必须属于白名单；
- SKU 指纹、官方 SKU ID 和报价 ID 去重；
- V1 每个 SKU 在规范化渠道中必须恰好一个报价；
- 在售和预售必须有明确直接销售总价；
- 原价低于当前价时告警；
- 支持对超过阈值的价格变化产生告警。

### 2.6 批次流水线

- MySQL `GET_LOCK` 防止同渠道任务重叠；
- 创建、完成和失败批次状态；
- 超时遗留的 `RUNNING` 批次自动标记 `FAILED/STALE_RUN`；
- 单商品事务写入产品、SKU、报价、当前价格、价格历史和采集审计；
- 单商品失败不覆盖已有可信价格，其他商品继续处理；
- 同一发现批次重复产品跳过并审计；
- 全成功、部分成功和全部失败分别落为 `SUCCEEDED/PARTIAL/FAILED`。

### 2.7 调度

- APScheduler 单进程定时调度；
- 每渠道独立 job；
- `max_instances=1`、合并错过执行和误触发宽限；
- 数据库命名锁作为跨进程第二层防重；
- 没有注册 adapter 时拒绝启动；
- 不使用 Redis 或消息队列。

## 3. 关键实现文件

- `src/device_price_service/crawlers/base.py`
- `src/device_price_service/crawlers/registry.py`
- `src/device_price_service/fetchers/url_policy.py`
- `src/device_price_service/fetchers/http.py`
- `src/device_price_service/fetchers/browser.py`
- `src/device_price_service/domain/crawl.py`
- `src/device_price_service/domain/price_policy.py`
- `src/device_price_service/normalization/specs.py`
- `src/device_price_service/validation/rules.py`
- `src/device_price_service/services/artifact_store.py`
- `src/device_price_service/services/crawl_pipeline.py`
- `src/device_price_service/services/replay_service.py`
- `src/device_price_service/services/locks.py`
- `src/device_price_service/scheduler.py`

## 4. 测试结果

验证环境：Python 3.12、Docker MySQL 8.4。

```bash
uv run ruff check .
uv run mypy src
uv run pytest -m "not integration"
RUN_MYSQL_INTEGRATION=1 uv run pytest
```

结果：

- Ruff：通过；
- mypy strict：通过；
- 单元测试：38/38 通过；
- 完整测试：46/46 通过；
- fixture 完整流水线：通过；
- 原始证据离线重放：通过；
- MySQL 命名锁防重：通过；
- 陈旧批次恢复：通过；
- 阶段 1 迁移和价格历史回归：通过。
- 生产 Docker 镜像 `device-price-service:phase2`：构建通过；
- 容器 CLI 入口：验证通过；
- 容器内 Chromium 151.0.7922.34：以非 root 用户启动通过。

## 5. 浏览器运行环境

本地首次使用浏览器抓取前执行：

```bash
make browser-install
```

生产 Dockerfile 使用与 Python Playwright 包版本对应的官方基础镜像，预装 Chromium 及系统依赖，应用进程以非 root 用户运行。HTTP 抓取仍是默认路径，品牌 adapter 只有在公开页面必须执行 JavaScript 时才能选择浏览器兜底。

## 6. 阶段边界和下一步

当前没有任何真实品牌 adapter，因此调度器不会启动真实任务，也没有访问官方商城。

阶段 3 应先实现 Apple 与小米：

1. 保存并脱敏少量人工 fixture；
2. 实现官方分类或 sitemap 商品发现；
3. 实现商品配置、SKU 和价格解析；
4. 为正常、缺货、预售、无原价和条件促销页面建立快照测试；
5. 通过 fixture 后，再按阶段 0 门禁执行少量人工 smoke test；
6. 不在阶段 3 中修改通用价格口径来迎合某个站点。
