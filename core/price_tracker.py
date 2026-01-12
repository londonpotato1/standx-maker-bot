"""
가격 추적 모듈
- Mid price / Mark price 실시간 추적
- 가격 변동 감지
- 변동성 계산
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Deque, Tuple

from ..api.websocket_client import StandXWebSocket, PriceData, OrderbookData
from ..api.rest_client import StandXRestClient
from ..utils.logger import get_logger

logger = get_logger('price_tracker')


@dataclass
class SymbolPrice:
    """심볼 가격 정보"""
    symbol: str
    mid_price: float
    best_bid: float
    best_ask: float
    spread_bps: float
    last_update: float
    source: str = "ws"  # "ws" or "rest"
    mark_price: float = 0.0  # Mark price (포인트 계산 기준)

    @property
    def age_seconds(self) -> float:
        """마지막 업데이트로부터 경과 시간"""
        return time.time() - self.last_update

    @property
    def is_stale(self) -> bool:
        """데이터가 오래되었는지 (10초 이상)"""
        return self.age_seconds > 10

    @property
    def reference_price(self) -> float:
        """
        기준 가격 (포인트 밴드 계산용)
        - mark_price가 있으면 mark_price 사용 (공식 기준)
        - 없으면 mid_price 폴백
        """
        return self.mark_price if self.mark_price > 0 else self.mid_price

    @property
    def mark_mid_divergence_bps(self) -> float:
        """
        Mark Price와 Mid Price의 괴리 (bps)
        - DEX 환경에서 Mark ≠ Mid 일 수 있음
        - 큰 괴리는 가격 왜곡 신호
        """
        if self.mid_price <= 0 or self.mark_price <= 0:
            return 0.0
        return abs(self.mark_price - self.mid_price) / self.mid_price * 10000

    @property
    def is_price_diverged(self) -> bool:
        """
        Mark/Mid 괴리가 위험 수준인지 (3 bps 초과)
        - 3 bps 초과 시 Band 계산 신뢰도 저하
        """
        return self.mark_mid_divergence_bps > 3.0


# 콜백 타입
PriceChangeCallback = Callable[[str, float, float], None]  # symbol, old_price, new_price


class PriceTracker:
    """
    가격 추적기

    - WebSocket으로 실시간 가격 수신
    - REST API로 폴백
    - 가격 변동 콜백 지원
    """

    def __init__(
        self,
        ws_client: StandXWebSocket,
        rest_client: Optional[StandXRestClient] = None,
        stale_threshold_seconds: float = 10.0,
    ):
        """
        Args:
            ws_client: WebSocket 클라이언트
            rest_client: REST 클라이언트 (폴백용)
            stale_threshold_seconds: 데이터 만료 임계값
        """
        self.ws_client = ws_client
        self.rest_client = rest_client
        self.stale_threshold = stale_threshold_seconds

        self._prices: Dict[str, SymbolPrice] = {}
        self._callbacks: List[PriceChangeCallback] = []
        self._running = False

        # 변동성 계산용 가격 히스토리 (최근 30초)
        self._price_history: Dict[str, Deque[Tuple[float, float]]] = {}  # symbol -> deque of (timestamp, price)
        self._volatility_window_seconds: float = 10.0  # 변동성 계산 윈도우

        # WebSocket 콜백 등록
        self.ws_client.on_price(self._on_price_update)
        self.ws_client.on_orderbook(self._on_orderbook_update)

    def on_price_change(self, callback: PriceChangeCallback):
        """가격 변동 콜백 등록"""
        self._callbacks.append(callback)

    def _notify_price_change(self, symbol: str, old_price: float, new_price: float):
        """가격 변동 알림"""
        for callback in self._callbacks:
            try:
                callback(symbol, old_price, new_price)
            except Exception as e:
                logger.error(f"가격 변동 콜백 오류: {e}")

    def _update_price_history(self, symbol: str, price: float):
        """가격 히스토리 업데이트 (변동성 계산용)"""
        now = time.time()

        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=1000)

        self._price_history[symbol].append((now, price))

        # 오래된 데이터 제거 (30초 이상)
        while self._price_history[symbol] and now - self._price_history[symbol][0][0] > 30:
            self._price_history[symbol].popleft()

    def _on_price_update(self, data: PriceData):
        """WebSocket 가격 업데이트 처리"""
        symbol = data.symbol
        old_price = self._prices.get(symbol)
        old_mid = old_price.mid_price if old_price else 0

        # mark_price가 있으면 사용, 없으면 mid_price
        mark_price = getattr(data, 'mark_price', 0.0) or data.mid_price

        self._prices[symbol] = SymbolPrice(
            symbol=symbol,
            mid_price=data.mid_price,
            best_bid=data.best_bid,
            best_ask=data.best_ask,
            spread_bps=data.spread_bps,
            last_update=time.time(),
            source="ws",
            mark_price=mark_price,
        )

        # 변동성 계산용 히스토리 업데이트
        self._update_price_history(symbol, mark_price)

        # 가격 변동 감지 (0.01% 이상)
        if old_mid > 0 and abs(data.mid_price - old_mid) / old_mid > 0.0001:
            self._notify_price_change(symbol, old_mid, data.mid_price)

    def _on_orderbook_update(self, data: OrderbookData):
        """WebSocket 오더북 업데이트 처리"""
        symbol = data.symbol

        # 기존 price 데이터가 없거나 오래된 경우에만 업데이트
        existing = self._prices.get(symbol)
        if existing and existing.source == "ws" and not existing.is_stale:
            return

        old_mid = existing.mid_price if existing else 0

        spread_bps = 0
        if data.mid_price > 0:
            spread_bps = (data.best_ask - data.best_bid) / data.mid_price * 10000

        self._prices[symbol] = SymbolPrice(
            symbol=symbol,
            mid_price=data.mid_price,
            best_bid=data.best_bid,
            best_ask=data.best_ask,
            spread_bps=spread_bps,
            last_update=time.time(),
            source="ws",
        )

        # 가격 변동 감지
        if old_mid > 0 and abs(data.mid_price - old_mid) / old_mid > 0.0001:
            self._notify_price_change(symbol, old_mid, data.mid_price)

    async def _fetch_rest_price(self, symbol: str) -> Optional[SymbolPrice]:
        """REST API로 가격 조회"""
        if not self.rest_client:
            return None

        try:
            price_info = self.rest_client.get_symbol_price(symbol)

            return SymbolPrice(
                symbol=symbol,
                mid_price=price_info.mid_price,
                best_bid=price_info.best_bid,
                best_ask=price_info.best_ask,
                spread_bps=price_info.spread_bps,
                last_update=time.time(),
                source="rest",
            )

        except Exception as e:
            logger.warning(f"REST 가격 조회 실패 ({symbol}): {e}")
            return None

    # ========== Public Methods ==========

    def get_price(self, symbol: str) -> Optional[SymbolPrice]:
        """
        심볼 가격 가져오기

        Args:
            symbol: 심볼

        Returns:
            SymbolPrice 또는 None
        """
        return self._prices.get(symbol)

    def get_mid_price(self, symbol: str) -> float:
        """
        Mid price 가져오기

        Args:
            symbol: 심볼

        Returns:
            Mid price (없으면 0)
        """
        price = self._prices.get(symbol)
        return price.mid_price if price else 0

    def get_best_bid(self, symbol: str) -> float:
        """Best bid 가져오기"""
        price = self._prices.get(symbol)
        return price.best_bid if price else 0

    def get_best_ask(self, symbol: str) -> float:
        """Best ask 가져오기"""
        price = self._prices.get(symbol)
        return price.best_ask if price else 0

    def get_spread_bps(self, symbol: str) -> float:
        """스프레드 (bps) 가져오기"""
        price = self._prices.get(symbol)
        return price.spread_bps if price else 0

    def get_mark_price(self, symbol: str) -> float:
        """
        Mark price 가져오기 (포인트 밴드 계산 기준)

        Args:
            symbol: 심볼

        Returns:
            Mark price (없으면 mid_price, 둘 다 없으면 0)
        """
        price = self._prices.get(symbol)
        if price and price.reference_price > 0:
            return price.reference_price

        # REST API 폴백 (WebSocket 데이터 없을 때)
        if self.rest_client:
            try:
                orderbook = self.rest_client.get_orderbook(symbol)
                if orderbook.bids and orderbook.asks:
                    # OrderbookLevel 객체에서 price 추출
                    best_bid = orderbook.bids[0].price
                    best_ask = orderbook.asks[0].price
                    mid_price = (best_bid + best_ask) / 2

                    # 캐시 업데이트
                    spread_bps = (best_ask - best_bid) / mid_price * 10000
                    self._prices[symbol] = SymbolPrice(
                        symbol=symbol,
                        mid_price=mid_price,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        spread_bps=spread_bps,
                        last_update=time.time(),
                        source="rest",
                        mark_price=mid_price,
                    )
                    logger.info(f"[{symbol}] REST 폴백 가격: ${mid_price:,.2f}")
                    return mid_price
            except Exception as e:
                logger.warning(f"[{symbol}] REST 가격 조회 실패: {e}")

        return 0

    def get_reference_price(self, symbol: str) -> float:
        """
        기준 가격 가져오기 (mark_price 우선, mid_price 폴백)
        get_mark_price의 alias
        """
        return self.get_mark_price(symbol)

    def get_volatility_bps(self, symbol: str, window_seconds: float = 10.0) -> float:
        """
        최근 변동성 계산 (bps)

        Args:
            symbol: 심볼
            window_seconds: 계산 윈도우 (초)

        Returns:
            변동성 (max-min)/mid * 10000, 데이터 없으면 0
        """
        if symbol not in self._price_history:
            return 0.0

        now = time.time()
        history = self._price_history[symbol]

        # 윈도우 내 가격만 필터링
        recent_prices = [p for t, p in history if now - t <= window_seconds]

        if len(recent_prices) < 2:
            return 0.0

        max_price = max(recent_prices)
        min_price = min(recent_prices)
        mid_price = (max_price + min_price) / 2

        if mid_price <= 0:
            return 0.0

        return (max_price - min_price) / mid_price * 10000

    def get_recent_volatility_bps(self, symbol: str, seconds: float = 1.0) -> float:
        """
        최근 N초간 변동성 (Hard Kill 판단용)

        Args:
            symbol: 심볼
            seconds: 시간 범위

        Returns:
            변동성 (bps)
        """
        return self.get_volatility_bps(symbol, seconds)

    def is_price_valid(self, symbol: str) -> bool:
        """
        가격 데이터 유효성 확인

        Args:
            symbol: 심볼

        Returns:
            유효 여부
        """
        price = self._prices.get(symbol)
        if not price:
            return False
        return not price.is_stale

    def get_all_prices(self) -> Dict[str, SymbolPrice]:
        """모든 가격 가져오기"""
        return dict(self._prices)

    async def refresh_price(self, symbol: str) -> Optional[SymbolPrice]:
        """
        가격 강제 갱신 (REST API 사용)

        Args:
            symbol: 심볼

        Returns:
            갱신된 가격
        """
        price = await self._fetch_rest_price(symbol)
        if price:
            old = self._prices.get(symbol)
            old_mid = old.mid_price if old else 0

            self._prices[symbol] = price

            if old_mid > 0 and abs(price.mid_price - old_mid) / old_mid > 0.0001:
                self._notify_price_change(symbol, old_mid, price.mid_price)

        return price

    async def refresh_stale_prices(self, symbols: List[str]):
        """
        오래된 가격 갱신

        Args:
            symbols: 심볼 목록
        """
        for symbol in symbols:
            price = self._prices.get(symbol)
            if not price or price.is_stale:
                await self.refresh_price(symbol)

    # ========== Background Tasks ==========

    async def _stale_price_checker(self, symbols: List[str], check_interval: float = 5.0):
        """
        오래된 가격 자동 갱신 태스크

        Args:
            symbols: 심볼 목록
            check_interval: 체크 주기
        """
        while self._running:
            try:
                await self.refresh_stale_prices(symbols)
                await asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"가격 갱신 오류: {e}")
                await asyncio.sleep(1)

    async def start(self, symbols: List[str]):
        """
        가격 추적 시작

        Args:
            symbols: 추적할 심볼 목록
        """
        self._running = True

        # 초기 가격 로드 (REST)
        if self.rest_client:
            for symbol in symbols:
                await self.refresh_price(symbol)

        logger.info(f"가격 추적 시작: {symbols}")

    async def stop(self):
        """가격 추적 중지"""
        self._running = False
        logger.info("가격 추적 중지")
