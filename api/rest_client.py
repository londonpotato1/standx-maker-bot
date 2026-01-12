"""
StandX REST API 클라이언트
"""
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

from .auth import StandXAuth
from ..utils.logger import get_logger

logger = get_logger('rest_client')


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(Enum):
    GTC = "gtc"  # Good Till Cancel
    IOC = "ioc"  # Immediate Or Cancel
    FOK = "fok"  # Fill Or Kill
    POST_ONLY = "post_only"  # Maker only


class MarginMode(Enum):
    CROSS = "cross"
    ISOLATED = "isolated"


@dataclass
class PriceInfo:
    """가격 정보"""
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
class OrderbookLevel:
    """오더북 레벨"""
    price: float
    quantity: float


@dataclass
class Orderbook:
    """오더북"""
    symbol: str
    bids: List[OrderbookLevel]
    asks: List[OrderbookLevel]
    timestamp: float

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return 0

    @property
    def spread_bps(self) -> float:
        if self.mid_price > 0:
            return (self.best_ask - self.best_bid) / self.mid_price * 10000
        return 0


@dataclass
class Order:
    """주문"""
    order_id: str
    cl_ord_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: float
    quantity: float
    filled_qty: float
    status: str
    created_at: float
    updated_at: float


@dataclass
class Position:
    """포지션"""
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    margin_mode: str
    leverage: int


@dataclass
class Balance:
    """잔액"""
    available: float
    equity: float
    margin: float
    unrealized_pnl: float


class StandXRestClient:
    """
    StandX REST API 클라이언트
    """

    def __init__(self, auth: StandXAuth, base_url: str = "https://perps.standx.com"):
        """
        Args:
            auth: 인증 관리자
            base_url: API 기본 URL
        """
        self.auth = auth
        self.base_url = base_url.rstrip('/')
        self._session = requests.Session()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        auth_required: bool = True,
        sign_required: bool = False,
    ) -> dict:
        """
        API 요청

        Args:
            method: HTTP 메서드
            endpoint: 엔드포인트
            params: 쿼리 파라미터
            data: 요청 본문
            auth_required: 인증 필요 여부
            sign_required: 요청 서명 필요 여부

        Returns:
            응답 JSON
        """
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}

        if auth_required:
            headers.update(self.auth.get_auth_headers())

        # For signed requests, we must serialize JSON exactly as signed
        body = None
        if sign_required and data:
            # Serialize with same format as auth.sign_request uses
            body = json.dumps(data, separators=(',', ':'), sort_keys=True)
            headers.update(self.auth.sign_request(data))
        elif data:
            body = json.dumps(data)

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                data=body,  # Use pre-serialized string instead of json=
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP 오류: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"요청 오류: {e}")
            raise

    # ========== Public Endpoints ==========

    def get_symbol_price(self, symbol: str) -> PriceInfo:
        """
        심볼 가격 조회

        Args:
            symbol: 심볼 (예: BTC-USD)

        Returns:
            PriceInfo
        """
        response = self._request(
            "GET",
            "/api/query_symbol_price",
            params={"symbol": symbol},
            auth_required=False,
        )

        return PriceInfo(
            symbol=symbol,
            index_price=float(response.get('indexPrice', 0)),
            mark_price=float(response.get('markPrice', 0)),
            last_price=float(response.get('lastPrice', 0)),
            mid_price=float(response.get('midPrice', 0)),
            best_bid=float(response.get('bestBid', 0)),
            best_ask=float(response.get('bestAsk', 0)),
            spread_bps=float(response.get('spreadBps', 0)),
            timestamp=time.time(),
        )

    def get_orderbook(self, symbol: str) -> Orderbook:
        """
        오더북 조회

        Args:
            symbol: 심볼

        Returns:
            Orderbook
        """
        response = self._request(
            "GET",
            "/api/query_depth_book",
            params={"symbol": symbol},
            auth_required=False,
        )

        bids = [
            OrderbookLevel(price=float(b[0]), quantity=float(b[1]))
            for b in response.get('bids', [])
        ]
        asks = [
            OrderbookLevel(price=float(a[0]), quantity=float(a[1]))
            for a in response.get('asks', [])
        ]

        return Orderbook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

    def get_symbol_info(self, symbol: str) -> dict:
        """
        심볼 정보 조회

        Args:
            symbol: 심볼

        Returns:
            심볼 정보
        """
        return self._request(
            "GET",
            "/api/query_symbol_info",
            params={"symbol": symbol},
            auth_required=False,
        )

    # ========== Trade Endpoints ==========

    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
        reduce_only: bool = False,
        cl_ord_id: Optional[str] = None,
        margin_mode: MarginMode = MarginMode.CROSS,
        leverage: int = 1,
    ) -> dict:
        """
        주문 생성

        Args:
            symbol: 심볼
            side: 매수/매도
            order_type: 주문 타입
            quantity: 수량
            price: 가격 (limit 주문 시 필수)
            time_in_force: 주문 유효 기간
            reduce_only: 포지션 축소 전용
            cl_ord_id: 클라이언트 주문 ID
            margin_mode: 마진 모드
            leverage: 레버리지

        Returns:
            주문 응답
        """
        payload = {
            "symbol": symbol,
            "side": side.value,
            "order_type": order_type.value,
            "qty": str(quantity),
            "time_in_force": time_in_force.value,
            "reduce_only": reduce_only,
            "margin_mode": margin_mode.value,
            "leverage": leverage,
        }

        if price is not None:
            payload["price"] = str(price)

        if cl_ord_id:
            payload["cl_ord_id"] = cl_ord_id

        logger.info(f"주문 생성: {symbol} {side.value} {quantity} @ {price or 'market'}")

        return self._request(
            "POST",
            "/api/new_order",
            data=payload,
            auth_required=True,
            sign_required=True,
        )

    def cancel_order(self, order_id: Optional[str] = None, cl_ord_id: Optional[str] = None) -> dict:
        """
        주문 취소

        Args:
            order_id: 주문 ID
            cl_ord_id: 클라이언트 주문 ID

        Returns:
            취소 응답
        """
        payload = {}
        if order_id:
            payload["order_id"] = order_id
        if cl_ord_id:
            payload["cl_ord_id"] = cl_ord_id

        if not payload:
            raise ValueError("order_id 또는 cl_ord_id 중 하나는 필수입니다")

        logger.info(f"주문 취소: {order_id or cl_ord_id}")

        return self._request(
            "POST",
            "/api/cancel_order",
            data=payload,
            auth_required=True,
            sign_required=True,
        )

    def cancel_orders(
        self,
        order_ids: Optional[List[str]] = None,
        cl_ord_ids: Optional[List[str]] = None,
    ) -> dict:
        """
        다중 주문 취소

        Args:
            order_ids: 주문 ID 목록
            cl_ord_ids: 클라이언트 주문 ID 목록

        Returns:
            취소 응답
        """
        payload = {}
        if order_ids:
            payload["order_id_list"] = order_ids
        if cl_ord_ids:
            payload["cl_ord_id_list"] = cl_ord_ids

        if not payload:
            raise ValueError("order_id_list 또는 cl_ord_id_list 중 하나는 필수입니다")

        logger.info(f"다중 주문 취소: {len(order_ids or cl_ord_ids or [])}건")

        return self._request(
            "POST",
            "/api/cancel_orders",
            data=payload,
            auth_required=True,
            sign_required=True,
        )

    # ========== User Endpoints ==========

    def get_open_orders(self, symbol: Optional[str] = None, limit: int = 100) -> List[Order]:
        """
        미체결 주문 조회

        Args:
            symbol: 심볼 (선택)
            limit: 최대 개수

        Returns:
            주문 목록
        """
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol

        response = self._request(
            "GET",
            "/api/query_open_orders",
            params=params,
            auth_required=True,
        )

        orders = []
        for o in response.get('orders', []):
            orders.append(Order(
                order_id=o.get('orderId', ''),
                cl_ord_id=o.get('clOrdId', ''),
                symbol=o.get('symbol', ''),
                side=OrderSide(o.get('side', 'buy')),
                order_type=OrderType(o.get('orderType', 'limit')),
                price=float(o.get('price', 0)),
                quantity=float(o.get('qty', 0)),
                filled_qty=float(o.get('filledQty', 0)),
                status=o.get('status', ''),
                created_at=float(o.get('createdAt', 0)),
                updated_at=float(o.get('updatedAt', 0)),
            ))

        return orders

    def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        포지션 조회

        Args:
            symbol: 심볼 (선택)

        Returns:
            포지션 목록
        """
        params = {}
        if symbol:
            params["symbol"] = symbol

        response = self._request(
            "GET",
            "/api/query_positions",
            params=params,
            auth_required=True,
        )

        # API 응답이 리스트일 수도 있고 딕셔너리일 수도 있음
        if isinstance(response, list):
            position_list = response
        else:
            position_list = response.get('positions', [])

        positions = []
        for p in position_list:
            # API는 'qty' 필드 사용 (size가 아님)
            qty = float(p.get('qty', p.get('size', 0)))
            if qty != 0:  # 포지션이 있는 경우만
                # qty가 음수면 SHORT, 양수면 LONG
                side = 'short' if qty < 0 else 'long'
                positions.append(Position(
                    symbol=p.get('symbol', ''),
                    side=side,
                    size=abs(qty),
                    entry_price=float(p.get('entry_price', p.get('entryPrice', 0))),
                    mark_price=float(p.get('mark_price', p.get('markPrice', 0))),
                    unrealized_pnl=float(p.get('upnl', p.get('unrealizedPnl', 0))),
                    margin_mode=p.get('margin_mode', p.get('marginMode', 'cross')),
                    leverage=int(p.get('leverage', 1)),
                ))

        return positions

    def get_balance(self) -> Balance:
        """
        잔액 조회

        Returns:
            Balance
        """
        response = self._request(
            "GET",
            "/api/query_balance",
            auth_required=True,
        )

        # API 응답 필드명 다양하게 시도
        available = (
            float(response.get('availableBalance', 0)) or
            float(response.get('available', 0)) or
            float(response.get('free', 0)) or
            float(response.get('equity', 0))  # equity를 fallback으로
        )

        return Balance(
            available=available,
            equity=float(response.get('equity', 0)),
            margin=float(response.get('margin', response.get('usedMargin', 0))),
            unrealized_pnl=float(response.get('unrealizedPnl', response.get('unrealisedPnl', 0))),
        )

    def get_order(self, order_id: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Optional[Order]:
        """
        주문 조회

        Args:
            order_id: 주문 ID
            cl_ord_id: 클라이언트 주문 ID

        Returns:
            Order 또는 None
        """
        params = {}
        if order_id:
            params["order_id"] = order_id
        if cl_ord_id:
            params["cl_ord_id"] = cl_ord_id

        if not params:
            raise ValueError("order_id 또는 cl_ord_id 중 하나는 필수입니다")

        try:
            response = self._request(
                "GET",
                "/api/query_order",
                params=params,
                auth_required=True,
            )

            if not response:
                return None

            return Order(
                order_id=response.get('orderId', ''),
                cl_ord_id=response.get('clOrdId', ''),
                symbol=response.get('symbol', ''),
                side=OrderSide(response.get('side', 'buy')),
                order_type=OrderType(response.get('orderType', 'limit')),
                price=float(response.get('price', 0)),
                quantity=float(response.get('qty', 0)),
                filled_qty=float(response.get('filledQty', 0)),
                status=response.get('status', ''),
                created_at=float(response.get('createdAt', 0)),
                updated_at=float(response.get('updatedAt', 0)),
            )
        except Exception:
            return None

    # ========== Helper Methods ==========

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        모든 주문 취소

        Args:
            symbol: 심볼 (선택, 없으면 전체)

        Returns:
            취소된 주문 수
        """
        orders = self.get_open_orders(symbol)
        if not orders:
            return 0

        order_ids = [o.order_id for o in orders if o.order_id]
        if order_ids:
            self.cancel_orders(order_ids=order_ids)

        logger.info(f"전체 주문 취소 완료: {len(order_ids)}건")
        return len(order_ids)

    def has_position(self, symbol: str) -> bool:
        """
        포지션 보유 여부

        Args:
            symbol: 심볼

        Returns:
            포지션 보유 여부
        """
        positions = self.get_positions(symbol)
        return len(positions) > 0

    def get_position_size(self, symbol: str) -> float:
        """
        포지션 크기

        Args:
            symbol: 심볼

        Returns:
            포지션 크기 (양수: 롱, 음수: 숏, 0: 없음)
        """
        positions = self.get_positions(symbol)
        if not positions:
            return 0

        for p in positions:
            if p.symbol == symbol:
                return p.size if p.side == 'long' else -p.size

        return 0
