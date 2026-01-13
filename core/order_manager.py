"""
주문 관리 모듈
- 주문 생성/취소/재배치
- 주문 상태 추적
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

try:
    from api.rest_client import (
        StandXRestClient,
        Order,
        OrderSide,
        OrderType,
        TimeInForce,
        MarginMode,
    )
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.api.rest_client import (
        StandXRestClient,
        Order,
        OrderSide,
        OrderType,
        TimeInForce,
        MarginMode,
    )
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('order_manager')


class ManagedOrderStatus(Enum):
    """관리 주문 상태"""
    PENDING = "pending"      # 전송 대기
    SUBMITTED = "submitted"  # 전송됨
    OPEN = "open"           # 오더북에 있음
    FILLED = "filled"       # 체결됨
    CANCELLED = "cancelled"  # 취소됨
    REJECTED = "rejected"   # 거부됨
    ERROR = "error"         # 오류


@dataclass
class ManagedOrder:
    """관리 주문"""
    cl_ord_id: str
    symbol: str
    side: OrderSide
    price: float
    quantity: float
    status: ManagedOrderStatus = ManagedOrderStatus.PENDING
    order_id: Optional[str] = None
    filled_qty: float = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error_message: str = ""

    @property
    def is_active(self) -> bool:
        """활성 주문 여부"""
        return self.status in [ManagedOrderStatus.SUBMITTED, ManagedOrderStatus.OPEN]

    @property
    def is_done(self) -> bool:
        """완료 여부"""
        return self.status in [
            ManagedOrderStatus.FILLED,
            ManagedOrderStatus.CANCELLED,
            ManagedOrderStatus.REJECTED,
            ManagedOrderStatus.ERROR,
        ]

    @property
    def notional_usd(self) -> float:
        """노출 금액"""
        return self.price * self.quantity


# 콜백 타입
OrderCallback = Callable[[ManagedOrder], None]


class OrderManager:
    """
    주문 관리자

    - 주문 생성/취소
    - 주문 상태 추적
    - 재배치 로직
    """

    def __init__(
        self,
        rest_client: StandXRestClient,
        leverage: int = 1,
        margin_mode: MarginMode = MarginMode.CROSS,
    ):
        """
        Args:
            rest_client: REST 클라이언트
            leverage: 레버리지
            margin_mode: 마진 모드
        """
        self.rest_client = rest_client
        self.leverage = leverage
        self.margin_mode = margin_mode

        self._orders: Dict[str, ManagedOrder] = {}  # cl_ord_id -> ManagedOrder
        self._callbacks: List[OrderCallback] = []

    def on_order_update(self, callback: OrderCallback):
        """주문 업데이트 콜백 등록"""
        self._callbacks.append(callback)

    def _notify_order_update(self, order: ManagedOrder):
        """주문 업데이트 알림"""
        for callback in self._callbacks:
            try:
                callback(order)
            except Exception as e:
                logger.error(f"주문 콜백 오류: {e}")

    def _generate_cl_ord_id(self, symbol: str, side: str) -> str:
        """클라이언트 주문 ID 생성"""
        short_uuid = uuid.uuid4().hex[:8]
        return f"maker_{symbol}_{side}_{short_uuid}"

    # ========== Order Operations ==========

    async def create_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        reduce_only: bool = True,
    ) -> Optional[ManagedOrder]:
        """
        시장가 주문 생성 (청산용)

        Args:
            symbol: 심볼
            side: 매수/매도
            quantity: 수량
            reduce_only: 포지션 축소 전용

        Returns:
            ManagedOrder 또는 None
        """
        cl_ord_id = self._generate_cl_ord_id(symbol, f"mkt_{side.value}")

        order = ManagedOrder(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            price=0,  # market order
            quantity=quantity,
        )

        self._orders[cl_ord_id] = order

        try:
            logger.info(f"시장가 주문 (청산): {symbol} {side.value} {quantity}")

            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
            response = await asyncio.to_thread(
                self.rest_client.create_order,
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                reduce_only=reduce_only,
                cl_ord_id=cl_ord_id,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )

            order.status = ManagedOrderStatus.FILLED
            order.order_id = response.get('orderId', '')
            order.filled_qty = quantity
            order.updated_at = time.time()

            logger.info(f"청산 완료: {cl_ord_id}")

        except Exception as e:
            order.status = ManagedOrderStatus.ERROR
            order.error_message = str(e)
            order.updated_at = time.time()
            logger.error(f"청산 실패: {e}")
            return None

        self._notify_order_update(order)
        return order

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        quantity: float,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ) -> ManagedOrder:
        """
        주문 생성

        Args:
            symbol: 심볼
            side: 매수/매도
            price: 가격
            quantity: 수량
            time_in_force: 주문 유효 기간

        Returns:
            ManagedOrder
        """
        cl_ord_id = self._generate_cl_ord_id(symbol, side.value)

        order = ManagedOrder(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
        )

        self._orders[cl_ord_id] = order

        try:
            logger.info(f"주문 생성: {symbol} {side.value} {quantity} @ {price}")

            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
            response = await asyncio.to_thread(
                self.rest_client.create_order,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                price=price,
                time_in_force=time_in_force,
                cl_ord_id=cl_ord_id,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )

            order.status = ManagedOrderStatus.SUBMITTED
            order.order_id = response.get('orderId', '')
            order.updated_at = time.time()

            logger.debug(f"주문 전송 완료: {cl_ord_id} -> {order.order_id}")

        except Exception as e:
            order.status = ManagedOrderStatus.ERROR
            order.error_message = str(e)
            order.updated_at = time.time()
            logger.error(f"주문 생성 실패: {e}")

        self._notify_order_update(order)
        return order

    async def cancel_order(self, cl_ord_id: str) -> bool:
        """
        주문 취소

        Args:
            cl_ord_id: 클라이언트 주문 ID

        Returns:
            취소 성공 여부
        """
        order = self._orders.get(cl_ord_id)
        if not order:
            logger.warning(f"주문을 찾을 수 없음: {cl_ord_id}")
            return False

        if not order.is_active:
            logger.debug(f"이미 완료된 주문: {cl_ord_id}")
            return True

        try:
            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
            if order.order_id:
                await asyncio.to_thread(self.rest_client.cancel_order, order_id=order.order_id)
            else:
                await asyncio.to_thread(self.rest_client.cancel_order, cl_ord_id=cl_ord_id)

            order.status = ManagedOrderStatus.CANCELLED
            order.updated_at = time.time()

            logger.info(f"주문 취소 완료: {cl_ord_id}")
            self._notify_order_update(order)
            return True

        except Exception as e:
            error_str = str(e)
            # 404 "order not found" = 이미 취소되었거나 체결됨
            if "404" in error_str or "not found" in error_str.lower():
                logger.debug(f"주문 이미 취소/체결됨: {cl_ord_id}")
                order.status = ManagedOrderStatus.CANCELLED
                order.updated_at = time.time()
                self._notify_order_update(order)
                return True
            logger.error(f"주문 취소 실패: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        모든 주문 취소

        Args:
            symbol: 심볼 (선택)

        Returns:
            취소된 주문 수
        """
        count = 0

        for cl_ord_id, order in list(self._orders.items()):
            if not order.is_active:
                continue
            if symbol and order.symbol != symbol:
                continue

            if await self.cancel_order(cl_ord_id):
                count += 1

        logger.info(f"전체 주문 취소: {count}건")
        return count

    async def replace_order(
        self,
        cl_ord_id: str,
        new_price: float,
        new_quantity: Optional[float] = None,
    ) -> Optional[ManagedOrder]:
        """
        주문 재배치 (취소 후 재생성)

        Args:
            cl_ord_id: 기존 주문 ID
            new_price: 새 가격
            new_quantity: 새 수량 (없으면 기존 수량)

        Returns:
            새 ManagedOrder
        """
        old_order = self._orders.get(cl_ord_id)
        if not old_order:
            logger.warning(f"주문을 찾을 수 없음: {cl_ord_id}")
            return None

        # 기존 주문 취소
        await self.cancel_order(cl_ord_id)

        # 새 주문 생성
        quantity = new_quantity if new_quantity is not None else old_order.quantity

        return await self.create_order(
            symbol=old_order.symbol,
            side=old_order.side,
            price=new_price,
            quantity=quantity,
        )

    # ========== Order State ==========

    def get_order(self, cl_ord_id: str) -> Optional[ManagedOrder]:
        """주문 조회"""
        return self._orders.get(cl_ord_id)

    def get_active_orders(self, symbol: Optional[str] = None) -> List[ManagedOrder]:
        """
        활성 주문 목록

        Args:
            symbol: 심볼 (선택)

        Returns:
            활성 주문 목록
        """
        orders = []
        for order in self._orders.values():
            if not order.is_active:
                continue
            if symbol and order.symbol != symbol:
                continue
            orders.append(order)
        return orders

    def get_active_orders_by_side(
        self,
        symbol: str,
        side: OrderSide,
    ) -> List[ManagedOrder]:
        """
        사이드별 활성 주문

        Args:
            symbol: 심볼
            side: 매수/매도

        Returns:
            주문 목록
        """
        return [
            o for o in self.get_active_orders(symbol)
            if o.side == side
        ]

    def has_active_order(self, symbol: str, side: OrderSide) -> bool:
        """
        활성 주문 존재 여부

        Args:
            symbol: 심볼
            side: 매수/매도

        Returns:
            존재 여부
        """
        return len(self.get_active_orders_by_side(symbol, side)) > 0

    def get_total_notional(self, symbol: Optional[str] = None) -> float:
        """
        총 노출 금액

        Args:
            symbol: 심볼 (선택)

        Returns:
            노출 금액 (USD)
        """
        total = 0
        for order in self.get_active_orders(symbol):
            total += order.notional_usd
        return total

    # ========== Sync with Exchange ==========

    async def sync_orders(self, symbol: Optional[str] = None):
        """
        거래소와 주문 상태 동기화

        Args:
            symbol: 심볼 (선택)
        """
        try:
            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
            exchange_orders = await asyncio.to_thread(self.rest_client.get_open_orders, symbol)

            # 거래소 주문을 딕셔너리로
            exchange_map = {o.cl_ord_id: o for o in exchange_orders if o.cl_ord_id}

            # 로컬 주문 업데이트
            for cl_ord_id, order in self._orders.items():
                if not order.is_active:
                    continue
                if symbol and order.symbol != symbol:
                    continue

                ex_order = exchange_map.get(cl_ord_id)

                if ex_order:
                    # 거래소에 있음 -> OPEN
                    if order.status != ManagedOrderStatus.OPEN:
                        order.status = ManagedOrderStatus.OPEN
                        order.order_id = ex_order.order_id
                        order.updated_at = time.time()
                        self._notify_order_update(order)
                else:
                    # 거래소에 없음 - 최근 생성된 주문은 아직 거래소에 반영 안됐을 수 있음
                    # 생성 후 3초 이내면 동기화 스킵 (거래소 반영 대기)
                    order_age = time.time() - order.created_at
                    if order_age < 3.0:
                        logger.debug(f"최근 생성된 주문 동기화 스킵: {cl_ord_id} ({order_age:.1f}초)")
                        continue

                    # 3초 이상 지났는데도 거래소에 없으면 상세 조회 시도
                    # 단, 404 에러 시 바로 CANCELLED 처리하지 않음
                    try:
                        # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
                        detail = await asyncio.to_thread(self.rest_client.get_order, cl_ord_id=cl_ord_id)
                        if detail:
                            if detail.status == 'filled':
                                order.status = ManagedOrderStatus.FILLED
                                order.filled_qty = detail.filled_qty
                                order.updated_at = time.time()
                                self._notify_order_update(order)
                            elif detail.status in ['cancelled', 'canceled', 'rejected']:
                                order.status = ManagedOrderStatus.CANCELLED
                                order.updated_at = time.time()
                                self._notify_order_update(order)
                            # 다른 상태면 유지 (pending, open 등)
                        # detail이 None이면 유지 (API 응답 불완전)

                    except Exception as e:
                        # 404 또는 다른 에러 - 주문 상태 유지 (급하게 CANCELLED 처리하지 않음)
                        error_str = str(e).lower()
                        if "404" in error_str or "not found" in error_str:
                            # 10초 이상 지났는데 404면 진짜 없는 것으로 판단
                            if order_age > 10.0:
                                logger.debug(f"주문 미발견 (10초 초과): {cl_ord_id}")
                                order.status = ManagedOrderStatus.CANCELLED
                                order.updated_at = time.time()
                                self._notify_order_update(order)
                            else:
                                logger.debug(f"주문 조회 404 (대기 중): {cl_ord_id} ({order_age:.1f}초)")
                        else:
                            logger.debug(f"주문 조회 실패: {cl_ord_id} - {e}")

            logger.debug(f"주문 동기화 완료: {len(exchange_orders)}건")

        except Exception as e:
            logger.error(f"주문 동기화 실패: {e}")

    # ========== Cleanup ==========

    def cleanup_old_orders(self, max_age_seconds: float = 3600):
        """
        오래된 완료 주문 정리

        Args:
            max_age_seconds: 최대 보관 시간
        """
        now = time.time()
        to_remove = []

        for cl_ord_id, order in self._orders.items():
            if order.is_done and (now - order.updated_at) > max_age_seconds:
                to_remove.append(cl_ord_id)

        for cl_ord_id in to_remove:
            del self._orders[cl_ord_id]

        if to_remove:
            logger.debug(f"오래된 주문 정리: {len(to_remove)}건")
