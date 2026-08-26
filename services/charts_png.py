"""Рендер картинок для Telegram: свои графики в PNG, без браузера.

Дашборд рисует SVG в React, но альбом «разбора дня» уходит в чат картинками, и
гонять ради них headless-браузер (или тащить matplotlib с его памятью — на
проде уже ловили OOM на 768 МиБ) незачем: нужны четыре простых сюжета —
диверг-бары, бары, линии, столбики по дням. Всё это Pillow рисует примитивами,
занимая единицы мегабайт.

Рисуем в SS раз крупнее и уменьшаем LANCZOS: у PIL нет сглаживания линий, и
диагонали кривой без суперсэмплинга выглядят лесенкой.

Палитра — тёмная, как у десктопной витрины: картинка приходит в чат, который
почти у всех тёмный, и белый лист бьёт по глазам."""
from __future__ import annotations

import io
import logging
import os
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

SS = 2                       # суперсэмплинг
W, H = 1000, 620             # итоговый размер картинки, px

BG = (15, 17, 21)
PANEL = (21, 24, 30)
GRID = (38, 43, 51)
TEXT = (216, 222, 233)
MUTED = (138, 148, 163)
UP = (53, 192, 127)          # сужение спреда / приток — зелёный
DOWN = (226, 86, 77)         # расширение — красный
ACCENT = (90, 169, 230)
ACCENT2 = (163, 132, 232)

# Шрифты ищем по списку: в контейнере DejaVu (fonts-dejavu-core в Dockerfile),
# на маке — системные. Оба с кириллицей; без неё подписи превратятся в квадраты.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_font_cache: dict = {}
# Какой файл в итоге взяли: у Arial с мака нет знака рубля, и «₽» рисуется
# квадратом-тофу. Подпись «руб» уродливее символа, но читается.
_font_path: Optional[str] = None


def _font(size: int, bold: bool = False):
    key = (size, bold)
    f = _font_cache.get(key)
    if f is not None:
        return f
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size * SS)
                globals()["_font_path"] = path
                break
            except Exception:
                continue
    if f is None:
        # Встроенный битмап — некрасиво и без кириллицы, но лучше, чем падение
        # воркера из-за отсутствующего пакета шрифтов.
        logger.warning("charts_png: TTF не найден, беру встроенный шрифт")
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def fonts_ok() -> bool:
    """Есть ли настоящий TTF: без него картинки слать стыдно (см. tg_digest)."""
    return any(os.path.exists(p) for p in _FONT_CANDIDATES)


class Canvas:
    """Холст с шапкой и полем графика. Координаты — в ИТОГОВЫХ пикселях,
    умножение на SS живёт внутри, чтобы верстка сюжетов читалась как обычная."""

    def __init__(self, title: str, subtitle: str = "", top: int = 96):
        self.img = Image.new("RGB", (W * SS, H * SS), BG)
        self.d = ImageDraw.Draw(self.img)
        self.top = top
        self.d.text((32 * SS, 34 * SS), title, font=_font(26, True),
                    fill=TEXT, anchor="lm")
        if subtitle:
            self.d.text((32 * SS, 66 * SS), subtitle, font=_font(15),
                        fill=MUTED, anchor="lm")

    # --- примитивы ---
    def line(self, xy: Sequence[Tuple[float, float]], color, width: int = 2):
        self.d.line([(x * SS, y * SS) for x, y in xy], fill=color,
                    width=width * SS, joint="curve")

    def rect(self, box: Tuple[float, float, float, float], color, radius: int = 0):
        b = [box[0] * SS, box[1] * SS, box[2] * SS, box[3] * SS]
        if b[2] - b[0] < 1:
            b[2] = b[0] + 1
        if b[3] - b[1] < 1:
            b[3] = b[1] + 1
        if radius:
            self.d.rounded_rectangle(b, radius=radius * SS, fill=color)
        else:
            self.d.rectangle(b, fill=color)

    def text(self, xy: Tuple[float, float], s: str, size: int = 15,
             color=TEXT, bold: bool = False, anchor: str = "lm"):
        self.d.text((xy[0] * SS, xy[1] * SS), s, font=_font(size, bold),
                    fill=color, anchor=anchor)

    def dot(self, xy: Tuple[float, float], r: float, color):
        x, y = xy[0] * SS, xy[1] * SS
        self.d.ellipse([x - r * SS, y - r * SS, x + r * SS, y + r * SS], fill=color)

    def png(self) -> bytes:
        out = self.img.resize((W, H), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _fit(s: str, size: int, max_px: float, bold: bool = False) -> str:
    """Подпись, урезанная под ширину колонки: имена выпусков длинные, и без
    обрезки они лезут на сам график."""
    f = _font(size, bold)
    if f.getlength(s) <= max_px * SS:
        return s
    while s and f.getlength(s + "…") > max_px * SS:
        s = s[:-1]
    return s + "…"


def rub() -> str:
    """Знак рубля, если шрифт его знает (DejaVu — да, системный Arial — нет)."""
    _font(12)                                  # прогреть выбор файла
    return "₽" if (_font_path or "").lower().find("dejavu") >= 0 else "руб"


def _money(v: float) -> str:
    a = abs(v)
    if a < 1:
        return "0"
    if a >= 1e9:
        return f"{v / 1e9:.1f} млрд".replace(".", ",")
    if a >= 1e6:
        return f"{v / 1e6:.0f} млн"
    return f"{v / 1e3:.0f} тыс"


# ── сюжет 1: движения спреда ───────────────────────────────────────────────
def movers(rows: List[dict], title: str, subtitle: str = "") -> bytes:
    """Диверг-бары Δ Y-IDX: расширение вправо красным, сужение влево зелёным.

    Ноль по центру, а не слева: за день важно не «на сколько уехали» вообще, а
    в какую сторону — и разложенные по обе стороны от оси бумаги читаются одним
    взглядом, без чтения знаков.

    rows: [{name, delta_bps, y_bps}] — уже отсортированные, как рисовать."""
    c = Canvas(title, subtitle)
    if not rows:
        c.text((32, H / 2), "нет данных за день", 18, MUTED)
        return c.png()
    left, right, top, bottom = 190, W - 90, c.top, H - 40
    mid = (left + right) / 2
    span = max(abs(r["delta_bps"]) for r in rows) or 1.0
    step = (bottom - top) / len(rows)
    bar_h = min(24.0, step * 0.62)
    c.line([(mid, top - 8), (mid, bottom + 6)], GRID, 1)
    for i, r in enumerate(rows):
        y = top + step * i + step / 2
        d = float(r["delta_bps"])
        w = abs(d) / span * (mid - left - 12)
        color = DOWN if d > 0 else UP
        x0, x1 = (mid, mid + w) if d > 0 else (mid - w, mid)
        c.rect((x0, y - bar_h / 2, x1, y + bar_h / 2), color, radius=3)
        c.text((left - 12, y), _fit(str(r["name"]), 15, 150), 15, TEXT, anchor="rm")
        # значение — со стороны бара, чтобы взгляд не возвращался к центру
        label = f"{d:+.0f}"
        if r.get("y_bps") is not None:
            label += f"  ({r['y_bps']:.0f})"
        c.text((x1 + 10 if d > 0 else x0 - 10, y), label, 14, MUTED,
               anchor="lm" if d > 0 else "rm")
    return c.png()


# ── сюжет 2: обороты ───────────────────────────────────────────────────────
def turnover(rows: List[dict], title: str, subtitle: str = "") -> bytes:
    """Горизонтальные бары оборота: длина — рубли, подпись справа — сумма.

    Сортировка приходит готовой; шкала от нуля, потому что оборот сравнивают
    кратно («вдвое больше»), а не по приращению."""
    c = Canvas(title, subtitle)
    if not rows:
        c.text((32, H / 2), "сделок не было", 18, MUTED)
        return c.png()
    # правое поле под хвост «943 млн ₽ · 656 сд»: он длиннее, чем кажется, и
    # у самого длинного бара уезжал за край картинки
    left, right, top, bottom = 190, W - 250, c.top, H - 40
    top_val = max(r["value"] for r in rows) or 1.0
    step = (bottom - top) / len(rows)
    bar_h = min(26.0, step * 0.62)
    for i, r in enumerate(rows):
        y = top + step * i + step / 2
        w = float(r["value"]) / top_val * (right - left)
        c.rect((left, y - bar_h / 2, left + w, y + bar_h / 2), ACCENT, radius=3)
        c.text((left - 12, y), _fit(str(r["name"]), 15, 150), 15, TEXT, anchor="rm")
        tail = f"{_money(float(r['value']))} {rub()}"
        if r.get("trades"):
            tail += f"  ·  {int(r['trades'])} сд"
        c.text((left + w + 10, y), tail, 14, MUTED, anchor="lm")
    return c.png()


# ── сюжет 3: кривая ────────────────────────────────────────────────────────
def curve(series: List[dict], labels: List[str], title: str,
          subtitle: str = "") -> bytes:
    """Ломаные по общим тенорам: сегодня против прошлого среза.

    Точки равномерно по оси X, а не по времени: сравнивают форму кривой, и
    сжатый левый край (ON…3M) прятал бы именно ту часть, где ставки живут.

    series: [{label, values:[float|None], color}] — длиной с labels."""
    c = Canvas(title, subtitle)
    pts = [s for s in series if any(v is not None for v in s["values"])]
    if not pts or len(labels) < 2:
        c.text((32, H / 2), "архив кривой ещё не покрывает эти даты", 18, MUTED)
        return c.png()
    left, right, top, bottom = 70, W - 40, c.top + 10, H - 60
    vals = [v for s in pts for v in s["values"] if v is not None]
    lo, hi = min(vals), max(vals)
    pad = max(0.05, (hi - lo) * 0.15)
    lo, hi = lo - pad, hi + pad

    def px(i: int) -> float:
        return left + (right - left) * i / (len(labels) - 1)

    def py(v: float) -> float:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    # сетка: пять уровней ставки — больше линий на четырёх точках кривой шумят
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = py(v)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 10, y), f"{v:.2f}".replace(".", ","), 13, MUTED, anchor="rm")
    for i, lab in enumerate(labels):
        c.text((px(i), bottom + 22), lab, 13, MUTED, anchor="mm")
    for s in pts:
        xy = [(px(i), py(v)) for i, v in enumerate(s["values"]) if v is not None]
        c.line(xy, s["color"], 3)
        for p in xy:
            c.dot(p, 3.5, s["color"])
    # легенда в правом верхнем углу поля — там кривая почти никогда не проходит
    for j, s in enumerate(pts):
        y = top + 6 + j * 22
        c.rect((right - 150, y - 3, right - 132, y + 3), s["color"], radius=2)
        c.text((right - 124, y), str(s["label"]), 14, TEXT)
    return c.png()


# ── сюжет 4: выплаты по дням ───────────────────────────────────────────────
def payments(days: List[dict], title: str, subtitle: str = "") -> bytes:
    """Столбики по дням: купоны снизу, погашения сверху одним столбцом.

    Стеком, а не рядом: вопрос календаря — «сколько денег придёт в этот день»,
    а разбивка уточняет, чем именно.

    days: [{label, coupon, redemption}]."""
    c = Canvas(title, subtitle)
    total = [d["coupon"] + d["redemption"] for d in days] if days else []
    if not days or not any(total):
        c.text((32, H / 2), "в ближайшие дни выплат нет", 18, MUTED)
        return c.png()
    left, right, top, bottom = 90, W - 30, c.top + 20, H - 64
    hi = max(total) or 1.0
    step = (right - left) / len(days)
    bar_w = min(46.0, step * 0.6)
    for k in range(4):
        v = hi * k / 3
        y = bottom - (v / hi) * (bottom - top)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 12, y), _money(v), 13, MUTED, anchor="rm")
    for i, d in enumerate(days):
        x = left + step * i + step / 2
        cpn, red = float(d["coupon"]), float(d["redemption"])
        h_cpn = cpn / hi * (bottom - top)
        h_red = red / hi * (bottom - top)
        if cpn:
            c.rect((x - bar_w / 2, bottom - h_cpn, x + bar_w / 2, bottom),
                   ACCENT, radius=3)
        if red:
            c.rect((x - bar_w / 2, bottom - h_cpn - h_red,
                    x + bar_w / 2, bottom - h_cpn), ACCENT2, radius=3)
        c.text((x, bottom + 20), str(d["label"]), 13, MUTED, anchor="mm")
        if cpn + red:
            c.text((x, bottom - h_cpn - h_red - 12), _money(cpn + red), 12,
                   TEXT, anchor="mm")
    c.rect((left, H - 26, left + 18, H - 20), ACCENT, radius=2)
    c.text((left + 26, H - 23), "купоны", 13, MUTED)
    c.rect((left + 110, H - 26, left + 128, H - 20), ACCENT2, radius=2)
    c.text((left + 136, H - 23), "погашения и амортизация", 13, MUTED)
    return c.png()
