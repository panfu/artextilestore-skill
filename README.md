# artextilestore-skill

artex CLI skill 的分发仓库——让 Claude Code / ZCode / Codex 具备
Artextile(Odoo)业务数据查询能力(产品/库存/色卡/染缸/客户/订单/报价),
客户视角操作走真实 portal 权限(`--as`),内部视角需显式 `--internal-view`。

## 一句话安装

对任意 coding agent 说:

> Install artextilestore skill for me by reading
> https://github.com/panfu/artextilestore-skill/blob/main/llms.txt

agent 会读取 `llms.txt` 并自动完成安装。手动安装等价于:

```bash
git clone git@github.com:panfu/artextilestore-skill.git ~/ar/artextilestore-skill
bash ~/ar/artextilestore-skill/install.sh
```

## 前置条件

- `~/ar/chatbot` 检出 + `.venv`(artex.py 复用其 `ar_chatbot` 包与依赖)
- `~/ar/chatbot/.env` 配置 `ODOO_URL/ODOO_DB/ODOO_USER/ODOO_PASSWORD`

## 安全

- 仓库内与 skill 输出中**没有也不允许出现**任何凭据;
  凭据只存在本机 `.env` 与 `~/.config/artex/identities/`(0600)。
- 开发源:chatbot 仓库 `skills/artextilestore/`(PR #369);本仓库为分发镜像,
  发布时从开发源同步。

## 验证

```bash
artex doctor     # 环境/凭据/Odoo 连通性
artex list       # 能力与身份矩阵
artex product 660302
```
