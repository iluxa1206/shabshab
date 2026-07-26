from datetime import date, datetime
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field

# --- General System Models ---
class HealthResponse(BaseModel):
    status: str
    version: str
    time: datetime

class MetaResponse(BaseModel):
    calc_date: date
    rates_date: date
    sources: Dict[str, Any]
    source_status: Dict[str, bool] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

# --- Error Models ---
class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None

class ErrorResponse(BaseModel):
    error: ErrorDetail


# --- 5.1 BondReference ---
class BondReference(BaseModel):
    isin: str
    short_name: str
    face_value: float
    face_unit: str
    base_rate_type: str
    spread_bps: int
    formula: str
    start_date: Optional[date]
    maturity_date: Optional[date]
    coupon_period_days: Optional[int]
    coupons_per_year: Optional[int]
    next_coupon_date: Optional[date]
    accrued_interest: float
    # ближайшая будущая оферта (MOEX bondization offers). Информационный флаг:
    # DM/z считаются к погашению (НРД тоже — клэмп к оферте ухудшает сверку на
    # всех горизонтах), но рынок может прайсить бумагу к оферте.
    offer_date: Optional[date] = None
    offer_type: Optional[str] = None
    offer_kind: Optional[str] = None   # 'put' (держателя) | 'call' (эмитента)

# --- 5.2 BondMarketData ---
class BondMarketData(BaseModel):
    last_price_pct: Optional[float]
    price_source: str
    calc_date: date
    rates_date: date
    market_timestamp: Optional[datetime]
    is_stale: bool = False
    prev_close_clean_pct: Optional[float] = None
    prev_close_dm_bps: Optional[int] = None

# --- 5.3 BondValuation ---
class BondValuation(BaseModel):
    clean_price_pct: float
    dirty_price_rub: float
    dm_bps: Optional[int]                       # = sm_bps (backward-compat)
    sm_bps: Optional[int] = None               # наш simple margin ≈ НРД simple_margin
    disc_margin_bps: Optional[int] = None      # наш discount margin ≈ НРД discount_margin
    dm_label: Optional[str]
    yield_xirr_pct: Optional[float]
    index_yield_pct: Optional[float]        # эфф. годовая доходность роллирования индекса (форвард)
    yield_over_index_bps: Optional[int]     # IRR бумаги − доходность индекса, bps
    pricing_status: str
    warnings: List[str] = Field(default_factory=list)
    # ПОДСКАЗКА потребителю, какой горизонт показывать первым: "offer", если у
    # бумаги есть будущая оферта (рынок торгует к ней), иначе "maturity".
    # ВАЖНО: это НЕ описание полей выше. sm_bps / disc_margin_bps / yield_xirr_pct
    # ВСЕГДА считаются к ПОГАШЕНИЮ (так сверяемся с НРД) независимо от значения
    # этого поля; цифры к оферте лежат отдельно в *_to_offer. Прежнее имя
    # valuation_horizon читалось как «горизонт, по которому посчитан этот объект»,
    # что неверно. На 2026-07-20 фронт это поле не читает вовсе.
    preferred_horizon: str = "maturity"
    offer_date: Optional[date] = None
    offer_price_pct: Optional[float] = None
    sm_to_offer_bps: Optional[int] = None            # simple margin к оферте (yield-to-put)
    disc_margin_to_offer_bps: Optional[int] = None   # discount margin к оферте
    yield_to_offer_pct: Optional[float] = None       # XIRR к оферте


# Ответ /reprice: BondValuation + цена-зависимые z_model/carry (для live-рефреша
# всей строки таблицы по WS-тику, не только карточки-калькулятора)
class RepriceResponse(BondValuation):
    z_model_bps: Optional[int] = None
    carry_bps: Optional[int] = None

# --- 5.4 CashflowItem ---
class CashflowItem(BaseModel):
    number: int
    period_start: date
    period_end: date
    payment_date: date
    coupon_formula: str
    base_rate_pct: float
    spread_bps: int
    coupon_rate_pct: float
    amount_rub: float
    type: str

# --- 5.7 FloaterRisk (специфика бумаг с плавающим купоном) ---
class FloaterRisk(BaseModel):
    spread_duration_yrs: Optional[float] = None   # Macaulay потоков ≈ чувствительность к ΔDM/Δz
    rate_duration_yrs: Optional[float] = None      # ≈ дни до рефиксинга/365 — риск параллельного сдвига
    days_to_refix: Optional[int] = None            # дней до следующей переустановки ставки
    current_coupon_pct: Optional[float] = None     # зафикс. ставка текущего купона, %
    base_rate_pct: Optional[float] = None          # текущий уровень базы (КС/RUONIA), %
    carry_bps: Optional[int] = None                # купон-доходность − база, bps
    breakeven_base_pct: Optional[float] = None     # уровень базы, где carry-эдж исчезает, %
    mod_duration: Optional[float] = None
    convexity: Optional[float] = None
    pvbp: Optional[float] = None


# --- 5.5 BondDetailsResponse ---
class BondDetailsResponse(BaseModel):
    reference: BondReference
    market: BondMarketData
    valuation: BondValuation
    cashflow: List[CashflowItem]
    floater: Optional[FloaterRisk] = None
    sources: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)

# --- Cashflow & Valuation Endpoints ---
class CashflowResponse(BaseModel):
    isin: str
    calc_date: date
    currency: str = "RUB"
    items: List[CashflowItem]
    redemption_amount: float

class ValuationResponse(BaseModel):
    isin: str
    calc_date: date
    clean_price_pct: float
    dirty_price_rub: float
    dm_bps: Optional[int]
    dm_label: Optional[str]
    yield_xirr_pct: Optional[float]
    index_yield_pct: Optional[float]
    yield_over_index_bps: Optional[int]
    warnings: List[str] = Field(default_factory=list)

# --- 6.2 Bond List / Dashboard Models ---
class BondListItem(BaseModel):
    isin: str
    short_name: str
    base_rate_type: str
    formula: str
    emitter_id: Optional[int] = None       # MOEX EMITTER_ID (фильтр/агрегаты по эмитенту)
    emitter_name: Optional[str] = None
    spread_issue_bps: int
    maturity_date: Optional[date]
    next_coupon_date: Optional[date]
    last_price_pct: Optional[float]
    dirty_price_rub: Optional[float]
    dm_bps: Optional[int]
    delta_to_prev_close: Optional[float] = None # placeholder
    yield_xirr_pct: Optional[float] = None     # YTM бумаги (XIRR на проекции купонов по форварду), %
    index_yield_pct: Optional[float] = None    # YTM роллирования базы (КС/RUONIA) на тот же срок, %
    disc_margin_bps: Optional[int] = None      # наш discount margin (Fabozzi)
    yield_over_index_bps: Optional[int] = None # IRR бумаги − доходность роллирования индекса, bps
    price_implausible: bool = False            # цена → гарант. убыток (стейл/тонкая), спреды скрыты
    price_thin: bool = False                    # 0 сделок сегодня → цена несвежая, DM/z с ненадёжной цены
    price_stale: bool = False                   # показана prev-close (нет live/сделки сегодня), не текущая
    z_model_bps: Optional[int] = None  # наш z-спред над КБД ОФЗ
    rating: Optional[str] = None
    # флоатер-метрики (кросс-секция — по всему юниверсу; carry/refix — только watch)
    spread_dur_yrs: Optional[float] = None     # ≈ срок до погашения (лет) = spread duration
    carry_bps: Optional[int] = None            # текущий купон-доходность − база, bps (watch)
    days_to_refix: Optional[int] = None        # дни до следующего рефиксинга (watch)
    current_coupon_pct: Optional[float] = None # зафикс. ставка текущего купона, % (watch)
    # оферта: подсказка, что показать первым. preferred_horizon="offer" → бумага
    # имеет будущую оферту, осмысленно вывести sm_to_offer_bps. Поля dm_bps /
    # disc_margin_bps этого объекта ВСЕГДА к погашению (см. BondValuation).
    preferred_horizon: str = "maturity"
    offer_date: Optional[date] = None
    sm_to_offer_bps: Optional[int] = None          # simple margin к оферте (yield-to-put)
    disc_margin_to_offer_bps: Optional[int] = None # discount margin к оферте

class BondListResponse(BaseModel):
    items: List[BondListItem]
    total: int
    limit: int
    offset: int

class BondFiltersResponse(BaseModel):
    issuers: List[str]
    classes: List[str]
    base_rates: List[str] # RUONIA, KEYRATE
    maturities: List[str] # could be 1Y, 3Y, 5Y, etc.

# --- Curves & Forwards Models ---
class CurveNode(BaseModel):
    date: date
    discount_factor: float
    forward_pct: Optional[float] = None

class CurveSegment(BaseModel):
    start_date: date
    end_date: date
    forward_pct: float

class CurveResponse(BaseModel):
    curve_type: str
    calc_date: date
    nodes: List[CurveNode]
    segments: List[CurveSegment]
    warnings: List[str] = Field(default_factory=list)

class ForwardRateResponse(BaseModel):
    curve_type: str
    calc_date: date
    start_date: date
    end_date: date
    forward_pct: float
    warnings: List[str] = Field(default_factory=list)

# --- Curve plot (котировки + построенная кривая для вкладки) ---
class CurveQuote(BaseModel):
    tenor: str
    days: int            # срок до даты погашения тенора от start
    value_pct: float     # исходная par-котировка СПФИ (mid)
    name: str

class CurveSample(BaseModel):
    days: int            # смещение от calc_date
    date: date
    spot_pct: float      # эквивалентная средняя ставка индекса на срок (из DF)
    forward_pct: float   # мгновенный форвард на сегмент ~30д вперёд

class CurvePlotResponse(BaseModel):
    curve_type: str          # RUONIA | KEYRATE
    calc_date: date
    rates_date: Optional[date] = None
    quotes: List[CurveQuote]
    samples: List[CurveSample]
    warnings: List[str] = Field(default_factory=list)

# --- KS path (реплика КС-прогноз: рыночный форвард vs сценарии ЦБ) ---
class KsPathPoint(BaseModel):
    date: date
    actual_pct: Optional[float] = None     # факт КС (прошедшие заседания)
    market_pct: Optional[float] = None     # рыночный форвард bootstrap-кривой IRS KEYRATE
    forecast_pct: Optional[float] = None    # среднесрочный прогноз ЦБ (avg КС по годам)
    nrd_pril3_pct: Optional[float] = None  # ожидаемая КС по НРД met_float Прил.3
                                           # (сплайн свопов + затухание к нейтрали)

class KsPathResponse(BaseModel):
    calc_date: date
    current_ks_pct: Optional[float] = None
    # принятое, но ещё не вступившее решение ЦБ по КС (эффект со след. раб. дня)
    decided_rate_pct: Optional[float] = None
    decided_effective: Optional[str] = None
    decided_decision: Optional[str] = None
    points: List[KsPathPoint]
    warnings: List[str] = Field(default_factory=list)

# --- Orderbook Models ---
class OrderbookLevel(BaseModel):
    price_pct: float
    quantity: int
    yield_pct: Optional[float] = None
    sm_bps: Optional[int] = None
    dm_bps: Optional[int] = None

class OrderbookSnapshot(BaseModel):
    bids: List[OrderbookLevel]
    asks: List[OrderbookLevel]

class OrderbookResponse(BaseModel):
    isin: str
    market_timestamp: Optional[datetime]
    pricing_status: str
    calc_date: date
    orderbook: OrderbookSnapshot
    warnings: List[str] = Field(default_factory=list)

