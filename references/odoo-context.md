# Odoo 数据背景(artex 查询的背后)

## 环境

- artex 复用 `~/ar/chatbot/.env` 的 `ODOO_URL/ODOO_DB/ODOO_USER_INTERNAL/ODOO_INTERNAL_PASSWORD`。
- 该 .env 通常指向 Odoo.sh **mirror 构建**(生产数据镜像,如
  `main-artextile-mirror2-27410967.dev.odoo.com`)——查询结果是生产级真实数据,
  **只读操作安全,但不要在此环境产生测试数据**。
- 构建编号会轮换销毁。连接失败时先查真实 DNS(本机 DNS 被 TUN 劫持,nslookup 不可信):

  ```bash
  curl -sS "https://1.1.1.1/dns-query?name=<host>&type=A" -H "accept: application/dns-json"
  # 返回 0.0.0.0 = 构建已销毁,需在 Odoo.sh 取新构建并更新 ~/ar/chatbot/.env
  ```

- `artex.py doctor` 会做上述连通性检查并给出提示。

## 身份架构(为什么 --as 是真权限)

chatbot 自 2026-05(PR #87)起 Odoo 调用统一走系统账号,portal 限制靠应用层
(`get_restricted_customer_id()` 在 11 处强制 partner_id 过滤 +
`OrderPermissionChecker`)。artex 在此之上叠加:**`--as` 注入真实 portal
session**(OdooClient 以客户凭据 authenticate),工具方法内所有 Odoo 调用都跑在
该 session 下——Odoo 原生 record rules 与应用层过滤**双保险**生效:

- portal 只能读自己的订单/地址/历史(两层都拦)
- `get_price_quote`:portal 只见最终价,无原价/折扣
- `list_shipping_carriers`:portal 的 customer 参数被钉死为本人 partner_id

本 skill 无内部视角入口(`--internal-view` 已移除):客户资料只能以 portal
身份查,系统账号仅限公共目录。内部办公查数据走 bridge / oa_chat / odoo_app。

## 常用模型速查

| 模型 | 要点 |
|---|---|
| `product.template` / `product.product` | 6 位 `default_code`(如 660302);`product_misc_jsi` 的 Master/Version 商品体系,`master_no` 是字符串匹配非外键 |
| `res.partner` | 自定义 `code` = 客户码(1-2 字母+3-5 数字);portal 用户经 `res.users.share=True` 关联 |
| `sale.order`(+line) | state: draft→sent(报价)→sale→done;cancel;`order_search_by_customer_po_fabric_panfu` 支持按客户 PO 搜 |
| `product.pricelist`(+item) | `applied_on ∈ [2_product_category, 3_global]`;米/码双单位(`secondary_uom_jsi`) |
| `stock.picking` / `stock.move` | 发货与批次(dye lot) |
| 多公司 | Artextile=1,承达贸易(深圳)=4 |

业务规则文档:`~/ar/base/ODOO_SCHEMA.md` / `ODOO_CUSTOM_SCHEMA.md`;生态总览:`~/ar/ECOSYSTEM.md`。
