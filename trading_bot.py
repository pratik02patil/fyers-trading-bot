import streamlit as st
import pandas as pd
import psycopg2 
from psycopg2.extras import execute_values
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
DB_URI = st.secrets["postgres"]["uri"] 

def init_db():
    try:
        conn = psycopg2.connect(DB_URI)
        with conn.cursor() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                            symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                            sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                            symbol TEXT PRIMARY KEY, entry REAL, sl REAL, target REAL, 
                            qty INTEGER, mode TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                            symbol TEXT, entry REAL, exit REAL, result TEXT, pnl REAL, time TEXT)''')
        conn.commit()
        return conn
    except Exception as e:
        st.error(f"Database Connection Error: {e}")
        st.stop()

def get_lot_size(symbol):
    if "NIFTY" in symbol.upper(): return 65
    if "SENSEX" in symbol.upper(): return 20
    return 1

# --- CORE LOGIC (UNCHANGED) ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
    pre_atl = df.iloc[max(0, min_idx - 300):min_idx].reset_index(drop=True)
    if len(pre_atl) < 5: return None
    all_peaks = [pre_atl['h'].iloc[i] for i in range(len(pre_atl)-2, 1, -1) 
                 if pre_atl['h'].iloc[i] > pre_atl['h'].iloc[i-1] and pre_atl['h'].iloc[i] > pre_atl['h'].iloc[i+1]]
    if not all_peaks: return None
    lh1, lh2 = all_peaks[0], (all_peaks[1] if len(all_peaks) > 1 else all_peaks[0])
    
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    fvg_entry = fvg_entry or (atl_val * 1.05)
    sl_val = round(atl_val * 0.98, 1)
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return None
    return {"ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), "lh1": round(float(lh1), 1), 
            "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1), "sl": round(float(sl_val), 1), 
            "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S")}

def run_background_engine():
    while True:
        try:
            with psycopg2.connect(DB_URI) as conn:
                if os.path.exists(TOKEN_FILE):
                    token = open(TOKEN_FILE).read().strip()
                    fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
                    with conn.cursor() as cur:
                        cur.execute("SELECT symbol FROM scanned_symbols")
                        for (sym,) in cur.fetchall():
                            res = fyers.quotes({"symbols": sym})
                            if res.get('s') == 'ok':
                                cur.execute("UPDATE scanned_symbols SET ltp=%s WHERE symbol=%s", (res['d'][0]['v']['lp'], sym))
                conn.commit()
        except: pass
        time.sleep(15)

def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_background_engine, daemon=True).start()
        st.session_state['bg_active'] = True

    st.sidebar.title("Fyers Login & Controls")
    trade_mode = st.sidebar.radio("Trade Mode", ["Virtual", "Real Account"])

    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
        fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
        if trade_mode == "Real Account":
            funds = fyers.funds()
            if funds.get('s') == 'ok':
                balance = next((x['fifo_margin'] for x in funds['fund_limit'] if x['id'] == 10), 0.0)
                st.sidebar.metric("Real Balance", f"₹{balance:,.2f}")
        else:
            st.sidebar.metric("Virtual Balance", "₹1,00,000.00")

    if not os.path.exists(TOKEN_FILE):
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.markdown(f"[Authorize App]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Enter Authorization Code:")
        if st.sidebar.button("Save Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
            st.rerun()
    else:
        st.sidebar.success(f"Fyers Active ✅")
        if st.sidebar.button("Fetch High RR Options", width='stretch'):
            token = open(TOKEN_FILE).read().strip()
            fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)
            
            # UPDATED: Loop through Nifty and Sensex for multiple expiries
            for idx in ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]:
                # Fetching the option chain to get all expiry dates first
                oc_info = fyers.optionchain({"symbol": idx, "strikecount": 1})
                if oc_info.get('s') == 'ok':
                    # Get the list of all expiries and take the first 3
                    all_expiries = oc_info['data']['expiryData']
                    target_expiries = [e['expiry'] for e in all_expiries[:3]]
                    
                    for exp_date in target_expiries:
                        oc = fyers.optionchain({"symbol": idx, "strikecount": 7, "expiry": exp_date})
                        if oc.get('s') == 'ok':
                            for opt in oc['data']['optionsChain']:
                                sym = opt['symbol']
                                hist = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d"), "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"})
                                if hist.get('s') == 'ok':
                                    df = pd.DataFrame(hist['candles'], columns=['t','o','h','l','c','v'])
                                    data = analyze_logic_main40(df, sym)
                                    if data:
                                        with psycopg2.connect(DB_URI) as conn:
                                            with conn.cursor() as cur:
                                                cur.execute("""INSERT INTO scanned_symbols (symbol, ltp, atl, lh1, fvg, lh2, sl, rr, atl_time, status) 
                                                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'FOUND') 
                                                               ON CONFLICT (symbol) DO UPDATE SET ltp=EXCLUDED.ltp, rr=EXCLUDED.rr""", 
                                                            (sym, data['ltp'], data['atl'], data['lh1'], data['fvg'], data['lh2'], data['sl'], data['rr'], data['atl_time']))
                                            conn.commit()

    tab1, tab_watchlist, tab2, tab_history = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades", "📜 History"])
    
    with psycopg2.connect(DB_URI) as conn:
        with tab1:
            st.subheader("All Scanned Patterns (3 Expiries)")
            st.dataframe(pd.read_sql("SELECT * FROM scanned_symbols WHERE status='FOUND' ORDER BY rr DESC", conn), use_container_width=True)

        with tab_watchlist:
            st.subheader("LH1 Break & Retracing into FVG")
            full_df = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='FOUND'", conn)
            valid = full_df[(full_df['ltp'] >= full_df['lh1']) & (full_df['ltp'] <= (full_df['fvg'] * 1.01)) & (full_df['ltp'] >= full_df['sl'])]
            st.dataframe(valid, use_container_width=True)

        with tab2:
            st.subheader("Currently Monitored Trades")
            st.dataframe(pd.read_sql("SELECT * FROM active_trades", conn), use_container_width=True)

        with tab_history:
            st.subheader("Completed Trade Performance")
            st.dataframe(pd.read_sql("SELECT * FROM trade_history ORDER BY time DESC", conn), use_container_width=True)

    st_autorefresh(interval=10000, key="ui_refresh")

if __name__ == "__main__": main()
