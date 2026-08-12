from datetime import date, datetime
from datetime import date as _date   # для моделей, где поле называется date
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
    # ID выпуска в Cbonds (из bondsearch-выгрузки) — единственный способ дать
    # прямую ссылку: поиска по ISIN на сайте нет. None → ссылки не будет.
    cbonds_id: Optional[int] = None
    # SECID MOEX — запасная ссылка на страницу выпуска, когда cbonds_id нет
    # (у ОФЗ issue.aspx понимает только SECID, по ISIN редиректит)
    moex_secid: Optional[str] = None

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

# Метрики одного горизонта оценки (погашение / пут-оферта / call-оферта). Поток
# режется к date, выкуп остатка идёт по price_pct, база Y-IDX (роллирование
# RUONIA) считается до той же даты — иначе спред сравнивал бы бумагу с
# депозитом другого срока.
class HorizonMetrics(BaseModel):
    # _date, а не date: имя поля затеняет тип внутри тела класса, и аннотация
    # Optional[date] разрешилась бы в само поле → pydantic принимал только None
    date: Optional[_date] = None
    price_pct: Optional[float] = None            # цена выкупа на горизонте, % (обычно 100)
    sm_bps: Optional[int] = None
    disc_margin_bps: Optional[int] = None
    yield_xirr_pct: Optional[float] = None
    index_yield_pct: Optional[float] = None
    yield_over_index_bps: Optional[int] = None
    # {alt-цена: Y-IDX bps} — уровни стакана в метрике этого горизонта
    y_idx_by_price: Dict[float, Optional[int]] = Field(default_factory=dict)


# --- 5.3 BondValuation ---
class BondValuation(BaseModel):
    clean_price_pct: float
    # Optional: guard'ы calculate_valuation_metrics (MATURED / NO_MATURITY /
    # UNKNOWN_BASE) возвращают dirty=None. Пока поле было обязательным, такие
    # бумаги роняли /reprice в 500 на сериализации ответа. Фронт null-safe.
    dirty_price_rub: Optional[float] = None
    # Расчёт ведётся НА ДАТУ ПОСТАВКИ (T+1 раб): цена — котировка дня расчёта,
    # НКД и дисконтирование — на settlement_date. Поля отдаются наружу, чтобы
    # калькулятор карточки показывал, из чего собран dirty.
    settlement_date: Optional[date] = None
    accrued_settle_rub: Optional[float] = None   # НКД на дату поставки (в dirty; live —
                                                 # ровно биржевой ACCRUEDINT, он уже на T+1)
    accrued_calc_rub: Optional[float] = None     # НКД на дату расчёта (справочно; live —
                                                 # посчитан из графика купонов, не биржевой)
    pricing_face_rub: Optional[float] = None     # номинал, от которого котируется цена
    dm_bps: Optional[int]                       # = sm_bps (backward-compat)
    sm_bps: Optional[int] = None               # наш simple margin ≈ НРД simple_margin
    disc_margin_bps: Optional[int] = None      # наш discount margin ≈ НРД discount_margin
    dm_label: Optional[str]
    yield_xirr_pct: Optional[float]
    index_yield_pct: Optional[float]        # эфф. годовая доходность роллирования RUONIA (форвард) —
                                            # единая база и для КС-бумаг
    yield_over_index_bps: Optional[int]     # IRR бумаги − доходность роллирования RUONIA, bps
    pricing_status: str
    warnings: List[str] = Field(default_factory=list)
    # ГОРИЗОНТ, к которому бумага реально прайсится, по ПРАВИЛУ ЦЕНЫ:
    # "put" (цена ниже цены пут-выкупа — держатель предъявит), "call" (цена выше
    # цены call-выкупа — эмитент отзовёт), иначе "maturity".
    # ВАЖНО: это НЕ описание полей выше. sm_bps / disc_margin_bps / yield_xirr_pct
    # ВСЕГДА считаются к ПОГАШЕНИЮ (так сверяемся с НРД) независимо от значения
    # этого поля; цифры каждого горизонта лежат в horizons (и legacy *_to_offer).
    preferred_horizon: str = "maturity"
    # Полный набор метрик по каждому доступному горизонту: ключи maturity|put|call.
    # Свитчер в карточке переключает отображение между ними без пересчёта.
    horizons: Dict[str, HorizonMetrics] = Field(default_factory=dict)
    offer_date: Optional[date] = None
    offer_price_pct: Optional[float] = None
    sm_to_offer_bps: Optional[int] = None            # simple margin к оферте (yield-to-put)
    disc_margin_to_offer_bps: Optional[int] = None   # discount margin к оферте
    yield_to_offer_pct: Optional[float] = None       # XIRR к оферте


# Ответ /reprice: BondValuation + цена-зависимый z_model (для live-рефреша
# всей строки таблицы по WS-тику, не только карточки-калькулятора)
class RepriceResponse(BondValuation):
    z_model_bps: Optional[int] = None

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

# --- 5.6 Паспорт бумаги (аудит-развёртка для верификации расчётов) ---
class AuditCheck(BaseModel):
    id: str
    label: str
    status: str                      # ok | warn | bad | info | na
    detail: Optional[str] = None

class BondAuditResponse(BaseModel):
    isin: str
    generated_at: datetime
    calc_date: Optional[date]
    checks: List[AuditCheck]
    registry: Optional[Dict[str, Any]] = None   # строка реестра + enrich_seen
    spec: Dict[str, Any]                        # слои спеки фиксинга + провенанс
    backtest: Dict[str, Any]                    # по-купонный обратный пересчёт
    market: Dict[str, Any]                      # цена/НКД/свежесть индекса
    valuation: Dict[str, Any]                   # метрики оценки (как карточка)
    waterfall: Dict[str, Any]                   # по-платёжная развёртка PV
    schedule: Dict[str, Any]                    # сырой график MOEX (купоны/аморт/оферты)
    formula: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

# Дневная раскладка базовой ставки — все неистёкшие купоны одним списком
class CouponDayRow(BaseModel):
    day: date                      # день дохода (average) / день окна (avg_prev) / start
    obs_date: date                 # дата наблюдения индекса (день − lag)
    rate_pct: Optional[float]      # значение индекса, %
    src: str                       # fact | forward
    close_pct: Optional[float] = None   # цена закрытия дня (spread_daily)
    y_idx_bps: Optional[float] = None   # Y-IDX as-of дня по этой цене (spread_daily)
    index: Optional[float] = None       # расчётный индекс базы, старт 1.0 на начало периода
                                        # (капитализация по рабочим дням, выходные простыми)

class CouponDayGroup(BaseModel):
    n: Optional[int]               # № купона (как в таблице PV паспорта)
    start: date
    end: date
    pay_date: Optional[date]
    mean_pct: Optional[float]          # среднее индекса по дням раскладки
    projected_pct: Optional[float]     # боевой projected_ks_pct (кросс-чек)
    coupon_rate_pct: Optional[float]   # среднее + маржа, с кэпом/полом
    display_rate_pct: Optional[float]  # ставка купона из display-cashflow
    n_fact: int
    index_start: Optional[float] = None    # расчётный индекс на начало периода (сквозной!)
    index_end: Optional[float] = None      # он же на конец = index первой строки след. купона
    index_rate_pct: Optional[float] = None # рост ЗА ПЕРИОД в % годовых (End/Start−1)·365/дней
    rows: List[CouponDayRow]

class CouponDaysResponse(BaseModel):
    isin: str
    calc_date: Optional[date]
    base: str
    spec: Dict[str, Any]
    coupons: List[CouponDayGroup]
    n_days: int

# --- Cashflow & Valuation Endpoints ---
class CashflowResponse(BaseModel):
    isin: str
    calc_date: date
    currency: str = "RUB"
    items: List[CashflowItem]
    redemption_amount: float

# --- Календарь выплат (юниверс): купоны/погашения в деньгах на одну бумагу ---
class PaymentEvent(BaseModel):
    date: date
    isin: str
    name: str
    emitter: str
    base: str                      # KEYRATE | RUONIA | прочее
    type: str                      # COUPON | REDEMPTION
    amount_rub: float              # на одну бумагу
    total_rub: Optional[float] = None   # всего по выпуску (× ISSUESIZE); None если объём неизвестен
    rate_pct: Optional[float] = None
    projected: bool = False        # купон не зафиксирован — проекция форвардом

class PaymentsCalendarResponse(BaseModel):
    calc_date: date
    date_from: date
    date_to: date
    events: List[PaymentEvent]

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
    coupons_per_year: Optional[int] = None  # частота купона для подписи формулы (N/год)
    maturity_date: Optional[date]
    next_coupon_date: Optional[date]
    last_price_pct: Optional[float]
    # верх стакана MOEX (чистые цены, % номинала) + Y-IDX по ним. ask — это MOEX
    # OFFER (лучшая продажа), НЕ оферта put/call (та живёт в offer_date ниже).
    bid_price_pct: Optional[float] = None
    ask_price_pct: Optional[float] = None
    y_idx_bid_bps: Optional[int] = None
    y_idx_ask_bps: Optional[int] = None
    # сырьё фильтра по объёму: деньги уровня стакана = qty × (face×цена%/100 + НКД),
    # а Y-IDX по VWAP-цене = Y-IDX(верх) + Δцены × y_idx_slope_bps_per_pct.
    # face/НКД — ровно те, из которых собран dirty_price_rub (амортизация учтена,
    # НКД на дату поставки T+1).
    face_value_rub: Optional[float] = None
    accrued_rub: Optional[float] = None
    y_idx_slope_bps_per_pct: Optional[float] = None
    dirty_price_rub: Optional[float]
    dm_bps: Optional[int]
    # средневзвешенная цена дня, % номинала. У избранного это НАШ VWAP по тикам
    # Alor (тот же, что рисует слой «Средневзвес» на графике), приходит живым
    # push'ем; у остальных — биржевой WAPRICE из board-снапшота MOEX.
    wap_price_pct: Optional[float] = None
    val_today: Optional[float] = None           # оборот сегодня, ₽ (MOEX VALTODAY)
    # средний ДНЕВНОЙ оборот за 30 дней, ₽ — из архива часовых баров (см.
    # services.bars.adv_map): Σ денег окна / число торговых дней рынка
    adv_1m_rub: Optional[float] = None
    delta_to_prev_close: Optional[float] = None # placeholder
    yield_xirr_pct: Optional[float] = None     # YTM бумаги (XIRR на проекции купонов по форварду), %
    index_yield_pct: Optional[float] = None    # YTM роллирования RUONIA на тот же срок, % (база для всех флоатеров)
    disc_margin_bps: Optional[int] = None      # наш discount margin (Fabozzi)
    yield_over_index_bps: Optional[int] = None # IRR бумаги − доходность роллирования индекса, bps
    price_implausible: bool = False            # цена → гарант. убыток (стейл/тонкая), спреды скрыты
    price_thin: bool = False                    # 0 сделок сегодня → цена несвежая, DM/z с ненадёжной цены
    price_stale: bool = False                   # показана prev-close (нет live/сделки сегодня), не текущая
    z_model_bps: Optional[int] = None  # наш z-спред над КБД ОФЗ
    rating: Optional[str] = None
    # флоатер-метрики (кросс-секция — по всему юниверсу; refix — только watch)
    spread_dur_yrs: Optional[float] = None     # ≈ срок до погашения (лет) = spread duration
    days_to_refix: Optional[int] = None        # дни до следующего рефиксинга (watch)
    current_coupon_pct: Optional[float] = None # зафикс. ставка текущего купона, % (watch)
    # ГОРИЗОНТ ПРАЙСИНГА по правилу цены: "put" (цена ниже цены пут-выкупа),
    # "call" (цена выше цены call-выкупа), иначе "maturity". В ОТЛИЧИЕ от
    # BondValuation, цено-зависимые поля ЭТОГО объекта (yield_xirr_pct /
    # index_yield_pct / dm_bps / disc_margin_bps / yield_over_index_bps и Y-IDX
    # стакана) посчитаны К ВЫБРАННОМУ ГОРИЗОНТУ — витрина сравнивает бумаги по
    # той цифре, к которой рынок их реально прайсит.
    preferred_horizon: str = "maturity"
    # Маркер p/c у даты погашения в таблице. Два НЕЗАВИСИМЫХ факта из разных
    # источников: offer_date/offer_kind — ближайшая будущая оферта из MOEX
    # bondization (kind практически всегда 'put': MOEX в offertype колл не
    # различает); has_call — есть ли у эмитента call-опцион, из corpbonds через
    # реестр, БЕЗ даты. Могут быть оба сразу. has_call=None — не знаем (бумага
    # ещё не скрейплена), False — колла нет.
    offer_date: Optional[date] = None
    offer_kind: Optional[str] = None               # 'put' | 'call'
    has_call: Optional[bool] = None
    # Статичные признаки выпуска для быстрых фильтров витрины (не цено-зависимые,
    # в WS-патч не уходят). is_ofz — суверен Минфина (субфеды сюда НЕ попадают),
    # has_amort — в графике MOEX больше одного транша погашения номинала.
    is_ofz: bool = False
    has_amort: bool = False
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
    implied_avg_pct: Optional[float] = None  # avg по листу (par-based, годовые купоны)
    forward_pct: Optional[float] = None      # форвард телескопированием avg (чистая база)
    fwd_span: Optional[str] = None           # подпись окна форварда: "3m3m", "1Y1Y"

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
    quantity: Optional[int] = None   # None — синтетический уровень лестницы (нет заявки)
    yield_pct: Optional[float] = None
    sm_bps: Optional[int] = None
    dm_bps: Optional[int] = None       # флоатеры: дисконт-маржа (вспом.)
    y_idx_bps: Optional[int] = None    # флоатеры: IRR − роллирование RUONIA — ПЕРВИЧНАЯ метрика
    g_spread_bps: Optional[int] = None # фиксы: g-спред к КБД
    horizon: Optional[str] = None      # к чему посчитан уровень: maturity | put | call

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

