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
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    try:
        c.execute("SELECT ATL_time FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalERRor:
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        symbol TEXT PRIMARY KEY, LTP REAL, ATL REAL, LH1 REAL, FVG REAL, LH2 REAL, 
                        SL REAL, RR REAL, ATL_time TEXT, status TEXT)''')
    conn.commit()
    return conn

# --- LOGIC RETAINED FROM PREVIOUS ITERATION ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    min_idx = df['l'].idxmin()
    ATL_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < ATL_val < 250): return None
    if min_idx >= len(df) - 3: return None
    ATL_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    search_start = max(0, min_idx - 300)
    pre_ATL = df.iloc[search_start:min_idx].reset_index(drop=True)
    if len(pre_ATL) < 5: return None
    all_peaks = []
    for i in range(len(pre_ATL) - 2, 1, -1):
        cuRR_h = pre_ATL['h'].iloc[i]
        if cuRR_h > pre_ATL['h'].iloc[i-1] and cuRR_h > pre_ATL['h'].iloc[i+1]:
            all_peaks.append(cuRR_h)
    if not all_peaks: return None
    LH1 = all_peaks[0] 
    LH2 = None
    for p in all_peaks[1:]:
        if p >= LH1 * 1.5:
            LH2 = p
            break
    if LH2 is None and len(all_peaks) > 1: LH2 = all_peaks[1]
    elif LH2 is None: return None 
    FVG_entry = None
    post_ATL_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_ATL_data)-2):
        if post_ATL_data['l'].iloc[i+2] > post_ATL_data['h'].iloc[i]:
            FVG_entry = (post_ATL_data['l'].iloc[i+2] + post_ATL_data['h'].iloc[i]) / 2
            break
    if not FVG_entry: FVG_entry = ATL_val * 1.05
    SL_val = round(ATL_val - (ATL_val * 0.02), 1)
    if FVG_entry <= SL_val: return None
    RR = round((LH2 - FVG_entry)/(FVG_entry - SL_val), 2)
    if RR <= 4: return None
    return {
        "LTP": round(float(df['c'].iloc[-1]), 1), "ATL": round(float(ATL_val), 1), 
        "LH1": round(float(LH1), 1), "FVG": round(float(FVG_entry), 1), "LH2": round(float(LH2), 1),
        "SL": round(float(SL_val), 1), "RR": round(float(RR), 1), "ATL_time": ATL_ts.strftime("%H:%M:%S")
    }

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
                    r_from = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                    res = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": r_from, "range_to": r_to, "cont_flag": "1"})
                    if res.get('s') == 'ok':
                        df = pd.DataFrame(res['candles'], columns=['t','o','h','l','c','v'])
                        data = analyze_logic_main40(df, sym)
                        if data:
                            worker_conn.execute("""UPDATE scanned_symbols SET LTP=?, ATL=?, LH1=?, FVG=?, LH2=?, SL=?, RR=?, ATL_time=?, status='FOUND' WHERE symbol=?""", (data['LTP'], data['ATL'], data['LH1'], data['FVG'], data['LH2'], data['SL'], data['RR'], data['ATL_time'], sym))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.SLeep(300)

# --- UI INTERFACE ---
def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    conn = init_db()
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_scanner, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Login & Controls")
    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Code:")
        if st.sidebar.button("Save Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success("Fyers API Active ✅")
        if st.sidebar.button("Fetch High RR Options", width='stretch'):
            token = open(TOKEN_FILE, "r").read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                oc = fyers.optionchain({"symbol": idx, "strikecount": 7}) 
                if oc.get('s') == 'ok':
                    for opt in oc['data']['optionsChain']:
                        sym = opt['symbol']
                        hist = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d"), "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"})
                        if hist.get('s') == 'ok':
                            df = pd.DataFrame(hist['candles'], columns=['t','o','h','l','c','v'])
                            data = analyze_logic_main40(df, sym)
                            if data:
                                conn.execute("INSERT OR REPLACE INTO scanned_symbols (symbol, LTP, ATL, LH1, FVG, LH2, SL, RR, ATL_time, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FOUND')", (sym, data['LTP'], data['ATL'], data['LH1'], data['FVG'], data['LH2'], data['SL'], data['RR'], data['ATL_time']))
            conn.commit()

    # --- UPDATED TABS ---
    tab1, tab_watchlist, tab2 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades"])
    
    # 1. LIVE PATTERNS TAB
    with tab1:
        st.subheader("All Scanned Patterns")
        full_df = pd.read_sql("SELECT symbol, LTP, ATL, LH1, FVG, LH2, SL, RR, ATL_time FROM scanned_symbols WHERE status='FOUND' ORDER BY RR DESC", conn)
        st.dataframe(full_df, width='stretch')

    # 2. WATCHLIST TAB (LH1 Break + FVG Retracement)
    with tab_watchlist:
        st.subheader("LH1 Break & Retracing into FVG")
        # Filters: LTP > LH1 (Breakout) and LTP is near FVG (Retracement)
        # We check if LTP is between SL and FVG + a small buffer for the retracement entry
        watchlist_df = full_df[
            (full_df['LTP'] >= full_df['LH1']) & 
            (full_df['LTP'] <= (full_df['FVG'] * 1.01)) & 
            (full_df['LTP'] >= full_df['SL'])
        ]
        if watchlist_df.empty:
            st.info("No symbols cuRRently breaking LH1 and retracing to FVG.")
        else:
            st.dataframe(watchlist_df, width='stretch')

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()

