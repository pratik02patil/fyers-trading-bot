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
# 1. SETTINGS & SECRETS
# ==========================================
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_FILE = "trading_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Mirroring main40.py data structure exactly
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, lh2 REAL, 
                    fvg_low REAL, target REAL, sl REAL, rr REAL, 
                    atl_date TEXT, status TEXT, is_today INTEGER)''')
    conn.commit()
    return conn

# ==========================================
# 2. EXACT SMC LOGIC FROM MAIN40.PY
# ==========================================
def analyze_logic_fixed(df):
    """Exact logic from main40.py to find peaks and gaps."""
    if df.empty or len(df) < 100:  # Safety check: need enough candles
        return None
    
    # 1. Identify ATL (All-Time Low)
    atl_idx = df['l'].idxmin()
    atl_val = df.loc[atl_idx, 'l']
    
    # 2. Look for LH1 and LH2 peaks BEFORE the ATL
    # This is why we need 200+ candles: peaks usually happen much earlier
    df_before = df.iloc[:atl_idx]
    if len(df_before) < 15: return None
    
    peaks = []
    for i in range(2, len(df_before)-2):
        # A high higher than 2 candles on each side
        if df_before['h'].iloc[i] == df_before['h'].iloc[i-2:i+3].max():
            peaks.append(df_before['h'].iloc[i])
            
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2] # Last two peaks
    
    # 3. Find Fair Value Gap (FVG) AFTER ATL
    df_after = df.iloc[atl_idx:]
    fvg = None
    for i in range(len(df_after) - 2):
        if df_after.iloc[i+2]['l'] > df_after.iloc[i]['h']:
            fvg = df_after.iloc[i]['h']
            break
            
    if not fvg: return None
    
    # Logic Parameters
    sl = atl_val - (atl_val * 0.001)
    target = lh2
    rr = round((target - fvg) / (fvg - sl), 2) if (fvg - sl) != 0 else 0
    
    return {"atl": atl_val, "lh1": lh1, "lh2": lh2, "fvg": fvg, "sl": sl, "target": target, "rr": rr}

# ==========================================
# 3. FIXED BACKGROUND ENGINE (10-DAY FETCH)
# ==========================================
def background_scanner():
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                scanned_list = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)['symbol'].tolist()
                
                for sym in scanned_list:
                    # FIX: range_from set to 10 days ago to ensure 200+ candles (75 candles/day)
                    range_to = datetime.datetime.now().strftime("%Y-%m-%d")
                    range_from = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
                    
                    data = {
                        "symbol": sym, "resolution": "5", "date_format": "1", 
                        "range_from": range_from, "range_to": range_to, "cont_flag": "1"
                    }
                    
                    res = fyers.history(data=data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        analysis = analyze_logic_fixed(df)
                        
                        if analysis:
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                ltp=?, atl=?, lh1=?, lh2=?, fvg_low=?, target=?, sl=?, rr=?, status='PATTERN' 
                                WHERE symbol=?""", (df['c'].iloc[-1], analysis['atl'], analysis['lh1'], 
                                                    analysis['lh2'], analysis['fvg'], analysis['target'], 
                                                    analysis['sl'], analysis['rr'], sym))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300) # Scan every 5 mins

# ==========================================
# 4. STREAMLIT UI (TABS)
# ==========================================
def main():
    st.set_page_config(page_title="SMC Scanner Cloud", layout="wide")
    conn = init_db()
    
    if 'bg_running' not in st.session_state:
        threading.Thread(target=background_scanner, daemon=True).start()
        st.session_state['bg_running'] = True

    st.sidebar.title("Configuration")
    # ... (Keep your existing Login logic here) ...

    # Seed Button (Crucial to get symbols into the scanner)
    if st.sidebar.button("Seed Nifty Options"):
        token = open(TOKEN_FILE, "r").read().strip()
        fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
        oc = fyers.optionchain({"symbol": "NSE:NIFTY50-INDEX", "strikecount": 10})
        if oc.get('s') == 'ok':
            for opt in oc['data']['optionsChain']:
                conn.execute("INSERT OR IGNORE INTO scanned_symbols (symbol, status) VALUES (?, 'WATCHING')", (opt['symbol'],))
            conn.commit()
            st.toast("Symbols Added! Scanning will start...")

    # Exact Tab Structure as main40.py
    t1, t2, t3 = st.tabs(["📅 Today's Patterns", "🌎 Global Patterns", "🚀 Active Trades"])
    
    with t1:
        df_today = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='PATTERN'", conn)
        st.dataframe(df_today, width="stretch")

    st_autorefresh(interval=60000, key="ui_pulse")
    conn.close()

if __name__ == "__main__": main()
