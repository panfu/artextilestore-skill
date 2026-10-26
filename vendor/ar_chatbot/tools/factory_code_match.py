"""工厂号（供应商编号）→ Odoo 产品匹配。

客户/工厂提供的编号（布厂号，如 VBC 499.893/2、DELFINO 6268M/102/3、
CARNET CDJ501、DRA 14051）不是 Artextile 的六位产品编号。旧的
`_sanitize_product_code` 会去掉全部非数字再取前 6 位 —— "499.893/2" 会被
静默当成 "499893" 查到一个完全无关的产品（错报价/错下单）。

本模块把 bridge `VendorItemMatchService`（~/ar/bridge，2026-09 定稿）的
匹配分层移植到 chatbot，改为针对单条输入、直连 Odoo JSON-RPC 的在线匹配：

  匹配目标（两个代码集的并集，同 bridge）：
    1. product.template.vendor_item_no（stored compute：master 取
       seller_ids[0].product_code，version 继承 master）；
    2. product.supplierinfo.product_code（供应商价格表全量代码 ——
       vendor_item_no 只镜像 seller_ids[0] 一条）。

  分层（同 bridge，确定性优先、不做模糊猜测）：
    L1 exact        — 规则候选（原样 / 末段分隔符转 / / 分段重组 / 全点式）
                      精确命中；
    L2 normalized   — 去除全部非字母数字后的归一键命中；
    ambiguous       — 命中多个不同产品，交客户确认；
    absent_colorway — 编号未命中，但布号（前两段）在库；
    absent          — 布号完全不在库。

  展示产品（每家族一个可售产品，同 bridge 第三版口径）：非 master
  （version）优先 → create_date 较新 → id 兜底；再取其 product.product
  变体的 default_code（即六位产品编号）。

输入分类（classify_code_input）：
  "ar_code"      — 仅数字/空格/横线（可带 AR 前缀）且数字 ≥ 6 位：
                   227707、AR227707、2277 07、227707-1600（code+价签）、
                   2277 07 1600 —— 走原有"取前 6 位"路径，行为不变；
  "factory_code" — 含 "." 或 "/"，或含字母（6268M-0102-0003、CDJ501、
                   2134T/20/9225、3003S），或纯数字恰好 5 位（DRA/Standeven
                   工厂号 14051、24039、30101）—— 必须走工厂号匹配，
                   禁止退回数字截断。
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 展示产品/候选上限：聊天场景单条输入，多候选时列给客户选
MAX_PRODUCTS = 6
MAX_L2_ROWS = 50

STATUS_EXACT = "exact"
STATUS_NORMALIZED = "normalized"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_ABSENT_COLORWAY = "absent_colorway"
STATUS_ABSENT = "absent"

# 全角分隔符 → 半角（同 bridge normalizeCode；客户在手机输入法下常见）
_FULLWIDTH_MAP = {
    "．": ".",
    "。": ".",
    "·": ".",
    "・": ".",
    "－": "-",
    "—": "-",
    "–": "-",
    "−": "-",
    "／": "/",
    "：": ":",
    "\u3000": " ",
    "\u00a0": " ",
    "\u200b": "",
    "\ufeff": "",
}

_AR_PREFIX_RE = re.compile(r"^AR\s*(?=[0-9])", re.IGNORECASE)
_SEGMENTS_RE = re.compile(r"[^0-9A-Z]+")
_ALNUM_KEY_RE = re.compile(r"[^0-9A-Za-z]")
# 仅数字/空格/横线（AR 前缀已剥离）—— 六位产品编号的合法输入形态
_PLAIN_DIGIT_INPUT_RE = re.compile(r"^[\d\- ]+$")


# ==================== 纯函数（可单测，无 Odoo 依赖） ====================


def normalize_code(raw: str) -> str:
    """规范化输入编号：去空白/零宽字符，全角分隔符转半角，大写，折叠空格。"""
    code = str(raw or "").strip().translate(_FULLWIDTH_MAP)
    code = code.upper()
    return re.sub(r"\s+", " ", code).strip()


def strip_ar_prefix(normalized: str) -> str:
    """剥离 AR 前缀（"AR227707" → "227707"；前缀后必须跟字母数字）。"""
    return _AR_PREFIX_RE.sub("", normalized, count=1)


def strip_brand_prefix(normalized: str) -> str:
    """剥离单个纯字母开头的品牌词（"VBC 499.893/2" → "499.893/2"）。

    仅当首段为纯字母（2-8 位）且后面还有段时才剥离，避免把 "CDJ501" 整个吃掉。
    """
    parts = normalized.split(" ", 1)
    if len(parts) == 2 and 2 <= len(parts[0]) <= 8 and parts[0].isalpha():
        return parts[1].strip()
    return normalized


def segments(normalized_code: str) -> list[str]:
    """字母数字段列表（任意非字母数字字符都视为分隔符）。"""
    return [s for s in _SEGMENTS_RE.split(normalized_code) if s]


def candidates_for(normalized_code: str) -> list[str]:
    """确定性候选形态（去重，同 bridge candidatesFor）：

    原样 → 最后一个 . / - 换 /（保留色号内连字符）→ 分段重组
    布号.布号/色号 → 全点式。含空格品牌词时同时为剥离品牌词后的形态
    生成候选。
    """
    variants = [normalized_code]
    stripped = strip_brand_prefix(normalized_code)
    if stripped != normalized_code:
        variants.append(stripped)

    candidates: list[str] = []
    for variant in variants:
        if not variant:
            continue
        candidates.append(variant)
        if "/" not in variant:
            for separator in (".", "-"):
                position = variant.rfind(separator)
                if position != -1:
                    candidates.append(
                        variant[:position] + "/" + variant[position + 1 :]
                    )
        segs = segments(variant)
        if len(segs) >= 2:
            candidates.append(".".join(segs[:-1]) + "/" + segs[-1])
            candidates.append(".".join(segs))
    return list(dict.fromkeys(c for c in candidates if c))


def normalized_key(code: str) -> str:
    """归一键：去除全部非字母数字字符并大写。"""
    return _ALNUM_KEY_RE.sub("", str(code or "")).upper()


def article_of(normalized_code: str) -> str | None:
    """布号（absent_colorway 判断）：≥3 段取前两段，2 段取全部，1 段取原段。"""
    stripped = strip_brand_prefix(normalized_code)
    segs = segments(stripped)
    if not segs:
        return None
    return ".".join(segs[: min(2, len(segs))])


def classify_code_input(raw: str) -> str:
    """判断输入是六位产品编号形态还是工厂号形态。

    返回 "ar_code" / "factory_code" / "unknown"：
      - ar_code：仅数字/空格/横线（可带 AR 前缀）且数字 ≥ 6 位。包括
        OCR 变体（"2277 07 1600" code+价签、"227707-1600"）——这些必须
        保持原有"取前 6 位"行为，不额外打 Odoo；
      - factory_code：含 "." 或 "/"、含字母（6268M、CDJ501、2134T、3003S），
        或纯数字恰好 5 位（DRA/Standeven 4-5 位工厂号；4 位纯数字留给
        样本册规则，不在本层处理）。这些输入绝不允许再做数字截断 ——
        "499.893/2" 截成 "499893" 会命中无关产品；
      - unknown：其余（4 位纯数字、无数字等）—— 走原有的格式错误提示。
    """
    normalized = strip_ar_prefix(normalize_code(raw))
    if not normalized:
        return "unknown"

    digits = re.sub(r"\D", "", normalized)
    if _PLAIN_DIGIT_INPUT_RE.fullmatch(normalized):
        if len(digits) >= 6:
            return "ar_code"
        if len(digits) == 5:
            return "factory_code"
        return "unknown"

    # 非纯数字形态：含分隔符（. / -）或字母。横线-only 且数字 ≥ 6 位
    # 已被上面分支接走（227707-1600 code+价签），落到这里的都是工厂号形态
    # （含 . / 或字母），必须走工厂号匹配
    if digits:
        return "factory_code"
    # 纯字母（无数字）不是编号
    return "unknown"


# ==================== Odoo 匹配与解析 ====================


@dataclass
class FactoryProduct:
    """工厂号解析出的可售产品（product.product 变体）。"""

    code: str  # 六位产品编号（default_code）
    name: str
    product_id: int
    template_id: int
    matched_codes: list[str] = field(default_factory=list)


@dataclass
class FactoryCodeMatch:
    """单条工厂号输入的匹配结果。"""

    status: str  # exact / normalized / ambiguous / absent_colorway / absent
    input: str  # 规范化后的原始输入
    hit_codes: list[str] = field(default_factory=list)  # 命中的库内编号
    products: list[FactoryProduct] = field(default_factory=list)


def _read_templates(client, template_ids: list[int]) -> list[dict]:
    if not template_ids:
        return []
    rows = client._call(
        model="product.template",
        method="read",
        args=[template_ids],
        kwargs={
            "fields": ["id", "name", "is_master", "master_product_id", "create_date"]
        },
    )
    return rows if isinstance(rows, list) else []


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else None
    return int(value) if value else None


def _family_display_template(members: list[dict]) -> dict | None:
    """每个 master 家族选一个展示产品（同 bridge isLaterProduct）：
    非 master（version）优先 → create_date 较新 → id 兜底。"""
    best = None
    best_key = None
    for member in members:
        create_date = member.get("create_date")
        if isinstance(create_date, str):
            try:
                create_date = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
            except ValueError:
                create_date = None
        ts = create_date.timestamp() if create_date else 0
        key = (0 if member.get("is_master") else 1, ts, int(member.get("id") or 0))
        if best_key is None or key > best_key:
            best, best_key = member, key
    return best


def _match_l1(client, candidates: list[str]) -> list[str]:
    """L1 精确命中：候选（含大小写变体）in 查询两个代码集，返回命中的库内编号。"""
    variants = []
    for candidate in candidates:
        for form in (candidate, candidate.upper(), candidate.lower()):
            if form not in variants:
                variants.append(form)

    hits = []
    supplier_rows = client._call(
        model="product.supplierinfo",
        method="search_read",
        args=[[("product_code", "in", variants)]],
        kwargs={"fields": ["product_code"], "limit": MAX_L2_ROWS},
    )
    if isinstance(supplier_rows, list):
        hits.extend(
            row.get("product_code") for row in supplier_rows if row.get("product_code")
        )

    template_rows = client._call(
        model="product.template",
        method="search_read",
        args=[[("vendor_item_no", "in", variants)]],
        kwargs={"fields": ["vendor_item_no"], "limit": 20},
    )
    if isinstance(template_rows, list):
        hits.extend(
            row.get("vendor_item_no")
            for row in template_rows
            if row.get("vendor_item_no")
        )

    # 只保留与候选精确相等（忽略大小写）的编号
    wanted = {c.upper() for c in candidates}
    return list(dict.fromkeys(h for h in hits if str(h).upper() in wanted))


def _match_l2(client, candidates: list[str]) -> list[str]:
    """L2 归一命中：ilike 分段通配模式取超集，客户端按归一键精确过滤。"""
    keys = {normalized_key(c) for c in candidates if normalized_key(c)}
    hits = []
    for candidate in candidates:
        segs = segments(candidate)
        if not segs:
            continue
        pattern = "%" + "%".join(segs) + "%"

        supplier_rows = client._call(
            model="product.supplierinfo",
            method="search_read",
            args=[[("product_code", "ilike", pattern)]],
            kwargs={"fields": ["product_code"], "limit": MAX_L2_ROWS},
        )
        if isinstance(supplier_rows, list):
            hits.extend(
                row.get("product_code")
                for row in supplier_rows
                if row.get("product_code")
            )

        template_rows = client._call(
            model="product.template",
            method="search_read",
            args=[[("vendor_item_no", "ilike", pattern)]],
            kwargs={"fields": ["vendor_item_no"], "limit": 20},
        )
        if isinstance(template_rows, list):
            hits.extend(
                row.get("vendor_item_no")
                for row in template_rows
                if row.get("vendor_item_no")
            )

    return list(dict.fromkeys(h for h in hits if normalized_key(str(h)) in keys))


def _article_in_library(client, article: str | None) -> bool:
    """布号（前两段）是否在库（template vin ∪ 价格表代码）。"""
    if not article:
        return False
    for pattern in (article + "%", article + "/%"):
        supplier_rows = client._call(
            model="product.supplierinfo",
            method="search_read",
            args=[[("product_code", "ilike", pattern)]],
            kwargs={"fields": ["product_code"], "limit": 1},
        )
        if supplier_rows:
            return True
        template_rows = client._call(
            model="product.template",
            method="search_read",
            args=[[("vendor_item_no", "ilike", pattern)]],
            kwargs={"fields": ["vendor_item_no"], "limit": 1},
        )
        if template_rows:
            return True
    return False


def _resolve_hit_codes_to_products(
    client, hit_codes: list[str]
) -> list[FactoryProduct]:
    """命中的库内编号 → 可售产品（经 master 家族 → 最新 version → 变体）。"""
    wanted = {c.upper() for c in hit_codes}

    # 编号 → template（supplierinfo 行经 product_tmpl_id；template 直接命中）
    supplier_rows = client._call(
        model="product.supplierinfo",
        method="search_read",
        args=[[("product_code", "in", hit_codes)]],
        kwargs={"fields": ["product_code", "product_tmpl_id"], "limit": MAX_L2_ROWS},
    )
    template_ids = set()
    if isinstance(supplier_rows, list):
        for row in supplier_rows:
            tmpl_id = _m2o_id(row.get("product_tmpl_id"))
            if tmpl_id:
                template_ids.add(tmpl_id)

    # 大小写变体再查一次 template vin 命中（hit_codes 大小写可能不一致）
    template_rows = client._call(
        model="product.template",
        method="search_read",
        args=[
            [
                "|",
                ("vendor_item_no", "in", hit_codes),
                ("vendor_item_no", "in", [c.upper() for c in hit_codes]),
            ]
        ],
        kwargs={"fields": ["id", "vendor_item_no"], "limit": 20},
    )
    if isinstance(template_rows, list):
        for row in template_rows:
            if str(row.get("vendor_item_no") or "").upper() in wanted:
                template_ids.add(int(row["id"]))

    templates = _read_templates(client, sorted(template_ids))
    if not templates:
        return []

    # 家族归并：version → 其 master
    master_ids = set()
    for template in templates:
        if template.get("is_master"):
            master_ids.add(int(template["id"]))
        else:
            master_id = _m2o_id(template.get("master_product_id"))
            if master_id:
                master_ids.add(master_id)
            else:
                master_ids.add(int(template["id"]))  # 无 master 的孤儿 version

    # 家族成员（master 本身 + master_no 指向它的 versions）
    members = client._call(
        model="product.template",
        method="search_read",
        args=[
            [
                "|",
                ("id", "in", sorted(master_ids)),
                ("master_product_id", "in", sorted(master_ids)),
            ]
        ],
        kwargs={
            "fields": ["id", "name", "is_master", "master_product_id", "create_date"]
        },
    )
    if not isinstance(members, list) or not members:
        members = templates

    family_members: dict[int, list[dict]] = {}
    for member in members:
        if member.get("is_master"):
            family_id = int(member["id"])
        else:
            family_id = _m2o_id(member.get("master_product_id")) or int(member["id"])
        if family_id in master_ids:
            family_members.setdefault(family_id, []).append(member)

    display_templates = []
    for family_id in sorted(family_members):
        display = _family_display_template(family_members[family_id])
        if display:
            display_templates.append(display)
    if not display_templates:
        return []

    variants = client._call(
        model="product.product",
        method="search_read",
        args=[[("product_tmpl_id", "in", [t["id"] for t in display_templates])]],
        kwargs={
            "fields": ["id", "default_code", "name", "product_tmpl_id"],
            "limit": 30,
        },
    )
    if not isinstance(variants, list):
        return []

    template_names = {int(t["id"]): t.get("name", "") for t in display_templates}

    # 去重按六位编号合并（同编号多家族/多变体只保留一个产品）
    products: dict[str, FactoryProduct] = {}
    for variant in variants:
        code = str(variant.get("default_code") or "").strip()
        if not code:
            continue
        template_id = _m2o_id(variant.get("product_tmpl_id"))
        existing = products.get(code)
        if existing:
            continue
        products[code] = FactoryProduct(
            code=code,
            name=str(variant.get("name") or template_names.get(template_id, "")),
            product_id=int(variant["id"]),
            template_id=template_id or 0,
            matched_codes=list(hit_codes),
        )
    return list(products.values())[:MAX_PRODUCTS]


def match_factory_code(client, raw_code: str) -> FactoryCodeMatch:
    """工厂号 → Odoo 产品。分层 L1 exact → L2 normalized → L3 布号在库。

    匹配失败不抛异常：任何 Odoo 错误都记日志并按 absent 返回，
    让调用方回落到"请客户提供六位产品编号"的话术，绝不能退回数字截断。
    """
    normalized = normalize_code(raw_code)
    match = FactoryCodeMatch(status=STATUS_ABSENT, input=normalized)
    if not segments(normalized):
        return match

    try:
        candidates = candidates_for(normalized)
        hit_codes = _match_l1(client, candidates)
        status = STATUS_EXACT
        if not hit_codes:
            hit_codes = _match_l2(client, candidates)
            status = STATUS_NORMALIZED

        if hit_codes:
            match.hit_codes = hit_codes
            match.products = _resolve_hit_codes_to_products(client, hit_codes)
            if len(match.products) == 1:
                match.status = status
            elif len(match.products) > 1:
                match.status = STATUS_AMBIGUOUS
            else:
                # 编号在库但解析不出可售变体 —— 按 ambiguous 处理交人工，
                # 比"未收录"更接近事实
                match.status = STATUS_AMBIGUOUS
            return match

        if _article_in_library(client, article_of(normalized)):
            match.status = STATUS_ABSENT_COLORWAY
        return match
    except Exception as exc:  # noqa: BLE001 — 匹配失败必须可回落，不能炸工具层
        logger.error(f"[FactoryCodeMatch] match error for {raw_code!r}: {exc}")
        match.status = STATUS_ABSENT
        match.products = []
        return match
