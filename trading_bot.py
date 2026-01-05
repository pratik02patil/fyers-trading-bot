import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import time
import threading
import datetime
import os
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# --- DATABASE SETUP ---
DB_URI = st.secrets["postgres"]["uri"]

def init_db():
    conn = psycopg2.connect(DB_URI)
    with conn.cursor() as c:
        # Scanned symbols table
        c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                        symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                        sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
        # Active trades table
        c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                        symbol TEXT PRIMARY KEY, entry REAL, sl REAL, target REAL, 
                        qty INTEGER, mode TEXT)''')
        # History table
        c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                        symbol TEXT, entry REAL, exit REAL, result TEXT, pnl REAL, time TEXT)''')
    conn.commit()
    return conn

def get_lot_size(symbol):
    if "NIFTY" in symbol.upper(): return 65
    if "SENSEX" in symbol.upper(): return 20
    return 1

# --- LOGIC (UNCHANGED CORE) ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    pre_atl = df.iloc[max(0, min_idx - 300):min_idx].reset_index(drop=True)
    if len(pre_atl) < 5: return None
    all_peaks = [pre_atl['h'].iloc[i] for i in range(len(pre_atl)-2, 1, -1) 
                 if pre_atl['h'].iloc[i] > pre_atl['h'].iloc[i-1] and pre_atl['h'].iloc[i] > pre_atl['h'].iloc[i+1]]
    if not all_peaks: return None
    lh1, lh2 = all_peaks[0], (all_peaks[1] if len(all_peaks) > 1 else all_peaks[0])
    
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    fvg_entry = fvg_entry or (atl_val * 1.05)
    sl_val = round(atl_val * 0.98, 1)
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return None
    return {"ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), "lh1": round(float(lh1), 1), 
            "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1), "sl": round(float(sl_val), 1), 
            "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S")}

def run_background_engine():
    """Persistent engine using PostgreSQL connections."""
    while True:
        try:
            with psycopg2.connect(DB_URI) as conn:
                if os.path.exists("access_token.txt"):
                    token = open("access_token.txt").read().strip()
                    fyers = fyersModel.FyersModel(client_id=st.secrets["fyers"]["client_id"], token=token)
                    
                    # 1. Update Scanned LTP
                    with conn.cursor() as cur:
                        cur.execute("SELECT symbol FROM scanned_symbols")
                        for (sym,) in cur.fetchall():
                            res = fyers.quotes({"symbols": sym})
                            if res.get('s') == 'ok':
                                cur.execute("UPDATE scanned_symbols SET ltp=%s WHERE symbol=%s", (res['d'][0]['v']['lp'], sym))
                    
                    # 2. Monitor Active Trades
                    active_trades = pd.read_sql("SELECT * FROM active_trades", conn)
                    for _, trade in active_trades.iterrows():
                        res = fyers.quotes({"symbols": trade['symbol']})
                        if res.get('s') == 'ok':
                            ltp = res['d'][0]['v']['lp']
                            result = "TARGET" if ltp >= trade['target'] else ("SL" if ltp <= trade['sl'] else None)
                            if result:
                                exit_px = trade['target'] if result == "TARGET" else trade['sl']
                                pnl = (exit_px - trade['entry']) * trade['qty']
                                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                with conn.cursor() as cur:
                                    cur.execute("INSERT INTO trade_history VALUES (%s,%s,%s,%s,%s,%s)", 
                                                (trade['symbol'], trade['entry'], exit_px, result, pnl, ts))
                                    cur.execute("DELETE FROM active_trades WHERE symbol=%s", (trade['symbol'],))
                conn.commit()
        except Exception as e: print(f"DB Error: {e}")
        time.sleep(10)

def main():
    st.set_page_config(page_title="Postgres SMC Bot", layout="wide")
    init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_background_engine, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Trading Controls")
    mode = st.sidebar.radio("Execution Mode", ["Virtual", "Real Account"])
    
    # Token & Capital Logic (Same as before)
    # ... [Fyers Login Logic] ...

    # --- UPDATED TAB QUERIES ---
    tab1, tab2, tab3, tab4 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades", "📜 History"])
    
    with psycopg2.connect(DB_URI) as conn:
        with tab1:
            st.dataframe(pd.read_sql("SELECT * FROM scanned_symbols", conn), use_container_width=True)

        with tab2:
            watchlist = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='FOUND'", conn)
            valid = watchlist[(watchlist['ltp'] >= watchlist['lh1']) & (watchlist['ltp'] <= (watchlist['fvg'] * 1.01)) & (watchlist['ltp'] >= watchlist['sl'])]
            if not valid.empty:
                for _, r in valid.iterrows():
                    # Place trade logic remains same, just uses %s for Postgres placeholders
                    pass
                st.dataframe(valid)

        with tab3:
            active = pd.read_sql("SELECT a.*, s.ltp as current_ltp FROM active_trades a JOIN scanned_symbols s ON a.symbol = s.symbol", conn)
            st.table(active)

        with tab4:
            st.dataframe(pd.read_sql("SELECT * FROM trade_history ORDER BY time DESC", conn), use_container_width=True)

    st_autorefresh(interval=10000, key="ui_refresh")

if __name__ == "__main__": main()
