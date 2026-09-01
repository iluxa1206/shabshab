// Умный поиск по рынку: запрос вида «РЖД 3» должен находить все похожие выпуски
// эмитента (РЖД 2Р3, 3Р2, 1Р-03R), а не только точное совпадение подстроки.
//
// Правила:
//  • запрос режется на токены — по разделителям И по границе буквы/цифры
//    («ржд3» = «ржд» + «3»);
//  • каждый токен обязан найтись (AND) — иначе «ржд 3» вернул бы весь рынок;
//  • буквенные токены ищутся в имени/эмитенте/формуле, длинные (≥3) — ещё и в
//    ISIN; цифровые короткие токены — ТОЛЬКО в имени, иначе «3» совпадает с
//    цифрами любого ISIN и фильтр перестаёт фильтровать;
//  • латиница нормализуется в кириллицу по начертанию: в тикерах и в наборе с
//    чужой раскладки «P» и «Р», «BO» и «БО» перемешаны.
// Опечатки: токен от 4 символов допускает одну лишнюю/пропущенную букву.
// Раскладка: не нашлось по набранному — пробуем ту же строку в другой
// раскладке («Ufpgy» это «Газпн»), см. filterBonds. Порт бэка —
// services/text_search.py; таблицы обязаны совпадать, иначе таблица монитора
// и пикер бумаг понимали бы один и тот же ввод по-разному.

const HOMOGLYPH = {
  a: "а", b: "в", e: "е", k: "к", m: "м", h: "н",
  o: "о", p: "р", c: "с", t: "т", y: "у", x: "х",
};

// ЙЦУКЕН → QWERTY ПО КЛАВИШАМ: что напечатается, если не переключить язык.
// Раскладка русская стандартная, латиница US.
const RU_BY_KEY = "йцукенгшщзхъфывапролджэячсмитьбю.";
const EN_BY_KEY = "qwertyuiop[]asdfghjkl;'zxcvbnm,./";
const TO_RU = {}, TO_EN = {};
for (let i = 0; i < EN_BY_KEY.length; i++) {
  TO_RU[EN_BY_KEY[i]] = RU_BY_KEY[i];
  TO_EN[RU_BY_KEY[i]] = EN_BY_KEY[i];
}
TO_RU["`"] = "ё";
TO_EN["ё"] = "`";

// Клавиши пунктуации ЙЦУКЕН: «ж» превращается в «;», «х» в «[». В именах
// выпусков таких знаков нет, поэтому вариант с ними — заведомо мусор от
// кириллического запроса («РЖД» → «h;l»), и гонять его по рынку незачем.
const KEY_PUNCT = new Set(";'[]`,./");

/** Строка так, как если бы её набрали в другой раскладке. Обе стороны сразу и
 *  посимвольно: запрос бывает смешанным, и одна общая карта разбирает его без
 *  выбора направления. */
export function swapLayout(s) {
  let out = "";
  for (const ch of String(s || "").toLowerCase()) {
    out += TO_RU[ch] || TO_EN[ch] || ch;
  }
  return out;
}

/** Как ещё мог выглядеть запрос — набранное первым, догадка следом: человек
 *  чаще всего набрал верно, и никакая догадка не должна его опережать. */
export function queryVariants(q) {
  const raw = String(q || "").trim();
  if (!raw) return [];
  const out = [raw];
  const sw = swapLayout(raw);
  const junk = [...sw].some((ch) => KEY_PUNCT.has(ch) && !raw.includes(ch));
  if (sw && !junk && sw.toLowerCase() !== raw.toLowerCase()) out.push(sw);
  return out;
}

export function normalize(s) {
  let out = "";
  for (const ch of String(s || "").toLowerCase()) {
    out += HOMOGLYPH[ch] || ch;
  }
  return out;
}

// «ржд-2р3» → ["ржд", "2", "р", "3"]
export function tokenize(s) {
  const n = normalize(s);
  const toks = [];
  let cur = "", curDigit = null;
  const push = () => { if (cur) toks.push(cur); cur = ""; };
  for (const ch of n) {
    const isAl = /[a-zа-яё]/.test(ch);
    const isNum = /[0-9]/.test(ch);
    if (!isAl && !isNum) { push(); curDigit = null; continue; }
    if (curDigit !== null && isNum !== curDigit) push();
    curDigit = isNum;
    cur += ch;
  }
  push();
  return toks;
}

// подстрока с допуском в одну лишнюю букву в токене (опечатка набора)
function looseIncludes(hay, tok) {
  if (hay.includes(tok)) return true;
  if (tok.length < 4) return false;
  for (let i = 0; i < tok.length; i++) {
    if (hay.includes(tok.slice(0, i) + tok.slice(i + 1))) return true;
  }
  return false;
}

// haystack бумаги: имя/эмитент/формула отдельно от ISIN — у них разные правила
export function bondHaystack(b) {
  return {
    name: normalize([b.short_name, b.emitter_name, b.formula].filter(Boolean).join(" ")),
    isin: normalize(b.isin || ""),
  };
}

export function matchTokens(hay, tokens, flat) {
  // ISIN целиком или его кусок: токенайзер режет «RU000A105GG3» на буквы и
  // цифры, поэтому по токенам он бы не собрался — сверяем склейку запроса
  if (flat && flat.length >= 3 && hay.isin.includes(flat)) return true;
  for (const t of tokens) {
    const digitOnly = /^[0-9]+$/.test(t);
    const inName = looseIncludes(hay.name, t);
    // цифровой токен по ISIN ищем только длинным куском («000а105»), иначе
    // одиночная цифра совпадёт с любым ISIN
    const inIsin = (!digitOnly && t.length >= 3) || t.length >= 4
      ? hay.isin.includes(t) : false;
    if (!inName && !inIsin) return false;
  }
  return true;
}

// готовый предикат по строке запроса (пустой запрос → null: фильтр не нужен)
export function makeBondFilter(query) {
  const tokens = tokenize(query);
  if (!tokens.length) return null;
  const flat = tokens.join("");
  return (b) => matchTokens(bondHaystack(b), tokens, flat);
}

/**
 * Отбор ЛЮБОГО списка по одному текстовому полю — теми же правилами, что и
 * рынок: токены, допуск опечатки, гомоглифы, запасная раскладка.
 *
 * Для полей без ISIN (эмитент в меню фильтров). getText(item) → строка, по
 * которой ищем. Правила одни на все поля интерфейса намеренно: набрав имя
 * одинаково, человек не должен получать разный ответ в таблице и в фильтре.
 */
export function filterByText(items, query, getText = (x) => x) {
  return filterBonds(items, query,
                     (it) => ({ name: normalize(getText(it) || ""), isin: "" }));
}

/**
 * Отбор бумаг по строке поиска с ЗАПАСНОЙ РАСКЛАДКОЙ.
 *
 * Варианты пробуются по очереди, побеждает ПЕРВЫЙ давший хоть что-то.
 * Объединять выдачи нельзя: «ср» в чужой раскладке это «cg», и обычный запрос
 * разбавлялся бы случайными совпадениями догадки — поиск переставал бы быть
 * предсказуемым. Ничего не нашлось ни по одному варианту — честно пусто.
 */
export function filterBonds(rows, query, hay = bondHaystack) {
  const variants = queryVariants(query);
  if (!variants.length) return rows;
  for (const v of variants) {
    const tokens = tokenize(v);
    if (!tokens.length) return rows;  // запрос из одних разделителей
    const flat = tokens.join("");
    const hit = rows.filter((b) => matchTokens(hay(b), tokens, flat));
    if (hit.length) return hit;
  }
  return [];
}
