# import public modules
import numpy as np

# import private class
from ForTrade.OrderBookFetcher import OrderBookFetcher

# import settings variables from settings file
import settings as set
range = set.range

class OrderBook:
    def __init__(self):
        self.OBFetcher = OrderBookFetcher()
        self.range = range
    
    def get_orderbook(self):
        ask_price_, ask_size_, bid_price_, bid_size_, flg = self.OBFetcher.fetch()
        if not(flg):
            return False
        ask_price = ask_price_[ask_size_ > 0.001][:range]
        ask_size = ask_size_[ask_size_ > 0.001][:range]
        bid_price = bid_price_[bid_size_ > 0.001][:range]
        bid_size = bid_size_[bid_size_ > 0.001][:range]
        ask_q75, ask_q25 = np.percentile(ask_size[ask_size >= 0.01], [75 ,25])
        bid_q75, bid_q25 = np.percentile(bid_size[bid_size >= 0.01], [75 ,25])
        ask_iqr = ask_q75 - ask_q25
        bid_iqr = bid_q75 - bid_q25
        ask_high_q = ask_q75 + 1.5 * ask_iqr
        bid_high_q = bid_q75 + 1.5 * bid_iqr
        ask_size_modify = np.where(ask_size <= ask_high_q, ask_size, ask_high_q)
        bid_size_modify = np.where(bid_size <= bid_high_q, bid_size, bid_high_q)
        mid_price = (ask_price[0] + bid_price[0]) / 2.
        # spread = ask_price[0] - bid_price[0]
        ob_map = np.array([[a_p, a_s, b_p, -b_s] for a_p, a_s, b_p, b_s in zip(ask_price, ask_size_modify, bid_price, bid_size_modify)]).reshape(-1)
        return ob_map, mid_price # , spread, ask_price[0], bid_price[0]


