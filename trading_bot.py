import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import os
import hashlib
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# --- DATABASE SETUP ---
DB_FILE = "trading_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    try:
        # Added user_hash to isolate data between different users
        c.execute("SELECT user_hash FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        user_hash TEXT, symbol TEXT, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                        sl REAL, rr REAL, atl_time TEXT, status TEXT, lh1_broken INTEGER DEFAULT 0,
                        PRIMARY KEY (user_hash, symbol))''')
    conn.commit()
    return conn

# --- SMC LOGIC (STRICT main40.py FILTERS) ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    
    # Strict Price Filter: 30-250
    if not (30 < atl_val < 250): return None
    if min_idx >= len(df) - 3: return None

    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)

    # Peak Detection
    search_start = max(0, min_idx - 300)
    pre_atl = df.iloc[search_start:min_idx].reset_index(drop=True)
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

    # FVG & SL
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

    lh1_broken = 1 if post_atl_data['h'].max() > lh1 else 0

    return {
        "ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1),
        "sl": round(float(sl_val), 1), "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S"),
        "lh1_broken": lh1_broken
    }

# --- USER-SESSION SCANNER ---
def run_user_scanner(user_hash, client_id, access_token):
    """Specific background thread for an individual user session."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, is_async=False)
            
            # Only scan symbols belonging to THIS user
            symbols_df = pd.read_sql("SELECT symbol FROM scanned_symbols WHERE user_hash=?", worker_conn, params=(user_hash,))
            for sym in symbols_df['symbol'].tolist():
                r_to = datetime.datetime.now().strftime("%Y-%m-%d")
                r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                res = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": r_from, "range_to": r_to, "cont_flag": "1"})
                
                if res.get('s') == 'ok':
                    df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                    data = analyze_logic_main40(df, sym)
                    if data:
                        worker_conn.execute("""UPDATE scanned_symbols SET 
                            ltp=?, atl=?, lh1=?, fvg=?, lh2=?, sl=?, rr=?, atl_time=?, lh1_broken=?, status='FOUND' 
                            WHERE symbol=? AND user_hash=?""", (data['ltp'], data['atl'], data['lh1'], data['fvg'], 
                                                                data['lh2'], data['sl'], data['rr'], data['atl_time'], 
                                                                data['lh1_broken'], sym, user_hash))
            worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300)

# --- UI INTERFACE ---
def main():
    st.set_page_config(page_title="SMC Multi-User Bot", layout="wide")
    conn = init_db()
    
    st.sidebar.title("🔑 User Login")
    
    # 1. User Credential Inputs
    u_client_id = st.sidebar.text_input("App ID (Client ID)", type="password")
    u_secret_key = st.sidebar.text_input("Secret Key", type="password")
    u_redirect_url = st.sidebar.text_input("Redirect URL", value="https://www.google.com/")
    
    if u_client_id and u_secret_key:
        user_hash = hashlib.sha256(u_client_id.encode()).hexdigest()
        
        if 'access_token' not in st.session_state:
            session = fyersModel.SessionModel(client_id=u_client_id, secret_key=u_secret_key, redirect_uri=u_redirect_url, response_type="code", grant_type="authorization_code")
            st.sidebar.markdown(f"[1. Click to Authorize]({session.generate_authcode()})")
            auth_code = st.sidebar.text_input("2. Enter Auth Code Here")
            
            if st.sidebar.button("3. Complete Login"):
                session.set_token(auth_code)
                res = session.generate_token()
                if "access_token" in res:
                    st.session_state['access_token'] = res["access_token"]
                    st.rerun()
        else:
            st.sidebar.success("Logged In ✅")
            if st.sidebar.button("Logout"):
                del st.session_state['access_token']
                st.rerun()

            # Start background scanner for this specific user if not already running
            if f'bg_{user_hash}' not in st.session_state:
                threading.Thread(target=run_user_scanner, args=(user_hash, u_client_id, st.session_state['access_token']), daemon=True).start()
                st.session_state[f'bg_{user_hash}'] = True

            # FETCH OPTIONS BUTTON
            if st.sidebar.button("Fetch & Seed Options", width='stretch'):
                fyers = fyersModel.FyersModel(client_id=u_client_id, token=st.session_state['access_token'])
                for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                    oc = fyers.optionchain({"symbol": idx, "strikecount": 30})
                    if oc.get('s') == 'ok':
                        for opt in oc['data']['optionsChain']:
                            conn.execute("INSERT OR IGNORE INTO scanned_symbols (user_hash, symbol, status) VALUES (?, ?, 'WATCHING')", (user_hash, opt['symbol']))
                conn.commit()
                st.toast("Seeded personal scan list!")

            # --- DISPLAY DATA FOR THIS USER ONLY ---
            tab1, tab_watchlist, tab2 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades"])
            
            # Filter all queries by user_hash
            full_df = pd.read_sql("SELECT symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time, lh1_broken FROM scanned_symbols WHERE status='FOUND' AND user_hash=? ORDER BY rr DESC", conn, params=(user_hash,))
            
            with tab1:
                st.subheader("Your Scanned Patterns")
                st.dataframe(full_df, width='stretch')

            with tab_watchlist:
                st.subheader("Watchlist")
                watchlist_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['ltp'] > full_df['fvg'] * 1.02)]
                st.dataframe(watchlist_df, width='stretch')

            with tab2:
                st.subheader("Active Trades")
                active_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['ltp'] <= full_df['fvg'] * 1.02) & (full_df['ltp'] >= full_df['sl'])]
                st.dataframe(active_df, width='stretch')
    else:
        st.info("Please enter your App ID and Secret Key in the sidebar to begin.")

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()
