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
# 1. AUTH & DB INITIALIZATION
# ==========================================
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_FILE = "trading_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Mirroring main40.py columns: ATL, LH1, LH2, FVG
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, lh2 REAL, 
                    fvg_low REAL, target REAL, sl REAL, rr REAL, 
                    status TEXT, is_today INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

# ==========================================
# 2. SMC LOGIC PORTED FROM MAIN40.PY
# ==========================================
def analyze_smc_main40(df):
    if df.empty or len(df) < 20: return None
    
    # 1. Find Global ATL (All-Time Low)
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    
    # Logic from main40.py: ATL must be in a realistic trade range
    if not (10 < atl_val < 600): return None
    if min_idx >= len(df) - 3: return None
    
    # 2. Identify LH1 & LH2 peaks BEFORE the ATL
    df_before = df.iloc[:min_idx]
    if len(df_before) < 10: return None
    
    peaks = []
    for i in range(2, len(df_before)-2):
        # A high higher than 2 candles on each side
        if df_before['h'].iloc[i] == df_before['h'].iloc[i-2:i+3].max():
            peaks.append(df_before['h'].iloc[i])
            
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2]
    
    # 3. Find Fair Value Gap (FVG) after ATL
    df_after = df.iloc[min_idx:]
    fvg = None
    for i in range(len(df_after) - 2):
        if df_after['l'].iloc[i+2] > df_after['h'].iloc[i]:
            fvg = df_after['h'].iloc[i]
            break
    
    if not fvg: return None
    
    # SL/Target calculation based on main40 peaks
    sl = atl_val - (atl_val * 0.001)
    target = lh2
    rr = round((target - fvg) / (fvg - sl), 2) if (fvg - sl) != 0 else 0
    
    return {"atl": atl_val, "lh1": lh1, "lh2": lh2, "fvg": fvg, "sl": sl, "target": target, "rr": rr}

# ==========================================
# 3. BACKGROUND ENGINE
# ==========================================
def run_scanner():
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                # Fetch symbols from DB to scan
                symbols_to_scan = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)['symbol'].tolist()
                
                for sym in symbols_to_scan:
                    # History fetch (5-min, 10-day lookback for peaks)
                    hist_data = {"symbol": sym, "resolution": "5", "date_format": "1", 
                                 "range_from": (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
                                 "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"}
                    
                    res = fyers.history(data=hist_data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        analysis = analyze_smc_main40(df)
                        
                        if analysis:
                            # Update with pattern data
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
# 4. TABBED UI
# ==========================================
def main():
    st.set_page_config(page_title="SMC Pro Scanner", layout="wide")
    conn = init_db()
    
    if 'bg_task' not in st.session_state:
        threading.Thread(target=run_scanner, daemon=True).start()
        st.session_state['bg_task'] = True

    # Login Logic
    st.sidebar.title("Fyers Cloud Connect")
    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                          redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Login to Fyers]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Code:")
        if st.sidebar.button("Authorize"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success("Fyers API Active")
        if st.sidebar.button("Seed Nifty Options"):
            # This logic adds symbols to the DB so the scanner has data to work with
            token = open(TOKEN_FILE, "r").read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            oc_res = fyers.optionchain({"symbol": "NSE:NIFTY50-INDEX", "strikecount": 10})
            if oc_res.get('s') == 'ok':
                for opt in oc_res['data']['optionsChain']:
                    conn.execute("INSERT OR IGNORE INTO scanned_symbols (symbol, status) VALUES (?, 'WATCHING')", (opt['symbol'],))
                conn.commit()
                st.toast("Seeded 20 Options!")

    # Tabs mirroring main40.py
    tab1, tab2, tab3 = st.tabs(["🌎 Global Patterns", "📅 Today's Scanner", "🚀 Active Trades"])
    
    with tab1:
        df = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='PATTERN'", conn)
        st.dataframe(df, width="stretch") # Fixed Width

    st_autorefresh(interval=60000, key="refresh")
    conn.close()

if __name__ == "__main__": main()
