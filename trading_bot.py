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

# --- CONFIG & SECRETS ---
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_URI = st.secrets["postgres"]["uri"] 

def init_db():
    try:
        conn = psycopg2.connect(DB_URI)
        with conn.cursor() as c:
            # 1. Scanned Symbols
            c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                            symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                            sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
            # 2. Active Trades
            c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                            symbol TEXT PRIMARY KEY, entry REAL, sl REAL, target REAL, 
                            qty INTEGER, mode TEXT)''')
            # 3. Trade History
            c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                            symbol TEXT, entry REAL, exit REAL, result TEXT, pnl REAL, time TEXT)''')
        conn.commit()
        return conn
    except Exception as e:
        st.error(f"Database Connection Error: {e}")
        st.stop()

def get_lot_size(symbol):
    if "NIFTY" in symbol.upper(): return 65
    if "SENSEX" in symbol.upper(): return 20
    return 1

# --- CORE LOGIC (UNCHANGED) ---
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
    while True:
        try:
            with psycopg2.connect(DB_URI) as conn:
                if os.path.exists(TOKEN_FILE):
                    token = open(TOKEN_FILE).read().strip()
                    fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
                    
                    # Update LTP
                    with conn.cursor() as cur:
                        cur.execute("SELECT symbol FROM scanned_symbols")
                        for (sym,) in cur.fetchall():
                            res = fyers.quotes({"symbols": sym})
                            if res.get('s') == 'ok':
                                cur.execute("UPDATE scanned_symbols SET ltp=%s WHERE symbol=%s", (res['d'][0]['v']['lp'], sym))
                    
                    # Monitor Active Trades
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
        except: pass
        time.sleep(15)

def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    init_db()
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_background_engine, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Controls")
    trade_mode = st.sidebar.radio("Mode", ["Virtual", "Real"])
    
    # Capital Display
    cap = 100000.0 if trade_mode == "Virtual" else 0.0 # Placeholder for API funds
    st.sidebar.metric("Capital", f"₹{cap:,.2f}")

    if not os.path.exists(TOKEN_FILE):
        # ... [Fyers Authorization Logic] ...
        pass
    else:
        if st.sidebar.button("Fetch High RR Options", width='stretch'):
            token = open(TOKEN_FILE).read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                oc = fyers.optionchain({"symbol": idx, "strikecount": 7}) 
                if oc.get('s') == 'ok':
                    for opt in oc['data']['optionsChain']:
                        sym = opt['symbol']
                        hist = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d"), "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"})
                        if hist.get('s') == 'ok':
                            df = pd.DataFrame(hist['candles'], columns=['t','o','h','l','c','v'])
                            data = analyze_logic_main40(df, sym)
                            if data:
                                with psycopg2.connect(DB_URI) as conn:
                                    with conn.cursor() as cur:
                                        cur.execute("""INSERT INTO scanned_symbols VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'FOUND') 
                                                       ON CONFLICT (symbol) DO UPDATE SET ltp=EXCLUDED.ltp, rr=EXCLUDED.rr""", 
                                                    (sym, data['ltp'], data['atl'], data['lh1'], data['fvg'], data['lh2'], data['sl'], data['rr'], data['atl_time']))
                                    conn.commit()

    tab1, tab_watchlist, tab2, tab_history = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades", "📜 History"])
    
    with psycopg2.connect(DB_URI) as conn:
        with tab1:
            st.dataframe(pd.read_sql("SELECT * FROM scanned_symbols", conn), use_container_width=True)

        with tab_watchlist:
            watchlist = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='FOUND'", conn)
            valid = watchlist[(watchlist['ltp'] >= watchlist['lh1']) & (watchlist['ltp'] <= (watchlist['fvg'] * 1.01)) & (watchlist['ltp'] >= watchlist['sl'])]
            if not valid.empty:
                for _, r in valid.iterrows():
                    # Logic to move to Active Trades if not already present
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM active_trades WHERE symbol=%s", (r['symbol'],))
                        if not cur.fetchone():
                            lot = get_lot_size(r['symbol'])
                            qty = int((cap // r['ltp']) // lot) * lot
                            if qty > 0:
                                cur.execute("INSERT INTO active_trades VALUES (%s,%s,%s,%s,%s,%s)", (r['symbol'], r['ltp'], r['sl'], r['lh2'], qty, trade_mode))
                conn.commit()
                st.dataframe(valid, use_container_width=True)

        with tab2:
            st.dataframe(pd.read_sql("SELECT * FROM active_trades", conn), use_container_width=True)

        with tab_history:
            st.dataframe(pd.read_sql("SELECT * FROM trade_history ORDER BY time DESC", conn), use_container_width=True)

    st_autorefresh(interval=10000, key="ui_refresh")

if __name__ == "__main__": main()
