import random
import sys
from datetime import datetime
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .models import Coin, CoinValue, Pair


class AutoTrader:
    def __init__(self, binance_manager: BinanceAPIManager, database: Database, logger: Logger, config: Config):
        self.manager = binance_manager
        self.db = database
        self.logger = logger
        self.config = config
        self.best_ratios: Dict[Tuple[str, str], float] = {}

    def transaction_through_bridge(self, pair: Pair):
        """
        Jump from the source coin to the destination coin through bridge coin
        """
        if self.manager.sell_alt(pair.from_coin, self.config.BRIDGE) is None:
            self.logger.info("Couldn't sell, going back to scouting mode...")
            return None
        # This isn't pretty, but at the moment we don't have implemented logic to escape from a bridge coin...
        # This'll do for now
        result = None
        while result is None:
            result = self.manager.buy_alt(pair.to_coin, self.config.BRIDGE)

        self.db.set_current_coin(pair.to_coin)
        self.update_trade_threshold(float(result["price"]))

    def update_trade_threshold(self, current_coin_price: float):
        """
        Update all the coins with the threshold of buying the current held coin
        """
        current_coin = self.db.get_current_coin()

        if current_coin_price is None:
            self.logger.info("Skipping update... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.to_coin == current_coin):
                from_coin_price = self.manager.get_ticker_price(pair.from_coin + self.config.BRIDGE)

                if from_coin_price is None:
                    self.logger.info(
                        "Skipping update for coin {} not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / current_coin_price

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.ratio.is_(None)).all():
                if not pair.from_coin.enabled or not pair.to_coin.enabled:
                    continue
                self.logger.info(f"Initializing {pair.from_coin} vs {pair.to_coin}", False)

                from_coin_price = self.manager.get_ticker_price(pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                to_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / to_coin_price

    def initialize_current_coin(self):
        """
        Decide what is the current coin, and set it up in the DB.
        """
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit("***\nERROR!\nSince there is no backup file, a proper coin name must be provided at init\n***")
            self.db.set_current_coin(current_coin_symbol)

            # if we don't have a configuration, we selected a coin at random... Buy it so we can start trading.
            if self.config.CURRENT_COIN_SYMBOL == "":
                current_coin = self.db.get_current_coin()
                self.logger.info(f"Purchasing {current_coin} to begin trading")
                self.manager.buy_alt(current_coin, self.config.BRIDGE)
                self.logger.info("Ready to start trading")

    def scout(self):
        """
        Scout for potential jumps from the current coin to another coin
        """

        current_coin = self.db.get_current_coin()
        # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot has
        # stopped. Not logging though to reduce log size.
        print(f"{str(datetime.now())} - CONSOLE - INFO - scouting for the best trades. Current coin: {current_coin}-{self.config.BRIDGE}", end="")

        current_coin_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)

        if current_coin_price is None:
            self.logger.info("Skipping scouting... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        ratio_dict: Dict[Pair, float] = {}

        for pair in self.db.get_pairs_from(current_coin):
            if not pair.to_coin.enabled:
                continue
            optional_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)

            if optional_coin_price is None:
                self.logger.info(
                    "Skipping scouting... optional coin {} not found".format(pair.to_coin + self.config.BRIDGE)
                )
                continue

            # self.db.log_scout(pair, pair.ratio, current_coin_price, optional_coin_price)

            # Obtain (current coin)/(optional coin)
            coin_opt_coin_ratio = current_coin_price / optional_coin_price

            transaction_fee = self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True) + self.manager.get_fee(
                pair.to_coin, self.config.BRIDGE, False
            )

            # save ratio so we can pick the best option, not necessarily the first
            ratio_dict[pair] = (
                coin_opt_coin_ratio - transaction_fee * self.config.SCOUT_MULTIPLIER * coin_opt_coin_ratio
            ) - pair.ratio

        # keep only ratios bigger than zero
        ratio_dict_filtered = {k: v for k, v in ratio_dict.items() if v > 0}

        # if we have any viable options, pick the one with the biggest ratio
        if ratio_dict_filtered:
            print()
            best_pair = max(ratio_dict_filtered, key=ratio_dict_filtered.get)
            self.logger.info(f"Will be jumping from {current_coin} to {best_pair.to_coin_id}")
            self.transaction_through_bridge(best_pair)
            self.best_ratios.clear()

        current_best_coin = ""
        current_best_ratio = -float("inf")

        for pair in ratio_dict:
            pair_tuple = (pair.from_coin.symbol, pair.to_coin.symbol)
            if current_best_ratio < ratio_dict[pair]:
                current_best_ratio = ratio_dict[pair]
                current_best_coin = pair.to_coin.symbol
            if pair_tuple in self.best_ratios:
                self.best_ratios[pair_tuple] = max(self.best_ratios[pair_tuple], ratio_dict[pair])
            else:
                self.best_ratios[pair_tuple] = ratio_dict[pair]
        
        print(f" -- best coin {current_best_coin} ({current_best_ratio*100:0.5f})")

    def heartbeat(self):
        if len(self.best_ratios) == 0:
            self.logger.info("No best scouting ratios available. A trade was probably just made.")
            return
        messages = [
            f"{pair[0]:<5} to {pair[1]:<5} Diff: {round(self.best_ratios[pair]*100, 4):0.4f}%"
            for pair in self.best_ratios
        ]
        heartbeat_msg = "Best scouting ratios since last trade:\n" + "\n".join(messages)
        self.logger.info(heartbeat_msg)

    def update_values(self):
        """
        Log current value state of all altcoin balances against BTC and USDT in DB.
        """
        now = datetime.now()

        session: Session
        with self.db.db_session() as session:
            coins: List[Coin] = session.query(Coin).all()
            for coin in coins:
                balance = self.manager.get_currency_balance(coin.symbol)
                if balance == 0:
                    continue
                usd_value = self.manager.get_ticker_price(coin + "USDT")
                btc_value = self.manager.get_ticker_price(coin + "BTC")
                cv = CoinValue(coin, balance, usd_value, btc_value, datetime=now)
                session.add(cv)
                self.db.send_update(cv)
