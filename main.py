#!/usr/bin/env python3
"""
StandX 메이커 포인트 파밍 봇
CLI 진입점
"""
import argparse
import asyncio
import signal
import sys
from pathlib import Path

# 프로젝트 루트의 부모 디렉토리를 path에 추가 (로컬 개발용)
# Railway 배포 시에는 프로젝트 루트에서 직접 실행되므로 불필요
project_root = Path(__file__).parent.parent
if project_root.name == '03_Claude':
    sys.path.insert(0, str(project_root))
    from standx_maker_bot.api.auth import StandXAuth
    from standx_maker_bot.api.rest_client import StandXRestClient
    from standx_maker_bot.api.websocket_client import StandXWebSocket
    from standx_maker_bot.strategy.maker_farming import MakerFarmingStrategy
    from standx_maker_bot.utils.config import Config
    from standx_maker_bot.utils.logger import setup_logger, get_logger
else:
    # Railway 등 클라우드 배포 환경 (프로젝트 폴더가 루트)
    from api.auth import StandXAuth
    from api.rest_client import StandXRestClient
    from api.websocket_client import StandXWebSocket
    from strategy.maker_farming import MakerFarmingStrategy
    from utils.config import Config
    from utils.logger import setup_logger, get_logger


def print_banner():
    """배너 출력"""
    banner = """
================================================================
          StandX Maker Points Farming Bot v1.0.0

  Band A (0-10 bps): 100% points
  Strategy: Place limit orders near mark price (8-9 bps)
================================================================
"""
    print(banner)


def print_status(strategy: MakerFarmingStrategy):
    """상태 출력"""
    status = strategy.get_status()
    stats = status['stats']

    print("\n" + "=" * 60)
    print(f"Runtime: {status['runtime_hours']:.2f} hours")
    print(f"Emergency Stopped: {status['emergency_stopped']}")
    print("-" * 60)
    print(f"Orders Placed: {stats['orders_placed']}")
    print(f"Orders Cancelled: {stats['orders_cancelled']}")
    print(f"Rebalances: {stats['rebalances']}")
    print(f"Fills (unwanted): {stats['fills']}")
    print(f"Estimated Points: {stats['estimated_points']:.1f}")
    print("-" * 60)

    for symbol, sym_status in status['symbols'].items():
        print(f"\n[{symbol}]")
        print(f"  Mid Price: ${sym_status['mid_price']:,.2f}")
        print(f"  Spread: {sym_status['spread_bps']:.1f} bps")

        if sym_status['buy_order']:
            buy = sym_status['buy_order']
            print(f"  BUY: {buy['quantity']} @ ${buy['price']:,.2f} ({buy['status']})")

        if sym_status['sell_order']:
            sell = sym_status['sell_order']
            print(f"  SELL: {sell['quantity']} @ ${sell['price']:,.2f} ({sell['status']})")

    print("=" * 60)


async def status_printer(strategy: MakerFarmingStrategy, interval: float = 30.0):
    """주기적 상태 출력"""
    while True:
        try:
            await asyncio.sleep(interval)
            print_status(strategy)
        except asyncio.CancelledError:
            break


async def main_async(config_path: str, dry_run: bool = False, order_size: float = None):
    """비동기 메인 함수"""
    logger = get_logger('main')

    # 설정 로드
    logger.info("설정 로드 중...")
    config = Config.load(config_path)

    # CLI에서 주문 크기 지정 시 덮어쓰기
    if order_size is not None:
        config.strategy.order_size_usd = order_size
        logger.info(f"주문 크기 덮어쓰기: ${order_size}")

    # 설정 검증
    errors = config.validate()
    if errors:
        for error in errors:
            logger.error(f"설정 오류: {error}")
        return 1

    logger.info(f"체인: {config.standx.chain}")
    logger.info(f"심볼: {config.strategy.symbols}")
    logger.info(f"주문 크기: ${config.strategy.order_size_usd}/symbol")

    # Dry run 모드
    if dry_run:
        logger.info("Dry run 모드 - 실제 주문 없음")
        print("\n설정 확인:")
        print(f"  - 지갑: {config.wallet.address[:10]}...")
        print(f"  - 심볼: {config.strategy.symbols}")
        print(f"  - 주문 크기: ${config.strategy.order_size_usd}")
        print(f"  - 목표 거리: {config.strategy.target_distance_bps} bps")
        print(f"  - 최대 거리: {config.strategy.max_distance_bps} bps")
        return 0

    # 인증
    logger.info("인증 중...")
    auth = StandXAuth(
        wallet_address=config.wallet.address,
        wallet_private_key=config.wallet.private_key,
        chain=config.standx.chain,
    )

    try:
        auth.authenticate()
        logger.info("인증 성공")
    except Exception as e:
        logger.error(f"인증 실패: {e}")
        return 1

    # 클라이언트 초기화
    rest_client = StandXRestClient(auth, config.standx.base_url)
    ws_client = StandXWebSocket(config.standx.ws_url, auth)

    # 잔액 확인
    try:
        balance = rest_client.get_balance()
        logger.info(f"잔액: ${balance.available:.2f} available, ${balance.equity:.2f} equity")
    except Exception as e:
        logger.warning(f"잔액 조회 실패: {e}")

    # 전략 초기화
    strategy = MakerFarmingStrategy(config, rest_client, ws_client)

    # 시그널 핸들러
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("종료 시그널 수신")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows에서는 signal handler가 제한적
            pass

    # 전략 시작
    try:
        await strategy.start()

        # 상태 출력 태스크
        status_task = asyncio.create_task(status_printer(strategy, 30))

        # 메인 루프
        run_task = asyncio.create_task(strategy.run())

        # 종료 대기
        done, pending = await asyncio.wait(
            [run_task, asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 정리
        status_task.cancel()
        run_task.cancel()

        try:
            await status_task
        except asyncio.CancelledError:
            pass

        try:
            await run_task
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        logger.info("키보드 인터럽트")

    finally:
        # 전략 종료
        await strategy.stop()

        # 최종 상태 출력
        print_status(strategy)

    logger.info("봇 종료")
    return 0


def main():
    """CLI 메인 함수"""
    parser = argparse.ArgumentParser(
        description="StandX Maker Points Farming Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '-c', '--config',
        default='config.yaml',
        help='설정 파일 경로 (기본: config.yaml)',
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='상세 로그 출력',
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='설정만 확인하고 실제 거래 없음',
    )

    parser.add_argument(
        '-l', '--log-file',
        help='로그 파일 경로',
    )

    parser.add_argument(
        '-s', '--size',
        type=float,
        help='주문 크기 (USD) - config.yaml 설정 덮어쓰기',
    )

    args = parser.parse_args()

    # 로거 설정
    import logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logger('standx_bot', log_level, args.log_file)

    # 배너
    print_banner()

    # 실행
    try:
        exit_code = asyncio.run(main_async(args.config, args.dry_run, args.size))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n중단됨")
        sys.exit(130)


if __name__ == '__main__':
    main()
