/**
 * Слияние котировок рынка (такт 5 с) в строку таблицы монитора.
 *
 * Две разные новости в одном ответе, и обращаться с ними надо по-разному:
 *
 *  - ЦЕНЫ (last/bid/ask/средневзвес/оборот). У бумаги на push-потоке Alor они
 *    свежее в потоке, чем в биржевом снапшоте: снапшот откатил бы строку назад.
 *    Такие бумаги («streamed») цены из котировок не берут.
 *
 *  - РАСЧЁТНЫЕ МЕТРИКИ (spread по цене сделки и по средневзвесу). Их считает
 *    бэкенд, и котировки — единственный путь, которым они доезжают до бумаги
 *    ВНЕ избранного.
 *
 * БАГ, ради которого это разделено (прод, 26.08.2026): «streamed» отсекал строку
 * целиком, вместе с метриками. Push'и идут по всему рынку (wildcard-подписка), а
 * событийный пересчёт бэка просыпается ТОЛЬКО на смене цены сделки — движение
 * bid/ask его не будит (это осознанно: они тикают на порядок чаще). У ликвидной
 * бумаги, которую весь день двигают в стакане, но по которой не проходит сделок,
 * пуш прилетал чаще, чем раз в 15 с (порог свежести), поэтому котировки
 * пропускались НАВСЕГДА, а метрик-патча не было вовсе. spread в мониторе
 * оставался тем, каким приехал при загрузке страницы, и «оживал» только от F5.
 * Десятиминутный поллер юниверса всё это время исправно клал в кэш свежее число.
 */

/** Поля метрик, которые приезжают котировками: ключ ответа → поле строки.
 *
 * Цена набора на объём (vol_bid_px/vol_bid_y) — тоже МЕТРИКА, а не цена: её
 * считает бэкенд по методике, из книги её не вывести, и у бумаги на стриме она
 * приезжает этим же путём. Размер тикета в ключе не участвует — он задан в
 * самом запросе котировок (см. fetchQuotes). */
export const QUOTE_METRIC_FIELDS = {
  yoi: "yield_over_index_bps",
  vol_bid_px: "vol_bid_price_pct",
  vol_ask_px: "vol_ask_price_pct",
  vol_bid_y: "y_idx_vol_bid_bps",
  vol_ask_y: "y_idx_vol_ask_bps",
};

/** Поля цен: у бумаги на стриме они свои, из котировок не берутся. */
const QUOTE_PRICE_FIELDS = {
  last: "last_price_pct",
  bid: "bid_price_pct",
  ask: "ask_price_pct",
  wap: "wap_price_pct",
  vol: "val_today",
};

/** Отличается ли хоть одно поле котировки от того, что уже в строке.
 *
 * ПО НАЛИЧИЮ КЛЮЧА: движок кладёт явный null, когда считать стало нечем, и
 * ручка везёт его как есть (api/routes/bonds.py). Сверка по значению читала
 * стирание как «изменений нет», и в ячейке оставался спред от прошлой цены. */
export function quoteChanges(row, q, keys) {
  for (const [k, field] of Object.entries(keys)) {
    if (k in q && q[k] !== row[field]) return true;
  }
  return false;
}

/**
 * Патч метрик для бумаги НА СТРИМЕ: только расчётные поля, цены не трогаем.
 * Возвращает ту же ссылку, если менять нечего (без лишнего ререндера таблицы).
 */
export function mergeStreamedQuote(row, q) {
  const sides = sideMetricPatch(row, q);
  const wap = wapMetricPatch(row, q);
  if (!q || (!quoteChanges(row, q, QUOTE_METRIC_FIELDS) && !sides && !wap)) return row;
  const n = { ...row };
  for (const [k, field] of Object.entries(QUOTE_METRIC_FIELDS)) {
    if (k in q) n[field] = q[k];
  }
  // ЧИСЛО ПРИЕХАЛО — МЕТКА СНИМАЕТСЯ ЗДЕСЬ ЖЕ. _yoi_stale ставит и снимает
  // поллер (App.jsx), а на потоковом пути её только ставили: у бумаги на
  // стриме свежий Y-IDX оставался приглушённым с подписью «спред к прежней
  // цене» до следующей смены цены сделки.
  //
  // НО ТОЛЬКО ПРИ СОВПАДЕНИИ ЦЕНЫ РАСЧЁТА. У бумаги на стриме цена в строке
  // своя (из push'а), а yoi считался к цене, известной движку: раньше этот
  // путь ставил число вообще без сверки и снимал приглушение, перетирая
  // результат более строгой WS-сверки — поллер ходит раз в секунду и всегда
  // оказывается последним.
  if ("yoi_px" in q && q.yoi != null && !eqPx3(q.yoi_px, row.last_price_pct)) {
    n.yield_over_index_bps = row.yield_over_index_bps;
    n._yoi_stale = true;
  } else if (q.yoi != null) {
    n._yoi_stale = false;
  }
  // у бумаги на стриме цены свои, из push'а — сверка со ценой движка тем более
  // обязательна, снапшот тут отстаёт заведомо
  if (sides) Object.assign(n, sides);
  if (wap) Object.assign(n, wap);
  return n;
}

export { QUOTE_PRICE_FIELDS };

/** Спреды сторон из котировок: ставим ТОЛЬКО при совпадении цены.
 *
 * Стороны приезжают вторым путём (кроме WS-патча движка) с 02.09.2026: пока
 * патч и снимок движка согласны, разницы нет, но если патч разошёлся с
 * реальностью, на сервере число правильное, а в строке прочерк — и лечило его
 * лишь следующее движение книги (у неликвида его может не быть часами).
 *
 * Слепо присваивать нельзя: цена строки идёт из снапшота (или из стрима), спред
 * — из движка, и на такт они расходятся. Число, посчитанное по прошлой цене,
 * рядом с новой ценой выглядит согласованным и врёт — ровно так ошибалась
 * лестница стакана 27.08.2026. Поэтому бэкенд отдаёт цену, по которой считал
 * (yoi_bid_px/yoi_ask_px), и мы сверяем её с ценой, которая окажется в строке.
 *
 * row — строка ДО патча, patch — уже накопленные изменения (там может лежать
 * новая цена стороны из этого же ответа).
 */
export function sideMetricPatch(row, q, patch = null) {
  if (!q) return null;
  let out = null;
  for (const [side, pxField, yField] of [
    ["bid", "bid_price_pct", "y_idx_bid_bps"],
    ["ask", "ask_price_pct", "y_idx_ask_bps"],
  ]) {
    const v = q[`yoi_${side}`];
    const at = q[`yoi_${side}_px`];
    if (v == null || at == null) continue;
    const px = patch && pxField in patch ? patch[pxField] : row[pxField];
    // ОКРУГЛЕНИЕ, А НЕ 1e-9: канонический ключ цены в проекте — 3 знака
    // (universe_stream._px_key). Сверка «до последнего бита» отвергала готовый
    // спред из-за незначащих знаков (замер 02.09: 2 строки из 588 держали
    // приглушённое число при посчитанном сервере).
    if (px == null || Math.round(px * 1000) !== Math.round(at * 1000)) continue;
    if (row[yField] === v && !(patch && yField in patch)) continue;
    (out ||= {})[yField] = v;
  }
  return out;
}

/** Спред по СРЕДНЕВЗВЕСУ — с той же сверкой цены, что у сторон.
 *
 * Средневзвес в строке и средневзвес, к которому движок посчитал спред, берутся
 * из разных мест (свой счёт по тикам против биржевого WAPRICE) и расходятся на
 * такт. Теперь движок кладёт цену расчёта в строку и отдаёт её рядом с числом
 * (yoi_wap_px) — сверяем.
 *
 * ЦЕНЫ РАСЧЁТА НЕТ — ставим как раньше: у бумаг вне движка (их в ответе вчетверо
 * больше, чем он считает) сверять нечего, а прочерк вместо числа хуже.
 */
export function wapMetricPatch(row, q, patch = null) {
  if (!q || !("yoi_wap" in q)) return null;
  const v = q.yoi_wap;
  const at = q.yoi_wap_px;
  const px = patch && "wap_price_pct" in patch ? patch.wap_price_pct : row.wap_price_pct;
  if (v != null && at != null
      && (px == null || Math.round(px * 1000) !== Math.round(at * 1000))) return null;
  if (row.y_idx_wap_bps === v && !(patch && "y_idx_wap_bps" in patch)) return null;
  return { y_idx_wap_bps: v };
}

/** Есть ли в котировке спред стороны, которого нет в строке (для «строка не
 * изменилась» — без этого патч сторон не доехал бы вовсе). */
export function sideMetricChanges(row, q, patch = null) {
  return sideMetricPatch(row, q, patch) !== null;
}

// Новая цена стороны в строку. Спред этой цены НЕ достраиваем: раньше он
// двигался наклоном dY/dP от якоря, и пара «цена → спред» выглядела
// согласованной, но обе цифры уезжали вместе с якорем (27.08.2026 — вся
// лестница стакана в телеграме).
// Теперь спред стороны считает бэкенд по методике и присылает патчем: движок
// будит отдельная очередь на движение bid/ask, такт ≤5 с. До прихода числа
// ячейка спреда пуста — прочерк честнее правдоподобной прикидки.
//
// px === null значит «стороны в книге НЕТ» (котировка — полный снимок верха
// стакана): гасим и цену, и спред. Пропускать такой случай нельзя — в строке
// осталась бы цена заявки, которой на рынке уже нет.
// Новая цена СРЕДНЕВЗВЕСА в строку — по тем же правилам, что у сторон.
// Спред к прежнему средневзвесу уводим в y_idx_wap_stale (таблица покажет его
// приглушённым с подписью «спред к прежней цене»), расчётное поле гасим: без
// этого сверка wapMetricPatch отклоняла новый спред, а СТАРЫЙ оставался в
// ячейке рядом со свежей ценой и на полной яркости — то есть выдавал себя за
// посчитанный к ней.
export function applyWapQuote(b, n, px) {
  if (px == null || px === b.wap_price_pct) return;
  n.wap_price_pct = px;
  if (b.y_idx_wap_bps != null) n.y_idx_wap_stale = b.y_idx_wap_bps;
  n.y_idx_wap_bps = null;
}

export function applySideQuote(b, n, side, px, hasKey) {
  const pxField = side === "bid" ? "bid_price_pct" : "ask_price_pct";
  if (px === undefined || (!hasKey && px == null) || px === b[pxField]) return;
  const yField = side === "bid" ? "y_idx_bid_bps" : "y_idx_ask_bps";
  const sField = side === "bid" ? "y_idx_bid_stale" : "y_idx_ask_stale";
  n[pxField] = px ?? null;
  // ПОСЛЕДНЕЕ ИЗВЕСТНОЕ ЧИСЛО НЕ ВЫБРАСЫВАЕМ. Спред к новой цене не относится
  // и ячейка обязана показать, что он пересчитывается, — но пустая клетка
  // говорит «спреда нет», хотя порядок величины известен и не изменится на
  // порядок. Держим его отдельным полем: таблица рисует его приглушённым,
  // сортировка и фильтр держат по нему место строки, а расчётным его никто не
  // считает — y_idx_*_bps по-прежнему пуст, пока движок не пришлёт новое.
  if (px == null) {
    n[sField] = null;              // стороны в книге нет — показывать нечего
  } else if (b[yField] != null) {
    n[sField] = b[yField];
  }
  n[yField] = null;
}

/** Поля патча движка, посчитанные ПО ЦЕНЕ СДЕЛКИ. */
export const STREAM_LEVEL_KEYS = [
  "yield_over_index_bps", "dm_bps", "disc_margin_bps", "z_model_bps",
  "yield_xirr_pct", "index_yield_pct", "dirty_price_rub",
  "delta_to_prev_close", "preferred_horizon", "spread_dur_yrs",
];

/** [поле метрики, поле цены расчёта в патче, поле цены в строке]. */
export const STREAM_PRICED_KEYS = [
  ["y_idx_bid_bps", "yoi_bid_px", "bid_price_pct"],
  ["y_idx_ask_bps", "yoi_ask_px", "ask_price_pct"],
  ["y_idx_wap_bps", "yoi_wap_px", "wap_price_pct"],
];

// ОКРУГЛЕНИЕ ДО 3 ЗНАКОВ — канонический ключ цены в проекте
// (universe_stream._px_key), тот же, что в sideMetricPatch.
const eqPx3 = (a, c) =>
  a != null && c != null && Math.round(a * 1000) === Math.round(c * 1000);

/**
 * Метрики WS-патча движка — со сверкой цены, к которой они посчитаны.
 *
 * БАГ, ради которого это заведено (аудит 03.09, находка 2): пуши одной бумаги
 * копятся в 400-мс буфере и склеиваются плоским спредом. Патч движка везёт пару
 * «цена → её спред» из одного расчёта, но пришедшая ПОСЛЕ него котировка
 * перетирала в буфере цену, оставив спред от прежней, и флаг metrics
 * наследовался от прежнего патча. В флаше applySideQuote честно гасил спред под
 * новую цену, а следующая строка возвращала старое число обратно — и строка
 * даже не приглушалась. Ровно рассинхрон 27.08.2026 на главном (WS) пути.
 *
 * Теперь движок везёт цены расчёта отдельными полями (yoi_px/yoi_bid_px/
 * yoi_ask_px/yoi_wap_px, см. universe_stream._METRIC_PX_FIELDS), которых нет в
 * котировке — склейка их не портит.
 *
 * prices — цены, которые ОКАЖУТСЯ в строке после этого флаша.
 * Возвращает {fields, stale}: stale=true — строку надо приглушить, число
 * посчитано к другой цене и на экран не идёт.
 *
 * ЯВНЫЙ null ПРИМЕНЯЕТСЯ ВСЕГДА: «считать стало нечем» верно при любой цене, а
 * задержать стирание значит оставить в строке спред ушедшей заявки.
 */
export function streamMetricPatch(q, prices) {
  if (!q || !q.metrics) return null;
  const fields = {};
  let stale = false;
  // цены расчёта нет вовсе (старый бэк) — ведём себя как раньше, без сверки
  const at = "yoi_px" in q ? q.yoi_px : undefined;
  const lastOk = at === undefined || eqPx3(at, prices.last);
  if (lastOk && q.yield_over_index_bps != null) fields._yoi_stale = false;
  for (const k of STREAM_LEVEL_KEYS) {
    if (!(k in q)) continue;
    if (q[k] == null || lastOk) fields[k] = q[k];
    else stale = true;
  }
  for (const [yField, atField, pxField] of STREAM_PRICED_KEYS) {
    if (!(yField in q)) continue;
    const px = atField in q ? q[atField] : undefined;
    // сторону, не прошедшую сверку, гасить не нужно: applySideQuote уже увёл
    // прежнее число в y_idx_*_stale под новую цену
    if (q[yField] == null || px === undefined || eqPx3(px, prices[pxField])) {
      fields[yField] = q[yField];
    }
  }
  return { fields, stale };
}
