import csv
from datetime import datetime

def log_trade(trade):
    with open("trades.csv", "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            datetime.now(),
            trade["pair"],
            trade["signal"],
            trade["entry"],
            trade["sl"],
            trade["tp"],
            "OPEN"
        ])