"""
StandX WebSocket 클라이언트
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.client import WebSocketClientProtocol

try:
    from api.auth import StandXAuth
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.api.auth import StandXAuth
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('websocket')


class Channel(Enum):
    """WebSocket 채널"""
    PRICE = "price"
    DEPTH_BOOK = "depth_book"
    PUBLIC_TRADE = "public_trade"
    ORDER = "order"
    POSITION = "position"
    BALANCE = "balance"
    TRADE = "trade"


@dataclass
class PriceData:
    """가격 데이터"""
    symbol: str
    index_price: float
    mark_price: float
    last_price: float
    mid_price: float
    best_bid: float
    best_ask: float
    spread_bps: float
    timestamp: float


@dataclass
class OrderbookData:
    """오더북 데이터"""
    symbol: str
    bids: List[List[float]]  # [[price, qty], ...]
    asks: List[List[float]]
    timestamp: float
    sequence: int

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return 0


@dataclass
class OrderUpdate:
    """주문 업데이트"""
    order_id: str
    cl_ord_id: str
    symbol: str
    side: str
    status: str
    price: float
    quantity: float
    filled_qty: float
    timestamp: float


# 콜백 타입
PriceCallback = Callable[[PriceData], None]
OrderbookCallback = Callable[[OrderbookData], None]
OrderCallback = Callable[[OrderUpdate], None]


class StandXWebSocket:
    """
    StandX WebSocket 클라이언트

    실시간 가격, 오더북, 주문 업데이트 수신
    """

    def __init__(
        self,
        ws_url: str = "wss://perps.standx.com/ws-stream/v1",
        auth: Optional[StandXAuth] = None,
    ):
        """
        Args:
            ws_url: WebSocket URL
            auth: 인증 관리자 (private 채널용)
        """
        self.ws_url = ws_url
        self.auth = auth

        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._subscribed: Set[str] = set()

        # 콜백
        self._price_callbacks: List[PriceCallback] = []
        self._orderbook_callbacks: List[OrderbookCallback] = []
        self._order_callbacks: List[OrderCallback] = []

        # 캐시
        self._price_cache: Dict[str, PriceData] = {}
        self._orderbook_cache: Dict[str, OrderbookData] = {}

        # 재연결 (업타임 개선: 최대 10초로 제한)
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 10.0  # 60초→10초
        self._last_pong = time.time()

    # ========== Callback Registration ==========

    def on_price(self, callback: PriceCallback):
        """가격 콜백 등록"""
        self._price_callbacks.append(callback)

    def on_orderbook(self, callback: OrderbookCallback):
        """오더북 콜백 등록"""
        self._orderbook_callbacks.append(callback)

    def on_order(self, callback: OrderCallback):
        """주문 콜백 등록"""
        self._order_callbacks.append(callback)

    # ========== Cache Access ==========

    def get_price(self, symbol: str) -> Optional[PriceData]:
        """캐시된 가격 가져오기"""
        return self._price_cache.get(symbol)

    def get_orderbook(self, symbol: str) -> Optional[OrderbookData]:
        """캐시된 오더북 가져오기"""
        return self._orderbook_cache.get(symbol)

    def get_mid_price(self, symbol: str) -> float:
        """Mid price 가져오기"""
        price = self._price_cache.get(symbol)
        if price:
            return price.mid_price

        orderbook = self._orderbook_cache.get(symbol)
        if orderbook:
            return orderbook.mid_price

        return 0

    def get_best_bid(self, symbol: str) -> float:
        """Best bid 가져오기"""
        orderbook = self._orderbook_cache.get(symbol)
        if orderbook:
            return orderbook.best_bid

        price = self._price_cache.get(symbol)
        if price:
            return price.best_bid

        return 0

    def get_best_ask(self, symbol: str) -> float:
        """Best ask 가져오기"""
        orderbook = self._orderbook_cache.get(symbol)
        if orderbook:
            return orderbook.best_ask

        price = self._price_cache.get(symbol)
        if price:
            return price.best_ask

        return 0

    # ========== Connection Management ==========

    async def connect(self):
        """WebSocket 연결"""
        logger.info(f"WebSocket 연결 중: {self.ws_url}")

        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            self._reconnect_delay = 1.0
            self._last_pong = time.time()

            logger.info("WebSocket 연결 성공")

        except Exception as e:
            logger.error(f"WebSocket 연결 실패: {e}")
            raise

    async def disconnect(self):
        """WebSocket 연결 종료"""
        self._running = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._subscribed.clear()
        logger.info("WebSocket 연결 종료")

    async def _reconnect(self):
        """재연결"""
        logger.warning(f"재연결 시도 ({self._reconnect_delay}초 후)...")
        await asyncio.sleep(self._reconnect_delay)

        # 지수 백오프
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        try:
            await self.connect()

            # 이전 구독 복원
            subscriptions = list(self._subscribed)
            self._subscribed.clear()

            for sub in subscriptions:
                parts = sub.split(':')
                if len(parts) == 2:
                    channel, symbol = parts
                    await self.subscribe(channel, symbol)

        except Exception as e:
            logger.error(f"재연결 실패: {e}")

    # ========== Subscription ==========

    async def subscribe(self, channel: str, symbol: str):
        """
        채널 구독

        Args:
            channel: 채널명 (price, depth_book, order 등)
            symbol: 심볼
        """
        if not self._ws:
            raise RuntimeError("WebSocket이 연결되지 않았습니다")

        sub_key = f"{channel}:{symbol}"
        if sub_key in self._subscribed:
            return

        # private 채널은 인증 필요
        if channel in ['order', 'position', 'balance', 'trade']:
            if not self.auth:
                raise ValueError(f"'{channel}' 채널은 인증이 필요합니다")
            token = self.auth.get_token()
            # StandX auth format for private channels
            message = {
                "auth": {
                    "token": token.token,
                    "streams": [{"channel": channel, "symbol": symbol}]
                }
            }
        else:
            # StandX subscribe format for public channels
            message = {
                "subscribe": {
                    "channel": channel,
                    "symbol": symbol,
                }
            }

        await self._ws.send(json.dumps(message))
        self._subscribed.add(sub_key)

        logger.debug(f"구독: {channel}:{symbol}")

    async def unsubscribe(self, channel: str, symbol: str):
        """
        구독 해제

        Args:
            channel: 채널명
            symbol: 심볼
        """
        if not self._ws:
            return

        sub_key = f"{channel}:{symbol}"
        if sub_key not in self._subscribed:
            return

        # StandX unsubscribe format
        message = {
            "unsubscribe": {
                "channel": channel,
                "symbol": symbol,
            }
        }

        await self._ws.send(json.dumps(message))
        self._subscribed.discard(sub_key)

        logger.debug(f"구독 해제: {channel}:{symbol}")

    async def subscribe_price(self, symbol: str):
        """가격 구독"""
        await self.subscribe(Channel.PRICE.value, symbol)

    async def subscribe_orderbook(self, symbol: str):
        """오더북 구독"""
        await self.subscribe(Channel.DEPTH_BOOK.value, symbol)

    async def subscribe_orders(self, symbol: str):
        """주문 구독"""
        await self.subscribe(Channel.ORDER.value, symbol)

    # ========== Message Handling ==========

    def _handle_price(self, msg: dict):
        """가격 메시지 처리"""
        symbol = msg.get('symbol', '')
        data = msg.get('data', {})

        # Parse spread array [bid, ask]
        spread = data.get('spread', [])
        best_bid = float(spread[0]) if len(spread) > 0 else 0
        best_ask = float(spread[1]) if len(spread) > 1 else 0

        # Calculate spread in bps
        mid_price = float(data.get('mid_price', 0))
        spread_bps = 0
        if mid_price > 0 and best_bid > 0 and best_ask > 0:
            spread_bps = (best_ask - best_bid) / mid_price * 10000

        price_data = PriceData(
            symbol=symbol,
            index_price=float(data.get('index_price', 0)),
            mark_price=float(data.get('mark_price', 0)),
            last_price=float(data.get('last_price', 0)),
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            timestamp=time.time(),
        )

        self._price_cache[symbol] = price_data

        for callback in self._price_callbacks:
            try:
                callback(price_data)
            except Exception as e:
                logger.error(f"가격 콜백 오류: {e}")

    def _handle_orderbook(self, msg: dict):
        """오더북 메시지 처리"""
        symbol = msg.get('symbol', '')
        data = msg.get('data', {})

        orderbook_data = OrderbookData(
            symbol=symbol,
            bids=[[float(b[0]), float(b[1])] for b in data.get('bids', [])],
            asks=[[float(a[0]), float(a[1])] for a in data.get('asks', [])],
            timestamp=time.time(),
            sequence=int(msg.get('seq', 0)),
        )

        self._orderbook_cache[symbol] = orderbook_data

        for callback in self._orderbook_callbacks:
            try:
                callback(orderbook_data)
            except Exception as e:
                logger.error(f"오더북 콜백 오류: {e}")

    def _handle_order(self, msg: dict):
        """주문 메시지 처리"""
        data = msg.get('data', msg)  # fallback to msg if no data field
        order_update = OrderUpdate(
            order_id=data.get('order_id', data.get('orderId', '')),
            cl_ord_id=data.get('cl_ord_id', data.get('clOrdId', '')),
            symbol=msg.get('symbol', data.get('symbol', '')),
            side=data.get('side', ''),
            status=data.get('status', ''),
            price=float(data.get('price', 0)),
            quantity=float(data.get('qty', data.get('quantity', 0))),
            filled_qty=float(data.get('filled_qty', data.get('filledQty', 0))),
            timestamp=time.time(),
        )

        for callback in self._order_callbacks:
            try:
                callback(order_update)
            except Exception as e:
                logger.error(f"주문 콜백 오류: {e}")

    async def _handle_message(self, message: str):
        """메시지 처리"""
        try:
            data = json.loads(message)

            channel = data.get('channel', '')
            event = data.get('event', '')

            if event == 'pong':
                self._last_pong = time.time()
                return

            if event == 'subscribed':
                logger.debug(f"구독 확인: {data.get('channel')}:{data.get('symbol')}")
                return

            if event == 'error':
                logger.error(f"서버 오류: {data.get('message')}")
                return

            # 채널별 처리
            if channel == Channel.PRICE.value:
                self._handle_price(data)
            elif channel == Channel.DEPTH_BOOK.value:
                self._handle_orderbook(data)
            elif channel == Channel.ORDER.value:
                self._handle_order(data)

        except json.JSONDecodeError:
            logger.warning(f"잘못된 JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")

    # ========== Main Loop ==========

    def _is_connected(self) -> bool:
        """WebSocket 연결 상태 확인"""
        if not self._ws:
            return False
        try:
            # websockets 14.x uses close_code (None if open)
            return self._ws.close_code is None
        except AttributeError:
            # Fallback for older versions
            return True

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
                    await self._handle_message(message)

                except asyncio.TimeoutError:
                    # ping 체크
                    if time.time() - self._last_pong > 60:
                        logger.warning("Pong 타임아웃, 재연결...")
                        await self._reconnect()

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket 연결 끊김")
                if self._running:
                    await self._reconnect()

            except Exception as e:
                logger.error(f"수신 루프 오류: {e}")
                if self._running:
                    await asyncio.sleep(1)

    async def start(self, symbols: List[str]):
        """
        WebSocket 시작 및 구독

        Args:
            symbols: 구독할 심볼 목록
        """
        await self.connect()

        # 구독
        for symbol in symbols:
            await self.subscribe_price(symbol)
            await self.subscribe_orderbook(symbol)

            # private 채널 (인증 있는 경우)
            if self.auth:
                await self.subscribe_orders(symbol)

        logger.info(f"구독 완료: {symbols}")

    async def stop(self):
        """WebSocket 중지"""
        await self.disconnect()
