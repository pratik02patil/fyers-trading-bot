import streamlit as st
import pandas as pd
import sqlite3
import time
import threading
import datetime
import pytz
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

# ==========================================
# 1. CONFIGURATION & DATABASE SETUP
# ==========================================
DB_FILE = "trading_bot.db"
CLIENT_ID = st.secrets["fyers"]["client_id"]
SECRET_KEY = st.secrets["fyers"]["secret_key"]
REDIRECT_URI = "https://www.google.com/"
TOKEN_FILE = "access_token.txt"
IST = pytz.timezone('Asia/Kolkata')

def init_db():
    """Initializes SQLite tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS scanned_symbols (
                    symbol TEXT PRIMARY KEY, ltp REAL, atl REAL, lh1 REAL, lh2 REAL, 
                    fvg_low REAL, target REAL, sl REAL, status TEXT, 
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                    symbol TEXT PRIMARY KEY, entry_price REAL, ltp REAL, 
                    pnl REAL, target REAL, sl REAL, qty INTEGER, 
                    trade_type TEXT, entry_time DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry REAL, 
                    exit_price REAL, result TEXT, total_pnl REAL, trade_type TEXT,
                    exit_time DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

# ==========================================
# 2. FYERS WORKER
# ==========================================
class FyersWorker:
    def __init__(self):
        self.access_token = self._load_token()
        self.fyers = None
        if self.access_token:
            # Initialize V3 Model
            self.fyers = fyersModel.FyersModel(client_id=CLIENT_ID, token=self.access_token, is_async=False, log_path="")

    def _load_token(self):
        try:
            with open(TOKEN_FILE, "r") as f:
                return f.read().strip()
        except: return None

    def get_ltp(self, symbols):
        """Fetch quotes for multiple symbols at once (Max 50)."""
        if not self.fyers or not symbols: return {}
        try:
            # Join symbols with comma for quotes API
            response = self.fyers.quotes(data={"symbols": ",".join(symbols)})
            if 'd' in response:
                return {x['n']: x['v']['lp'] for x in response['d']}
        except: pass
        return {}

# ==========================================
# 3. FIXED SYMBOL SEEDING (FROM MAIN29 LOGIC)
# ==========================================
def seed_symbols(worker, conn):
    """Uses Fyers Option Chain API to fetch valid tradeable symbols."""
    indices = ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]
    
    for idx in indices:
        try:
            # strikecount 5 fetches 5 strikes above/below ATM
            data = {"symbol": idx, "strikecount": 5, "timestamp": ""}
            response = worker.fyers.optionchain(data=data)
            
            if response.get('s') == 'ok' and 'data' in response:
                oc_data = response['data']
                options = oc_data.get('optionsChain', [])
                
                # Identify the nearest expiry timestamp safely
                expiry_data = oc_data.get('expiryData', [])
                if not expiry_data:
                    continue
                
                # Get first expiry in the list (nearest)
                nearest_expiry = expiry_data[0].get('expiry')
                
                # Add valid symbols to our database
                for opt in options:
                    # Match only current week's contracts
                    if opt.get('expiry') == nearest_expiry:
                        sym = opt.get('symbol')
                        price = opt.get('ltp', 0.0)
                        conn.execute("INSERT OR IGNORE INTO scanned_symbols (symbol, ltp, status) VALUES (?,?,?)", 
                                     (sym, price, "SCANNING"))
            st.toast(f"Successfully seeded {idx} options.")
        except Exception as e:
            st.error(f"Seeding Failed for {idx}: {str(e)}")
    conn.commit()

# ==========================================
# 4. BACKGROUND PRICE UPDATER
# ==========================================
def background_loop():
    """Independent thread to update prices every 60 seconds."""
    while True:
        try:
            worker_conn = sqlite3.connect(DB_FILE)
            worker = FyersWorker()
            if worker.fyers:
                # Update Scanner Prices
                scanned = pd.read_sql("SELECT symbol FROM scanned_symbols", worker_conn)
                if not scanned.empty:
                    prices = worker.get_ltp(scanned['symbol'].tolist())
                    for s, p in prices.items():
                        worker_conn.execute("UPDATE scanned_symbols SET ltp=? WHERE symbol=?", (p, s))
                worker_conn.commit()
            worker_conn.close()
        except: pass
        time.sleep(60)

# ==========================================
# 5. STREAMLIT UI
# ==========================================
def main():
    st.set_page_config(page_title="Fyers Trading Bot", layout="wide")
    conn = init_db()
    
    # Start background pricing engine
    if 'thread_active' not in st.session_state:
        threading.Thread(target=background_loop, daemon=True).start()
        st.session_state['thread_active'] = True

    # SIDEBAR: AUTHENTICATION
    st.sidebar.title("Bot Controls")
    worker = FyersWorker()
    if not worker.access_token:
        # Auth Flow
        session = fyersModel.SessionModel(client_id=CLIENT_ID, secret_key=SECRET_KEY, 
                                          redirect_uri=REDIRECT_URI, response_type="code", grant_type="authorization_code")
        st.sidebar.info("Login Required")
        st.sidebar.markdown(f"[Get Auth Code]({session.generate_authcode()})")
        auth_code = st.sidebar.text_input("Paste Auth Code Here:")
        if st.sidebar.button("Save Access Token"):
            session.set_token(auth_code)
            res = session.generate_token()
            if "access_token" in res:
                with open(TOKEN_FILE, "w") as f: f.write(res["access_token"])
                st.rerun()
    else:
        st.sidebar.success("Fyers Connected ✅")
        if st.sidebar.button("Re-Seed Option Chain", width='stretch'):
            seed_symbols(worker, conn)
            st.rerun()

    # DEBUG SECTION
    with st.expander("🛠️ API Debugger"):
        if worker.fyers:
            nifty_price = worker.get_ltp(["NSE:NIFTY50-INDEX"])
            st.write("Nifty Spot:", nifty_price.get("NSE:NIFTY50-INDEX", "Error fetching"))
            
            db_size = conn.execute("SELECT COUNT(*) FROM scanned_symbols").fetchone()[0]
            st.write(f"Database contains **{db_size}** tracked symbols.")
        else:
            st.error("Please login to see live debug data.")

    # MAIN DASHBOARD TABS
    tab1, tab2, tab3 = st.tabs(["Scanner", "Active Trades", "History"])

    with tab1:
        st.subheader("Market Scanner (ATL/FVG)")
        df_scan = pd.read_sql("SELECT * FROM scanned_symbols", conn)
        # Updated to width='stretch' per Streamlit 2026 guidelines
        st.dataframe(df_scan, width='stretch')

    with tab2:
        st.subheader("Live Positions")
        df_active = pd.read_sql("SELECT * FROM active_trades", conn)
        st.dataframe(df_active, width='stretch')

    with tab3:
        st.subheader("Completed Trades")
        df_history = pd.read_sql("SELECT * FROM history", conn)
        st.dataframe(df_history, width='stretch')

    # Keep UI fresh every 30 seconds
    st_autorefresh(interval=30000, key="global_refresh")
    conn.close()

if __name__ == "__main__":
    main()