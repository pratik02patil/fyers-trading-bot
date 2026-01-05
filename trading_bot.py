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
    # Scanned symbols table
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, fvg REAL, lh2 REAL, 
                    sl REAL, rr REAL, atl_time TEXT, status TEXT)''')
    # Active trades table
    c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                    symbol TEXT PRIMARY KEY, entry REAL, sl REAL, target REAL, 
                    qty INTEGER, mode TEXT)''')
    # History table
    c.execute('''CREATE TABLE IF NOT EXISTS trade_history (
                    symbol TEXT, entry REAL, exit REAL, result TEXT, pnl REAL, time TEXT)''')
    conn.commit()
    return conn

def get_lot_size(symbol):
    if "NIFTY" in symbol.upper():
        return 65
    elif "SENSEX" in symbol.upper():
        return 20
    return 1

# --- LOGIC ---
def analyze_logic_main40(df, sym):
    if df.empty or len(df) < 20: return None
    min_idx = df['l'].idxmin()
    atl_val = round(df['l'].iloc[min_idx], 2)
    if not (30 < atl_val < 250): return None
    if min_idx >= len(df) - 3: return None
    atl_ts = pd.to_datetime(df['t'].iloc[min_idx], unit='s') + datetime.timedelta(hours=5, minutes=30)
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
    for p in all_peaks[1:]:
        if p >= lh1 * 1.5:
            lh2 = p
            break
    if lh2 is None and len(all_peaks) > 1: lh2 = all_peaks[1]
    elif lh2 is None: return None 
    fvg_entry = None
    post_atl_data = df.iloc[min_idx:].reset_index(drop=True)
    for i in range(len(post_atl_data)-2):
        if post_atl_data['l'].iloc[i+2] > post_atl_data['h'].iloc[i]:
            fvg_entry = (post_atl_data['l'].iloc[i+2] + post_atl_data['h'].iloc[i]) / 2
            break
    if not fvg_entry: fvg_entry = atl_val * 1.05
    sl_val = round(atl_val - (atl_val * 0.02), 1)
    if fvg_entry <= sl_val: return None
    rr = round((lh2 - fvg_entry)/(fvg_entry - sl_val), 2)
    if rr <= 4: return None
    return {
        "ltp": round(float(df['c'].iloc[-1]), 1), "atl": round(float(atl_val), 1), 
        "lh1": round(float(lh1), 1), "fvg": round(float(fvg_entry), 1), "lh2": round(float(lh2), 1),
        "sl": round(float(sl_val), 1), "rr": round(float(rr), 1), "atl_time": atl_ts.strftime("%H:%M:%S")
    }

def run_background_engine():
    """Background thread to update LTP, check Watchlist entries, and manage Active Trades."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "r") as f: token = f.read().strip()
                fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token, is_async=False)
                
                # 1. Update Scanned Symbols LTP
                scanned = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)
                for sym in scanned['symbol']:
                    res = fyers.quotes({"symbols": sym})
                    if res.get('s') == 'ok':
                        ltp = res['d'][0]['v']['lp']
                        worker_conn.execute("UPDATE scanned_symbols SET ltp=? WHERE symbol=?", (ltp, sym))
                
                # 2. Check Active Trades for Target/SL
                active = pd.read_sql("SELECT * FROM active_trades", worker_conn)
                for _, trade in active.iterrows():
                    res = fyers.quotes({"symbols": trade['symbol']})
                    if res.get('s') == 'ok':
                        curr_ltp = res['d'][0]['v']['lp']
                        result = None
                        exit_px = 0
                        
                        if curr_ltp >= trade['target']:
                            result, exit_px = "TARGET", trade['target']
                        elif curr_ltp <= trade['sl']:
                            result, exit_px = "SL", trade['sl']
                        
                        if result:
                            pnl = (exit_px - trade['entry']) * trade['qty']
                            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            worker_conn.execute("INSERT INTO trade_history VALUES (?,?,?,?,?,?)", 
                                             (trade['symbol'], trade['entry'], exit_px, result, pnl, ts))
                            worker_conn.execute("DELETE FROM active_trades WHERE symbol=?", (trade['symbol'],))
                
                worker_conn.commit()
            worker_conn.close()
        except Exception as e:
            print(f"Engine Error: {e}")
        time.sleep(10) # Fast refresh for price monitoring

# --- UI INTERFACE ---
def main():
    st.set_page_config(page_title="SMC Pro Bot", layout="wide")
    conn = init_db()
    
    if 'bg_active' not in st.session_state:
        threading.Thread(target=run_background_engine, daemon=True).start()
        st.session_state['bg_active'] = True

    # --- SIDEBAR ---
    st.sidebar.title("Login & Controls")
    trade_mode = st.sidebar.radio("Trade Mode", ["Virtual", "Real Account"])
    
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
        st.sidebar.success(f"Fyers Active ({trade_mode}) ✅")
        token = open(TOKEN_FILE, "r").read().strip()
        fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=token)

        # Capital Logic
        if trade_mode == "Virtual":
            capital = 100000.0
        else:
            funds = fyers.funds()
            capital = funds['fund_limit'][0]['equityAmount'] if funds.get('s') == 'ok' else 0.0
        
        st.sidebar.metric("Available Capital", f"₹{capital:,.2f}")

        if st.sidebar.button("Scan Market", width='stretch'):
            for idx in ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]:
                oc = fyers.optionchain({"symbol": idx, "strikecount": 5}) 
                if oc.get('s') == 'ok':
                    for opt in oc['data']['optionsChain']:
                        sym = opt['symbol']
                        hist = fyers.history({"symbol": sym, "resolution": "15", "date_format": "1", "range_from": (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d"), "range_to": datetime.datetime.now().strftime("%Y-%m-%d"), "cont_flag": "1"})
                        if hist.get('s') == 'ok':
                            df = pd.DataFrame(hist['candles'], columns=['t','o','h','l','c','v'])
                            data = analyze_logic_main40(df, sym)
                            if data:
                                conn.execute("INSERT OR REPLACE INTO scanned_symbols VALUES (?,?,?,?,?,?,?,?,?,?)", 
                                           (sym, data['ltp'], data['atl'], data['lh1'], data['fvg'], data['lh2'], data['sl'], data['rr'], data['atl_time'], 'FOUND'))
            conn.commit()

    # --- TABS ---
    t1, t2, t3, t4 = st.tabs(["📊 Live Patterns", "🔭 Watchlist", "🚀 Active Trades", "📜 History"])
    
    with t1:
        df_all = pd.read_sql("SELECT * FROM scanned_symbols", conn)
        st.dataframe(df_all, use_container_width=True)

    with t2:
        st.subheader("Automated Execution (FVG Entry)")
        watchlist = pd.read_sql("SELECT * FROM scanned_symbols WHERE status='FOUND'", conn)
        
        # Filtering logic for Watchlist
        valid_watchlist = watchlist[
            (watchlist['ltp'] >= watchlist['lh1']) & 
            (watchlist['ltp'] <= (watchlist['fvg'] * 1.01)) & 
            (watchlist['ltp'] >= watchlist['sl'])
        ]

        if not valid_watchlist.empty:
            st.write("The following symbols hit FVG and will be traded:")
            for _, row in valid_watchlist.iterrows():
                # Check if already in active trades
                exists = conn.execute("SELECT 1 FROM active_trades WHERE symbol=?", (row['symbol'],)).fetchone()
                if not exists:
                    lot_size = get_lot_size(row['symbol'])
                    qty = int((capital // row['ltp']) // lot_size) * lot_size
                    
                    if qty > 0:
                        conn.execute("INSERT INTO active_trades VALUES (?,?,?,?,?,?)",
                                   (row['symbol'], row['ltp'], row['sl'], row['lh2'], qty, trade_mode))
                        conn.commit()
                        st.success(f"Executed {trade_mode} trade for {row['symbol']} @ {row['ltp']}")
            st.dataframe(valid_watchlist, use_container_width=True)
        else:
            st.info("Searching for LH1 Break + FVG Retracement...")

    with t3:
        st.subheader("Live P&L Tracking")
        active_df = pd.read_sql("SELECT * FROM active_trades", conn)
        if not active_df.empty:
            # Join with scanned_symbols to get current LTP
            display_active = []
            for _, r in active_df.iterrows():
                ltp_row = conn.execute("SELECT ltp FROM scanned_symbols WHERE symbol=?", (r['symbol'],)).fetchone()
                ltp = ltp_row[0] if ltp_row else r['entry']
                pnl = round((ltp - r['entry']) * r['qty'], 2)
                display_active.append({
                    "Symbol": r['symbol'], "LTP": ltp, "Entry": r['entry'], 
                    "SL": r['sl'], "Target": r['target'], "Qty": r['qty'], "P&L": pnl
                })
            st.table(display_active)
        else:
            st.write("No active positions.")

    with t4:
        st.subheader("Completed Trades")
        history_df = pd.read_sql("SELECT * FROM trade_history ORDER BY time DESC", conn)
        st.dataframe(history_df, use_container_width=True)

    st_autorefresh(interval=10000, key="ui_refresh")
    conn.close()

if __name__ == "__main__": main()
