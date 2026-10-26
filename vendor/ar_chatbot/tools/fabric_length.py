"""布料長度單位換算工具

支援香港/廣東布料貿易常用口語叫法（角/骨/半）與碼、米之間的互換。

口訣：
- 角 = 0.1 碼（十分之一碼）
- 角半 = 0.05 碼
- 骨（個骨）= 0.25 碼（四分之一碼）
- 半（碼半）= 0.5 碼

範例：
    >>> parse_fabric_length("1碼3角半")
    1.35
    >>> parse_fabric_length("碼半")
    1.5
    >>> to_meters("1碼1個骨")
    1.143
"""
from __future__ import annotations

import re
from dataclasses import dataclass

YARD_TO_METER = 0.9144

# 八分之一系統（精細裁剪用）
_EIGHTH_FRACTIONS = {
    "1/8": 0.125,
    "3/8": 0.375,
    "5/8": 0.625,
    "7/8": 0.875,
    "1/4": 0.25,
    "3/4": 0.75,
    "1/2": 0.5,
}

# 中文數字 → 阿拉伯數字（支援 0-10）
_CN_DIGITS = {
    "零": 0, "〇": 0, "0": 0,
    "一": 1, "1": 1, "壹": 1,
    "二": 2, "兩": 2, "2": 2, "貳": 2,
    "三": 3, "3": 3, "參": 3,
    "四": 4, "4": 4, "肆": 4,
    "五": 5, "5": 5, "伍": 5,
    "六": 6, "6": 6, "陸": 6,
    "七": 7, "7": 7, "柒": 7,
    "八": 8, "8": 8, "捌": 8,
    "九": 9, "9": 9, "玖": 9,
    "十": 10, "10": 10, "拾": 10,
}


class FabricLengthParseError(ValueError):
    """無法辨識布料長度表達式時拋出。"""


@dataclass(frozen=True)
class FabricLength:
    """布料長度（碼為內部單位）。"""
    yards: float

    @property
    def meters(self) -> float:
        return round(self.yards * YARD_TO_METER, 4)

    def __str__(self) -> str:
        return f"{self.yards:g}yd ({self.meters:g}m)"


def _normalize(text: str) -> str:
    text = text.strip()
    # 統一全形空格與逗號；保留半形空格以區分「1 1/4」(=1.25) 與「11/4」(=2.75)
    text = text.replace("　", " ").replace("，", " ")
    return re.sub(r"\s+", " ", text)


def _parse_fraction(token: str) -> float | None:
    """解析 '1/4' '3/8' 之類分數。回傳 None 表示格式不符或分母為零。"""
    m = re.fullmatch(r"(\d+)/(\d+)", token)
    if not m:
        return None
    denom = int(m.group(2))
    if denom == 0:
        return None
    return int(m.group(1)) / denom


def _parse_cn_int(token: str) -> int:
    """解析中文/阿拉伯數字整數（0-10）。"""
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if token.isdigit():
        return int(token)
    raise FabricLengthParseError(f"無法辨識數字：{token}")


def parse_fabric_length(text: str) -> float:
    """將口語化布料長度解析為碼數（float）。

    支援格式：
        - "1碼3角半" → 1.35
        - "碼半" → 0.5
        - "1碼1個骨" → 1.25
        - "1 1/4" / "1碼1/4" → 1.25
        - "1.35" → 1.35（純小數直通）
        - "1碼8" → 1.8

    Args:
        text: 客戶輸入的布料長度表達式。

    Returns:
        碼數（yard, float）。

    Raises:
        FabricLengthParseError: 無法辨識時。
    """
    raw = text.strip()
    if not raw:
        raise FabricLengthParseError("輸入為空")

    normalized = _normalize(raw)

    # 1) 純小數/整數直通（"1.35" / "2"）
    if re.fullmatch(r"\d+(\.\d+)?", normalized):
        return float(normalized)

    # 2) 純分數（"1/4"）
    if (frac := _parse_fraction(normalized)) is not None:
        return frac

    yards = 0.0

    # 3) 拆出「碼」前面的整數部分；碼必須緊跟在整數後（或單獨出現）
    m = re.match(r"^(?P<yards>\d+|[一二兩三四五六七八九十])?(?P<ma>碼)?(?P<rest>.*)$", normalized)
    if not m:
        raise FabricLengthParseError(f"無法辨識：{raw}")

    if m.group("yards"):
        yards += _parse_cn_int(m.group("yards"))
    elif m.group("ma"):
        # 「碼半」「碼1個骨」這類「碼」前省略整數，預設為 1 碼
        yards += 1
    rest = m.group("rest").strip()

    # 4) 處理剩餘部分：分數 / 角N / 角半 / N個骨 / 半 / 半碼前綴
    if rest:
        yards += _parse_remainder(rest)

    return yards


def _parse_remainder(rest: str) -> float:
    """解析碼數後的小數部分；亦支援「半碼」這類前綴寫法。"""
    # 「半碼」「半碼X角」這類：半在前
    if rest.startswith("半"):
        tail = rest[1:].replace("碼", "").strip()
        total = 0.5
        if tail:
            total += _parse_remainder(tail)
        return total

    # 純分數
    if (frac := _parse_fraction(rest)) is not None:
        return frac

    total = 0.0

    # 「半」單獨出現 = 0.5（如「碼半」）
    if rest == "半":
        return 0.5

    # 「N個骨」= N * 0.25
    m = re.match(r"^(?P<n>\d+|[一二三四五六七八九十])個?骨(?P<tail>半)?$", rest)
    if m:
        n = _parse_cn_int(m.group("n"))
        total += n * 0.25
        if m.group("tail"):
            total += 0.125  # 「骨半」= 八分之一碼
        return total

    # 「N角半?」= N * 0.1 [+ 0.05]
    m = re.match(r"^(?P<n>\d+|[零一二兩三四五六七八九十])?角(?P<half>半)?$", rest)
    if m:
        n = _parse_cn_int(m.group("n")) if m.group("n") else 0
        total += n * 0.1
        if m.group("half"):
            total += 0.05
        return total

    # 純數字（如「1碼8」中的「8」= 8角 = 0.8）
    if re.fullmatch(r"\d+", rest):
        return int(rest) * 0.1

    raise FabricLengthParseError(f"無法辨識剩餘部分：{rest}")


def to_meters(text: str, *, rounded: int = 3) -> float:
    """便利函數：直接把口語長度轉成米。"""
    yards = parse_fabric_length(text)
    return round(yards * YARD_TO_METER, rounded)


def to_yards(text: str) -> float:
    """便利函數：確保輸出為碼數（解析失敗會拋錯）。"""
    return parse_fabric_length(text)


# 對照表（可用於 UI 顯示或反查）
COLLOQUIAL_TABLE: list[tuple[str, float, str]] = [
    ("1碼0角半", 1.05, "1.05yd"),
    ("1碼1角", 1.10, "1.10yd"),
    ("1碼1角半", 1.15, "1.15yd"),
    ("1碼2角", 1.20, "1.20yd"),
    ("1碼1個骨", 1.25, "1.25yd"),
    ("1碼3角", 1.30, "1.30yd"),
    ("1碼3角半", 1.35, "1.35yd"),
    ("1碼4角", 1.40, "1.40yd"),
    ("1碼4角半", 1.45, "1.45yd"),
    ("碼半", 1.50, "1.50yd"),
    ("1碼5角半", 1.55, "1.55yd"),
    ("1碼6角", 1.60, "1.60yd"),
    ("1碼6角半", 1.65, "1.65yd"),
    ("1碼3個骨", 1.75, "1.75yd"),
    ("1碼8角", 1.80, "1.80yd"),
    ("1碼8角半", 1.85, "1.85yd"),
    ("1碼9角", 1.90, "1.90yd"),
    ("1碼9角半", 1.95, "1.95yd"),
]


__all__ = [
    "FabricLength",
    "FabricLengthParseError",
    "YARD_TO_METER",
    "COLLOQUIAL_TABLE",
    "parse_fabric_length",
    "to_meters",
    "to_yards",
]
