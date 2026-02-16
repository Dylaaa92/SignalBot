import os
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

def _api_url(env: str) -> str:
    return constants.MAINNET_API_URL if env == "mainnet" else constants.TESTNET_API_URL

class HL:
    def __init__(self, env: str):
        self.env = env
        self.api_url = _api_url(env)

        self.account_address = os.getenv("HL_ACCOUNT_ADDRESS", "")
        self.secret_key = os.getenv("HL_SECRET_KEY", "")

        if not self.account_address or not self.secret_key:
            raise RuntimeError("Missing HL_ACCOUNT_ADDRESS or HL_SECRET_KEY in environment")

        self.info = Info(self.api_url, skip_ws=True)
        self.ex = Exchange(self.api_url, self.secret_key, account_address=self.account_address)

    def mid(self, coin: str) -> float:
        # You already subscribe to allMids, but this is useful as a fallback.
        m = self.info.all_mids()
        return float(m[coin])

    def open_orders(self, coin: str):
        return self.info.open_orders(self.account_address, coin)

    def cancel_all(self, coin: str):
        orders = self.open_orders(coin) or []
        cancels = []
        for o in orders:
            cancels.append({"a": o["asset"], "o": o["oid"]})
        if not cancels:
            return None
        return self.ex.cancel(cancels)

    def place_limit(self, coin: str, is_buy: bool, px: float, sz: float, reduce_only: bool, post_only=True):
        # post_only maps to ALO in HL terms (add liquidity only). :contentReference[oaicite:3]{index=3}
        tif = "Alo" if post_only else "Gtc"
        return self.ex.order(
            coin,
            is_buy=is_buy,
            sz=sz,
            limit_px=px,
            order_type={"limit": {"tif": tif}},
            reduce_only=reduce_only,
        )
