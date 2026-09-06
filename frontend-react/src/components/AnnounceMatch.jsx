import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { fetchPrimaryAnnounces } from "../api.js";
import { fmt } from "../format.js";

// СВЕРКА «ОРИЕНТИР ↔ ФАКТ»: где закрылась книга относительно того, что обещал
// организатор. Ориентир почти всегда потолок («КС + не выше 300 бп»), и вопрос
// не в нём, а в расстоянии до факта.
//
// Архив копится с момента, когда завели таблицу: выгрузка bondresearch держит
// только ~20 будущих размещений и перезаписывается, восстановить прошлые
// ориентиры неоткуда. Поэтому пустая таблица в первые недели — норма, а не сбой.
//
// Матч по серии выпуска внутри имени («Т Плюс 002Р-03» ↔ анонс «002Р-03») плюс
// эмитент; score показывает, на чём сошлись, — привязка автоматическая, и её
// цена ошибки выше цены пробела.

const guide = (s) => (s || "").replace(/^ставка купона\s*/i, "").trim() || "—";

export default function AnnounceMatch() {
  const [onlyMatched, setOnlyMatched] = useState(false);
  const nav = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ["primary-announces"],
    queryFn: () => fetchPrimaryAnnounces({ limit: 500 }),
    staleTime: 3600e3,
  });

  const rows = useMemo(() => {
    const all = data?.rows || [];
    return onlyMatched ? all.filter((r) => r.matched_secid) : all;
  }, [data, onlyMatched]);

  const matched = (data?.rows || []).filter((r) => r.matched_secid).length;

  if (isLoading) return <div className="ia-hint">Загрузка…</div>;
  if (error) return <div className="ia-hint">Не удалось загрузить архив анонсов</div>;

  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          архив ориентиров организатора рядом с фактом размещения. Ориентир —
          потолок, а не прогноз, поэтому интересна разница: спред книги считается
          нашей моделью на дату размещения. Архив копится с момента запуска —
          выгрузка источника хранит только ближайшие анонсы, прошлых ориентиров
          не существует нигде{" · "}{rows.length} анонсов · сведено {matched}
        </span>
        <div className="ia-filters">
          <button className={"chip-btn" + (onlyMatched ? " on" : "")}
                  onClick={() => setOnlyMatched((v) => !v)}>
            Только сведённые
          </button>
        </div>
      </div>

      <table className="grid packed">
        <thead>
          <tr>
            <th className="left">Анонс</th>
            <th className="left">Эмитент</th>
            <th className="left">Серия</th>
            <th className="left">Ориентир</th>
            <th className="num" title="млн ₽, ориентир организатора">Объём</th>
            <th className="left">Факт</th>
            <th className="num" title="% номинала">Цена</th>
            <th className="num" title="базисные пункты, Y-IDX по цене книги">Спред факта</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="left">{fmt.date(r.issue_date || r.book_date) || "—"}</td>
              <td className="left">{r.issuer}</td>
              <td className="left">{r.series || "—"}</td>
              <td className="left" title={r.coupon_guide || ""}>
                <span className={"pri-type " + (r.is_floater ? "pri-fl" : "pri-fx")}>
                  {r.is_floater ? "флоатер" : "фикс"}
                </span>
                {" "}{guide(r.coupon_guide)}
              </td>
              <td className="num">{fmt.num(r.volume_mln, 0) || "—"}</td>
              <td className="left">
                {r.matched_secid ? (
                  <a href="#" onClick={(e) => { e.preventDefault(); nav(`/chart/${r.matched_secid}`); }}
                     /* score < 1 — сошлись не по всем признакам: привязку стоит
                        перепроверить глазами, поэтому она помечена */
                     title={`${r.fact_name || r.matched_secid} · ${fmt.date(r.fact_date)}`
                            + (r.match_score < 1 ? ` · привязка по совпадению ${r.match_score}` : "")}>
                    {fmt.date(r.fact_date) || r.matched_secid}
                    {r.match_score < 1 && <span className="mut">?</span>}
                  </a>
                ) : <span className="mut">ждём</span>}
              </td>
              <td className="num">{fmt.pct(r.fact_price) || "—"}</td>
              <td className="num pri-spread">{fmt.bps(r.fact_spread_bps) || "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={8} className="left mut">
              Архив пуст — он наполняется с каждой выгрузкой анонсов
            </td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
