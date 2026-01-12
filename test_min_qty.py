#!/usr/bin/env python3
"""최소 수량 테스트"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from standx_maker_bot.api.auth import StandXAuth
from standx_maker_bot.api.rest_client import StandXRestClient, OrderSide, OrderType
from standx_maker_bot.utils.config import Config

def main():
    print("=== StandX min qty test ===\n")

    config = Config.load("config.yaml")

    auth = StandXAuth(
        wallet_address=config.wallet.address,
        wallet_private_key=config.wallet.private_key,
        chain=config.standx.chain,
    )
    auth.authenticate()
    print("Auth OK!")

    rest = StandXRestClient(auth, config.standx.base_url)

    # 잔액
    balance = rest.get_balance()
    print(f"Available: ${balance.available:.2f}")

    # 수량 테스트 (점점 키우면서)
    test_qtys = [0.0001, 0.0002, 0.0005, 0.001]
    test_price = 97000.0  # 현재가보다 낮게

    for qty in test_qtys:
        notional = qty * test_price
        print(f"\nTest: qty={qty} (${notional:.2f})")

        try:
            result = rest.create_order(
                symbol="BTC-USD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=qty,
                price=test_price,
            )
            print(f"  SUCCESS! Result: {result}")

            # 성공하면 취소
            cl_ord_id = result.get('clOrdId') or result.get('cl_ord_id')
            if cl_ord_id:
                rest.cancel_order(cl_ord_id)
                print(f"  Cancelled: {cl_ord_id}")
            break

        except Exception as e:
            error_msg = str(e)
            print(f"  FAIL: {error_msg}")

if __name__ == "__main__":
    main()
