#!/usr/bin/env bash
# Применение новой спеки фиксинга на проде (после deploy.sh):
#   1) unfreeze_fixing_spec        — снять замороженные xlsx-импортом поля там,
#                                    где БД == парсер (безопасное подмножество);
#   2) import_bondresearch_specs   — залить лаг/метод с bondresearch.ru (br_* слой);
#   3) clear_conflicting_fixing_fields — снять явные поля, конфликтующие с BR,
#                                    чтобы победил bondresearch (бэкап в data/).
# Все шаги идемпотентны; скрипты печатают, что делают.
# Использование: ./scripts/prod_apply_fixing_specs.sh [--dry-run]
set -euo pipefail

SERVER=root@161.104.17.23
REMOTE=/root/floaters
DC="docker compose -f docker-compose.prod.yml"

RUN() { ssh -o BatchMode=yes "$SERVER" "cd $REMOTE && $*"; }

if [[ "${1:-}" == "--verify" ]]; then
  echo '>>> verify: эффективная спека контрольных бумаг + расклад источников'
  RUN "$DC exec -T floaters python - <<'PY'
from services.ref_data import coupon_formula
for isin, name in [('RU000A10D1H3','РЖД 1Р-46R'), ('RU000A106K43','avg_prev-кейс'),
                   ('RU000A102QL3','point·5 BR')]:
    s = coupon_formula(isin)
    print(f'{isin} {name}: {s[\"coupon_mode\"]}·{s[\"fixing_lag\"]} {s[\"fixing_lag_unit\"]}')
from services import instruments_registry as reg
from collections import Counter
rows = reg.list_catalog(floaters_only=True)
print(Counter((x.get('spec_eff') or 'нет').split('(')[-1].rstrip(')') for x in rows).most_common(8))
PY"
  exit 0
fi

if [[ "${1:-}" == "--dry-run" ]]; then
  echo '>>> [dry-run] unfreeze'
  RUN "$DC exec -T floaters python scripts/unfreeze_fixing_spec.py"
  echo '>>> [dry-run] bondresearch import'
  RUN "$DC exec -T floaters python scripts/import_bondresearch_specs.py"
  echo '>>> [dry-run] clear conflicts'
  RUN "$DC exec -T floaters python scripts/clear_conflicting_fixing_fields.py"
  exit 0
fi

echo '>>> unfreeze (APPLY=1, только строки БД==парсер)'
RUN "$DC exec -T -e APPLY=1 floaters python scripts/unfreeze_fixing_spec.py"

echo '>>> bondresearch import --apply'
RUN "$DC exec -T floaters python scripts/import_bondresearch_specs.py --apply"

echo '>>> clear conflicting fixing fields --apply'
RUN "$DC exec -T floaters python scripts/clear_conflicting_fixing_fields.py --apply"

echo '>>> done'
