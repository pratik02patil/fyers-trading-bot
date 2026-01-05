import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import os
import json
import hashlib
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# --- CONFIG & LOCAL STORAGE ---
DB_FILE = "trading_bot.db"
USER_CONFIG_FILE = "user_configs.json"

def load_user_configs():
    if os.path.exists(USER_CONFIG_FILE):
        with open(USER_CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_user_config(app_id, secret_key):
    configs = load_user_configs()
    configs[app_id] = secret_key
    with open(USER_CONFIG_FILE, "w") as f:
        json.dump(configs, f)

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    try:
        # Added user_id to isolate data between different users
        c.execute("SELECT user_id FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        user_id TEXT, symbol TEXT, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                        sl REAL, rr REAL, atl_time TEXT, status TEXT,
                        PRIMARY KEY (user_id, symbol))''')
    conn.commit()
    return conn

# --- LOGIC RETAINED FROM PREVIOUS ITERATION (UNTOUCHED) ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    if min_idx >= len(df) - 3: return None
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    search_start = max(0, min_idx - 300)
    pre_atl = df.iloc[search_start:min_idx].reset_index(drop=True)
    if len(pre_atl) < 5: return None
    all_peaks = []
    for i in range(len(pre_atl) - 2, 1, -1):
        curr_h = pre_atl['h'].iloc[i]
        if curr_h > pre_atl['h'].iloc[i-1] and curr_h > pre_atl['h'].iloc[i+1]:
            all_peaks.append(curr_h)
    if not all_peaks: return None
    lh1 = all_peaks[0] 
    lh2 = None
    for p in all_peaks[1:]:
        if p >= lh1 * 1.5:
            lh2 = p
            break
    if lh2 is None and len(all_peaks) > 1: lh2 = all_peaks[1]
    elif lh2 is None: return None 
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    if not fvg_entry: fvg_entry = atl_val * 1.05
    sl_val = round(atl_val - (atl_val * 0.02), 1)
    if fvg_entry <= sl_val: return None
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return None
    return {
        "ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1),
        "sl": round(float(sl_val), 1), "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S")
    }

def run_scanner(user_id, app_id, token):
    """Background scanner specific to a user's session."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            fyers = fyersModel.FyersModel(client_id=app_id, token=token, is_async=False)
            symbols_df = pd.read_sql("SELECT symbol FROM scanned_symbols WHERE user_id=?", worker_conn, params=(user_id,))
            for sym in symbols_df['symbol'].tolist():
                r_to = datetime.datetime.now().strftime("%Y-%m-%d")
                r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                res = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": r_from, "range_to": r_to, "cont_flag": "1"})
                if res.get('s') == 'ok':
                    df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                    data = analyze_logic_main40(df, sym)
                    if data:
                        worker_conn.execute("""UPDATE scanned_symbols SET ltp=?, atl=?, lh1=?, fvg=?, lh2=?, sl=?, rr=?, atl_time=?, status='FOUND' 
                                            WHERE symbol=? AND user_id=?""", (data['ltp'], data['atl'], data['lh1'], data['fvg'], 
                                                                             data['lh2'], data['sl'], data['rr'], data['atl_time'], sym, user_id))
            worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300)

# --- UI INTERFACE ---
def main():
    st.set_page_config(page_title="SMC Multi-User Bot", layout="wide")
    conn = init_db()
    
    # 1. Sidebar Login Handling
    st.sidebar.title("🔐 Multi-User Login")
    saved_configs = load_user_configs()
    
    app_id = st.sidebar.selectbox("Select Saved App ID", ["New Login"] + list(saved_configs.keys()))
    
    if app_id == "New Login":
        u_app_id = st.sidebar.text_input("Enter App ID")
        u_secret = st.sidebar.text_input("Enter Secret Key", type="password")
        if st.sidebar.button("Save & Use"):
            save_user_config(u_app_id, u_secret)
            st.rerun()
        return # Stop execution until user logs in
    else:
        u_app_id = app_id
        u_secret = saved_configs[app_id]
        user_id = hashlib.md5(u_app_id.encode()).hexdigest() # Unique ID for DB isolation

    # 2. Token Authentication
    if f'token_{user_id}' not in st.session_state:
        session = fyersModel.SessionModel(client_id=u_app_id, secret_key=u_secret, redirect_uri="https://www.google.com/", response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Auth Code:")
        if st.sidebar.button("Login"):
            session.set_token(auth_code)
            res = session.generate_token()
            if "access_token" in res:
                st.session_state[f'token_{user_id}'] = res["access_token"]
                st.rerun()
    else:
        st.sidebar.success(f"Connected: {u_app_id} ✅")
        token = st.session_state[f'token_{user_id}']
        
        # Start background scanner for this specific user
        if f'bg_{user_id}' not in st.session_state:
            threading.Thread(target=run_scanner, args=(user_id, u_app_id, token), daemon=True).start()
            st.session_state[f'bg_{user_id}'] = True

        if st.sidebar.button("Fetch Options", width='stretch'):
            fyers = fyersModel.FyersModel(client_id=u_app_id, token=token)
            for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                oc = fyers.optionchain({"symbol": idx, "strikecount": 7})
                if oc.get('s') == 'ok':
                    for opt in oc['data']['optionsChain']:
                        sym = opt['symbol']
                        # Insert with user_id to prevent data mixing
                        conn.execute("INSERT OR IGNORE INTO scanned_symbols (user_id, symbol, status) VALUES (?, ?, 'WATCHING')", (user_id, sym))
            conn.commit()

        # --- DATA DISPLAY (Filtered by user_id) ---
        tab1, tab_watchlist, tab2 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades"])
        
        with tab1:
            st.subheader("Your Scanned Patterns")
            full_df = pd.read_sql("SELECT symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time FROM scanned_symbols WHERE status='FOUND' AND user_id=? ORDER BY rr DESC", conn, params=(user_id,))
            st.dataframe(full_df, width='stretch')

        with tab_watchlist:
            st.subheader("Your Watchlist")
            if not full_df.empty:
                watchlist_df = full_df[(full_df['ltp'] >= full_df['lh1']) & (full_df['ltp'] <= (full_df['fvg'] * 1.01)) & (full_df['ltp'] >= full_df['sl'])]
                st.dataframe(watchlist_df, width='stretch')

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()
