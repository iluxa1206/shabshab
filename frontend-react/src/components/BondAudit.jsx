import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchBondAudit, fetchCouponDays } from "../api.js";
import { fmt, baseLabel } from "../format.js";
import { InstrumentForm } from "./AdminPanel.jsx";
import IsinCopy from "./IsinCopy.jsx";

// Паспорт бумаги: страница верификации расчётов. Каждая цифра карточки —
// откуда взялась (источник + давность), как посчиталась (спека по слоям,
// по-купонный бэктест, waterfall PV) и сходится ли (светофор чеков).

const ST_LABEL = { ok: "OK", warn: "WARN", bad: "BAD", info: "INFO", na: "—" };

function CheckRow({ c }) {
  return (
    <div className={"audit-check st-" + c.status}>
      <span className="ac-badge">{ST_LABEL[c.status] || c.status}</span>
      <span className="ac-label">{c.label}</span>
      <span className="ac-detail">{c.detail}</span>
    </div>
  );
}

function KV({ k, v, title }) {
  return (
    <div className="ref-cell" title={title}>
      <div className="ref-k">{k}</div>
      <div className="ref-v">{v == null || v === "" ? <span className="muted">—</span> : v}</div>
    </div>
  );
}

// ISO-таймстемп → «дд.мм.гггг чч:мм UTC»
const ts = (s) => {
  if (!s) return null;
  const d = s.slice(0, 10).split("-").reverse().join(".");
  const t = s.length > 11 ? s.slice(11, 16) : "";
  return t ? `${d} ${t} UTC` : d;
};

const SRC_LABEL = {
  cbonds: "Cbonds-выгрузка", nrd_frozen: "замороженный seed", moex: "MOEX",
  manual: "ручной ввод", corpbonds: "corpbonds", "manual/db": "ручной/реестр",
  parser: "парсер проспекта", calibrator: "калибратор (история купонов)",
  default: "дефолт (point, lag 0)", none: "нет",
};

function SpecSection({ spec, backtest, base }) {
  const [showText, setShowText] = useState(false);
  const eff = spec.effective || {};
  const L = spec.layers || {};
  const w = eff.avg_window_days;
  const modeLbl = (m) => m === "point" ? "average · окно 1 (точечный фиксинг)"
    : m === "average" ? (w === 1 ? "average · окно 1 день (точечный фиксинг)"
      : w ? `average · окно ${w} дн` : "average (среднее по периоду)")
    : m === "avg_prev" ? "avg_prev (среднее пред. периода)" : m;
  const specStr = (s) => s == null ? "—"
    : `${s.mode ?? s.coupon_mode ?? "?"} · lag ${s.lag ?? s.fixing_lag ?? "?"}${(s.lag_unit ?? s.fixing_lag_unit) === "work" ? " раб." : ""}`
      + (s.err_pp != null ? ` · ошибка фита ${s.err_pp} пп` : "")
      + (s.cap_pct != null ? ` · кэп ${s.cap_pct}%` : "")
      + (s.floor_pct != null ? ` · пол ${s.floor_pct}%` : "");

  return (
    <>
      <div className="section-title">Спека фиксинга купона</div>
      <div className="ref-grid">
        <KV k="Режим (effective)" v={modeLbl(eff.coupon_mode) || "не определён"} />
        <KV k="Лаг фиксинга" v={eff.fixing_lag != null ? eff.fixing_lag + (eff.fixing_lag_unit === "work" ? " раб. дн" : " кал. дн") : null} />
        <KV k="Источник режима" v={SRC_LABEL[spec.sources?.mode] || spec.sources?.mode} />
        <KV k="Источник лага" v={SRC_LABEL[spec.sources?.lag] || spec.sources?.lag} />
        <KV k="Кэп / пол ставки" v={(eff.cap_pct != null || eff.floor_pct != null)
          ? `${eff.cap_pct != null ? "≤ " + eff.cap_pct + "%" : ""} ${eff.floor_pct != null ? "≥ " + eff.floor_pct + "%" : ""}`.trim()
          : "нет"} />
        <KV k="База · маржа" v={`${baseLabel(base)} ${eff.margin_bps != null ? "+" + eff.margin_bps + " bps" : ""}`} />
      </div>
      <div className="fnote">
        Слои (приоритет: ручной &gt; парсер &gt; калибратор):
        {" ручной/реестр = "}{specStr(L.manual)};
        {" парсер проспекта = "}{specStr(L.parser)};
        {" калибратор = "}{specStr(L.calibrator)}.
      </div>
      {spec.coupon_text && (
        <div className="audit-text">
          <button className="btn" onClick={() => setShowText((v) => !v)}>
            {showText ? "скрыть" : "показать"} текст формулы из проспекта
          </button>
          {showText && <pre className="audit-pre">{spec.coupon_text}</pre>}
        </div>
      )}

      <div className="section-title">
        Бэктест спеки: пересчёт прошлых купонов по истории {baseLabel(base)}
        {backtest.fix_prelude > 0 ? ` · срезано ${backtest.fix_prelude} фикс-купонов прелюдии` : ""}
      </div>
      {(backtest.rows || []).length === 0 ? (
        <div className="fnote">Нет прошлых зафиксированных купонов для проверки.</div>
      ) : (
        <table className="cf-table">
          <thead>
            <tr>
              <th className="left">Период</th><th>Дней</th>
              <th>Факт ставка %</th><th>Наша спека %</th><th>Δ пп</th>
            </tr>
          </thead>
          <tbody>
            {backtest.rows.map((r, i) => (
              <tr key={i}>
                <td className="left">{fmt.date(r.start)} — {fmt.date(r.end)}</td>
                <td>{r.days}</td>
                <td>{fmt.pct(r.observed_pct, 4)}</td>
                <td>{r.skipped ? <span className="muted" title={r.skipped}>пропуск</span> : fmt.pct(r.predicted_pct, 4)}</td>
                <td className={r.err_pp != null && Math.abs(r.err_pp) > 0.15 ? "neg" : ""}>
                  {r.err_pp != null ? fmt.signed(r.err_pp, 4) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {backtest.n > 0 && (
        <div className="fnote">
          {backtest.n} куп.: средняя |ошибка| {backtest.mean_err_pp} пп, максимум {backtest.max_err_pp} пп.
          Ошибка ≈ 0 доказывает, что режим/лаг/маржа согласованы с реальными выплатами
          (&lt;0.15 OK, &gt;0.5 — спека почти наверняка неверна).
        </div>
      )}
    </>
  );
}

function RegistrySection({ r }) {
  if (!r) return (
    <>
      <div className="section-title">Реестр инструментов</div>
      <div className="warn-box">Бумаги нет в реестре — параметры только из Cbonds/MOEX-кэша.</div>
    </>
  );
  const en = r.enrich;
  return (
    <>
      <div className="section-title">Реестр инструментов (спарсенное сырьё)</div>
      <div className="ref-grid">
        <KV k="Источник параметров" v={SRC_LABEL[r.source] || r.source} />
        <KV k="Обновлено" v={ts(r.updated_at)} />
        <KV k="Впервые увидена" v={ts(r.first_seen)} />
        <KV k="Ручная заморозка" v={r.manual_locked ? "ДА — sync/парсер не правят поля" : "нет"} />
        <KV k="База · маржа" v={`${r.base || "—"} ${r.margin_bps != null ? "+" + r.margin_bps + " bps" : ""}`} />
        <KV k="Погашение / размещение" v={`${fmt.date(r.maturity_date) || "—"} / ${fmt.date(r.issue_date) || "—"}`} />
        <KV k="Период · куп/год" v={`${r.coupon_period_days || "—"} дн · ${r.coupons_per_year || "—"}×`} />
        <KV k="Номинал · day count" v={`${r.face_value ?? "—"} · ${r.day_count || "—"}`} />
        <KV k="coupon_mode / lag (БД)" v={r.coupon_mode || r.fixing_lag != null
          ? `${r.coupon_mode ?? "—"} / ${r.fixing_lag ?? "—"}${r.fixing_lag_unit === "work" ? " раб." : ""}`
          : "не заданы (возьмётся парсер/калибратор)"} />
        <KV k="Кэп / пол (БД)" v={r.cap_pct != null || r.floor_pct != null ? `${r.cap_pct ?? "—"} / ${r.floor_pct ?? "—"}` : "нет"} />
        <KV k="Рейтинг" v={r.rating} />
        <KV k="margin_check_pp" v={r.margin_check_pp != null ? fmt.signed(r.margin_check_pp, 2) + " пп" : null}
          title="бэк-аут маржи из последнего купона vs факт индекса; |>1.5| = подозрение" />
        <KV k="corpbonds-обогащение" v={en ? `${en.result} · ${ts(en.attempted_at)} · парсер v${en.parser_ver ?? "?"}` : "не пробовалось"} />
        <KV k="Ревью" v={r.reviewed ? "да" : "НЕТ — новая, параметры не проверены"} />
      </div>
    </>
  );
}

function MarketSection({ m, v }) {
  const idx = m.index;
  return (
    <>
      <div className="section-title">Рынок и входные данные</div>
      <div className="ref-grid">
        <KV k="Цена (live)" v={m.last_price_pct != null ? fmt.pct(m.last_price_pct) + " % · " + (m.price_source || "") : "нет"} />
        <KV k="Цена сессии / prev close" v={`${m.session_price_pct != null ? fmt.pct(m.session_price_pct) + " %" : "—"} / ${m.prev_close_pct != null ? fmt.pct(m.prev_close_pct) + " %" : "—"}`} />
        <KV k="НКД MOEX (в расчёте)" v={m.accrued_moex_rub != null ? fmt.num(m.accrued_moex_rub) + " ₽" : null} />
        <KV k="НКД наш кэш" v={m.accrued_cache_rub != null ? fmt.num(m.accrued_cache_rub) + " ₽" : null} />
        <KV k="calc_date / rates_date" v={`${fmt.date(m.calc_date) || "—"} / ${fmt.date(m.rates_date) || "—"}`} />
        <KV k="Снято" v={ts(m.fetched_at)} />
        {idx && <KV k={`История ${baseLabel(idx.base)}`}
          v={`${fmt.pct(idx.last_value_pct)} % на ${fmt.date(idx.last_date)} (${idx.age_days} дн назад) · ${idx.n_points} точек с ${fmt.date(idx.first_date)}`} />}
        {v.pricing_status && <KV k="Статус оценки" v={v.pricing_status} />}
      </div>
    </>
  );
}

// Окно дневной раскладки фиксинга: ВСЕ неистёкшие купоны одним прокручиваемым
// списком — по каждому дню ставка индекса (факт ЦБ / форвард-ступень кривой).
export function DayRatesModal({ isin, onClose }) {
  const q = useQuery({
    queryKey: ["coupon-days", isin],
    queryFn: () => fetchCouponDays(isin),
    staleTime: 60_000,
  });
  const d = q.data;
  const modeLbl = { average: "average · дни дохода (start, end]", avg_prev: "avg_prev · окно пред. периода",
    point: "point · один фиксинг", month_start: "month_start · 1-е число месяца" };
  return (
    <div className="daymodal-overlay" onClick={onClose}>
      <div className="daymodal" onClick={(e) => e.stopPropagation()}>
        <div className="daymodal-head">
          <b>Фиксинг по дням · все будущие купоны{d ? ` (${d.coupons.length} куп. · ${d.n_days} дн)` : ""}</b>
          <button className="btn" onClick={onClose}>ЗАКРЫТЬ</button>
        </div>
        {q.isError && <div className="warn-box">Ошибка: {q.error?.message}</div>}
        {!d && !q.isError && <div className="loading">ЗАГРУЗКА</div>}
        {d && (
          <>
            <div className="fnote" style={{ marginTop: 0 }}>
              {modeLbl[d.spec?.mode] || d.spec?.mode} · лаг {d.spec?.lag}{d.spec?.lag_unit === "work" ? " раб." : " кал."} дн ·
              маржа {d.spec?.margin_bps != null ? "+" + d.spec.margin_bps + " bps" : "—"}
              {d.spec?.cap_pct != null && <> · кэп {d.spec.cap_pct}%</>}
              {d.spec?.floor_pct != null && <> · пол {d.spec.floor_pct}%</>}
              {" · Close/R-spread — из spread_daily (та же серия, что график «Динамика DM»): сверка с историческим калькулятором спредов"}
            </div>
            <div className="daymodal-body">
              <table className="cf-table">
                <thead>
                  <tr>
                    <th className="left">День</th><th className="left">Наблюдение</th><th>Ставка %</th>
                    <th title="расчётный индекс базы: старт 1.0 в начале периода, фиксинг дня даёт прирост следующего; капитализация по рабочим дням, выходные простыми">Индекс</th>
                    <th className="left">Источник</th><th>Close %</th><th>R-spread</th>
                  </tr>
                </thead>
                <tbody>
                  {d.coupons.map((g) => (
                    [
                      <tr key={"h" + g.n} className="daygroup">
                        <td className="left" colSpan={7}>
                          Купон #{g.n} · {fmt.date(g.start)} — {fmt.date(g.end)} · выплата {fmt.date(g.pay_date)} ·
                          факт {g.n_fact}/{g.rows.length} дн · среднее {fmt.pct(g.mean_pct, 4) ?? "—"}%
                          {g.coupon_rate_pct != null && <> · купон {fmt.pct(g.coupon_rate_pct, 4)}%</>}
                          {g.index_end != null && <> · индекс {g.index_start?.toFixed(8).replace(".", ",")} → {g.index_end.toFixed(8).replace(".", ",")}
                            {g.index_rate_pct != null && <> ({fmt.pct(g.index_rate_pct, 4)}% годовых за период)</>}</>}
                          {g.projected_pct != null && g.mean_pct != null && g.projected_pct !== g.mean_pct
                            && <span className="neg"> · прайсинг {fmt.pct(g.projected_pct, 4)}% — РАСХОЖДЕНИЕ</span>}
                        </td>
                      </tr>,
                      ...g.rows.map((r, i) => (
                        <tr key={g.n + "-" + i} className={r.src === "forward" ? "" : "past"}>
                          <td className="left">{fmt.date(r.day)}</td>
                          <td className="left">{fmt.date(r.obs_date)}</td>
                          <td>{r.rate_pct != null ? fmt.pct(r.rate_pct, 4) : "—"}</td>
                          <td className="mono-idx">{r.index != null ? r.index.toFixed(8).replace(".", ",") : "—"}</td>
                          <td className="left">{r.src === "fact" ? "факт ЦБ" : "форвард (ступень)"}</td>
                          <td>{r.close_pct != null ? fmt.pct(r.close_pct) : "—"}</td>
                          <td>{r.y_idx_bps != null ? fmt.bps(r.y_idx_bps) : "—"}</td>
                        </tr>
                      )),
                    ]
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function WaterfallSection({ w, v, isin }) {
  const [showDays, setShowDays] = useState(false);
  const rows = w.rows || [];
  // дневная раскладка фиксинга НЕ зависит от цены — кнопка доступна и когда
  // PV-развёртки нет (нет цены/кривой: выходной, тонкая бумага)
  if (!rows.length) return (
    <>
      <div className="section-title">
        Развёртка PV
        <button className="btn day-btn" onClick={() => setShowDays(true)}
          title="Базовая ставка на каждый день всех будущих купонов">ФИКСИНГ ПО ДНЯМ</button>
      </div>
      <div className="fnote">Нет цены/кривой — PV-развёртка недоступна; дневная раскладка фиксинга работает.</div>
      {showDays && <DayRatesModal isin={isin} onClose={() => setShowDays(false)} />}
    </>
  );
  return (
    <>
      <div className="section-title">
        Развёртка PV: дисконтирование на решённом XIRR
        <button className="btn day-btn" onClick={() => setShowDays(true)}
          title="Базовая ставка на каждый день всех будущих купонов">ФИКСИНГ ПО ДНЯМ</button>
      </div>
      <div className="ref-grid">
        <KV k="Dirty (факт)" v={fmt.num(w.dirty_price_rub) + " ₽"} />
        <KV k="Σ PV потоков" v={fmt.num(w.pv_sum_rub) + " ₽"} />
        <KV k="Δ (обязана ≈ 0)" v={fmt.signed(w.pv_gap_rub, 2) + " ₽"} />
        <KV k="YTM (XIRR)" v={fmt.pct(w.yield_pct) + " %"} />
        <KV k="SM / DM" v={`${fmt.bps(v.sm_bps ?? v.dm_bps) ?? "—"} / ${fmt.bps(v.disc_margin_bps) ?? "—"} bps`} />
        <KV k="RUONIA-ролл · R-spread" v={`${fmt.pct(v.index_yield_pct) ?? "—"} % · ${fmt.bps(v.yield_over_index_bps) ?? "—"} bps`} />
      </div>
      <div style={{ maxHeight: 380, overflow: "auto" }}>
        <table className="cf-table">
          <thead>
            <tr>
              <th className="left">#</th><th className="left">Выплата</th><th className="left">Тип</th>
              <th>База %</th><th>Купон %</th><th>Сумма ₽</th><th>t, лет</th><th>DF</th><th>PV ₽</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => (
              <tr key={i} className={c.type === "REDEMPTION" ? "redemption" : c.pv_rub == null ? "past" : ""}>
                <td className="left">{c.number}</td>
                <td className="left">{fmt.date(c.payment_date)}</td>
                <td className="left">{c.type === "REDEMPTION" ? "погаш." : "купон"}</td>
                <td>{fmt.pct(c.base_rate_pct)}</td>
                <td>{fmt.pct(c.coupon_rate_pct)}</td>
                <td>{fmt.num(c.amount_rub)}</td>
                <td>{c.t_yrs != null ? fmt.num(c.t_yrs, 3) : "—"}</td>
                <td>{c.df != null ? fmt.num(c.df, 4) : "—"}</td>
                <td>{c.pv_rub != null ? fmt.num(c.pv_rub) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="fnote">
        Прошлые выплаты (PV —) в дисконтирование не входят. DF = (1+YTM)^−t. Σ PV
        обязана сойтись с dirty: XIRR решён именно на этих потоках; расхождение =
        рассинхрон display-cashflow с pricing-потоками. «Фиксинг по дням» — базовая
        ставка на каждый день всех будущих купонов (факт / форвард-ступень между тенорами).
      </div>
      {showDays && <DayRatesModal isin={isin} onClose={() => setShowDays(false)} />}
    </>
  );
}

function ScheduleSection({ s }) {
  const [show, setShow] = useState(false);
  return (
    <>
      <div className="section-title">Сырой график MOEX (bondization)</div>
      <div className="fnote">
        Купонов {s.n_coupons}, амортизаций {(s.amorts || []).length}, оферт {(s.offers || []).length}.
        {" "}
        <button className="btn" onClick={() => setShow((v) => !v)}>{show ? "скрыть" : "показать"} JSON</button>
      </div>
      {show && <pre className="audit-pre">{JSON.stringify(s, null, 2)}</pre>}
    </>
  );
}

export default function BondAudit() {
  const { isin } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const q = useQuery({
    queryKey: ["bond-audit", isin],
    queryFn: () => fetchBondAudit(isin),
    enabled: !!isin,
    staleTime: 60_000,
  });
  const d = q.data;
  const regName = d?.registry?.short_name;

  // правка сохранена → пересобрать паспорт (бэктест/чеки/раскладку) сразу,
  // не гоняя пользователя в Справочник и обратно
  const onSaved = () => {
    setEditing(false);
    qc.invalidateQueries({ queryKey: ["bond-audit", isin] });
    qc.invalidateQueries({ queryKey: ["coupon-days", isin] });
    qc.invalidateQueries({ queryKey: ["admin", "catalog"] });
    q.refetch();
  };

  return (
    <div className="audit-page">
      <div className="audit-head">
        <button className="btn" onClick={() => navigate(-1)}>← НАЗАД</button>
        <h2>ПАСПОРТ · {regName || isin}</h2>
        <IsinCopy isin={isin} className="isin-copy-inl mono muted" />
        {d && <span className="muted audit-gen">собрано {ts(d.generated_at)}</span>}
        <button className="btn" onClick={() => q.refetch()} disabled={q.isFetching}>
          {q.isFetching ? "СБОР…" : "ПЕРЕСОБРАТЬ"}
        </button>
        <button className={"btn" + (editing ? " on" : "")} onClick={() => setEditing((v) => !v)}
          title="Править параметры бумаги прямо здесь — паспорт пересоберётся после сохранения">
          {editing ? "× ЗАКРЫТЬ ПРАВКУ" : "ПРАВКА ПАРАМЕТРОВ"}
        </button>
        <Link className="btn" to={`/reference?isin=${isin}`}
          title="Открыть бумагу в Справочнике (вся таблица параметров)">СПРАВОЧНИК ↗</Link>
      </div>

      {editing && (
        <div className="audit-edit">
          <div className="section-title">Правка параметров (реестр — источник истины; сохранение пересоберёт паспорт)</div>
          <InstrumentForm isin={isin} onSaved={onSaved} />
        </div>
      )}

      {q.isError && <div className="warn-box">Ошибка: {q.error?.message}</div>}
      {!d && !q.isError && <div className="loading">СБОР ДАННЫХ (сеть MOEX — до ~10с)</div>}

      {d && (
        <>
          <div className="section-title">Санити-чеки</div>
          <div className="audit-checks">
            {d.checks.map((c) => <CheckRow key={c.id} c={c} />)}
          </div>
          {d.warnings?.length > 0 && <div className="warn-box">{d.warnings.join(" · ")}</div>}

          <SpecSection spec={d.spec} backtest={d.backtest} base={d.registry?.base || d.spec?.effective?.base} />
          <RegistrySection r={d.registry} />
          <MarketSection m={d.market} v={d.valuation || {}} />
          <WaterfallSection w={d.waterfall} v={d.valuation || {}} isin={isin} />
          <ScheduleSection s={d.schedule} />
        </>
      )}
    </div>
  );
}
