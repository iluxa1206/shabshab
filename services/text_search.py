"""Разбор ПОИСКОВОГО ЗАПРОСА: чужая раскладка и латиница вместо кириллицы.

Имена выпусков набирают на бегу, между двумя окнами терминала, и запрос
регулярно приезжает не в той раскладке: «Ufpgy» вместо «Газпн», «Hf,jnf»
вместо «Работа». Символ в символ это ровно то, что человек хотел набрать, —
поиск обязан такое понимать, а не отвечать «ничего не найдено» на верно
набранное слово.

Второй случай — латиница ПО НАЧЕРТАНИЮ внутри кириллического имени: «Gazpn»,
«PЖД» (латинская P), «BO» вместо «БО». В тикерах биржи и в чужих выгрузках
буквы перемешаны, и глазом разницы нет вовсе.

Правило разбора одно: ВАРИАНТЫ ПРОБУЮТСЯ ПО ОЧЕРЕДИ, побеждает первый, давший
хоть что-то (см. first_hit). Объединять их выдачи нельзя: «ср» в чужой
раскладке это «cg», и нормальный запрос разбавлялся бы случайными
совпадениями по второму варианту — поиск переставал бы быть предсказуемым.

Порт этой же логики на фронте — frontend-react/src/search.js (таблицы обязаны
совпадать: таблица монитора и пикер бумаг не должны расходиться в том, что
считается совпадением)."""
from typing import Callable, Iterable, List, Optional, TypeVar

# ЙЦУКЕН → QWERTY по КЛАВИШАМ: что напечатается, если не переключить язык.
# Раскладка русская стандартная, латиница US — та пара, что стоит у всех.
_RU_BY_KEY = "йцукенгшщзхъфывапролджэячсмитьбю."
_EN_BY_KEY = "qwertyuiop[]asdfghjkl;'zxcvbnm,./"

_TO_RU = {en: ru for en, ru in zip(_EN_BY_KEY, _RU_BY_KEY)}
_TO_EN = {ru: en for en, ru in zip(_EN_BY_KEY, _RU_BY_KEY)}
_TO_RU["`"] = "ё"
_TO_EN["ё"] = "`"

# Латиница, неотличимая от кириллицы НАЧЕРТАНИЕМ. Только эти буквы: «g» на «г»
# менять нельзя — это уже транслитерация, а она даёт ложные совпадения
# («gaz» → «газ» превратило бы любой английский текст в русский).
_HOMOGLYPH = {"a": "а", "b": "в", "e": "е", "k": "к", "m": "м", "h": "н",
              "o": "о", "p": "р", "c": "с", "t": "т", "y": "у", "x": "х"}


def swap_layout(s: str) -> str:
    """Строка так, как если бы её набрали в другой раскладке.

    Обе стороны сразу и посимвольно: запрос бывает смешанным («Ufpgy3P13R» —
    имя чужой раскладкой плюс латинский хвост тикера), и одна общая карта
    разбирает его без выбора направления. Незнакомый символ (цифра, дефис)
    остаётся собой."""
    out = []
    for ch in (s or "").lower():
        out.append(_TO_RU.get(ch) or _TO_EN.get(ch) or ch)
    return "".join(out)


def fold_homoglyphs(s: str) -> str:
    """Латинские двойники кириллицы — в кириллицу («PЖД» → «ржд»)."""
    return "".join(_HOMOGLYPH.get(ch, ch) for ch in (s or "").lower())


# Клавиши пунктуации ЙЦУКЕН: «ж» превращается в «;», «х» в «[». В именах
# выпусков таких знаков не бывает, поэтому вариант с ними — заведомо мусор от
# кириллического запроса («РЖД» → «h;l»), и гонять его по базе незачем.
_KEY_PUNCT = set(";'[]`,./")


def _plausible(cand: str, raw: str) -> bool:
    """Стоит ли вообще искать по этой догадке. Отсекаем ровно то, чего в
    исходном запросе не было: пунктуацию, вылезшую из карты клавиш."""
    return not (set(cand) & _KEY_PUNCT - set(raw))


def query_variants(q: str) -> List[str]:
    """Как ещё мог выглядеть этот запрос — в порядке убывания доверия.

    Первым всегда идёт то, что человек набрал: чаще всего он набрал верно, и
    никакая догадка не должна опережать буквальный запрос. Дальше — чужая
    раскладка, затем латинские двойники. Дубликаты выкидываем: на чистой
    кириллице все три варианта совпадают, и лишний проход по базе не нужен."""
    raw = (q or "").strip()
    if not raw:
        return []
    out = [raw]
    for cand in (swap_layout(raw), fold_homoglyphs(raw),
                 fold_homoglyphs(swap_layout(raw))):
        if (cand and cand.lower() not in {v.lower() for v in out}
                and _plausible(cand, raw)):
            out.append(cand)
    return out


T = TypeVar("T")


def ranked(term: str, items: Iterable[T],
           hay: Callable[[T], tuple], limit: int) -> List[T]:
    """Выдача по одному варианту запроса, отсортированная по близости.

    hay(item) → (имя выпуска, имя эмитента, ISIN). Четыре корзины по убыванию
    доверия:
      1. точное попадание в ИМЯ ВЫПУСКА — то, что человек чаще всего и ищет;
      2. точное попадание с учётом эмитента («газпром» → все его выпуски);
      3. приблизительное (допуск опечатки) по имени выпуска;
      4. приблизительное с учётом эмитента.

    Ранжирование обязательно, иначе допуск опечатки вредит: «газпн» ловит и
    «Газпром нефть» (через «газп»), и в короткой выдаче пикера сам Газпн3P13R
    оказывался вытеснен соседями по алфавиту. По той же причине имя выпуска
    отделено от эмитента: набирают обычно выпуск, а эмитент — способ достать
    всю группу.

    Один проход по данным на все правила: список бумаг перебирается целиком, и
    ходить по нему четырежды незачем."""
    exact_m = make_matcher(term, typo=False)
    loose_m = make_matcher(term)
    tiers: List[List[T]] = [[], [], [], []]
    for it in items:
        name, emitter, isin = hay(it)
        both = f"{name or ''} {emitter or ''}"
        if exact_m(name, isin):
            tiers[0].append(it)
            if len(tiers[0]) >= limit:
                return tiers[0][:limit]
        elif exact_m(both, isin):
            tiers[1].append(it)
        elif loose_m(name, isin):
            tiers[2].append(it)
        elif loose_m(both, isin):
            tiers[3].append(it)
    out: List[T] = []
    for tier in tiers:
        out += tier[:max(0, limit - len(out))]
        if len(out) >= limit:
            break
    return out


def first_hit(q: str, run: Callable[[str], Iterable[T]]) -> List[T]:
    """Прогоняет варианты запроса по очереди и отдаёт ПЕРВУЮ непустую выдачу.

    Не объединение: см. модульную шапку — иначе обычный запрос разбавлялся бы
    случайными попаданиями догадок."""
    for v in query_variants(q):
        got = list(run(v) or [])
        if got:
            return got
    return []


# ─────────────────────────── токены и опечатки ────────────────────────────
#
# Порт frontend-react/src/search.js: правила отбора у таблицы монитора и у
# серверного пикера обязаны совпадать, иначе одно и то же имя, набранное
# одинаково, в двух полях интерфейса даёт разный ответ.
#
#  • запрос режется на токены — по разделителям И по границе буквы/цифры
#    («ржд3» = «ржд» + «3»);
#  • каждый токен обязан найтись (И), иначе «ржд 3» вернул бы весь рынок;
#  • буквенные токены ищутся в имени/эмитенте, длинные (≥3) — ещё и в ISIN;
#    короткие цифровые — ТОЛЬКО в имени, иначе «3» совпадает с цифрами любого
#    ISIN и фильтр перестаёт фильтровать;
#  • токен от 4 символов допускает ОДНУ лишнюю букву: «газпм» ищется и как
#    «газп» — так ловится и промах по соседней клавише, и лишний символ.

_TYPO_MIN_LEN = 4


def tokenize(s: str) -> List[str]:
    """«ржд-2р3» → ['ржд', '2', 'р', '3']. Регистр и латинские двойники
    сведены (fold_homoglyphs), разделители выброшены."""
    toks: List[str] = []
    cur = ""
    cur_digit: Optional[bool] = None
    for ch in fold_homoglyphs(s):
        is_al = ch.isalpha()
        is_num = ch.isdigit()
        if not is_al and not is_num:
            if cur:
                toks.append(cur)
            cur, cur_digit = "", None
            continue
        if cur_digit is not None and is_num != cur_digit and cur:
            toks.append(cur)
            cur = ""
        cur_digit = is_num
        cur += ch
    if cur:
        toks.append(cur)
    return toks


def loose_includes(hay: str, tok: str) -> bool:
    """Подстрока с допуском ОДНОЙ лишней буквы в токене.

    Короткие токены проверяются точно: на «ржд» допуск оставил бы «рж», и
    выдача перестала бы иметь отношение к запросу."""
    if tok in hay:
        return True
    if len(tok) < _TYPO_MIN_LEN:
        return False
    return any(tok[:i] + tok[i + 1:] in hay for i in range(len(tok)))


def make_matcher(term: str,
                 typo: bool = True) -> Callable[[Optional[str], Optional[str]], bool]:
    """Готовый предикат (имя, ISIN) → подходит ли. Токены считаются один раз на
    запрос, а не на каждую бумагу.

    typo=False — сравнение БЕЗ допуска опечатки. Нужно для ранжирования: точные
    попадания обязаны стоять выше приблизительных, иначе допуск вредит («газпн»
    с допуском ловит и «Газпром нефть» через «газп», и в короткой выдаче пикера
    сам Газпн3P13R оказывался вытеснен)."""
    tokens = tokenize(term)
    flat = "".join(tokens)
    includes = loose_includes if typo else (lambda hay, tok: tok in hay)

    def ok(name: Optional[str], isin: Optional[str] = "") -> bool:
        if not tokens:
            return True
        hay_name = fold_homoglyphs(name or "")
        hay_isin = fold_homoglyphs(isin or "")
        # ISIN целиком или его кусок: токенайзер режет «RU000A105GG3» на буквы
        # и цифры, поэтому по токенам он бы не собрался — сверяем склейку
        if len(flat) >= 3 and flat in hay_isin:
            return True
        for t in tokens:
            in_name = includes(hay_name, t)
            digit_only = t.isdigit()
            long_enough = len(t) >= (4 if digit_only else 3)
            in_isin = long_enough and t in hay_isin
            if not (in_name or in_isin):
                return False
        return True

    return ok
