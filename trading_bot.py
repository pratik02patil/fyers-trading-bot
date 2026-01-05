import streamlit as st
import pandas as pd
import sqlite3
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
DB_FILE = "trading_bot.db"

def init_db():
    """Initializes and repairs the database to ensure it has exactly 10 columns."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Check if the existing table matches the required schema
    try:
        # Testing for the newest column 'atl_time'
        c.execute("SELECT atl_time FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalError:
        # If column doesn't exist, drop and recreate for a clean 10-column start
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                        sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
    conn.commit()
    return conn

# --- LOGIC & FILTERS FROM MAIN40.PY ---
def analyze_logic_main40(df, sym):
    """Exact filters from main40.py: 30<ATL<250, Peaks, RR>4."""
    if df.empty or len(df) < 20: return None
    
    # 1. Find ATL & Price Range (Filter: 30 < ATL < 250)
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    if min_idx >= len(df) - 3: return None

    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)

    # 2. LH1 & LH2 Peaks (Filter: Historical Peak Check)
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
    # 1.5x Peak Fallback Logic
    for p in all_peaks[1:]:
        if p >= lh1 * 1.5:
            lh2 = p
            break
    if lh2 is None and len(all_peaks) > 1:
        lh2 = all_peaks[1]
    elif lh2 is None:
        return None 

    # 3. FVG & SL
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    
    if not fvg_entry: fvg_entry = atl_val * 1.05
    sl_val = round(atl_val - (atl_val * 0.02), 1)
    
    # 4. Final RR Check (Filter: RR > 4)
    if fvg_entry <= sl_val: return None
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return None

    return {
        "ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1),
        "sl": round(float(sl_val), 1), "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S")
    }

# --- SCANNER ENGINE ---
def run_scanner():
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                symbols = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)['symbol'].tolist()
                for sym in symbols:
                    r_to = datetime.datetime.now().strftime("%Y-%m-%d")
                    # 14-day lookback as per main40.py
                    r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                    
                    hist_data = {"symbol": sym, "resolution": "15", "date_format": "1", 
                                 "range_from": r_from, "range_to": r_to, "cont_flag": "1"}
                    
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
        time.sleep(300)

# --- STREAMLIT UI ---
def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    conn = init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_scanner, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Login & Controls")
    
    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                          redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Code:")
        if st.sidebar.button("Save Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success("Fyers API Active ✅")
        
        # High RR Seeding (15 Nifty + 15 Sensex ATM strikes)
        if st.sidebar.button("Fetch High RR Options (Nifty & Sensex)", width='stretch'):
            token = open(TOKEN_FILE, "r").read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            found_count = 0
            
            with st.spinner("Applying all filters..."):
                for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                    oc = fyers.optionchain({"symbol": idx, "strikecount": 7}) 
                    if oc.get('s') == 'ok':
                        for opt in oc['data']['optionsChain']:
                            sym = opt['symbol']
                            r_to = datetime.datetime.now().strftime("%Y-%m-%d")
                            r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                            hist = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": r_from, "range_to": r_to, "cont_flag": "1"})
                            
                            if hist.get('s') == 'ok':
                                df = pd.DataFrame(hist['candles'], columns=['t','o','h','l','c','v'])
                                data = analyze_logic_main40(df, sym)
                                # Only seed if RR > 4 and passes all main40.py price/peak filters
                                if data:
                                    conn.execute("""INSERT OR REPLACE INTO scanned_symbols 
                                        (symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time, status) 
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FOUND')""", 
                                        (sym, data['ltp'], data['atl'], data['lh1'], data['fvg'], 
                                         data['lh2'], data['sl'], data['rr'], data['atl_time']))
                                    found_count += 1
                conn.commit()
                st.toast(f"Found {found_count} matches!")

    tab1, tab2 = st.tabs(["📊 Live Patterns", "🚀 Active Trades"])
    with tab1:
        st.subheader("High RR Pattern Scanner")
        df = pd.read_sql("SELECT symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time FROM scanned_symbols WHERE status='FOUND' ORDER BY rr DESC", conn)
        st.dataframe(df, width='stretch')

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": 
    main()
