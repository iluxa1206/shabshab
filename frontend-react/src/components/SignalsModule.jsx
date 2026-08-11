import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearSignalEvents, createSignalFilter, deleteSignalFilter, fetchSignalEmitters,
  fetchSignalEvents, fetchSignalFilters, markSignalEventsSeen, patchSignalFilter,
  previewSignalFilter, searchInstruments,
} from "../api.js";
import { fmt } from "../format.js";

const RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"];
// Порог «шевеления»: насколько должна сдвинуться цена, спред или объём, чтобы
// прилетело повторное уведомление по уже найденной бумаге.
const CHANGES = [[5, "5 %"], [10, "10 %"], [20, "20 %"], [50, "50 %"]];
const REASON = { new: "новая", price: "цена", spread: "спред", money: "объём" };

const money = (v) =>
  v == null ? "—"
    : v >= 1e6 ? fmt.num(v / 1e6, 1) + " млн"
    : v >= 1e3 ? fmt.num(v / 1e3, 0) + " тыс" : fmt.num(v, 0);

const chLabel = (v) => (CHANGES.find(([x]) => x === v) || [null, v + " %"])[1];

const timeOf = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
};

/** Человеческое описание условий фильтра — одной строкой, как в карточке. */
function describe(p) {
  const who = [];
  if (p.ratings?.length) who.push("рейтинг " + p.ratings.join("/"));
  if (p.emitters?.length)
    who.push(p.emitters.length === 1 ? p.emitters[0] : p.emitters.length + " эмитентов");
  if (p.isins?.length) who.push(p.isins.length + " бумаг");
  let scope = who.length ? who.join(" или ") : "весь рынок";
  if (p.hide_subord) scope += ", без субордов";
  let range;
  if (p.spread_min != null && p.spread_max != null) range = `${p.spread_min}–${p.spread_max} бп`;
  else if (p.spread_min != null) range = `от ${p.spread_min} бп`;
  else range = `до ${p.spread_max} бп`;
  let years = null;
  if (p.years_min != null && p.years_max != null) years = `${p.years_min}–${p.years_max} л`;
  else if (p.years_min != null) years = `от ${p.years_min} л`;
  else if (p.years_max != null) years = `до ${p.years_max} л`;
  return { scope, range, years };
}

/** Пикер с накоплением выбранного в чипы (эмитенты, отдельные бумаги). */
function MultiPicker({ label, placeholder, items, onChange, search, keyOf, labelOf, subOf }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState([]);
  const [open, setOpen] = useState(false);
  const timer = useRef(null);
  const box = useRef(null);

  useEffect(() => {
    clearTimeout(timer.current);
    if (q.trim().length < 2) { setRes([]); setOpen(false); return; }
    timer.current = setTimeout(async () => {
      try {
        const r = await search(q.trim());
        setRes(r); setOpen(r.length > 0);
      } catch { setRes([]); setOpen(false); }
    }, 220);
    return () => clearTimeout(timer.current);
  }, [q, search]);

  useEffect(() => {
    const onDoc = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const add = (it) => {
    const k = keyOf(it);
    if (!items.includes(k)) onChange([...items, k]);
    setQ(""); setOpen(false);
  };

  return (
    <div className="sig-field" ref={box}>
      <label className="sig-label">{label}</label>
      <div className="sig-picker">
        <input className="sig-input" value={q} placeholder={placeholder}
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => res.length && setOpen(true)} />
        {open && (
          <div className="sig-suggest">
            {res.map((it) => (
              <button type="button" key={keyOf(it)} onClick={() => add(it)}>
                <span>{labelOf(it)}</span>
                {subOf(it) && <span className="sig-suggest-sub">{subOf(it)}</span>}
              </button>
            ))}
          </div>
        )}
      </div>
      {items.length > 0 && (
        <div className="sig-chips">
          {items.map((k) => (
            <button type="button" key={k} className="sig-chip on"
              onClick={() => onChange(items.filter((x) => x !== k))}>
              {k}<span className="sig-chip-x">✕</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Форма фильтра: отбор бумаг (ИЛИ) + условия сделки (И) + живое превью. */
function FilterForm({ onSubmit, busy }) {
  const [name, setName] = useState("");
  const [ratings, setRatings] = useState([]);
  const [emitters, setEmitters] = useState([]);
  const [isins, setIsins] = useState([]);
  const [side, setSide] = useState("ask");
  const [smin, setSmin] = useState("");
  const [smax, setSmax] = useState("");
  const [minMoney, setMinMoney] = useState("");
  const [ymin, setYmin] = useState("");
  const [ymax, setYmax] = useState("");
  const [hideSub, setHideSub] = useState(false);
  const [changePct, setChangePct] = useState(10);
  const [sound, setSound] = useState(true);
  const [desktop, setDesktop] = useState(true);
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState(null);

  const params = useMemo(() => ({
    ratings, emitters, isins, side,
    spread_min: smin === "" ? null : Number(smin),
    spread_max: smax === "" ? null : Number(smax),
    min_money_rub: minMoney === "" ? null : Number(minMoney),
    years_min: ymin === "" ? null : Number(ymin),
    years_max: ymax === "" ? null : Number(ymax),
    hide_subord: hideSub,
  }), [ratings, emitters, isins, side, smin, smax, minMoney, ymin, ymax, hideSub]);

  // Живое превью: показывает, что попадёт под условия ПРЯМО СЕЙЧАС — иначе
  // фильтр сохраняют вслепую и ждут сигнала, которого может не быть никогда.
  useEffect(() => {
    if (smin === "" && smax === "") { setPreview(null); return; }
    let dead = false;
    const t = setTimeout(async () => {
      try {
        const r = await previewSignalFilter(params);
        if (!dead) setPreview(r);
      } catch { if (!dead) setPreview(null); }
    }, 400);
    return () => { dead = true; clearTimeout(t); };
  }, [params, smin, smax]);

  const searchEmitters = useCallback(
    async (q) => (await fetchSignalEmitters(q)).emitters.map((n) => ({ n })), []);
  const searchBonds = useCallback(async (q) => (await searchInstruments(q)).results, []);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!name.trim()) { setErr("Дай сигналу название"); return; }
    if (smin === "" && smax === "") { setErr("Задай хотя бы одну границу диапазона Y-IDX"); return; }
    try {
      await onSubmit({ name: name.trim(), params, change_pct: changePct, sound, desktop });
      setName(""); setRatings([]); setEmitters([]); setIsins([]);
      setSmin(""); setSmax(""); setMinMoney(""); setYmin(""); setYmax("");
      setHideSub(false); setPreview(null);
    } catch (e2) { setErr(e2.message); }
  };

  return (
    <form className="sig-form" onSubmit={submit}>
      <div className="sig-form-head">Новый сигнал</div>

      <div className="sig-field">
        <label className="sig-label" htmlFor="sig-name">Название</label>
        <input id="sig-name" className="sig-input" value={name} placeholder="например, широкие ААА"
          onChange={(e) => setName(e.target.value)} />
      </div>

      <div className="sig-section">Какие бумаги <span>условия объединяются по «или»</span></div>

      <div className="sig-field">
        <label className="sig-label">Рейтинг</label>
        <div className="sig-chips">
          {RATINGS.map((r) => (
            <button type="button" key={r}
              className={"sig-chip" + (ratings.includes(r) ? " on" : "")}
              onClick={() => setRatings(ratings.includes(r)
                ? ratings.filter((x) => x !== r) : [...ratings, r])}>{r}</button>
          ))}
        </div>
      </div>

      <MultiPicker label="Эмитенты" placeholder="начни вводить название"
        items={emitters} onChange={setEmitters} search={searchEmitters}
        keyOf={(x) => x.n} labelOf={(x) => x.n} subOf={() => ""} />

      <MultiPicker label="Отдельные бумаги" placeholder="ISIN или название"
        items={isins} onChange={setIsins} search={searchBonds}
        keyOf={(r) => r.isin} labelOf={(r) => r.name}
        subOf={(r) => r.isin + (r.rating ? " · " + r.rating : "")} />

      <div className="sig-field">
        <label className="sig-check-line">
          <input type="checkbox" checked={hideSub}
            onChange={(e) => setHideSub(e.target.checked)} />
          <span>Прятать суборды</span>
        </label>
        <div className="sig-note">
          Определяем по названию (СУБ, Т1, перп) — отдельного признака в реестре нет.
          Такие выпуски дают широчайший спред из-за риска списания и иначе занимают весь верх.
        </div>
      </div>

      <div className="sig-section">Условия</div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Сторона стакана</label>
          <div className="sig-seg">
            <button type="button" className={side === "ask" ? "on up" : ""}
              onClick={() => setSide("ask")}>Оффер</button>
            <button type="button" className={side === "bid" ? "on down" : ""}
              onClick={() => setSide("bid")}>Бид</button>
          </div>
        </div>
        <div className="sig-field">
          <label className="sig-label">Диапазон Y-IDX, бп</label>
          <div className="sig-row tight">
            <input className="sig-input num" type="number" placeholder="от" value={smin}
              onChange={(e) => setSmin(e.target.value)} />
            <input className="sig-input num" type="number" placeholder="до" value={smax}
              onChange={(e) => setSmax(e.target.value)} />
          </div>
        </div>
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label" htmlFor="sig-money">Денег в стакане, ₽</label>
          <input id="sig-money" className="sig-input num" type="number" placeholder="не важно"
            value={minMoney} onChange={(e) => setMinMoney(e.target.value)} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Срок до погашения, лет</label>
          <div className="sig-row tight">
            <input className="sig-input num" type="number" step="any" placeholder="от"
              value={ymin} onChange={(e) => setYmin(e.target.value)} />
            <input className="sig-input num" type="number" step="any" placeholder="до"
              value={ymax} onChange={(e) => setYmax(e.target.value)} />
          </div>
        </div>
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Повторно сообщать при сдвиге</label>
          <div className="sig-seg">
            {CHANGES.map(([v, t]) => (
              <button type="button" key={v} className={changePct === v ? "on" : ""}
                onClick={() => setChangePct(v)}>{t}</button>
            ))}
          </div>
        </div>
      </div>

      <div className="sig-field">
        <label className="sig-label">Как оповещать</label>
        <div className="sig-checks">
          <label><input type="checkbox" checked={sound}
            onChange={(e) => setSound(e.target.checked)} /> звук</label>
          <label><input type="checkbox" checked={desktop}
            onChange={(e) => setDesktop(e.target.checked)} /> окно системы</label>
        </div>
      </div>

      {preview && (
        <div className="sig-preview">
          {!preview.ready ? "Метрики ещё прогреваются — превью будет через минуту."
            : preview.total === 0 ? "Сейчас под условия не попадает ни одна бумага."
            : <>Сейчас под условия попадает <b>{preview.total}</b>:{" "}
                {preview.matches.slice(0, 4).map((m) =>
                  m.name + (m.years != null ? ` (${fmt.num(m.years, 1)} л)` : "")).join(", ")}
                {preview.total > 4 && ` и ещё ${preview.total - 4}`}</>}
        </div>
      )}

      {err && <div className="sig-err">{err}</div>}
      <button className="btn sig-submit" type="submit" disabled={busy}>
        {busy ? "Создаём…" : "Создать сигнал"}</button>
    </form>
  );
}

function FilterRow({ f, onToggle, onDelete }) {
  const d = describe(f.params);
  return (
    <div className={"sig-row-card" + (f.enabled ? "" : " off")}>
      <div className="sig-rc-main">
        <div className="sig-rc-title">
          {f.name}
          <span className={"sig-state " + (f.enabled ? "on" : "off")}>
            {f.enabled ? "включён" : "выключен"}</span>
        </div>
        <div className="sig-rc-sub">{d.scope}</div>
        <div className="sig-rc-cond num">
          <span className={f.params.side === "ask" ? "pos" : "neg"}>
            {f.params.side === "ask" ? "оффер" : "бид"}</span>
          {" · Y-IDX "}{d.range}
          {d.years ? ` · срок ${d.years}` : ""}
          {f.params.min_money_rub ? ` · от ${money(f.params.min_money_rub)} ₽` : ""}
          {" · сдвиг "}{chLabel(f.change_pct)}
          {f.sound ? " · звук" : ""}{f.desktop ? " · окно" : ""}
        </div>
      </div>
      <div className="sig-rc-actions">
        <button className="btn" onClick={() => onToggle(f)}>
          {f.enabled ? "Выключить" : "Включить"}</button>
        <button className="btn btn-danger" onClick={() => onDelete(f.id)}>Удалить</button>
      </div>
    </div>
  );
}

export default function SignalsModule() {
  const qc = useQueryClient();
  const filters = useQuery({ queryKey: ["signal-filters"], queryFn: fetchSignalFilters });
  const events = useQuery({
    queryKey: ["signal-events"], queryFn: () => fetchSignalEvents(100), refetchInterval: 30000,
  });

  const create = useMutation({
    mutationFn: createSignalFilter,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["signal-filters"] }),
  });
  const patch = useMutation({
    mutationFn: ({ id, body }) => patchSignalFilter(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["signal-filters"] }),
  });
  const del = useMutation({
    mutationFn: deleteSignalFilter,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["signal-filters"] });
      qc.invalidateQueries({ queryKey: ["signal-events"] });
    },
  });
  const clear = useMutation({
    mutationFn: clearSignalEvents,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["signal-events"] }),
  });

  // вкладка открыта — лента считается прочитанной (счётчик колокольчика гаснет)
  useEffect(() => {
    if (!events.data?.unseen) return;
    markSignalEventsSeen()
      .then(() => qc.invalidateQueries({ queryKey: ["signal-events"] }))
      .catch(() => {});
  }, [events.data, qc]);

  const rows = filters.data?.filters || [];
  const feed = events.data?.events || [];

  return (
    <div className="sig-wrap">
      <div className="sig-col">
        <div className="sig-head">
          Сигналы
          <span className="sig-head-sub">
            бот проверяет рынок в торговые часы и показывает бумаги, попавшие под условия</span>
        </div>

        {filters.isLoading ? <div className="muted">Загрузка…</div>
          : rows.length === 0
            ? <div className="sig-empty">Сигналов нет. Опиши условия — при совпадении
                придёт всплывающее окно, звук и запись в ленту справа.</div>
            : rows.map((f) => (
                <FilterRow key={f.id} f={f}
                  onToggle={(x) => patch.mutate({ id: x.id, body: { enabled: !x.enabled } })}
                  onDelete={(id) => del.mutate(id)} />
              ))}

        <FilterForm busy={create.isPending}
          onSubmit={(body) => create.mutateAsync(body)} />
      </div>

      <div className="sig-col sig-feed-col">
        <div className="sig-head">
          Лента срабатываний
          {feed.length > 0 && (
            <button className="btn sig-clear" onClick={() => clear.mutate()}>Очистить</button>
          )}
        </div>
        {feed.length === 0
          ? <div className="sig-empty">Пока пусто.</div>
          : feed.map((h) => (
              <div className="sig-hit" key={h.id}>
                <div className="sig-hit-top">
                  <span className="sig-hit-name">{h.name || h.isin}</span>
                  <span className={"sb-tag sb-" + h.reason}>{REASON[h.reason] || h.reason}</span>
                  <span className="sig-hit-time num">{timeOf(h.fired_at)}</span>
                </div>
                <div className="sig-hit-body num">
                  <span className={h.side === "ask" ? "pos" : "neg"}>
                    {h.side === "ask" ? "оффер" : "бид"}</span>
                  {" "}<b>{fmt.num(h.val_bps, 0)} бп</b>
                  {h.price != null && <> · {fmt.num(h.price, 2)}%</>}
                  {h.money_rub != null && <> · {money(h.money_rub)} ₽</>}
                  {h.levels ? <> · {h.levels} ур</> : null}
                </div>
                <div className="sig-hit-meta">{h.filter_name || "фильтр удалён"} · {h.isin}</div>
              </div>
            ))}
      </div>
    </div>
  );
}
