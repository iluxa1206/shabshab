import { useQuery } from "@tanstack/react-query";
import { fetchStatus } from "../api.js";
import { fmt } from "../format.js";

function Dot({ up }) {
  return <span className={"st-dot " + (up ? "up" : "down")} />;
}

// Полоса заполненности X/Y с процентом
function Bar({ n, total, pct }) {
  const cls = pct >= 90 ? "full" : pct >= 50 ? "part" : "low";
  return (
    <div className="st-bar-wrap">
      <div className="st-bar-track"><div className={"st-bar-fill " + cls} style={{ width: pct + "%" }} /></div>
      <span className="st-bar-num">{fmt.num(n, 0)} / {fmt.num(total, 0)} <b>{pct}%</b></span>
    </div>
  );
}

// «2 мин 30 с» — время на прогресс-баре читается взглядом, секунды в тысячах не
const dur = (sec) => {
  if (sec == null) return null;
  if (sec < 60) return `${Math.round(sec)} с`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} мин${sec % 60 >= 30 ? " 30 с" : ""}`;
  return `${Math.floor(m / 60)} ч ${m % 60} мин`;
};

const JOB_STATE = {
  running: ["идёт", "run"],
  done: ["готово", "done"],
  failed: ["ошибка", "fail"],
  stale: ["оборвалась", "stale"],
};

// Полоса фоновой задачи: доля выполнения, остаток времени, что именно грузится.
// Задача без известного объёма (total=null) показывает бегущую полосу.
function JobBar({ job }) {
  const [word, cls] = JOB_STATE[job.state] || ["—", ""];
  const pct = job.pct;
  return (
    <div className="st-job">
      <div className="st-job-head">
        <span className="st-job-name">{job.label}</span>
        <span className={"st-job-state " + cls}>{word}</span>
      </div>
      <div className="st-bar-wrap">
        <div className="st-bar-track">
          {pct != null
            ? <div className={"st-bar-fill " + (job.state === "running" ? "run" : cls)}
                style={{ width: pct + "%" }} />
            : <div className={"st-bar-fill indet " + cls} />}
        </div>
        <span className="st-bar-num">
          {job.total ? <>{fmt.num(job.done, 0)} / {fmt.num(job.total, 0)} <b>{pct}%</b></>
                     : <>{fmt.num(job.done, 0)} шт</>}
        </span>
      </div>
      <div className="st-job-foot">
        {job.detail && <span className="st-job-detail">{job.detail}</span>}
        <span className="st-job-time">
          {job.state === "running" && job.eta_sec != null && <>осталось ~{dur(job.eta_sec)} · </>}
          идёт {dur(job.elapsed_sec)}
        </span>
      </div>
    </div>
  );
}

export default function StatusPage() {
  // пока что-то грузится — опрашиваем чаще, чтобы полосы двигались на глазах
  const q = useQuery({
    queryKey: ["status"],
    queryFn: fetchStatus,
    refetchInterval: (query) =>
      (query.state.data?.jobs || []).some((j) => j.state === "running") ? 5000 : 15000,
  });
  const d = q.data;

  if (q.isPending) return <section className="status-page"><div className="an-empty">загрузка…</div></section>;
  if (q.error) return <section className="status-page"><div className="an-empty">ошибка загрузки статуса</div></section>;

  return (
    <section className="status-page">
      <div className="st-grid">
        <div className="st-card">
          <div className="st-title">Подключения</div>
          <table className="st-conn">
            <tbody>
              {d.connections.map((c) => (
                <tr key={c.name}>
                  <td className="st-conn-name"><Dot up={c.up} /> {c.name}</td>
                  <td className="st-conn-det">{c.detail}</td>
                  <td className={"st-conn-state " + (c.up ? "up" : "down")}>{c.up ? "ОК" : "НЕТ"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="st-card">
          <div className="st-title">Прогрев данных <span className="st-sub">флоатеры {d.universe.floaters} · фиксы {d.universe.fixed}</span></div>
          <table className="st-data">
            <tbody>
              {d.data.map((r) => (
                <tr key={r.key}>
                  <td className="st-data-key">{r.key}<div className="st-data-hint">{r.hint}</div></td>
                  <td className="st-data-bar"><Bar n={r.n} total={r.total} pct={r.pct} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Фоновые задачи: то, что грузится ПРЯМО СЕЙЧАС. Карточку держим на
            месте и когда задач нет — иначе сетка прыгает на каждом опросе. */}
        <div className="st-card st-card-wide">
          <div className="st-title">
            Фоновые загрузки
            <span className="st-sub">
              {(d.jobs || []).filter((j) => j.state === "running").length
                ? `идёт ${(d.jobs || []).filter((j) => j.state === "running").length}`
                : "сейчас ничего не грузится"}
            </span>
          </div>
          {(d.jobs || []).length
            ? (d.jobs || []).map((j) => <JobBar key={j.key} job={j} />)
            : <div className="st-data-hint">
                Здесь появляются прогрев после рестарта, часовой обход баров,
                дрейн рейтингов и разовые бэкфиллы. Завершённые видны 30 минут.
              </div>}
        </div>

        {d.registry_queues && (
          <div className="st-card">
            <div className="st-title">Очереди реестра <span className="st-sub">manual-locked {d.registry_queues.manual_locked}</span></div>
            <table className="st-ts">
              <tbody>
                {[
                  ["Без параметров (incomplete)", d.registry_queues.incomplete],
                  ["Маржа расходится (suspect)", d.registry_queues.suspect],
                  ["Пересмотр на оферте (offer_reset)", d.registry_queues.offer_reset],
                  ["Экзотика (exotic)", d.registry_queues.exotic],
                  ["Ждут ревью (unreviewed)", d.registry_queues.unreviewed],
                ].map(([label, q]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td><b>{fmt.num(q.n, 0)}</b>{q.never_tried != null && <span className="st-data-hint"> · не пробовано {q.never_tried}</span>}{q.oldest_days != null && <span className="st-data-hint"> · голове {q.oldest_days} дн</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="st-data-hint">Растущие n и возраст головы = обогащение не сходится (голодание очереди).</div>
          </div>
        )}

        <div className="st-card">
          <div className="st-title">Драйн рейтингов (corpbonds)</div>
          <div className="st-drain">
            <span><b>{fmt.num(d.ratings_drain.rated, 0)}</b> с рейтингом</span>
            <span><b>{fmt.num(d.ratings_drain.miss, 0)}</b> промах (нет на corpbonds)</span>
            <span><b>{fmt.num(d.ratings_drain.cached, 0)}</b> в кэше</span>
          </div>
          <div className="st-data-hint">{d.ratings_drain.hint}</div>
        </div>

        <div className="st-card">
          <div className="st-title">Кэши / даты</div>
          <table className="st-ts">
            <tbody>
              <tr><td>Дата расчёта</td><td>{d.timestamps.calc_date || "—"}</td></tr>
              <tr><td>Ставки (КС/RUONIA)</td><td>{d.timestamps.rates_date || "—"}</td></tr>
              <tr><td>Фиксы (calc)</td><td>{d.timestamps.fixed_calc_date || "—"}</td></tr>
              <tr><td>Торговый день расписаний</td><td>{d.timestamps.schedules_trading_day || "—"}</td></tr>
              <tr><td>День кэша расписаний</td><td>{d.timestamps.schedules_cache_day || "—"}</td></tr>
            </tbody>
          </table>
          <div className="st-data-hint">Тяжёлый прогрев расписаний — ежедневно в 09:00 МСК (готово к 10:00).</div>
        </div>
      </div>
      <div className="st-foot">
        {q.isFetching ? "обновление…"
          : (d.jobs || []).some((j) => j.state === "running")
            ? "автообновление · 5с (идут загрузки)" : "автообновление · 15с"}
      </div>
    </section>
  );
}
