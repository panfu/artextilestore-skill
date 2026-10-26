# artex 子命令详解

所有命令的公共旗标(置于子命令名之前):

```
--as <客户码|portal login|identities 别名>   # 以该客户身份(portal session)执行
--internal-view                              # 内部视角查客户数据(与 --as 互斥)
--env-file <path>                            # 覆盖 .env(默认 ARTEX_ENV_FILE 或 ~/ar/chatbot/.env)
--password <cred>                            # 一次性提供 portal 凭据(不推荐,会进 shell 历史)
```

输出约定:stderr 首行 `# env: <db> @ <host> | identity: ...` 标明本次查询的
环境与身份;内部视角额外带 `# WARNING: internal view` 警示行。业务数据在
stdout,为繁体中文格式化文本(与 chatbot 客户端所见一致)。

## 公共目录(系统账号即可)

```bash
# 6 位产品码查询:价格前的库存/规格/状态(stock_info JSON 含 available/low_stock/sold_out)
artex.py product 660302
artex.py product 660302 --pattern 素色 --width 150

# 按名称模糊搜产品
artex.py product-search "cotton poplin" --limit 10

# 品牌/系列(含分类链接)
artex.py brand carnet

# 色卡册(4 位册号)内在库产品
artex.py sample-book 6606

# 当前在库产品清单
artex.py stock --limit 20
```

## 客户资料(必须 --as 或 --internal-view)

```bash
# 客户视角(真实 portal 权限:只见自己的订单/最终价格)
artex.py --as E0056 orders                          # 近 90 天订单
artex.py --as E0056 orders --days 30 --state sale
artex.py --as E0056 order SO00123                   # 订单详情
artex.py --as E0056 order-history --product 660302  # "我以前订过这个吗"(portal 自动限本人)
artex.py --as E0056 dye-lot 660302                  # 配同匹/批次
artex.py --as E0056 addresses
artex.py --as E0056 price-quote "660302 1600 5Y"    # portal 只见最终价
artex.py --as E0056 carriers --order SO00123        # 物流商+运费(portal 钉死本人)
artex.py --as E0056 small-order-check SO00123 660302

# 内部视角(跨客户、含原价折扣;输出不得给客户)
artex.py --internal-view orders E0056
artex.py --internal-view price-quote E0056 "660302 1600 5Y"
```

`--as` 的解析顺序:identities 别名文件(`~/.config/artex/identities/<别名>.env`)
→ 含 `@` 视为 portal login → 其余按客户码解析(res.partner.code → 该客户唯一
portal 账号)。多 portal 账号或无 portal 账号时报错并给出候选。

## 仅内部

```bash
# 客户搜索(结果含内部备注 x_studio_customer_remarks —— 物流要求/折扣门槛等)
artex.py customer Marubeni
```
