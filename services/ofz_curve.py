"""Своя кривая ОФЗ: Нельсон–Сигел–Свенссон по точкам выпусков (YTM × дюрация
Маколея), см. docs/ofz_curve_tz.md.

Зачем своя, когда есть КБД МосБиржи: КБД — zero-кривая по своей методике и со
своим набором бумаг, и отклонение выпуска от неё смешивает «дёшево/дорого» с
разницей методик (YTM vs zero, купон, дюрация). Кривая, проведённая сквозь те
же точки, что лежат на графике, отвечает на вопрос страницы напрямую: где
выпуск сидит относительно СВОИХ соседей.

Только numpy — scipy нет в requirements, а прод живёт в 768 МиБ. Поэтому
нелинейность (λ1, λ2) перебирается по сетке с локальным уточнением, а при
фиксированных λ β — взвешенный линейный МНК (lstsq). Робастность — IRLS с
весами Хьюбера: одна дорогая/дешёвая бумага не должна тащить кривую.

Модуль ЧИСТЫЙ: ни сети, ни базы, ни кэша — всё это в ручке
(api/routes/fixed.py, /ofz/curve). Тесты — tests/test_ofz_curve.py.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

# ── параметры подгонки (по ТЗ) ──
# сетка λ: λ1 — «горб» на коротком/среднем участке, λ2 — второй горб NSS
# на длинном; λ2 ≥ 2.5·λ1, иначе два горба сливаются и матрица вырождается
LAMBDA1_GRID = np.logspace(math.log10(0.3), math.log10(6.0), 24)
LAMBDA2_GRID = np.logspace(math.log10(2.0), math.log10(25.0), 16)
LAMBDA2_MIN_RATIO = 2.5     # при близких λ f2(λ1) и f2(λ2) почти совпадают
# после сетки — уточнение по каждой λ с уменьшающимся мультипликативным шагом
# (шаг сетки ≈14 %, без уточнения синтетика с λ между узлами не сходилась в
# 1 бп)
REFINE_STEPS = (1.07, 1.03, 1.01)

MIN_POINTS = 4              # меньше — подгонки нет (ручка отдаёт 422)
NSS_MIN_POINTS = 8          # с меньшим числом точек четвёртый β не оправдан
NSS_MIN_GAIN = 0.05         # NSS должен снять ≥5 % RMSE относительно NS
IRLS_ITERS = 3
HUBER_K = 1.5               # c = 1.5·σ_robust
WEIGHT_FLOOR = 0.1          # выброс не выкидываем, но и рулить не даём
SIGMA_FLOOR_PCT = 0.005     # 0.5 бп: на идеальной синтетике MAD→0 и веса шумели бы
# Коллинеарность базисов: на коротком облаке (у ОФЗ при 16 % дюрации ≤ 7 лет)
# факторы NSS почти совпадают с константой, и голый lstsq находил β0 = −670,
# β3 = +1688 при той же RMSE — кривая внутри domain та же, а за ним улетала в
# минус (key_tenors 10Y/15Y). Лечение двойное: узлы сетки λ, где решение
# выходит за экономические границы β, отбраковываются; плюс ридж на β1..β3
# (β0 — уровень, свободен) — среди почти равных по SSE решений берётся малое
# по норме, т.е. хвост за облаком сжимается к плоскому уровню, а не к
# случайному горбу. Штраф = RIDGE_C·Σw·σ̂², где σ̂ — робастная σ остатков
# прошлой итерации IRLS: он ЖИВЁТ В МАСШТАБЕ ШУМА — на рынке (σ̂ ≈ 20 бп)
# сжимает, на точной синтетике (σ̂ → пол) исчезает и β восстанавливаются
# точно. Ридж в абсолютных единицах β этого не умел: либо гнул синтетику,
# либо не держал рынок.
BETA0_RANGE = (0.0, 30.0)   # уровень, % годовых
BETA_ABS_MAX = 30.0         # |β1|, |β2|, |β3|
RIDGE_C = 1e-3

TAU_MIN = 0.25              # короче — цена/НКД шумят сильнее доходности
YTM_RANGE = (0.0, 40.0)     # вне диапазона — мусор в метриках, не рынок
KEY_TENORS = (0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0)
SAMPLE_STEP = 0.05
SAMPLE_START = 0.1
SAMPLE_TAIL = 0.5           # линия чуть дальше последней точки


@dataclass
class FitPoint:
    """Одна бумага на входе: YTM (%), τ = дюрация Маколея (лет), оборот дня (₽)
    как мера надёжности цены (для bid/ask — котировки)."""
    isin: str
    ytm: Optional[float]
    tau: Optional[float]
    turnover: float = 0.0


@dataclass
class FitResult:
    method: str                      # "NSS" | "NS"
    params: dict                     # beta0..beta3, lambda1, lambda2 (у NS beta3=0, lambda2=None)
    n_used: int
    n_total: int
    rmse_bps: float                  # по использованным точкам, без весов
    domain: list                     # [τmin, τmax] использованных точек
    samples: list = field(default_factory=list)      # [{years, yield_pct}]
    key_tenors: list = field(default_factory=list)   # [{years, yield_pct, extrap}]
    residuals: dict = field(default_factory=dict)    # {isin: {bps, weight, used}}

    def to_dict(self) -> dict:
        return {"method": self.method, "params": self.params, "n_used": self.n_used,
                "n_total": self.n_total, "rmse_bps": self.rmse_bps, "domain": self.domain,
                "samples": self.samples, "key_tenors": self.key_tenors,
                "residuals": self.residuals}


# ───────────────────────────── модель ─────────────────────────────

def _f1(tau: np.ndarray, lam: float) -> np.ndarray:
    x = np.asarray(tau, dtype=float) / lam
    return (1.0 - np.exp(-x)) / x


def _f2(tau: np.ndarray, lam: float) -> np.ndarray:
    x = np.asarray(tau, dtype=float) / lam
    return _f1(tau, lam) - np.exp(-x)


def _design(tau: np.ndarray, lam1: float, lam2: Optional[float]) -> np.ndarray:
    cols = [np.ones_like(tau, dtype=float), _f1(tau, lam1), _f2(tau, lam1)]
    if lam2 is not None:
        cols.append(_f2(tau, lam2))
    return np.column_stack(cols)


def evaluate(params: dict, tau) -> np.ndarray:
    """y(τ), % годовых, по параметрам FitResult.params. τ — число или массив.
    Для будущих потребителей (аукционы: премия к кривой) — та же формула, что
    у samples, чтобы число на другой странице не разошлось с линией."""
    t = np.atleast_1d(np.asarray(tau, dtype=float))
    lam1 = float(params["lambda1"])
    lam2 = params.get("lambda2")
    y = (float(params["beta0"]) + float(params["beta1"]) * _f1(t, lam1)
         + float(params["beta2"]) * _f2(t, lam1))
    b3 = params.get("beta3") or 0.0
    if lam2 is not None and b3:
        y = y + float(b3) * _f2(t, float(lam2))
    return y if np.ndim(tau) else float(y[0])


# ───────────────────────────── веса ─────────────────────────────

def point_weight(turnover_rub: Optional[float]) -> float:
    """Стартовый вес точки по обороту дня: неликвид считается, но не рулит.
    0.25 при нулевом обороте, 1 при ~30 млн, потолок 2 при ~1 млрд."""
    v = max(float(turnover_rub or 0.0), 0.0)
    return float(np.clip(0.25 + math.log10(1.0 + v / 1e6) / 2.0, 0.25, 2.0))


def _robust_sigma(resid: np.ndarray) -> float:
    """σ остатков по MAD (выброс её не раздувает), не ниже пола."""
    mad = float(np.median(np.abs(resid - np.median(resid))))
    return max(1.4826 * mad, SIGMA_FLOOR_PCT)


def _huber_weights(resid: np.ndarray, sigma: float) -> np.ndarray:
    """Веса Хьюбера от остатков: |r| ≤ c — 1, дальше c/|r|, не ниже пола."""
    c = HUBER_K * sigma
    a = np.abs(resid)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(a <= c, 1.0, c / np.maximum(a, 1e-12))
    return np.clip(w, WEIGHT_FLOOR, 1.0)


# ───────────────────────────── подгонка ─────────────────────────────

def _wls(x: np.ndarray, y: np.ndarray, w: np.ndarray, ridge: float) -> tuple:
    """Взвешенный МНК с риджем на β1..β3: (β, целевая = Σw·r² + ridge·Σβ²).
    Штраф — в целевой, чтобы перебор λ сравнивал узлы той же меркой."""
    sw = np.sqrt(w)
    k = x.shape[1]
    a = math.sqrt(ridge)
    xa = np.vstack([x * sw[:, None], a * np.eye(k)[1:]])   # β0 без штрафа
    ya = np.concatenate([y * sw, np.zeros(k - 1)])
    beta, *_ = np.linalg.lstsq(xa, ya, rcond=None)
    r = y - x @ beta
    return beta, float(np.sum(w * r * r) + a * a * np.sum(beta[1:] ** 2))


def _beta_ok(beta: np.ndarray) -> bool:
    """Экономические границы β: уровень в [0, 30] %, наклон/горбы по модулю
    ≤ 30 пп. Решение вне них — артефакт коллинеарности, не рынок."""
    return bool(BETA0_RANGE[0] <= beta[0] <= BETA0_RANGE[1]
                and np.all(np.abs(beta[1:]) <= BETA_ABS_MAX))


def _best_lambdas(tau, y, w, nss: bool, ridge: float) -> tuple:
    """Перебор λ по сетке (+ локальное уточнение) при фиксированных весах.
    Возвращает (lam1, lam2|None, β)."""
    best = (math.inf, None, None, None)

    def _try(l1, l2):
        nonlocal best
        if l2 is not None and l2 < LAMBDA2_MIN_RATIO * l1:
            return
        beta, sse = _wls(_design(tau, l1, l2), y, w, ridge)
        if sse < best[0] and _beta_ok(beta):
            best = (sse, l1, l2, beta)

    for l1 in LAMBDA1_GRID:
        if nss:
            for l2 in LAMBDA2_GRID:
                _try(float(l1), float(l2))
        else:
            _try(float(l1), None)
    # уточнение: координатный спуск по log λ с убывающим шагом; сетка узкая
    # только для «где-то горб», точный λ между узлами сдвигал бы RMSE на бп
    if best[1] is None:
        return None, None, None     # ни одного узла в границах β
    for step in REFINE_STEPS:
        for _ in range(4):
            _, l1, l2, _ = best
            moved = False
            for f in (step, 1.0 / step):
                before = best[0]
                _try(l1 * f, l2)
                if l2 is not None:
                    _try(l1, l2 * f)
                if best[0] < before:
                    moved = True
            if not moved:
                break
    _, l1, l2, beta = best
    return l1, l2, beta


def _fit_model(tau, y, w0, nss: bool) -> tuple:
    """IRLS: стартовые веса по обороту, потом IRLS_ITERS переоценок Хьюбером.
    Возвращает (params, rmse_pct, финальные веса) или None, если ни один узел
    сетки не дал решения в границах β."""
    w = w0.copy()
    l1 = l2 = beta = None
    # первая итерация — почти без риджа (σ̂ ещё не известна): границы β уже
    # держат решение в коробке, σ̂ с её остатков задаёт штраф дальше
    ridge = RIDGE_C * float(w.sum()) * SIGMA_FLOOR_PCT ** 2
    for it in range(IRLS_ITERS + 1):
        l1, l2, beta = _best_lambdas(tau, y, w, nss, ridge)
        if beta is None:
            return None
        resid = y - _design(tau, l1, l2) @ beta
        if it < IRLS_ITERS:
            sigma = _robust_sigma(resid)
            w = w0 * _huber_weights(resid, sigma)
            ridge = RIDGE_C * float(w.sum()) * sigma ** 2
    resid = y - _design(tau, l1, l2) @ beta
    rmse = float(np.sqrt(np.mean(resid * resid)))
    params = {"beta0": float(beta[0]), "beta1": float(beta[1]), "beta2": float(beta[2]),
              "beta3": float(beta[3]) if nss else 0.0,
              "lambda1": float(l1), "lambda2": float(l2) if nss else None}
    return params, rmse, w


def _usable(p: FitPoint) -> bool:
    if p.tau is None or p.ytm is None:
        return False
    try:
        t, y = float(p.tau), float(p.ytm)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(t) and math.isfinite(y) and t >= TAU_MIN
            and YTM_RANGE[0] <= y <= YTM_RANGE[1])


def fit(points: Iterable[FitPoint]) -> FitResult:
    """Подгонка кривой по точкам. ValueError, если использовать можно < 4.

    Исключённые точки (used=false) в подгонке не участвуют, но остаток к
    кривой у них всё равно считается — таблица показывает отклонение всем,
    у кого есть τ и YTM."""
    pts = list(points)
    used = [p for p in pts if _usable(p)]
    if len(used) < MIN_POINTS:
        raise ValueError(f"для подгонки нужно ≥{MIN_POINTS} точек, есть {len(used)}")
    tau = np.array([float(p.tau) for p in used])
    y = np.array([float(p.ytm) for p in used])
    w0 = np.array([point_weight(p.turnover) for p in used])

    ns = _fit_model(tau, y, w0, nss=False)
    if ns is None:
        raise ValueError("ни одна кривая NS не уложилась в границы параметров")
    params, rmse, w = ns
    method = "NS"
    if len(used) >= NSS_MIN_POINTS:
        nss = _fit_model(tau, y, w0, nss=True)
        # лишний параметр оправдан только заметным выигрышем — иначе NSS
        # подгоняет шум и «горбит» хвост между редкими длинными точками
        if nss is not None and nss[1] < (1.0 - NSS_MIN_GAIN) * rmse:
            params, rmse, w = nss
            method = "NSS"

    tmin, tmax = float(tau.min()), float(tau.max())
    grid = np.arange(SAMPLE_START, tmax + SAMPLE_TAIL + SAMPLE_STEP / 2, SAMPLE_STEP)
    ys = evaluate(params, grid)
    samples = [{"years": round(float(t), 2), "yield_pct": round(float(v), 4)}
               for t, v in zip(grid, ys)]
    key = [{"years": t, "yield_pct": round(float(evaluate(params, t)), 4),
            "extrap": not (tmin <= t <= tmax)} for t in KEY_TENORS]

    residuals: dict = {}
    wmap = {p.isin: float(wi) for p, wi in zip(used, w)}
    for p in pts:
        is_used = p.isin in wmap
        bps = None
        if p.tau is not None and p.ytm is not None:
            try:
                t, yy = float(p.tau), float(p.ytm)
                if math.isfinite(t) and math.isfinite(yy) and t > 0:
                    bps = round((yy - evaluate(params, t)) * 100.0, 2)
            except (TypeError, ValueError):
                bps = None
        residuals[p.isin] = {"bps": bps,
                             "weight": round(wmap[p.isin], 3) if is_used else 0.0,
                             "used": is_used}

    return FitResult(
        method=method,
        params={k: (round(v, 6) if isinstance(v, float) else v) for k, v in params.items()},
        n_used=len(used), n_total=len(pts),
        rmse_bps=round(rmse * 100.0, 2),
        domain=[round(tmin, 3), round(tmax, 3)],
        samples=samples, key_tenors=key, residuals=residuals,
    )


def input_fingerprint(points: Iterable[FitPoint]) -> str:
    """Отпечаток ВСЕГО входа — ключ кэша ручки (правило проекта: в ключе весь
    вход, иначе кэш отдаёт кривую от прошлых цен)."""
    rows = sorted(
        (p.isin,
         None if p.ytm is None else round(float(p.ytm), 4),
         None if p.tau is None else round(float(p.tau), 3))
        for p in points)
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]
