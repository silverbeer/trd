"""The symbols Sunday Prep snapshots and scans.

`UNIVERSE` is a curated set of broad-impact large caps grouped by theme — the
companies whose earnings move the whole tape. It is deliberately finite: each name
costs one provider call per briefing, so this is a hand-picked ~80, not the S&P 500.
Swap to a market-wide earnings-calendar provider later to go exhaustive.
"""

# label -> futures symbol (yfinance continuous front-month).
FUTURES: dict[str, str] = {
    "S&P 500 Futures": "ES=F",
    "Nasdaq 100 Futures": "NQ=F",
    "Dow Futures": "YM=F",
    "Russell 2000 Futures": "RTY=F",
}

# The three ETFs traders quote levels on.
INDEX_PROXIES: dict[str, str] = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "IWM": "Russell 2000 ETF",
}

VIX_SYMBOL = "^VIX"

# The 11 SPDR sector ETFs — the standard read on leadership/lag.
SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

# Curated large-cap universe: symbol -> (company name, theme tag).
# Theme tags drive the "emerging themes" read; keep them coarse.
UNIVERSE: dict[str, tuple[str, str]] = {
    # Mega-cap tech / AI
    "AAPL": ("Apple", "AI/Tech"),
    "MSFT": ("Microsoft", "AI/Tech"),
    "GOOGL": ("Alphabet", "AI/Tech"),
    "AMZN": ("Amazon", "AI/Tech"),
    "META": ("Meta Platforms", "AI/Tech"),
    "NVDA": ("NVIDIA", "Semiconductors"),
    "AVGO": ("Broadcom", "Semiconductors"),
    "AMD": ("Advanced Micro Devices", "Semiconductors"),
    "TSM": ("Taiwan Semiconductor", "Semiconductors"),
    "MU": ("Micron", "Semiconductors"),
    "INTC": ("Intel", "Semiconductors"),
    "QCOM": ("Qualcomm", "Semiconductors"),
    "TXN": ("Texas Instruments", "Semiconductors"),
    "ASML": ("ASML", "Semiconductors"),
    "ORCL": ("Oracle", "AI/Tech"),
    "CRM": ("Salesforce", "AI/Tech"),
    "ADBE": ("Adobe", "AI/Tech"),
    "PLTR": ("Palantir", "AI/Tech"),
    "NOW": ("ServiceNow", "AI/Tech"),
    "SMCI": ("Super Micro", "AI/Tech"),
    "DELL": ("Dell", "AI/Tech"),
    "ARM": ("Arm Holdings", "Semiconductors"),
    # Consumer / discretionary
    "TSLA": ("Tesla", "EV/Consumer"),
    "HD": ("Home Depot", "Consumer"),
    "MCD": ("McDonald's", "Consumer"),
    "NKE": ("Nike", "Consumer"),
    "SBUX": ("Starbucks", "Consumer"),
    "COST": ("Costco", "Consumer"),
    "WMT": ("Walmart", "Consumer"),
    "TGT": ("Target", "Consumer"),
    "LOW": ("Lowe's", "Consumer"),
    "DIS": ("Disney", "Media/Consumer"),
    "NFLX": ("Netflix", "Media/Consumer"),
    # Financials
    "JPM": ("JPMorgan Chase", "Financials"),
    "BAC": ("Bank of America", "Financials"),
    "WFC": ("Wells Fargo", "Financials"),
    "GS": ("Goldman Sachs", "Financials"),
    "MS": ("Morgan Stanley", "Financials"),
    "C": ("Citigroup", "Financials"),
    "BLK": ("BlackRock", "Financials"),
    "SCHW": ("Charles Schwab", "Financials"),
    "AXP": ("American Express", "Financials"),
    "V": ("Visa", "Financials"),
    "MA": ("Mastercard", "Financials"),
    "BRK-B": ("Berkshire Hathaway", "Financials"),
    # Health care
    "UNH": ("UnitedHealth", "Health Care"),
    "JNJ": ("Johnson & Johnson", "Health Care"),
    "LLY": ("Eli Lilly", "Health Care"),
    "PFE": ("Pfizer", "Health Care"),
    "MRK": ("Merck", "Health Care"),
    "ABBV": ("AbbVie", "Health Care"),
    "TMO": ("Thermo Fisher", "Health Care"),
    "ABT": ("Abbott", "Health Care"),
    "AMGN": ("Amgen", "Health Care"),
    # Energy
    "XOM": ("ExxonMobil", "Energy"),
    "CVX": ("Chevron", "Energy"),
    "COP": ("ConocoPhillips", "Energy"),
    "SLB": ("Schlumberger", "Energy"),
    "OXY": ("Occidental", "Energy"),
    # Industrials / defense / space
    "BA": ("Boeing", "Industrials"),
    "CAT": ("Caterpillar", "Industrials"),
    "GE": ("GE Aerospace", "Industrials"),
    "HON": ("Honeywell", "Industrials"),
    "RTX": ("RTX", "Defense"),
    "LMT": ("Lockheed Martin", "Defense"),
    "DE": ("Deere", "Industrials"),
    "UPS": ("UPS", "Industrials"),
    "RKLB": ("Rocket Lab", "Space"),
    # Staples / telecom / other broad-impact
    "PG": ("Procter & Gamble", "Staples"),
    "KO": ("Coca-Cola", "Staples"),
    "PEP": ("PepsiCo", "Staples"),
    "T": ("AT&T", "Telecom"),
    "VZ": ("Verizon", "Telecom"),
    "CMCSA": ("Comcast", "Media/Consumer"),
    "IBM": ("IBM", "AI/Tech"),
    "CSCO": ("Cisco", "AI/Tech"),
    "UBER": ("Uber", "Consumer"),
}


def name_for(symbol: str) -> str:
    """Display name for a known symbol; falls back to the bare ticker."""
    symbol = symbol.upper()
    if symbol in UNIVERSE:
        return UNIVERSE[symbol][0]
    if symbol in SECTOR_ETFS:
        return SECTOR_ETFS[symbol]
    if symbol in INDEX_PROXIES:
        return INDEX_PROXIES[symbol]
    return symbol
