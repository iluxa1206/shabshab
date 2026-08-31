/**
 * «Танцующий червячок» у имени уведомления — метка лучшей заявки (best).
 *
 * Не GIF: растр пришлось бы держать двумя файлами под светлую и тёмную тему, он
 * не попадал бы в цвет стороны и весил бы килобайты. Это inline-SVG — тело в
 * currentColor (красится темой), глаза и рот свои, вес — десятки байт.
 *
 * Анимация двойная: волна бежит по телу от хвоста к голове (сегменты сдвинуты
 * по фазе), и вся тушка покачивается. При prefers-reduced-motion (styles.css)
 * обе гаснут и остаётся статичный значок: смысл метки в её наличии, а не в
 * движении.
 */
export default function BestWorm() {
  return (
    <svg className="best-worm" viewBox="0 0 36 24" width="30" height="20"
      aria-hidden="true" focusable="false">
      <g className="bw-wig">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <circle key={i} className="bw-seg" cx={4 + i * 4.6} cy="12" r="3.1"
            style={{ animationDelay: `${i * 0.1}s` }} />
        ))}
        <circle className="bw-head" cx="31" cy="12" r="4.2" />
        <circle className="bw-eyew" cx="30" cy="10.4" r="1.5" />
        <circle className="bw-eyew" cx="33.2" cy="10.8" r="1.5" />
        <circle className="bw-pup" cx="30.4" cy="10.5" r="0.75" />
        <circle className="bw-pup" cx="33.6" cy="10.9" r="0.75" />
        <path className="bw-mouth" d="M29.6 14.4 q1.8 1.6 3.6 0" fill="none"
          strokeWidth="0.9" strokeLinecap="round" />
      </g>
    </svg>
  );
}
