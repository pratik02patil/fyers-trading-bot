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
        # User isolation via user_hash
        c.execute("SELECT user_hash FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        user_hash TEXT, symbol TEXT, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                        sl REAL, rr REAL, atl_time TEXT, status TEXT, lh1_broken INTEGER DEFAULT 0,
                        PRIMARY KEY (user_hash, symbol))''')
    conn.commit()
    return conn

# --- STRICT LOGIC FROM main40.py ---
def analyze_logic_main40(df, sym):
    """Directly implements filters from main40.py."""
    if df.empty or len(df) < 20: return "Insufficient Candles"
    
    # 1. ATL & Price Filter (30-250)
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    
    if not (30 < atl_val < 250): 
        return f"Price ₹{atl_val} out of range (30-250)"
    if min_idx >= len(df) - 3: 
        return "ATL too recent"

    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)

    # 2. Peak Detection (300 candle lookback)
    search_start = max(0, min_idx - 300)
    pre_atl = df.iloc[search_start:min_idx].reset_index(drop=True)
    all_peaks = []
    for i in range(len(pre_atl) - 2, 1, -1):
        curr_h = pre_atl['h'].iloc[i]
        if curr_h > pre_atl['h'].iloc[i-1] and curr_h > pre_atl['h'].iloc[i+1]:
            all_peaks.append(curr_h)
            
    if not all_peaks: return "No Peaks Found"
    lh1 = all_peaks[0] 
    
    # LH2 search with 1.5x fallback
    lh2 = None
    for p in all_peaks[1:]:
        if p >= lh1 * 1.5:
            lh2 = p
            break
    if lh2 is None and len(all_peaks) > 1: lh2 = all_peaks[1]
    elif lh2 is None: return "No LH2 Peak"

    # 3. FVG & SL
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    
    if not fvg_entry: fvg_entry = atl_val * 1.05
    sl_val = round(atl_val - (atl_val * 0.02), 1)
    
    if fvg_entry <= sl_val: return "Entry <= SL"
    
    # 4. RR Filter (Must be > 4)
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return f"Low RR: {rr}"

    # Check for LH1 Break (Tracking for Watchlist/Active)
    lh1_broken = 1 if post_atl_data['h'].max() > lh1 else 0

    return {
        "ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1),
        "sl": round(float(sl_val), 1), "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S"),
        "lh1_broken": lh1_broken
    }

# --- BACKGROUND SCANNER ---
def run_user_scanner(user_hash, client_id, access_token):
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, is_async=False)
            symbols_df = pd.read_sql("SELECT symbol FROM scanned_symbols WHERE user_hash=?", worker_conn, params=(user_hash,))
            
            for sym in symbols_df['symbol'].tolist():
                r_to = datetime.datetime.now().strftime("%Y-%m-%d")
                r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                res = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": r_from, "range_to": r_to, "cont_flag": "1"})
                
                if res.get('s') == 'ok':
                    df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                    data = analyze_logic_main40(df, sym)
                    if isinstance(data, dict):
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
    st.set_page_config(page_title="Institutional Pro (Multi-User)", layout="wide")
    conn = init_db()
    
    st.sidebar.title("🔐 Multi-User Login")
    u_client_id = st.sidebar.text_input("Fyers App ID", type="password", help="Enter your unique Client ID")
    u_secret_key = st.sidebar.text_input("Fyers Secret Key", type="password")
    u_redirect_url = st.sidebar.text_input("Redirect URL", value="https://www.google.com/")
    
    if u_client_id and u_secret_key:
        user_hash = hashlib.sha256(u_client_id.encode()).hexdigest()
        
        if 'access_token' not in st.session_state:
            session = fyersModel.SessionModel(client_id=u_client_id, secret_key=u_secret_key, redirect_uri=u_redirect_url, response_type="code", grant_type="authorization_code")
            st.sidebar.markdown(f"**[1. Click to Authorize App]({session.generate_authcode()})**")
            auth_code = st.sidebar.text_input("2. Enter Auth Code from URL")
            
            if st.sidebar.button("3. Verify & Login"):
                session.set_token(auth_code)
                res = session.generate_token()
                if "access_token" in res:
                    st.session_state['access_token'] = res["access_token"]
                    st.rerun()
        else:
            st.sidebar.success("Session Active ✅")
            if st.sidebar.button("Logout"):
                del st.session_state['access_token']
                st.rerun()

            if f'bg_{user_hash}' not in st.session_state:
                threading.Thread(target=run_user_scanner, args=(user_hash, u_client_id, st.session_state['access_token']), daemon=True).start()
                st.session_state[f'bg_{user_hash}'] = True

            if st.sidebar.button("Fetch & Seed Options (All Indices)", width='stretch'):
                fyers = fyersModel.FyersModel(client_id=u_client_id, token=st.session_state['access_token'])
                # Nifty, BankNifty, and Sensex as per main40.py
                for idx in ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "BSE:SENSEX-INDEX"]:
                    oc = fyers.optionchain({"symbol": idx, "strikecount": 30})
                    if oc.get('s') == 'ok':
                        for opt in oc['data']['optionsChain']:
                            conn.execute("INSERT OR IGNORE INTO scanned_symbols (user_hash, symbol, status) VALUES (?, ?, 'WATCHING')", (user_hash, opt['symbol']))
                conn.commit()
                st.toast("Scanning initialized for 180+ options.")

            # --- TABS ---
            tab1, tab_watchlist, tab2 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades"])
            full_df = pd.read_sql("SELECT symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time, lh1_broken FROM scanned_symbols WHERE status='FOUND' AND user_hash=? ORDER BY rr DESC", conn, params=(user_hash,))
            
            with tab1:
                st.subheader("Detected Patterns (Strict main40.py Filters)")
                st.dataframe(full_df, width='stretch')

            with tab_watchlist:
                st.subheader("Watchlist: Waiting for Retracement")
                # LH1 is broken, waiting for price to drop to FVG entry
                watchlist_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['ltp'] > full_df['fvg'] * 1.02)]
                st.dataframe(watchlist_df, width='stretch')

            with tab2:
                st.subheader("Active Trades: Retracement Complete")
                # LH1 is broken and price is in FVG entry zone
                active_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['ltp'] <= full_df['fvg'] * 1.02) & (full_df['ltp'] >= full_df['sl'])]
                st.dataframe(active_df, width='stretch')
    else:
        st.warning("Please enter your own App ID and Secret Key to access your private trading dashboard.")

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()
