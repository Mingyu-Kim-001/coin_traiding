import os
from datetime import datetime, timedelta
from collections import deque
import pandas as pd
from binance.client import Client
import asyncio
from binance import AsyncClient, BinanceSocketManager, ThreadedWebsocketManager

import alpha_collection
from utils import *
from decimal import Decimal


api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_SECRET_API_KEY')
client = Client(api_key, api_secret)

def get_binance_klines_data(timestamp_start, timestamp_end, symbol, interval='1m'):
    timestamp_start_str = str(int(timestamp_start.timestamp()))
    timestamp_end_str = str(int(timestamp_end.timestamp()))
    klines = client.futures_historical_klines(symbol, interval, timestamp_start_str, timestamp_end_str)
    df = pd.DataFrame(klines,
                      columns=['open_time', 'open', 'high', 'low', 'close', 'volume',
                               'close_time',
                               'quote_asset_volume',
                               'number_of_trades', 'taker_buy_base_asset_volume',
                               'taker_buy_quote_asset_volume', 'ignore'])
    df['open_time'] = df['open_time'].apply(lambda x: datetime.fromtimestamp(x / 1000))
    df['close_time'] = df['close_time'].apply(lambda x: datetime.fromtimestamp(x / 1000))
    return df


def get_current_futures_position(symbols):
    account_info = client.futures_account()
    positions = account_info['positions']
    df_futures_position = pd.DataFrame([position for position in positions if position['symbol'] in symbols]).set_index("symbol")[['entryPrice', 'positionAmt', 'leverage']].astype(float)
    max_withdraw_amount = float(account_info['maxWithdrawAmount'])
    return df_futures_position, max_withdraw_amount

def create_order(symbol, price, quantity, leverage, is_dryrun=False):
    side = "BUY" if quantity > 0 else "SELL"
    quantity = str(abs(Decimal(str(quantity))))
    if not is_dryrun and quantity != '0':
        try:
            order = client.futures_create_order(
                symbol=symbol,
                side=side,
                price=price,
                quantity=quantity,
                #leverage=leverage,
                timeInForce='GTC',
                type="LIMIT",
                reduceOnly=False
            )
        except:
            print(f'Failed to create order for {symbol} {side} {quantity} at price {price}')
            return False
    print(f'{side} {quantity} {symbol} at price {price}, leverage={leverage}')
    return True

def get_futures_trading_rules():
    with open('./futures_trading_rules/futures_trading_rules.csv', 'r') as f:
        df_future_trading_rules = pd.read_csv(f)
    return df_future_trading_rules

def get_futures_order_book(symbol):
    order_book = client.futures_order_book(symbol=symbol)
    return order_book


def order_with_quantity(df, quantity_column_name, price_column_name, is_dryrun=False, leverage=1):
    unfilled_symbols = deque(df.index)
    while unfilled_symbols:
        symbol = unfilled_symbols.popleft()
        if not create_order(symbol=symbol, price=df.loc[symbol, price_column_name], quantity=df.loc[symbol, quantity_column_name], leverage=leverage, is_dryrun=is_dryrun):
            unfilled_symbols.append(symbol)


def cancel_all_orders(symbols):
    for symbol in symbols:
        open_orders = client.futures_get_open_orders(symbol=symbol)
        for order in open_orders:
            order_id = order['orderId']
            cancel_response = client.futures_cancel_order(symbol=symbol, orderId=order_id)
            if cancel_response['status'] == 'CANCELED':
                print(f"Order {order_id} canceled successfully.")
            else:
                print(f"Failed to cancel order {order_id}. Error: {cancel_response['msg']}")


if __name__ == '__main__':
    is_dryrun = True
    minimum_initial_usdt = 300
    leverage = 3
    pd.set_option('display.max_columns', 500)
    pd.set_option('display.width', 1000)
    symbols = ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'DOGEUSDT', 'LTCUSDT', 'MATICUSDT', 'TRXUSDT', 'ADAUSDT', 'SOLUSDT']
    close_48hours = {}
    dict_df_klines = {}
    df_current_futures_position, max_withdraw_amount = get_current_futures_position(symbols)
    old_leverage = df_current_futures_position['leverage'].iloc[0] # we assume leverage are all the same
    if not is_dryrun:
        cancel_all_orders(symbols)
        for symbol in symbols:
            client.futures_change_leverage(symbol=symbol, leverage=str(leverage))
    past_price = {symbol: float(client.futures_historical_klines(symbol, '1h', '48 hours ago UTC')[0][4]) for symbol in symbols}
    current_price = {symbol: float(client.futures_ticker(symbol=symbol)['lastPrice']) for symbol in symbols}
    dict_df_close = {symbol: pd.DataFrame({'close': [past_price[symbol], current_price[symbol]]}, index=['past', 'current']) for symbol in symbols}
    alphas = alpha_collection.Alphas()
    df_weight = alphas.close_momentum_nday(dict_df_close, n=1, weight_max=None, shift=0).loc[['current']].T.rename(columns={'current':'next_position_usdt'}) # we have a pre-processed data, so n must be 1, shift must be 0
    df_current_price_and_amount = pd.DataFrame.from_dict(current_price, orient='index', columns=['price']).join(df_current_futures_position)
    non_leveraged_total_quantity_usdt = ((df_current_price_and_amount['positionAmt'].abs() * df_current_price_and_amount['price']).sum() / old_leverage + max_withdraw_amount) * 0.95
    non_leveraged_total_quantity_usdt = np.max([non_leveraged_total_quantity_usdt, minimum_initial_usdt])
    df_quantity = df_weight * non_leveraged_total_quantity_usdt * leverage
    df_quantity_and_price = df_quantity.join(df_current_price_and_amount)\
        .assign(current_position_usdt=lambda x: x.positionAmt * x.price)\
        .assign(changing_position_usdt=lambda x: x.next_position_usdt - x.current_position_usdt)\
        .assign(margin_increase=lambda x: x.next_position_usdt.abs() - x.current_position_usdt.abs())
    df_quantity_and_price_trimmed = trim_quantity(df_quantity_and_price, usdt_column_name='changing_position_usdt', price_column_name='price').sort_values('margin_increase')
    order_with_quantity(df_quantity_and_price_trimmed, quantity_column_name='quantity_trimmed', price_column_name='price', leverage=leverage, is_dryrun=is_dryrun)
    print()
