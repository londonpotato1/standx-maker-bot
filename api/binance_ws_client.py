"""
Binance Futures WebSocket 클라이언트

StandX보다 100-500ms 빠른 mark price 선행 감지용
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from ..utils.logger import get_logger

logger = get_logger('binance_ws')


@dataclass
class BinanceMarkPrice:
    """Binance Mark Price 데이터"""
    symbol: str              # BTCUSDT
    mark_price: float        # Mark price
    index_price: float       # Index price
    funding_rate: float      # Funding rate
    next_funding_time: int   # Next funding timestamp
    timestamp: float         # Event time (seconds)
    received_at: float       # 로컬 수신 시간


# 콜백 타입
MarkPriceCallback = Callable[[BinanceMarkPrice], None]


class BinanceWebSocket:
    """
    Binance Futures WebSocket 클라이언트

    목적:
    - Mark price 실시간 수신 (1초 또는 3초 간격)
    - StandX mark price보다 선행하여 체결 위험 사전 감지

    스트림:
    - btcusdt@markPrice: 3초 간격 (기본)
    - btcusdt@markPrice@1s: 1초 간격 (더 빠름, 트래픽 증가)
    """

    WS_URL = "wss://fstream.binance.com/ws"

    def __init__(self, use_1s_stream: bool = True):
        """
        Args:
            use_1s_stream: True면 1초 간격 스트림, False면 3초 간격
        """
        self.use_1s_stream = use_1s_stream
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._callbacks: List[MarkPriceCallback] = []
        self._price_cache: Dict[str, BinanceMarkPrice] = {}

        # 가격 히스토리 (변동 감지용)
        self._price_history: Dict[str, Deque[tuple]] = {}  # symbol -> deque of (time, price)
        self._history_max_len = 100

        # 재연결
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 10.0
        self._subscribed_symbols: List[str] = []

        # 심볼 매핑 (StandX -> Binance)
        self._symbol_map = {
            "BTC-USD": "BTCUSDT",
            "ETH-USD": "ETHUSDT",
            "SOL-USD": "SOLUSDT",
        }
        # 역방향 매핑
        self._reverse_symbol_map = {v: k for k, v in self._symbol_map.items()}

    # ========== Callback Registration ==========

    def on_mark_price(self, callback: MarkPriceCallback):
        """Mark price 콜백 등록"""
        self._callbacks.append(callback)

    # ========== Cache Access ==========

    def get_mark_price(self, standx_symbol: str) -> Optional[BinanceMarkPrice]:
        """
        캐시된 mark price 조회

        Args:
            standx_symbol: StandX 심볼 (예: BTC-USD)

        Returns:
            BinanceMarkPrice 또는 None
        """
        binance_symbol = self._symbol_map.get(standx_symbol, standx_symbol)
        return self._price_cache.get(binance_symbol)

    def get_price_change_bps(self, standx_symbol: str, window_seconds: float = 0.5) -> float:
        """
        지정된 시간 윈도우 내 가격 변동 (bps)

        Args:
            standx_symbol: StandX 심볼
            window_seconds: 측정 윈도우 (초)

        Returns:
            변동 bps (양수=상승, 음수=하락)
        """
        binance_symbol = self._symbol_map.get(standx_symbol, standx_symbol)
        history = self._price_history.get(binance_symbol)

        if not history or len(history) < 2:
            return 0.0

        now = time.time()

        # 윈도우 내 가장 오래된 가격과 최신 가격
        oldest_in_window = None
        for ts, price in history:
            if now - ts <= window_seconds:
                if oldest_in_window is None:
                    oldest_in_window = (ts, price)

        if oldest_in_window is None:
            return 0.0

        oldest_price = oldest_in_window[1]
        newest_price = history[-1][1]

        if oldest_price <= 0:
            return 0.0

        return (newest_price - oldest_price) / oldest_price * 10000

    def convert_symbol(self, standx_symbol: str) -> str:
        """StandX 심볼을 Binance 심볼로 변환"""
        return self._symbol_map.get(standx_symbol, standx_symbol)

    def convert_symbol_reverse(self, binance_symbol: str) -> str:
        """Binance 심볼을 StandX 심볼로 변환"""
        return self._reverse_symbol_map.get(binance_symbol, binance_symbol)

    # ========== Connection Management ==========

    async def connect(self):
        """WebSocket 연결"""
        logger.info(f"Binance WebSocket 연결 중: {self.WS_URL}")

        try:
            self._ws = await websockets.connect(
                self.WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            self._reconnect_delay = 1.0

            logger.info("Binance WebSocket 연결 성공")

        except Exception as e:
            logger.error(f"Binance WebSocket 연결 실패: {e}")
            raise

    async def disconnect(self):
        """WebSocket 연결 종료"""
        self._running = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("Binance WebSocket 연결 종료")

    async def _reconnect(self):
        """재연결"""
        logger.warning(f"Binance 재연결 시도 ({self._reconnect_delay}초 후)...")
        await asyncio.sleep(self._reconnect_delay)

        # 지수 백오프
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        try:
            await self.connect()

            # 이전 구독 복원
            if self._subscribed_symbols:
                await self.subscribe(self._subscribed_symbols)

        except Exception as e:
            logger.error(f"Binance 재연결 실패: {e}")

    # ========== Subscription ==========

    async def subscribe(self, standx_symbols: List[str]):
        """
        Mark price 스트림 구독

        Args:
            standx_symbols: StandX 심볼 목록 (예: ["BTC-USD", "ETH-USD"])
        """
        if not self._ws:
            raise RuntimeError("Binance WebSocket이 연결되지 않았습니다")

        self._subscribed_symbols = standx_symbols

        # 스트림 목록 생성
        streams = []
        for symbol in standx_symbols:
            binance_symbol = self._symbol_map.get(symbol, symbol).lower()

            if self.use_1s_stream:
                streams.append(f"{binance_symbol}@markPrice@1s")
            else:
                streams.append(f"{binance_symbol}@markPrice")

        # 구독 메시지
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1
        }

        await self._ws.send(json.dumps(subscribe_msg))
        logger.info(f"Binance mark price 구독: {streams}")

    # ========== Message Handling ==========

    def _handle_mark_price(self, data: dict):
        """Mark price 메시지 처리"""
        # 응답 형식:
        # {
        #   "e": "markPriceUpdate",
        #   "E": 1591702613943,  # Event time (ms)
        #   "s": "BTCUSDT",      # Symbol
        #   "p": "50500.00",     # Mark price
        #   "i": "50450.00",     # Index price
        #   "P": "50525.00",     # Estimated settle price
        #   "r": "0.0001",       # Funding rate
        #   "T": 1591718400000   # Next funding time
        # }

        symbol = data.get("s", "")
        mark_price = float(data.get("p", 0))

        mark_data = BinanceMarkPrice(
            symbol=symbol,
            mark_price=mark_price,
            index_price=float(data.get("i", 0)),
            funding_rate=float(data.get("r", 0)),
            next_funding_time=int(data.get("T", 0)),
            timestamp=data.get("E", 0) / 1000,  # ms -> seconds
            received_at=time.time(),
        )

        # 캐시 업데이트
        self._price_cache[symbol] = mark_data

        # 히스토리 업데이트
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=self._history_max_len)
        self._price_history[symbol].append((time.time(), mark_price))

        # 콜백 호출
        for callback in self._callbacks:
            try:
                callback(mark_data)
            except Exception as e:
                logger.error(f"Binance mark price 콜백 오류: {e}")

    def _is_connected(self) -> bool:
        """WebSocket 연결 상태 확인"""
        if not self._ws:
            return False
        try:
            return self._ws.close_code is None
        except AttributeError:
            return True

    # ========== Main Loop ==========

    async def run(self):
        """메인 수신 루프"""
        while self._running:
            try:
                if not self._is_connected():
                    await self._reconnect()
                    continue

                try:
                    message = await asyncio.wait_for(
                        self._ws.recv(),
                        timeout=30.0
                    )

                    data = json.loads(message)

                    # mark price 업데이트
                    if data.get("e") == "markPriceUpdate":
                        self._handle_mark_price(data)
                    # 구독 확인
                    elif "result" in data:
                        logger.debug(f"Binance 구독 응답: {data}")

                except asyncio.TimeoutError:
                    # 타임아웃 시 재연결
                    logger.warning("Binance 수신 타임아웃")
                    await self._reconnect()

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Binance WebSocket 연결 끊김")
                if self._running:
                    await self._reconnect()

            except Exception as e:
                logger.error(f"Binance 수신 루프 오류: {e}")
                if self._running:
                    await asyncio.sleep(1)

    async def start(self, symbols: List[str]):
        """
        WebSocket 시작 및 구독

        Args:
            symbols: 구독할 심볼 목록 (StandX 심볼)
        """
        await self.connect()
        await self.subscribe(symbols)

    async def stop(self):
        """WebSocket 중지"""
        await self.disconnect()
