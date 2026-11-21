---
name: artextilestore
description: |
  Artextile 面料业务数据查询与操作 CLI(基于 AR Chatbot 工具层,连 Odoo)。

  ⚠️ skill 选择规则:凡涉及 Artextile 业务数据的问题都用本 skill
  (产品/库存/价格/订单/客户/色卡/染缸/物流/报价)。

  触发词(任一匹配即调用):
  查产品、查库存、有没有货、产品查询、bunch、色卡、样册、collection、系列、染缸、批次、配同匹、
  查订单、订单状态、查客户、客户地址、报价、多少钱、运费、物流商、
  660302、E0056、SO0123 之类的产品码/客户码/订单号
---

# artextilestore — Artextile Odoo 业务查询

## Run

```bash
~/ar/chatbot/.venv/bin/python ~/.agents/skills/artextilestore/artex.py <command> [args]
# 全局旗标(放在子命令前):--as <客户> / --env-file <path>
```

先跑一次 `artex.py doctor` 可确认环境与连通性;`artex.py list` 列出全部能力。

## 客户登录(推荐入口:artex login)

```bash
artex.py login --bridge https://uat.artexbridge.com   # ARTEX_BRIDGE_URL 可替代 --bridge
```

生成授权链接(自动打开浏览器)→ 用户在 Odoo 页面**一次确认** → CLI 自动取回专属 API key 写入 `~/.config/artex/identities/<客户码>.env`(0600)。全程不向 bridge/CLI 提供密码;key 可随时在 Odoo 用户设置 → API Keys 撤销,撤销后重跑 `artex login` 即可。之后所有 `--as <客户码>` 直接可用。

## 全局配置与快速启动(artex set)

```bash
artex.py set                                # 查看全部配置与生效环境
artex.py set odoo_base_url https://main-artextile-uat-39657068.dev.odoo.com   # 测试期切 UAT
artex.py set odoo_login customer@wear.com   # 默认身份(可不带 --as 直接跑)
artex.py set odoo_api_key <40位hex>         # 直接配 API key(客户快速启动)
artex.py set odoo_db <库名>                 # 仅自定义域名时需要(dev.odoo.com 自动推断)
artex.py unset odoo_base_url                # 清除一项
```

- 存 `~/.config/artex/config.env`(0600)。`odoo_base_url` 默认 `https://www.artexilestore.com`(生产)。
- **`odoo_base_url` 一经设置即全局生效**,压过 .env 与 identities 文件里的环境坐标——切 UAT 一行搞定;`*.dev.odoo.com` 域名自动推断库名。
- 快速启动三件套:`set odoo_base_url`(可省)+ `set odoo_login` + `set odoo_api_key`,之后 `artex orders` 不带 `--as` 即以该客户身份执行(显式 `--as` 仍优先)。
- 凭证优先级:`--password` > identities 文件 > `ARTEX_PORTAL_PASSWORD` > config 的 `odoo_api_key`。

## 身份与权限(最重要,先读)

| 你要做什么 | 怎么调 |
|---|---|
| 公共目录:产品/库存/色卡/品牌 | 直接调(系统账号) |
| 以客户视角查订单/地址/报价/历史 | 加 `--as <客户码或login>`,走该客户真实 portal session |

铁律:
1. **客户视角操作必须 `--as`**——查询结果与下单权限属于该客户在 Odoo 的真实权限(portal 看不到原价/折扣/他人订单)。绝不能用系统账号替客户查/下单。
2. 本 skill **面向客户使用,内部办公不用它**(2026-11-06 定):无任何内部视角入口(`--internal-view`、客户搜索已移除);内部办公查数据走 bridge / oa_chat / odoo_app。
3. **下单(2026-11-21 开放)仅限客户本人 portal 身份**:`order-create → order-add → order-confirm` 分步执行,每一步服务端(Odoo 模块 `artex_oauth_panfu`)都强制:订单建在调用者自己的 partner、价格由 Odoo 按客户 pricelist 计算(不接受客户端传价)、MOQ/停産/低库存/库存量闸门与 WeChat 渠道一致。内部账号身份不可下单;系统账号连查都不行。

## 下单流程(客户本人身份)

```bash
artex --as E0056 order-create [--shipping ADDRESS_ID]   # 建草稿单(地址用 artex addresses 查)
artex --as E0056 order-add SO01555 660302:3y [--po PO号] [--remark 备注]   # 加行(僅 CODE:QTYy|m)
artex --as E0056 order-show SO01555                      # 核对(草稿也可见)
artex --as E0056 order-confirm SO01555                   # 正式下單(最後一步,須顯式執行)
artex --as E0056 order-cancel SO01555                    # 反悔取消
```

## 速查

| 格式 | 例子 | 说明 |
|---|---|---|
| 产品码 | `660302` | 6 位 default_code;**"660302 1600" 是一个产品**(码+价签) |
| 客户码 | `E0056` / `W0039` | res.partner.code,1-2 字母+3-5 数字 |
| 订单号 | `SO00123` | 用 NAME,不是数字 ID |
| bunch | `6606` | 色卡册的官方术语(旧称 sample book),4 位册号,`bunch` 命令用 |
| 多公司 | Artextile=1,承达=4 | 报价/订单涉及公司时的背景知识 |

## 子命令索引

公共(直接调):`product` / `product-search` / `brand` / `collections` / `bunch`
客户资料(须 `--as`):`orders` / `order` / `order-history` / `dye-lot` / `addresses` / `price-quote` / `carriers` / `small-order-check`
下单(须客户本人 portal 身份):`order-create` / `order-add` / `order-show` / `order-confirm` / `order-cancel`
元:`doctor` / `list` / `set` / `unset`
> 本 skill 面向客户使用,内部办公不用它——无任何仅内部命令(customer 搜索已移除)。
> 术语约定:**bunch** 是 sample book(色卡册)的官方术语,输出与文档一律用 bunch;`collections` 按 oa_chat customer sales report 同定义(Brand & Collection 根类目下全部子类目:品牌/collections/bunch)。

参数与示例:`references/capabilities.md`;Odoo 模型与数据背景:`references/odoo-context.md`。

## 红线

- 客户 portal 凭据优先经 `artex login` 获取(Odoo 签发的 API key,可撤销);identities 文件(`~/.config/artex/identities/<客户码>.env`,`ARTEX_LOGIN=`/`ARTEX_CREDENTIAL=`)不得写入代码、日志或提交;手工密码模式仅作兜底,密码在进程内即抛。
- Odoo 限速 5 req/s:不要写循环批量调用 artex;批量需求一次命令内用 `--limit`,或分少量多次。
- 常见故障(mirror/UAT 构建轮换、DNS 劫持)排障见 `references/odoo-context.md` §环境。
