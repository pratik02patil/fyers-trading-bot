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
        c.execute("SELECT ATL_Time FROM scanned_symbols LIMIT 1")
    except sqlite3.OperationalERRor:
        c.execute("DROP TABLE IF EXISTS scanned_symbols")
        c.execute('''CREATE TABLE scanned_symbols (
                        symbol TEXT PRIMARY KEY, LTP REAL, atl REAL, lh1 REAL, FVG REAL, LH2 REAL, 
                        SL REAL, RR REAL, ATL_Time TEXT, status TEXT, lh1_broken INTEGER DEFAULT 0)''')
    conn.commit()
    return conn

# --- REFINED LOGIC TO TRACK LH1 BREAK ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    
    # Peak Detection (LH1 & LH2)
    search_start = max(0, min_idx - 300)
    pre_atl = df.iloc[search_start:min_idx].reset_index(drop=True)
    all_peaks = []
    for i in range(len(pre_atl) - 2, 1, -1):
        cuRR_h = pre_atl['h'].iloc[i]
        if cuRR_h > pre_atl['h'].iloc[i-1] and cuRR_h > pre_atl['h'].iloc[i+1]:
            all_peaks.append(cuRR_h)
            
    if not all_peaks: return None
    lh1 = all_peaks[0] 
    LH2 = all_peaks[1] if len(all_peaks) > 1 else lh1 * 2
    
    # FVG Calculation
    FVG_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            FVG_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    if not FVG_entry: FVG_entry = atl_val * 1.05
    
    SL_val = round(atl_val - (atl_val * 0.02), 1)
    RR = round((LH2 - FVG_entry)/(FVG_entry - SL_val), 2)
    if RR <= 4: return None

    # NEW: Check if LH1 was broken at any point after ATL
    lh1_broken = 1 if post_atl_data['h'].max() > lh1 else 0

    return {
        "LTP": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "FVG": round(float(FVG_entry), 1), "LH2": round(float(LH2), 1),
        "SL": round(float(SL_val), 1), "RR": round(float(RR), 1), "ATL_Time": atl_ts.strftime("%H:%M:%S"),
        "lh1_broken": lh1_broken
    }

# --- BACKGROUND SCANNER ---
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
                            worker_conn.execute("""UPDATE scanned_symbols SET 
                                LTP=?, atl=?, lh1=?, FVG=?, LH2=?, SL=?, RR=?, ATL_Time=?, lh1_broken=?, status='FOUND' 
                                WHERE symbol=?""", (data['LTP'], data['atl'], data['lh1'], data['FVG'], 
                                                    data['LH2'], data['SL'], data['RR'], data['ATL_Time'], 
                                                    data['lh1_broken'], sym))
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

    # Sidebar Login logic remains same...
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
                        conn.execute("INSERT OR IGNORE INTO scanned_symbols (symbol, status) VALUES (?, 'WATCHING')", (opt['symbol'],))
            conn.commit()

    # --- TABS: ALIGNED WITH YOUR TRADING FLOW ---
    tab1, tab_watchlist, tab2 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades"])
    
    with tab1:
        st.subheader("All Scanned Patterns")
        full_df = pd.read_sql("SELECT symbol, LTP, atl, lh1, FVG, LH2, SL, RR, ATL_Time, lh1_broken FROM scanned_symbols WHERE status='FOUND' ORDER BY RR DESC", conn)
        st.dataframe(full_df, width='stretch')

    with tab_watchlist:
        st.subheader("Waiting for Retracement (LH1 Broken, LTP > FVG)")
        # 26400 PE Logic: LH1 is broken, but LTP is still higher than FVG (Waiting for dip)
        watchlist_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['LTP'] > full_df['FVG'])]
        st.dataframe(watchlist_df, width='stretch')

    with tab2:
        st.subheader("Active Trades (Retracement Complete, LTP near/at FVG)")
        # 26300 PE Logic: LH1 is broken, and LTP has retraced to FVG level
        active_df = full_df[(full_df['lh1_broken'] == 1) & (full_df['LTP'] <= (full_df['FVG'] * 1.02)) & (full_df['LTP'] >= full_df['SL'])]
        st.dataframe(active_df, width='stretch')

    st_autorefresh(interval=60000, key="bot_refresh")
    conn.close()

if __name__ == "__main__": main()
