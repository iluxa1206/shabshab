/**
 * Вкладка ПОРТФЕЛЬ: конструктор набора флоатеров.
 *
 * Состояние ручной правки (убрать / прикнопить / задать сумму) живёт ЗДЕСЬ и
 * уезжает в тело запроса — бэкенд stateless и ничего не помнит между сборками.
 * Параметры сборки зеркалятся в URL (?q=), чтобы набор можно было переслать и
 * открыть заново.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { buildPortfolio, fetchSignalEmitters, searchInstruments } from "../api.js";
import { copyText } from "../clipboard.js";
import { DASH, baseLabel, fmt, orDash, ratingColor, RT_FILTER } from "../format.js";

// значения — из общего списка (совпадает со screener_core.RATINGS на бэке)
const RATINGS = RT_FILTER;
const ISSUERS = [["ofz", "ОФЗ"], ["corp", "Корп"]];
const BASES = [["KEYRATE", "КС"], ["RUONIA", "RUONIA"]];
const MODES = [["spread", "Спред"], ["ladder", "Лесенка"]];

const EMPTY = {
  ratings: [], emitters: [], isins: [], issuer: "all",
  years_min: null, years_max: null, hide_subord: true,
  spread_min: null, spread_max: null,
  bases: [], no_amort: false, no_call: false, min_adv_rub: null,
  mode: "spread", n: 15, amount_rub: 100_000_000,
  max_per_emitter: 1, max_emitter_share: null, max_rating_share: null,
};

const numOrNull = (s) => {
  const v = String(s ?? "").replace(",", ".").trim();
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const mln = (rub) => (rub == null ? "" : String(rub / 1e6).replace(".", ","));
const toRub = (s) => { const v = numOrNull(s); return v == null ? null : v * 1e6; };
const toggle = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

/** Пикер с накоплением выбранного в чипы (эмитенты, отдельные бумаги). */
function MultiPicker({ label, placeholder, items, onChange, search, keyOf, labelOf, subOf }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState([]);
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!q.trim()) { setRes([]); return; }
    const t = setTimeout(async () => {
      try { setRes(await search(q)); setOpen(true); } catch { setRes([]); }
    }, 250);
    return () => clearTimeout(t);
  }, [q, search]);

  useEffect(() => {
    const onDoc = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const add = (v) => { if (!items.includes(v)) onChange([...items, v]); setQ(""); setOpen(false); };
  return (
    <div className="sig-field" ref={box}>
      <label className="sig-label">{label}</label>
      <div className="pf-pick">
        <input className="sig-input" placeholder={placeholder} value={q}
          onChange={(e) => setQ(e.target.value)} onFocus={() => res.length && setOpen(true)} />
        {open && res.length > 0 && (
          <div className="pf-pick-drop">
            {res.slice(0, 12).map((r) => (
              <button type="button" key={keyOf(r)} className="pf-pick-opt"
                onClick={() => add(keyOf(r))}>
                <span>{labelOf(r)}</span><span className="pf-pick-sub">{subOf(r)}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {items.length > 0 && (
        <div className="sig-chips pf-mt">
          {items.map((v) => (
            <button type="button" key={v} className="sig-chip on"
              onClick={() => onChange(items.filter((x) => x !== v))}>{v} ×</button>
          ))}
        </div>
      )}
    </div>
  );
}

function Num({ label, value, onChange, unit, title }) {
  return (
    <div className="sig-field" title={title}>
      <label className="sig-label">{label}</label>
      <span className="pc-input-wrap">
        <input className="pc-input" value={value ?? ""} inputMode="decimal"
          onChange={(e) => onChange(e.target.value)} />
        {unit && <span className="pc-unit">{unit}</span>}
      </span>
    </div>
  );
}

function Chips({ options, value, onChange }) {
  return (
    <div className="sig-chips">
      {options.map(([v, t]) => (
        <button type="button" key={v}
          className={"sig-chip" + (value.includes(v) ? " on" : "")}
          onClick={() => onChange(toggle(value, v))}>{t}</button>
      ))}
    </div>
  );
}

function Form({ f, set, onSubmit, busy }) {
  const searchEmitters = useCallback(
    async (q) => (await fetchSignalEmitters(q)).emitters.map((n) => ({ n })), []);
  const searchBonds = useCallback(async (q) => (await searchInstruments(q)).results, []);
  const issuers = f.issuer === "all" ? [] : [f.issuer];

  return (
    <form className="sig-form pf-form" onSubmit={(e) => { e.preventDefault(); onSubmit(); }}>
      <div className="sig-section">Какие бумаги <span>селекторы объединяются по «или»</span></div>
      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Эмитент</label>
          <Chips options={ISSUERS} value={issuers}
            onChange={(a) => set({ issuer: a.length === 1 ? a[a.length - 1] : "all" })} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Рейтинг</label>
          <Chips options={RATINGS.map((r) => [r, r])} value={f.ratings}
            onChange={(ratings) => set({ ratings })} />
        </div>
        <div className="sig-field">
          <label className="sig-label">База купона</label>
          <Chips options={BASES} value={f.bases} onChange={(bases) => set({ bases })} />
        </div>
      </div>

      <MultiPicker label="Эмитенты" placeholder="начни вводить название"
        items={f.emitters} onChange={(emitters) => set({ emitters })} search={searchEmitters}
        keyOf={(x) => x.n} labelOf={(x) => x.n} subOf={() => ""} />
      <MultiPicker label="Отдельные бумаги" placeholder="ISIN или название"
        items={f.isins} onChange={(isins) => set({ isins })} search={searchBonds}
        keyOf={(r) => r.isin} labelOf={(r) => r.name}
        subOf={(r) => r.isin + (r.rating ? " · " + r.rating : "")} />

      <div className="sig-row tight">
        <Num label="Срок от" unit="л" value={f.years_min ?? ""}
          onChange={(v) => set({ years_min: numOrNull(v) })} />
        <Num label="Срок до" unit="л" value={f.years_max ?? ""}
          onChange={(v) => set({ years_max: numOrNull(v) })} />
        <Num label="Y-IDX от" unit="бп" value={f.spread_min ?? ""}
          onChange={(v) => set({ spread_min: numOrNull(v) })} />
        <Num label="Y-IDX до" unit="бп" value={f.spread_max ?? ""}
          onChange={(v) => set({ spread_max: numOrNull(v) })} />
        <Num label="Оборот от" unit="млн" value={mln(f.min_adv_rub)}
          title="Средний дневной оборот за 30 дней. Бумаги без записей в архиве баров считаются неликвидом."
          onChange={(v) => set({ min_adv_rub: toRub(v) })} />
      </div>

      <div className="sig-row tight pf-checks">
        <label className="sig-check-line" title="Опознаём по названию (СУБ, Т1, перп): признака в реестре нет.">
          <input type="checkbox" checked={f.hide_subord}
            onChange={(e) => set({ hide_subord: e.target.checked })} />
          <span>Без субордов</span>
        </label>
        <label className="sig-check-line">
          <input type="checkbox" checked={f.no_amort}
            onChange={(e) => set({ no_amort: e.target.checked })} />
          <span>Без амортизации</span>
        </label>
        <label className="sig-check-line" title="Колл известен только из corpbonds: вид оферты MOEX не отдаёт, поэтому фильтр убирает известные, а не все.">
          <input type="checkbox" checked={f.no_call}
            onChange={(e) => set({ no_call: e.target.checked })} />
          <span>Без колла/оферты</span>
        </label>
      </div>

      <div className="sig-section">Как собрать <span>деньги делятся поровну, каждая позиция урезается стаканом</span></div>
      <div className="sig-row tight">
        <div className="sig-field">
          <label className="sig-label">Режим</label>
          <Chips options={MODES} value={[f.mode]} onChange={(a) => set({ mode: a[a.length - 1] || f.mode })} />
        </div>
        <Num label="Бумаг" value={f.n} onChange={(v) => set({ n: numOrNull(v) ?? 1 })} />
        <Num label="Сумма" unit="млн" value={mln(f.amount_rub)}
          onChange={(v) => set({ amount_rub: toRub(v) ?? 0 })} />
        <Num label="Бумаг на эмитента" value={f.max_per_emitter}
          onChange={(v) => set({ max_per_emitter: numOrNull(v) ?? 1 })} />
        <Num label="Доля эмитента" unit="%" value={f.max_emitter_share == null ? "" : f.max_emitter_share * 100}
          onChange={(v) => set({ max_emitter_share: numOrNull(v) == null ? null : numOrNull(v) / 100 })} />
        <Num label="Доля рейтинга" unit="%" value={f.max_rating_share == null ? "" : f.max_rating_share * 100}
          onChange={(v) => set({ max_rating_share: numOrNull(v) == null ? null : numOrNull(v) / 100 })} />
      </div>

      <button className="btn sig-submit" type="submit" disabled={busy}>
        {busy ? "Считаю…" : "Собрать портфель"}
      </button>
    </form>
  );
}

function Tiles({ t }) {
  const cells = [
    ["Сумма", fmt.mln(t.money_rub), "млн ₽",
     t.shortfall_rub > 0 ? `недобор ${fmt.mln(t.shortfall_rub)}` : null],
    ["Y-IDX", fmt.bps(t.y_idx_w), "бп", `${t.count} бумаг · ${t.emitters} эмитентов`],
    ["Дюрация", fmt.num(t.dur_w, 2), "лет", null],
    ["±100 бп", fmt.mln(t.pnl_100bp_rub), "млн ₽", "цена спред-риска"],
    ["Купон 12м", fmt.mln(t.coupon_12m_rub), "млн ₽",
     t.current_coupon_w != null ? `текущий ${fmt.pct(t.current_coupon_w)}%` : null],
    ["Рейтинг", orDash(t.rating_avg), "средний", `HHI ${fmt.num(t.hhi_emitter, 2)}`],
  ];
  return (
    <div className="kpis">
      {cells.map(([label, val, unit, sub]) => (
        <div className="kpi" key={label}>
          <span className="kpi-label">{label}</span>
          <span className="kpi-val sm">{orDash(val)} <span className="kpi-unit">{unit}</span></span>
          {sub && <span className="kpi-sub">{sub}</span>}
        </div>
      ))}
    </div>
  );
}

function Positions({ rows, manual, onManual, onExclude, onOpen }) {
  return (
    <div className="ia-table-wrap">
      <table className="grid packed">
        <thead>
          <tr>
            <th className="left">Выпуск</th><th className="left">Эмитент</th>
            <th>Рейт</th><th>База</th><th>Срок</th><th>Цена</th><th>Y-IDX</th>
            <th>Дюр</th><th>Бумаг</th><th>Сумма</th><th>Доля</th><th>Обор.дн</th>
            <th className="left">Сумма вручную</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.isin}>
              <td className="left">
                <button type="button" className="pf-link" onClick={() => onOpen(r)}>{r.name}</button>
                {r.pinned && <span className="pf-badge" title="Прикноплена вручную">pin</span>}
                {r.capped && <span className="pf-badge warn" title="Позиция урезана глубиной стакана">стакан</span>}
                {r.price_estimated && <span className="pf-badge warn" title="Сумма больше книги — цена по всему стакану, оценочная">оценка</span>}
              </td>
              <td className="left pf-mut">{orDash(r.emitter)}</td>
              <td style={{ color: ratingColor(r.rating) }}>{orDash(r.rating)}</td>
              <td>{baseLabel(r.base)}{r.margin_bps ? ` +${r.margin_bps}` : ""}</td>
              <td>{orDash(fmt.yrs(r.years))}</td>
              <td className="num">{orDash(fmt.num(r.price, 2))}</td>
              <td className="num">{orDash(fmt.bps(r.y_idx_bps))}</td>
              <td className="num">{orDash(fmt.num(r.spread_dur, 1))}</td>
              <td className="num">{r.qty}</td>
              <td className="num">{fmt.mln(r.money_rub)}</td>
              <td className="num">{fmt.num(r.weight_pct, 1)}</td>
              <td className="num" title="Сколько средних дневных оборотов весит позиция">
                {orDash(fmt.num(r.adv_days, 1))}
              </td>
              <td className="left">
                <input className="pc-input pf-manual" placeholder="млн"
                  defaultValue={manual[r.isin] != null ? mln(manual[r.isin]) : ""}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onManual(r.isin, e.target.value); } }}
                  onBlur={(e) => onManual(r.isin, e.target.value)} />
              </td>
              <td>
                <button type="button" className="pf-x" title="Убрать из набора"
                  onClick={() => onExclude(r.isin)}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Shares({ title, map, hint }) {
  const rows = Object.entries(map || {});
  if (!rows.length) return null;
  return (
    <div className="pf-panel">
      <div className="sig-section">{title} {hint && <span>{hint}</span>}</div>
      {rows.map(([k, v]) => (
        <div className="pf-bar-row" key={k}>
          <span className="pf-bar-lbl">{k}</span>
          <span className="pf-bar"><span style={{ width: `${Math.round(v * 100)}%` }} /></span>
          <span className="pf-bar-val">{Math.round(v * 100)}%</span>
        </div>
      ))}
    </div>
  );
}

function Calendar({ rows, date }) {
  if (!rows?.length) return null;
  const max = Math.max(...rows.map((r) => r.total_rub)) || 1;
  return (
    <div className="pf-panel pf-panel-wide">
      <div className="sig-section">
        Выплаты по месяцам <span>млн ₽{date ? ` · график на ${fmt.date(date)}` : ""}</span>
      </div>
      <div className="pf-cal">
        {rows.map((r) => (
          <div className="pf-cal-col" key={r.month} title={
            `${r.month}: купоны ${fmt.mln(r.coupon_rub)} · погашения ${fmt.mln(r.redemption_rub)}`}>
            <span className="pf-cal-bar" style={{ height: `${Math.round(100 * r.total_rub / max)}%` }}>
              {r.redemption_rub > 0 && (
                <span className="pf-cal-red"
                  style={{ height: `${Math.round(100 * r.redemption_rub / r.total_rub)}%` }} />
              )}
            </span>
            <span className="pf-cal-lbl">{r.month.slice(5)}<br />{r.month.slice(2, 4)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Pool({ rows, onPin }) {
  if (!rows?.length) return null;
  return (
    <div className="pf-panel pf-panel-wide">
      <div className="sig-section">Замена <span>кандидаты, не попавшие в набор</span></div>
      <div className="pf-pool">
        {rows.map((r) => (
          <button type="button" className="pf-pool-row" key={r.isin} onClick={() => onPin(r.isin)}
            title="Взять в портфель">
            <span className="pf-pool-name">{r.name}</span>
            <span className="pf-mut">{orDash(r.rating)}</span>
            <span className="pf-mut">{orDash(fmt.yrs(r.years))}</span>
            <span className="pf-pool-y">{orDash(fmt.bps(r.y_idx_bps))} бп</span>
            <span className="pf-pool-add">+</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Rejected({ rows }) {
  const [open, setOpen] = useState(false);
  const byReason = useMemo(() => {
    const m = {};
    for (const r of rows || []) (m[r.reason_txt || r.reason] ||= []).push(r);
    return m;
  }, [rows]);
  if (!rows?.length) return null;
  return (
    <div className="pf-panel pf-panel-wide">
      <button type="button" className="pf-fold" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Не взято: {rows.length}
      </button>
      <div className="sig-chips pf-mt">
        {Object.entries(byReason).map(([k, v]) => (
          <span className="sig-chip" key={k}>{k} · {v.length}</span>
        ))}
      </div>
      {open && (
        <div className="pf-rej-list">
          {rows.map((r) => (
            <div key={r.isin + r.reason} className="pf-rej-row">
              <span>{r.name}</span>
              <span className="pf-mut">{r.reason_txt || r.reason}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function PortfolioModule() {
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const [f, setF] = useState(EMPTY);
  const [edits, setEdits] = useState({ exclude: [], pin: [], manual: {} });
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const restored = useRef(false);
  const runRef = useRef(null);

  // восстановление из ссылки: параметры сборки и ручные правки едут одним ?q=
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const q = sp.get("q");
    if (!q) return;
    try {
      const saved = JSON.parse(decodeURIComponent(q));
      const form = { ...EMPTY, ...saved.f };
      const ed = { exclude: [], pin: [], manual: {}, ...saved.e };
      setF(form);
      setEdits(ed);
      runRef.current(form, ed);
    } catch { /* битая ссылка — просто открываем пустую форму */ }
  }, [sp]);

  const run = useCallback(async (form, ed) => {
    setBusy(true); setErr(null);
    try {
      const body = { ...form, ...ed };
      const out = await buildPortfolio(body);
      setRes(out);
      setSp({ q: encodeURIComponent(JSON.stringify({ f: form, e: ed })) }, { replace: true });
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [setSp]);

  runRef.current = run;

  const set = (patch) => setF((cur) => ({ ...cur, ...patch }));

  const rebuild = (ed) => { setEdits(ed); run(f, ed); };
  const onExclude = (isin) => {
    const manual = { ...edits.manual }; delete manual[isin];
    rebuild({ exclude: [...edits.exclude, isin], pin: edits.pin.filter((x) => x !== isin), manual });
  };
  const onPin = (isin) =>
    rebuild({ ...edits, pin: [...edits.pin, isin], exclude: edits.exclude.filter((x) => x !== isin) });
  const onManual = (isin, val) => {
    const rub = toRub(val);
    const manual = { ...edits.manual };
    if (rub == null || rub <= 0) {
      if (manual[isin] == null) return;             // поле было и осталось пустым
      delete manual[isin];
    } else {
      if (manual[isin] === rub) return;             // значение не изменилось
      manual[isin] = rub;
    }
    rebuild({ ...edits, manual });
  };
  const openBond = (r) => {
    const p = new URLSearchParams({ isin: r.isin, ob: "1", sigside: "ask" });
    if (r.money_rub) p.set("sigvol", String(Math.round(r.money_rub)));
    nav(`/floaters?${p}`);
  };
  const copyIsins = async () => {
    if (!res?.positions?.length) return;
    setCopied(await copyText(res.positions.map((r) => r.isin).join("\n")));
    setTimeout(() => setCopied(false), 1500);
  };
  const reset = () => { setEdits({ exclude: [], pin: [], manual: {} }); run(f, { exclude: [], pin: [], manual: {} }); };

  const t = res?.totals;
  const touched = edits.exclude.length || edits.pin.length || Object.keys(edits.manual).length;

  return (
    <div className="issuer-agg">
      <div className="ia-head">
        <h2 className="ia-title">Портфель</h2>
        <div className="ia-hint">
          Набор считается заново на каждый запрос: цена позиции — средневзвешенная
          по стакану на её размер, а не верх книги. Ничего не сохраняется.
        </div>
      </div>

      <Form f={f} set={set} onSubmit={() => run(f, edits)} busy={busy} />

      {err && <div className="pf-err">{err}</div>}

      {res && (
        <>
          <div className="pf-actions">
            <button type="button" className="btn" onClick={() => run(f, edits)} disabled={busy}>
              Пересобрать
            </button>
            <button type="button" className="btn" onClick={copyIsins}>
              {copied ? "Скопировано" : "Копировать ISIN"}
            </button>
            {touched > 0 && (
              <button type="button" className="btn" onClick={reset}>
                Сбросить правки ({touched})
              </button>
            )}
            <span className="pc-status">расчёт на {fmt.date(res.calc_date)}</span>
          </div>

          {res.warnings?.map((w) => <div className="pf-warn" key={w}>{w}</div>)}

          {t && <Tiles t={t} />}

          {res.positions.length > 0 && (
            <Positions rows={res.positions} manual={edits.manual} onManual={onManual}
              onExclude={onExclude} onOpen={openBond} />
          )}
          {res.positions.length === 0 && (
            <div className="ia-empty">Под фильтр не нашлось ни одной бумаги</div>
          )}

          <div className="pf-panels">
            <Shares title="Эмитенты" map={Object.fromEntries(
              (t?.top_emitters || []).map((x) => [x.emitter, x.share]))} hint="топ-5" />
            <Shares title="Рейтинги" map={t?.by_rating} />
            <Shares title="База купона" map={t?.by_base} />
            <Shares title="Срок" map={t?.by_bucket} />
          </div>

          <Calendar rows={res.calendar} date={res.calendar_date} />
          <Pool rows={res.pool} onPin={onPin} />
          <Rejected rows={res.rejected} />
        </>
      )}
    </div>
  );
}
