// Инвалидация всех запросов, зависящих от состава/позиций фонда.
// Дёргается после мутаций (snapshot, position, repo, capital) — чинит stale-баг,
// когда изменение qty без изменения числа позиций не рефетчило сценарии/кэшфлоу.
export function invalidateFund(qc, code) {
  qc.invalidateQueries({ queryKey: ["fund", code] });
  qc.invalidateQueries({ queryKey: ["fundRepos", code] });
  qc.invalidateQueries({ queryKey: ["scenarios", code] });
  qc.invalidateQueries({ queryKey: ["cashflow", code] });
  qc.invalidateQueries({ queryKey: ["funds"] });
  qc.invalidateQueries({ queryKey: ["fundsCalendar"] });
}
