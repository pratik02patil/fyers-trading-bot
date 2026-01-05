import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import pytz
import json
import os
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# ==========================================
# 1. CONFIGURATION & SECRETS
# ==========================================
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_FILE = "trading_bot.db"

def init_db():
    """Initializes SQLite tables for scanner and active trades."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Table structure updated to match main40.py logic needs
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, lh2 REAL, 
                    fvg_low REAL, target REAL, sl REAL, rr REAL, 
                    status TEXT, is_today INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                    symbol TEXT PRIMARY KEY, entry_price REAL, ltp REAL, 
                    pnl REAL, target REAL, sl REAL, status TEXT)''')
    conn.commit()
    return conn

# ==========================================
# 2. SMC ANALYSIS LOGIC (FROM MAIN40.PY)
# ==========================================
def analyze_smc_logic(df):
    """
    Implements the SMC logic from main40.py:
    - Identifies All-Time Low (ATL)
    - Detects two previous peaks (Lower Highs: LH1, LH2)
    - Finds Fair Value Gap (FVG) after ATL
    """
    if df.empty or len(df) < 20: return None
    
    # Identify ATL (All-Time Low)
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    
    # Logic from main40.py: ATL must be in a tradeable range
    if not (10 < atl_val < 500): return None
    if min_idx >= len(df) - 3: return None
    
    # Identify Peaks (LH1 & LH2) before the ATL
    df_before = df.iloc[:min_idx]
    if len(df_before) < 10: return None
    
    peaks = []
    # Peak detection: Current high is greater than 2 previous and 2 next candles
    for i in range(2, len(df_before)-2):
        if df_before['h'].iloc[i] == df_before['h'].iloc[i-2:i+3].max():
            peaks.append(df_before['h'].iloc[i])
            
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2]
    
    # Find Fair Value Gap (FVG) after ATL
    df_after = df.iloc[min_idx:]
    fvg = None
    for i in range(len(df_after) - 2):
        # Bullish FVG check
        if df_after['l'].iloc[i+2] > df_after['h'].iloc[i]:
            fvg = df_after['h'].iloc[i]
            break
    
    if not fvg: return None
    
    # Trade Parameter Calculations
    sl = atl_val - (atl_val * 0.001)
    target = lh2
    rr = round((target - fvg) / (fvg - sl), 2) if (fvg - sl) != 0 else 0
    
    return {"atl": atl_val, "lh1": lh1, "lh2": lh2, "fvg": fvg, "sl": sl, "target": target, "rr": rr}

# ==========================================
# 3. BACKGROUND WORKER (DATA UPDATER)
# ==========================================
def background_worker():
    """Thread to update prices and scan for patterns every 5 minutes."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            token = ""
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
            
            if token:
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                scanned = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)
                
                for sym in scanned['symbol'].tolist():
                    # Fetch historical data (5-minute resolution)
                    hist_data = {
                        "symbol": sym, "resolution": "5", "date_format": "1", 
                        "range_from": (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
                        "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"
                    }
                    
                    res = fyers.history(data=hist_data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        analysis = analyze_smc_logic(df)
                        
                        if analysis:
                            curr_ltp = df['c'].iloc[-1]
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                ltp=?, atl=?, lh1=?, lh2=?, fvg_low=?, target=?, sl=?, rr=?, status='PATTERN_DETECTED' 
                                WHERE symbol=?""", (curr_ltp, analysis['atl'], analysis['lh1'], analysis['lh2'], 
                                                    analysis['fvg'], analysis['target'], analysis['sl'], analysis['rr'], sym))
                worker_conn.commit()
            worker_conn.close()
        except Exception: pass
        time.sleep(300)

# ==========================================
# 4. STREAMLIT UI
# ==========================================
def main():
    st.set_page_config(page_title="SMC Trading Bot", layout="wide")
    conn = init_db()
    
    if 'bg_task_running' not in st.session_state:
        threading.Thread(target=background_worker, daemon=True).start()
        st.session_state['bg_task_running'] = True

    # SIDEBAR: FYERS AUTHENTICATION
    st.sidebar.title("Fyers Cloud Login")
    token = ""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f: token = f.read().strip()

    if not token:
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                          redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.warning("Login Required")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter the code from URL:")
        if st.sidebar.button("Save Access Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            if "access_token" in res:
                with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
                st.rerun()
    else:
        st.sidebar.success("Fyers API Active ✅")
        if st.sidebar.button("Re-Scan Option Chain"):
            # Logic to fetch new symbols via Fyers Option Chain API
            st.toast("Refreshing symbols...")

    # MAIN DASHBOARD TABS
    tab_scan, tab_active = st.tabs(["📊 SMC Pattern Scanner", "📉 Active Trades"])
    
    with tab_scan:
        st.subheader("Detected Smart Money Patterns")
        df_scan = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='PATTERN_DETECTED' ORDER BY rr DESC", conn)
        
        # FIXED: width="stretch" resolves the StreamlitInvalidWidthError
        st.dataframe(df_scan, width="stretch")

    with tab_active:
        st.subheader("Current Market Positions")
        df_active = pd.read_sql("SELECT * FROM active_trades", conn)
        st.dataframe(df_active, width="stretch")

    # Refresh the UI every 60 seconds
    st_autorefresh(interval=60000, key="global_refresh")
    conn.close()

if __name__ == "__main__":
    main()
