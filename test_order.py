#!/usr/bin/env python3
"""주문 테스트 스크립트"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from standx_maker_bot.api.auth import StandXAuth
from standx_maker_bot.api.rest_client import StandXRestClient, OrderSide, OrderType
from standx_maker_bot.utils.config import Config

async def main():
    print("=== StandX 주문 테스트 ===\n")

    # 설정 로드
    config = Config.load("config.yaml")
    print(f"지갑: {config.wallet.address[:15]}...")

    # 인증
    print("\n인증 중...")
    auth = StandXAuth(
        wallet_address=config.wallet.address,
        wallet_private_key=config.wallet.private_key,
        chain=config.standx.chain,
    )
    auth.authenticate()
    print("인증 성공!")

    # REST 클라이언트
    rest = StandXRestClient(auth, config.standx.base_url)

    # 잔액 확인
    print("\n잔액 확인...")
    balance = rest.get_balance()
    print(f"  Available: ${balance.available:.2f}")
    print(f"  Equity: ${balance.equity:.2f}")

    # 현재가 조회
    print("\n현재가 조회...")
    try:
        ticker = rest.get_ticker("BTC-USD")
        print(f"  BTC-USD: ${ticker.last_price:,.2f}")
        print(f"  Bid: ${ticker.best_bid:,.2f}")
        print(f"  Ask: ${ticker.best_ask:,.2f}")

        mid_price = (ticker.best_bid + ticker.best_ask) / 2
    except Exception as e:
        print(f"  티커 조회 실패: {e}")
        # 기본값 사용
        mid_price = 99000

    # 테스트 주문 생성 (매우 낮은 가격으로 체결 안 되게)
    test_price = round(mid_price * 0.98, 1)  # 2% 아래
    test_qty = 0.00005  # 최소 수량

    print(f"\n테스트 주문 생성...")
    print(f"  심볼: BTC-USD")
    print(f"  방향: BUY")
    print(f"  가격: ${test_price:,.1f}")
    print(f"  수량: {test_qty}")

    try:
        order = rest.create_order(
            symbol="BTC-USD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=test_qty,
            price=test_price,
        )
        print(f"\n✅ 주문 성공!")
        print(f"  Order ID: {order.order_id}")
        print(f"  Client Order ID: {order.cl_ord_id}")
        print(f"  Status: {order.status}")

        # 주문 취소
        print(f"\n주문 취소 중...")
        rest.cancel_order(order.cl_ord_id)
        print("✅ 취소 성공!")

    except Exception as e:
        print(f"\n❌ 주문 실패: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
