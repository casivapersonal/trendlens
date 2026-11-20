The trading arena scans Nifty 500 by default. It looks, in order, for:
  universe/nifty500.csv     (NSE constituents CSV with a column named Symbol; download by hand from niftyindices.com)
  pipeline/nifty500.txt     (symbols separated by spaces or lines; the TrendLens v3 pipeline already uses this file)
  pipeline/syms.txt         (existing TrendLens list; last fallback)
For Nifty 1000 put universe/nifty1000.csv here and set [universe] name = "nifty1000" in trading/config.toml.
NSE gives constituent lists as hand-downloads only. Refresh after each index review (March and September).
