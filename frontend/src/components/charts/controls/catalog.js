// Symbols offered by search. Only tickers the backend accepts (letters, digits, dot),
// so no index symbols like $SPX. Any other valid ticker can still be typed directly.
export const CATALOG = [
  ["QQQ", "Invesco QQQ Trust", "ETF", "NASDAQ"], ["QQQM", "Invesco Nasdaq 100 ETF", "ETF", "NASDAQ"],
  ["TQQQ", "ProShares UltraPro QQQ", "ETF", "NASDAQ"], ["SQQQ", "ProShares UltraPro Short QQQ", "ETF", "NASDAQ"],
  ["SPY", "SPDR S&P 500 ETF Trust", "ETF", "NYSE Arca"], ["VOO", "Vanguard S&P 500 ETF", "ETF", "NYSE Arca"],
  ["IVV", "iShares Core S&P 500 ETF", "ETF", "NYSE Arca"], ["IWM", "iShares Russell 2000 ETF", "ETF", "NYSE Arca"],
  ["DIA", "SPDR Dow Jones Industrial Average ETF", "ETF", "NYSE Arca"], ["XSP", "Mini-SPX (cash-settled index options)", "Index", "CBOE"],
  ["TLT", "iShares 20+ Year Treasury Bond ETF", "ETF", "NASDAQ"], ["GLD", "SPDR Gold Shares", "ETF", "NYSE Arca"],
  ["XLF", "Financial Select Sector SPDR", "ETF", "NYSE Arca"], ["XLK", "Technology Select Sector SPDR", "ETF", "NYSE Arca"],
  ["XLE", "Energy Select Sector SPDR", "ETF", "NYSE Arca"], ["SMH", "VanEck Semiconductor ETF", "ETF", "NASDAQ"],
  ["AAPL", "Apple Inc.", "Stock", "NASDAQ"], ["MSFT", "Microsoft Corporation", "Stock", "NASDAQ"],
  ["NVDA", "NVIDIA Corporation", "Stock", "NASDAQ"], ["AMZN", "Amazon.com Inc.", "Stock", "NASDAQ"],
  ["GOOGL", "Alphabet Inc. Class A", "Stock", "NASDAQ"], ["META", "Meta Platforms Inc.", "Stock", "NASDAQ"],
  ["TSLA", "Tesla Inc.", "Stock", "NASDAQ"], ["AMD", "Advanced Micro Devices", "Stock", "NASDAQ"],
  ["AVGO", "Broadcom Inc.", "Stock", "NASDAQ"], ["NFLX", "Netflix Inc.", "Stock", "NASDAQ"],
  ["JPM", "JPMorgan Chase & Co.", "Stock", "NYSE"], ["BRK.B", "Berkshire Hathaway Class B", "Stock", "NYSE"],
  ["V", "Visa Inc.", "Stock", "NYSE"], ["UNH", "UnitedHealth Group", "Stock", "NYSE"],
  ["XOM", "Exxon Mobil Corporation", "Stock", "NYSE"], ["COST", "Costco Wholesale", "Stock", "NASDAQ"],
].map(([symbol, name, type, exchange]) => ({ symbol, name, type, exchange }));

export const SYMBOL_PATTERN = /^[A-Z][A-Z0-9.]{0,9}$/;

export function searchCatalog(query, kind = "All") {
  const q = query.trim().toUpperCase();
  const pool = kind === "All" ? CATALOG : CATALOG.filter(c => c.type === (kind === "ETFs" ? "ETF" : "Stock"));
  if (!q) return pool.slice(0, 8);
  const starts = pool.filter(c => c.symbol.startsWith(q));
  const contains = pool.filter(c => !c.symbol.startsWith(q) && (c.symbol.includes(q) || c.name.toUpperCase().includes(q)));
  return [...starts, ...contains].slice(0, 8);
}

export const describe = symbol => CATALOG.find(c => c.symbol === symbol);
