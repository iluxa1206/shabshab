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
// Спред строки — тот же, что рисует общая лента: spread у флоатера, G-спред у
// фикса, посчитанный по цене самой сделки (as-of для прошлых сессий).

// Окно ленты — максимум, что вообще есть: поштучные тики Alor живут 30 дней,
// глубже история существует только дневными агрегатами. Переключателя 1/7/30
// больше нет — выбирать между «часть данных» и «все данные» смысла нет, отбор
// делает порог объёма.
const DAYS = 30;
const LIMIT = 300;
const DEFAULT_VOL_MLN = 1;

const dpart = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : "—");
// МСК-дата (UTC+3, без DST): иначе 00:00–03:00 МСК считали бы «сегодня» вчерашним
const todayMsk = () => new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);
const tpart = (s) => ((s || "").split(" ")[1] || "").slice(0, 5) || "—";

// Подпись адресной сделки по режиму. Размещение и выкуп — тоже борды NDM, но
// это не РПС между двумя контрагентами: цена размещения — 100 по книге, выкуп —
// по оферте. Раньше все три шли одним «РПС», и день размещения читался как
// поток договорных сделок. Вид приходит машинным полем board_kind (бэк,
// services.block_trades.tag_board) — подпись «Размещ.» текст для человека и
// разбирать её нельзя.
const NDM_TAGS = {
  placement: { t: "Р", cls: "bt-plc", what: "размещение" },
  buyback: { t: "В", cls: "bt-bb", what: "выкуп" },
};
const ndmTag = (r) => {
  const { t, cls, what } = NDM_TAGS[r.board_kind] || { t: "РПС", cls: "", what: "адресная сделка" };
  return { t, cls, title: what + (r.board ? ` (${r.board})` : "") };
};

// Итог по адресным в шапке — раздельно по видам, тем же правилом, что теги
// строк: «РПС 2 на 14» при строках «Р» противоречило бы само себе.
function ndmSummary(rows) {
  const acc = {};
  for (const r of rows) {
    if (!r.negotiated) continue;
    const k = NDM_TAGS[r.board_kind] ? r.board_kind : "rps";
    acc[k] = acc[k] || { n: 0, value: 0 };
    acc[k].n += 1;
    acc[k].value += r.value || 0;
  }
  return ["rps", "placement", "buyback"].filter((k) => acc[k]).map((k) =>
    `${k === "rps" ? "РПС" : NDM_TAGS[k].t} ${acc[k].n} на ${fmt.mln(acc[k].value) ?? "—"}`);
}

export default function BondTrades({ isin, kind, onClose }) {
  const isFixed = kind === "fixed";
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
    queryKey: ["bond-trades", isin, kind, volMln],
    // refresh=true дёргает дрейн тиков по бумаге — он и так нужен соседним
    // слоям карточки. Порог объёма фильтрует НА БЭКЕ (min_value в ₽): под
    // лимитом строк тогда остаются крупные принты, а не последние по времени.
    queryFn: () => fetchTrades(isin, { days: DAYS, minValue: Math.round(volMln * 1e6),
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
  // в окне 30 дней глаз должен сразу отделять живой день от истории
  const today = todayMsk();
  // Полоса на день: подложка чередуется при СМЕНЕ ДАТЫ, а не через строку —
  // за 30 дней лента это сплошной столбик цифр, и границу сессии иначе не
  // видно. Дата у сделки повторяется в каждой строке, но глаз её не считывает.
  let band = 0, prevDay = null;
  const bandOf = (r) => {
    const day = String(r.ts || "").slice(0, 10);
    if (prevDay !== null && day !== prevDay) band ^= 1;
    prevDay = day;
    return band;
  };

  return (
    <div className="ob-panel-inner">
      <div className="ob-head">
        <div className="ob-title">Сделки</div>
        <button className="btn ob-close" onClick={onClose} aria-label="Закрыть ленту сделок">✕</button>
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
            + ndmSummary(rows).map((x) => ` · ${x}`).join("")
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
                <th title={isFixed ? "G-спред по цене сделки" : "spread по цене сделки"}>
                  {isFixed ? "G-спред" : "spread"}</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const sp = spreadOf(r);
                const bd = bandOf(r);
                const tag = r.negotiated ? ndmTag(r) : null;
                return (
                  <tr key={r.trade_id}
                    className={"bt-row" + (bd ? " bt-band" : "") + (tag ? " bt-ndm"
                      : r.side === "buy" ? " bt-buy" : r.side === "sell" ? " bt-sell" : "")}
                    title={`${r.ts} · ${fmt.num(r.qty, 0)} шт`
                      + (r.side ? ` · агрессор ${r.side}` : "")
                      + (tag ? ` · ${tag.title}` : "")}>
                    <td className={"left bt-d" + (String(r.ts || "").slice(0, 10) === today ? " bt-today" : "")}>
                      {dpart(r.ts)}</td>
                    <td className="left bt-d">{tpart(r.ts)}</td>
                    <td>{fmt.pct(r.price) ?? "—"}</td>
                    {/* у адресной сделки агрессора нет по определению — она
                        договорная; вместо стороны показываем сам режим */}
                    <td className={"bt-side" + (tag?.cls ? " " + tag.cls : "")} title={tag?.title}>
                      {tag ? tag.t
                      : r.side === "buy" ? "buy"
                      : r.side === "sell" ? "sell" : "—"}</td>
                    <td>{fmt.mln1(r.value) ?? "—"}</td>
                    <td style={sp == null ? undefined : dmColor(sp)}>{fmt.bps(sp) ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

    </div>
  );
}
