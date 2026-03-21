import csv

def update_performance():
    try:
        with open("trades.csv", "r") as f:
            trades = list(csv.reader(f))

        total = len(trades)
        wins = sum(1 for t in trades if t[-1] == "WIN")
        losses = sum(1 for t in trades if t[-1] == "LOSS")

        if total > 0:
            winrate = (wins / total) * 100
            print(f"📊 Trades: {total} | Win Rate: {winrate:.2f}%")

    except:
        pass