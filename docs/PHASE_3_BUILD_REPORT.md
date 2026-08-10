# 阶段 3 构建报告：Apple 与小米

> 构建日期：2026-08-08  
> 开发状态：已完成  
> 阶段退出状态：待公司授权后的 smoke test、人工价格抽查和两个调度周期  
> 生产调度：保持关闭

## 1. 本阶段边界

阶段 3 实现 Apple 与小米中国大陆官方直营商城 adapter，并继续遵守 V1 口径：

- 只处理手机、平板、笔记本、台式电脑和手表；
- 只保存 SKU 的人民币直接销售总价；
- 保存页面明确展示的原价和当前售价；
- 不使用列表起售价；
- 不使用国补、优惠券、换购、会员、月供、定金或到手价；
- 不采集 REDMI、手环、耳机、保护壳、显示器和第三方商城；
- 不登录、不处理验证码、不破解签名或风控；
- 不绕过阶段 0 的生产授权门禁。

## 2. 公开页面结构复核

### 2.1 Apple

人工只读复核了以下官方入口：

- <https://www.apple.com.cn/shop/buy-iphone>
- <https://www.apple.com.cn/shop/buy-ipad>
- <https://www.apple.com.cn/shop/buy-mac>
- <https://www.apple.com.cn/shop/buy-watch>

结论：

- 列表页用于发现产品系列，但其中的“RMB ... 起”不入库；
- 产品选择页的 SKU 链接包含 Apple 料号；
- SKU 节点提供 `dimension*` 配置和 `current_price` 总价；
- 配置维度按页面实际字段保留，可包含容量、颜色、内存、屏幕尺寸、连接方式和处理器；
- 分期月供、换购和国补文案不参与价格解析；
- Mac 列表中的显示器被明确排除。

### 2.2 小米

人工只读复核了小米商城商品页：

- <https://www.mi.com/shop/>
- <https://www.mi.com/shop/buy/detail?product_id=21871>

结论：

- 静态 HTML 不能完整表达全部 SKU；
- 页面运行后通过版本、颜色和套装选项展示具体总价；
- 页面会访问小米自有商品接口，但直接请求返回 406，程序不尝试规避；
- adapter 使用正常浏览器页面交互，只枚举“版本 × 颜色”；
- 套装固定为“标准版”，不点击购物车，不选择保障服务；
- 每个组合只保存 `.product-con` 最小渲染片段，而不是复制整页；
- 营销描述中的政府补贴或赠品不会污染 `.price-info` 中的直接销售价；
- 如果价格节点本身是国补到手价等条件价格，整项拒绝入库。

## 3. 已完成实现

### 3.1 Apple adapter

- 四个核心品类入口发现；
- 官方 `/shop/buy-*` 路径约束；
- 系列页与具体 SKU 链接区分；
- Apple 料号作为官方 SKU/offer ID；
- 通用 `dimension*` 配置解析；
- 原价、当前售价和缺货状态解析；
- MacBook 归类为笔记本，iMac/Mac mini/Mac Studio/Mac Pro 归类为台式机；
- 显示器、配件和外部链接过滤。

### 3.2 小米 adapter

- 小米商城公开入口发现；
- 手机、Pad、Book、Watch 分类；
- REDMI 和范围外商品过滤；
- 浏览器多规格快照计划；
- 仅枚举版本和颜色，固定标准版；
- 内存、容量、柔光版等配置规范化；
- 当前售价、明确原价和销售状态解析；
- 无官方 SKU ID 时使用稳定配置指纹幂等识别。

### 3.3 通用运行能力

- 内置 adapter 注册表；
- `device-price adapters` 查看镜像内 adapter；
- `device-price crawl --brand ... --mode full` 手工运行；
- `device-price replay --record-id ...` 离线重放；
- `device-price scheduler` 启动异步单进程调度器；
- APScheduler 与 HTTP 客户端共用同一 asyncio 事件循环；
- `LIVE_CRAWL_ENABLED=false` 默认禁止真实采集；
- 开启真实采集时，User-Agent 不能继续使用示例联系人。

## 4. Fixture 和证据策略

测试 fixture 是根据人工观察结果重新制作的最小脱敏结构，不保存完整官网页面、图片、脚本、Cookie 或个人信息。

覆盖场景：

- Apple 产品发现、手机多 SKU、Mac 多配置维度；
- Apple 当前价、明确原价、缺货、条件价格拒绝；
- 小米四类核心设备发现；
- REDMI、手环、配件和第三方链接过滤；
- 小米多版本、多颜色、柔光版、缺货；
- 营销文案包含补贴但直接售价仍明确；
- 价格节点本身为国补到手价时拒绝；
- Apple、小米完整入库和原始证据离线重放。

## 5. 本地命令

```bash
make adapters
make crawl BRAND=APPLE MODE=full
make replay RECORD_ID=123
make scheduler
```

后三个涉及真实运行或数据库的命令遵守各自门禁。未设置 `LIVE_CRAWL_ENABLED=true` 时，`crawl` 和 `scheduler` 会在任何商城请求或数据库写入前退出。

## 6. 验收结果

验证环境：Python 3.12、Docker MySQL 8.4、Playwright Chromium。

```bash
uv run ruff check .
uv run mypy src
uv run pytest -m "not integration"
RUN_MYSQL_INTEGRATION=1 uv run pytest
```

结果：

- Ruff：通过；
- mypy strict：通过；
- 非集成测试：52/52 通过；
- 完整测试：61/61 通过；
- Apple 与小米双品牌 fixture 入库：通过；
- 两品牌原始证据离线重放：通过；
- 阶段 0～2 回归测试：通过；
- 生产镜像 `device-price-service:phase3`：构建通过；
- 镜像运行用户 `65532:65532`：验证通过；
- 容器内 Chromium 151.0.7922.34：启动通过；
- 容器内置 adapter 列表：验证通过；
- 容器默认真实采集门禁：验证为拒绝运行。

## 7. 未完成的阶段退出条件

以下事项必须在公司环境和审批完成后执行，本次开发不能代替：

1. 法务或数据负责人批准 Apple、小米的用途、频率和 User-Agent；
2. 技术负责人批准首次品牌 smoke test；
3. 每个品牌选择少量固定产品做低频人工触发；
4. 将数据库价格与人工打开的官方页面逐项核对；
5. 检查发现数量、SKU 数量、失败率和价格异常；
6. 连续运行两个调度周期，确认无重复 SKU 和历史污染；
7. 验收完成前保持 `LIVE_CRAWL_ENABLED=false`。

因此，本报告中的“开发完成”不等于“阶段 3 已获准生产上线”。
