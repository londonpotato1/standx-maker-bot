"""
StandX 인증 모듈
- JWT 토큰 획득
- ed25519 서명
- 요청 서명
"""
import base64
import base58
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

import jwt
import requests
from nacl.signing import SigningKey
from eth_account import Account
from eth_account.messages import encode_defunct

from ..utils.logger import get_logger

logger = get_logger('auth')


@dataclass
class AuthToken:
    """인증 토큰"""
    token: str
    address: str
    chain: str
    expires_at: float  # Unix timestamp
    request_id: str
    signing_key: SigningKey  # ed25519 키


class StandXAuth:
    """
    StandX 인증 관리자

    인증 플로우:
    1. ed25519 키 쌍 생성
    2. prepare-signin 호출하여 서명할 메시지 획득
    3. 지갑으로 메시지 서명
    4. login 호출하여 JWT 토큰 획득
    5. 이후 요청에 Bearer 토큰 + 요청 서명 사용
    """

    AUTH_BASE_URL = "https://api.standx.com"

    def __init__(self, wallet_address: str, wallet_private_key: str, chain: str = "bsc"):
        """
        Args:
            wallet_address: 지갑 주소
            wallet_private_key: 지갑 개인키
            chain: 체인 (bsc 또는 solana)
        """
        self.wallet_address = wallet_address
        self.wallet_private_key = wallet_private_key
        self.chain = chain
        self._token: Optional[AuthToken] = None

    def _generate_ed25519_keypair(self) -> Tuple[SigningKey, str]:
        """
        ed25519 키 쌍 생성

        Returns:
            (SigningKey, request_id)
        """
        signing_key = SigningKey.generate()
        public_key = signing_key.verify_key

        # base58 인코딩 (StandX 문서 요구사항)
        request_id = base58.b58encode(bytes(public_key)).decode('utf-8')

        return signing_key, request_id

    def _sign_message_with_wallet(self, message: str) -> str:
        """
        지갑으로 메시지 서명 (BSC/EVM 체인용)

        Args:
            message: 서명할 메시지

        Returns:
            서명 (hex)
        """
        account = Account.from_key(self.wallet_private_key)
        signable = encode_defunct(text=message)
        signed = account.sign_message(signable)
        # StandX expects 0x prefix on signature
        return "0x" + signed.signature.hex()

    def _prepare_signin(self, request_id: str) -> dict:
        """
        서명할 데이터 요청

        Args:
            request_id: ed25519 공개키 (base58)

        Returns:
            signedData JWT
        """
        url = f"{self.AUTH_BASE_URL}/v1/offchain/prepare-signin"
        params = {"chain": self.chain}
        payload = {
            "address": self.wallet_address,
            "requestId": request_id,
        }

        response = requests.post(url, params=params, json=payload, timeout=30)
        response.raise_for_status()

        return response.json()

    def _login(self, signed_data: str, signature: str) -> dict:
        """
        로그인하여 JWT 토큰 획득

        Args:
            signed_data: prepare-signin에서 받은 JWT
            signature: 지갑으로 서명한 결과

        Returns:
            로그인 응답 (token, address, chain 등)
        """
        url = f"{self.AUTH_BASE_URL}/v1/offchain/login"
        params = {"chain": self.chain}
        payload = {
            "signedData": signed_data,
            "signature": signature,
            "expiresSeconds": 604800,  # 7일
        }

        response = requests.post(url, params=params, json=payload, timeout=30)
        response.raise_for_status()

        return response.json()

    def authenticate(self) -> AuthToken:
        """
        전체 인증 플로우 실행

        Returns:
            AuthToken
        """
        logger.info(f"인증 시작: {self.wallet_address[:10]}... (chain={self.chain})")

        # 1. ed25519 키 쌍 생성
        signing_key, request_id = self._generate_ed25519_keypair()
        logger.debug(f"ed25519 키 생성 완료: request_id={request_id[:20]}...")

        # 2. prepare-signin
        logger.debug("prepare-signin 요청 중...")
        prepare_response = self._prepare_signin(request_id)
        signed_data = prepare_response.get('signedData', '')

        if not signed_data:
            raise ValueError("prepare-signin 응답에 signedData가 없습니다")

        # JWT 디코딩하여 메시지 추출 (검증 없이)
        jwt_payload = jwt.decode(signed_data, options={"verify_signature": False})
        message = jwt_payload.get('message', '')

        if not message:
            raise ValueError("JWT에 message가 없습니다")

        logger.debug(f"서명할 메시지: {message[:50]}...")

        # 3. 지갑으로 서명
        signature = self._sign_message_with_wallet(message)
        logger.debug("지갑 서명 완료")

        # 4. 로그인
        logger.debug("login 요청 중...")
        login_response = self._login(signed_data, signature)

        token = login_response.get('token', '')
        if not token:
            raise ValueError("login 응답에 token이 없습니다")

        # 만료 시간 계산 (7일)
        expires_at = time.time() + 604800

        self._token = AuthToken(
            token=token,
            address=login_response.get('address', self.wallet_address),
            chain=login_response.get('chain', self.chain),
            expires_at=expires_at,
            request_id=request_id,
            signing_key=signing_key,
        )

        logger.info(f"인증 성공: {self._token.address[:10]}...")
        return self._token

    def get_token(self) -> AuthToken:
        """
        토큰 가져오기 (필요시 재인증)

        Returns:
            AuthToken
        """
        if self._token is None:
            return self.authenticate()

        # 만료 1시간 전이면 갱신
        if time.time() > self._token.expires_at - 3600:
            logger.info("토큰 만료 임박, 재인증 중...")
            return self.authenticate()

        return self._token

    def get_auth_headers(self) -> dict:
        """
        인증 헤더 생성

        Returns:
            Authorization 헤더
        """
        token = self.get_token()
        return {
            "Authorization": f"Bearer {token.token}",
        }

    def sign_request(self, payload: dict) -> dict:
        """
        요청 본문 서명

        Args:
            payload: 요청 본문

        Returns:
            서명 헤더
        """
        token = self.get_token()

        version = "v1"
        request_id = str(uuid.uuid4())
        timestamp = str(int(time.time() * 1000))
        payload_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)

        # 서명할 메시지: "{version},{id},{timestamp},{payload}"
        message = f"{version},{request_id},{timestamp},{payload_str}"
        message_bytes = message.encode('utf-8')

        # ed25519 서명
        signed = token.signing_key.sign(message_bytes)
        signature = base64.b64encode(signed.signature).decode('utf-8')

        return {
            "x-request-sign-version": version,
            "x-request-id": request_id,
            "x-request-timestamp": timestamp,
            "x-request-signature": signature,
        }

    def is_authenticated(self) -> bool:
        """인증 여부 확인"""
        if self._token is None:
            return False
        return time.time() < self._token.expires_at

    def get_remaining_time(self) -> float:
        """토큰 남은 시간 (초)"""
        if self._token is None:
            return 0
        return max(0, self._token.expires_at - time.time())
