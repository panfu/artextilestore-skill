#!/usr/bin/env python3
"""Odoo Order Management Script

Design Philosophy: Discrete operations, flexible combination

Workflow Example:
    1. Search customer: --search 華遠
    2. Create order: --create 10640
    3. Add product: --add 119010 227707:3m
    4. Update price: --update-line 119010 227707 price=310 disc=22
    5. Verify order: --verify 119010
    6. Confirm order: --confirm 119010

Each command is independent and can be combined as needed

Context Support:
    - Auto-track customer searches
    - Auto-track order creation
    - Support for pronouns like "this product"
"""

import os
import sys
import argparse
import json
import logging
import urllib3
from typing import Optional
from pathlib import Path

# 设置日志
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# 加载 .env 文件（从项目根目录）
try:
    from dotenv import load_dotenv

    # 查找项目根目录的 .env 文件
    project_root = Path(__file__).parent.parent.parent
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass  # dotenv 未安装，使用环境变量

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add skill lib directory to path
# Add scripts directory (parent of lib) to path so we can import from lib
skill_lib_path = os.path.dirname(os.path.abspath(__file__))
if skill_lib_path not in sys.path:
    sys.path.insert(0, skill_lib_path)

# Import context manager (default to fallback mode)
# To enable Agno memory manager, set environment variable: AGNO_ENABLE=1
USE_AGNO = False

# Try to import memory manager (optional, for standalone CLI use)
try:
    if os.environ.get("AGNO_ENABLE", "0") == "1":
        from lib.agno_memory import AgnoMemoryManager

        USE_AGNO = True
    else:
        from lib.conversation_context import ConversationContext

        USE_AGNO = False
except ImportError:
    # Create a simple fallback class
    class SimpleMemory:
        def add_customer(self, **kwargs):
            pass

        def add_order(self, **kwargs):
            pass

        def add_product(self, **kwargs):
            pass

        def get_context_summary(self):
            return ""

        def clear(self):
            pass

    ConversationContext = SimpleMemory
    AgnoMemoryManager = SimpleMemory

from ar_chatbot.scripts.lib import OdooClient

# Store last operation ID
LAST_STATE_FILE = ".odoo_last_state.json"

# UOM Cache
_UOM_CACHE = {}


def get_uom_id(client: OdooClient, uom_name: str) -> Optional[int]:
    """Get UOM ID by name, with caching

    Args:
        client: OdooClient instance
        uom_name: UOM name (e.g., "M", "Y", "米", "码")

    Returns:
        UOM ID or None
    """
    if uom_name in _UOM_CACHE:
        return _UOM_CACHE[uom_name]

    uom = client.get_uom_by_name(uom_name)
    if uom:
        _UOM_CACHE[uom_name] = uom["id"]
        return uom["id"]

    # Try alternative names
    alternatives = {
        "m": ["M", "Meter", "m", "米"],
        "y": ["Y", "Yard", "yd", "码", "Yards"],
        "meter": ["M", "Meter", "m", "米"],
        "yard": ["Y", "Yard", "yd", "码"],
    }

    for alt in alternatives.get(uom_name.lower(), []):
        uom = client.get_uom_by_name(alt)
        if uom:
            _UOM_CACHE[uom_name] = uom["id"]
            return uom["id"]

    return None


def load_state():
    """Load previously saved state"""
    if os.path.exists(LAST_STATE_FILE):
        with open(LAST_STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(key, value):
    """Save state"""
    state = load_state()
    state[key] = value
    with open(LAST_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def search_customer(
    client: OdooClient, memory, keyword: str, limit: int = 10, interactive: bool = False
):
    """Search for customers

    Args:
        memory: Memory manager instance
        interactive: If True and multiple results, wait for user input
    """
    customers = client.search_customer(keyword, limit=limit)
    if customers:
        print(f"\nFound {len(customers)} customer(s):")
        print("-" * 60)
        for i, c in enumerate(customers, 1):
            print(f"  [{i}] ID: {c.get('id')} | {c.get('name')}")
            # Customer remarks are NOT displayed to user, but stored in metadata for calculations
            remarks = c.get("x_studio_customer_remarks")

            # Get default shipping address
            addresses = client.get_customer_addresses(c.get('id'))
            default_shipping = None
            for addr in addresses:
                if addr.get('type') == 'delivery':
                    default_shipping = {
                        'id': addr.get('id'),
                        'name': addr.get('name'),
                        'street': addr.get('street'),
                        'street2': addr.get('street2'),
                        'city': addr.get('city'),
                    }
                    # Print default shipping address for parsing
                    print(f"      Default Shipping: {addr.get('name')}")
                    print(f"          ID: {addr.get('id')}")
                    if addr.get('street'):
                        print(f"          Street: {addr.get('street')}")
                    if addr.get('city'):
                        print(f"          City: {addr.get('city')}")
                    break  # Use first delivery address as default

            # Update context
            memory.add_customer(
                name=c.get("name"),
                customer_id=c.get("id"),
                metadata={
                    "email": c.get("email"),
                    "phone": c.get("phone"),
                    "remarks": remarks,
                    "default_shipping": default_shipping,
                },
            )
        print("-" * 60)

        # Interactive selection
        if interactive and len(customers) > 1:
            try:
                choice = input(
                    f"\nSelect customer [1-{len(customers)}] (or press Enter to skip): "
                ).strip()
                if choice:
                    idx = int(choice) - 1
                    if 0 <= idx < len(customers):
                        selected = customers[idx]
                        print(
                            f"\nSelected: {selected.get('name')} (ID: {selected.get('id')})"
                        )
                        return selected
            except (ValueError, KeyboardInterrupt, EOFError):
                pass

        return customers
    else:
        print(f"\nNo customers found containing '{keyword}'")
        return None


def get_or_select_customer(
    client: OdooClient, memory, partner_id: int = None, keyword: str = None
):
    """Get or let user select a customer

    Args:
        client: OdooClient instance
        memory: Memory manager instance
        partner_id: Explicit customer ID (optional)
        keyword: Search keyword (used when partner_id is empty)

    Returns:
        Selected customer dict, or None if cancelled
    """
    if partner_id:
        # Verify customer exists
        customer = client._call(
            model="res.partner",
            method="read",
            args=[[partner_id]],
            kwargs={"fields": ["name"]},
        )
        if customer:
            return {"id": partner_id, "name": customer[0].get("name", "N/A")}
        else:
            print(f"\nCustomer not found with ID: {partner_id}")
            return None

    if keyword:
        # Search and let user select
        selected = search_customer(client, memory, keyword, interactive=True)
        if isinstance(selected, list) and len(selected) == 1:
            return selected[0]
        elif isinstance(selected, dict):
            return selected
        return None

    print("\nError: No customer ID or search keyword provided")
    return None


def resolve_partner_id(client: OdooClient, memory, partner_input) -> int:
    """Resolve customer input (ID, keyword or empty) to customer ID

    Args:
        client: OdooClient instance
        memory: Memory manager instance
        partner_input: Integer ID, string keyword, or None

    Returns:
        Customer ID, or None on failure
    """
    if not partner_input:
        print("\n[!] No customer specified, using search mode...")
        customer = get_or_select_customer(client, memory, keyword="customer")
        if not customer:
            print("Cancelled")
            return None
        return customer["id"]

    # Try to parse as integer ID, if fails use as keyword
    try:
        partner_id = int(partner_input)
        # Verify customer exists
        customer_data = get_or_select_customer(client, memory, partner_id=partner_id)
        if not customer_data:
            return None
        return partner_id
    except ValueError:
        # Use as keyword search
        customer = get_or_select_customer(client, memory, keyword=str(partner_input))
        if not customer:
            print("Cancelled")
            return None
        return customer["id"]


def resolve_order_id(client: OdooClient, order_ref) -> int:
    """Resolve order reference (name like '26030081' or 'SO26030081') to order ID

    IMPORTANT: Always try to find order by name FIRST, because the order name
    like "26030081" is stored as the name field (e.g., "SO26030081"), not ID.

    which means the user may pass the order reference string, and actual numeric ID.

    Args:
        client: OdooClient instance
        order_ref: Order reference (e.g., "26030081", "SO26030081") or numeric ID

    Returns:
        Order ID if found, None otherwise
    """
    logger.debug(f"Resolving order_ref: {order_ref}")

    if not order_ref:
        return None

    # Convert to string for comparison
    order_ref_str = str(order_ref).strip()

    # Step 1: Always try to find order by name first
    # This handles cases where user passes "26030081" which is the order name, not ID
    logger.debug(f"Searching for order by name: {order_ref_str}")
    orders = client._call(
        model="sale.order",
        method="search_read",
        args=[[("name", "=", order_ref_str)]],
        kwargs={"fields": ["id", "name"]},
    )
    if orders:
        order_id = orders[0]["id"]
        logger.info(f"Found order by name: {order_ref_str} -> ID: {order_id}")
        return order_id

    logger.debug(f"Order not found by name: {order_ref_str}, trying as numeric ID")

    # Step 2: Try as numeric ID if input looks like a pure number
    try:
        potential_id = int(order_ref_str)
        logger.debug(f"Trying as numeric ID: {potential_id}")
        # Verify this ID exists
        order = client.get_order(potential_id)
        if order:
            logger.info(f"Found order by ID: {potential_id}")
            return potential_id
    except (ValueError, TypeError):
        pass  # Not a number
    except Exception:
        pass  # Order not found by name or ID

    logger.warning(f"Order not found: {order_ref_str}")
    return None


def create_order(
    client: OdooClient,
    memory,
    partner_id: int,
    note: str = "",
    shipping_id: int = None,
    invoice_id: int = None,
):
    """Create an order"""
    order_id = client.create_order(
        partner_id,
        note=note,
        partner_shipping_id=shipping_id,
        partner_invoice_id=invoice_id,
    )

    # Get order name to display (use name field, not id)
    order = client.get_order(order_id)
    order_name = order.get("name", f"SO{order_id}") if order else f"SO{order_id}"

    save_state("last_order_id", order_id)
    print(f"\nOrder created successfully!")
    print(f"  Order Reference: {order_name}")
    print(f"  Customer ID: {partner_id}")

    # Generate portal URL for customer
    portal_url = client.get_order_portal_url(order_id)
    if portal_url:
        print(f"  Portal URL: {portal_url}")

    # Update context - use order name as the primary identifier
    memory.add_order(
        order_id=order_id, partner_id=partner_id, order_name=order_name
    )

    return order_id


def parse_qty(qty_str: str) -> float:
    """Parse quantity string to extract numeric value only.

    IMPORTANT: This function does NOT do any unit conversion.
    It simply extracts the numeric value from the string.
    Unit conversion is handled later based on the detected unit.

    Supported formats:
        3, 3.0           -> returns 3.0
        3m, 3米          -> returns 3.0
        3y, 3码, 3yd     -> returns 3.0
        1/4, 3/8         -> returns 0.25, 0.375 (純分數)
        1碼3角半          -> returns 1.35 (布料口語，角=0.1/角半=0.05/骨=0.25/半=0.5)
        碼半, 半碼        -> returns 1.5, 0.5
    """
    import re

    if not qty_str:
        raise ValueError(f"Invalid quantity: empty string")

    s = qty_str.strip()

    # 1) 純分數（"1/4", "3/8"）
    m_frac = re.fullmatch(r"(\d+)/(\d+)", s)
    if m_frac:
        denom = int(m_frac.group(2))
        if denom == 0:
            raise ValueError(f"Invalid quantity: '{qty_str}' - division by zero")
        return int(m_frac.group(1)) / denom

    # 2) 布料口語化表達（角/骨/半/碼）— 內建為碼數
    if any(marker in s for marker in ("角", "骨", "半", "碼")):
        try:
            from ar_chatbot.tools.fabric_length import parse_fabric_length

            return parse_fabric_length(s)
        except Exception as e:
            raise ValueError(f"Invalid fabric quantity: '{qty_str}' - {e}")

    # 3) 原始數字前綴解析
    qty_str_lower = s.lower()

    # Extract numeric part - find longest prefix that can be converted to float
    numeric_part = ""
    for i, char in enumerate(qty_str_lower):
        if char.isdigit() or char in ".-":
            numeric_part += char
        else:
            break

    if not numeric_part:
        raise ValueError(f"Invalid quantity: '{qty_str}' - no numeric part found")

    try:
        return float(numeric_part)
    except ValueError:
        raise ValueError(
            f"Invalid quantity: '{qty_str}' - cannot convert '{numeric_part}' to float"
        )


def detect_unit(qty_str: str) -> str:
    """Detect unit from quantity string.

    Returns:
        'm' for meters (米, m)
        'y' for yards (码, 碼, y, yd) — 包含布料口語（角/骨/半/碼）
        'y' for raw numbers (default to yards)
    """
    if not qty_str:
        return "y"

    s = qty_str.strip()

    # 布料口語化表達（角/骨/碼，及「半碼」「碼半」組合）內建為碼數
    if any(marker in s for marker in ("角", "骨", "碼")):
        return "y"
    if "半" in s and "碼" in s:
        return "y"

    qty_str_lower = s.lower()

    if "m" in qty_str_lower or "米" in qty_str_lower:
        return "m"
    elif "y" in qty_str_lower or "码" in qty_str_lower or "yd" in qty_str_lower:
        return "y"
    else:
        return "y"  # default to yards


def convert_to_yards(qty: float, unit: str) -> float:
    """Convert quantity to yards.

    Args:
        qty: Quantity value
        unit: 'm' for meters, 'y' for yards

    Returns:
        Quantity in yards
    """
    if unit == "m":
        return qty / 0.9144  # Convert meters to yards
    else:
        return qty  # Already in yards


def parse_product_spec(spec: str) -> dict:
    """Parse product specification

    Supported formats:
        227707:3m           (product_code:qty in meters)
        227707:3y           (product_code:qty in yards)
        227707:3m:310       (product_code:qty:price override)
        227707:3m:310:22    (product_code:qty:price:discount%)

    Product code extraction:
        - If product code contains more than 6 digits, only the first 6 are used
        - Example: "227707 1600:3m" -> product_code="227707"
        - This handles cases where customers provide "code + price tag" format

    IMPORTANT: Price field selection based on ORDER UNIT
        - When ordering in METERS (m): use list_price (price per meter)
        - When ordering in YARDS (y): use secondary_uom_list_price (price per yard)

    Example for 华远:
        - 227707:2m  -> uses list_price (339.02 HK$/m) -> qty=2m, price=339.02
        - 227707:2y  -> uses secondary_uom_list_price (310 HK$/yd) -> qty=2yd, price=310
    """
    import re

    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid product spec: {spec}")

    product_code_raw = parts[0]
    qty_str = parts[1]

    # Extract first 6 digits from product code
    # This handles inputs like "227707 1600" or "227707-1600"
    digits = re.sub(r'[^0-9]', '', product_code_raw)
    if len(digits) >= 6:
        product_code = digits[:6]
    else:
        product_code = product_code_raw  # Keep original if not enough digits

    # Detect unit from quantity string
    unit = detect_unit(qty_str)
    qty = parse_qty(qty_str)  # Just the numeric value, no conversion

    price = None
    discount = 0.0

    if len(parts) >= 3:
        price = float(parts[2])
    if len(parts) >= 4:
        discount = float(parts[3])

    return {
        "code": product_code,
        "raw_code": product_code_raw,  # Verbatim input (factory-number detection)
        "qty": qty,  # Original numeric value (2 for "2m", 2 for "2y")
        "qty_str": qty_str,  # Preserve original quantity string with unit
        "unit": unit,  # 'm' for meters, 'y' for yards
        "price": price,
        "discount": discount,
    }


def check_order_editable(
    client: OdooClient, order_id: int
) -> tuple[bool, str, Optional[dict]]:
    """Check if order is editable (draft state only).

    Business Rule:
        - draft (Quotation): User can modify order
        - sent (Quotation Sent): User confirmed quotation, cannot modify
        - sale (Sale Order): Confirmed order, cannot modify
        - cancel (Cancelled): Cancelled order, cannot modify

    Args:
        client: OdooClient instance
        order_id: Order ID

    Returns:
        tuple: (is_editable, error_message, order_info)
        - is_editable: True if order can be modified
        - error_message: Error message if not editable, None otherwise
        - order_info: Order dict if found, None otherwise
    """
    order = client.get_order(order_id)
    if not order:
        return False, f"Order {order_id} not found", None

    state = order.get("state", "")
    order_name = order.get("name", f"#{order_id}")

    if state != "draft":
        if state == "sent":
            error_msg = (
                f"Order {order_name} is in 'Quotation Sent' state. "
                f"Modifications are not allowed after confirmation."
            )
        elif state == "sale":
            error_msg = (
                f"Order {order_name} is already confirmed (Sale Order). "
                f"Cannot modify."
            )
        elif state == "cancel":
            error_msg = f"Order {order_name} has been cancelled."
        else:
            error_msg = (
                f"Order {order_name} is in '{state}' state. "
                f"Only draft orders can be modified."
            )
        return False, error_msg, order

    return True, None, order


def add_order_line(
    client: OdooClient,
    memory,
    order_id: int,
    product_spec: str,
    price: float = None,
    discount: float = None,
    skip_confirmation: bool = False,
    customer_po: str = None,
):
    """Add order line

    PRICE CALCULATION based on order unit:
        - When ordering in METERS (m): uses list_price (e.g., 339.02 HK$/m)
        - When ordering in YARDS (y): uses secondary_uom_list_price (e.g., 310 HK$/yd)

    Example for 華遠 with product 227707:
        - 227707:2m  -> 2m * 339.02 HK$/m = 678.04 HK$
        - 227707:2y  -> 2yd * 310 HK$/yd = 620 HK$
    """
    # Check if order is editable (draft state only)
    is_editable, error_msg, order = check_order_editable(client, order_id)
    if not is_editable:
        print(f"\n[ERROR] {error_msg}")
        return None

    spec = parse_product_spec(product_spec)

    # Query product
    product = client.search_product_by_code(spec["code"])
    if not product:
        print(f"\nProduct not found: {spec['code']}")
        return None

    product_id = product["id"]
    unit = spec.get("unit", "y")  # 'm' for meters, 'y' for yards
    qty = spec["qty"]  # Original numeric value (no conversion)
    product_name = product.get("name")
    product_code = spec["code"]
    line_price = price if price is not None else spec.get("price")
    line_discount = discount if discount is not None else spec.get("discount", 0)

    # ========== Price and UOM Selection ==========
    uom_id = None  # Use default UOM (will be set if ordering in meters)
    qty_for_odoo = qty  # Quantity to send to Odoo

    if unit == "m":
        # Customer ordered in METERS
        # Use list_price (price per METER)
        line_price = product.get("list_price", 0) if line_price is None else line_price
        price_unit_label = "/m"
        # Get meter UOM ID so Odoo knows this is in meters
        uom_id = get_uom_id(client, "M")
        # Quantity stays in meters
        qty_for_odoo = qty
        display_qty_m = qty
        display_qty_yd = qty / 0.9144
    else:
        # Customer ordered in YARDS
        # Use secondary_uom_list_price (price per YARD)
        line_price = (
            product.get("secondary_uom_list_price", product.get("list_price", 0))
            if line_price is None
            else line_price
        )
        price_unit_label = "/yd"
        # Use Yard UOM explicitly to override product default
        uom_id = get_uom_id(client, "Y")
        # Quantity stays in yards
        qty_for_odoo = qty
        display_qty_yd = qty
        display_qty_m = qty * 0.9144
    # ========== End Price and UOM Selection ==========

    # ========== Stock Status Check ==========
    # Calculate stock status using the static method
    stock_info = OdooClient.calculate_stock_status(product)

    # Check if ordering is allowed
    if not stock_info["can_order"]:
        print(f"\n[ERROR] {stock_info['guidance_message']}")
        return None

    # Display stock status and guidance
    if stock_info["guidance_message"]:
        print(f"\n[INFO] {stock_info['guidance_message']}")

    # If confirmation is required, wait for user input
    if stock_info["requires_confirmation"]:
        print(f"\n{stock_info['confirmation_message']}")
        if skip_confirmation:
            # Auto-confirm for non-interactive mode (AI calls)
            print("[AUTO-CONFIRMED] Proceeding with order.")
        else:
            try:
                confirm = (
                    input("Enter 'yes' to continue, or any other key to cancel: ")
                    .strip()
                    .lower()
                )
                if confirm not in ("yes", "y"):
                    print("[CANCELLED] Operation cancelled.")
                    return None
            except (KeyboardInterrupt, EOFError):
                print("\n[CANCELLED] Operation cancelled.")
                return None

    # ========== End Stock Status Check ==========

    # ========== Stock Quantity Check ==========
    qty_available_yd = product.get("qty_available", 0)  # Stock in yards
    virtual_available_yd = product.get("virtual_available", 0)  # Virtual stock in yards
    allow_out_of_stock = product.get("allow_out_of_stock_order", False)

    # If allow_out_of_stock_order is enabled, skip quantity check
    if allow_out_of_stock:
        print(f"\n[OK] Product allows out-of-stock orders, quantity check bypassed")
        print(f"  Product: [{product_code}] {product_name}")
        print(f"  Required: {display_qty_m:.2f}m ({display_qty_yd:.4f}yd)")
        print(f"  Stock: {qty_available_yd * 0.9144:.2f}m ({qty_available_yd:.4f}yd)")
    else:
        # Convert required quantity to yards for stock check
        if unit == "m":
            required_yd = qty / 0.9144
        else:
            required_yd = qty

        # Check if stock is sufficient
        if required_yd > qty_available_yd:
            print(f"\n[!] Insufficient stock!")
            print(f"  Product: [{product_code}] {product_name}")
            print(f"  Required: {display_qty_m:.2f}m ({display_qty_yd:.4f}yd)")
            print(
                f"  Stock: {qty_available_yd * 0.9144:.2f}m ({qty_available_yd:.4f}yd)"
            )
            print(
                f"  Virtual: {virtual_available_yd * 0.9144:.2f}m ({virtual_available_yd:.4f}yd)"
            )
            print(f"\n[x] Cannot add order line. Contact warehouse or adjust quantity.")
            return None
        else:
            # Stock is sufficient, display stock info
            print(f"\n[OK] Stock quantity check passed")
            print(f"  Product: [{product_code}] {product_name}")
            print(f"  Required: {display_qty_m:.2f}m ({display_qty_yd:.4f}yd)")
            print(
                f"  Stock: {qty_available_yd * 0.9144:.2f}m ({qty_available_yd:.4f}yd)"
            )
    # ========== End Stock Quantity Check ==========

    # ========== Narrow Fabric Check ==========
    narrow_info = OdooClient.check_narrow_fabric_status(product)

    if narrow_info["is_narrow"]:
        print(f"\n[WARNING] {narrow_info['warning_message']}")
        if skip_confirmation:
            # Auto-confirm for non-interactive mode (AI calls)
            print("[AUTO-CONFIRMED] Proceeding with narrow fabric order.")
        else:
            try:
                confirm = (
                    input("Enter 'yes' to continue, or any other key to cancel: ")
                    .strip()
                    .lower()
                )
                if confirm not in ("yes", "y"):
                    print("[CANCELLED] Operation cancelled.")
                    return None
            except (KeyboardInterrupt, EOFError):
                print("\n[CANCELLED] Operation cancelled.")
                return None
    # ========== End Narrow Fabric Check ==========

    # Add order line with correct UOM, price, and customer_po
    logger.info(f"Adding order line: order_id={order_id}, product_id={product_id}, qty={qty_for_odoo}, price={line_price}, uom_id={uom_id}, customer_po={customer_po}")
    try:
        line_id = client.add_order_line(
            order_id, product_id, qty_for_odoo, line_price, uom_id=uom_id,
            customer_po=customer_po
        )
        logger.info(f"Order line created successfully, line_id={line_id}")
    except Exception as e:
        logger.error(f"Failed to add order line: {e}", exc_info=True)
        print(f"\n[ERROR] Failed to add product to order: {e}")
        return None
    # If there's a discount, update
    if line_discount:
        client._call(
            model="sale.order.line",
            method="write",
            args=[[line_id], {"discount": line_discount}],
        )

    # Calculate subtotal
    subtotal = qty_for_odoo * line_price * (1 - line_discount / 100)

    print(f"\nOrder line added:")
    print(f"  Order ID: {order_id}")
    print(f"  Product: {spec['code']} - {product.get('name')}")
    print(f"  Quantity: {display_qty_m:.2f}m ({display_qty_yd:.4f}yd)")
    print(f"  Unit Price: HK$ {line_price:.2f}{price_unit_label}")
    # Always show discount, making it prominent when applied
    if line_discount > 0:
        original_subtotal = qty_for_odoo * line_price
        savings = original_subtotal - subtotal
        print(f"  💰 Discount: {line_discount}% (save HK$ {savings:.2f})")
    else:
        print(f"  Discount: 0% (no discount applied)")
    print(f"  Subtotal: HK$ {subtotal:.2f}")
    if customer_po:
        print(f"  Customer PO: {customer_po}")

    # Update context - add this product to recent products list
    memory.add_product(
        code=product_code,
        name=product_name,
        product_id=product_id,
        metadata={"qty": qty_for_odoo, "price": line_price, "unit": unit},
    )

    return line_id


# ========== Original functions below (unchanged) ==========


def update_order_line(
    client: OdooClient,
    order_id: int,
    product_code: str = None,
    price: float = None,
    discount: float = None,
):
    """Update order line"""
    order = client.get_order(order_id)
    order_lines = order.get("order_line", [])

    updated = False

    for line_id in order_lines:
        line = client._call(
            model="sale.order.line",
            method="read",
            args=[[line_id]],
            kwargs={
                "fields": ["product_id", "price_unit", "discount", "product_uom_qty"]
            },
        )
        if line:
            l = line[0]
            product = l.get("product_id", [None, ""])

            if product_code:
                if product and f"[{product_code}]" in str(product[1]):
                    target_line_id = line_id
                    target_product = product[1]
                    current_qty = l.get("product_uom_qty", 0)
                    break
            else:
                target_line_id = line_id
                target_product = product[1] if product else "N/A"
                current_qty = l.get("product_uom_qty", 0)
                break
    else:
        if product_code:
            print(f"\nProduct {product_code} not found in order {order_id}")
        else:
            print(f"\nOrder {order_id} has no order lines")
        return False

    update_data = {}
    if price is not None:
        update_data["price_unit"] = price
    if discount is not None:
        update_data["discount"] = discount

    if update_data:
        client._call(
            model="sale.order.line",
            method="write",
            args=[[target_line_id], update_data],
        )

        print(f"\nOrder line updated:")
        print(f"  Product: {target_product}")
        print(f"  Quantity: {current_qty:.4f}yd")
        if price is not None:
            print(f"  New Unit Price: HK$ {price:.2f}/yd")
        if discount is not None:
            print(f"  New Discount: {discount}%")

        new_price = update_data.get("price_unit", l.get("price_unit", 0))
        new_discount = update_data.get("discount", l.get("discount", 0))
        subtotal = current_qty * new_price * (1 - new_discount / 100)
        print(f"  New Subtotal: HK$ {subtotal:.2f}")

        updated = True

    return updated


def verify_order(client: OdooClient, order_id: int):
    """Verify order"""
    order = client._call(
        model="sale.order",
        method="read",
        args=[[order_id]],
        kwargs={
            "fields": ["order_line", "partner_id", "amount_total", "amount_untaxed"]
        },
    )

    if not order:
        print(f"\nOrder {order_id} not found")
        return False

    o = order[0]
    print("\n" + "=" * 70)
    print(f"Order Verification: {order_id}")
    print("=" * 70)
    print(f"\nCustomer: {o.get('partner_id')}")
    print(f"Total Amount: HK$ {o.get('amount_total', 0):.2f}")

    order_lines = o.get("order_line", [])
    print(f"\nOrder Lines ({len(order_lines)} line(s)):")
    print("-" * 70)

    calculated_total = 0

    for i, line_id in enumerate(order_lines, 1):
        line = client._call(
            model="sale.order.line",
            method="read",
            args=[[line_id]],
            kwargs={
                "fields": [
                    "product_id",
                    "product_uom_qty",
                    "price_unit",
                    "discount",
                    "price_subtotal",
                    "product_uom",
                ]
            },
        )
        if line:
            l = line[0]
            product = (
                l.get("product_id", [None, ""])[1] if l.get("product_id") else "N/A"
            )
            product_id = (
                l.get("product_id", [None, ""])[0] if l.get("product_id") else None
            )

            # Fetch product template details by default_code to check narrow fabric status
            is_narrow = False
            default_code = l.get("product_default_code")
            if default_code:
                # Search product.template by default_code
                template_search = client._call(
                    model="product.template",
                    method="search_read",
                    args=[[["default_code", "=", default_code]]],
                    kwargs={"fields": ["width_cm"], "limit": 1},
                )
                if template_search:
                    width_cm = template_search[0].get("width_cm")
                    is_narrow = (
                        width_cm is not None
                        and width_cm > 0
                        and width_cm < OdooClient.NARROW_FABRIC_THRESHOLD_CM
                    )

            # Add narrow fabric prefix
            product_display = f"【窄边】{product}" if is_narrow else product

            qty = l.get("product_uom_qty", 0)
            price = l.get("price_unit", 0)
            discount = l.get("discount", 0)
            subtotal = l.get("price_subtotal", 0)
            product_uom = l.get("product_uom", [None, ""])

            # Determine UOM for display
            uom_name = product_uom[1] if product_uom and len(product_uom) > 1 else ""
            uom_name_lower = uom_name.lower() if uom_name else ""

            # Check if UOM is Meter (米, meter, m) or Yard (码, yard, yd)
            is_meter = any(
                keyword in uom_name_lower for keyword in ["meter", "米", "m"]
            )
            is_yard = any(keyword in uom_name_lower for keyword in ["yard", "码", "yd"]) or uom_name_lower == "y"

            # Display quantity based on actual UOM
            if is_meter:
                # qty is in meters
                qty_m = qty
                qty_yd = qty / 0.9144
                qty_display = f"{qty_m:.2f}m ({qty_yd:.4f}yd)"
            elif is_yard:
                # qty is in yards
                qty_yd = qty
                qty_m = qty * 0.9144
                qty_display = f"{qty_yd:.4f}yd ({qty_m:.2f}m)"
            else:
                # Unknown UOM, show raw value
                qty_display = f"{qty:.4f} ({uom_name or 'unknown unit'})"

            calc = qty * price * (1 - discount / 100)
            calculated_total += subtotal

            print(f"\n[{i}] {product_display}")
            print(f"    Quantity: {qty_display}")
            print(f"    Unit Price: HK$ {price:.2f}")
            if discount > 0:
                original_subtotal = qty * price
                savings = original_subtotal - subtotal
                print(f"    💰 Discount: {discount}% (save HK$ {savings:.2f})")
            else:
                print(f"    Discount: {discount}%")
            print(f"    Subtotal: HK$ {subtotal:.2f}")
            print(f"    Verify: {subtotal:.2f} = {qty} × {price} × (1 - {discount}%)")

            if abs(subtotal - calc) < 0.01:
                print(f"    [OK] Passed")
            else:
                print(f"    [FAIL] Diff: {abs(subtotal - calc):.2f}")

    print("\n" + "=" * 70)
    print(f"Total: HK$ {o.get('amount_total', 0):.2f}")
    if abs(o.get("amount_total", 0) - calculated_total) < 0.01:
        print("[OK] Verification passed")
        return True
    else:
        print(
            f"[FAIL] Verification failed! Diff: {abs(o.get('amount_total', 0) - calculated_total):.2f}"
        )
        return False


def confirm_order(
    client: OdooClient,
    order_id: int,
    require_address: bool = True,
    auto_select_address: bool = False,
    selected_address_id: Optional[int] = None,
    skip_confirmation: bool = False,
):
    """Confirm order with address confirmation

    Args:
        client: OdooClient instance
        order_id: Order ID to confirm
        require_address: If True, require address confirmation before confirming
        auto_select_address: If True, auto-select first address when multiple exist (for CLI mode)
        selected_address_id: Specific address ID to use (overrides auto-selection)

    Returns:
        True if order confirmed successfully, False otherwise
    """
    try:
        # Get order information
        order = client._call(
            model="sale.order",
            method="read",
            args=[[order_id]],
            kwargs={"fields": ["partner_id", "partner_shipping_id", "state"]},
        )

        if not order:
            print(f"\nOrder {order_id} not found")
            return False

        o = order[0]

        # Check if order is already confirmed
        if o.get("state") not in ["draft", "sent"]:
            print(
                f"\nOrder {order_id} is already confirmed or cancelled (state: {o.get('state')})"
            )
            return False

        # Address confirmation flow
        if require_address:
            partner_id = o.get("partner_id")
            if partner_id and isinstance(partner_id, list):
                partner_id = partner_id[0]

            shipping_id = o.get("partner_shipping_id")
            if shipping_id and isinstance(shipping_id, list):
                shipping_id = shipping_id[0]

            # Get customer delivery addresses
            addresses = client.get_customer_addresses(partner_id)
            delivery_addresses = [a for a in addresses if a.get("type") == "delivery"]

            # If order already has a valid shipping address (not main customer), use it
            # Don't override an explicitly set address
            if shipping_id and shipping_id != partner_id:
                # Order has an explicit shipping address set, verify it's still valid
                address_is_valid = any(
                    a.get("id") == shipping_id for a in delivery_addresses
                )
                if address_is_valid:
                    print(f"\n[OK] Using existing shipping address (ID: {shipping_id})")
                    shipping_id_to_use = shipping_id
                else:
                    # Previously set address is no longer valid, need to select new one
                    print(
                        f"\n[WARNING] Previously set shipping address (ID: {shipping_id}) is no longer valid"
                    )
                    print(f"[INFO] Please select a new shipping address")
                    shipping_id_to_use = None
            else:
                # No valid shipping address set, need to select one
                shipping_id_to_use = None

            if delivery_addresses and shipping_id_to_use is None:
                if len(delivery_addresses) == 1:
                    # Single address - confirm it
                    addr = delivery_addresses[0]
                    print(f"\nPlease confirm shipping address:")
                    print(f"  📍 {addr.get('name', 'N/A')}")
                    print(f"     {format_address_inline(addr)}")

                    # In CLI mode, default to yes if no input
                    # In Agent mode, this will be handled differently
                    print(f"\n[OK] Using shipping address: {addr.get('name')}")
                    shipping_id_to_use = addr.get("id")

                else:
                    # Multiple addresses - require active selection
                    print(
                        f"\n📍 Customer has {len(delivery_addresses)} shipping addresses:"
                    )
                    print("=" * 60)

                    for i, addr in enumerate(delivery_addresses, 1):
                        print(f"\n[{i}] {addr.get('name', 'N/A')}")
                        print(f"    Address ID: {addr.get('id')}")
                        print(f"    {format_address_inline(addr)}")

                    print("\n" + "=" * 60)

                    # Check if user explicitly selected an address
                    if selected_address_id is not None:
                        selected_addr = next(
                            (
                                a
                                for a in delivery_addresses
                                if a.get("id") == selected_address_id
                            ),
                            None,
                        )
                        if selected_addr:
                            shipping_id_to_use = selected_addr.get("id")
                            print(
                                f"\n[OK] Using selected address: {selected_addr.get('name')} (ID: {selected_address_id})"
                            )
                        else:
                            print(
                                f"\n[ERROR] Selected address ID {selected_address_id} not found in delivery addresses"
                            )
                            return False
                    elif auto_select_address:
                        # CLI mode: auto-select with warning
                        print(
                            f"\n[WARNING] Auto-selecting first address: {delivery_addresses[0].get('name')} (ID: {delivery_addresses[0].get('id')})"
                        )
                        print(
                            f"[INFO] Verify address is correct before confirming order."
                        )
                        shipping_id_to_use = delivery_addresses[0].get("id")
                    else:
                        # Agent mode: require explicit selection
                        print(f"\n[ACTION REQUIRED] Multiple delivery addresses found.")
                        print(
                            f"Please ask the customer to select their shipping address (1-{len(delivery_addresses)})."
                        )
                        return False

            # Update order address if different (only when shipping_id_to_use was just determined)
            if shipping_id_to_use and shipping_id_to_use != shipping_id:
                client.update_order(
                    order_id=order_id,
                    partner_shipping_id=shipping_id_to_use,
                    partner_invoice_id=shipping_id_to_use,
                )
                print(f"[OK] Shipping address updated")

            elif not shipping_id_to_use:
                # No valid address found, use main address as fallback
                print(
                    f"\n[WARNING] No valid shipping address found, will use customer main address"
                )
                shipping_id_to_use = partner_id

        # ========== Narrow Fabric Summary Check ==========
        # Get order lines for narrow fabric check
        order_lines_data = client._call(
            model="sale.order",
            method="read",
            args=[[order_id]],
            kwargs={"fields": ["order_line"]},
        )

        if order_lines_data:
            line_ids = order_lines_data[0].get("order_line", [])

            # Check each product for narrow fabric
            narrow_products = []
            for line_id in line_ids:
                line = client._call(
                    model="sale.order.line",
                    method="read",
                    args=[[line_id]],
                    kwargs={"fields": ["product_default_code"]},
                )
                if line:
                    default_code = line[0].get("product_default_code")
                    if default_code:
                        # Search product.template by default_code
                        template = client._call(
                            model="product.template",
                            method="search_read",
                            args=[[["default_code", "=", default_code]]],
                            kwargs={
                                "fields": ["default_code", "name", "width_cm"],
                                "limit": 1,
                            },
                        )
                        if template:
                            width_cm = template[0].get("width_cm")
                            if (
                                width_cm is not None
                                and width_cm > 0
                                and width_cm < OdooClient.NARROW_FABRIC_THRESHOLD_CM
                            ):
                                narrow_products.append(
                                    {
                                        "code": template[0].get("default_code"),
                                        "name": template[0].get("name"),
                                        "width_cm": width_cm,
                                    }
                                )

            # Display summary if narrow fabrics found
            if narrow_products:
                print(f"\n[WARNING] 訂單包含 {len(narrow_products)} 種窄幅面料:")
                for np in narrow_products:
                    print(f"  - [{np['code']}] {np['name']} ({np['width_cm']}cm)")
                print("\n請確認所需碼數/米數是否足夠。")
                if skip_confirmation:
                    # Auto-confirm for non-interactive mode (AI calls)
                    print("[AUTO-CONFIRMED] Proceeding with order confirmation.")
                else:
                    try:
                        confirm = (
                            input(
                                "Enter 'yes' to confirm order, or any other key to cancel: "
                            )
                            .strip()
                            .lower()
                        )
                        if confirm not in ("yes", "y"):
                            print("[CANCELLED] Order confirmation cancelled.")
                            return False
                    except (KeyboardInterrupt, EOFError):
                        print("\n[CANCELLED] Order confirmation cancelled.")
                        return False
        # ========== End Narrow Fabric Summary Check ==========

        # Confirm the order
        client.confirm_order(order_id)
        print(f"\nOrder {order_id} confirmed successfully")
        return True

    except Exception as e:
        print(f"\nFailed to confirm order: {e}")
        return False


def get_address_detail(client: OdooClient, address_id: int):
    """Fetch and print full address details by address ID.

    Args:
        client: OdooClient instance
        address_id: res.partner ID of the address
    """
    addresses = client._call(
        model="res.partner",
        method="read",
        args=[[address_id]],
        kwargs={"fields": ["name", "street", "street2", "city", "zip", "state_id", "country_id"]},
    )

    if not addresses or len(addresses) == 0:
        print(f"Address ID {address_id} not found")
        return

    addr = addresses[0]
    name = addr.get("name", "N/A")
    full_address = format_address_inline(addr)
    print(f"Name: {name}")
    print(f"Address: {full_address}")


def format_address_inline(address: dict) -> str:
    """Format address as a single line string

    Args:
        address: Address dictionary with keys: street, street2, city, zip, state_id, country_id

    Returns:
        Formatted address string
    """
    parts = []

    if address.get("street"):
        parts.append(address["street"])
    if address.get("street2"):
        parts.append(address["street2"])

    city_parts = []
    if address.get("zip"):
        city_parts.append(str(address["zip"]))
    if address.get("city"):
        city_parts.append(address["city"])
    if city_parts:
        parts.append(" ".join(city_parts))

    if (
        address.get("state_id")
        and isinstance(address["state_id"], list)
        and len(address["state_id"]) > 1
    ):
        parts.append(address["state_id"][1])

    if (
        address.get("country_id")
        and isinstance(address["country_id"], list)
        and len(address["country_id"]) > 1
    ):
        parts.append(address["country_id"][1])

    return ", ".join(parts) if parts else "N/A"


def cancel_order(client: OdooClient, order_id: int):
    """Cancel order"""
    # Check if order is editable (draft state only)
    is_editable, error_msg, order = check_order_editable(client, order_id)
    if not is_editable:
        print(f"\n[ERROR] {error_msg}")
        return False

    try:
        client.cancel_order(order_id)
        print(f"\nOrder {order_id} cancelled")
        return True
    except Exception as e:
        print(f"\nFailed to cancel order: {e}")
        return False


def remove_order_line_cli(client: OdooClient, order_id: int, product_code: str = None):
    """Remove order line"""
    # Check if order is editable (draft state only)
    is_editable, error_msg, order_info = check_order_editable(client, order_id)
    if not is_editable:
        print(f"\n[ERROR] {error_msg}")
        return False

    # Get order lines from the order info
    order_lines = order_info.get("order_line", [])

    if not order_lines:
        print(f"\nOrder {order_id} has no order lines")
        return False

    target_line_id = None
    target_product = None

    if product_code:
        for line_id in order_lines:
            line = client._call(
                model="sale.order.line",
                method="read",
                args=[[line_id]],
                kwargs={"fields": ["product_id", "product_uom_qty", "price_subtotal"]},
            )
            if line:
                l = line[0]
                product = l.get("product_id", [None, ""])
                if product and f"[{product_code}]" in str(product[1]):
                    target_line_id = line_id
                    target_product = product[1]
                    break
        if not target_line_id:
            print(f"\nProduct {product_code} not found in order")
            return False
    else:
        print(f"\nOrder {order_id} order lines:")
        print("-" * 60)
        line_list = []
        for i, line_id in enumerate(order_lines, 1):
            line = client._call(
                model="sale.order.line",
                method="read",
                args=[[line_id]],
                kwargs={"fields": ["product_id", "product_uom_qty", "price_subtotal"]},
            )
            if line:
                l = line[0]
                product = (
                    l.get("product_id", [None, ""])[1] if l.get("product_id") else "N/A"
                )
                qty = l.get("product_uom_qty", 0)
                subtotal = l.get("price_subtotal", 0)
                line_list.append((line_id, product))
                print(f"  [{i}] {product}")
                print(f"      Qty: {qty:.4f}yd, Subtotal: HK$ {subtotal:.2f}")
        print("-" * 60)

        try:
            choice = input(
                f"\nSelect order line to remove [1-{len(line_list)}]: "
            ).strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < len(line_list):
                    target_line_id, target_product = line_list[idx]
                else:
                    print("Invalid selection")
                    return False
            else:
                print("Cancelled")
                return False
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\nCancelled")
            return False

    result = client.remove_order_line(target_line_id)

    if result:
        print(f"\nOrder line removed: {target_product}")
        return True
    else:
        print(f"\nRemoval failed")
        return False


def rebuild_order_for_reduction(
    client: OdooClient,
    order_id: int,
    cancel_line_ids: list[int] = None,
    qty_changes: dict[int, float] = None,
) -> dict:
    """Rebuild order for reductions (cancel lines or reduce quantities)

    This is used when:
    - Cancelling order lines
    - Reducing order line quantities

    For draft orders, we cancel the old order and create a new one with
    the modified lines, linking them via notes.

    Args:
        client: OdooClient instance
        order_id: Original order ID
        cancel_line_ids: List of line IDs to cancel/remove
        qty_changes: Dict of {line_id: new_qty} for quantity reductions

    Returns:
        Dict with 'success', 'new_order_id', 'new_order_name', 'message'
    """
    cancel_line_ids = cancel_line_ids or []
    qty_changes = qty_changes or {}

    try:
        # Get original order info
        order = client.get_order(order_id)
        if not order:
            return {"success": False, "message": f"Order {order_id} not found"}

        old_order_name = order.get("name", f"#{order_id}")
        state = order.get("state", "")
        old_order_name = order.get("name", f"#{order_id}")

        # Only rebuild for draft orders (not sent/sale/cancel)
        if state != "draft":
            if state == "sent":
                error_detail = "Order is in 'Quotation Sent' state. Modifications are not allowed after confirmation."
            elif state == "sale":
                error_detail = "Order is already confirmed (Sale Order)."
            else:
                error_detail = f"Order is in '{state}' state."

            return {
                "success": False,
                "message": f"Order {old_order_name} cannot be rebuilt. {error_detail}",
            }

        # Get all order lines
        all_lines = client.get_order_lines(order_id)
        if not all_lines:
            return {"success": False, "message": "Order has no lines to copy"}

        # Filter and modify lines
        lines_to_copy = []
        for line in all_lines:
            line_id = line.get("id")

            # Skip cancelled lines
            if line_id in cancel_line_ids:
                continue

            # Check for quantity changes
            if line_id in qty_changes:
                new_qty = qty_changes[line_id]
                if new_qty <= 0:
                    continue  # Skip if new qty is 0 or negative
                line = line.copy()
                line["product_uom_qty"] = new_qty

            lines_to_copy.append(line)

        if not lines_to_copy:
            return {
                "success": False,
                "message": "No lines remaining after changes. Consider cancelling the entire order instead.",
            }

        # Create new order
        partner_id = (
            order.get("partner_id", [None])[0]
            if order.get("partner_id")
            else None
        )
        if not partner_id:
            return {"success": False, "message": "Could not determine customer"}

        # Get addresses
        shipping_id = (
            order.get("partner_shipping_id", [None])[0]
            if order.get("partner_shipping_id")
            else None
        )
        invoice_id = (
            order.get("partner_invoice_id", [None])[0]
            if order.get("partner_invoice_id")
            else None
        )

        # Create new order with reference note
        new_note = f"Replaces {old_order_name}"
        new_order_id = client.create_order(
            partner_id=partner_id,
            note=new_note,
            partner_shipping_id=shipping_id,
            partner_invoice_id=invoice_id,
        )

        # Get new order name
        new_order = client.get_order(new_order_id)
        new_order_name = new_order.get("name", f"#{new_order_id}") if new_order else f"#{new_order_id}"

        # Copy lines to new order
        for line in lines_to_copy:
            product_id = (
                line.get("product_id", [None])[0]
                if line.get("product_id")
                else None
            )
            if not product_id:
                continue

            qty = line.get("product_uom_qty", 1)
            price = line.get("price_unit", 0)
            uom_id = line.get("product_uom", [None])[0] if isinstance(line.get("product_uom"), list) else line.get("product_uom")
            customer_po = line.get("customer_po")

            new_line_id = client.add_order_line(
                order_id=new_order_id,
                product_id=product_id,
                qty=qty,
                price=price,
                uom_id=uom_id,
                customer_po=customer_po,
            )

            # Copy discount if any
            discount = line.get("discount", 0)
            if discount:
                client._call(
                    model="sale.order.line",
                    method="write",
                    args=[[new_line_id], {"discount": discount}],
                )

        # Cancel old order with reference to new order
        cancel_note = f"Cancelled - Replaced by {new_order_name}"
        client.update_order_note(order_id, cancel_note)
        client.cancel_order(order_id)

        print(f"\n{'=' * 60}")
        print("Order Rebuilt for Reduction")
        print("=" * 60)
        print(f"  Old Order: {old_order_name} (cancelled)")
        print(f"  New Order: {new_order_name}")
        print(f"  Lines copied: {len(lines_to_copy)}")
        if cancel_line_ids:
            print(f"  Lines cancelled: {len(cancel_line_ids)}")
        if qty_changes:
            print(f"  Lines with qty changes: {len(qty_changes)}")
        print("=" * 60)

        return {
            "success": True,
            "new_order_id": new_order_id,
            "new_order_name": new_order_name,
            "old_order_name": old_order_name,
            "lines_copied": len(lines_to_copy),
            "message": f"Created new order {new_order_name} to replace {old_order_name}",
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"Failed to rebuild order: {e}"}


def cancel_order_lines(client: OdooClient, order_id: int, line_ids: str):
    """Cancel specific order lines (partial cancellation)

    Only allowed for draft orders.
    """
    # Check if order is editable (draft state only)
    is_editable, error_msg, order = check_order_editable(client, order_id)
    if not is_editable:
        print(f"\n[ERROR] {error_msg}")
        return False

    order_name = order.get("name", f"#{order_id}")

    try:
        # Parse comma-separated line IDs
        line_id_list = [int(x.strip()) for x in line_ids.split(",") if x.strip()]
        if not line_id_list:
            print("\nNo valid line IDs provided")
            return False

        # For draft orders, directly remove order lines
        print(f"\nCancelling order lines on order {order_name}...")
        success_count = 0
        for line_id in line_id_list:
            try:
                client.remove_order_line(line_id)
                print(f"  Cancelled line {line_id}")
                success_count += 1
            except Exception as e:
                print(f"  Failed to cancel line {line_id}: {e}")

        print(f"\n{success_count}/{len(line_id_list)} order lines cancelled")
        return success_count > 0
    except Exception as e:
        print(f"\nFailed to cancel order lines: {e}")
        return False


def update_order_line_qty(client: OdooClient, line_id: int, qty: float):
    """Update order line quantity

    For all orders: Direct update (no rebuild needed for quotation stage)
    """
    try:
        # Get current line info
        line = client._call(
            model="sale.order.line",
            method="read",
            args=[[line_id]],
            kwargs={"fields": ["product_id", "product_uom_qty", "order_id"]},
        )

        if not line:
            print(f"\nOrder line {line_id} not found")
            return False

        l = line[0]
        product = l.get("product_id", [None, ""])[1] if l.get("product_id") else "N/A"
        old_qty = l.get("product_uom_qty", 0)
        order_id = l.get("order_id", [None])[0] if l.get("order_id") else None

        if not order_id:
            print(f"\nCould not determine order for line {line_id}")
            return False

        # Get order state
        order = client.get_order(order_id)
        if not order:
            print(f"\nOrder {order_id} not found")
            return False

        state = order.get("state", "")
        order_name = order.get("name", f"#{order_id}")

        # For all orders, directly update the quantity
        print(f"\nUpdating quantity directly on order {order_name} ({state} state)...")
        client.update_order_line(line_id, qty=qty)

        print(f"\nOrder line {line_id} updated:")
        print(f"  Product: {product}")
        print(f"  Quantity: {old_qty} -> {qty}")
        print(f"  Order: {order_name} ({state})")
        return True
    except Exception as e:
        print(f"\nFailed to update order line: {e}")
        return False


def set_order_line_customer_po(client: OdooClient, line_id: int, customer_po: str):
    """Set customer PO for an order line

    Args:
        client: OdooClient instance
        line_id: Order line ID
        customer_po: Customer PO Number
    """
    try:
        # Get order line info first
        line = client._call(
            model="sale.order.line",
            method="read",
            args=[[line_id]],
            kwargs={
                "fields": ["product_id", "order_id"]
            },
        )

        if not line:
            print(f"\nOrder line {line_id} not found")
            return False

        l = line[0]
        product = l.get("product_id", [None, "N/A"])[1] if l.get("product_id") else "N/A"

        # Update customer_po
        client.update_order_line(line_id, customer_po=customer_po)

        print(f"\nCustomer PO set for order line {line_id}:")
        print(f"  Product: {product}")
        print(f"  Customer PO: {customer_po}")
        return True
    except Exception as e:
        print(f"\nFailed to set customer PO: {e}")
        return False


def add_address(
    client: OdooClient,
    partner_id: int,
    name: str,
    street: str,
    street2: str = None,
    city: str = None,
    zip: str = None,
    address_type: str = "delivery",
):
    """Add new address for customer"""
    try:
        new_id = client.create_address(
            partner_id=partner_id,
            name=name,
            street=street,
            street2=street2,
            city=city,
            zip=zip,
            address_type=address_type,
        )
        print(f"\nNew address created!")
        print(f"  Address ID: {new_id}")
        print(f"  Name: {name}")
        print(f"  Address: {street}")
        if street2:
            print(f"          {street2}")
        if city:
            print(f"  City: {city}")
        print(f"  Type: {address_type}")
        return new_id
    except Exception as e:
        print(f"\nFailed to create address: {e}")
        return None


def get_payment_terms(client: OdooClient) -> list:
    """Get all payment terms"""
    terms = client._call(
        model="account.payment.term",
        method="search_read",
        args=[[]],
        kwargs={"fields": ["id", "name"], "order": "name asc"},
    )
    return terms


def set_order_addresses(
    client: OdooClient, order_id: int, shipping_id: int = None, invoice_id: int = None
):
    """Set shipping and invoice address for order"""
    order = client._call(
        model="sale.order",
        method="read",
        args=[[order_id]],
        kwargs={"fields": ["partner_id", "partner_shipping_id", "partner_invoice_id"]},
    )

    if not order:
        print(f"\nOrder {order_id} not found")
        return False

    o = order[0]
    partner_id = o.get("partner_id", [None])[0] if o.get("partner_id") else None

    if not partner_id:
        print(f"\nOrder has no customer associated")
        return False

    if shipping_id is None:
        all_addresses = client.get_customer_addresses(partner_id)

        main_customer = client._call(
            model="res.partner",
            method="read",
            args=[[partner_id]],
            kwargs={
                "fields": [
                    "name",
                    "street",
                    "street2",
                    "city",
                    "zip",
                    "state_id",
                    "country_id",
                ]
            },
        )

        if main_customer and len(main_customer) > 0:
            c = main_customer[0]
            if c.get("street"):
                main_addr = {
                    "id": partner_id,
                    "name": c.get("name", "N/A") + " (Main)",
                    "street": c.get("street"),
                    "street2": c.get("street2", ""),
                    "city": c.get("city"),
                }
                all_addresses.insert(0, main_addr)

        if not all_addresses:
            print(f"\nCustomer has no available addresses")
            return False

        print(f"\nAvailable shipping addresses:")
        print("-" * 70)
        for i, addr in enumerate(all_addresses, 1):
            addr_id = addr.get("id", "N/A")
            name = addr.get("name", "N/A")
            street = addr.get("street", "")
            street2 = addr.get("street2", "")
            city = addr.get("city", "N/A")
            zip_code = addr.get("zip", "")
            state = addr.get("state_id", "")
            country = addr.get("country_id", "")

            # Build full address
            addr_parts = []
            if street:
                addr_parts.append(street)
            if street2:
                addr_parts.append(street2)
            if city or zip_code:
                city_parts = []
                if zip_code:
                    city_parts.append(str(zip_code))
                if city:
                    city_parts.append(city)
                addr_parts.append(" ".join(city_parts))
            if state and isinstance(state, list) and len(state) > 1:
                addr_parts.append(state[1])
            if country and isinstance(country, list) and len(country) > 1:
                addr_parts.append(country[1])

            full_addr = ", ".join(addr_parts) if addr_parts else "N/A"

            print(f"  [{i}] ID:{addr_id} {name}")
            print(f"      {full_addr}")
        print("-" * 70)

        try:
            choice = input(
                f"\nSelect shipping address [1-{len(all_addresses)}]: "
            ).strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < len(all_addresses):
                    shipping_id = all_addresses[idx]["id"]
                else:
                    print("Invalid selection")
                    return False
            else:
                print("Cancelled")
                return False
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\nCancelled")
            return False

    # Build update dict - only update addresses that are explicitly specified
    # When user says "change address", they ONLY mean shipping address
    # Do NOT modify invoice address unless explicitly requested
    update_data = {}
    if shipping_id is not None:
        update_data["partner_shipping_id"] = shipping_id
    if invoice_id is not None:
        update_data["partner_invoice_id"] = invoice_id

    result = client.update_order(
        order_id=order_id,
        **update_data,
    )

    if result:
        print(f"\nOrder {order_id} addresses updated")
        print(f"  Shipping Address ID: {shipping_id}")
        print(f"  Invoice Address ID: {invoice_id}")
        return True
    else:
        print(f"\nUpdate failed")
        return False


def set_order_po(
    client: OdooClient, order_id: int, po_number: str, product_code: str = None
):
    """Set customer PO number

    Args:
        client: OdooClient instance
        order_id: Order ID
        po_number: Customer PO number
        product_code: Product code (optional), sets PO for single line if provided
    """
    if not po_number:
        print(f"\nPO# cannot be empty")
        return False

    if product_code:
        # Set PO for single order line
        line_id = client.get_order_line_id_by_product(order_id, product_code)
        if not line_id:
            print(f"\nProduct {product_code} not found in order {order_id}")
            return False

        result = client.set_order_line_client_order_ref(line_id, po_number)
        if result:
            print(
                f"\nProduct [{product_code}] in order {order_id} PO# set to: {po_number}"
            )
            print(f"  Order Line ID: {line_id}")
            return True
        else:
            print(f"\nSet failed")
            return False
    else:
        # Set PO for entire order
        result = client.update_order(order_id, client_order_ref=po_number)

        if result:
            print(f"\nOrder {order_id} customer PO# set: {po_number}")
            return True
        else:
            print(f"\nSet failed")
            return False


def set_order_payment(client: OdooClient, order_id: int, payment_term_id: int = None):
    """Set payment terms"""
    if payment_term_id is None:
        terms = get_payment_terms(client)

        if not terms:
            print(f"\nNo payment terms available")
            return False

        print(f"\nAvailable payment terms:")
        print("-" * 60)
        for i, term in enumerate(terms, 1):
            print(f"  [{i}] {term.get('name', 'N/A')} (ID: {term.get('id')})")
        print("-" * 60)

        try:
            choice = input(
                f"\nSelect payment term [1-{len(terms)}] (or press Enter to skip): "
            ).strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < len(terms):
                    payment_term_id = terms[idx]["id"]
                else:
                    print("Invalid selection")
                    return False
            else:
                print("Cancelled")
                return False
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\nCancelled")
            return False

    result = client.update_order(order_id, payment_term_id=payment_term_id)

    if result:
        term = client._call(
            model="account.payment.term",
            method="read",
            args=[[payment_term_id]],
            kwargs={"fields": ["name"]},
        )
        term_name = term[0].get("name", "N/A") if term else "N/A"

        print(f"\nOrder {order_id} payment term set: {term_name}")
        return True
    else:
        print(f"\nSet failed")
        return False


def get_address_details(client: OdooClient, address_id: int) -> str:
    """Get full address details"""
    if not address_id:
        return "N/A"

    address = client._call(
        model="res.partner",
        method="read",
        args=[[address_id]],
        kwargs={
            "fields": [
                "name",
                "street",
                "street2",
                "city",
                "zip",
                "state_id",
                "country_id",
            ]
        },
    )

    if not address:
        return "N/A"

    a = address[0]
    parts = []

    # Name
    if a.get("name"):
        parts.append(a["name"])

    # Street
    if a.get("street"):
        parts.append(a["street"])
    if a.get("street2"):
        parts.append(a["street2"])

    # City and zip
    city_parts = []
    if a.get("zip"):
        city_parts.append(str(a["zip"]))
    if a.get("city"):
        city_parts.append(a["city"])
    if city_parts:
        parts.append(" ".join(city_parts))

    # State
    if a.get("state_id") and isinstance(a["state_id"], list) and len(a["state_id"]) > 1:
        parts.append(a["state_id"][1])

    # Country
    if (
        a.get("country_id")
        and isinstance(a["country_id"], list)
        and len(a["country_id"]) > 1
    ):
        parts.append(a["country_id"][1])

    return ", ".join(parts) if parts else "N/A"


def show_order_detail(client: OdooClient, order_id: int):
    """Show order details"""
    order = client._call(
        model="sale.order",
        method="read",
        args=[[order_id]],
        kwargs={
            "fields": [
                "name",
                "state",
                "partner_id",
                "partner_shipping_id",
                "partner_invoice_id",
                "client_order_ref",
                "payment_term_id",
                "amount_total",
                "amount_untaxed",
                "order_line",
                "date_order",
            ]
        },
    )

    if not order:
        print(f"\nOrder {order_id} not found")
        return False

    o = order[0]

    print("\n" + "=" * 70)
    print(f"Order Detail: {order_id}")
    print("=" * 70)

    print(f"\nOrder #: {o.get('name')}")
    print(f"Status: {o.get('state')}")
    print(
        f"Customer: {o.get('partner_id', [None, ''])[1] if o.get('partner_id') else 'N/A'}"
    )
    print(f"Date: {o.get('date_order')}")

    shipping = o.get("partner_shipping_id")
    invoice = o.get("partner_invoice_id")

    shipping_id = shipping[0] if shipping and isinstance(shipping, list) else shipping
    invoice_id = invoice[0] if invoice and isinstance(invoice, list) else invoice

    shipping_name = (
        shipping[1] if shipping and isinstance(shipping, list) else str(shipping)
    )
    invoice_name = invoice[1] if invoice and isinstance(invoice, list) else str(invoice)

    shipping_full = get_address_details(client, shipping_id) if shipping_id else "N/A"
    invoice_full = get_address_details(client, invoice_id) if invoice_id else "N/A"

    print(f"\nShipping Address ({shipping_name}):")
    print(f"  {shipping_full}")
    print(f"\nInvoice Address ({invoice_name}):")
    print(f"  {invoice_full}")

    print(f"\nCustomer PO#: {o.get('client_order_ref') or 'N/A'}")
    payment_term = o.get("payment_term_id")
    print(
        f"Payment Term: {payment_term[1] if payment_term and isinstance(payment_term, list) else payment_term}"
    )

    print(f"\nUntaxed Amount: HK$ {o.get('amount_untaxed', 0):.2f}")
    print(f"Total Amount: HK$ {o.get('amount_total', 0):.2f}")

    order_lines = o.get("order_line", [])
    print(f"\nOrder Lines ({len(order_lines)} line(s)):")
    print("-" * 70)

    for i, line_id in enumerate(order_lines, 1):
        line = client._call(
            model="sale.order.line",
            method="read",
            args=[[line_id]],
            kwargs={
                "fields": [
                    "product_id",
                    "product_uom_qty",
                    "price_unit",
                    "discount",
                    "price_subtotal",
                    "product_uom",
                    "customer_po",
                ]
            },
        )
        if line:
            l = line[0]
            product = (
                l.get("product_id", [None, ""])[1] if l.get("product_id") else "N/A"
            )
            qty = l.get("product_uom_qty", 0)
            price = l.get("price_unit", 0)
            discount = l.get("discount", 0)
            subtotal = l.get("price_subtotal", 0)
            line_po = l.get("client_order_ref")
            customer_po = l.get("customer_po")

            print(f"\n[{i}] {product}")
            print(f"    Quantity: {qty:.4f}yd ({qty * 0.9144:.2f}m)")
            print(f"    Unit Price: HK$ {price:.2f}")
            if discount > 0:
                original_subtotal = qty * price
                savings = original_subtotal - subtotal
                print(f"    💰 Discount: {discount}% (save HK$ {savings:.2f})")
            else:
                print(f"    Discount: {discount}%")
            print(f"    Subtotal: HK$ {subtotal:.2f}")
            if line_po:
                print(f"    PO#: {line_po}")
            if customer_po:
                print(f"    Customer PO: {customer_po}")

    print("\n" + "=" * 70)

    return True


def print_order_report(client: OdooClient, order_id: int):
    """Print order confirmation link"""
    portal_url = client.get_order_portal_url(order_id)

    if portal_url:
        order = client._call(
            model="sale.order",
            method="read",
            args=[[order_id]],
            kwargs={"fields": ["name", "state"]},
        )
        o = order[0] if order else {}

        print("\n" + "=" * 70)
        print(f"Order Confirmation Link")
        print("=" * 70)
        print(f"\nOrder #: {o.get('name', order_id)}")
        print(f"Status: {o.get('state', 'N/A')}")
        print(f"\n[PDF] Confirmation Link (click to view/print):")
        print(f"\n{portal_url}")
        print("\n" + "=" * 70)
        print("\nTip: Open this link in browser to view and print PDF confirmation")
        return True
    else:
        print(f"\nOrder {order_id} has no portal link available")
        return False


def list_addresses(client: OdooClient, partner_id: int, with_stats: bool = False):
    """List all shipping addresses for customer"""
    all_addresses = client.get_customer_addresses(partner_id)

    main_customer = client._call(
        model="res.partner",
        method="read",
        args=[[partner_id]],
        kwargs={"fields": ["name", "street", "street2", "city", "type"]},
    )

    main_addr_street = None
    if main_customer and len(main_customer) > 0:
        c = main_customer[0]
        main_addr_street = c.get("street")
        if main_addr_street:
            main_addr = {
                "id": partner_id,
                "name": c.get("name", "N/A") + " (Main)",
                "street": c.get("street"),
                "street2": c.get("street2", ""),
                "city": c.get("city"),
                "type": c.get("type", "contact"),
            }
            all_addresses.insert(0, main_addr)

    if not all_addresses:
        print(f"\nCustomer {partner_id} has no shipping addresses")
        return

    latest_shipping_id = None
    latest_order_id = None
    latest_order_date = None
    latest_shipping_street = None

    try:
        orders = client.search_orders(partner_id, state=None, limit=1)
        if orders:
            latest_order = orders[0]
            latest_order_id = latest_order.get("id")
            latest_order_date = latest_order.get("date_order")

            order_detail = client._call(
                model="sale.order",
                method="read",
                args=[[latest_order_id]],
                kwargs={"fields": ["partner_shipping_id", "partner_invoice_id"]},
            )

            if order_detail and len(order_detail) > 0:
                shipping_id = order_detail[0].get("partner_shipping_id")
                if shipping_id:
                    if isinstance(shipping_id, list):
                        latest_shipping_id = (
                            shipping_id[0] if len(shipping_id) > 0 else None
                        )
                    else:
                        latest_shipping_id = shipping_id

                    if latest_shipping_id == partner_id:
                        latest_shipping_street = main_addr_street
                    else:
                        sub_addr = client._call(
                            model="res.partner",
                            method="read",
                            args=[[latest_shipping_id]],
                            kwargs={"fields": ["street"]},
                        )
                        if sub_addr and len(sub_addr) > 0:
                            latest_shipping_street = sub_addr[0].get("street")
    except:
        pass

    latest_addr = None
    other_addrs = []

    for addr in all_addresses:
        addr_id = addr.get("id")
        street = addr.get("street", "")

        is_latest = False
        if latest_shipping_street and street == latest_shipping_street:
            is_latest = True
        elif latest_shipping_id and addr_id == latest_shipping_id:
            is_latest = True

        if is_latest:
            latest_addr = addr
        else:
            other_addrs.append(addr)

    if latest_addr:
        all_addresses = [latest_addr] + other_addrs

    print(f"\nShipping addresses for customer {partner_id}:")
    if latest_order_id:
        print(f"(Latest order: #{latest_order_id}, {latest_order_date})")
    print("=" * 60)

    for i, addr in enumerate(all_addresses, 1):
        addr_type = addr.get("type", "other")
        name = addr.get("name", "N/A")
        street = addr.get("street", "")
        street2 = addr.get("street2", "")
        city = addr.get("city", "N/A")
        state = addr.get("state_id", "")
        addr_id = addr.get("id")

        badge = ""
        if i == 1 and (latest_shipping_street or latest_shipping_id):
            badge = " [Recently Used]"

        full_address = street
        if street2:
            full_address += " " + street2
        if city != "N/A":
            full_address += ", " + city
        if state and isinstance(state, list) and len(state) > 1:
            full_address += ", " + state[1]

        print(f"\n[{i}] {badge} {name}")
        print(f"    Address ID: {addr_id}")
        print(f"    Address: {full_address}")

    print("\n" + "=" * 60)


def list_customer_orders(
    client: OdooClient,
    partner_id: int,
    days: int = 90,
    state: str = None,
    limit: int = 20,
):
    """List customer's orders

    Args:
        client: OdooClient instance
        partner_id: Customer ID
        days: Time range in days (default 90)
        state: Filter by order state (draft/sent/sale/done/cancel)
        limit: Maximum results
    """
    from datetime import datetime, timedelta

    # Build domain with time filter
    domain = [["partner_id", "=", partner_id]]

    # Add date filter
    start_date = datetime.now() - timedelta(days=days)
    date_str = start_date.strftime("%Y-%m-%d 00:00:00")
    domain.append(["date_order", ">=", date_str])

    # Add state filter if specified
    if state:
        domain.append(["state", "=", state])

    # Search orders
    orders = client._call(
        model="sale.order",
        method="search_read",
        args=[domain],
        kwargs={
            "fields": ["id", "name", "state", "amount_total", "date_order"],
            "limit": limit,
            "order": "date_order DESC",
        },
    )

    if not orders:
        print(f"\nNo orders found for customer {partner_id}")
        if days:
            print(f"(within the last {days} days)")
        if state:
            print(f"(state: {state})")
        return

    # Get customer name
    customer = client._call(
        model="res.partner",
        method="read",
        args=[[partner_id]],
        kwargs={"fields": ["name"]},
    )
    customer_name = customer[0].get("name", "N/A") if customer else "N/A"

    # Print header
    print("\n" + "=" * 70)
    print(f"📋 Order List (Last {days} days)")
    print("=" * 70)
    print(f"\nCustomer: {customer_name}")
    print(f"Orders found: {len(orders)}")
    print("\n" + "-" * 70)

    # State labels
    state_labels = {
        "draft": "Draft",
        "sent": "Sent",
        "sale": "Confirmed",
        "done": "Done",
        "cancel": "Cancelled",
    }

    for i, order in enumerate(orders, 1):
        order_id = order.get("id")
        order_name = order.get("name")
        order_state = order.get("state")
        amount = order.get("amount_total", 0)
        date_order = order.get("date_order", "")

        # Format date
        if date_order:
            try:
                dt = datetime.fromisoformat(
                    date_order.replace("T", " ").replace("Z", "")
                )
                date_str = dt.strftime("%Y-%m-%d")
            except:
                date_str = str(date_order)[:10]
        else:
            date_str = "N/A"

        state_label = state_labels.get(order_state, order_state)

        print(f"\n[{i}] {order_name} - {date_str}")
        print(f"    Status: {state_label} | Amount: HK$ {amount:.2f}")

    print("\n" + "=" * 70)


def get_price_quote(client: OdooClient, partner_id: int, product_spec: str):
    """Get customer-specific price quote

    PRICE CALCULATION based on order unit:
        - When ordering in METERS (m): uses list_price (e.g., 339.02 HK$/m)
        - When ordering in YARDS (y): uses secondary_uom_list_price (e.g., 310 HK$/yd)
    """
    try:
        spec = parse_product_spec(product_spec)
    except ValueError as e:
        print(f"\n[ERROR] Invalid product spec '{product_spec}': {e}")
        print(f"Expected format: code:qty (e.g., 227707:1m, 227707:3y)")
        return None

    product = client.search_product_by_code(spec["code"])
    if not product:
        print(f"\nProduct not found: {spec['code']}")
        return None

    product_id = product["id"]
    unit = spec.get("unit", "y")  # 'm' for meters, 'y' for yards
    qty = spec["qty"]  # Original numeric value

    # Select price and UOM based on unit
    uom_id = None
    if unit == "m":
        # Meter price
        list_price = product.get("list_price", 0)
        price_label = "/m"
        qty_for_odoo = qty  # Keep in meters
        uom_id = get_uom_id(client, "M")
        display_meters = qty_for_odoo
        display_yards = qty_for_odoo / 0.9144
    else:
        # Yard price
        list_price = product.get(
            "secondary_uom_list_price", product.get("list_price", 0)
        )
        price_label = "/yd"
        qty_for_odoo = qty  # Keep in yards
        uom_id = get_uom_id(client, "Y")  # Use Yard UOM explicitly
        display_yards = qty_for_odoo
        display_meters = qty_for_odoo * 0.9144

    temp_order_id = client.create_order(partner_id, note="Temporary Quote Order")

    line_id = client.add_order_line(
        temp_order_id, product_id, qty_for_odoo, list_price, uom_id=uom_id
    )

    line = client._call(
        model="sale.order.line",
        method="read",
        args=[[line_id]],
        kwargs={
            "fields": [
                "product_id",
                "product_uom_qty",
                "price_unit",
                "discount",
                "price_subtotal",
            ]
        },
    )

    if line:
        l = line[0]
        list_price_unit = l.get("price_unit", 0)  # Original unit price
        actual_discount = l.get("discount", 0)
        subtotal = l.get("price_subtotal", 0)

        # Calculate discounted price per unit
        actual_price = list_price_unit * (1 - actual_discount / 100)

        # Also show the other price for reference
        if unit == "m":
            yard_price = product.get("secondary_uom_list_price")
            other_price_label = (
                f"Yard Price: HK$ {yard_price:.2f}/yd" if yard_price else ""
            )
        else:
            meter_price = product.get("list_price")
            other_price_label = (
                f"Meter Price: HK$ {meter_price:.2f}/m" if meter_price else ""
            )

        print("\n" + "=" * 60)
        print("Customer Price Quote")
        print("=" * 60)
        print(f"\nCustomer ID: {partner_id}")
        print(f"Product: {spec['code']} - {product.get('name')}")
        print(f"Quantity: {display_meters:.2f}m ({display_yards:.4f}yd)")
        print(f"Order Unit: {'Meters' if unit == 'm' else 'Yards'}")
        print("-" * 60)

        # Always show discount section, making it very prominent
        has_discount = actual_discount > 0

        if has_discount:
            print(f"📊 PRICE BREAKDOWN (with discount applied):")
            print(f"   List Price:      HK$ {list_price:.2f}{price_label}")
            print(f"   Your Price:      HK$ {actual_price:.2f}{price_label}")
            print(f"   💰 Your Discount: {actual_discount}% OFF")
            print(f"   → You save:      HK$ {list_price * qty_for_odoo - subtotal:.2f}")
        else:
            print(f"📊 PRICE BREAKDOWN:")
            print(f"   List Price:      HK$ {list_price:.2f}{price_label}")
            print(f"   Your Price:      HK$ {actual_price:.2f}{price_label} (no discount)")

        if other_price_label:
            print(f"   ({other_price_label} for reference)")

        print("-" * 60)
        print(f"💵 Subtotal: HK$ {subtotal:.2f}")
        if has_discount:
            print(f"   (includes {actual_discount}% discount)")
        print("=" * 60)

        try:
            client._call(model="sale.order", method="unlink", args=[[temp_order_id]])
        except:
            pass

        return {
            "price": actual_price,
            "discount": actual_discount,
            "subtotal": subtotal,
            "order_id": temp_order_id,
            "unit": unit,
            "price_label": price_label,
        }

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Odoo Order Management Tool - Flexible combination, discrete operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Environment options
    parser.add_argument(
        "--prod", "-p", action="store_true", help="Use production environment"
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Search customer
    search_parser = subparsers.add_parser("search", help="Search for customers")
    search_parser.add_argument("keyword", help="Customer name keyword")
    search_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Interactive customer selection",
    )

    # Get quote
    quote_parser = subparsers.add_parser(
        "quote", help="Get customer-specific price quote"
    )
    quote_parser.add_argument(
        "partner_id",
        nargs="?",
        default=None,
        help="Customer ID or name keyword (optional, searches if not specified)",
    )
    quote_parser.add_argument("product_spec", help="Product spec (format: code:qty_m)")

    # List addresses
    addresses_parser = subparsers.add_parser(
        "addresses", help="List customer shipping addresses"
    )
    addresses_parser.add_argument(
        "partner_id",
        nargs="?",
        default=None,
        help="Customer ID or name keyword (optional, searches if not specified)",
    )
    addresses_parser.add_argument(
        "--stats", action="store_true", help="Show historical usage stats"
    )

    # Create order
    create_parser = subparsers.add_parser("create", help="Create order")
    create_parser.add_argument(
        "partner_id",
        nargs="?",
        default=None,
        help="Customer ID or name keyword (optional, searches if not specified)",
    )
    create_parser.add_argument("--shipping", type=int, help="Delivery address ID")
    create_parser.add_argument("--invoice", type=int, help="Invoice address ID")
    create_parser.add_argument("--note", type=str, default="", help="Order note")

    # Add order line
    add_parser = subparsers.add_parser("add", help="Add order line")
    add_parser.add_argument("order_id", type=int, help="Order ID")
    add_parser.add_argument(
        "product_spec",
        help="Product spec (format: code:qty_m or code:qty_m:price:discount)",
    )
    add_parser.add_argument("--price", type=float, help="Override unit price")
    add_parser.add_argument(
        "--discount", type=float, help="Override discount percentage"
    )
    add_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompts (for AI/non-interactive use)"
    )
    add_parser.add_argument(
        "--customer-po", type=str, help="Customer PO Number"
    )

    # Update order line
    update_parser = subparsers.add_parser("update", help="Update order line")
    update_parser.add_argument("order_id", type=int, help="Order ID")
    update_parser.add_argument(
        "product_code",
        help="Product code (leave empty to update first line)",
        nargs="?",
    )
    update_parser.add_argument("--price", type=float, help="New unit price")
    update_parser.add_argument("--discount", type=float, help="New discount percentage")

    # Verify order
    verify_parser = subparsers.add_parser("verify", help="Verify order")
    verify_parser.add_argument("order_id", type=int, help="Order ID")

    # Confirm order
    confirm_parser = subparsers.add_parser(
        "confirm", help="Confirm order (with address confirmation)"
    )
    confirm_parser.add_argument("order_id", type=int, help="Order ID")
    confirm_parser.add_argument(
        "--no-address", action="store_true", help="Skip address confirmation"
    )
    confirm_parser.add_argument(
        "--auto-select",
        action="store_true",
        help="Auto-select first address when multiple exist",
    )
    confirm_parser.add_argument(
        "--address", type=int, metavar="ID", help="Specific address ID to use"
    )
    confirm_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompts (for AI/non-interactive use)"
    )

    # Cancel order
    cancel_parser = subparsers.add_parser("cancel-order", help="Cancel order")
    cancel_parser.add_argument("order_id", type=int, help="Order ID")

    # Remove order line
    remove_line_parser = subparsers.add_parser("remove-line", help="Remove order line")
    remove_line_parser.add_argument("order_id", type=int, help="Order ID")
    remove_line_parser.add_argument(
        "product_code",
        help="Product code (optional, interactive if not specified)",
        nargs="?",
    )

    # Cancel multiple order lines
    cancel_lines_parser = subparsers.add_parser(
        "cancel-lines", help="Cancel specific order lines (partial cancellation)"
    )
    cancel_lines_parser.add_argument(
        "order_id", type=int, help="Order ID"
    )
    cancel_lines_parser.add_argument(
        "line_ids",
        help="Comma-separated order line IDs to cancel (e.g., 227701,227707)",
    )

    # Update order line quantity
    update_qty_parser = subparsers.add_parser(
        "update-line-qty", help="Update order line quantity"
    )
    update_qty_parser.add_argument("line_id", type=int, help="Order line ID")
    update_qty_parser.add_argument("qty", type=float, help="New quantity")

    # Add new address
    add_addr_parser = subparsers.add_parser(
        "add-address", help="Add new address for customer"
    )
    add_addr_parser.add_argument(
        "partner_id",
        nargs="?",
        type=int,
        help="Customer ID (optional, searches if not specified)",
    )
    add_addr_parser.add_argument("name", help="Address name")
    add_addr_parser.add_argument("street", help="Street address")
    add_addr_parser.add_argument("--street2", help="Street address (line 2)")
    add_addr_parser.add_argument("--city", help="City")
    add_addr_parser.add_argument("--zip", help="Zip code")
    add_addr_parser.add_argument(
        "--type", help="Address type (delivery/invoice/other)", default="delivery"
    )

    # Set customer PO for order line
    set_customer_po_parser = subparsers.add_parser(
        "set-customer-po", help="Set customer PO for order line"
    )
    set_customer_po_parser.add_argument("line_id", type=int, help="Order line ID")
    set_customer_po_parser.add_argument("customer_po", help="Customer PO Number")

    # Set order address
    address_parser = subparsers.add_parser("set-address", help="Set order address")
    address_parser.add_argument("order_id", type=int, help="Order ID")
    address_parser.add_argument("--shipping", type=int, help="Shipping address ID")
    address_parser.add_argument("--invoice", type=int, help="Invoice address ID")

    # Set PO number
    po_parser = subparsers.add_parser("set-po", help="Set customer PO number (PO#)")
    po_parser.add_argument("order_id", type=int, help="Order ID")
    po_parser.add_argument("po_number", help="Customer PO number")
    po_parser.add_argument(
        "product_code",
        help="Product code (optional, sets PO for entire order if not specified)",
        nargs="?",
    )

    # Set payment terms
    payment_parser = subparsers.add_parser("set-payment", help="Set payment terms")
    payment_parser.add_argument("order_id", type=int, help="Order ID")
    payment_parser.add_argument("--term-id", type=int, help="Payment term ID")

    # Order detail
    detail_parser = subparsers.add_parser("detail", help="Show order details")
    detail_parser.add_argument("order_id", type=int, help="Order ID")

    # List orders
    list_parser = subparsers.add_parser("list", help="List customer orders")
    list_parser.add_argument(
        "partner_id",
        help="Customer ID or name keyword (optional, searches if not specified)",
    )
    list_parser.add_argument(
        "--days", type=int, default=90, help="Time range in days (default: 90)"
    )
    list_parser.add_argument(
        "--state", help="Filter by state (draft/sent/sale/done/cancel)"
    )
    list_parser.add_argument(
        "--limit", type=int, default=20, help="Maximum results (default: 20)"
    )

    # Report link
    report_parser = subparsers.add_parser("report", help="Get order confirmation link")
    report_parser.add_argument("order_id", type=int, help="Order ID")

    # Address detail
    address_detail_parser = subparsers.add_parser(
        "address-detail", help="Get full address details by ID"
    )
    address_detail_parser.add_argument("address_id", type=int, help="Address ID")

    # Context related
    subparsers.add_parser("context", help="Show current context summary")
    subparsers.add_parser("clear-context", help="Clear conversation context")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        # Initialize memory manager
        if USE_AGNO:
            memory = AgnoMemoryManager(user_id="odoo_order")
            print("[OK] Using Agno Memory Manager")
        else:
            memory = ConversationContext()
            print("[OK] Using fallback memory manager")

        # Handle context commands
        if args.command == "context":
            print("\n" + memory.get_context_summary())
            return 0

        if args.command == "clear-context":
            memory.clear()
            print("\nConversation context cleared")
            return 0

        client = OdooClient(verify_ssl=False)
        print("Odoo connection successful!\n")

        # Execute commands
        if args.command == "search":
            interactive = getattr(args, "interactive", False)
            search_customer(client, memory, args.keyword, interactive=interactive)

        elif args.command == "addresses":
            # List customer addresses
            partner_id = resolve_partner_id(client, memory, args.partner_id)
            if partner_id is None:
                return 1
            with_stats = getattr(args, "stats", False)
            list_addresses(client, partner_id, with_stats=with_stats)

        elif args.command == "quote":
            # Get customer price quote
            partner_id = resolve_partner_id(client, memory, args.partner_id)
            if partner_id is None:
                return 1
            get_price_quote(client, partner_id, args.product_spec)

        elif args.command == "create":
            # Create order
            partner_id = resolve_partner_id(client, memory, args.partner_id)
            if partner_id is None:
                return 1
            shipping_id = getattr(args, "shipping", None)
            invoice_id = getattr(args, "invoice", None)
            note = getattr(args, "note", "")
            create_order(
                client,
                memory,
                partner_id,
                note=note,
                shipping_id=shipping_id,
                invoice_id=invoice_id,
            )

        elif args.command == "add":
            skip_confirmation = getattr(args, "yes", False)
            customer_po = getattr(args, "customer_po", None)
            # Resolve order_ref to order_id (support both name like "26030081" and numeric ID)
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            add_order_line(
                client,
                memory,
                order_id,
                args.product_spec,
                args.price,
                args.discount,
                skip_confirmation=skip_confirmation,
                customer_po=customer_po,
            )

        elif args.command == "update":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            update_order_line(
                client, order_id, args.product_code, args.price, args.discount
            )

        elif args.command == "verify":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            verify_order(client, order_id)

        elif args.command == "confirm":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            require_address = not getattr(args, "no_address", False)
            auto_select_address = getattr(args, "auto_select", False)
            selected_address_id = getattr(args, "address", None)
            skip_confirmation = getattr(args, "yes", False)
            confirm_order(
                client,
                order_id,
                require_address=require_address,
                auto_select_address=auto_select_address,
                selected_address_id=selected_address_id,
                skip_confirmation=skip_confirmation,
            )

        elif args.command == "cancel-order":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            cancel_order(client, order_id)

        elif args.command == "remove-line":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            product_code = getattr(args, "product_code", None)
            remove_order_line_cli(client, order_id, product_code)

        elif args.command == "cancel-lines":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            cancel_order_lines(client, order_id, args.line_ids)

        elif args.command == "update-line-qty":
            update_order_line_qty(client, args.line_id, args.qty)

        elif args.command == "set-customer-po":
            set_order_line_customer_po(client, args.line_id, args.customer_po)

        elif args.command == "add-address":
            # Add new address for customer
            partner_id = resolve_partner_id(client, memory, args.partner_id)
            if partner_id is None:
                return 1
            street2 = getattr(args, "street2", None)
            city = getattr(args, "city", None)
            zip_code = getattr(args, "zip", None)
            address_type = getattr(args, "type", "delivery")
            add_address(
                client,
                partner_id,
                args.name,
                args.street,
                street2,
                city,
                zip_code,
                address_type,
            )

        elif args.command == "set-address":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            shipping_id = getattr(args, "shipping", None)
            invoice_id = getattr(args, "invoice", None)
            set_order_addresses(client, order_id, shipping_id, invoice_id)

        elif args.command == "set-po":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            product_code = getattr(args, "product_code", None)
            set_order_po(client, order_id, args.po_number, product_code)

        elif args.command == "set-payment":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            term_id = getattr(args, "term-id", None)
            set_order_payment(client, order_id, term_id)

        elif args.command == "list":
            # List customer orders
            partner_id = resolve_partner_id(client, memory, args.partner_id)
            if partner_id is None:
                return 1
            days = getattr(args, "days", 90)
            state = getattr(args, "state", None)
            limit = getattr(args, "limit", 20)
            list_customer_orders(
                client, partner_id, days=days, state=state, limit=limit
            )

        elif args.command == "detail":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            show_order_detail(client, order_id)

        elif args.command == "report":
            order_id = resolve_order_id(client, args.order_id)
            if order_id is None:
                print(f"\n[ERROR] Order '{args.order_id}' not found")
                return 1
            print_order_report(client, order_id)

        elif args.command == "address-detail":
            get_address_detail(client, args.address_id)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
