# artex 子命令详解

所有命令的公共旗标(置于子命令名之前):

```
--as <客户码|portal login|identities 别名>   # 以该客户身份(portal session)执行
--env-file <path>                            # 覆盖 .env(默认 ARTEX_ENV_FILE 或 ~/ar/chatbot/.env)
--password <cred>                            # 一次性提供 portal 凭据(不推荐,会进 shell 历史)
```

输出约定:stderr 首行 `# env: <db> @ <host> | identity: ...` 标明本次查询的
环境与身份。业务数据在 stdout,为繁体中文格式化文本(与 chatbot 客户端所见一致)。

## 公共目录(系统账号即可)

```bash
# 6 位产品码查询:价格前的库存/规格/状态(stock_info JSON 含 available/low_stock/sold_out)
artex.py product 660302
artex.py product 660302 --pattern 素色 --width 150

# 按名称模糊搜产品
artex.py product-search "cotton poplin" --limit 10

# 品牌/系列(含分类链接)
artex.py brand carnet

# collections/系列搜索(含品牌与 bunch;oa_chat customer sales report 同定义)
artex.py collections Fantasia            # 模糊匹配
artex.py collections 8239 --exact --limit 5

# bunch(= sample book 色卡册,官方术语 bunch;4 位册号)内在库产品
artex.py bunch 6606
```

## 客户资料(必须 --as)

```bash
# 客户视角(真实 portal 权限:只见自己的订单/最终价格)——本 skill 唯一的视角
artex.py --as E0056 orders                          # 近 90 天订单
artex.py --as E0056 orders --days 30 --state sale
artex.py --as E0056 order SO00123                   # 订单详情
artex.py --as E0056 order-history --product 660302  # "我以前订过这个吗"(portal 自动限本人)
artex.py --as E0056 dye-lot 660302                  # 配同匹/批次
artex.py --as E0056 addresses
artex.py --as E0056 price-quote "660302 1600 5Y"    # portal 只见最终价
artex.py --as E0056 carriers --order SO00123        # 物流商+运费(portal 钉死本人)
artex.py --as E0056 small-order-check SO00123 660302
```

`--as` 的解析顺序:identities 别名文件(`~/.config/artex/identities/<别名>.env`)
→ 含 `@` 视为 portal login → 其余按客户码解析(res.partner.code → 该客户唯一
portal 账号)。多 portal 账号或无 portal 账号时报错并给出候选。

> 本 skill 面向客户使用,内部办公不用它(2026-11-06 定)。
> `customer` 客户搜索命令(结果含内部备注)已移除,不留任何仅内部命令。

## 全局配置(set / unset)

配置文件 `~/.config/artex/config.env`(0600,目录 0700),纯本地操作、不碰网络:

```bash
artex.py set                                    # 列出全部配置 + 当前生效环境
artex.py set odoo_base_url                      # 只看某一项(api_key 自动打码)
artex.py set odoo_base_url https://main-artextile-uat-39657068.dev.odoo.com
artex.py set odoo_login customer@wear.com
artex.py set odoo_api_key 9f8e7d6c...           # 非 40 位 hex 会警告但仍保存
artex.py set odoo_db main-artextile-uat-39657068
artex.py unset odoo_base_url
```

| 设置项 | 作用 | 默认 |
|---|---|---|
| `odoo_base_url` | Odoo 地址,**全局开关**(压过 .env 与 identities 坐标) | `https://www.artextilestore.com` |
| `odoo_login` | 默认身份:设置后不带 `--as` 直接以该 portal 身份运行 | 无(系统身份) |
| `odoo_api_key` | 全局 API key,凭证链兜底;一律走 /jsonrpc | 无 |
| `odoo_db` | 数据库名;`*.dev.odoo.com` 域名可自动推断(子域即库名) | 按推断/环境 |

**环境坐标优先级**

- URL:`odoo_base_url` > identities 文件 `ARTEX_ODOO_URL` > `.env` 的 `ODOO_URL` > 生产默认
- DB:`odoo_db` > 按 URL 推断(`*.dev.odoo.com`) > identities `ARTEX_ODOO_DB` > `.env` 的 `ODOO_DB` > 生产默认(仅生产域)
- 凭证:`--password` > identities `ARTEX_CREDENTIAL` > `ARTEX_PORTAL_PASSWORD` > `odoo_api_key`
- 身份:显式 `--as` > `odoo_login` > 系统身份

**典型场景**

```bash
# 测试期切 UAT(一行;所有命令含既有 --as 身份都跟着切)
artex.py set odoo_base_url https://main-artextile-uat-39657068.dev.odoo.com

# 客户快速启动(拿到 Odoo 签发的 API key 后三行;之后 artex orders 直接跑)
artex.py set odoo_login customer@wear.com
artex.py set odoo_api_key <key>
# 生产默认地址可省 odoo_base_url;UAT 则按上面一行切换
```
