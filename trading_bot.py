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
    Implements the logic from main40.py:
    1. Finds All-Time Low (ATL).
    2. Identifies two previous peaks (Lower Highs).
    3. Finds a Fair Value Gap (FVG) after the ATL.
    """
    if df.empty or len(df) < 20: return None
    
    # Identify ATL
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    
    # Filter: ATL must be in a realistic range for the strategy (30-250)
    if not (30 < atl_val < 250): return None
    if min_idx >= len(df) - 3: return None
    
    # Identify Peaks (LH1 & LH2) before ATL
    df_before = df.iloc[:min_idx]
    if len(df_before) < 10: return None
    
    # Peak detection logic: a high higher than 2 neighbors on each side
    peaks = []
    for i in range(2, len(df_before)-2):
        if df_before['h'].iloc[i] == df_before['h'].iloc[i-2:i+3].max():
            peaks.append(df_before['h'].iloc[i])
            
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2]
    
    # Find Fair Value Gap (FVG) after ATL
    df_after = df.iloc[min_idx:]
    fvg = None
    for i in range(len(df_after) - 2):
        # Bullish FVG: Low of candle 3 is higher than High of candle 1
        if df_after['l'].iloc[i+2] > df_after['h'].iloc[i]:
            fvg = df_after['h'].iloc[i]
            break
    
    if not fvg: return None
    
    # Calculate Trade Parameters
    sl = atl_val - (atl_val * 0.001)
    target = lh2
    rr = round((target - fvg) / (fvg - sl), 2) if (fvg - sl) != 0 else 0
    
    return {"atl": atl_val, "lh1": lh1, "lh2": lh2, "fvg": fvg, "sl": sl, "target": target, "rr": rr}

# ==========================================
# 3. BACKGROUND WORKER
# ==========================================
def background_worker():
    """Independent thread that updates prices and runs SMC logic."""
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
                    # Fetch 250 candles of 5-min data (similar to main40.py)
                    hist_data = {"symbol": sym, "resolution": "5", "date_format": "1", 
                                 "range_from": (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d"),
                                 "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"}
                    
                    res = fyers.history(data=hist_data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        analysis = analyze_smc_logic(df)
                        
                        if analysis:
                            curr_ltp = df['c'].iloc[-1]
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                ltp=?, atl=?, lh1=?, lh2=?, fvg_low=?, target=?, sl=?, rr=?, status='PATTERN' 
                                WHERE symbol=?""", (curr_ltp, analysis['atl'], analysis['lh1'], analysis['lh2'], 
                                                    analysis['fvg'], analysis['target'], analysis['sl'], analysis['rr'], sym))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300) # Scan every 5 minutes

# ==========================================
# 4. STREAMLIT UI
# ==========================================
def main():
    st.set_page_config(page_title="SMC Trading Bot Cloud", layout="wide")
    conn = init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=background_worker, daemon=True).start()
        st.session_state['bg_active'] = True

    # SIDEBAR: FYERS LOGIN (KEEPING AS IS)
    st.sidebar.title("Bot Settings")
    token = ""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f: token = f.read().strip()

    if not token:
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.info("Please Login")
        st.sidebar.markdown(f"[Login to Fyers]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Auth Code:")
        if st.sidebar.button("Generate Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            if "access_token" in res:
                with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
                st.rerun()
    else:
        st.sidebar.success("Connected ✅")
        if st.sidebar.button("Fetch New Options", use_container_width=True):
            # Seed symbols logic here using optionchain API...
            st.toast("Fetching Option Chain...")

    # MAIN DASHBOARD
    tab1, tab2 = st.tabs(["📊 SMC Scanner", "📈 Active Positions"])
    
    with tab1:
        df_scan = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='PATTERN' ORDER BY rr DESC", conn)
        st.dataframe(df_scan, width=None)

    with tab2:
        df_active = pd.read_sql("SELECT * FROM active_trades", conn)
        st.dataframe(df_active, width=None)

    st_autorefresh(interval=60000, key="ui_refresh")
    conn.close()

if __name__ == "__main__": main()
