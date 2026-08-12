import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearSignalEvents, createSignalFilter, deleteAllSignalFilters, deleteSignalFilter,
  fetchSignalEmitters, fetchSignalEvents, fetchSignalFilters, markSignalEventsSeen,
  patchSignalFilter, previewBlockFilter, previewSignalFilter, searchInstruments,
} from "../api.js";
import { fmt } from "../format.js";

const RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"];
// Порог «шевеления»: насколько должна сдвинуться цена, спред или объём, чтобы
// прилетело повторное уведомление по уже найденной бумаге.
const CHANGES = [[5, "5 %"], [10, "10 %"], [20, "20 %"], [50, "50 %"]];
// block — не срабатывание фильтра, а рыночное событие (filter_id=0), поэтому
// filter_name у него пустой: подпись собираем сами, см. sig-hit-meta ниже.
const REASON = { new: "новая", price: "цена", spread: "спред", money: "объём",
                 block: "блок" };

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

// Фильтр крупной сделки: база купона и режим торгов — то, чем крупняк
// отличается от стакана (адресные РПС в стакане не видны вообще).
const BASES = [["KEYRATE", "КС"], ["RUONIA", "RUONIA"], ["FIXED", "фикс"]];
const MARKETS = [["all", "все"], ["main", "безадресные"], ["ndm", "адресные"]];
const SIDES = [["any", "любая"], ["buy", "покупка"], ["sell", "продажа"]];
const labelOfPair = (pairs, v) => (pairs.find(([x]) => x === v) || [null, v])[1];
// «1 сделка / 2 сделки / 5 сделок» — счётчик превью читается как текст, а не как лог
const plural = (n, one, few, many) => {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
};

/** Человеческое описание условий блок-фильтра — строкой в карточке. */
function describeBlock(p) {
  const bases = p.bases?.length
    ? p.bases.map((b) => labelOfPair(BASES, b)).join("/") : "любая база";
  const money = p.min_value_rub >= 1e6
    ? fmt.num(p.min_value_rub / 1e6, 1) + " млн" : fmt.num(p.min_value_rub, 0);
  return `от ${money} ₽ · ${labelOfPair(MARKETS, p.markets)} · ${bases}`
    + (p.side !== "any" ? ` · ${labelOfPair(SIDES, p.side)}` : "");
}

/** Человеческое описание условий фильтра — одной строкой, как в карточке. */
function describe(p) {
  const who = [];
  if (p.ratings?.length) who.push("рейтинг " + p.ratings.join("/"));
  if (p.emitters?.length)
    who.push(p.emitters.length === 1 ? p.emitters[0] : p.emitters.length + " эмитентов");
  if (p.isins?.length) who.push(p.isins.length + " бумаг");
  let scope = who.length ? who.join(" или ") : "весь рынок";
  if (p.hide_subord) scope += ", без субордов";
  let range = null;
  if (p.spread_min != null && p.spread_max != null) range = `${p.spread_min}–${p.spread_max} бп`;
  else if (p.spread_min != null) range = `от ${p.spread_min} бп`;
  else if (p.spread_max != null) range = `до ${p.spread_max} бп`;
  let moneyTxt = null;
  if (p.min_money_rub) {
    const m = p.min_money_rub >= 1e6
      ? fmt.num(p.min_money_rub / 1e6, 1) + " млн" : fmt.num(p.min_money_rub, 0);
    moneyTxt = p.money_mode === "single" ? `заявка от ${m} ₽` : `набор от ${m} ₽`;
  }
  let years = null;
  if (p.years_min != null && p.years_max != null) years = `${p.years_min}–${p.years_max} л`;
  else if (p.years_min != null) years = `от ${p.years_min} л`;
  else if (p.years_max != null) years = `до ${p.years_max} л`;
  return { scope, range, years, moneyTxt };
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

const numOrEmpty = (v) => (v == null ? "" : String(v));

// Объём вводится в МЛН ₽ — как фильтр тикета в СПИСКЕ (Toolbar): «100000000»
// в поле никто не читает с первого раза. В состоянии и в API — по-прежнему ₽.
const rubToMln = (v) => (v == null || v === "" ? "" : String(v / 1e6));
const mlnToRub = (s) => {
  const v = parseFloat(String(s).replace(",", "."));
  return Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : null;
};

/** Форма фильтра: отбор бумаг (ИЛИ) + условия сделки (И) + живое превью.
 *  edit — существующий фильтр: те же поля, но сохраняем правкой. Монтируется
 *  с key={edit?.id ?? "new"}, поэтому начальные значения ставятся один раз. */
function FilterForm({ onSubmit, busy, edit, onCancel }) {
  const ep = edit?.params || {};
  const [name, setName] = useState(edit?.name || "");
  const [ratings, setRatings] = useState(ep.ratings || []);
  const [emitters, setEmitters] = useState(ep.emitters || []);
  const [isins, setIsins] = useState(ep.isins || []);
  const [side, setSide] = useState(ep.side || "ask");
  const [smin, setSmin] = useState(numOrEmpty(ep.spread_min));
  const [smax, setSmax] = useState(numOrEmpty(ep.spread_max));
  const [minMoney, setMinMoney] = useState(rubToMln(ep.min_money_rub));  // млн ₽
  const [moneyMode, setMoneyMode] = useState(ep.money_mode || "book");
  const [ymin, setYmin] = useState(numOrEmpty(ep.years_min));
  const [ymax, setYmax] = useState(numOrEmpty(ep.years_max));
  const [hideSub, setHideSub] = useState(!!ep.hide_subord);
  const [changePct, setChangePct] = useState(edit?.change_pct ?? 10);
  const [sound, setSound] = useState(edit ? !!edit.sound : true);
  const [desktop, setDesktop] = useState(edit ? !!edit.desktop : true);
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState(null);

  const params = useMemo(() => ({
    ratings, emitters, isins, side,
    spread_min: smin === "" ? null : Number(smin),
    spread_max: smax === "" ? null : Number(smax),
    min_money_rub: mlnToRub(minMoney),
    money_mode: moneyMode,
    years_min: ymin === "" ? null : Number(ymin),
    years_max: ymax === "" ? null : Number(ymax),
    hide_subord: hideSub,
  }), [ratings, emitters, isins, side, smin, smax, minMoney, moneyMode, ymin, ymax, hideSub]);

  // Живое превью: показывает, что попадёт под условия ПРЯМО СЕЙЧАС — иначе
  // фильтр сохраняют вслепую и ждут сигнала, которого может не быть никогда.
  useEffect(() => {
    if (smin === "" && smax === "" && minMoney === "") { setPreview(null); return; }
    let dead = false;
    const t = setTimeout(async () => {
      try {
        const r = await previewSignalFilter(params);
        if (!dead) setPreview(r);
      } catch { if (!dead) setPreview(null); }
    }, 400);
    return () => { dead = true; clearTimeout(t); };
  }, [params, smin, smax, minMoney]);

  const searchEmitters = useCallback(
    async (q) => (await fetchSignalEmitters(q)).emitters.map((n) => ({ n })), []);
  const searchBonds = useCallback(async (q) => (await searchInstruments(q)).results, []);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!name.trim()) { setErr("Дай сигналу название"); return; }
    if (smin === "" && smax === "" && minMoney === "") {
      setErr("Задай диапазон R-spread или объём — иначе условий нет"); return; }
    try {
      await onSubmit({ name: name.trim(), kind: "book", params,
                       change_pct: changePct, sound, desktop });
      if (edit) return;      // правка закрывает форму снаружи
      setName(""); setRatings([]); setEmitters([]); setIsins([]);
      setSmin(""); setSmax(""); setMinMoney(""); setYmin(""); setYmax("");
      setMoneyMode("book"); setHideSub(false); setPreview(null);
    } catch (e2) { setErr(e2.message); }
  };

  return (
    <form className={"sig-form" + (edit ? " editing" : "")} onSubmit={submit}>
      <div className="sig-form-head">
        {edit ? `Правка: ${edit.name}` : "Новый сигнал"}
        {edit && (
          <button type="button" className="sig-cancel" onClick={onCancel}>Отмена</button>
        )}
      </div>

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
          <label className="sig-label">Диапазон R-spread, бп</label>
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
          <label className="sig-label" htmlFor="sig-money">Объём, млн ₽</label>
          <input id="sig-money" className="sig-input num" type="number" step="0.5" min="0"
            placeholder="не важно" value={minMoney}
            onChange={(e) => setMinMoney(e.target.value)} />
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

      {minMoney !== "" && (
        <div className="sig-field">
          <label className="sig-label">Как считать объём</label>
          <div className="sig-seg">
            <button type="button" className={moneyMode === "book" ? "on" : ""}
              onClick={() => setMoneyMode("book")}>Набором по стакану</button>
            <button type="button" className={moneyMode === "single" ? "on" : ""}
              onClick={() => setMoneyMode("single")}>Одной заявкой</button>
          </div>
          <div className="sig-note">
            {moneyMode === "book"
              ? "Собираем сумму по лестнице от лучшей цены; цена и спред — по средневзвесу набора."
              : "Ждём ОДНУ заявку не меньше суммы; двадцать мелких на ту же сумму сигналом не считаются."}
          </div>
        </div>
      )}

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
        {busy ? "Сохраняем…" : edit ? "Сохранить изменения" : "Создать сигнал"}</button>
    </form>
  );
}

/** Форма фильтра крупной сделки. Условий стакана здесь нет: событие — факт
 *  сделки в ленте (в т.ч. адресной РПС, которой в стакане не бывает). */
function BlockForm({ onSubmit, busy, edit, onCancel }) {
  const ep = edit?.params || {};
  const [name, setName] = useState(edit?.name || "");
  const [ratings, setRatings] = useState(ep.ratings || []);
  const [emitters, setEmitters] = useState(ep.emitters || []);
  const [isins, setIsins] = useState(ep.isins || []);
  const [bases, setBases] = useState(ep.bases || ["KEYRATE", "RUONIA"]);
  const [minValue, setMinValue] = useState(rubToMln(ep.min_value_rub ?? 100000000));  // млн ₽
  const [markets, setMarkets] = useState(ep.markets || "all");
  const [side, setSide] = useState(ep.side || "any");
  const [hideSub, setHideSub] = useState(!!ep.hide_subord);
  const [sound, setSound] = useState(edit ? !!edit.sound : true);
  const [desktop, setDesktop] = useState(edit ? !!edit.desktop : true);
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState(null);

  const params = useMemo(() => ({
    ratings, emitters, isins, bases, markets, side, hide_subord: hideSub,
    min_value_rub: mlnToRub(minValue),
  }), [ratings, emitters, isins, bases, markets, side, hideSub, minValue]);

  // Превью по СЕГОДНЯШНЕЙ ленте: у события нет «набора сейчас», а вслепую
  // выставленный порог либо молчит неделю, либо звонит каждые пять минут.
  useEffect(() => {
    if (!(mlnToRub(minValue) >= 1e6)) { setPreview(null); return; }
    let dead = false;
    const t = setTimeout(async () => {
      try {
        const r = await previewBlockFilter(params);
        if (!dead) setPreview(r);
      } catch { if (!dead) setPreview(null); }
    }, 400);
    return () => { dead = true; clearTimeout(t); };
  }, [params, minValue]);

  const searchEmitters = useCallback(
    async (q) => (await fetchSignalEmitters(q)).emitters.map((n) => ({ n })), []);
  const searchBonds = useCallback(async (q) => (await searchInstruments(q)).results, []);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!name.trim()) { setErr("Дай сигналу название"); return; }
    if (!(mlnToRub(minValue) >= 1e6)) {
      setErr("Порог объёма: от 1 млн ₽ — мельче лента не хранит"); return; }
    try {
      await onSubmit({ name: name.trim(), kind: "block", params, sound, desktop });
      if (edit) return;
      setName(""); setRatings([]); setEmitters([]); setIsins([]); setPreview(null);
    } catch (e2) { setErr(e2.message); }
  };

  return (
    <form className={"sig-form" + (edit ? " editing" : "")} onSubmit={submit}>
      <div className="sig-form-head">
        {edit ? `Правка: ${edit.name}` : "Новый сигнал: крупная сделка"}
        {edit && (
          <button type="button" className="sig-cancel" onClick={onCancel}>Отмена</button>
        )}
      </div>

      <div className="sig-field">
        <label className="sig-label" htmlFor="blk-name">Название</label>
        <input id="blk-name" className="sig-input" value={name} placeholder="например, блоки в моих эмитентах"
          onChange={(e) => setName(e.target.value)} />
      </div>

      <div className="sig-section">Какие сделки</div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label" htmlFor="blk-money">Объём сделки от, млн ₽</label>
          <input id="blk-money" className="sig-input num" type="number" step="1" min="1"
            value={minValue} onChange={(e) => setMinValue(e.target.value)} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Режим торгов</label>
          <div className="sig-seg">
            {MARKETS.map(([v, t]) => (
              <button type="button" key={v} className={markets === v ? "on" : ""}
                onClick={() => setMarkets(v)}>{t}</button>
            ))}
          </div>
        </div>
      </div>
      <div className="sig-note">
        Адресные — РПС, размещения и выкупы: в стакане их не видно вообще, крупняк чаще идёт именно так.
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">База купона</label>
          <div className="sig-chips">
            {BASES.map(([v, t]) => (
              <button type="button" key={v}
                className={"sig-chip" + (bases.includes(v) ? " on" : "")}
                onClick={() => setBases(bases.includes(v)
                  ? bases.filter((x) => x !== v) : [...bases, v])}>{t}</button>
            ))}
          </div>
        </div>
        <div className="sig-field">
          <label className="sig-label">Сторона (агрессор)</label>
          <div className="sig-seg">
            {SIDES.map(([v, t]) => (
              <button type="button" key={v} className={side === v ? "on" : ""}
                onClick={() => setSide(v)}>{t}</button>
            ))}
          </div>
        </div>
      </div>
      <div className="sig-note">
        Пустая база — любые бумаги. Крупняк рынка в рублях почти целиком ОФЗ-ПД,
        поэтому без КС/RUONIA лента уведомлений вырождается в фиксы. У адресных сделок стороны нет.
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
          {preview.total === 0
            ? "Сегодня под условия не попала ни одна сделка."
            : <>Сегодня под условия {plural(preview.total, "попала", "попало", "попало")}{" "}
                <b>{preview.total}</b>{preview.capped ? "+" : ""}{" "}
                {plural(preview.total, "сделка", "сделки", "сделок")}:{" "}
                {preview.matches.slice(0, 4).map(
                  (m) => `${m.name} (${money(m.money_rub)} ₽)`).join(", ")}
                {preview.total > 4 && ` и ещё ${preview.total - 4}`}</>}
        </div>
      )}

      {err && <div className="sig-err">{err}</div>}
      <button className="btn sig-submit" type="submit" disabled={busy}>
        {busy ? "Сохраняем…" : edit ? "Сохранить изменения" : "Создать сигнал"}</button>
    </form>
  );
}

function FilterRow({ f, onToggle, onDelete, onEdit, editing }) {
  if (f.kind === "block") {
    return (
      <div className={"sig-row-card" + (f.enabled ? "" : " off") + (editing ? " on-edit" : "")}>
        <div className="sig-rc-main">
          <div className="sig-rc-title">
            {f.name}
            <span className="sb-tag sb-block">блок</span>
            <span className={"sig-state " + (f.enabled ? "on" : "off")}>
              {f.enabled ? "включён" : "выключен"}</span>
          </div>
          <div className="sig-rc-sub">{describe(f.params).scope}</div>
          <div className="sig-rc-cond num">
            {describeBlock(f.params)}
            {f.sound ? " · звук" : ""}{f.desktop ? " · окно" : ""}
          </div>
        </div>
        <div className="sig-rc-actions">
          <button className="btn" onClick={() => onEdit(f)}>Изменить</button>
          <button className="btn" onClick={() => onToggle(f)}>
            {f.enabled ? "Выключить" : "Включить"}</button>
          <button className="btn btn-danger" onClick={() => onDelete(f.id)}>Удалить</button>
        </div>
      </div>
    );
  }
  return <BookFilterRow f={f} onToggle={onToggle} onDelete={onDelete}
    onEdit={onEdit} editing={editing} />;
}

function BookFilterRow({ f, onToggle, onDelete, onEdit, editing }) {
  const d = describe(f.params);
  return (
    <div className={"sig-row-card" + (f.enabled ? "" : " off") + (editing ? " on-edit" : "")}>
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
          {d.range ? ` · R-spread ${d.range}` : ""}
          {d.moneyTxt ? ` · ${d.moneyTxt}` : ""}
          {d.years ? ` · срок ${d.years}` : ""}
          {" · сдвиг "}{chLabel(f.change_pct)}
          {f.sound ? " · звук" : ""}{f.desktop ? " · окно" : ""}
        </div>
      </div>
      <div className="sig-rc-actions">
        <button className="btn" onClick={() => onEdit(f)}>Изменить</button>
        <button className="btn" onClick={() => onToggle(f)}>
          {f.enabled ? "Выключить" : "Включить"}</button>
        <button className="btn btn-danger" onClick={() => onDelete(f.id)}>Удалить</button>
      </div>
    </div>
  );
}

// Повторное срабатывание по УЖЕ найденной бумаге: показываем, что именно
// шевельнулось и на сколько — иначе в ленте десяток одинаковых строк подряд.
const REPEAT = {
  price: ["цена", "price", (v) => fmt.num(v, 2) + "%"],
  spread: ["спред", "val_bps", (v) => fmt.num(v, 0) + " бп"],
  money: ["объём", "money_rub", (v) => money(v) + " ₽"],
};

function WhyLine({ h }) {
  const r = REPEAT[h.reason];
  if (!r) {
    return h.reason === "new"
      ? <div className="sig-hit-why">нашлась под условия</div> : null;
  }
  const [what, field, fmtv] = r;
  const prev = h["prev_" + field];
  const cur = h[field];
  return (
    <div className="sig-hit-why">
      повтор: {what}{" "}
      {prev != null && <span className="num">{fmtv(prev)} → </span>}
      <span className="num">{cur != null ? fmtv(cur) : "—"}</span>
    </div>
  );
}

/** Колонка одного вида сигналов: список фильтров + своя форма под ним.
 *  Виды не переключаются табом — они стоят рядом, потому что настраивают их
 *  вместе (порог блока смотрят на условия стакана и наоборот). */
function FilterColumn({ title, hint, empty, Form, formKey, rows, editing, loading,
                        busy, onToggle, onEdit, onDelete, onCancel, onSubmit,
                        onDeleteAll }) {
  return (
    <div className="sig-col">
      <div className="sig-head">
        {title}
        <span className="sig-head-sub">{hint}</span>
        {rows.length > 0 && (
          <button className="btn sig-clear"
            onClick={() => onDeleteAll(rows.length)}>Удалить все</button>
        )}
      </div>

      {loading ? <div className="muted">Загрузка…</div>
        : rows.length === 0
          ? <div className="sig-empty">{empty}</div>
          : rows.map((f) => (
              <FilterRow key={f.id} f={f} editing={editing?.id === f.id}
                onToggle={onToggle} onEdit={onEdit} onDelete={onDelete} />
            ))}

      {/* key переинициализирует поля при смене правимого фильтра */}
      <Form key={editing?.id ?? formKey} edit={editing} busy={busy}
        onCancel={onCancel} onSubmit={onSubmit} />
    </div>
  );
}

export default function SignalsModule() {
  const qc = useQueryClient();
  const [editId, setEditId] = useState(null);
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
  const save = useMutation({
    mutationFn: ({ id, body }) => patchSignalFilter(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["signal-filters"] });
      setEditId(null);
    },
  });
  const del = useMutation({
    mutationFn: deleteSignalFilter,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["signal-filters"] });
      qc.invalidateQueries({ queryKey: ["signal-events"] });
    },
  });
  const delAll = useMutation({
    mutationFn: deleteAllSignalFilters,
    onSuccess: () => {
      setEditId(null);
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
  const editing = rows.find((f) => f.id === editId) || null;
  // два вида сигналов — две колонки; правка живёт в колонке своего вида
  const bookRows = rows.filter((f) => f.kind !== "block");
  const blockRows = rows.filter((f) => f.kind === "block");

  const colProps = (kind) => ({
    rows: kind === "block" ? blockRows : bookRows,
    editing: editing && (editing.kind === "block") === (kind === "block") ? editing : null,
    loading: filters.isLoading,
    busy: editing ? save.isPending : create.isPending,
    onToggle: (x) => patch.mutate({ id: x.id, body: { enabled: !x.enabled } }),
    onEdit: (x) => setEditId(editId === x.id ? null : x.id),
    onDelete: (id) => { if (editId === id) setEditId(null); del.mutate(id); },
    onCancel: () => setEditId(null),
    onSubmit: (body) => (editing && (editing.kind === "block") === (kind === "block")
      ? save.mutateAsync({ id: editing.id, body })
      : create.mutateAsync(body)),
    onDeleteAll: (n) => {
      if (window.confirm(`Удалить все фильтры этого вида (${n})? Их события в ленте тоже уйдут.`))
        delAll.mutate(kind);
    },
  });

  return (
    <div className="sig-wrap">
      <FilterColumn
        title="Сигналы стакана"
        hint="бот проверяет рынок в торговые часы и показывает бумаги, попавшие под условия"
        empty="Сигналов по стакану нет. Опиши условия — при совпадении придёт
               всплывающее окно, звук и запись в ленту."
        Form={FilterForm} formKey="new" {...colProps("book")} />

      <FilterColumn
        title="Крупные сделки"
        hint="звонит на факт сделки в ленте, включая адресные — в стакане их не видно"
        empty="Фильтров крупных сделок нет — звонит умолчание: от 100 млн ₽ и только флоатеры."
        Form={BlockForm} formKey="new-block" {...colProps("block")} />

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
              <div key={h.id}
                className={"sig-hit " + (h.reason === "block" ? "hit-block" : "hit-book")
                  + (REPEAT[h.reason] ? " hit-repeat" : "")}>
                <div className="sig-hit-top">
                  <span className="sig-hit-name">{h.name || h.isin}</span>
                  <span className={"sb-tag sb-" + h.reason}>{REASON[h.reason] || h.reason}</span>
                  <span className="sig-hit-time num">{timeOf(h.fired_at)}</span>
                </div>
                <div className="sig-hit-body num">
                  {/* у крупной сделки сторона — агрессор (buy/sell), а не сторона
                      стакана; у адресной её нет вообще */}
                  {h.reason === "block"
                    ? (h.side === "buy" || h.side === "sell") && (
                        <span className={h.side === "buy" ? "pos" : "neg"}>
                          {h.side === "buy" ? "покупка" : "продажа"}</span>)
                    : <span className={h.side === "ask" ? "pos" : "neg"}>
                        {h.side === "ask" ? "оффер" : "бид"}</span>}
                  {h.val_bps != null && <> <b>{fmt.num(h.val_bps, 0)} бп</b></>}
                  {h.price != null && <> · {fmt.num(h.price, 2)}%</>}
                  {h.money_rub != null && <> · {money(h.money_rub)} ₽</>}
                  {h.levels ? <> · {h.levels} ур</> : null}
                </div>
                <WhyLine h={h} />
                <div className="sig-hit-meta">
                  {/* у блока filter_name пустой, когда звонило умолчание
                      (env-порог), а не заведённый пользователем фильтр */}
                  {h.filter_name
                    || (h.reason === "block" ? "крупная сделка по рынку" : "фильтр удалён")}
                  {" · "}{h.isin}
                </div>
              </div>
            ))}
      </div>
    </div>
  );
}
