"""
밴드 계산 모듈
- Band A/B/C 계산 (Mark Price 기준)
- 주문 가격이 어느 밴드에 속하는지 판단
- 동적 거리 계산 (spread/volatility 기반)
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

try:
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('band_calculator')


class Band(Enum):
    """포인트 밴드"""
    A = "A"  # 0-10 bps: 100% 포인트
    B = "B"  # 10-30 bps: 50% 포인트
    C = "C"  # 30-100 bps: 10% 포인트
    OUT = "OUT"  # 100 bps 초과: 0% 포인트


@dataclass
class BandConfig:
    """밴드 설정"""
    # 밴드 경계 (bps)
    band_a_max_bps: float = 10.0
    band_b_max_bps: float = 30.0
    band_c_max_bps: float = 100.0

    # 포인트 비율
    band_a_points: float = 1.0   # 100%
    band_b_points: float = 0.5   # 50%
    band_c_points: float = 0.1   # 10%


@dataclass
class BandInfo:
    """밴드 정보"""
    band: Band
    distance_bps: float  # mark price로부터 거리 (bps)
    points_multiplier: float  # 포인트 배율
    is_near_boundary: bool = False  # Band A 경계 근처 (9.2 bps 이상)


@dataclass
class OrderPlacement:
    """주문 배치 정보"""
    buy_price: float
    sell_price: float
    buy_band: Band
    sell_band: Band
    buy_distance_bps: float
    sell_distance_bps: float


class BandCalculator:
    """
    밴드 계산기

    StandX 메이커 포인트 밴드 시스템 (Mark Price 기준):
    - Band A (0-10 bps): 100% 포인트
    - Band B (10-30 bps): 50% 포인트
    - Band C (30-100 bps): 10% 포인트

    새 전략:
    - Band A 경계 근처 (8-9 bps)에 주문하여 100% 포인트 + 체결 우선순위 낮춤
    - 동적 거리 계산: spread/volatility 기반
    """

    def __init__(
        self,
        config: Optional[BandConfig] = None,
        band_warning_bps: float = 9.2,  # Band A 이탈 경고 거리
    ):
        """
        Args:
            config: 밴드 설정
            band_warning_bps: Band A 경계 근처 판단 기준
        """
        self.config = config or BandConfig()
        self.band_warning_bps = band_warning_bps

    def calculate_distance_bps(self, reference_price: float, order_price: float) -> float:
        """
        기준 가격으로부터 거리 계산 (bps)

        Args:
            reference_price: 기준 가격 (mark price 권장)
            order_price: 주문 가격

        Returns:
            거리 (bps, 절대값)
        """
        if reference_price <= 0:
            return float('inf')

        return abs(order_price - reference_price) / reference_price * 10000

    def calculate_dynamic_distance(
        self,
        spread_bps: float,
        volatility_bps: float,
        tick_bps: float = 0.0,
        min_bps: float = 5.0,
        max_bps: float = 9.0,
        spread_factor: float = 0.6,
        volatility_factor: float = 0.8,
    ) -> float:
        """
        동적 목표 거리 계산

        수식: d_target = clamp(max(tick*2, spread*factor, vol*factor), min, max)

        Args:
            spread_bps: 현재 스프레드 (bps)
            volatility_bps: 최근 변동성 (bps)
            tick_bps: 틱 사이즈 (bps)
            min_bps: 최소 거리
            max_bps: 최대 거리 (Band A 내)
            spread_factor: 스프레드 계수
            volatility_factor: 변동성 계수

        Returns:
            동적 목표 거리 (bps)
        """
        candidates = [
            tick_bps * 2 if tick_bps > 0 else 0,
            spread_bps * spread_factor,
            volatility_bps * volatility_factor,
        ]

        # 가장 큰 값 선택 (안전 마진 확보)
        raw_distance = max(candidates) if any(c > 0 for c in candidates) else min_bps

        # min/max 범위로 clamp
        return max(min_bps, min(max_bps, raw_distance))

    def get_band(self, distance_bps: float) -> Band:
        """
        거리에 따른 밴드 결정

        Args:
            distance_bps: mid price로부터 거리 (bps)

        Returns:
            Band
        """
        if distance_bps <= self.config.band_a_max_bps:
            return Band.A
        elif distance_bps <= self.config.band_b_max_bps:
            return Band.B
        elif distance_bps <= self.config.band_c_max_bps:
            return Band.C
        else:
            return Band.OUT

    def get_points_multiplier(self, band: Band) -> float:
        """
        밴드의 포인트 배율

        Args:
            band: 밴드

        Returns:
            포인트 배율
        """
        if band == Band.A:
            return self.config.band_a_points
        elif band == Band.B:
            return self.config.band_b_points
        elif band == Band.C:
            return self.config.band_c_points
        else:
            return 0.0

    def get_band_info(self, reference_price: float, order_price: float) -> BandInfo:
        """
        주문 가격의 밴드 정보

        Args:
            reference_price: 기준 가격 (mark price 권장)
            order_price: 주문 가격

        Returns:
            BandInfo
        """
        distance_bps = self.calculate_distance_bps(reference_price, order_price)
        band = self.get_band(distance_bps)
        multiplier = self.get_points_multiplier(band)

        # Band A 경계 근처 여부 (9.2 bps 이상이면 곧 이탈 가능)
        is_near_boundary = (
            band == Band.A and
            distance_bps >= self.band_warning_bps
        )

        return BandInfo(
            band=band,
            distance_bps=distance_bps,
            points_multiplier=multiplier,
            is_near_boundary=is_near_boundary,
        )

    def is_in_band_a(self, mid_price: float, order_price: float) -> bool:
        """
        주문이 Band A 내에 있는지

        Args:
            mid_price: Mid price
            order_price: 주문 가격

        Returns:
            Band A 내 여부
        """
        distance_bps = self.calculate_distance_bps(mid_price, order_price)
        return distance_bps <= self.config.band_a_max_bps

    def calculate_order_prices(
        self,
        mid_price: float,
        target_distance_bps: float = 5.0,
    ) -> Tuple[float, float]:
        """
        주문 가격 계산 (매수/매도)

        Args:
            mid_price: Mid price
            target_distance_bps: 목표 거리 (bps)

        Returns:
            (buy_price, sell_price)
        """
        offset = mid_price * target_distance_bps / 10000

        buy_price = mid_price - offset
        sell_price = mid_price + offset

        return buy_price, sell_price

    def get_order_placement(
        self,
        mid_price: float,
        target_distance_bps: float = 5.0,
    ) -> OrderPlacement:
        """
        주문 배치 정보 계산

        Args:
            mid_price: Mid price
            target_distance_bps: 목표 거리 (bps)

        Returns:
            OrderPlacement
        """
        buy_price, sell_price = self.calculate_order_prices(mid_price, target_distance_bps)

        buy_info = self.get_band_info(mid_price, buy_price)
        sell_info = self.get_band_info(mid_price, sell_price)

        return OrderPlacement(
            buy_price=buy_price,
            sell_price=sell_price,
            buy_band=buy_info.band,
            sell_band=sell_info.band,
            buy_distance_bps=buy_info.distance_bps,
            sell_distance_bps=sell_info.distance_bps,
        )

    def needs_rebalance(
        self,
        reference_price: float,
        order_price: float,
        max_distance_bps: float = 10.0,
        rebalance_on_band_exit: bool = True,
    ) -> Tuple[bool, str]:
        """
        재배치 필요 여부 (새 전략: Band 상태 기반)

        Args:
            reference_price: 기준 가격 (mark price)
            order_price: 현재 주문 가격
            max_distance_bps: 최대 허용 거리 (Band A 한계)
            rebalance_on_band_exit: Band 이탈 시에만 재배치

        Returns:
            (재배치 필요 여부, 사유)
        """
        band_info = self.get_band_info(reference_price, order_price)

        # Band A 이탈 시에만 재배치 (10 bps 초과)
        if band_info.band != Band.A:
            return True, f"Band A 이탈 ({band_info.distance_bps:.1f} bps)"

        # 주의: is_near_boundary 체크 제거
        # 이전에는 9.2 bps 이상이면 재배치했지만, 이로 인해 주문이 너무 자주 취소됨
        # 이제 실제 Band A 이탈(10 bps 초과) 시에만 재배치
        # Drift 체크는 maker_farming.py에서 별도로 수행

        return False, ""

    def needs_rebalance_legacy(
        self,
        reference_price: float,
        order_price: float,
        threshold_bps: float = 5.0,
        max_distance_bps: float = 10.0,
    ) -> bool:
        """
        재배치 필요 여부 (레거시: 가격 변동 기반)

        Args:
            reference_price: 기준 가격
            order_price: 현재 주문 가격
            threshold_bps: 재배치 임계값 (bps) - 미사용
            max_distance_bps: 최대 허용 거리 (bps)

        Returns:
            재배치 필요 여부
        """
        distance_bps = self.calculate_distance_bps(reference_price, order_price)

        # Band A 이탈
        if distance_bps > max_distance_bps:
            return True

        return False

    def get_band_boundaries(self, mid_price: float) -> dict:
        """
        밴드 경계 가격

        Args:
            mid_price: Mid price

        Returns:
            {band: (lower, upper)} 딕셔너리
        """
        def to_price(bps: float) -> Tuple[float, float]:
            offset = mid_price * bps / 10000
            return mid_price - offset, mid_price + offset

        return {
            Band.A: to_price(self.config.band_a_max_bps),
            Band.B: to_price(self.config.band_b_max_bps),
            Band.C: to_price(self.config.band_c_max_bps),
        }

    def estimate_daily_points(
        self,
        notional_usd: float,
        band: Band = Band.A,
    ) -> float:
        """
        일일 예상 포인트

        Args:
            notional_usd: 노출 금액 (USD)
            band: 밴드

        Returns:
            예상 일일 포인트
        """
        multiplier = self.get_points_multiplier(band)
        return notional_usd * multiplier
