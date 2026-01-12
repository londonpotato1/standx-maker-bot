"""
안전 장치 모듈
- 체결 방지 로직 (Lock 존중)
- Hard Kill 조건 (Lock 무시)
- 포지션 감시
- 비상 정지
"""
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Dict, Tuple

try:
    from api.rest_client import StandXRestClient, OrderSide
    from core.price_tracker import PriceTracker
    from core.order_manager import OrderManager, ManagedOrder
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.api.rest_client import StandXRestClient, OrderSide
    from standx_maker_bot.core.price_tracker import PriceTracker
    from standx_maker_bot.core.order_manager import OrderManager, ManagedOrder
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('safety_guard')


class SafetyAction(Enum):
    """안전 조치"""
    NONE = "none"
    CANCEL_ORDER = "cancel_order"
    CANCEL_ALL = "cancel_all"
    HARD_KILL = "hard_kill"  # Lock 무시 즉시 취소
    PRE_KILL_PAUSE = "pre_kill_pause"  # 신규 주문 일시 중단 (기존 주문 유지)
    EMERGENCY_STOP = "emergency_stop"


@dataclass
class HardKillConfig:
    """Hard Kill 조건 (Lock 무시)"""
    min_spread_bps: float = 1.5  # 스프레드 붕괴 기준
    max_volatility_bps: float = 30.0  # 급변 감지 기준 (1초 내)
    stale_threshold_seconds: float = 0.5  # 데이터 stale 임계값


@dataclass
class PreKillConfig:
    """Pre-Kill 조건 (신규 주문 일시 중단)"""
    volatility_threshold_bps: float = 15.0  # 1초 내 변동성 임계값
    mark_mid_divergence_bps: float = 3.0  # Mark/Mid 괴리 임계값
    pause_duration_seconds: float = 5.0  # 일시 중단 기간


@dataclass
class SafetyConfig:
    """안전 설정"""
    # 체결 방지 (Lock 중에는 무시됨)
    cancel_if_within_bps: float = 2.0  # 이 거리 내면 즉시 취소

    # 포지션 한도
    max_position_usd: float = 50.0  # 최대 허용 포지션

    # 가격 급변 (레거시)
    max_price_change_bps: float = 50.0  # 1초 내 최대 변동폭

    # Hard Kill 조건
    hard_kill: HardKillConfig = None

    # Pre-Kill 조건 (신규 주문 일시 중단)
    pre_kill: PreKillConfig = None

    # 체크 주기
    check_interval_seconds: float = 0.5

    def __post_init__(self):
        if self.hard_kill is None:
            self.hard_kill = HardKillConfig()
        if self.pre_kill is None:
            self.pre_kill = PreKillConfig()


@dataclass
class SafetyEvent:
    """안전 이벤트"""
    action: SafetyAction
    reason: str
    symbol: str
    details: dict
    timestamp: float


# 콜백 타입
SafetyCallback = Callable[[SafetyEvent], None]


class SafetyGuard:
    """
    안전 장치 (새 전략: Pre-Kill + Lock 존중 + Hard Kill)

    3단계 안전 체계:
    1. Pre-Kill: 위험 징후 감지 → 신규 주문 일시 중단 (기존 주문 유지)
       - 1초 내 volatility > 15 bps
       - Mark/Mid 괴리 > 3 bps
    2. Lock 존중: 0.5초 Duration 조건 충족을 위한 취소 보류
    3. Hard Kill: 심각한 위험 → Lock 무시하고 즉시 취소
       - 스프레드 붕괴
       - 급변 (1초 내 30 bps)
       - 데이터 stale
    """

    def __init__(
        self,
        price_tracker: PriceTracker,
        order_manager: OrderManager,
        rest_client: StandXRestClient,
        config: Optional[SafetyConfig] = None,
    ):
        """
        Args:
            price_tracker: 가격 추적기
            order_manager: 주문 관리자
            rest_client: REST 클라이언트
            config: 안전 설정
        """
        self.price_tracker = price_tracker
        self.order_manager = order_manager
        self.rest_client = rest_client
        self.config = config or SafetyConfig()

        self._running = False
        self._emergency_stop = False
        self._callbacks: List[SafetyCallback] = []

        # 가격 히스토리 (급변 감지용)
        self._price_history: dict = {}  # symbol -> [(timestamp, price), ...]

        # 주문별 Lock 상태 (스마트 보호용: 시작/종료 시간 저장)
        self._order_locks: Dict[str, Tuple[float, float]] = {}  # cl_ord_id -> (lock_start, lock_until)

        # Pre-Kill 상태 (신규 주문 일시 중단)
        self._pre_kill_until: Dict[str, float] = {}  # symbol -> pause_until_timestamp
        self._pre_kill_reason: Dict[str, str] = {}  # symbol -> reason

        # 포지션 초과 감지 시각 (청산 유예 시간용)
        self._position_excess_since: Dict[str, float] = {}  # symbol -> first_detected_timestamp
        self._position_excess_grace_seconds: float = 5.0  # 청산 대기 유예 시간

    def on_safety_event(self, callback: SafetyCallback):
        """안전 이벤트 콜백 등록"""
        self._callbacks.append(callback)

    def _emit_event(self, action: SafetyAction, reason: str, symbol: str, details: dict):
        """안전 이벤트 발생"""
        event = SafetyEvent(
            action=action,
            reason=reason,
            symbol=symbol,
            details=details,
            timestamp=time.time(),
        )

        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"안전 콜백 오류: {e}")

        # 심각한 이벤트 로깅
        if action in [SafetyAction.CANCEL_ALL, SafetyAction.EMERGENCY_STOP]:
            logger.warning(f"안전 이벤트: {action.value} - {reason}")

    # ========== Pre-Kill (Preventive Kill) ==========

    def check_pre_kill_conditions(self, symbol: str) -> tuple[bool, str]:
        """
        Pre-Kill 조건 확인 (신규 주문 일시 중단해야 하는 상황)

        Hard Kill보다 낮은 임계값으로 "사전 예방" 역할:
        - 기존 주문은 유지 (Duration 보존)
        - 신규 주문만 일시 중단

        Args:
            symbol: 심볼

        Returns:
            (Pre-Kill 필요 여부, 사유)
        """
        price = self.price_tracker.get_price(symbol)
        if not price:
            return False, ""

        pre_kill = self.config.pre_kill

        # 1. 중간 수준 변동성 체크 (15 bps/sec)
        volatility = self.price_tracker.get_recent_volatility_bps(symbol, 1.0)
        if volatility > pre_kill.volatility_threshold_bps:
            return True, f"변동성 경고 ({volatility:.1f} bps/1초)"

        # 2. Mark/Mid 괴리 체크
        if price.mark_mid_divergence_bps > pre_kill.mark_mid_divergence_bps:
            return True, f"Mark/Mid 괴리 ({price.mark_mid_divergence_bps:.1f} bps)"

        return False, ""

    def activate_pre_kill(self, symbol: str, reason: str):
        """
        Pre-Kill 활성화 (신규 주문 일시 중단)

        Args:
            symbol: 심볼
            reason: 사유
        """
        pause_duration = self.config.pre_kill.pause_duration_seconds
        self._pre_kill_until[symbol] = time.time() + pause_duration
        self._pre_kill_reason[symbol] = reason

        logger.warning(f"[{symbol}] Pre-Kill 활성화: {reason} ({pause_duration}초 중단)")

        self._emit_event(
            SafetyAction.PRE_KILL_PAUSE,
            reason,
            symbol,
            {"pause_duration": pause_duration},
        )

    def is_pre_kill_active(self, symbol: str) -> bool:
        """
        Pre-Kill이 활성 상태인지 (신규 주문 가능 여부)

        Args:
            symbol: 심볼

        Returns:
            True면 신규 주문 불가
        """
        if symbol not in self._pre_kill_until:
            return False

        is_active = time.time() < self._pre_kill_until[symbol]

        # 만료 시 제거
        if not is_active:
            del self._pre_kill_until[symbol]
            self._pre_kill_reason.pop(symbol, None)
            logger.info(f"[{symbol}] Pre-Kill 해제")

        return is_active

    def get_pre_kill_reason(self, symbol: str) -> str:
        """Pre-Kill 사유 조회"""
        return self._pre_kill_reason.get(symbol, "")

    def get_pre_kill_remaining(self, symbol: str) -> float:
        """Pre-Kill 남은 시간 (초)"""
        if symbol not in self._pre_kill_until:
            return 0.0
        return max(0.0, self._pre_kill_until[symbol] - time.time())

    # ========== Lock Management ==========

    def set_order_lock(self, cl_ord_id: str, lock_seconds: float):
        """
        주문에 Lock 설정 (취소 금지)

        Args:
            cl_ord_id: 주문 ID
            lock_seconds: Lock 시간 (초)
        """
        now = time.time()
        self._order_locks[cl_ord_id] = (now, now + lock_seconds)  # (시작, 종료)
        logger.debug(f"주문 Lock 설정: {cl_ord_id} ({lock_seconds}초)")

    def is_order_locked(self, cl_ord_id: str) -> bool:
        """
        주문이 Lock 상태인지

        Args:
            cl_ord_id: 주문 ID

        Returns:
            Lock 여부
        """
        if cl_ord_id not in self._order_locks:
            return False

        _, lock_until = self._order_locks[cl_ord_id]  # 튜플에서 lock_until만 추출
        is_locked = time.time() < lock_until

        # Lock 만료 시 제거
        if not is_locked:
            del self._order_locks[cl_ord_id]

        return is_locked

    def clear_order_lock(self, cl_ord_id: str):
        """주문 Lock 해제"""
        self._order_locks.pop(cl_ord_id, None)

    def get_lock_elapsed_seconds(self, cl_ord_id: str) -> Optional[float]:
        """
        Lock 경과 시간 조회 (스마트 보호용)

        Args:
            cl_ord_id: 주문 ID

        Returns:
            Lock 경과 시간 (초), Lock 없으면 None
        """
        if cl_ord_id not in self._order_locks:
            return None

        lock_start, lock_until = self._order_locks[cl_ord_id]
        now = time.time()

        # Lock 만료된 경우
        if now >= lock_until:
            del self._order_locks[cl_ord_id]
            return None

        return now - lock_start

    # ========== Hard Kill Check ==========

    def check_hard_kill_conditions(self, symbol: str) -> tuple[bool, str]:
        """
        Hard Kill 조건 확인 (Lock 무시하고 즉시 취소해야 하는 상황)

        Args:
            symbol: 심볼

        Returns:
            (Hard Kill 필요 여부, 사유)
        """
        price = self.price_tracker.get_price(symbol)
        if not price:
            return False, ""

        hard_kill = self.config.hard_kill

        # 1. 데이터 Stale 체크
        # 주의: Stale은 경고만 (Hard Kill 하지 않음) - REST 폴백으로 대응
        if price.age_seconds > hard_kill.stale_threshold_seconds:
            logger.warning(f"[{symbol}] 데이터 Stale 경고 ({price.age_seconds:.2f}초) - Hard Kill 하지 않음")
            # Hard Kill 대신 Pre-Kill만 활성화 (신규 주문 중단, 기존 주문 유지)
            return False, ""

        # 2. 스프레드 붕괴 체크 (너무 좁아지면 체결 위험)
        # 주의: spread_bps가 0이면 데이터 문제일 가능성 높음 (Hard Kill 하지 않음)
        if hard_kill.min_spread_bps > 0 and 0 < price.spread_bps < hard_kill.min_spread_bps:
            return True, f"스프레드 붕괴 ({price.spread_bps:.2f} bps)"

        # 3. 급변 체크 (1초 내)
        volatility = self.price_tracker.get_recent_volatility_bps(symbol, 1.0)
        if volatility > hard_kill.max_volatility_bps:
            return True, f"급변 감지 ({volatility:.1f} bps/1초)"

        return False, ""

    async def execute_hard_kill(self, symbol: str, reason: str) -> int:
        """
        Hard Kill 실행 (Lock 무시하고 즉시 취소)

        Args:
            symbol: 심볼
            reason: 사유

        Returns:
            취소된 주문 수
        """
        logger.warning(f"Hard Kill 실행: {symbol} - {reason}")

        # 해당 심볼의 모든 활성 주문 취소
        orders = self.order_manager.get_active_orders(symbol)
        count = 0

        for order in orders:
            # Lock 강제 해제
            self.clear_order_lock(order.cl_ord_id)

            await self.order_manager.cancel_order(order.cl_ord_id)
            count += 1

        self._emit_event(
            SafetyAction.HARD_KILL,
            reason,
            symbol,
            {"cancelled_count": count},
        )

        return count

    # ========== Check Methods ==========

    def _is_order_too_close(
        self,
        order: ManagedOrder,
        mid_price: float,
        best_bid: float,
        best_ask: float,
    ) -> bool:
        """
        주문이 체결 임박 상태인지

        Args:
            order: 주문
            mid_price: Mid price
            best_bid: Best bid
            best_ask: Best ask

        Returns:
            체결 임박 여부
        """
        if mid_price <= 0:
            return False

        threshold_bps = self.config.cancel_if_within_bps
        threshold_price = mid_price * threshold_bps / 10000

        if order.side == OrderSide.BUY:
            # 매수 주문: best_ask가 주문가격에 근접하면 위험
            distance = best_ask - order.price
            if distance <= threshold_price:
                return True

        elif order.side == OrderSide.SELL:
            # 매도 주문: best_bid가 주문가격에 근접하면 위험
            distance = order.price - best_bid
            if distance <= threshold_price:
                return True

        return False

    async def check_orders(self, symbol: str) -> List[ManagedOrder]:
        """
        체결 임박 주문 확인

        Args:
            symbol: 심볼

        Returns:
            취소해야 할 주문 목록
        """
        price = self.price_tracker.get_price(symbol)
        if not price or price.is_stale:
            return []

        orders_to_cancel = []

        for order in self.order_manager.get_active_orders(symbol):
            if self._is_order_too_close(
                order,
                price.mid_price,
                price.best_bid,
                price.best_ask,
            ):
                orders_to_cancel.append(order)
                logger.warning(
                    f"체결 임박 감지: {order.symbol} {order.side.value} @ {order.price}"
                    f" (mid={price.mid_price:.2f}, bid={price.best_bid:.2f}, ask={price.best_ask:.2f})"
                )

        return orders_to_cancel

    async def cancel_dangerous_orders(self, symbol: str, respect_lock: bool = True) -> int:
        """
        위험 주문 취소 (Lock 상태 존중)

        Args:
            symbol: 심볼
            respect_lock: Lock 상태 존중 여부

        Returns:
            취소된 주문 수
        """
        # cancel_if_within_bps가 0이면 이 기능 비활성화
        if self.config.cancel_if_within_bps <= 0:
            return 0

        orders = await self.check_orders(symbol)
        cancelled = 0

        for order in orders:
            # Lock 상태 확인
            if respect_lock and self.is_order_locked(order.cl_ord_id):
                logger.debug(f"주문 Lock 중 (취소 보류): {order.cl_ord_id}")
                continue

            await self.order_manager.cancel_order(order.cl_ord_id)
            cancelled += 1

            self._emit_event(
                SafetyAction.CANCEL_ORDER,
                "체결 임박",
                symbol,
                {"order": order.cl_ord_id, "price": order.price},
            )

        return cancelled

    async def check_position(self, symbol: str) -> bool:
        """
        포지션 한도 확인 (청산 유예 시간 적용)

        Args:
            symbol: 심볼

        Returns:
            한도 초과 여부 (유예 시간 경과 후에만 True)
        """
        try:
            positions = self.rest_client.get_positions(symbol)
            now = time.time()

            for pos in positions:
                notional = abs(pos.size * pos.mark_price)
                if notional > self.config.max_position_usd:
                    # 최초 감지 시: 시각 기록 및 경고
                    if symbol not in self._position_excess_since:
                        self._position_excess_since[symbol] = now
                        logger.warning(
                            f"포지션 한도 초과 감지: {symbol} ${notional:.2f} > ${self.config.max_position_usd}"
                            f" - 청산 대기 중 ({self._position_excess_grace_seconds}초 유예)"
                        )
                        return False  # 유예 시간 동안 Emergency Stop 안 함

                    # 유예 시간 경과 확인
                    elapsed = now - self._position_excess_since[symbol]
                    if elapsed < self._position_excess_grace_seconds:
                        logger.warning(
                            f"포지션 초과 지속: {symbol} ${notional:.2f} - 청산 대기 중 ({elapsed:.1f}s/{self._position_excess_grace_seconds}s)"
                        )
                        return False  # 아직 유예 시간 내

                    # 유예 시간 경과 → Emergency Stop
                    logger.error(
                        f"포지션 한도 초과 (유예 시간 경과): {symbol} {pos.size} @ {pos.mark_price}"
                        f" = ${notional:.2f} > ${self.config.max_position_usd}"
                    )

                    self._emit_event(
                        SafetyAction.EMERGENCY_STOP,
                        f"포지션 한도 초과 ({elapsed:.1f}초 지속)",
                        symbol,
                        {"size": pos.size, "notional": notional},
                    )

                    return True

            # 포지션이 한도 이내이면 초과 기록 제거
            if symbol in self._position_excess_since:
                logger.info(f"포지션 정상화: {symbol} - 초과 상태 해제")
                del self._position_excess_since[symbol]

            return False

        except Exception as e:
            logger.error(f"포지션 확인 실패: {e}")
            return False

    def _update_price_history(self, symbol: str, price: float):
        """가격 히스토리 업데이트"""
        now = time.time()

        if symbol not in self._price_history:
            self._price_history[symbol] = []

        history = self._price_history[symbol]
        history.append((now, price))

        # 최근 5초만 유지
        history[:] = [(t, p) for t, p in history if now - t < 5]

    def check_price_volatility(self, symbol: str) -> bool:
        """
        가격 급변 확인

        Args:
            symbol: 심볼

        Returns:
            급변 여부
        """
        price = self.price_tracker.get_mid_price(symbol)
        if price <= 0:
            return False

        self._update_price_history(symbol, price)

        history = self._price_history.get(symbol, [])
        if len(history) < 2:
            return False

        # 최근 1초 내 변동폭 계산
        now = time.time()
        recent = [(t, p) for t, p in history if now - t < 1]

        if len(recent) < 2:
            return False

        prices = [p for _, p in recent]
        max_price = max(prices)
        min_price = min(prices)

        if min_price <= 0:
            return False

        change_bps = (max_price - min_price) / min_price * 10000

        if change_bps > self.config.max_price_change_bps:
            logger.warning(f"가격 급변 감지: {symbol} {change_bps:.1f} bps")

            self._emit_event(
                SafetyAction.CANCEL_ALL,
                "가격 급변",
                symbol,
                {"change_bps": change_bps},
            )

            return True

        return False

    # ========== Emergency Stop ==========

    async def emergency_stop(self, reason: str):
        """
        비상 정지

        Args:
            reason: 정지 사유
        """
        logger.critical(f"비상 정지: {reason}")
        self._emergency_stop = True

        # 모든 주문 취소
        try:
            await self.order_manager.cancel_all_orders()
            count = self.rest_client.cancel_all_orders()
            logger.info(f"모든 주문 취소 완료: {count}건")
        except Exception as e:
            logger.error(f"주문 취소 실패: {e}")

        self._emit_event(
            SafetyAction.EMERGENCY_STOP,
            reason,
            "",
            {},
        )

    def is_emergency_stopped(self) -> bool:
        """비상 정지 상태"""
        return self._emergency_stop

    def reset_emergency_stop(self):
        """비상 정지 해제"""
        self._emergency_stop = False
        logger.info("비상 정지 해제")

    # ========== Main Loop ==========

    async def run(self, symbols: List[str]):
        """
        안전 감시 루프 (3단계: Pre-Kill → Lock 존중 → Hard Kill)

        Args:
            symbols: 감시할 심볼 목록
        """
        self._running = True
        logger.info(f"안전 감시 시작: {symbols}")

        while self._running and not self._emergency_stop:
            try:
                for symbol in symbols:
                    # 1. Hard Kill 조건 확인 (최우선 - Lock 무시)
                    hard_kill_needed, reason = self.check_hard_kill_conditions(symbol)
                    if hard_kill_needed:
                        await self.execute_hard_kill(symbol, reason)
                        continue  # 다른 체크 스킵

                    # 2. Pre-Kill 조건 확인 (신규 주문 일시 중단)
                    if not self.is_pre_kill_active(symbol):
                        pre_kill_needed, reason = self.check_pre_kill_conditions(symbol)
                        if pre_kill_needed:
                            self.activate_pre_kill(symbol, reason)

                    # 3. 체결 임박 주문 취소 (Lock 존중)
                    await self.cancel_dangerous_orders(symbol, respect_lock=True)

                    # 4. 포지션 한도 체크
                    if await self.check_position(symbol):
                        await self.emergency_stop("포지션 한도 초과")
                        break

                await asyncio.sleep(self.config.check_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"안전 감시 오류: {e}")
                await asyncio.sleep(1)

        logger.info("안전 감시 중지")

    async def stop(self):
        """안전 감시 중지"""
        self._running = False
