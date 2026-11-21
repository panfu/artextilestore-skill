# artextilestore-skill

artex CLI skill 的分发仓库——让 Claude Code / ZCode / Codex 具备
Artextile(Odoo)业务数据能力(产品/库存/色卡/染缸/客户/订单/报价/下单),
客户视角操作走真实 portal 权限(`--as`,API key)。**skill 只面向客户使用,
无任何内部视角入口**(内部办公走 bridge/oa_chat/odoo_app)。

**自包含**:不依赖任何 `~/ar` 工作区。仓库自带 vendored 运行时
(`vendor/ar_chatbot` 精简子集)+ 自动创建独立 venv(`~/.config/artex/venv`,
需 python ≥3.10 或 uv)。有 `~/ar/chatbot` 检出的机器自动升级为 full 模式
(直接用该检出与 `.env`,免建独立 venv)。

## 一句话安装

对任意 coding agent 说:

> Install artextilestore skill for me by reading
> https://github.com/panfu/artextilestore-skill/blob/main/llms.txt

agent 会读取 `llms.txt` 并自动完成安装(clone 到 `~/artex-skill-src`)。
手动等价:

```bash
gh repo clone panfu/artextilestore-skill ~/artex-skill-src
bash ~/artex-skill-src/install.sh
```

## 安装后(客户模式)

两种入口,任选:

```bash
# A) 引导式(推荐):浏览器一次确认,取回专属 API key(可随时在 Odoo 撤销)
artex login --bridge <bridge 地址>
artex --as <客户码> orders           # 以客户真实权限查询

# B) 快速启动:拿到 Odoo 签发的 API key 后手工配置(三行,之后不用 --as)
artex set odoo_login <login邮箱>
artex set odoo_api_key <API key>
# 默认连生产 https://www.artextilestore.com;测试期切 UAT:
# artex set odoo_base_url https://main-artextile-uat-39657068.dev.odoo.com
artex orders
```

## 开发与同步

- 开发源:chatbot 仓库 `skills/artextilestore/`(PR #369);本仓库为分发镜像。
- 业务逻辑上游:chatbot `ar_chatbot/`(OdooSalesTools)。
- 发布:`bash scripts/sync-from-chatbot.sh <chatbot 检出>` 重新生成
  `vendor/`(含空壳 `__init__` 隔离重依赖),再同步 `artex.py/SKILL.md/references/`。

## 安全

- 仓库内与 skill 输出中**没有也不允许出现**任何凭据;凭据只存于本机
  `~/.config/artex/`(0600)或内部机器的 `~/ar/chatbot/.env`。
- skill 面向客户:客户资料只能以客户本人 portal 身份查,系统账号仅限
  公共目录;无内部视角入口。
