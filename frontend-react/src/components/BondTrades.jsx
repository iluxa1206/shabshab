import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchTrades } from "../api.js";

// Сжатая лента сделок ОДНОЙ бумаги — третья панель карточки, слева от стакана.
// Смысл: стакан показывает, по чему ГОТОВЫ торговать, лента — по чему УЖЕ
// сторговали. Колонки урезаны до пяти (дата, время, цена, объём, спред): полный
// набор фильтров и метрик живёт на вкладке СДЕЛКИ, сюда он не влезает и не нужен.
// Спред строки — тот же, что рисует общая лента: R-spread у флоатера, G-спред у
// фикса, посчитанный по цене самой сделки (as-of для прошлых сессий).

const WINDOWS = [1, 7, 30];
// Порог объёма в МИЛЛИОНАХ ₽ — та же единица денег, что во всём интерфейсе.
// Отсекает розничную мелочь (лента бумаги на 90% состоит из сделок по 1-5 тыс ₽),
// оставляя принты, по которым видно реальный уровень.
const VOLS = [0, 0.5, 1, 5, 10];
const LIMIT = 300;

const dpart = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : "—");
const tpart = (s) => ((s || "").split(" ")[1] || "").slice(0, 5) || "—";

export default function BondTrades({ isin, kind, onClose }) {
  const isFixed = kind === "fixed";
  const [days, setDays] = useState(7);
  const [volMln, setVolMln] = useState(0);

  const q = useQuery({
    queryKey: ["bond-trades", isin, kind, days, volMln],
    // refresh=true дёргает дрейн тиков по бумаге — он и так нужен соседним
    // слоям карточки. Порог объёма фильтрует НА БЭКЕ (min_value в ₽): под
    // лимитом строк тогда остаются крупные принты, а не последние по времени.
    queryFn: () => fetchTrades(isin, { days, minValue: Math.round(volMln * 1e6),
                                       limit: LIMIT, kind: isFixed ? "fixed" : "floater" }),
    enabled: !!isin,
    // сделки не тикают так же часто, как стакан: 30с хватает, а дрейн дорогой
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const d = q.data;
  const rows = d?.trades || [];
  // лента читается сверху вниз от свежего: бэк отдаёт по возрастанию времени
  const shown = [...rows].reverse();
  const spreadOf = (r) => (isFixed ? r.g_spread_bps : r.y_idx_bps);

  return (
    <div className="ob-panel-inner">
      <div className="ob-head">
        <div className="ob-title">Сделки</div>
        <button className="btn ob-close" onClick={onClose} aria-label="Закрыть ленту сделок">✕</button>
      </div>

      <div className="ob-ctl bt-ctl">
        {WINDOWS.map((n) => (
          <button key={n} className={"chip-btn" + (days === n ? " on" : "")}
            onClick={() => setDays(n)} title={`окно ${n} дн`}>{n}д</button>
        ))}
      </div>

      <div className="ob-ctl bt-ctl">
        <span className="bt-ctl-lbl" title="Показывать сделки не меньше порога, млн ₽">от, млн</span>
        {VOLS.map((v) => (
          <button key={v} className={"chip-btn" + (volMln === v ? " on" : "")}
            onClick={() => setVolMln(v)}
            title={v === 0 ? "все сделки" : `сделки от ${fmt.num(v, v < 1 ? 1 : 0)} млн ₽`}>
            {v === 0 ? "все" : fmt.num(v, v < 1 ? 1 : 0)}
          </button>
        ))}
      </div>

      <div className="ob-status">
        {q.isLoading ? "загрузка…"
          : q.isError ? "нет данных"
          : rows.length === 0
            ? (volMln > 0 ? "нет сделок крупнее порога" : "сделок за окно нет")
          : `${d.n} сд · оборот ${fmt.mln(d.value) ?? "—"} млн ₽`
            + (d.truncated ? ` · последние ${LIMIT} из ${d.total}` : "")}
      </div>

      <div className="ob-scroll">
        {rows.length > 0 && (
          <table className="ob-table bt-table">
            <thead>
              <tr>
                <th className="left">Дата</th>
                <th className="left">Время</th>
                <th>Цена</th>
                <th title="объём сделки, млн ₽">Объём</th>
                <th title={isFixed ? "G-спред по цене сделки" : "R-spread по цене сделки"}>
                  {isFixed ? "G-спред" : "R-spread"}</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const sp = spreadOf(r);
                return (
                  <tr key={r.trade_id}
                    className={"bt-row" + (r.side === "buy" ? " bt-buy" : r.side === "sell" ? " bt-sell" : "")}
                    title={`${r.ts} · ${fmt.num(r.qty, 0)} шт`
                      + (r.side ? ` · агрессор ${r.side}` : "")}>
                    <td className="left bt-d">{dpart(r.ts)}</td>
                    <td className="left bt-d">{tpart(r.ts)}</td>
                    <td>{fmt.pct(r.price) ?? "—"}</td>
                    <td>{fmt.mln(r.value) ?? "—"}</td>
                    <td style={sp == null ? undefined : dmColor(sp)}>{fmt.bps(sp) ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="ob-note">
        Объём — млн ₽. Цвет строки — агрессор (покупка/продажа). Спред считается
        по цене сделки; прошлые сессии — моделью того дня.
      </div>
    </div>
  );
}
