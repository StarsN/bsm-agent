import sqlite3, json

DB = r"C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\binance_square.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# FET 持仓
print("=== FET trade_positions ===")
for r in conn.execute("SELECT * FROM trade_positions WHERE token='FET'"):
    r = dict(r)
    for k in ['id','token','side','status','entry_price','current_price','stop_loss_price',
              'tp1_price','tp2_price','realized_pnl','unrealized_pnl','pnl_pct',
              'advice','open_reason','created_at','closed_at']:
        print(f"  {k}: {r.get(k)}")
    print()

# FET journal
print("=== FET journal ===")
for r in conn.execute("SELECT * FROM journal WHERE token='FET' ORDER BY id"):
    r = dict(r)
    for k in ['id','token','action','price','tier','stop_loss','tp1_price','tp2_price',
              'pnl_pct','close_reason','reason','hold_duration','created_at']:
        print(f"  {k}: {r.get(k)}")
    print()

# FET loss archive
print("=== FET loss_archive ===")
for r in conn.execute("SELECT * FROM trade_loss_archive WHERE token='FET'"):
    r = dict(r)
    for k in ['id','token','entry_price','exit_price','pnl_pct','failed_reason','reason_tags']:
        print(f"  {k}: {r.get(k)}")
    print()

# FET pending_decisions
print("=== FET pending_decisions ===")
for r in conn.execute("SELECT * FROM pending_decisions WHERE token='FET' ORDER BY id"):
    r = dict(r)
    for k in ['id','action','token','tier','status','reason','reject_reason',
              'close_reason','created_at','consumed_at']:
        print(f"  {k}: {r.get(k)}")
    print()

conn.close()
