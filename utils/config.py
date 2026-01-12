"""
설정 관리 모듈
"""
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# 암호화 모듈 (Railway 배포 시 마스터 비밀번호로 복호화)
try:
    from utils.password_crypto import PasswordCrypto
except ImportError:
    try:
        from standx_maker_bot.utils.password_crypto import PasswordCrypto
    except ImportError:
        PasswordCrypto = None


@dataclass
class StandXConfig:
    """StandX API 설정"""
    base_url: str = "https://perps.standx.com"
    ws_url: str = "wss://perps.standx.com/ws-stream/v1"
    chain: str = "bsc"


@dataclass
class WalletConfig:
    """지갑 설정"""
    address: str = ""
    private_key: str = ""


@dataclass
class DynamicDistanceConfig:
    """동적 거리 설정"""
    enabled: bool = True
    min_bps: float = 5.0
    max_bps: float = 9.0
    spread_factor: float = 0.6
    volatility_factor: float = 0.8


@dataclass
class StrategyConfig:
    """전략 설정"""
    symbols: List[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD"])
    leverage: int = 10  # 레버리지 (10x = $228로 $2,280 노출 가능)
    order_size_usd: float = 333.0  # 심볼당 주문 크기 (노출 금액)
    margin_reserve_percent: float = 30.0  # 청산용 마진 예약 비율 (%)

    # 2+2 전략 설정
    num_orders_per_side: int = 2  # 방향당 주문 개수 (1=1+1, 2=2+2)
    order_distances_bps: List[float] = field(default_factory=lambda: [7.0, 9.0])  # 각 주문 거리

    min_distance_bps: float = 3.0  # 최소 거리 (체결 방지)
    target_distance_bps: float = 8.0  # 목표 거리 (1+1 전략용, 2+2에서는 order_distances_bps 사용)
    max_distance_bps: float = 10.0  # 최대 거리 (Band A 한계)
    band_warning_bps: float = 9.2  # Band A 이탈 경고 거리

    # Lock & Cooldown
    order_lock_seconds: float = 0.7  # 주문 생성 후 취소 금지 시간
    rebalance_cooldown_seconds: float = 3.0  # 재배치 후 대기 시간

    # 재배치 트리거
    rebalance_on_band_exit: bool = True  # Band 이탈 시에만 재배치
    rebalance_threshold_bps: float = 5.0  # 레거시 (비활성)
    drift_threshold_bps: float = 4.0  # 가격 드리프트 임계값 (기준가격 변동 시 재배치)

    # 동적 거리
    dynamic_distance: DynamicDistanceConfig = field(default_factory=DynamicDistanceConfig)

    check_interval_seconds: float = 1.0  # 체크 주기


@dataclass
class PreKillConfig:
    """Pre-Kill 조건 (신규 주문 일시 중단)"""
    volatility_threshold_bps: float = 15.0  # 1초 내 변동성 임계값
    mark_mid_divergence_bps: float = 3.0  # Mark/Mid 괴리 임계값
    pause_duration_seconds: float = 5.0  # 일시 중단 기간


@dataclass
class HardKillConfig:
    """Hard Kill 조건 (Lock 무시)"""
    min_spread_bps: float = 1.5  # 스프레드 붕괴 기준
    max_volatility_bps: float = 30.0  # 급변 감지 기준
    stale_threshold_seconds: float = 0.5  # 데이터 stale 임계값


@dataclass
class SafetyConfig:
    """안전 설정"""
    max_position_usd: float = 50.0  # 최대 허용 포지션
    cancel_if_within_bps: float = 2.0  # 이 거리 내면 즉시 취소 (Lock 아닐 때)
    pre_kill: PreKillConfig = field(default_factory=PreKillConfig)
    hard_kill: HardKillConfig = field(default_factory=HardKillConfig)


@dataclass
class TelegramConfig:
    """텔레그램 설정"""
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class BinanceProtectionConfig:
    """Binance 선행 감지 설정"""
    enabled: bool = True
    trigger_bps: float = 3.0  # 변동 트리거 (bps)
    window_seconds: float = 0.5  # 감지 윈도우
    cooldown_seconds: float = 0.5  # 쿨다운


@dataclass
class QueueProtectionConfig:
    """오더북 큐 프로텍션 설정"""
    enabled: bool = True
    drop_threshold_percent: float = 30.0  # 감소 임계값 (%)
    window_seconds: float = 2.0  # 모니터링 윈도우
    min_queue_ahead_usd: float = 100.0  # 최소 앞 물량 (USD)


@dataclass
class FillProtectionConfig:
    """체결 방지 보호 설정"""
    binance: BinanceProtectionConfig = field(default_factory=BinanceProtectionConfig)
    queue: QueueProtectionConfig = field(default_factory=QueueProtectionConfig)
    check_interval_seconds: float = 0.1  # 체크 주기
    # 스마트 보호: Lock 경과 임계값 (초)
    # 이 시간 경과 전에는 Fill Protection 비활성화 (포인트 적립 보장)
    smart_protection_threshold_seconds: float = 2.5


@dataclass
class Config:
    """전체 설정"""
    standx: StandXConfig = field(default_factory=StandXConfig)
    wallet: WalletConfig = field(default_factory=WalletConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    fill_protection: FillProtectionConfig = field(default_factory=FillProtectionConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @classmethod
    def load(cls, config_path: Optional[str] = None, env_path: Optional[str] = None) -> 'Config':
        """
        설정 로드

        Args:
            config_path: config.yaml 경로
            env_path: .env 파일 경로

        Returns:
            Config 인스턴스
        """
        # 기본 경로
        base_dir = Path(__file__).parent.parent

        if config_path is None:
            config_path = base_dir / "config.yaml"
        else:
            config_path = Path(config_path)

        if env_path is None:
            env_path = base_dir / ".env"
        else:
            env_path = Path(env_path)

        # .env 로드
        if env_path.exists():
            load_dotenv(env_path)

        # YAML 로드
        config_data = {}
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f) or {}

        # Config 객체 생성
        config = cls()

        # StandX 설정
        if 'standx' in config_data:
            sx = config_data['standx']
            config.standx = StandXConfig(
                base_url=sx.get('base_url', config.standx.base_url),
                ws_url=sx.get('ws_url', config.standx.ws_url),
                chain=sx.get('chain', config.standx.chain),
            )

        # Wallet 설정 (우선순위: 환경변수 > 암호화 파일 > config.yaml)
        wallet_data = config_data.get('wallet', {})
        wallet_address = os.getenv('WALLET_ADDRESS', wallet_data.get('address', ''))
        wallet_private_key = os.getenv('WALLET_PRIVATE_KEY', wallet_data.get('private_key', ''))

        # MASTER_PASSWORD가 있으면 암호화된 파일에서 복호화 시도
        master_password = os.getenv('MASTER_PASSWORD')
        if master_password and PasswordCrypto:
            data_dir = base_dir / "data"
            crypto = PasswordCrypto(data_dir)

            if crypto.has_credentials():
                try:
                    cred = crypto.load_credential(master_password, "standx")
                    if cred:
                        wallet_address = cred.address or wallet_address
                        wallet_private_key = cred.private_key or wallet_private_key
                        print("[CONFIG] 암호화된 자격증명에서 지갑 정보 로드 완료")
                except Exception as e:
                    print(f"[CONFIG] 자격증명 복호화 실패: {e}")

        config.wallet = WalletConfig(
            address=wallet_address,
            private_key=wallet_private_key,
        )

        # Strategy 설정
        if 'strategy' in config_data:
            st = config_data['strategy']

            # 동적 거리 설정
            dd = st.get('dynamic_distance', {})
            dynamic_distance = DynamicDistanceConfig(
                enabled=dd.get('enabled', True),
                min_bps=float(dd.get('min_bps', 5.0)),
                max_bps=float(dd.get('max_bps', 9.0)),
                spread_factor=float(dd.get('spread_factor', 0.6)),
                volatility_factor=float(dd.get('volatility_factor', 0.8)),
            )

            # 2+2 전략 설정
            order_distances = st.get('order_distances_bps', config.strategy.order_distances_bps)
            if isinstance(order_distances, list):
                order_distances = [float(d) for d in order_distances]
            else:
                order_distances = config.strategy.order_distances_bps

            config.strategy = StrategyConfig(
                symbols=st.get('symbols', config.strategy.symbols),
                leverage=int(st.get('leverage', config.strategy.leverage)),
                order_size_usd=float(st.get('order_size_usd', config.strategy.order_size_usd)),
                margin_reserve_percent=float(st.get('margin_reserve_percent', config.strategy.margin_reserve_percent)),
                num_orders_per_side=int(st.get('num_orders_per_side', config.strategy.num_orders_per_side)),
                order_distances_bps=order_distances,
                min_distance_bps=float(st.get('min_distance_bps', config.strategy.min_distance_bps)),
                target_distance_bps=float(st.get('target_distance_bps', config.strategy.target_distance_bps)),
                max_distance_bps=float(st.get('max_distance_bps', config.strategy.max_distance_bps)),
                band_warning_bps=float(st.get('band_warning_bps', config.strategy.band_warning_bps)),
                order_lock_seconds=float(st.get('order_lock_seconds', config.strategy.order_lock_seconds)),
                rebalance_cooldown_seconds=float(st.get('rebalance_cooldown_seconds', config.strategy.rebalance_cooldown_seconds)),
                rebalance_on_band_exit=st.get('rebalance_on_band_exit', config.strategy.rebalance_on_band_exit),
                rebalance_threshold_bps=float(st.get('rebalance_threshold_bps', config.strategy.rebalance_threshold_bps)),
                drift_threshold_bps=float(st.get('drift_threshold_bps', config.strategy.drift_threshold_bps)),
                dynamic_distance=dynamic_distance,
                check_interval_seconds=float(st.get('check_interval_seconds', config.strategy.check_interval_seconds)),
            )

        # Safety 설정
        if 'safety' in config_data:
            sf = config_data['safety']

            # Pre-Kill 설정 (신규 주문 일시 중단)
            pk = sf.get('pre_kill', {})
            pre_kill = PreKillConfig(
                volatility_threshold_bps=float(pk.get('volatility_threshold_bps', 15.0)),
                mark_mid_divergence_bps=float(pk.get('mark_mid_divergence_bps', 3.0)),
                pause_duration_seconds=float(pk.get('pause_duration_seconds', 5.0)),
            )

            # Hard Kill 설정
            hk = sf.get('hard_kill', {})
            hard_kill = HardKillConfig(
                min_spread_bps=float(hk.get('min_spread_bps', 1.5)),
                max_volatility_bps=float(hk.get('max_volatility_bps', 30.0)),
                stale_threshold_seconds=float(hk.get('stale_threshold_seconds', 0.5)),
            )

            config.safety = SafetyConfig(
                max_position_usd=float(sf.get('max_position_usd', config.safety.max_position_usd)),
                cancel_if_within_bps=float(sf.get('cancel_if_within_bps', config.safety.cancel_if_within_bps)),
                pre_kill=pre_kill,
                hard_kill=hard_kill,
            )

        # Fill Protection 설정
        if 'fill_protection' in config_data:
            fp = config_data['fill_protection']

            # Binance 보호 설정
            bn = fp.get('binance', {})
            binance_protection = BinanceProtectionConfig(
                enabled=bn.get('enabled', True),
                trigger_bps=float(bn.get('trigger_bps', 3.0)),
                window_seconds=float(bn.get('window_seconds', 0.5)),
                cooldown_seconds=float(bn.get('cooldown_seconds', 0.5)),
            )

            # 큐 프로텍션 설정
            qp = fp.get('queue_protection', {})
            queue_protection = QueueProtectionConfig(
                enabled=qp.get('enabled', True),
                drop_threshold_percent=float(qp.get('drop_threshold_percent', 30.0)),
                window_seconds=float(qp.get('window_seconds', 2.0)),
                min_queue_ahead_usd=float(qp.get('min_queue_ahead_usd', 100.0)),
            )

            config.fill_protection = FillProtectionConfig(
                binance=binance_protection,
                queue=queue_protection,
                check_interval_seconds=float(fp.get('check_interval_seconds', 0.1)),
                smart_protection_threshold_seconds=float(fp.get('smart_protection_threshold_seconds', 2.5)),
            )

        # Telegram 설정 (환경변수 우선)
        tg_data = config_data.get('telegram', {})
        config.telegram = TelegramConfig(
            enabled=tg_data.get('enabled', config.telegram.enabled),
            bot_token=os.getenv('TELEGRAM_BOT_TOKEN', tg_data.get('bot_token', '')),
            chat_id=os.getenv('TELEGRAM_CHAT_ID', tg_data.get('chat_id', '')),
        )

        return config

    def validate(self) -> List[str]:
        """
        설정 유효성 검사

        Returns:
            오류 메시지 목록 (비어있으면 유효)
        """
        errors = []

        # 지갑 검증
        if not self.wallet.address:
            errors.append("지갑 주소가 설정되지 않았습니다 (WALLET_ADDRESS)")
        if not self.wallet.private_key:
            errors.append("지갑 개인키가 설정되지 않았습니다 (WALLET_PRIVATE_KEY)")

        # 전략 검증
        if not self.strategy.symbols:
            errors.append("거래 심볼이 설정되지 않았습니다")
        if self.strategy.order_size_usd <= 0:
            errors.append("주문 크기는 0보다 커야 합니다")
        if self.strategy.min_distance_bps >= self.strategy.max_distance_bps:
            errors.append("최소 거리가 최대 거리보다 크거나 같습니다")

        # 안전 설정 검증
        if self.safety.max_position_usd <= 0:
            errors.append("최대 포지션은 0보다 커야 합니다")

        return errors

    def to_dict(self) -> dict:
        """설정을 딕셔너리로 변환 (민감 정보 마스킹)"""
        return {
            'standx': {
                'base_url': self.standx.base_url,
                'ws_url': self.standx.ws_url,
                'chain': self.standx.chain,
            },
            'wallet': {
                'address': self.wallet.address[:10] + '...' if self.wallet.address else '',
                'private_key': '***' if self.wallet.private_key else '',
            },
            'strategy': {
                'symbols': self.strategy.symbols,
                'order_size_usd': self.strategy.order_size_usd,
                'num_orders_per_side': self.strategy.num_orders_per_side,
                'order_distances_bps': self.strategy.order_distances_bps,
                'min_distance_bps': self.strategy.min_distance_bps,
                'target_distance_bps': self.strategy.target_distance_bps,
                'max_distance_bps': self.strategy.max_distance_bps,
                'band_warning_bps': self.strategy.band_warning_bps,
                'order_lock_seconds': self.strategy.order_lock_seconds,
                'rebalance_cooldown_seconds': self.strategy.rebalance_cooldown_seconds,
                'rebalance_on_band_exit': self.strategy.rebalance_on_band_exit,
                'drift_threshold_bps': self.strategy.drift_threshold_bps,
                'dynamic_distance': {
                    'enabled': self.strategy.dynamic_distance.enabled,
                    'min_bps': self.strategy.dynamic_distance.min_bps,
                    'max_bps': self.strategy.dynamic_distance.max_bps,
                },
                'check_interval_seconds': self.strategy.check_interval_seconds,
            },
            'safety': {
                'max_position_usd': self.safety.max_position_usd,
                'cancel_if_within_bps': self.safety.cancel_if_within_bps,
                'hard_kill': {
                    'min_spread_bps': self.safety.hard_kill.min_spread_bps,
                    'max_volatility_bps': self.safety.hard_kill.max_volatility_bps,
                },
            },
            'fill_protection': {
                'binance': {
                    'enabled': self.fill_protection.binance.enabled,
                    'trigger_bps': self.fill_protection.binance.trigger_bps,
                    'window_seconds': self.fill_protection.binance.window_seconds,
                },
                'queue': {
                    'enabled': self.fill_protection.queue.enabled,
                    'drop_threshold_percent': self.fill_protection.queue.drop_threshold_percent,
                    'min_queue_ahead_usd': self.fill_protection.queue.min_queue_ahead_usd,
                },
                'check_interval_seconds': self.fill_protection.check_interval_seconds,
                'smart_protection_threshold_seconds': self.fill_protection.smart_protection_threshold_seconds,
            },
            'telegram': {
                'enabled': self.telegram.enabled,
                'bot_token': '***' if self.telegram.bot_token else '',
                'chat_id': self.telegram.chat_id,
            },
        }
