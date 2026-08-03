import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchCatalog, catalogExportUrl, importCatalogXlsx, markInstrumentReviewed, resetInstrumentManual, recheckInstrumentSpec, cbondsUrl } from "../api.js";
import { InstrumentForm } from "./AdminPanel.jsx";

const CATALOG_KEY = ["admin", "catalog"];

// Колонки справочника: [ключ, заголовок, форматтер?]. Пропуски (None) подсвечиваются.
const COLS = [
  ["short_name", "Название"],
  ["base", "База"],
  ["margin_bps", "Маржа, бп"],
  ["maturity_date", "Погашение"],
  ["issue_date", "Эмиссия"],
  ["coupon_period_days", "Период, дн"],
  ["coupons_per_year", "Куп/год"],
  ["day_count", "Base"],
  ["face_value", "Номинал"],
  ["coupon_mode", "Режим (БД)"],
  ["fixing_lag", "Лаг (БД)"],
  ["fixing_lag_unit", "Ед. лага"],
  ["avg_window_days", "Окно, дн"],
  ["br_coupon_mode", "Режим (BR)"],
  ["br_fixing_lag", "Лаг (BR)"],
  ["spec_eff", "Спека (эфф.)"],
  ["spec_backtest", "Бэктест"],
  ["cap_pct", "Кэп %"],
  ["floor_pct", "Флор %"],
  ["var_type", "Тип ставки"],
  ["coupon_text", "Формула"],
  ["rating", "Рейтинг"],
  ["source", "Источник"],
];

// Поля, обязательные для прайсинга — их пропуск красный (иначе бумага не считается).
const REQUIRED = new Set(["base", "margin_bps", "maturity_date"]);

// Бэктест спеки: вердикт + средняя |ошибка| пересчёта прошлых купонов.
// OK — лаг/окно/режим согласованы с фактическими выплатами эмитента.
function SpecBacktest({ r }) {
  const v = r.spec_verdict;
  if (!v) return <span className="cat-miss">—</span>;
  if (v === "NO_DATA") {
    return <span className="muted" title="Нет прошлых плавающих купонов для проверки">нет данных</span>;
  }
  const cls = v === "OK" ? "bt-ok" : v === "WARN" ? "bt-warn" : "bt-bad";
  return (
    <span className={"bt-badge " + cls}
      title={`Средняя |ошибка| пересчёта ${r.spec_n_coupons || 0} прошлых купонов нашей спекой`}>
      {v} {r.spec_err_pp != null ? `${r.spec_err_pp} пп` : ""}
    </span>
  );
}

function Cell({ col, val, row }) {
  if (col === "spec_backtest") return <SpecBacktest r={row} />;
  if (val === null || val === undefined || val === "") {
    return <span className={REQUIRED.has(col) ? "cat-miss cat-req" : "cat-miss"}>—</span>;
  }
  // длинные тексты (формула купона, тип ставки) — обрезка с полным текстом в title
  if ((col === "coupon_text" || col === "var_type") && String(val).length > 30) {
    return <span title={String(val)}>{String(val).slice(0, 30)}…</span>;
  }
  return <>{val}</>;
}

export default function Catalog({ user }) {
  const isAdmin = user?.role === "admin";
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const [missOnly, setMissOnly] = useState(false);
  const [specBadOnly, setSpecBadOnly] = useState(false);
  const [floatersOnly, setFloatersOnly] = useState(true);
  const [editIsin, setEditIsin] = useState(null);
  const [importMsg, setImportMsg] = useState(null);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef(null);
  const [sp] = useSearchParams();

  // deep-link из Паспорта: /reference?isin=… — сразу фильтр по бумаге
  // и раскрытая форма правки (не искать её руками в 600 строках)
  useEffect(() => {
    const i = (sp.get("isin") || "").trim().toUpperCase();
    if (i) {
      setQuery(i);
      setEditIsin(i);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const q = useQuery({
    queryKey: [...CATALOG_KEY, floatersOnly],
    queryFn: () => fetchCatalog({ floatersOnly }),
    enabled: isAdmin,
  });

  const rows = useMemo(() => {
    let items = q.data?.items || [];
    if (missOnly) items = items.filter((r) => !r.priceable);
    // спека расходится с фактом выплат: неверный лаг/окно/режим
    if (specBadOnly) items = items.filter((r) => r.spec_verdict === "WARN" || r.spec_verdict === "BAD");
    const s = query.trim().toLowerCase();
    if (s) items = items.filter((r) =>
      r.isin.toLowerCase().includes(s) || (r.short_name || "").toLowerCase().includes(s));
    if (specBadOnly) items = [...items].sort((a, b) => (b.spec_err_pp || 0) - (a.spec_err_pp || 0));
    return items;
  }, [q.data, missOnly, specBadOnly, query]);

  const specBadCount = useMemo(
    () => (q.data?.items || []).filter((r) => r.spec_verdict === "WARN" || r.spec_verdict === "BAD").length,
    [q.data]);

  const cnt = q.data?.count;
  const invalidate = () => qc.invalidateQueries({ queryKey: CATALOG_KEY });

  const onFile = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setImporting(true); setImportMsg(null);
    try {
      const r = await importCatalogXlsx(f);
      setImportMsg({ ok: true, ...r });
      invalidate();
    } catch (ex) {
      setImportMsg({ ok: false, error: ex.message || "Ошибка импорта" });
    } finally {
      setImporting(false);
    }
  };

  if (!isAdmin) {
    return <section className="cat-mod"><div className="admin-msg admin-err">Справочник — только для админов</div></section>;
  }

  return (
    <section className="cat-mod">
      <div className="cat-head">
        <h2 className="cat-title">
          Справочник инструментов
          {cnt && <span className="admin-badge">{cnt.priceable}/{cnt.floaters} прайсуемы</span>}
          {cnt?.incomplete > 0 && <span className="admin-badge admin-warn">{cnt.incomplete} без параметров</span>}
          {cnt?.suspect > 0 && <span className="admin-badge admin-warn">{cnt.suspect} подозрит. маржа</span>}
          {specBadCount > 0 && (
            <span className="admin-badge admin-warn"
              title="Пересчёт прошлых купонов расходится с фактом выплат — проверь лаг/окно/режим">
              {specBadCount} спека расходится
            </span>
          )}
          {q.data?.offers_no_spec?.length > 0 && (
            <span className="admin-badge admin-warn"
              title={"Будущая оферта, поведение купона не задано (var_type) — считаются к погашению:\n"
                + q.data.offers_no_spec.map((o) => `${o.short_name} · оферта ${o.offer_date}`).join("\n")}>
              {q.data.offers_no_spec.length} оферта без спеки
            </span>
          )}
        </h2>
        <div className="cat-tools">
          <span className="search-wrap">
            <input className="search" type="text" placeholder="ISIN / имя" value={query}
              autoComplete="off" spellCheck={false} onChange={(e) => setQuery(e.target.value)} />
            {query && <button className="search-clear" onClick={() => setQuery("")}>×</button>}
          </span>
          <button className={"chip-btn" + (missOnly ? " on" : "")} onClick={() => setMissOnly(!missOnly)}>
            только пропуски
          </button>
          <button className={"chip-btn" + (specBadOnly ? " on" : "")}
            onClick={() => setSpecBadOnly(!specBadOnly)}
            title="Бумаги, где пересчёт прошлых купонов нашей спекой расходится с фактом выплат — признак неверного лага/окна/режима">
            спека расходится{specBadCount ? ` (${specBadCount})` : ""}
          </button>
          <button className={"chip-btn" + (floatersOnly ? " on" : "")} onClick={() => setFloatersOnly(!floatersOnly)}>
            только флоатеры
          </button>
          <a className="chip-btn" href={catalogExportUrl({ floatersOnly })} download>⭳ Экспорт xlsx</a>
          <input ref={fileRef} type="file" accept=".xlsx" hidden onChange={onFile} />
          <button className="chip-btn" onClick={() => fileRef.current?.click()} disabled={importing}
            title="Импорт из xlsx (шаблон = файл экспорта)">
            {importing ? "Импорт…" : "⭱ Импорт xlsx"}
          </button>
        </div>
      </div>

      {importMsg && (
        <div className={"admin-msg " + (importMsg.ok ? "admin-ok" : "admin-err")}>
          {importMsg.ok
            ? `Импорт: обновлено ${importMsg.updated}, пропущено ${importMsg.skipped}` +
              (importMsg.error_count ? `, ошибок ${importMsg.error_count}` : "")
            : importMsg.error}
          {importMsg.errors?.length > 0 && (
            <ul className="cat-errs">{importMsg.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
        </div>
      )}

      {q.isPending ? (
        <div className="admin-msg">Загрузка…</div>
      ) : q.isError ? (
        <div className="admin-msg admin-err">{q.error?.message || "Ошибка загрузки"}</div>
      ) : (
        <div className="cat-table-wrap">
          <table className="admin-table cat-table">
            <thead>
              <tr>
                <th>ISIN</th>
                {COLS.map(([k, label]) => <th key={k}>{label}</th>)}
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <RowWithEdit key={r.isin} r={r}
                  editing={editIsin === r.isin}
                  onEdit={() => setEditIsin(editIsin === r.isin ? null : r.isin)}
                  onSaved={() => { setEditIsin(null); invalidate(); }} />
              ))}
            </tbody>
          </table>
          <div className="muted cat-count">{rows.length} бумаг</div>
        </div>
      )}
    </section>
  );
}

function RowWithEdit({ r, editing, onEdit, onSaved }) {
  const qc = useQueryClient();
  const review = useMutation({
    mutationFn: () => markInstrumentReviewed(r.isin),
    onSuccess: () => qc.invalidateQueries({ queryKey: CATALOG_KEY }),
  });
  const reset = useMutation({
    mutationFn: () => resetInstrumentManual(r.isin),
    onSuccess: () => qc.invalidateQueries({ queryKey: CATALOG_KEY }),
  });
  const recheck = useMutation({
    mutationFn: () => recheckInstrumentSpec(r.isin),
    onSuccess: () => qc.invalidateQueries({ queryKey: CATALOG_KEY }),
  });
  const onReset = () => {
    if (window.confirm(
      `${r.short_name || r.isin}: сбросить ручную правку?\n` +
      "Снимет 🔒 и очистит режим/лаг/окно — спека уйдёт на авто-источники " +
      "(bondresearch → парсер → калибратор). Маржа/даты останутся и будут обновляться синком."))
      reset.mutate();
  };
  return (
    <>
      <tr className={r.priceable ? "" : "cat-row-incomplete"}>
        <td className="cat-isin">
          {r.isin}
          <a className="cat-ext" title="страница выпуска на cbonds"
            href={cbondsUrl(r.isin, r.cbonds_id)}
            target="_blank" rel="noopener noreferrer">↗</a>
          {r.manual_locked ? <span className="cat-lock" title="ручной lock — sync не затрёт">🔒</span> : null}
          {!r.reviewed ? <span className="cat-new" title="новая, не подтверждена">•</span> : null}
        </td>
        {COLS.map(([k]) => <td key={k}><Cell col={k} val={r[k]} row={r} /></td>)}
        <td className="admin-actions">
          <Link className="btn admin-btn-sm" to={`/audit/${r.isin}`}
            title="Паспорт бумаги: провенанс, бэктест спеки, фиксинг по дням">Паспорт</Link>
          <button className="btn admin-btn-sm" onClick={() => recheck.mutate()}
            disabled={recheck.isPending}
            title="Пересчитать бэктест спеки по факту выплат (после правки лага/окна)">
            {recheck.isPending ? "…" : "Проверить"}
          </button>
          <button className="btn admin-btn-sm" onClick={onEdit}>{editing ? "×" : "Правка"}</button>
          {(r.manual_locked || r.coupon_mode || r.fixing_lag != null || r.avg_window_days != null) && (
            <button className="btn admin-btn-sm" onClick={onReset} disabled={reset.isPending}
              title="Сбросить ручную правку: снять 🔒, очистить режим/лаг/окно — спека от авто-источников">
              Сброс
            </button>
          )}
          {!r.reviewed && (
            <button className="btn admin-btn-sm" onClick={() => review.mutate()}
              disabled={review.isPending} title="пометить проверенной">Ок</button>
          )}
        </td>
      </tr>
      {editing && (
        <tr><td colSpan={COLS.length + 2}><InstrumentForm isin={r.isin} onSaved={onSaved} /></td></tr>
      )}
    </>
  );
}
