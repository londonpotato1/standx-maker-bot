#!/usr/bin/env python3
"""
StandX 메이커 포인트 파밍 봇 - 인터랙티브 모드
"""
import asyncio
import getpass
import msvcrt  # Windows 키보드 입력
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from standx_maker_bot.api.auth import StandXAuth
from standx_maker_bot.api.rest_client import StandXRestClient
from standx_maker_bot.api.websocket_client import StandXWebSocket
from standx_maker_bot.strategy.maker_farming import MakerFarmingStrategy
from standx_maker_bot.utils.config import Config
from standx_maker_bot.utils.logger import setup_logger, get_logger
from standx_maker_bot.utils.password_crypto import PasswordCrypto, InvalidToken

# 청산 수수료 예약 (USD) - 마켓 오더 실행에 필요한 최소 금액
LIQUIDATION_FEE_RESERVE_USD = 0.50


def print_banner():
    print("""
================================================================
          StandX Maker Points Farming Bot v1.0.0
                   Interactive Mode
================================================================
""")


def print_menu():
    print("""
[명령어]
  1. 주문 크기 설정
  2. 잔액 확인
  3. 봇 시작 (RUN)
  4. 봇 정지 (STOP)
  5. 상태 확인
  6. 설정 확인
  7. 모니터링 모드 (실시간 UI)
  q. 종료
""")


def print_status(strategy: MakerFarmingStrategy):
    """상태 출력"""
    status = strategy.get_status()
    stats = status['stats']

    print("\n" + "=" * 60)
    print(f"Running: {status['running']}")
    print(f"Runtime: {status['runtime_hours']:.2f} hours")
    print(f"Emergency Stopped: {status['emergency_stopped']}")
    print("-" * 60)
    print(f"Orders Placed: {stats['orders_placed']}")
    print(f"Orders Cancelled: {stats['orders_cancelled']}")
    print(f"Rebalances: {stats['rebalances']}")
    print(f"Fills: {stats['fills']}")
    print(f"Liquidations: {stats['liquidations']}")
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


class InteractiveBot:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config = None
        self.auth = None
        self.rest_client = None
        self.ws_client = None
        self.strategy = None
        self.logger = get_logger('interactive')

        # 사용자 설정
        self.order_size_usd = 5.0  # 기본값

        # 봇 상태
        self.bot_running = False
        self.bot_task = None

    async def initialize(self):
        """초기화"""
        self.logger.info("설정 로드 중...")
        self.config = Config.load(self.config_path)

        # 설정 검증 (지갑 관련 에러는 무시 - 암호화된 credentials 사용)
        errors = self.config.validate()
        errors = [e for e in errors if 'WALLET' not in e]  # 지갑 에러 필터링
        if errors:
            for error in errors:
                print(f"[오류] {error}")
            return False

        # 암호화된 credentials 로드
        data_dir = Path(__file__).parent / "data"
        crypto = PasswordCrypto(data_dir)

        if not crypto.has_credentials():
            print("[오류] 저장된 자격증명이 없습니다.")
            print("       먼저 'python setup_credentials.py --add standx'로 지갑을 등록하세요.")
            return False

        # 비밀번호 입력
        print("\n암호화된 지갑 정보를 복호화합니다.")
        password = getpass.getpass("비밀번호: ")

        try:
            cred = crypto.load_credential(password, "standx")
            if not cred:
                print("[오류] 'standx' 자격증명을 찾을 수 없습니다.")
                print("       'python setup_credentials.py --add standx'로 등록하세요.")
                return False

            wallet_address = cred.address
            wallet_private_key = cred.private_key

            if not wallet_address or not wallet_private_key:
                print("[오류] 지갑 주소 또는 개인키가 비어있습니다.")
                return False

            print(f"지갑 로드 완료: {wallet_address[:10]}...{wallet_address[-6:]}")

        except InvalidToken:
            print("[오류] 비밀번호가 틀렸습니다.")
            return False
        except Exception as e:
            print(f"[오류] 자격증명 로드 실패: {e}")
            return False

        # 인증
        print("인증 중...")
        self.auth = StandXAuth(
            wallet_address=wallet_address,
            wallet_private_key=wallet_private_key,
            chain=self.config.standx.chain,
        )

        try:
            self.auth.authenticate()
            print(f"인증 성공: {wallet_address[:10]}...")
        except Exception as e:
            print(f"[오류] 인증 실패: {e}")
            return False

        # 클라이언트 초기화
        self.rest_client = StandXRestClient(self.auth, self.config.standx.base_url)
        self.ws_client = StandXWebSocket(self.config.standx.ws_url, self.auth)

        return True

    def show_balance(self):
        """잔액 확인"""
        try:
            balance = self.rest_client.get_balance()
            leverage = self.config.strategy.leverage
            margin_reserve = self.config.strategy.margin_reserve_percent / 100

            print(f"\n[잔액]")
            print(f"  Available: ${balance.available:.2f}")
            print(f"  Equity: ${balance.equity:.2f}")
            print(f"  Margin: ${balance.margin:.2f}")
            print(f"  Unrealized PnL: ${balance.unrealized_pnl:.2f}")

            # 레버리지 기반 최대 주문 금액 계산
            # 2+2 전략: BUY 2개 + SELL 2개 = 4개 주문
            usable_margin = balance.available * (1 - margin_reserve) - LIQUIDATION_FEE_RESERVE_USD
            max_notional = usable_margin * leverage if usable_margin > 0 else 0
            max_per_side = max_notional / 4 if max_notional > 0 else 0  # 4개 주문

            print(f"\n[레버리지 설정]")
            print(f"  레버리지: {leverage}x")
            print(f"  마진 예약: {self.config.strategy.margin_reserve_percent}%")
            print(f"  청산 수수료 예약: ${LIQUIDATION_FEE_RESERVE_USD:.2f}")

            print(f"\n[주문 가능 금액] (레버리지 {leverage}x 기준)")
            print(f"  사용 가능 마진: ${usable_margin:.2f}")
            print(f"  최대 총 노출: ${max_notional:.2f}")
            print(f"  주문당 최대 (노출): ${max_per_side:.2f}")

            return balance.available
        except Exception as e:
            print(f"[오류] 잔액 조회 실패: {e}")
            return 0

    def set_order_size(self):
        """주문 크기 설정"""
        # 먼저 잔액 확인
        available = self.show_balance()

        if available <= 0:
            print("[오류] 잔액이 없습니다.")
            return

        # 레버리지 기반 최대 주문 크기 계산
        # 2+2 전략: 4개 주문
        leverage = self.config.strategy.leverage
        margin_reserve = self.config.strategy.margin_reserve_percent / 100
        usable_margin = available * (1 - margin_reserve) - LIQUIDATION_FEE_RESERVE_USD
        max_notional = usable_margin * leverage if usable_margin > 0 else 0
        max_per_side = max_notional / 4 if max_notional > 0 else 0  # 4개 주문

        print(f"\n현재 설정된 주문 크기: ${self.order_size_usd:.2f}")
        print(f"주문 가능 범위 (레버리지 {leverage}x, 2+2전략): $1.00 ~ ${max_per_side:.2f}")

        try:
            size_input = input("\n새 주문 크기 (USD, 취소=Enter): ").strip()
            if not size_input:
                print("취소됨")
                return

            new_size = float(size_input)

            if new_size < 1.0:
                print("[오류] 최소 주문 크기는 $1.00입니다.")
                return

            if new_size > max_per_side:
                print(f"[경고] 설정한 크기(${new_size:.2f})가 최대 가능 금액(${max_per_side:.2f})을 초과합니다.")
                confirm = input("그래도 설정하시겠습니까? (y/n): ").strip().lower()
                if confirm != 'y':
                    print("취소됨")
                    return

            self.order_size_usd = new_size
            print(f"주문 크기가 ${self.order_size_usd:.2f}로 설정되었습니다.")

        except ValueError:
            print("[오류] 유효한 숫자를 입력하세요.")

    def show_settings(self):
        """설정 확인"""
        print(f"\n[현재 설정]")
        print(f"  주문 크기: ${self.order_size_usd:.2f}")
        print(f"  목표 거리: {self.config.strategy.target_distance_bps} bps")
        print(f"  최대 거리: {self.config.strategy.max_distance_bps} bps")
        print(f"  청산 수수료 예약: ${LIQUIDATION_FEE_RESERVE_USD:.2f}")
        print(f"  봇 상태: {'실행 중' if self.bot_running else '정지'}")

    async def start_bot(self):
        """봇 시작"""
        if self.bot_running:
            print("[경고] 봇이 이미 실행 중입니다.")
            return

        # 잔액 확인
        try:
            balance = self.rest_client.get_balance()
            available = balance.available
        except:
            print("[오류] 잔액 확인 실패")
            return

        # 레버리지 기반 최대 주문 크기 확인 (2+2 전략: 4개 주문)
        leverage = self.config.strategy.leverage
        margin_reserve = self.config.strategy.margin_reserve_percent / 100
        usable_margin = available * (1 - margin_reserve) - LIQUIDATION_FEE_RESERVE_USD
        max_notional = usable_margin * leverage if usable_margin > 0 else 0
        max_per_side = max_notional / 4 if max_notional > 0 else 0  # 4개 주문

        if self.order_size_usd > max_per_side:
            print(f"[오류] 주문 크기(${self.order_size_usd:.2f})가 최대 가능 금액(${max_per_side:.2f})을 초과합니다.")
            print(f"       레버리지: {leverage}x, 마진 예약: {self.config.strategy.margin_reserve_percent}%")
            print(f"       주문 크기를 줄이거나 잔액을 늘려주세요.")
            return

        # config에 주문 크기 적용
        self.config.strategy.order_size_usd = self.order_size_usd

        # 새 WebSocket 클라이언트 (재연결용)
        self.ws_client = StandXWebSocket(self.config.standx.ws_url, self.auth)

        # 전략 초기화
        self.strategy = MakerFarmingStrategy(self.config, self.rest_client, self.ws_client)

        print(f"\n봇 시작 중... (주문 크기: ${self.order_size_usd:.2f})")

        try:
            await self.strategy.start()
            self.bot_running = True

            # 백그라운드 태스크로 실행
            self.bot_task = asyncio.create_task(self._run_bot())

            print("봇이 시작되었습니다!")

        except Exception as e:
            print(f"[오류] 봇 시작 실패: {e}")
            self.bot_running = False

    async def _run_bot(self):
        """봇 실행 (백그라운드)"""
        try:
            await self.strategy.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"봇 실행 오류: {e}")
        finally:
            self.bot_running = False

    async def stop_bot(self):
        """봇 정지"""
        if not self.bot_running:
            print("[경고] 봇이 실행 중이 아닙니다.")
            return

        print("봇 정지 중...")

        # 태스크 취소
        if self.bot_task:
            self.bot_task.cancel()
            try:
                await self.bot_task
            except asyncio.CancelledError:
                pass

        # 전략 정지
        if self.strategy:
            await self.strategy.stop()

        self.bot_running = False
        print("봇이 정지되었습니다.")

    def show_status(self):
        """상태 확인"""
        if not self.strategy:
            print("[알림] 봇이 아직 시작되지 않았습니다.")
            return

        print_status(self.strategy)

    async def run_monitor_mode(self):
        """모니터링 모드 실행"""
        if self.bot_running:
            print("[경고] 먼저 봇을 정지하세요. 모니터링 모드는 독립적으로 실행됩니다.")
            return

        # 모니터링 모드 임포트 및 실행
        try:
            from standx_maker_bot.monitor import MonitorUI
            from rich.console import Console
            from rich.live import Live

            console = Console()

            # config에 주문 크기 적용
            self.config.strategy.order_size_usd = self.order_size_usd

            # 새 WebSocket 클라이언트 (재연결용)
            self.ws_client = StandXWebSocket(self.config.standx.ws_url, self.auth)

            # 전략 초기화
            self.strategy = MakerFarmingStrategy(self.config, self.rest_client, self.ws_client)

            # 모니터 UI 초기화
            monitor = MonitorUI(self.strategy, self.config, self.rest_client)

            console.print("[bold blue]모니터링 모드 시작...[/bold blue]")
            console.print(f"주문 크기: ${self.order_size_usd:.2f}")
            console.print("[yellow]'s' 키: 봇 중지 → 메뉴 복귀 | Ctrl+C: 프로그램 종료[/yellow]")

            # 전략 시작
            await self.strategy.start()
            self.bot_running = True

            # 봇 실행 태스크
            self.bot_task = asyncio.create_task(self._run_bot())

            # Live 디스플레이
            stop_requested = False
            with Live(monitor.generate_display(), console=console, refresh_per_second=2, screen=True) as live:
                try:
                    while self.bot_running:
                        # 키보드 입력 체크 (non-blocking)
                        if msvcrt.kbhit():
                            key = msvcrt.getch()
                            # 's' 또는 'S' 키로 중지
                            if key in (b's', b'S', b'q', b'Q'):
                                stop_requested = True
                                break

                        # 상태 업데이트
                        status = self.strategy.get_status()

                        # 마지막 액션 업데이트
                        if status['stats']['rebalances'] > 0:
                            monitor.update_last_action(f"Rebalanced {status['stats']['rebalances']} times")

                        # 디스플레이 갱신
                        live.update(monitor.generate_display())

                        # 봇 태스크 체크
                        if self.bot_task and self.bot_task.done():
                            break

                        await asyncio.sleep(0.5)

                except KeyboardInterrupt:
                    pass

            # 정리
            await self.stop_bot()
            if stop_requested:
                console.print("[green]봇 중지됨 → 메뉴로 복귀합니다[/green]")
            else:
                console.print("[yellow]모니터링 모드 종료[/yellow]")

        except ImportError as e:
            print(f"[오류] rich 라이브러리가 필요합니다: pip install rich")
            print(f"상세: {e}")
        except Exception as e:
            print(f"[오류] 모니터링 모드 실행 실패: {e}")
            import traceback
            traceback.print_exc()
            if self.bot_running:
                await self.stop_bot()

    async def run(self):
        """메인 루프"""
        print_banner()

        if not await self.initialize():
            return

        # 초기 잔액 확인
        self.show_balance()

        print_menu()

        while True:
            try:
                # 비동기 입력 처리
                cmd = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\n명령> ").strip().lower()
                )

                if cmd == '1':
                    self.set_order_size()
                elif cmd == '2':
                    self.show_balance()
                elif cmd == '3':
                    await self.start_bot()
                elif cmd == '4':
                    await self.stop_bot()
                elif cmd == '5':
                    self.show_status()
                elif cmd == '6':
                    self.show_settings()
                elif cmd == '7':
                    await self.run_monitor_mode()
                    print_menu()  # 모니터링 종료 후 메뉴 다시 표시
                elif cmd == 'q':
                    if self.bot_running:
                        await self.stop_bot()
                    print("종료합니다.")
                    break
                elif cmd == 'h' or cmd == '?':
                    print_menu()
                else:
                    print("알 수 없는 명령입니다. 'h'로 도움말을 확인하세요.")

            except KeyboardInterrupt:
                print("\n\n인터럽트 감지...")
                if self.bot_running:
                    await self.stop_bot()
                break
            except Exception as e:
                print(f"[오류] {e}")


def main():
    import logging
    setup_logger('standx_bot', logging.INFO)

    bot = InteractiveBot()
    asyncio.run(bot.run())


if __name__ == '__main__':
    main()
