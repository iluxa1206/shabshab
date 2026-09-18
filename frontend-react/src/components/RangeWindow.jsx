// Окно «от — до» одним числом с каждой стороны (срок в годах, спред в bps,
// YTM в %): подпись, два поля, крестик сброса, когда хоть одно заполнено.
// Значения — строки как в инпуте ("" = не задано), разбор и правило «без
// значения при заданной границе — скрыть» остаются у хоста: у каждого окна
// своё поле строки. Раньше — шесть копий разметки в трёх файлах.
export default function RangeWindow({
  label, from, to, setFrom, setTo, title,
  ariaFrom, ariaTo, resetTitle = "Сбросить окно", step = "0.5", min,
}) {
  return (
    <div className="fgroup" title={title}>
      <span className="fg-lbl">{label}</span>
      <input className="num-input" type="number" min={min} step={step} placeholder="от"
        aria-label={ariaFrom} value={from} onChange={(e) => setFrom(e.target.value)} />
      <span className="fg-lbl">—</span>
      <input className="num-input" type="number" min={min} step={step} placeholder="до"
        aria-label={ariaTo} value={to} onChange={(e) => setTo(e.target.value)} />
      {(from || to) && (
        <button className="chip-btn" title={resetTitle}
          onClick={() => { setFrom(""); setTo(""); }}>×</button>
      )}
    </div>
  );
}
