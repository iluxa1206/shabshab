// Слияние живых данных ФИКСОВ — чистые функции, без React.
//
// Вынесено из components/fixed/FixedMonitor.jsx (14.10.2026), чтобы одни и те
// же правила обслуживали и монитор фиксов, и общий монитор облигаций: в них
// зашиты уроки 22–23.09.2026 (частичный патч не стирает строку; гасится только
// сдвинувшаяся цена; число списка не садится на чужую цену).

// Метрики фикса, которые движок пересчитывает по цене и шлёт патчем. Явный
// null в патче СТИРАЕТ число (сторона ушла из книги, контекст остыл) — поэтому
// копируем по наличию ключа, а не по «не пусто».
const PATCH_METRIC_KEYS = [
  "ytm", "ytm_bid", "ytm_ask", "ytm_wap", "cur_yield",
  "g_spread_bps", "g_spread_bid_bps", "g_spread_ask_bps", "g_spread_wap_bps",
  "z_spread_bps", "dirty", "mod_dur", "mac_dur", "convexity", "dv01",
  "delta_to_prev_close", "price_stale", "ytm_guard",
  // Y-IDX к роллированию RUONIA — имена как у флоатеров
  "yield_over_index_bps", "index_yield_pct", "y_idx_bid_bps", "y_idx_ask_bps", "y_idx_wap_bps",
  // средневзвес едет ВМЕСТЕ со своим спредом: пара «цена → спред» обязана быть
  // из одного расчёта, иначе в строке окажется спред по другой цене
  "wap_pct",
];

/** Новая цена в строку вместе с гашением того, что от неё производно: цена
 *  приезжает тиком, а метрики считает бэк своим тактом, и без гашения строка
 *  показывала бы свежую цену со спредом от прежней. full=true — цена стороны
 *  (полный снимок верха стакана: пусто значит «стороны нет»). */
export function applyPrice(row, field, px, derived, full = false) {
  // Пусто в НЕсторонней цене — это «новости нет» (снапшот не знает цены), а не
  // «цены не стало»: затирать ею цену строки значит выбросить prev-close,
  // которым живёт неликвид. У сторон стакана наоборот — снимок полный.
  const next = full ? (px ?? null) : px;
  if (next == null && !full) return;
  if (next === row[field]) return;
  row[field] = next;
  for (const k of derived) row[k] = null;
}

/** Наложение патча стрима на прежнее наложение той же бумаги. */
export function applyPatch(cur, q) {
  const n = { ...(cur || {}), _live: true };
  // СМЕНУ цены судим ПРОТИВ прежнего наложения: котировка Alor — полный снимок
  // верха стакана и приходит с каждым тиком, в том числе когда bid/ask/last не
  // сдвинулись (изменился объём стороны, повторный принт по той же цене).
  // Бэк по такой котировке ничего не пересчитывает (universe_stream._on_quote
  // ставит в очередь только СМЕНУ цены), и гашение «на всякий случай»
  // оставляло прочерк в спредах сторон и звёздочку на строке до следующей
  // настоящей сделки — то есть, у неликвида, до конца дня.
  const bidMoved = "bid" in q && cur && (q.bid ?? null) !== (cur.bid ?? null);
  const askMoved = "ask" in q && cur && (q.ask ?? null) !== (cur.ask ?? null);
  const lastMoved = q.last_price_pct != null && cur && q.last_price_pct !== cur.last_price_pct;
  if ("bid" in q) n.bid = q.bid ?? null;
  if ("ask" in q) n.ask = q.ask ?? null;
  if (q.vwap_pct != null) n.wap_pct = q.vwap_pct;
  // оборот назад не откатываем: патч может прийти от сокета, чей дневной
  // агрегат ещё догоняется архивом
  if (q.val_today != null && !(n.val_today > q.val_today)) n.val_today = q.val_today;
  // цена сделки: null в патче значит «сделок сегодня не было», а не «цены
  // больше нет» — строка остаётся на прежней (см. applyPrice)
  if (q.last_price_pct != null) n.last_price_pct = q.last_price_pct;
  if (q.metrics) {
    for (const k of PATCH_METRIC_KEYS) if (k in q) n[k] = q[k];
    n._mstale = false;
  } else {
    // патч без метрик — цены новее производных: гасим то, что от них зависит,
    // но только у СДВИНУВШЕЙСЯ цены (см. выше)
    if (bidMoved) { n.g_spread_bid_bps = null; n.ytm_bid = null; n.y_idx_bid_bps = null; }
    if (askMoved) { n.g_spread_ask_bps = null; n.ytm_ask = null; n.y_idx_ask_bps = null; }
    if (lastMoved) n._mstale = true;
  }
  return n;
}

/** Числа набора на объём из ответа котировок (плоские ключи vol_bid_px/…) — в
 *  поля строки витрины. Пусто значит «движок ещё не посчитал»: прежнее число не
 *  трогаем, его гасит перезагрузка списка при смене размера. */
export function applyVolQuote(row, it) {
  for (const side of ["bid", "ask"]) {
    const px = it[`vol_${side}_px`], g = it[`vol_${side}_g`], y = it[`vol_${side}_ytm`];
    const yi = it[`vol_${side}_y`];
    if (px != null) row[`vol_${side}_price_pct`] = px;
    if (g != null) row[`g_spread_vol_${side}_bps`] = g;
    if (y != null) row[`ytm_vol_${side}`] = y;
    if (yi != null) row[`y_idx_vol_${side}_bps`] = yi;
  }
}

// Цена наложения → её производные в строке списка. Список несёт числа к ТОЙ
// цене, по которой считал движок (fixed_metrics хранит last/bid/ask расчёта).
export const LIVE_DERIVED = [
  ["bid", ["g_spread_bid_bps", "ytm_bid", "y_idx_bid_bps"]],
  ["ask", ["g_spread_ask_bps", "ytm_ask", "y_idx_ask_bps"]],
];
export const LAST_DERIVED = ["ytm", "g_spread_bps", "z_spread_bps", "dirty", "cur_yield",
                      "mod_dur", "mac_dur", "convexity", "dv01", "yield_over_index_bps"];

/** Наложение стрима поверх строки списка. Если наложение принесло цену, но не
 *  принесло числа к ней (первый патч бумаги — котировка, а не метрики), а цена
 *  списка другая — число списка посчитано к прежней цене и в строку не идёт:
 *  то же правило, что applyPatch применяет между двумя патчами. */
export function mergeLive(row, live) {
  const out = { ...row, ...live };
  for (const [px, derived] of LIVE_DERIVED) {
    if (!(px in live)) continue;
    if ((live[px] ?? null) !== (row[px] ?? null)) {
      for (const k of derived) if (!(k in live)) out[k] = null;
    } else {
      // ТА ЖЕ ЦЕНА, ЧТО В СПИСКЕ: число списка посчитано ровно к ней. Прочерк
      // наложения (цена сдвинулась, патч движка ещё не дошёл или потерялся)
      // закрываем числом списка — он обновляется раз в минуту из fixed_metrics.
      for (const k of derived) if (out[k] == null && row[k] != null) out[k] = row[k];
    }
  }
  if (live.last_price_pct != null) {
    if (live.last_price_pct !== row.last_price_pct) {
      if (!("ytm" in live) && !("_mstale" in live)) out._mstale = true;
    } else if (live._mstale) {
      // звёздочка снята: список уже несёт числа к этой цене
      for (const k of LAST_DERIVED) if (row[k] != null) out[k] = row[k];
      out._mstale = false;
    }
  }
  return out;
}

/** Поверхностное сравнение строк: одинаковый набор ключей и значений. */
export function sameRow(a, b) {
  if (!a || !b) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  for (const k of ka) if (a[k] !== b[k]) return false;
  return true;
}
