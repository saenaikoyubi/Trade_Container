# import public modules
from collections import deque
import numpy as np
    
class Buffer:
    def __init__(self, buffer_size):
        self.buffer = deque(maxlen=buffer_size)
        
    def add(self, ob_map, mid_price):
        data = (ob_map , mid_price)
        self.buffer.append(data)

    def len(self):
        return len(self.buffer)
        
    def get(self):
        data = tuple(self.buffer)
        ob_maps = np.stack([x[0] for x in data])
        mid_prices = np.stack([x[1] for x in data])
        return ob_maps, mid_prices
    
class TradeBuffer:
    def __init__(self, buffer_size):
        self.buffer = deque(maxlen=buffer_size)
        
    def add(self, pred_side):
        self.buffer.append(pred_side)

    def len(self):
        return len(self.buffer)
        
    def get(self):
        pred_side = np.array(list(self.buffer))
        return pred_side
    