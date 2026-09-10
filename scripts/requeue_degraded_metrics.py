#!/usr/bin/env python3
"""Пересчёт метрик, посчитанных на суррогатных данных (авария источника).

Когда биржевой НКД пропадает (10.09.2026: iss.moex.com лежал полдня), спреды
считаются по графику купонов — это лучше прочерка, но хуже факта, и такие числа
успевают лечь в архив: дневной снимок, часовые бары, спреды сделок. Скрипт
снимает их с полки и возвращает в очередь пересчёта; сами числа пересчитают
штатные демоны на живых данных.

ЗАПУСКАТЬ ПОСЛЕ ВОССТАНОВЛЕНИЯ ИСТОЧНИКА — иначе пересчёт положит на место
такие же оценки, только свежие.

    python scripts/requeue_degraded_metrics.py --from 2026-09-10 --till 2026-09-10
    python scripts/requeue_degraded_metrics.py --from ... --till ... --apply

Без --apply только показывает, сколько строк затронет.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _count(sql, args):
    from services.portfolio_db import _connect
    with _connect() as c:
        return c.execute(sql, args).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True, help="дата начала, YYYY-MM-DD")
    ap.add_argument("--till", dest="till", required=True, help="дата конца, YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="без него — только показать")
    a = ap.parse_args()

    frm, till = f"{a.frm} 00:00:00", f"{a.till} 23:59:59"
    days = [a.frm] if a.frm == a.till else None

    ticks = _count("SELECT COUNT(*) FROM trade_tick WHERE metrics_at>=? AND metrics_at<=?",
                   (frm, till))
    blocks = _count("SELECT COUNT(*) FROM block_trade WHERE metrics_at>=? AND metrics_at<=?",
                    (frm, till))
    hours = _count("SELECT COUNT(*) FROM bar_hourly WHERE ts>=? AND ts<=? "
                   "AND (y_idx_bps IS NOT NULL OR g_spread_bps IS NOT NULL)", (frm, till))
    degraded = _count("SELECT COUNT(*) FROM spread_daily WHERE src='snap_degraded' "
                      "AND date>=? AND date<=?", (a.frm, a.till))

    print(f"окно {a.frm}..{a.till}")
    print(f"  тиков:           {ticks}")
    print(f"  блочных сделок:  {blocks}")
    print(f"  часовых баров:   {hours}")
    print(f"  снимков degraded:{degraded}")

    if not a.apply:
        print("\nэто предпросмотр — добавьте --apply, чтобы вернуть строки в очередь")
        return

    from services import bars, block_trades, spread_history
    tr = block_trades.requeue_metrics_window(frm, till)
    br = bars.reset_metrics_window(frm, till)
    sd = spread_history.drop_degraded(days or None)
    print(f"\nвернули в очередь: тиков {tr['trade_tick']}, блоков {tr['block_trade']}, "
          f"часов {br['hours']}, дней свёртки {br['days']}, снимков снято {sd}")
    print("пересчёт подхватят штатные демоны (price_new_trades, ensure_bars, "
          "вечерний снимок 19:00)")


if __name__ == "__main__":
    main()
