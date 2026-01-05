import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import os
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# --- 1. SETTINGS & SECRETS ---
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
DB_FILE = "trading_bot.db"

def init_db():
    """Initializes a local database. NOTE: On Streamlit Cloud, this resets on reboot."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Ensuring all columns from main40.py are present plus the new atl_time
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                    sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
    conn.commit()
    return conn

# --- 2. SMC LOGIC (STRICTLY FROM MAIN40.PY) ---
def analyze_logic_main40(df, sym):
    """Detects SMC patterns using 200+ candles of history"""
    if df.empty or len(df) < 20: return None
    
    # Identify ATL and specific formation time
    min_idx = df['l'].idxmin()
    atl_val = df['l'].iloc[min_idx]
    # Exact IST timestamp when the ATL candle formed
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    
    # Peak detection before ATL
    pre_atl = df.iloc[:min_idx]
    if len(pre_atl) < 10: return None
    
    peaks = []
    for i in range(2, len(pre_atl)-2):
        if pre_atl['h'].iloc[i] == pre_atl['h'].iloc[i-2:i+3].max():
            peaks.append(pre_atl['h'].iloc[i])
    if len(peaks) < 2: return None
    lh1, lh2 = peaks[-1], peaks[-2]
    
    # FVG detection after ATL
    post_atl = df.iloc[min_idx:]
    fvg = None
    for i in range(len(post_atl)-2):
        if post_atl['l'].iloc[i+2] > post_atl['h'].iloc[i]:
            fvg = post_atl['h'].iloc[i]
            break
    if not fvg: return None
    
    sl = atl_val - (atl_val * 0.001)
    rr = (lh2 - fvg) / (fvg - sl) if (fvg - sl) != 0 else 0
    
    # Formatting to 1 decimal place as requested
    return {
        "ltp": round(df['c'].iloc[-1], 1), "atl": round(atl_val, 1), 
        "lh1": round(lh1, 1), "fvg": round(fvg, 1), "lh2": round(lh2, 1),
        "sl": round(sl, 1), "rr": round(rr, 1), "atl_time": atl_ts.strftime("%H:%M:%S")
    }

# --- 3. BACKGROUND SCANNER ---
def run_scanner():
    """Continuous background scanning thread."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                # Fetch symbols that need scanning
                symbols = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)['symbol'].tolist()
                for sym in symbols:
                    # 10-day history for 200+ candles
                    hist_data = {"symbol": sym, "resolution": "5", "date_format": "1", 
                                 "range_from": (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
                                 "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"}
                    res = fyers.history(data=hist_data)
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        data = analyze_logic_main40(df, sym)
                        if data:
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                ltp=?, atl=?, lh1=?, fvg=?, lh2=?, sl=?, rr=?, atl_time=?, status='FOUND' 
                                WHERE symbol=?""", (data['ltp'], data['atl'], data['lh1'], data['fvg'], 
                                                    data['lh2'], data['sl'], data['rr'], data['atl_time'], sym))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(300) # Re-scan every 5 mins

# --- 4. MAIN INTERFACE ---
def main():
    st.set_page_config(page_title="SMC Cloud Bot", layout="wide")
    conn = init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_scanner, daemon=True).start()
        st.session_state['bg_active'] = True

    # SIDEBAR: PERSISTENT CONTROLS
    st.sidebar.title("Login & Controls")
    
    # Handling persistent token login
    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                          redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter code from URL:")
        if st.sidebar.button("Save Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success("Fyers API Active ✅")
        # SEEDING: Nifty & Sensex ATM +- 7 strikes (approx 30 total)
        if st.sidebar.button("Fetch ATM Options (Nifty & Sensex)"):
            token = open(TOKEN_FILE, "r").read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                oc = fyers.optionchain({"symbol": idx, "strikecount": 7}) 
                if oc.get('s') == 'ok':
                    for opt in oc['data']['optionsChain']:
                        conn.execute("INSERT OR IGNORE INTO scanned_symbols (symbol, status) VALUES (?, 'WATCHING')", (opt['symbol'],))
            conn.commit()
            st.toast("Seeded 30 ATM Options!")

    # TABBED DISPLAY (Matching main40.py layout)
    tab1, tab2 = st.tabs(["📊 Live Patterns", "🚀 Active Trades"])
    
    with tab1:
        st.subheader("Patterns Found")
        # Data rounded to 1 decimal place with ATL formation time
        df = pd.read_sql("SELECT symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time FROM scanned_symbols WHERE status='FOUND'", conn)
        st.dataframe(df, use_container_width=True)

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()
