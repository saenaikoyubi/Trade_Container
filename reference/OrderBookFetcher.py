# import public modules
import time
import pandas as pd
from dydx3 import Client
from dydx3.constants import API_HOST_MAINNET

# import settings variables from settings file
import settings as set
symbol = set.symbol


class OrderBookFetcher:
    def __init__(self):
        self.symbol = symbol
        self.client = Client(host=API_HOST_MAINNET)
        
    def fetch(self):
        for _ in range(2):
            try:
                # get order book
                ob = self.client.public.get_orderbook(market=self.symbol).data
                asks = pd.DataFrame(ob["asks"], dtype='float32')
                bids = pd.DataFrame(ob['bids'], dtype='float32')
                bid_size = bids['size'].values
                bid_price = bids['price'].values
                ask_size = asks['size'].values
                ask_price = asks['price'].values

                return ask_price, ask_size, bid_price, bid_size, True
            except Exception as e:
                print('Error Occured when fetch order book')
                print(e)
                time.sleep(1)
        return 0.0, 0.0, 0.0, 0.0, False
    
