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
            c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                            symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                            sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                            symbol TEXT PRIMARY KEY, entry REAL, sl REAL, target REAL, 
                            qty INTEGER, mode TEXT)''')
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
                    
                    with conn.cursor() as cur:
                        cur.execute("SELECT symbol FROM scanned_symbols")
                        for (sym,) in cur.fetchall():
                            res = fyers.quotes({"symbols": sym})
                            if res.get('s') == 'ok':
                                cur.execute("UPDATE scanned_symbols SET ltp=%s WHERE symbol=%s", (res['d'][0]['v']['lp'], sym))
                conn.commit()
        except: pass
        time.sleep(15)

def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_background_engine, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Fyers Login & Controls")
    trade_mode = st.sidebar.radio("Trade Mode", ["Virtual", "Real Account"])

    # --- RESTORED ACCOUNT BALANCE DISPLAY ---
    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
        fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
        
        if trade_mode == "Real Account":
            prof = fyers.funds()
            if prof.get('s') == 'ok':
                # Displays real margin available in your Fyers account
                balance = next((item['fifo_margin'] for item in prof['fund_limit'] if item['id'] == 10), 0.0)
                st.sidebar.metric("Real Account Balance", f"₹{balance:,.2f}")
        else:
            # Displays fixed virtual balance
            st.sidebar.metric("Virtual Account Balance", "₹1,00,000.00")

    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Code:")
        if st.sidebar.button("Save Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success(f"Fyers Active ✅")
        if st.sidebar.button("Fetch High RR Options", width='stretch'):
            # ... (Scanning and insertion logic)
            pass

    tab1, tab_watchlist, tab2, tab_history = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades", "📜 History"])
    
    # ... (Tab display logic using psycopg2 connections)
    
    st_autorefresh(interval=10000, key="ui_refresh")

if __name__ == "__main__": main()
