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
    from standx_maker_bot.utils.telegram_bot import TelegramBot, TelegramConfig
else:
    # Railway 등 클라우드 배포 환경 (프로젝트 폴더가 루트)
    from api.auth import StandXAuth
    from api.rest_client import StandXRestClient
    from api.websocket_client import StandXWebSocket
    from strategy.maker_farming import MakerFarmingStrategy
    from utils.config import Config
    from utils.logger import setup_logger, get_logger
    from utils.telegram_bot import TelegramBot, TelegramConfig


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


async def telegram_status_reporter(telegram_bot: TelegramBot, strategy: MakerFarmingStrategy):
    """텔레그램으로 주기적 상태 리포트 (텔레그램 봇 설정 주기 사용)"""
    while True:
        try:
            # 텔레그램 봇의 리포트 주기 사용 (동적 변경 지원)
            interval = telegram_bot.get_report_interval()
            if interval <= 0:
                # 리포트 비활성화 상태 - 1분마다 체크
                await asyncio.sleep(60)
                continue

            await asyncio.sleep(interval)
            status = strategy.get_status()
            telegram_bot.send_status_report(status, with_menu=False)  # 자동 리포트는 메뉴 버튼 없이
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"텔레그램 상태 리포트 실패: {e}")


async def main_async(config_path: str, dry_run: bool = False, order_size: float = None):
    """비동기 메인 함수"""
    logger = get_logger('main')
    telegram_bot = None

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
    print("[MAIN] REST 클라이언트 초기화 중...", flush=True)
    rest_client = StandXRestClient(auth, config.standx.base_url)
    print("[MAIN] WebSocket 클라이언트 초기화 중...", flush=True)
    ws_client = StandXWebSocket(config.standx.ws_url, auth)
    print("[MAIN] 클라이언트 초기화 완료", flush=True)

    # 잔액 확인
    print("[MAIN] 잔액 조회 중...", flush=True)
    try:
        balance = rest_client.get_balance()
        print(f"[MAIN] 잔액: ${balance.available:.2f} available, ${balance.equity:.2f} equity", flush=True)
    except Exception as e:
        print(f"[MAIN] 잔액 조회 실패: {e}", flush=True)

    # 전략 초기화
    print("[MAIN] 전략 초기화 중...", flush=True)
    strategy = MakerFarmingStrategy(config, rest_client, ws_client)
    print("[MAIN] 전략 초기화 완료", flush=True)

    # 텔레그램 봇 초기화
    telegram_bot = None
    print(f"[MAIN] 텔레그램 설정: enabled={config.telegram.enabled}, token={'있음' if config.telegram.bot_token else '없음'}, chat_id={'있음' if config.telegram.chat_id else '없음'}", flush=True)
    if config.telegram.enabled and config.telegram.bot_token and config.telegram.chat_id:
        telegram_config = TelegramConfig(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            enabled=True,
        )
        telegram_bot = TelegramBot(telegram_config)
        logger.info("텔레그램 봇 활성화됨")

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
        print("[MAIN] 전략 시작 중...", flush=True)
        await strategy.start()
        print("[MAIN] 전략 시작 완료", flush=True)

        # 텔레그램 봇 시작
        telegram_report_task = None
        if telegram_bot:
            # 콜백 설정
            async def on_stop():
                stop_event.set()

            async def on_start():
                # 이미 실행 중이면 무시
                pass

            def get_balance():
                """잔고 및 레버리지 정보 반환"""
                try:
                    balance = rest_client.get_balance()
                    return {
                        'available': balance.available,
                        'equity': balance.equity,
                        'leverage': config.strategy.leverage,
                        'margin_reserve_percent': config.strategy.margin_reserve_percent,
                        'current_order_size': config.strategy.order_size_usd,
                    }
                except Exception as e:
                    logger.error(f"잔고 조회 실패: {e}")
                    return {
                        'available': 0,
                        'equity': 0,
                        'leverage': config.strategy.leverage,
                        'margin_reserve_percent': config.strategy.margin_reserve_percent,
                        'current_order_size': config.strategy.order_size_usd,
                    }

            def get_config():
                """현재 설정 반환"""
                return {
                    'strategy': {
                        'symbols': config.strategy.symbols,
                        'leverage': config.strategy.leverage,
                        'order_size_usd': config.strategy.order_size_usd,
                        'margin_reserve_percent': config.strategy.margin_reserve_percent,
                        'num_orders_per_side': config.strategy.num_orders_per_side,
                        'order_distances_bps': config.strategy.order_distances_bps,
                    },
                    'safety': {
                        'max_position_usd': config.safety.max_position_usd,
                    },
                }

            def set_order_size(new_size: float, force_rebalance: bool = True):
                """주문 크기 변경 및 즉시 재배치"""
                try:
                    old_size = config.strategy.order_size_usd
                    config.strategy.order_size_usd = new_size
                    logger.info(f"주문 크기 변경: ${old_size} -> ${new_size}")

                    # 즉시 재배치 요청 (기존 주문 취소 후 새 크기로 재배치)
                    if force_rebalance:
                        strategy.request_force_rebalance()
                        logger.info("강제 재배치 요청됨")

                    return {
                        'success': True,
                        'old_size': old_size,
                        'new_size': new_size,
                        'leverage': config.strategy.leverage,
                        'rebalanced': force_rebalance,
                    }
                except Exception as e:
                    logger.error(f"주문 크기 변경 실패: {e}")
                    return {'success': False, 'error': str(e)}

            def set_leverage(new_leverage: int):
                """레버리지 변경"""
                try:
                    old_leverage = config.strategy.leverage
                    config.strategy.leverage = new_leverage
                    logger.info(f"레버리지 변경: {old_leverage}x -> {new_leverage}x")
                    return {
                        'success': True,
                        'old_leverage': old_leverage,
                        'new_leverage': new_leverage,
                    }
                except Exception as e:
                    logger.error(f"레버리지 변경 실패: {e}")
                    return {'success': False, 'error': str(e)}

            def set_strategy(num_orders: int):
                """전략 변경 (1+1 또는 2+2)"""
                try:
                    old_num = config.strategy.num_orders_per_side
                    config.strategy.num_orders_per_side = num_orders

                    # 주문 거리도 조정
                    if num_orders == 1:
                        config.strategy.order_distances_bps = [8.0]
                    else:
                        config.strategy.order_distances_bps = [7.5, 8.5]

                    logger.info(f"전략 변경: {old_num}+{old_num} -> {num_orders}+{num_orders}")
                    strategy.request_force_rebalance()

                    return {
                        'success': True,
                        'old_strategy': f"{old_num}+{old_num}",
                        'new_strategy': f"{num_orders}+{num_orders}",
                    }
                except Exception as e:
                    logger.error(f"전략 변경 실패: {e}")
                    return {'success': False, 'error': str(e)}

            def set_distances(preset: str):
                """주문 거리 프리셋 변경"""
                try:
                    presets = {
                        'conservative': [8.0, 9.0],  # 보수적
                        'standard': [7.5, 8.5],      # 표준
                        'aggressive': [6.0, 7.5],    # 공격적
                    }

                    if preset not in presets:
                        return {'success': False, 'error': f'알 수 없는 프리셋: {preset}'}

                    old_distances = config.strategy.order_distances_bps
                    new_distances = presets[preset]

                    # 1+1 전략이면 첫 번째 거리만 사용
                    if config.strategy.num_orders_per_side == 1:
                        new_distances = [new_distances[0]]

                    config.strategy.order_distances_bps = new_distances
                    logger.info(f"주문 거리 변경: {old_distances} -> {new_distances} ({preset})")
                    strategy.request_force_rebalance()

                    return {
                        'success': True,
                        'old_distances': old_distances,
                        'new_distances': new_distances,
                        'preset': preset,
                    }
                except Exception as e:
                    logger.error(f"주문 거리 변경 실패: {e}")
                    return {'success': False, 'error': str(e)}

            def set_protection(enabled: bool):
                """연속 체결 보호 On/Off"""
                try:
                    old_enabled = config.consecutive_fill_protection.enabled
                    config.consecutive_fill_protection.enabled = enabled
                    logger.info(f"연속 체결 보호: {'ON' if enabled else 'OFF'}")
                    return {
                        'success': True,
                        'old_enabled': old_enabled,
                        'new_enabled': enabled,
                    }
                except Exception as e:
                    logger.error(f"연속 체결 보호 설정 실패: {e}")
                    return {'success': False, 'error': str(e)}

            def get_positions():
                """현재 포지션 목록 반환"""
                try:
                    positions = rest_client.get_positions()
                    return [
                        {
                            'symbol': p.symbol,
                            'side': p.side,
                            'size': p.size,
                            'entry_price': p.entry_price,
                            'mark_price': p.mark_price,
                            'unrealized_pnl': p.unrealized_pnl,
                        }
                        for p in positions
                    ]
                except Exception as e:
                    logger.error(f"포지션 조회 실패: {e}")
                    return []

            def close_all_positions():
                """모든 포지션 시장가로 종료"""
                try:
                    # Import 처리
                    try:
                        from api.rest_client import OrderSide, OrderType
                    except ImportError:
                        from standx_maker_bot.api.rest_client import OrderSide, OrderType

                    positions = rest_client.get_positions()
                    if not positions:
                        return {'success': True, 'closed': []}

                    closed = []
                    errors = []

                    for pos in positions:
                        # 포지션 반대 방향으로 시장가 주문
                        # long 포지션이면 sell, short 포지션이면 buy
                        close_side = OrderSide.SELL if pos.side == 'long' else OrderSide.BUY

                        try:
                            result = rest_client.create_order(
                                symbol=pos.symbol,
                                side=close_side,
                                order_type=OrderType.MARKET,
                                quantity=pos.size,
                                reduce_only=True,
                            )
                            closed.append({
                                'symbol': pos.symbol,
                                'side': pos.side,
                                'size': pos.size,
                            })
                            logger.info(f"포지션 종료: {pos.symbol} {pos.side} {pos.size}")
                        except Exception as e:
                            errors.append(f"{pos.symbol}: {e}")
                            logger.error(f"포지션 종료 실패 {pos.symbol}: {e}")

                    if errors:
                        return {
                            'success': False,
                            'error': "; ".join(errors),
                            'closed': closed,
                        }
                    return {'success': True, 'closed': closed}

                except Exception as e:
                    logger.error(f"포지션 종료 실패: {e}")
                    return {'success': False, 'error': str(e)}

            telegram_bot.set_callbacks(
                on_stop=on_stop,
                on_start=on_start,
                get_status=strategy.get_status,
                get_stats=lambda: strategy.get_status()['stats'],
                get_balance=get_balance,
                get_config=get_config,
                set_order_size=set_order_size,
                get_positions=get_positions,
                close_all_positions=close_all_positions,
                set_leverage=set_leverage,
                set_strategy=set_strategy,
                set_distances=set_distances,
                set_protection=set_protection,
                enable_orders=strategy.enable_orders,
                disable_orders=strategy.disable_orders,
                is_orders_enabled=strategy.is_orders_enabled,
                reset_consecutive_fill_pause=strategy.reset_consecutive_fill_pause,
            )
            # ★ 텔레그램 봇을 태스크로 실행 (블로킹 방지)
            telegram_task = asyncio.create_task(telegram_bot.start())
            print("[MAIN] 텔레그램 봇 태스크 시작됨", flush=True)

            # 텔레그램 상태 리포트 태스크 (기본 5분, 동적 변경 지원)
            telegram_report_task = asyncio.create_task(
                telegram_status_reporter(telegram_bot, strategy)
            )

        # 상태 출력 태스크
        status_task = asyncio.create_task(status_printer(strategy, 30))

        # 메인 루프
        print("[MAIN] 메인 루프 태스크 생성 중...", flush=True)
        run_task = asyncio.create_task(strategy.run())
        print("[MAIN] 메인 루프 태스크 생성 완료", flush=True)

        # 종료 대기
        done, pending = await asyncio.wait(
            [run_task, asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 정리
        status_task.cancel()
        run_task.cancel()
        if telegram_report_task:
            telegram_report_task.cancel()
        if telegram_bot:
            telegram_task.cancel()

        try:
            await status_task
        except asyncio.CancelledError:
            pass

        try:
            await run_task
        except asyncio.CancelledError:
            pass

        if telegram_report_task:
            try:
                await telegram_report_task
            except asyncio.CancelledError:
                pass

        if telegram_bot:
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass

    except KeyboardInterrupt:
        logger.info("키보드 인터럽트")

    except Exception as e:
        # 예상치 못한 오류 - 텔레그램으로 알림
        logger.error(f"예상치 못한 오류: {e}")
        if telegram_bot:
            import traceback
            telegram_bot.send_error_message(str(e), traceback.format_exc())
        raise

    finally:
        # 전략 종료
        await strategy.stop()

        # 텔레그램 봇 종료
        if telegram_bot:
            telegram_bot.send_shutdown_message("정상 종료")
            await telegram_bot.stop()

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
