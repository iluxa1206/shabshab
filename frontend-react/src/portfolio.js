// Парсинг CSV портфеля: строки вида `ISIN,количество`.
// Толерантен к разделителям (, ; таб), заголовку, десятичной запятой, пробелам-тысячам.
// Возвращает { positions: {isin: qty}, isins: [isin], errors: [str] }.

const ISIN_RE = /\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b/;

function parseQty(s) {
  if (!s) return null;
  // убрать пробелы-тысячи, запятую-десятичную → точку
  const cleaned = s.replace(/\s/g, "").replace(",", ".").replace(/[^\d.]/g, "");
  if (!cleaned) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function parsePortfolioCsv(text) {
  const positions = {};
  const isins = [];
  const errors = [];
  const lines = text.split(/\r?\n/);

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim();
    if (!raw) continue;
    const upper = raw.toUpperCase();
    const m = upper.match(ISIN_RE);
    if (!m) continue; // строка без ISIN (заголовок/мусор) — молча пропускаем
    const isin = m[1];
    // остаток строки без ISIN → ищем количество
    const rest = raw.slice(0, m.index) + " " + raw.slice(m.index + isin.length);
    const numMatch = rest.match(/[\d][\d\s.,]*/);
    const qty = numMatch ? parseQty(numMatch[0]) : null;
    if (qty == null) {
      errors.push(`${isin}: не найдено количество`);
      continue;
    }
    if (positions[isin]) positions[isin] += qty; // дубликат — суммируем
    else { positions[isin] = qty; isins.push(isin); }
  }

  return { positions, isins, errors };
}
