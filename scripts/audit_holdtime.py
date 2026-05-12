"""How long did each rebalance-exited tail-NO trade actually live?"""
import sqlite3
from datetime import datetime

c = sqlite3.connect("data/weather/weather.db")
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, city, created_at, settled_at, entry_price, notional, pnl, "
    "       bracket_lower_f, bracket_upper_f, target_date, exit_reason "
    "FROM trade_history WHERE category='weather_tail_no' AND outcome=2 "
    "ORDER BY id"
).fetchall()
print(
    f"  {'id':>3} {'created':<17} {'settled':<17} {'hold':>7} "
    f"{'entry':>6} {'pnl':>8} {'%loss':>7} {'reason':<25}"
)
hold = []
for r in rows:
    # Strip timezones — stored times are mixed (created_at often naive UTC,
    # settled_at often '+00:00'). Compare as naive UTC.
    cr_s = r['created_at'].replace('Z', '').split('+')[0].split('.')[0]
    st_s = r['settled_at'].replace('Z', '').split('+')[0].split('.')[0]
    cr = datetime.fromisoformat(cr_s)
    st = datetime.fromisoformat(st_s)
    h_min = (st - cr).total_seconds() / 60
    hold.append(h_min)
    pct = float(r['pnl']) / float(r['notional']) * 100
    print(
        f"  #{r['id']:>3} {r['created_at'][:16]:<17} {r['settled_at'][:16]:<17} "
        f"{h_min:>5.0f}m {float(r['entry_price']):>6.3f} "
        f"${float(r['pnl']):>+7.2f} {pct:>+6.1f}% "
        f"{(r['exit_reason'] or '')[:25]:<25}"
    )
hold.sort()
n = len(hold)
print()
print(f"Hold time stats (n={n}):")
print(f"  min={hold[0]:.0f}m  p50={hold[n//2]:.0f}m  p90={hold[int(n*0.9)]:.0f}m  max={hold[-1]:.0f}m")
print(f"  in hours: p50={hold[n//2]/60:.2f}h  p90={hold[int(n*0.9)]/60:.2f}h")
