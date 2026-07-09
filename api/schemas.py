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
    dm_bps: Optional[int]
    dm_label: Optional[str]
    yield_xirr_pct: Optional[float]
    base_yield_pct: Optional[float]
    spread_to_base_bps: Optional[int]
    pricing_status: str
    warnings: List[str] = Field(default_factory=list)

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

# --- 5.6 BondNrd (НРД Ценовой центр: valuationnewadd; fair value — при доступе) ---
class BondNrd(BaseModel):
    # цены НРД
    fair_value_pct: Optional[float] = None        # справедливая чистая цена (valuationnew, если есть доступ)
    fair_dirty_rub: Optional[float] = None
    nrd_price_pct: Optional[float] = None         # средневзвешенная цена осн. сессии (wa_price), %
    nrd_close_pct: Optional[float] = None         # цена аукциона закрытия, %
    price_vs_nrd_pct: Optional[float] = None      # рыночная clean − цена НРД (>0 дорого, <0 дёшево)
    valuation_method: Optional[Any] = None
    # доходности (%)
    ytm_pct: Optional[float] = None
    ytm_close_pct: Optional[float] = None
    current_yield_pct: Optional[float] = None
    yield_maturity_pct: Optional[float] = None
    yield_call_pct: Optional[float] = None
    yield_put_pct: Optional[float] = None
    # риск-метрики
    duration: Optional[float] = None
    mod_duration: Optional[float] = None
    convexity: Optional[float] = None
    pvbp: Optional[float] = None
    # спреды НРД (bps)
    z_spread_bps: Optional[int] = None
    g_spread_bps: Optional[int] = None
    discount_margin_bps: Optional[int] = None
    simple_margin_bps: Optional[int] = None
    nominal_margin_bps: Optional[int] = None
    # параметры флоатера
    base_coupon_index: Optional[str] = None       # CBRATED / RUONIARATED / ...
    coupon_type: Optional[str] = None             # fix / float
    # рейтинги / ликвидность
    ratings: Optional[Dict[str, Any]] = None
    liquidity: Optional[Dict[str, Any]] = None
    # служебное
    nrd_calc_date: Optional[str] = None
    source: str = "NRD Price Center"
    is_stale: bool = False


# --- 5.7 FloaterRisk (специфика бумаг с плавающим купоном) ---
class FloaterRisk(BaseModel):
    spread_duration_yrs: Optional[float] = None   # Macaulay потоков ≈ чувствительность к ΔDM/Δz
    rate_duration_yrs: Optional[float] = None      # ≈ дни до рефиксинга/365 — риск параллельного сдвига
    days_to_refix: Optional[int] = None            # дней до следующей переустановки ставки
    current_coupon_pct: Optional[float] = None     # зафикс. ставка текущего купона, %
    base_rate_pct: Optional[float] = None          # текущий уровень базы (КС/RUONIA), %
    carry_bps: Optional[int] = None                # купон-доходность − база, bps
    breakeven_base_pct: Optional[float] = None     # уровень базы, где carry-эдж исчезает, %
    # риск-метрики НРД (дублируем для полноты карточки риска)
    mod_duration: Optional[float] = None
    convexity: Optional[float] = None
    pvbp: Optional[float] = None


# --- 5.5 BondDetailsResponse ---
class BondDetailsResponse(BaseModel):
    reference: BondReference
    market: BondMarketData
    valuation: BondValuation
    cashflow: List[CashflowItem]
    nrd: Optional[BondNrd] = None
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
    base_yield_pct: Optional[float]
    spread_to_base_bps: Optional[int]
    warnings: List[str] = Field(default_factory=list)

# --- 6.2 Bond List / Dashboard Models ---
class BondListItem(BaseModel):
    isin: str
    short_name: str
    base_rate_type: str
    formula: str
    spread_issue_bps: int
    maturity_date: Optional[date]
    next_coupon_date: Optional[date]
    last_price_pct: Optional[float]
    dirty_price_rub: Optional[float]
    dm_bps: Optional[int]
    delta_to_prev_close: Optional[float] = None # placeholder
    # НРД (лёгкие поля для таблицы; заполняются при with_nrd=true)
    nrd_price_pct: Optional[float] = None
    price_vs_nrd_pct: Optional[float] = None
    nrd_duration: Optional[float] = None
    discount_margin_bps: Optional[int] = None
    # simple_margin — правильный like-for-like якорь для нашего dm_bps
    # (наш DM ≈ НРД simple_margin; discount_margin — их fair-value метрика)
    simple_margin_bps: Optional[int] = None
    z_spread_bps: Optional[int] = None
    z_model_bps: Optional[int] = None  # наш z-спред над КБД ОФЗ (методика НРД)
    rating: Optional[str] = None
    # флоатер-метрики (кросс-секция — по всему юниверсу; carry/refix — только watch)
    spread_dur_yrs: Optional[float] = None     # ≈ срок до погашения (лет) = spread duration
    z_pctile: Optional[int] = None             # перцентиль z внутри рейтинг-бакета (0..100)
    delta_z_dod: Optional[int] = None          # Δ z-спреда день-к-дню, bps (из истории)
    delta_z_mom: Optional[int] = None          # Δ z-спреда за ~месяц, bps (из истории)
    carry_bps: Optional[int] = None            # текущий купон-доходность − база, bps (watch)
    days_to_refix: Optional[int] = None        # дни до следующего рефиксинга (watch)
    current_coupon_pct: Optional[float] = None # зафикс. ставка текущего купона, % (watch)

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

# --- Orderbook Models ---
class OrderbookLevel(BaseModel):
    price_pct: float
    quantity: int
    yield_pct: Optional[float] = None
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

