import { useQuery } from "@tanstack/react-query";
import { fetchWeekly } from "../api.js";
import { fmt } from "../format.js";

const D = "—";
const WEEKS = 12;
// «29.09» из ISO-даты
const ddmm = (s) => (s ? s.slice(8, 10) + "." + s.slice(5, 7) : "");

// Строка аукциона под неделей: одна строка текста на всю ширину таблицы —
// у аукциона свои поля (доходность, размещено), в колонки вторички они не ложатся.
function AuctionLine({ a }) {
  const what = (a.fmt === "drpa" ? "ДРПА " : "Аукцион ") + fmt.date(a.date);
  if (a.status === "failed") return <>{what} — не состоялся</>;
  const parts = [];
  if (a.wap_price != null) parts.push("ср.взв. " + fmt.pct(a.wap_price) + " %");
  if (a.wap_yield != null) parts.push("дох. " + fmt.pct(a.wap_yield) + " %");
  if (a.placed_mln != null) parts.push("размещено " + fmt.num(a.placed_mln, 0) + " млн ₽");
  return (
    <>
      {what}{parts.length > 0 && " · " + parts.join(" · ")}
      {a.prem_pp != null && (
        <span className="wk-prem"
          title={"Премия к вторичке: средневзвес недели минус ср.взв. цена аукциона. "
            + "Плюс — Минфин разместил дешевле рынка"
            + (a.premium_bps != null
              ? `. В доходности к YTM вторички накануне: ${fmt.devBps(a.premium_bps)} bps` : "")}>
          {" · премия " + fmt.signed(a.prem_pp, 2) + " п.п."}
        </span>
      )}
    </>
  );
}

// Средневзвес по календарным неделям (ПН–ВС): цена и спред взвешены оборотом
// дня; у ОФЗ под неделей — аукционы Минфина той же недели.
export default function WeeklyTable({ isin, kind }) {
  const isFixed = kind === "fixed";
  const q = useQuery({
    queryKey: ["weekly", isin, WEEKS],
    queryFn: () => fetchWeekly(isin, { weeks: WEEKS }),
    enabled: !!isin,
    staleTime: 300000,
  });
  const weeks = q.data?.weeks || [];
  const spKey = isFixed ? "g_spread_wap_bps" : "y_idx_wap_bps";
  // МСК-дата (UTC+3, без DST)
  const today = new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 10);

  return (
    <>
      <div className="section-title">По неделям · средневзвес</div>
      {q.isPending ? <div className="an-empty">загрузка…</div>
        : q.isError ? <div className="an-empty">не удалось загрузить</div>
        : !weeks.length ? <div className="an-empty">сделок за {WEEKS} недель нет</div>
        : (
          <div style={{ maxHeight: 340, overflow: "auto" }}>
            <table className="cf-table wk-table">
              <thead>
                <tr>
                  <th className="left">Неделя</th>
                  <th title="средневзвешенная чистая цена недели, вес — оборот дня">Средн.взв., %</th>
                  <th title="спред по средневзвешенной цене дня, взвешен оборотом дней, где он посчитан">
                    {isFixed ? "G-спред" : "spread"}, bps</th>
                  <th>Оборот, млн ₽</th>
                  <th>Сделок</th>
                </tr>
              </thead>
              <tbody>
                {weeks.flatMap((w) => [
                  <tr key={w.week}>
                    <td className="left" title={`${fmt.date(w.week)} – ${fmt.date(w.week_end)}, торговых дней: ${w.days}`}>
                      {ddmm(w.week)}–{ddmm(w.week_end)}
                      {w.week_end >= today && <span className="rc-dim"> · тек.</span>}
                    </td>
                    <td>{w.wap_pct != null ? fmt.pct(w.wap_pct) : D}</td>
                    <td>{w[spKey] != null ? fmt.bps(w[spKey]) : D}</td>
                    <td>{w.value != null ? fmt.mln(w.value) : D}</td>
                    <td>{w.trades ?? D}</td>
                  </tr>,
                  ...(w.auctions || []).map((a) => (
                    <tr key={w.week + a.date + a.fmt} className="wk-auc">
                      <td className="left" colSpan={5}><AuctionLine a={a} /></td>
                    </tr>
                  )),
                ])}
              </tbody>
            </table>
          </div>
        )}
    </>
  );
}
