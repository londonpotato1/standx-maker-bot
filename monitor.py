#!/usr/bin/env python3
"""
StandX Maker Bot - 실시간 모니터링 UI
rich 라이브러리 기반 터미널 대시보드
"""
import asyncio
import time
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich import box

from standx_maker_bot.api.auth import StandXAuth
from standx_maker_bot.api.rest_client import StandXRestClient
from standx_maker_bot.api.websocket_client import StandXWebSocket
from standx_maker_bot.api.binance_ws_client import BinanceWebSocket
from standx_maker_bot.strategy.maker_farming import MakerFarmingStrategy
from standx_maker_bot.utils.config import Config
from standx_maker_bot.utils.logger import setup_logger, get_logger

console = Console()


class MonitorUI:
    """실시간 모니터링 UI"""

    def __init__(self, strategy: MakerFarmingStrategy, config: Config, rest_client: StandXRestClient):
        self.strategy = strategy
        self.config = config
        self.rest_client = rest_client
        self.logger = get_logger('monitor')

        self.start_time = time.time()
        self.today_fills = 0
        self.today_pnl = 0.0
        self.last_action = "Starting..."
        self._running = False

    def _format_time(self, seconds: float) -> str:
        """초를 HH:MM:SS 형식으로 변환"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _get_header(self) -> Panel:
        """헤더 패널 생성"""
        status = self.strategy.get_status()
        runtime = time.time() - self.start_time

        # 심볼 및 타겟 거리
        symbols = self.config.strategy.symbols
        symbol_str = ", ".join(symbols)
        distances = self.config.strategy.order_distances_bps
        target_str = f"±[{', '.join(str(d) for d in distances)}]bps"

        # 상태 표시
        if status['emergency_stopped']:
            state_text = Text("EMERGENCY STOP", style="bold red blink")
        elif status['running']:
            state_text = Text("[LIVE]", style="bold green")
        else:
            state_text = Text("[STOPPED]", style="bold yellow")

        header = Table.grid(expand=True)
        header.add_column(justify="left", ratio=1)
        header.add_column(justify="center", ratio=1)
        header.add_column(justify="right", ratio=1)

        header.add_row(
            Text(f"Symbol: {symbol_str}", style="cyan"),
            Text(f"Time: {self._format_time(runtime)}", style="white"),
            Text(f"Target: {target_str}", style="cyan"),
        )

        title = Text("StandX Market Making ", style="bold white")
        title.append_text(state_text)

        return Panel(header, title=title, border_style="blue", box=box.ROUNDED)

    def _get_account_section(self) -> Panel:
        """ACCOUNT 섹션"""
        status = self.strategy.get_status()
        stats = status['stats']

        # 잔액 조회
        try:
            balance = self.rest_client.get_balance()
            available = balance.available
            equity = balance.equity
        except:
            available = 0.0
            equity = 0.0

        # 포지션 확인
        try:
            positions = self.rest_client.get_positions()
            pos_text = "No position"
            pos_style = "green"
            for pos in positions:
                if pos.size > 0:
                    pos_text = f"{pos.side.upper()} {pos.size} @ ${pos.entry_price:,.2f}"
                    pos_style = "red" if pos.unrealized_pnl < 0 else "green"
                    break
        except:
            pos_text = "No position"
            pos_style = "green"

        # 주문 크기 계산
        order_size = self.config.strategy.order_size_usd
        num_orders = self.config.strategy.num_orders_per_side * 2

        # 총 노출 금액
        total_notional = 0.0
        for sym_status in status['symbols'].values():
            total_notional += sym_status.get('total_notional', 0)

        content = Table.grid(expand=True, padding=(0, 1))
        content.add_column(justify="left")
        content.add_column(justify="left")
        content.add_column(justify="left")
        content.add_column(justify="left")

        # Row 1: DUSD, Today 체결, PnL
        content.add_row(
            Text(f"DUSD: ${available:.2f}", style="cyan"),
            Text(f"Today: {stats['fills']}회 체결", style="yellow" if stats['fills'] > 0 else "white"),
            Text("|", style="dim"),
            Text(f"PnL: ${self.today_pnl:+.2f}", style="green" if self.today_pnl >= 0 else "red"),
        )

        # Row 2: Order Size, Position
        content.add_row(
            Text(f"Order Size: ${order_size:.2f} x{num_orders}", style="white"),
            Text(f"Total Notional: ${total_notional:,.2f}", style="white"),
            Text("", style="dim"),
            Text("", style="dim"),
        )

        # 포지션 홀딩 상태 표시
        if status.get('holding_position') and status.get('held_position'):
            hp = status['held_position']
            pnl_style = "green" if hp['pnl_pct'] >= 0 else "red"
            remaining = hp['timeout_seconds'] - hp['elapsed_seconds']
            content.add_row(
                Text(f"★ HOLDING: {hp['side']} @ ${hp['entry_price']:,.2f}", style="bold yellow"),
                Text(f"PnL: {hp['pnl_pct']:+.2f}%", style=pnl_style),
                Text(f"Time: {remaining:.0f}s", style="cyan"),
                Text("(±1% 청산 대기)", style="dim"),
            )
        else:
            content.add_row(
                Text(f"Position: {pos_text}", style=pos_style),
                Text("", style="dim"),
                Text("", style="dim"),
                Text("", style="dim"),
            )

        return Panel(content, title="[yellow]ACCOUNT[/yellow]", border_style="yellow", box=box.ROUNDED)

    def _get_market_data_section(self) -> Panel:
        """MARKET DATA 섹션"""
        status = self.strategy.get_status()

        content = Table.grid(expand=True, padding=(0, 1))
        content.add_column(justify="left")
        content.add_column(justify="left")

        for symbol, sym_status in status['symbols'].items():
            mark_price = sym_status.get('mark_price', 0)
            mid_price = sym_status.get('mid_price', 0)
            reference_price = sym_status.get('reference_price', 0)
            spread_bps = sym_status.get('spread_bps', 0)
            volatility_bps = sym_status.get('volatility_bps', 0)

            # Binance mark price 가져오기
            binance_mark_price = 0
            binance_change_bps = 0
            try:
                binance_data = self.strategy.binance_ws.get_mark_price(symbol)
                if binance_data:
                    binance_mark_price = binance_data.mark_price
                binance_change_bps = self.strategy.binance_ws.get_price_change_bps(symbol, 0.5)
            except:
                pass

            # Mark-Mid 괴리 계산
            if mid_price > 0 and mark_price > 0:
                mark_mid_bps = abs(mark_price - mid_price) / mid_price * 10000
            else:
                mark_mid_bps = 0

            # StandX-Binance 괴리 계산
            if binance_mark_price > 0 and mark_price > 0:
                standx_binance_bps = (mark_price - binance_mark_price) / binance_mark_price * 10000
            else:
                standx_binance_bps = 0

            # Best Bid/Ask 추정 (mid price 기반)
            if mid_price > 0 and spread_bps > 0:
                half_spread = mid_price * spread_bps / 20000
                best_bid = mid_price - half_spread
                best_ask = mid_price + half_spread
            else:
                best_bid = mid_price
                best_ask = mid_price

            content.add_row(
                Text(f"[{symbol}]", style="bold cyan"),
                Text("", style="dim"),
            )
            content.add_row(
                Text(f"  Mark Price: ${mark_price:,.2f}", style="white"),
                Text(f"  Reference: ${reference_price:,.2f}", style="dim"),
            )
            content.add_row(
                Text(f"  Best Bid: ${best_bid:,.2f}", style="green"),
                Text(f"  Best Ask: ${best_ask:,.2f}", style="red"),
            )
            content.add_row(
                Text(f"  OB Spread: {spread_bps:.2f} bps", style="white"),
                Text(f"  Volatility: {volatility_bps:.2f} bps", style="white"),
            )
            content.add_row(
                Text(f"  Mark-Mid: {mark_mid_bps:.2f} bps", style="yellow" if mark_mid_bps > 3 else "white"),
                Text(f"  (limit: 5.0 bps)", style="dim"),
            )

            # Binance 정보 추가
            if binance_mark_price > 0:
                change_style = "green" if binance_change_bps >= 0 else "red"
                content.add_row(
                    Text(f"  [Binance] ${binance_mark_price:,.2f}", style="magenta"),
                    Text(f"  Δ{binance_change_bps:+.2f}bps/0.5s", style=change_style),
                )
                lag_style = "yellow" if abs(standx_binance_bps) > 2 else "dim"
                content.add_row(
                    Text(f"  StandX-Binance: {standx_binance_bps:+.2f} bps", style=lag_style),
                    Text("", style="dim"),
                )

        return Panel(content, title="[cyan]MARKET DATA[/cyan]", border_style="cyan", box=box.ROUNDED)

    def _get_orders_section(self) -> Panel:
        """ORDERS 섹션"""
        status = self.strategy.get_status()

        content = Table.grid(expand=True, padding=(0, 1))
        content.add_column(justify="left", width=70)

        for symbol, sym_status in status['symbols'].items():
            reference_price = sym_status.get('reference_price', 0)

            # SELL 주문들 (위에서 아래로: 먼 거리 → 가까운 거리)
            sell_orders = sym_status.get('sell_orders', [])
            sell_orders_sorted = sorted(
                [(i, o) for i, o in enumerate(sell_orders) if o],
                key=lambda x: x[1]['price'] if x[1] else 0,
                reverse=True
            )

            for i, order in sell_orders_sorted:
                if order:
                    price = order['price']
                    status_str = order['status']
                    distance = self.config.strategy.order_distances_bps[i] if i < len(self.config.strategy.order_distances_bps) else 0

                    # Drift 계산
                    if reference_price > 0:
                        actual_dist = abs(price - reference_price) / reference_price * 10000
                        drift = abs(actual_dist - distance)
                    else:
                        drift = 0

                    # 상태 색상
                    if status_str == 'OPEN':
                        status_icon = "[green]●[/green]"
                        status_text = "OPEN"
                    elif status_str == 'PENDING':
                        status_icon = "[yellow]●[/yellow]"
                        status_text = "PENDING"
                    else:
                        status_icon = "[red]●[/red]"
                        status_text = status_str

                    content.add_row(
                        Text.from_markup(
                            f"  [red]SELL[/red] {distance}bps: {status_icon} {status_text}  "
                            f"@ ${price:,.2f} (drift: {drift:.1f}bps)  [dim][MAKER][/dim]"
                        ),
                    )

            # 구분선
            content.add_row(Text("  " + "-" * 60, style="dim"))

            # BUY 주문들 (위에서 아래로: 가까운 거리 → 먼 거리)
            buy_orders = sym_status.get('buy_orders', [])
            buy_orders_sorted = sorted(
                [(i, o) for i, o in enumerate(buy_orders) if o],
                key=lambda x: x[1]['price'] if x[1] else 0,
                reverse=True
            )

            for i, order in buy_orders_sorted:
                if order:
                    price = order['price']
                    status_str = order['status']
                    distance = self.config.strategy.order_distances_bps[i] if i < len(self.config.strategy.order_distances_bps) else 0

                    # Drift 계산
                    if reference_price > 0:
                        actual_dist = abs(price - reference_price) / reference_price * 10000
                        drift = abs(actual_dist - distance)
                    else:
                        drift = 0

                    # 상태 색상
                    if status_str == 'OPEN':
                        status_icon = "[green]●[/green]"
                        status_text = "OPEN"
                    elif status_str == 'PENDING':
                        status_icon = "[yellow]●[/yellow]"
                        status_text = "PENDING"
                    else:
                        status_icon = "[red]●[/red]"
                        status_text = status_str

                    content.add_row(
                        Text.from_markup(
                            f"  [green]BUY[/green]  {distance}bps: {status_icon} {status_text}  "
                            f"@ ${price:,.2f} (drift: {drift:.1f}bps)  [dim][MAKER][/dim]"
                        ),
                    )

        return Panel(content, title="[white]ORDERS[/white]", border_style="white", box=box.ROUNDED)

    def _get_status_section(self) -> Panel:
        """STATUS 섹션"""
        status = self.strategy.get_status()

        content = Table.grid(expand=True, padding=(0, 1))
        content.add_column(justify="left")
        content.add_column(justify="left")

        # 상태 아이콘
        if status['emergency_stopped']:
            status_icon = "[red]●[/red]"
            status_text = "EMERGENCY STOPPED"
        elif status['running']:
            status_icon = "[green]●[/green]"
            status_text = "MONITORING - Orders active"
        else:
            status_icon = "[yellow]●[/yellow]"
            status_text = "STOPPED"

        content.add_row(
            Text.from_markup(f"  {status_icon} {status_text}"),
            Text("", style="dim"),
        )
        content.add_row(
            Text(f"  Last: {self.last_action}", style="dim"),
            Text("", style="dim"),
        )

        # Fill Protection 상태
        try:
            fp_stats = self.strategy.fill_protection.get_stats()
            fp_config = self.config.fill_protection

            # Binance 보호 상태
            binance_status = "[green]ON[/green]" if fp_config.binance.enabled else "[red]OFF[/red]"
            queue_status = "[green]ON[/green]" if fp_config.queue.enabled else "[red]OFF[/red]"

            content.add_row(
                Text("", style="dim"),
                Text("", style="dim"),
            )
            content.add_row(
                Text.from_markup(f"  [magenta]Fill Protection[/magenta]: Binance {binance_status} | Queue {queue_status}"),
                Text("", style="dim"),
            )
            content.add_row(
                Text(f"    Binance triggers: {fp_stats['binance_triggers']}", style="cyan"),
                Text(f"  Queue triggers: {fp_stats['queue_triggers']}", style="cyan"),
            )
            content.add_row(
                Text(f"    Protected cancels: {fp_stats['orders_cancelled']}", style="yellow"),
                Text(f"  Trigger: {fp_config.binance.trigger_bps}bps", style="dim"),
            )
        except:
            pass

        return Panel(content, title="[magenta]STATUS[/magenta]", border_style="magenta", box=box.ROUNDED)

    def _get_footer(self) -> Text:
        """하단 통계"""
        status = self.strategy.get_status()
        stats = status['stats']
        runtime = time.time() - self.start_time

        footer = Text()
        footer.append("Placed: ", style="dim")
        footer.append(f"{stats['orders_placed']}", style="white")
        footer.append("  Cancelled: ", style="dim")
        footer.append(f"{stats['orders_cancelled']}", style="white")
        footer.append("  Rebalanced: ", style="dim")
        footer.append(f"{stats['rebalances']}", style="white")
        footer.append("  Runtime: ", style="dim")
        footer.append(self._format_time(runtime), style="white")
        footer.append("\n")
        # 체결 관련 통계
        footer.append("Fills: ", style="dim")
        footer.append(f"{stats['fills']}", style="yellow" if stats['fills'] > 0 else "white")
        footer.append("  TP: ", style="dim")
        footer.append(f"{stats.get('take_profits', 0)}", style="green")
        footer.append("  SL: ", style="dim")
        footer.append(f"{stats.get('stop_losses', 0)}", style="red")
        footer.append("  Timeout: ", style="dim")
        footer.append(f"{stats.get('timeouts', 0)}", style="cyan")
        footer.append("  |  ", style="dim")
        footer.append("[s] 중지→메뉴  [Ctrl+C] 종료", style="dim")

        return footer

    def generate_display(self) -> Layout:
        """전체 레이아웃 생성"""
        layout = Layout()

        # 메인 레이아웃 구성
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )

        # Body 분할
        layout["body"].split_column(
            Layout(name="account", size=6),
            Layout(name="market", size=12),  # Binance 데이터 추가로 확장
            Layout(name="orders", size=10),
            Layout(name="status", size=9),   # Fill Protection 정보 추가로 확장
        )

        # 각 섹션 설정
        layout["header"].update(self._get_header())
        layout["account"].update(self._get_account_section())
        layout["market"].update(self._get_market_data_section())
        layout["orders"].update(self._get_orders_section())
        layout["status"].update(self._get_status_section())
        layout["footer"].update(Panel(self._get_footer(), border_style="dim", box=box.ROUNDED))

        return layout

    def update_last_action(self, action: str):
        """마지막 액션 업데이트"""
        self.last_action = action


async def run_monitor(config_path: str = "config.yaml"):
    """모니터링 모드 실행"""
    import logging
    setup_logger('standx_bot', logging.WARNING)  # 로그 레벨 낮춤
    logger = get_logger('monitor')

    console.print("[bold blue]StandX Maker Bot - Monitor Mode[/bold blue]")
    console.print("초기화 중...")

    # 설정 로드
    config = Config.load(config_path)
    errors = config.validate()
    if errors:
        for error in errors:
            console.print(f"[red]설정 오류: {error}[/red]")
        return 1

    # 인증
    console.print("인증 중...")
    auth = StandXAuth(
        wallet_address=config.wallet.address,
        wallet_private_key=config.wallet.private_key,
        chain=config.standx.chain,
    )

    try:
        auth.authenticate()
        console.print(f"[green]인증 성공: {config.wallet.address[:10]}...[/green]")
    except Exception as e:
        console.print(f"[red]인증 실패: {e}[/red]")
        return 1

    # 클라이언트 초기화
    rest_client = StandXRestClient(auth, config.standx.base_url)
    ws_client = StandXWebSocket(config.standx.ws_url, auth)

    # 전략 초기화
    strategy = MakerFarmingStrategy(config, rest_client, ws_client)

    # 모니터 UI 초기화
    monitor = MonitorUI(strategy, config, rest_client)

    console.print("봇 시작 중...")

    try:
        # 전략 시작
        await strategy.start()

        # 봇 실행 태스크
        bot_task = asyncio.create_task(strategy.run())

        # Live 디스플레이
        with Live(monitor.generate_display(), console=console, refresh_per_second=2, screen=True) as live:
            try:
                while True:
                    # 상태 업데이트
                    status = strategy.get_status()

                    # 마지막 액션 업데이트
                    if status['stats']['rebalances'] > 0:
                        monitor.update_last_action(f"Rebalanced {status['stats']['rebalances']} times")

                    # 디스플레이 갱신
                    live.update(monitor.generate_display())

                    # 봇 태스크 체크
                    if bot_task.done():
                        break

                    await asyncio.sleep(0.5)

            except KeyboardInterrupt:
                pass

        # 정리
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    except Exception as e:
        console.print(f"[red]오류: {e}[/red]")
        import traceback
        traceback.print_exc()

    finally:
        await strategy.stop()
        console.print("[yellow]봇 종료됨[/yellow]")

    return 0


def main():
    """메인 함수"""
    import argparse

    parser = argparse.ArgumentParser(description="StandX Maker Bot Monitor")
    parser.add_argument('-c', '--config', default='config.yaml', help='설정 파일 경로')
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(run_monitor(args.config))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]중단됨[/yellow]")
        sys.exit(130)


if __name__ == '__main__':
    main()
