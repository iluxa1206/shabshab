"""Pillow-рендер стакана в PNG для Telegram-оповещений. Рисует из уже
посчитанных уровней (те же _ob_levels, что у монитора алертов): asks сверху
(лучший внизу), спред-строка, bids снизу. Флоатер: Y-IDX первичная + DM;
фикс: YTM + G-спред. Сработавший уровень подсвечен.

Шрифты: DejaVu (в проде ставится fonts-dejavu-core, см. Dockerfile), на маке —
системная Helvetica; фолбэк — дефолт Pillow. CPU-bound — звать через
asyncio.to_thread."""
import io
import logging
from datetime import datetime
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_W = 760
_ROW = 34
_HDR = 78
_GAP = 40
_PAD = 16

_BG = (14, 20, 32)
_HDR_BG = (20, 28, 44)
_TEXT = (222, 228, 238)
_DIM = (128, 140, 158)
_ASK = (229, 72, 77)
_BID = (70, 167, 88)
_ASK_BAR = (229, 72, 77, 46)
_BID_BAR = (70, 167, 88, 46)
_HIT_BG = (250, 204, 21, 38)
_HIT_EDGE = (250, 204, 21)
_LINE = (34, 44, 62)

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = _FONT_PATHS if not bold else [_FONT_PATHS[1], _FONT_PATHS[0]] + _FONT_PATHS[2:]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:       # Pillow < 10.1
        return ImageFont.load_default()


def _fmt(v: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}".replace(",", " ")


def _fmt_qty(q: Optional[float]) -> str:
    if q is None:
        return "—"
    q = float(q)
    if q >= 1_000_000:
        return f"{q / 1_000_000:.1f}М"
    if q >= 10_000:
        return f"{q / 1000:.0f}К"
    return f"{q:,.0f}".replace(",", " ")


def render_orderbook(*, isin: str, name: Optional[str], kind: str,
                     bids: List[dict], asks: List[dict],
                     hit_price: Optional[float] = None,
                     hit_side: Optional[str] = None,
                     title: str = "", ts: Optional[datetime] = None,
                     depth: int = 10) -> bytes:
    """→ PNG. bids/asks — [{price, qty, y_idx_bps, dm_bps, yield_pct,
    g_spread_bps}] в порядке от лучшего (bids убыв., asks возр.)."""
    asks = [lv for lv in asks[:depth]][::-1]     # худший сверху, лучший к центру
    bids = bids[:depth]
    n_rows = len(asks) + len(bids)
    h = _HDR + n_rows * _ROW + _GAP + _PAD
    img = Image.new("RGB", (_W, h), _BG)
    d = ImageDraw.Draw(img, "RGBA")

    f_big = _font(21, bold=True)
    f_med = _font(16)
    f_sm = _font(13)

    # шапка
    d.rectangle([0, 0, _W, _HDR - 8], fill=_HDR_BG)
    head = f"{name or isin}"
    d.text((_PAD, 12), head, font=f_big, fill=_TEXT)
    sub = isin + ("  ·  фикс" if kind == "fixed" else "")
    ts_str = (ts or datetime.now()).strftime("%d.%m.%Y %H:%M МСК")
    d.text((_PAD, 44), sub, font=f_sm, fill=_DIM)
    d.text((_W - _PAD - d.textlength(ts_str, font=f_sm), 44), ts_str, font=f_sm, fill=_DIM)
    if title:
        d.text((_W - _PAD - d.textlength(title, font=f_sm), 14), title, font=f_sm, fill=_HIT_EDGE)

    # колонки: [метрика1, метрика2, цена, объём] + бар объёма справа
    if kind == "fixed":
        cols = [("YTM %", lambda lv: _fmt(lv.get("yield_pct"))),
                ("G-спр", lambda lv: _fmt(lv.get("g_spread_bps"), 0))]
    else:
        cols = [("R-spread", lambda lv: _fmt(lv.get("y_idx_bps"), 0)),
                ("DM", lambda lv: _fmt(lv.get("dm_bps"), 0))]
    x_m1, x_m2, x_px, x_q, x_bar = _PAD, 120, 250, 400, 500
    bar_w = _W - x_bar - _PAD

    y = _HDR
    d.text((x_m1, y - 20), cols[0][0], font=f_sm, fill=_DIM)
    d.text((x_m2, y - 20), cols[1][0], font=f_sm, fill=_DIM)
    d.text((x_px, y - 20), "Цена", font=f_sm, fill=_DIM)
    d.text((x_q, y - 20), "Объём", font=f_sm, fill=_DIM)

    max_q = max([float(lv.get("qty") or 0) for lv in asks + bids] or [1.0]) or 1.0

    def _row(lv: dict, side: str, y: int):
        is_hit = (hit_price is not None and lv.get("price") == hit_price
                  and (hit_side is None or side == hit_side))
        color = _ASK if side == "sell" else _BID
        bar_fill = _ASK_BAR if side == "sell" else _BID_BAR
        if is_hit:
            d.rectangle([0, y, _W, y + _ROW], fill=_HIT_BG)
            d.rectangle([0, y, 3, y + _ROW], fill=_HIT_EDGE)
        q = float(lv.get("qty") or 0)
        w = int(bar_w * min(1.0, q / max_q))
        d.rectangle([x_bar, y + 7, x_bar + max(w, 2), y + _ROW - 7], fill=bar_fill)
        cy = y + (_ROW - 16) // 2
        d.text((x_m1, cy), cols[0][1](lv), font=f_med, fill=_TEXT)
        d.text((x_m2, cy), cols[1][1](lv), font=f_med, fill=_DIM)
        d.text((x_px, cy), _fmt(lv.get("price"), 2), font=f_med, fill=color)
        d.text((x_q, cy), _fmt_qty(lv.get("qty")), font=f_med, fill=_TEXT)

    for lv in asks:
        _row(lv, "sell", y)
        y += _ROW
    # спред-строка
    best_ask = asks[-1]["price"] if asks else None
    best_bid = bids[0]["price"] if bids else None
    d.line([_PAD, y + _GAP // 2, _W - _PAD, y + _GAP // 2], fill=_LINE, width=1)
    if best_ask is not None and best_bid is not None:
        spread = best_ask - best_bid
        mid = f"спред {spread:.2f} пп"
        tw = d.textlength(mid, font=f_sm)
        d.rectangle([(_W - tw) / 2 - 10, y + _GAP // 2 - 10,
                     (_W + tw) / 2 + 10, y + _GAP // 2 + 10], fill=_BG)
        d.text(((_W - tw) / 2, y + _GAP // 2 - 8), mid, font=f_sm, fill=_DIM)
    y += _GAP
    for lv in bids:
        _row(lv, "buy", y)
        y += _ROW

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
