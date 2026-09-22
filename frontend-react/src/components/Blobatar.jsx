// Аватар-«блоб» на месте надписи DESK в левом верхнем углу: следит глазами за
// курсором, по клику корчит рожу. Пакет blobatar (MIT, Alain00/blobatar) — SVG
// считается в процессе, в сеть браузер не ходит.
//
// Картинка детерминирована по name — одно семя всегда даёт одну и ту же морду,
// поэтому шапка не меняется от сессии к сессии.
import { useCallback, useEffect, useRef, useState } from "react";
import { Blobatar } from "@blobatar/react";
import { useGaze } from "@blobatar/react/gaze";
// Выражения — ИМПОРТИРУЕМЫЕ ЗНАЧЕНИЯ, не строки: пакет так тришейкает позы,
// строкой компонента падает. Берём только те, что реально показываем.
import { idle, happy, surprised, wink, smug, love, thinking }
  from "blobatar/expression";
import "blobatar/motion.css";
import "blobatar/gaze.css";

// «Нечего возвращать в idle» — у поз нет таймеров и автосброса (см. README:
// «A state, not an event»), поэтому вспышку гасим сами.
const MOODS = [happy, surprised, wink, smug, love, thinking];
const MOOD_MS = 1400;

export default function BrandBlobatar({ size = 50, name = "desk2" }) {
  // travel — насколько далеко ходят зрачки (CSS-пиксели): на мелком лице
  // больше похоже на дёрганье, чем на взгляд.
  const { ref: gazeRef, remeasure } = useGaze({ travel: 9, lookAt: "pointer" });
  const box = useRef(null);
  const [svg, setSvg] = useState(null);
  const [mood, setMood] = useState(idle);
  const moodTimer = useRef(null);

  // ПОЧЕМУ НЕ ref={gazeRef} НА <Blobatar>, КАК В ДОКАХ: Blobatar — обычная
  // функциональная компонента без forwardRef, а у нас React 18, где ref ещё не
  // обычный проп (он станет им в 19). Ref молча терялся, gaze не стартовал —
  // глаза не двигались. Достаём <svg> из DOM сами.
  useEffect(() => {
    setSvg(box.current?.querySelector("svg") || null);
  }, [size, name]);
  useEffect(() => { if (svg) gazeRef(svg); }, [svg, gazeRef]);

  // Позицию лица gaze меряет один раз: шапка съезжает при скролле страницы и
  // смене раскладки — без перемера взгляд уходит мимо курсора.
  useEffect(() => {
    const on = () => remeasure();
    window.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    return () => {
      window.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
    };
  }, [remeasure]);

  useEffect(() => () => clearTimeout(moodTimer.current), []);

  // Клик — случайная рожа на MOOD_MS. Поза морфится, а не подменяет разметку:
  // gaze от этого не отваливается, тот же <svg> остаётся на месте.
  const poke = useCallback(() => {
    clearTimeout(moodTimer.current);
    setMood(MOODS[Math.floor(Math.random() * MOODS.length)]);
    moodTimer.current = setTimeout(() => setMood(idle), MOOD_MS);
  }, []);

  return (
    <span className="brand-blob" ref={box} title="DESK" onClick={poke}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") poke(); }}
          role="button" tabIndex={0} aria-label="DESK">
      {/* background не задаём: подложка-кружок на тёмной теме читается как
          белая обводка вокруг морды */}
      <Blobatar name={name} size={size} animate="always" expression={mood} />
    </span>
  );
}
