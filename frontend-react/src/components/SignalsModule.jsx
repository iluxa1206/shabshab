import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearSignalEvents, createSignalFilter, deleteAllSignalFilters, deleteSignalFilter,
  fetchSignalEmitters, fetchSignalEvents, fetchSignalFilters, fetchSignalTargets,
  markSignalEventsSeen,
  patchSignalFilter, previewBlockFilter, previewSignalFilter, searchInstruments,
} from "../api.js";
import { fmt } from "../format.js";
import { bookMode, eventTag, maturityTxt, reasonDelta, reasonTitle, sideInfo, tradeMode, tradeTone } from "../signalFormat.js";

const RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B"];
// Порог «шевеления»: насколько должна сдвинуться цена, спред или объём, чтобы
// прилетело повторное уведомление по уже найденной бумаге.
const CHANGES = [[5, "5 %"], [10, "10 %"], [20, "20 %"], [50, "50 %"]];
// block — не срабатывание фильтра, а рыночное событие (filter_id=0), поэтому
// filter_name у него пустой: подпись собираем сами, см. sig-hit-meta ниже.
// текст плашки — общий с колокольчиком (signalFormat.eventTag)

// единая единица проекта — млн ₽ голым числом (см. fmt.mln)
const money = (v) => (v == null ? "—" : fmt.mln(v));

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
const SIDES = [["any", "любая"], ["buy", "buy"], ["sell", "sell"]];
// ОФЗ или корпораты — режется в обоих видах сигналов: суверен и корп живут в
// разных диапазонах спреда, вместе они шумят друг другу. В форме это два чипа
// рядом с рейтингом (ни одного = весь рынок, как у рейтинга), в API — одно
// значение issuer, поэтому «оба отмечены» это тоже «все».
const ISSUERS = [["ofz", "ОФЗ"], ["corp", "корп"]];
const ISSUER_TXT = { ofz: "только ОФЗ", corp: "без ОФЗ" };
const issuerChips = (v) => (v === "ofz" || v === "corp" ? [v] : []);
const chipsIssuer = (a) => (a.length === 1 ? a[0] : "all");
const labelOfPair = (pairs, v) => (pairs.find(([x]) => x === v) || [null, v])[1];

/** Сегментированный переключатель одного значения. */
function Seg({ pairs, value, onChange, tone }) {
  return (
    <div className="sig-seg">
      {pairs.map(([v, t]) => (
        <button type="button" key={v}
          className={value === v ? "on" + (tone ? " " + tone(v) : "") : ""}
          onClick={() => onChange(v)}>{t}</button>
      ))}
    </div>
  );
}

/** Способ оповещения — общий хвост обеих форм. */
function Notify({ sound, setSound, desktop, setDesktop, target, setTarget, targets }) {
  return (
    <div className="sig-field">
      <label className="sig-label">Как оповещать</label>
      <div className="sig-checks">
        <label><input type="checkbox" checked={sound}
          onChange={(e) => setSound(e.target.checked)} /> звук</label>
        <label><input type="checkbox" checked={desktop}
          onChange={(e) => setDesktop(e.target.checked)} /> окно системы</label>
      </div>
      {/* Канал доставки — на фильтр: «Р5» уходит в свой канал, «Ф5» в свой.
          Селектор показываем, только если каналы вообще заведены: пустой
          выпадающий список объяснял бы функцию, которой у пользователя нет. */}
      {targets && targets.length > 0 && (
        <div className="sig-row tight" style={{ marginTop: 6 }}>
          <label className="sig-label" style={{ margin: 0 }}>Telegram</label>
          <select className="sig-input" value={target ?? ""}
            onChange={(e) => setTarget(e.target.value === "" ? null : Number(e.target.value))}>
            <option value="">мои личные чаты</option>
            {targets.map((t) => (
              <option key={t.id} value={t.id}>{t.title}</option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

/** Куда уходит фильтр в телеграме — в карточке списка. Без канала не пишем
 *  ничего: «в личку» это поведение по умолчанию, и строка только шумела бы. */
function tgTargetTitle(f, targets) {
  if (!f.tg_target_id) return "";
  const t = (targets || []).find((x) => x.id === f.tg_target_id);
  return ` · ✈ ${t ? t.title : "канал"}`;
}

/** Каналы доставки аккаунта. Заводятся в боте (переслать пост из канала),
 *  здесь только выбираются — список общий для обеих форм. */
let _targetsCache = null;      // один запрос на страницу: список правят в боте,
                               // а карточек фильтров на экране десятки
function useTgTargets() {
  const [targets, setTargets] = useState(_targetsCache?.value || []);
  useEffect(() => {
    let alive = true;
    if (!_targetsCache) {
      _targetsCache = { value: [], promise: fetchSignalTargets().catch(() => ({})) };
      _targetsCache.promise.then((r) => { _targetsCache.value = r.targets || []; });
    }
    _targetsCache.promise.then(() => { if (alive) setTargets(_targetsCache.value); });
    return () => { alive = false; };
  }, []);
  return targets;
}

/** Пара «от / до» — все диапазоны обеих форм вводятся одинаково. */
function RangeInputs({ min, max, setMin, setMax, step }) {
  return (
    <div className="sig-row tight">
      <input className="sig-input num" type="number" step={step} placeholder="от"
        value={min} onChange={(e) => setMin(e.target.value)} />
      <input className="sig-input num" type="number" step={step} placeholder="до"
        value={max} onChange={(e) => setMax(e.target.value)} />
    </div>
  );
}

/** Блок «какие бумаги» — общий у обеих форм: один порядок полей, одни подписи.
 *  Отбор бумаг ОДИНАКОВ у стакана и у сделок, поэтому и выглядеть должен так же. */
function BondScope({ issuers, setIssuers, ratings, setRatings, emitters, setEmitters,
                     isins, setIsins, hideSub, setHideSub }) {
  const searchEmitters = useCallback(
    async (q) => (await fetchSignalEmitters(q)).emitters.map((n) => ({ n })), []);
  const searchBonds = useCallback(async (q) => (await searchInstruments(q)).results, []);
  return (
    <>
      <div className="sig-section">Какие бумаги <span>селекторы объединяются по «или»</span></div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Эмитент</label>
          <div className="sig-chips">
            {ISSUERS.map(([v, t]) => (
              <button type="button" key={v}
                className={"sig-chip" + (issuers.includes(v) ? " on" : "")}
                onClick={() => setIssuers(issuers.includes(v)
                  ? issuers.filter((x) => x !== v) : [...issuers, v])}>{t}</button>
            ))}
          </div>
        </div>
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
      </div>

      <MultiPicker label="Эмитенты" placeholder="начни вводить название"
        items={emitters} onChange={setEmitters} search={searchEmitters}
        keyOf={(x) => x.n} labelOf={(x) => x.n} subOf={() => ""} />

      <MultiPicker label="Отдельные бумаги" placeholder="ISIN или название"
        items={isins} onChange={setIsins} search={searchBonds}
        keyOf={(r) => r.isin} labelOf={(r) => r.name}
        subOf={(r) => r.isin + (r.rating ? " · " + r.rating : "")} />

      <div className="sig-field">
        <label className="sig-check-line" title="Опознаём по названию (СУБ, Т1, перп): признака в реестре нет. Суборды дают широчайший спред из-за риска списания и иначе занимают весь верх выдачи.">
          <input type="checkbox" checked={hideSub}
            onChange={(e) => setHideSub(e.target.checked)} />
          <span>Прятать суборды</span>
        </label>
      </div>
    </>
  );
}

/** «250–400 бп» / «от 250 бп» / «до 400 бп» — одна подпись на все диапазоны. */
function rangeTxt(min, max, unit) {
  if (min != null && max != null) return `${min}–${max} ${unit}`;
  if (min != null) return `от ${min} ${unit}`;
  if (max != null) return `до ${max} ${unit}`;
  return null;
}
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
  const spread = rangeTxt(p.spread_min, p.spread_max, "бп");
  const years = rangeTxt(p.years_min, p.years_max, "л");
  return [`от ${fmt.mln(p.min_value_rub)} млн`, labelOfPair(MARKETS, p.markets), bases,
          p.side !== "any" ? labelOfPair(SIDES, p.side) : null,
          spread ? `R-spread ${spread}` : null,
          years ? `срок ${years}` : null].filter(Boolean).join(" · ");
}

/** Человеческое описание условий фильтра — одной строкой, как в карточке. */
function describe(p) {
  const who = [];
  if (p.ratings?.length) who.push("рейтинг " + p.ratings.join("/"));
  if (p.emitters?.length)
    who.push(p.emitters.length === 1 ? p.emitters[0] : p.emitters.length + " эмитентов");
  if (p.isins?.length) who.push(p.isins.length + " бумаг");
  let scope = who.length ? who.join(" или ")
    : p.issuer === "ofz" ? "все ОФЗ"
    : p.issuer === "corp" ? "весь рынок без ОФЗ" : "весь рынок";
  if (who.length && ISSUER_TXT[p.issuer]) scope += ", " + ISSUER_TXT[p.issuer];
  if (p.hide_subord) scope += ", без субордов";
  const range = rangeTxt(p.spread_min, p.spread_max, "бп");
  const years = rangeTxt(p.years_min, p.years_max, "л");
  let moneyTxt = null;
  if (p.min_money_rub) {
    const m = fmt.mln(p.min_money_rub);
    moneyTxt = p.money_mode === "single" ? `заявка от ${m} млн` : `набор от ${m} млн`;
  }
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
  const [issuers, setIssuers] = useState(issuerChips(ep.issuer));
  const [side, setSide] = useState(ep.side || "ask");
  const [smin, setSmin] = useState(numOrEmpty(ep.spread_min));
  const [smax, setSmax] = useState(numOrEmpty(ep.spread_max));
  const [minMoney, setMinMoney] = useState(rubToMln(ep.min_money_rub));  // млн ₽
  const [moneyMode, setMoneyMode] = useState(ep.money_mode || "book");
  const [ymin, setYmin] = useState(numOrEmpty(ep.years_min));
  const [ymax, setYmax] = useState(numOrEmpty(ep.years_max));
  const [hideSub, setHideSub] = useState(!!ep.hide_subord);
  const [repeatMoney, setRepeatMoney] = useState(ep.repeat_on_money !== false);
  const [changePct, setChangePct] = useState(edit?.change_pct ?? 10);
  const [sound, setSound] = useState(edit ? !!edit.sound : true);
  const [desktop, setDesktop] = useState(edit ? !!edit.desktop : true);
  const [target, setTarget] = useState(edit?.tg_target_id ?? null);
  const targets = useTgTargets();
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState(null);

  const params = useMemo(() => ({
    ratings, emitters, isins, issuer: chipsIssuer(issuers), side,
    spread_min: smin === "" ? null : Number(smin),
    spread_max: smax === "" ? null : Number(smax),
    min_money_rub: mlnToRub(minMoney),
    money_mode: moneyMode,
    years_min: ymin === "" ? null : Number(ymin),
    years_max: ymax === "" ? null : Number(ymax),
    hide_subord: hideSub,
    repeat_on_money: repeatMoney,
  }), [ratings, emitters, isins, issuers, side, smin, smax, minMoney, moneyMode,
       ymin, ymax, hideSub, repeatMoney]);

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

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!name.trim()) { setErr("Дай сигналу название"); return; }
    if (smin === "" && smax === "" && minMoney === "") {
      setErr("Задай диапазон R-spread или объём — иначе условий нет"); return; }
    try {
      await onSubmit({ name: name.trim(), kind: "book", params,
                       change_pct: changePct, sound, desktop,
                       tg_target_id: target });
      if (edit) return;      // правка закрывает форму снаружи
      setName(""); setRatings([]); setEmitters([]); setIsins([]); setIssuers([]);
      setSmin(""); setSmax(""); setMinMoney(""); setYmin(""); setYmax("");
      setMoneyMode("book"); setHideSub(false); setPreview(null);
    } catch (e2) { setErr(e2.message); }
  };

  return (
    <form className={"sig-form" + (edit ? " editing" : "")} onSubmit={submit}>
      <div className="sig-form-head">
        {edit ? `Правка: ${edit.name}` : "Новый сигнал"}
        <button type="button" className="sig-cancel" onClick={onCancel}>Отмена</button>
      </div>

      <div className="sig-field">
        <label className="sig-label" htmlFor="sig-name">Название</label>
        <input id="sig-name" className="sig-input" value={name} placeholder="например, широкие ААА"
          onChange={(e) => setName(e.target.value)} />
      </div>

      <BondScope issuers={issuers} setIssuers={setIssuers} ratings={ratings} setRatings={setRatings}
        emitters={emitters} setEmitters={setEmitters} isins={isins} setIsins={setIsins}
        hideSub={hideSub} setHideSub={setHideSub} />

      <div className="sig-section">Условия <span>складываются по «и»</span></div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Сторона стакана</label>
          <Seg pairs={[["ask", "оффер"], ["bid", "бид"]]} value={side} onChange={setSide}
            tone={(v) => (v === "ask" ? "up" : "down")} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Диапазон R-spread, бп</label>
          <RangeInputs min={smin} max={smax} setMin={setSmin} setMax={setSmax} />
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
          <RangeInputs min={ymin} max={ymax} setMin={setYmin} setMax={setYmax} step="any" />
        </div>
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Повторно сообщать при сдвиге</label>
          <Seg pairs={CHANGES} value={changePct} onChange={setChangePct} />
          {/* Спред звонит всегда, объём — по желанию: у ликвидной бумаги стакан
              дышит объёмом постоянно, и тому, кто следит за уровнем спреда, это
              шум. Первое попадание бумаги в набор приходит в любом случае. */}
          <label className="sig-check-inline" title="Стакан ликвидной бумаги дышит объёмом постоянно — выключи, если следишь только за уровнем спреда">
            <input type="checkbox" checked={repeatMoney}
              onChange={(e) => setRepeatMoney(e.target.checked)} /> и при изменении объёма
          </label>
        </div>
        {minMoney !== "" && (
          <div className="sig-field">
            <label className="sig-label">Как считать объём</label>
            <Seg pairs={[["book", "набором"], ["single", "одной заявкой"]]}
              value={moneyMode} onChange={setMoneyMode} />
          </div>
        )}
      </div>

      {minMoney !== "" && (
        <div className="sig-note">
          {moneyMode === "book"
            ? "Сумма набирается по лестнице от лучшей цены; цена и спред — по средневзвесу набора."
            : "Ждём ОДНУ заявку не меньше суммы; двадцать мелких на ту же сумму сигналом не считаются."}
        </div>
      )}

      <Notify sound={sound} setSound={setSound} desktop={desktop} setDesktop={setDesktop}
        target={target} setTarget={setTarget} targets={targets} />

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

/** Форма фильтра крупной сделки. Скелет тот же, что у фильтра стакана
 *  (название → какие бумаги → условия → оповещение → превью), и условия идут в
 *  том же порядке: сторона+спред, объём+срок, третьей строкой своё для вида.
 *  Разница только в смысле полей: событие — факт сделки в ленте (в т.ч.
 *  адресной РПС, которой в стакане не бывает), а не состояние очереди. */
function BlockForm({ onSubmit, busy, edit, onCancel }) {
  const ep = edit?.params || {};
  const [name, setName] = useState(edit?.name || "");
  const [ratings, setRatings] = useState(ep.ratings || []);
  const [emitters, setEmitters] = useState(ep.emitters || []);
  const [isins, setIsins] = useState(ep.isins || []);
  const [issuers, setIssuers] = useState(issuerChips(ep.issuer));
  const [bases, setBases] = useState(ep.bases || ["KEYRATE", "RUONIA"]);
  const [minValue, setMinValue] = useState(rubToMln(ep.min_value_rub ?? 100000000));  // млн ₽
  const [markets, setMarkets] = useState(ep.markets || "all");
  const [side, setSide] = useState(ep.side || "any");
  const [smin, setSmin] = useState(numOrEmpty(ep.spread_min));
  const [smax, setSmax] = useState(numOrEmpty(ep.spread_max));
  const [ymin, setYmin] = useState(numOrEmpty(ep.years_min));
  const [ymax, setYmax] = useState(numOrEmpty(ep.years_max));
  const [hideSub, setHideSub] = useState(!!ep.hide_subord);
  const [sound, setSound] = useState(edit ? !!edit.sound : true);
  const [desktop, setDesktop] = useState(edit ? !!edit.desktop : true);
  const [target, setTarget] = useState(edit?.tg_target_id ?? null);
  const targets = useTgTargets();
  const [err, setErr] = useState("");
  const [preview, setPreview] = useState(null);

  const params = useMemo(() => ({
    ratings, emitters, isins, issuer: chipsIssuer(issuers), bases, markets,
    side, hide_subord: hideSub,
    min_value_rub: mlnToRub(minValue),
    spread_min: smin === "" ? null : Number(smin),
    spread_max: smax === "" ? null : Number(smax),
    years_min: ymin === "" ? null : Number(ymin),
    years_max: ymax === "" ? null : Number(ymax),
  }), [ratings, emitters, isins, issuers, bases, markets, side, hideSub, minValue,
       smin, smax, ymin, ymax]);

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

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!name.trim()) { setErr("Дай сигналу название"); return; }
    if (!(mlnToRub(minValue) >= 1e6)) {
      setErr("Порог объёма: от 1 млн ₽ — мельче лента не хранит"); return; }
    try {
      await onSubmit({ name: name.trim(), kind: "block", params, sound, desktop,
                       tg_target_id: target });
      if (edit) return;
      setName(""); setRatings([]); setEmitters([]); setIsins([]); setIssuers([]);
      setSmin(""); setSmax(""); setYmin(""); setYmax(""); setPreview(null);
    } catch (e2) { setErr(e2.message); }
  };

  return (
    <form className={"sig-form" + (edit ? " editing" : "")} onSubmit={submit}>
      <div className="sig-form-head">
        {edit ? `Правка: ${edit.name}` : "Новый сигнал: крупная сделка"}
        <button type="button" className="sig-cancel" onClick={onCancel}>Отмена</button>
      </div>

      <div className="sig-field">
        <label className="sig-label" htmlFor="blk-name">Название</label>
        <input id="blk-name" className="sig-input" value={name} placeholder="например, блоки в моих эмитентах"
          onChange={(e) => setName(e.target.value)} />
      </div>

      <BondScope issuers={issuers} setIssuers={setIssuers} ratings={ratings} setRatings={setRatings}
        emitters={emitters} setEmitters={setEmitters} isins={isins} setIsins={setIsins}
        hideSub={hideSub} setHideSub={setHideSub} />

      <div className="sig-section">Условия <span>складываются по «и»</span></div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Сторона (агрессор)</label>
          <Seg pairs={SIDES} value={side} onChange={setSide} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Диапазон R-spread, бп</label>
          <RangeInputs min={smin} max={smax} setMin={setSmin} setMax={setSmax} />
        </div>
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label" htmlFor="blk-money">Объём сделки от, млн ₽</label>
          <input id="blk-money" className="sig-input num" type="number" step="1" min="1"
            value={minValue} onChange={(e) => setMinValue(e.target.value)} />
        </div>
        <div className="sig-field">
          <label className="sig-label">Срок до погашения, лет</label>
          <RangeInputs min={ymin} max={ymax} setMin={setYmin} setMax={setYmax} step="any" />
        </div>
      </div>

      <div className="sig-row">
        <div className="sig-field">
          <label className="sig-label">Режим торгов</label>
          <Seg pairs={MARKETS} value={markets} onChange={setMarkets} />
        </div>
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
      </div>

      <div className="sig-note">
        Адресные — РПС, размещения и выкупы: в стакане их не видно вообще, крупняк чаще идёт
        именно так (стороны у них нет). Пустая база — любые бумаги.
      </div>
      {(smin !== "" || smax !== "") && (
        <div className="sig-note">
          Спред сделки считается только флоатерам — с заданным диапазоном фиксы отсеются,
          даже если база «фикс» отмечена.
        </div>
      )}

      <Notify sound={sound} setSound={setSound} desktop={desktop} setDesktop={setDesktop}
        target={target} setTarget={setTarget} targets={targets} />

      {preview && (
        <div className="sig-preview">
          {preview.total === 0
            ? "Сегодня под условия не попала ни одна сделка."
            : <>Сегодня под условия {plural(preview.total, "попала", "попало", "попало")}{" "}
                <b>{preview.total}</b>{preview.capped ? "+" : ""}{" "}
                {plural(preview.total, "сделка", "сделки", "сделок")}:{" "}
                {preview.matches.slice(0, 4).map(
                  (m) => `${m.name} (${money(m.money_rub)} млн)`).join(", ")}
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
  const targets = useTgTargets();
  if (f.kind === "block") {
    return (
      <div className={"sig-row-card" + (f.enabled ? "" : " off") + (editing ? " on-edit" : "")}>
        <div className="sig-rc-main">
          <div className="sig-rc-title">
            {f.name}
            <span className="sb-tag sb-block">сделки</span>
            <span className={"sig-state " + (f.enabled ? "on" : "off")}>
              {f.enabled ? "включён" : "выключен"}</span>
          </div>
          <div className="sig-rc-sub">{describe(f.params).scope}</div>
          <div className="sig-rc-cond num">
            {describeBlock(f.params)}
            {f.sound ? " · звук" : ""}{f.desktop ? " · окно" : ""}
            {tgTargetTitle(f, targets)}
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
  const targets = useTgTargets();
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
          {/* оффер красный, бид зелёный — как в ленте событий */}
          <span className={f.params.side === "ask" ? "neg" : "pos"}>
            {f.params.side === "ask" ? "оффер" : "бид"}</span>
          {d.range ? ` · R-spread ${d.range}` : ""}
          {d.moneyTxt ? ` · ${d.moneyTxt}` : ""}
          {d.years ? ` · срок ${d.years}` : ""}
          {" · сдвиг "}{chLabel(f.change_pct)}
          {f.params.repeat_on_money === false ? " (только спред)" : ""}
          {f.sound ? " · звук" : ""}{f.desktop ? " · окно" : ""}
          {tgTargetTitle(f, targets)}
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
const REPEAT = { price: 1, spread: 1, money: 1 };   // причины-повторы (для стиля строки)

/** Колонка одного вида сигналов: список фильтров + своя форма под ним.
 *  Виды не переключаются табом — они стоят рядом, потому что настраивают их
 *  вместе (порог блока смотрят на условия стакана и наоборот). */
function FilterColumn({ title, hint, empty, Form, formKey, rows, editing, loading,
                        busy, onToggle, onEdit, onDelete, onCancel, onSubmit,
                        onDeleteAll }) {
  // Форма длинная (два десятка полей). Пока её держали раскрытой, список уже
  // заведённых фильтров тонул под ней — открываем по кнопке; правка открывает сама.
  const [adding, setAdding] = useState(false);
  const open = adding || !!editing;
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

      {!open && (
        <button className="btn sig-add" onClick={() => setAdding(true)}>
          + Новый сигнал</button>
      )}

      {/* key переинициализирует поля при смене правимого фильтра */}
      {open && (
        <Form key={editing?.id ?? formKey} edit={editing} busy={busy}
          onCancel={() => { setAdding(false); onCancel(); }}
          onSubmit={async (body) => { await onSubmit(body); setAdding(false); }} />
      )}
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
                  + (REPEAT[h.reason] ? " hit-repeat" : "")
                  + (tradeTone(h) ? " sb-t-" + tradeTone(h) : "")}>
                <div className="sig-hit-top">
                  <span className="sig-hit-name">{h.name || h.isin}</span>
                  <span className={"sb-tag sb-" + h.reason}>{eventTag(h)}</span>
                  <span className="sig-hit-time num">{timeOf(h.fired_at)}</span>
                </div>
                <div className="sig-hit-body num">
                  {/* у крупной сделки сторона — агрессор (buy/sell), а не сторона
                      стакана; у адресной её нет вообще */}
                  <span className={sideInfo(h).cls}>{sideInfo(h).text}</span>
                  {h.val_bps != null && (
                    <> · <span className="sig-hit-k">Y-IDX</span> <b>{fmt.num(h.val_bps, 0)} бп</b></>
                  )}
                  {h.price != null && <> · {fmt.num(h.price, 2)}%</>}
                  {h.money_rub != null && <> · {money(h.money_rub)} млн</>}
                </div>
                <div className="sig-hit-mode">
                  {[tradeMode(h), bookMode(h), maturityTxt(h)].filter(Boolean).join(" · ")}
                  {reasonDelta(h) && (
                    <span className="sig-why" title={reasonTitle(h)}>
                      {[tradeMode(h), bookMode(h), maturityTxt(h)].filter(Boolean).length ? " · " : ""}
                      {reasonDelta(h)}</span>
                  )}
                </div>
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
