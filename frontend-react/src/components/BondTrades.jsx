import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchTrades } from "../api.js";

// Сжатая лента сделок ОДНОЙ бумаги — третья панель карточки, слева от стакана.
// Смысл: стакан показывает, по чему ГОТОВЫ торговать, лента — по чему УЖЕ
// сторговали. Колонки урезаны до пяти (дата, время, цена, объём, спред): полный
// набор фильтров и метрик живёт на вкладке СДЕЛКИ, сюда он не влезает и не нужен.
// АДРЕСНЫЕ СДЕЛКИ (РПС, размещения, выкупы) здесь тоже есть — market=all. В
// стакане их не видно в принципе, а по бумаге это часто и есть весь объём дня;
// в ленте они помечены значком РПС и не участвуют в средневзвесе (цена
// договорная). Слою маркеров на графике market=all не нужен — там адресные
// рисует отдельный слой, иначе одна сделка получила бы два маркера.
// Спред строки — тот же, что рисует общая лента: R-spread у флоатера, G-спред у
// фикса, посчитанный по цене самой сделки (as-of для прошлых сессий).

const WINDOWS = [1, 7, 30];
const LIMIT = 300;
const DEFAULT_VOL_MLN = 1;

const dpart = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : "—");
// МСК-дата (UTC+3, без DST): иначе 00:00–03:00 МСК считали бы «сегодня» вчерашним
const todayMsk = () => new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);
const tpart = (s) => ((s || "").split(" ")[1] || "").slice(0, 5) || "—";

export default function BondTrades({ isin, kind, onClose }) {
  const isFixed = kind === "fixed";
  const [days, setDays] = useState(7);
  // Порог объёма — поле ввода в МИЛЛИОНАХ ₽ (единая денежная единица интерфейса,
  // как в фильтрах вкладки СДЕЛКИ). По умолчанию 1 млн: лента бумаги почти
  // целиком из розничных сделок на пару тысяч, и грузить их каждый раз незачем —
  // очистить поле (×) можно, когда мелочь действительно нужна. Пусто = все сделки.
  // Значение уходит в запрос с задержкой: иначе каждый набранный символ дёргал бы
  // дрейн тиков.
  const [volInput, setVolInput] = useState(String(DEFAULT_VOL_MLN));
  const [volMln, setVolMln] = useState(DEFAULT_VOL_MLN);
  useEffect(() => {
    const raw = volInput.trim().replace(",", ".");
    const v = raw === "" ? 0 : parseFloat(raw);
    if (!Number.isFinite(v) || v < 0) return;
    const t = setTimeout(() => setVolMln(v), 350);
    return () => clearTimeout(t);
  }, [volInput]);

  const q = useQuery({
    queryKey: ["bond-trades", isin, kind, days, volMln],
    // refresh=true дёргает дрейн тиков по бумаге — он и так нужен соседним
    // слоям карточки. Порог объёма фильтрует НА БЭКЕ (min_value в ₽): под
    // лимитом строк тогда остаются крупные принты, а не последние по времени.
    queryFn: () => fetchTrades(isin, { days, minValue: Math.round(volMln * 1e6),
                                       limit: LIMIT, kind: isFixed ? "fixed" : "floater",
                                       market: "all" }),
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
  // сделки СЕГОДНЯШНЕЙ сессии — основным цветом текста, прошлые дни приглушены:
  // в окне 7/30 дней глаз должен сразу отделять живой день от истории
  const today = todayMsk();

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

      <div className="ob-ctl bt-ctl"
        title="Нижний порог суммы сделки, млн ₽. Пусто — все сделки.">
        <span className="bt-ctl-lbl">объём от, млн</span>
        <input className="num-input bt-vol" type="number" min="0" step="0.5" placeholder="все"
          aria-label="Объём сделки от, млн ₽"
          value={volInput} onChange={(e) => setVolInput(e.target.value)} />
        {volInput !== "" && (
          <button className="chip-btn" title="Убрать порог — показать все сделки, включая розничные"
            onClick={() => setVolInput("")}>×</button>
        )}
      </div>

      <div className="ob-status">
        {q.isLoading ? "загрузка…"
          : q.isError ? "нет данных"
          : rows.length === 0
            ? (volMln > 0 ? "нет сделок крупнее порога" : "сделок за окно нет")
          : `${d.n} сд · оборот ${fmt.mln(d.value) ?? "—"} млн ₽`
            + (d.ndm_n ? ` · РПС ${d.ndm_n} на ${fmt.mln(d.ndm_value) ?? "—"}` : "")
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
                <th title="агрессор сделки: buy — забрали оффер, sell — отдали в бид">Стор.</th>
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
                    className={"bt-row" + (r.negotiated ? " bt-ndm"
                      : r.side === "buy" ? " bt-buy" : r.side === "sell" ? " bt-sell" : "")}
                    title={`${r.ts} · ${fmt.num(r.qty, 0)} шт`
                      + (r.side ? ` · агрессор ${r.side}` : "")
                      + (r.negotiated ? ` · адресная сделка${r.board ? ` (${r.board})` : ""}` : "")}>
                    <td className={"left bt-d" + (String(r.ts || "").slice(0, 10) === today ? " bt-today" : "")}>
                      {dpart(r.ts)}</td>
                    <td className="left bt-d">{tpart(r.ts)}</td>
                    <td>{fmt.pct(r.price) ?? "—"}</td>
                    {/* у адресной сделки агрессора нет по определению — она
                        договорная; вместо стороны показываем сам режим */}
                    <td className="bt-side">{r.negotiated ? "РПС"
                      : r.side === "buy" ? "buy"
                      : r.side === "sell" ? "sell" : "—"}</td>
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
        Объём — млн ₽. Цвет строки — агрессор (покупка/продажа). «РПС» — адресная
        сделка: агрессора у неё нет, цена договорная, в средневзвес она не идёт.
        Спред считается по цене сделки; прошлые сессии — моделью того дня.
      </div>
    </div>
  );
}
