import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import os
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# ==========================================
# 1. CORE CONFIG & PERSISTENCE
# ==========================================
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_FILE = "trading_bot.db"

def init_db():
    """Initializes tables for persistent storage"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Table structure mirrors the columns found in main40.py trees
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, lh2 REAL, 
                    fvg_low REAL, target REAL, sl REAL, rr REAL, 
                    atl_date TEXT, status TEXT, is_today INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                    symbol TEXT PRIMARY KEY, entry REAL, ltp REAL, pnl REAL, status TEXT)''')
    conn.commit()
    return conn

# ==========================================
# 2. SMC ANALYSIS LOGIC (FROM MAIN40.PY)
# ==========================================
def analyze_logic_main40(df):
    """
    Exact pattern detection from main40.py:
    1. Identifies Global/Daily ATL.
    2. Finds LH1 & LH2 before ATL.
    3. Locates first Fair Value Gap (FVG) after ATL.
    """
    if df.empty or len(df) < 15: return None
    
    # Identify All-Time Low (ATL)
    atl_idx = df['l'].idxmin()
    atl_val = df.loc[atl_idx, 'l']
    
    # Identify LH1 and LH2 peaks before the ATL
    df_before = df.iloc[:atl_idx]
    if len(df_before) < 10: return None
    
    # Peak logic: High must be higher than 2 surrounding candles
    peaks = []
    for i in range(2, len(df_before)-2):
        if df_before['h'].iloc[i] == df_before['h'].iloc[i-2:i+3].max():
            peaks.append(df_before['h'].iloc[i])
            
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2] # Last two peaks
    
    # Find Fair Value Gap (FVG) after ATL
    df_after = df.iloc[atl_idx:]
    fvg = None
    for i in range(len(df_after) - 2):
        # Bullish Gap: Low[3] > High[1]
        if df_after['l'].iloc[i+2] > df_after['h'].iloc[i]:
            fvg = df_after['h'].iloc[i]
            break
            
    if not fvg: return None
    
    # Calculation
    sl = atl_val - (atl_val * 0.001)
    target = lh2
    rr = round((target - fvg) / (fvg - sl), 2) if (fvg - sl) != 0 else 0
    
    return {"atl": atl_val, "lh1": lh1, "lh2": lh2, "fvg": fvg, "sl": sl, "target": target, "rr": rr}

# ==========================================
# 3. BACKGROUND UPDATER (ENGINE)
# ==========================================
def engine_loop():
    """Independent background scanner"""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                scanned = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)
                for sym in scanned['symbol']:
                    # Must fetch 150+ candles for peak detection to work
                    data = {"symbol": sym, "resolution": "5", "date_format": "1", 
                            "range_from": (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
                            "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"}
                    
                    res = fyers.history(data=data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        analysis = analyze_logic_main40(df)
                        
                        if analysis:
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                ltp=?, atl=?, lh1=?, lh2=?, fvg_low=?, target=?, sl=?, rr=?, status='PATTERN' 
                                WHERE symbol=?""", (df['c'].iloc[-1], analysis['atl'], analysis['lh1'], 
                                                    analysis['lh2'], analysis['fvg'], analysis['target'], 
                                                    analysis['sl'], analysis['rr'], sym))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300)

# ==========================================
# 4. TABBED USER INTERFACE
# ==========================================
def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    conn = init_db()
    
    if 'thread_started' not in st.session_state:
        threading.Thread(target=engine_loop, daemon=True).start()
        st.session_state['thread_started'] = True

    # SIDEBAR: FYERS LOGIN (As Requested)
    st.sidebar.title("Configuration")
    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Login to Fyers]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Auth Code:")
        if st.sidebar.button("Generate Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success("Fyers Connected")

    # DASHBOARD TABS (MIRRORS MAIN40.PY TREES)
    tab_today, tab_global, tab_active, tab_history = st.tabs(["📅 Today's Scanner", "🌎 Global Patterns", "🚀 Active Trades", "📜 Trade History"])
    
    with tab_today:
        st.subheader("Intraday Patterns")
        df = pd.read_sql("SELECT * FROM scanned_symbols WHERE is_today=1 AND status='PATTERN'", conn)
        st.dataframe(df, width="stretch") # Fix for width error

    with tab_global:
        st.subheader("Multi-Day Patterns")
        df = pd.read_sql("SELECT * FROM scanned_symbols WHERE is_today=0 AND status='PATTERN'", conn)
        st.dataframe(df, width="stretch")

    with tab_active:
        st.subheader("Open Positions")
        df = pd.read_sql("SELECT * FROM active_trades", conn)
        st.dataframe(df, width="stretch")

    with tab_history:
        st.subheader("Completed Trades")
        # History logic from main40.py
        pass

    st_autorefresh(interval=30000, key="ui_refresh")
    conn.close()

if __name__ == "__main__": main()
