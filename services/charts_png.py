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
# Линии поверх баров: цвет баров для них не годится — линия сливается со своим
# же столбиком. Тёплые тона на холодной заливке видно в обоих случаях.
LINE1 = (240, 196, 96)
LINE2 = (236, 130, 180)

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
def _diverg_panel(c: "Canvas", rows: List[dict], box, name_px: float = 150,
                  label_size: int = 14, fill: float = 0.92,
                  labels_right: bool = False) -> None:
    """Одна панель диверг-баров в заданной рамке (left, right, top, bottom).

    Вынесено из `movers`, потому что панелей стало две: флоатеры и фиксы
    считаются РАЗНЫМИ метриками (Y-IDX против g-спреда), и общий масштаб
    вместе с общим рейтингом смешивал бы два рынка в один список."""
    left, right, top, bottom = box
    mid = (left + right) / 2
    span = max(abs(r["delta_bps"]) for r in rows) or 1.0
    step = (bottom - top) / max(1, len(rows))
    bar_h = min(24.0, step * 0.62)
    c.line([(mid, top - 8), (mid, bottom + 6)], GRID, 1)
    for i, r in enumerate(rows):
        y = top + step * i + step / 2
        d = float(r["delta_bps"])
        # fill < 1 оставляет место подписи со стороны бара: в узкой колонке
        # парного сюжета полноразмерный бар упирался в имя выпуска
        w = abs(d) / span * (mid - left - 12) * fill
        color = DOWN if d > 0 else UP
        x0, x1 = (mid, mid + w) if d > 0 else (mid - w, mid)
        c.rect((x0, y - bar_h / 2, x1, y + bar_h / 2), color, radius=3)
        c.text((left - 12, y), _fit(str(r["name"]), label_size + 1, name_px),
               label_size + 1, TEXT, anchor="rm")
        # значение — со стороны бара, чтобы взгляд не возвращался к центру
        label = f"{d:+.0f}"
        if r.get("y_bps") is not None:
            label += f"  ({r['y_bps']:.0f})"
        # Серия: «третий день подряд в одну сторону» отличает тренд от разового
        # прыжка на одной сделке — ради этого числа и заводили историю.
        if (r.get("streak") or 0) >= 2:
            label += f"  ·  {int(r['streak'])}д"
        if labels_right:
            # В узкой колонке подпись «наружу» уезжала на имя выпуска. Держим
            # все числа СПРАВА: у расширения — за концом бара, у сужения — сразу
            # за осью (слева от неё бар, справа пусто). Знак называют сторона и
            # цвет бара, число его только уточняет.
            c.text((x1 + 10 if d > 0 else mid + 10, y), label, label_size, MUTED,
                   anchor="lm")
        else:
            c.text((x1 + 10 if d > 0 else x0 - 10, y), label, label_size, MUTED,
                   anchor="lm" if d > 0 else "rm")


def movers(rows: List[dict], title: str, subtitle: str = "") -> bytes:
    """Диверг-бары Δ спреда: расширение вправо красным, сужение влево зелёным.

    Ноль по центру, а не слева: за день важно не «на сколько уехали» вообще, а
    в какую сторону — и разложенные по обе стороны от оси бумаги читаются одним
    взглядом, без чтения знаков.

    rows: [{name, delta_bps, y_bps, streak}] — уже отсортированные."""
    c = Canvas(title, subtitle)
    if not rows:
        c.text((32, H / 2), "нет данных за день", 18, MUTED)
        return c.png()
    _diverg_panel(c, rows, (190, W - 130, c.top, H - 40))
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
    left, right, top, bottom = 190, W - 250, c.top, H - 46
    top_val = max(r["value"] for r in rows) or 1.0
    step = (bottom - top) / len(rows)
    bar_h = min(26.0, step * 0.62)
    for i, r in enumerate(rows):
        y = top + step * i + step / 2
        w = float(r["value"]) / top_val * (right - left)
        # Рубль оборота одинаков на обоих рынках, поэтому рейтинг общий — но
        # цвет сразу говорит, чей это оборот, без чтения имён.
        c.rect((left, y - bar_h / 2, left + w, y + bar_h / 2),
               ACCENT2 if r.get("kind") == "fixed" else ACCENT, radius=3)
        c.text((left - 12, y), _fit(str(r["name"]), 15, 150), 15, TEXT, anchor="rm")
        tail = f"{_money(float(r['value']))} {rub()}"
        if r.get("trades"):
            tail += f"  ·  {int(r['trades'])} сд"
        c.text((left + w + 10, y), tail, 14, MUTED, anchor="lm")
    c.rect((left, H - 26, left + 18, H - 20), ACCENT, radius=2)
    c.text((left + 26, H - 23), "флоатеры", 13, MUTED)
    c.rect((left + 140, H - 26, left + 158, H - 20), ACCENT2, radius=2)
    c.text((left + 166, H - 23), "фиксы", 13, MUTED)
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
    # Нижняя панель — сдвиг кривой в бп. Две ломаные, легшие друг на друга,
    # глазом не вычитаются: «на сколько уехала ставка» — отдельный вопрос, и
    # ради него мы и показываем срез недельной давности.
    delta = None
    if len(pts) == 2:
        a, b = pts[0]["values"], pts[1]["values"]
        delta = [None if (x is None or y is None) else (y - x) * 100
                 for x, y in zip(a, b)]
        if not any(d is not None for d in delta):
            delta = None
    panel = 132 if delta else 0
    left, right, top, bottom = 70, W - 40, c.top + 10, H - 60 - panel
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
    if delta:
        d_top, d_bottom = bottom + 46, H - 34
        span = max(abs(d) for d in delta if d is not None) or 1.0
        zero = (d_top + d_bottom) / 2
        c.line([(left, zero), (right, zero)], GRID, 1)
        c.text((left - 10, zero), "0", 12, MUTED, anchor="rm")
        c.text((left - 10, d_top), f"+{span:.0f}", 12, MUTED, anchor="rm")
        bw = (right - left) / len(labels) * 0.44
        for i, d in enumerate(delta):
            if d is None:
                continue
            x = px(i)
            h = abs(d) / span * (zero - d_top)
            y0, y1 = (zero - h, zero) if d > 0 else (zero, zero + h)
            c.rect((x - bw / 2, y0, x + bw / 2, y1),
                   DOWN if d > 0 else UP, radius=2)
        # справа: слева на этой высоте идут подписи теноров основного поля
        c.text((right, d_top - 16), "сдвиг за период, бп", 13, MUTED, anchor="rm")
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


# ── сюжет 5: крупные сделки ────────────────────────────────────────────────
def blocks(rows: List[dict], title: str, subtitle: str = "") -> bytes:
    """Бары по сумме сделки: адресные (РПС) лиловым, безадресные синим.

    Поштучно, а не суммой по бумаге: новость дня — «кто-то переложил миллиард
    одним тикетом», и агрегат по выпуску ровно это и стирает.

    rows: [{name, value, price, market, time, y_bps}] — отсортированные."""
    c = Canvas(title, subtitle)
    if not rows:
        c.text((32, H / 2), "крупных сделок не было", 18, MUTED)
        return c.png()
    left, right, top, bottom = 210, W - 260, c.top, H - 46
    top_val = max(r["value"] for r in rows) or 1.0
    step = (bottom - top) / len(rows)
    bar_h = min(24.0, step * 0.62)
    for i, r in enumerate(rows):
        y = top + step * i + step / 2
        w = float(r["value"]) / top_val * (right - left)
        rps = (r.get("market") or "") == "ndm"
        c.rect((left, y - bar_h / 2, left + w, y + bar_h / 2),
               ACCENT2 if rps else ACCENT, radius=3)
        name = str(r.get("name") or "")
        if r.get("time"):
            name = f"{r['time']}  {name}"
        c.text((left - 12, y), _fit(name, 15, 170), 15, TEXT, anchor="rm")
        tail = f"{_money(float(r['value']))} {rub()}"
        if r.get("price") is not None:
            tail += f"  ·  {float(r['price']):.2f}".replace(".", ",")
        if r.get("y_bps") is not None:
            tail += f"  ·  {float(r['y_bps']):.0f} бп"
        c.text((left + w + 10, y), tail, 14, MUTED, anchor="lm")
    c.rect((left, H - 26, left + 18, H - 20), ACCENT, radius=2)
    c.text((left + 26, H - 23), "безадресные", 13, MUTED)
    c.rect((left + 160, H - 26, left + 178, H - 20), ACCENT2, radius=2)
    c.text((left + 186, H - 23), "адресные (РПС)", 13, MUTED)
    return c.png()


# ── сюжет 6: широта движения ───────────────────────────────────────────────
def _hist_panel(c: "Canvas", deltas: List[float], box, label: str = "",
                axis_size: int = 13) -> None:
    """Одна гистограмма Δ в рамке (left, right, top, bottom) со своей шкалой.

    Своя шкала у каждой панели — обязательна: фиксы за день ходят на единицы
    базисных пунктов, флоатеры на сотни, и общий масштаб превратил бы один из
    рынков в плоскую полосу."""
    left, right, top, bottom = box
    wide = sum(1 for d in deltas if d > 0)
    tight = sum(1 for d in deltas if d < 0)
    # Хвост режем по 95-му перцентилю: одна бумага на +900 бп растягивает шкалу
    # так, что вся масса рынка складывается в один столбик у нуля.
    srt = sorted(abs(d) for d in deltas)
    cap = srt[int(len(srt) * 0.95)] if len(srt) > 20 else (srt[-1] if srt else 1.0)
    cap = max(cap, 10.0)
    nb = 21                                  # нечётное: у нуля свой центральный бин
    step_bps = (2 * cap) / nb
    bins = [0] * nb
    for d in deltas:
        k = int((min(max(d, -cap), cap) + cap) / step_bps)
        bins[min(nb - 1, max(0, k))] += 1
    hi = max(bins) or 1
    bw = (right - left) / nb
    for k in range(3):
        v = hi * k / 2
        y = bottom - (v / hi) * (bottom - top)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 10, y), f"{v:.0f}", axis_size, MUTED, anchor="rm")
    for i, n in enumerate(bins):
        if not n:
            continue
        x0 = left + bw * i + bw * 0.15
        x1 = left + bw * (i + 1) - bw * 0.15
        h = n / hi * (bottom - top)
        centre = -cap + step_bps * (i + 0.5)
        color = DOWN if centre > 0 else (UP if centre < 0 else MUTED)
        c.rect((x0, bottom - h, x1, bottom), color, radius=2)
    mid = left + (right - left) / 2
    c.line([(mid, top - 4), (mid, bottom + 4)], GRID, 1)
    # «и дальше»: крайние бины вбирают весь хвост — без пометки они читались бы
    # как «ровно −79», хотя там сидят и −900
    c.text((left, bottom + 18), f"≤ −{cap:.0f} бп", axis_size, MUTED, anchor="lm")
    c.text((mid, bottom + 18), "0", axis_size, MUTED, anchor="mm")
    c.text((right, bottom + 18), f"≥ +{cap:.0f} бп", axis_size, MUTED, anchor="rm")
    if label:
        c.text((left, top - 14), label, 14, MUTED)
    c.text((mid + 60, top - 14), f"шире: {wide}", 14, DOWN)
    c.text((mid + 190, top - 14), f"уже: {tight}", 14, UP)
    c.text((right, top - 14), f"бумаг: {len(deltas)}", 13, MUTED, anchor="rm")


def breadth(deltas: List[float], title: str, subtitle: str = "") -> bytes:
    """Гистограмма Δ спреда по всему торговавшемуся рынку — одной панелью."""
    c = Canvas(title, subtitle)
    if not deltas:
        c.text((32, H / 2), "нет данных за день", 18, MUTED)
        return c.png()
    _hist_panel(c, deltas, (70, W - 30, c.top + 30, H - 64))
    return c.png()


# ── сюжет 7: карта рынка ───────────────────────────────────────────────────
def scatter(points: List[dict], title: str, subtitle: str = "",
            x_label: str = "лет до погашения", y_label: str = "премия, бп",
            legend: bool = True) -> bytes:
    """Премия против срока: точка — выпуск, размер — оборот, цвет — тип.

    Плоский список «кто где стоит» такую картину не даёт: здесь сразу видно и
    наклон рынка по сроку, и выбросы, которые сидят вне облака.

    points: [{x, y, v, kind, name}] — kind 'floater'|'fixed'."""
    c = Canvas(title, subtitle)
    pts = [p for p in points if p.get("x") is not None and p.get("y") is not None]
    if not pts:
        c.text((32, H / 2), "нет данных за день", 18, MUTED)
        return c.png()
    left, right, top, bottom = 76, W - 30, c.top + 10, H - 64
    # Шкала по премии — до 95-го перцентиля. Десяток бумаг на 2000+ бп (дефолтные
    # истории и экзотика) растягивают ось так, что весь рынок ложится в одну
    # полоску у нуля; их считаем и выносим в подпись, а не в масштаб.
    ys_all = sorted(p["y"] for p in pts)
    y_cap = ys_all[int(len(ys_all) * 0.95)] if len(ys_all) > 20 else ys_all[-1]
    hidden = [p for p in pts if p["y"] > y_cap]
    pts = [p for p in pts if p["y"] <= y_cap]
    if not pts:
        c.text((32, H / 2), "нет данных за день", 18, MUTED)
        return c.png()
    xs = [p["x"] for p in pts]
    ys = [p["y"] for p in pts]
    x_lo, x_hi = 0.0, max(xs) or 1.0
    y_lo, y_hi = min(ys), max(ys)
    pad = max(10.0, (y_hi - y_lo) * 0.1)
    y_lo, y_hi = y_lo - pad, y_hi + pad
    v_hi = max((p.get("v") or 0) for p in pts) or 1.0

    def px(x: float) -> float:
        return left + (x - x_lo) / (x_hi - x_lo or 1) * (right - left)

    def py(y: float) -> float:
        return bottom - (y - y_lo) / (y_hi - y_lo or 1) * (bottom - top)

    # подпись оси — НАД полем слева: сбоку она не помещается между краем
    # картинки и цифрами шкалы, и первое слово срезалось
    c.text((left - 6, top - 16), y_label, 13, MUTED)
    for k in range(5):
        v = y_lo + (y_hi - y_lo) * k / 4
        y = py(v)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 10, y), f"{v:.0f}", 13, MUTED, anchor="rm")
    for k in range(6):
        x = x_lo + (x_hi - x_lo) * k / 5
        c.text((px(x), bottom + 20), f"{x:.1f}".replace(".", ","), 13, MUTED,
               anchor="mm")
    for p in pts:
        # Радиус по КОРНЮ оборота: линейный размер превращает миллиардную бумагу
        # в пятно на пол-графика, а всё остальное — в пыль.
        r = 3.0 + 11.0 * ((p.get("v") or 0) / v_hi) ** 0.5
        c.dot((px(p["x"]), py(p["y"])), r,
              ACCENT if p.get("kind") != "fixed" else ACCENT2)
    # Подписываем только самые крупные: имена всех точек слипаются в кашу
    for p in sorted(pts, key=lambda p: p.get("v") or 0, reverse=True)[:5]:
        x, y = px(p["x"]), py(p["y"])
        name = _fit(str(p.get("name") or ""), 13, 130)
        # у правого края подпись уходила за картинку — разворачиваем влево
        if x > right - 150:
            c.text((x - 12, y - 12), name, 13, TEXT, anchor="rm")
        else:
            c.text((x + 12, y - 12), name, 13, TEXT)
    c.text((left, bottom + 42), x_label, 13, MUTED)
    if hidden:
        c.text((right, top + 6), f"вне шкалы: {len(hidden)} бумаг(и) выше "
                                 f"{y_cap:.0f} бп", 13, MUTED, anchor="rm")
    if legend:
        c.rect((right - 300, H - 26, right - 282, H - 20), ACCENT, radius=2)
        c.text((right - 274, H - 23), "флоатеры", 13, MUTED)
        c.rect((right - 160, H - 26, right - 142, H - 20), ACCENT2, radius=2)
        c.text((right - 134, H - 23), "фиксы", 13, MUTED)
    return c.png()


# ── сюжет 8: премия по рейтингам ───────────────────────────────────────────
def grouped(cats: List[str], series: List[dict], title: str, subtitle: str = "",
            value_fmt: str = "{:.0f}") -> bytes:
    """Сгруппированные бары по категориям (рейтинговым бакетам).

    Медиана, а не среднее, считается вызывающим — здесь только рисуем: в
    бакете из десятка бумаг один выброс сдвигает среднее на сотню бп.

    series: [{label, color, values:[float|None]}] — длиной с cats."""
    c = Canvas(title, subtitle)
    live = [s for s in series if any(v is not None for v in s["values"])]
    if not cats or not live:
        c.text((32, H / 2), "рейтингов на сегодня нет", 18, MUTED)
        return c.png()
    left, right, top, bottom = 80, W - 30, c.top + 20, H - 64
    vals = [v for s in live for v in s["values"] if v is not None]
    hi = max(vals + [0]) or 1.0
    lo = min(vals + [0])
    step = (right - left) / len(cats)
    gw = step * 0.66 / len(live)

    def py(v: float) -> float:
        return bottom - (v - lo) / ((hi - lo) or 1) * (bottom - top)

    for k in range(4):
        v = lo + (hi - lo) * k / 3
        y = py(v)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 10, y), value_fmt.format(v), 13, MUTED, anchor="rm")
    for i, cat in enumerate(cats):
        x0 = left + step * i + step * 0.17
        for j, s in enumerate(live):
            v = s["values"][i]
            if v is None:
                continue
            bx = x0 + gw * j
            c.rect((bx, py(v), bx + gw * 0.86, py(lo)), s["color"], radius=3)
            c.text((bx + gw * 0.43, py(v) - 12), value_fmt.format(v), 12, TEXT,
                   anchor="mm")
        c.text((left + step * i + step / 2, bottom + 20), str(cat), 14, TEXT,
               anchor="mm")
    for j, s in enumerate(live):
        c.rect((left + j * 200, H - 26, left + j * 200 + 18, H - 20),
               s["color"], radius=2)
        c.text((left + j * 200 + 26, H - 23), str(s["label"]), 13, MUTED)
    return c.png()


# ── сюжет 9: профиль торгов ────────────────────────────────────────────────
def profile(bars: List[dict], title: str, subtitle: str = "",
            bar_label: str = "оборот") -> bytes:
    """Оборот стеком (флоатеры + фиксы) и медианная премия двумя линиями.

    Две шкалы в одной картинке оправданы вопросом: «в какие часы шли деньги и
    что в это время делала премия». Рынки разведены по цвету и по линиям —
    складывать Y-IDX флоатера с g-спредом фикса в одну медиану нельзя, а вот
    деньги складываются: рубль оборота везде рубль.

    bars: [{label, v_float, v_fixed, y_float, y_fixed}]."""
    c = Canvas(title, subtitle)
    tot = [float(b.get("v_float") or 0) + float(b.get("v_fixed") or 0) for b in bars]
    if not bars or not any(tot):
        c.text((32, H / 2), "торгов не было", 18, MUTED)
        return c.png()
    left, right, top, bottom = 80, W - 80, c.top + 20, H - 64
    hi = max(tot) or 1.0
    step = (right - left) / len(bars)
    bw = min(52.0, step * 0.6)
    for k in range(4):
        v = hi * k / 3
        y = bottom - (v / hi) * (bottom - top)
        c.line([(left, y), (right, y)], GRID, 1)
        c.text((left - 10, y), _money(v), 13, MUTED, anchor="rm")
    for i, b in enumerate(bars):
        x = left + step * i + step / 2
        h_f = float(b.get("v_float") or 0) / hi * (bottom - top)
        h_x = float(b.get("v_fixed") or 0) / hi * (bottom - top)
        if h_f:
            c.rect((x - bw / 2, bottom - h_f, x + bw / 2, bottom), ACCENT, radius=3)
        if h_x:
            c.rect((x - bw / 2, bottom - h_f - h_x, x + bw / 2, bottom - h_f),
                   ACCENT2, radius=3)
        c.text((x, bottom + 20), str(b.get("label") or ""), 13, MUTED, anchor="mm")
    # Линии премии — в СВОИХ координатах, общая шкала на обе: обе метрики в бп
    # и одного порядка, а раздельные оси на одной картинке никто не читает.
    ys = [b[k] for b in bars for k in ("y_float", "y_fixed") if b.get(k) is not None]
    if len(ys) >= 2:
        y_lo, y_hi = min(ys), max(ys)
        pad = max(2.0, (y_hi - y_lo) * 0.2)
        y_lo, y_hi = y_lo - pad, y_hi + pad
        for key, color in (("y_float", LINE1), ("y_fixed", LINE2)):
            xy = [(left + step * i + step / 2,
                   bottom - (b[key] - y_lo) / (y_hi - y_lo) * (bottom - top))
                  for i, b in enumerate(bars) if b.get(key) is not None]
            if len(xy) < 2:
                continue
            c.line(xy, color, 3)
            for pt in xy:
                c.dot(pt, 3.5, color)
        c.text((right + 10, bottom - (bottom - top)), f"{y_hi:.0f}", 13, MUTED,
               anchor="lm")
        c.text((right + 10, bottom), f"{y_lo:.0f}", 13, MUTED, anchor="lm")
    # Легенда — только по тем сериям, что реально нарисованы: в альбоме одного
    # рынка вторая пара всегда пуста, и «фиксы» под флоатерным профилем
    # выглядели бы потерянными данными.
    legend = []
    if any(b.get("v_float") for b in bars):
        legend.append((ACCENT, f"{bar_label}: флоатеры"))
    if any(b.get("v_fixed") for b in bars):
        legend.append((ACCENT2, f"{bar_label}: фиксы"))
    if any(b.get("y_float") is not None for b in bars):
        legend.append((LINE1, "медиана Y-IDX"))
    if any(b.get("y_fixed") is not None for b in bars):
        legend.append((LINE2, "медиана g-спреда"))
    x = left
    for color, text in legend:
        c.rect((x, H - 26, x + 18, H - 20), color, radius=2)
        c.text((x + 26, H - 23), text, 13, MUTED)
        x += 40 + _font(13).getlength(text) / SS
    return c.png()
