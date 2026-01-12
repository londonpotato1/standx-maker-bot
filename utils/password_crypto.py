"""
비밀번호 기반 API 키 암호화/복호화 모듈
- PBKDF2로 비밀번호에서 암호화 키 유도
- Fernet (AES-128-CBC + HMAC-SHA256)으로 암호화
- 파일이 유출되어도 비밀번호 없이는 복호화 불가
"""
import base64
import json
import os
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


@dataclass
class Credential:
    """자격증명 데이터"""
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""  # OKX, Bitget 등에서 사용
    private_key: str = ""  # StandX 지갑 등에서 사용
    address: str = ""  # 지갑 주소


class PasswordCrypto:
    """
    비밀번호 기반 API 키 암호화 관리자

    사용법:
        # 초기화
        crypto = PasswordCrypto(Path("data"))

        # 자격증명 저장 (암호화)
        crypto.save_credential("my_password", "binance", Credential(
            api_key="xxx",
            api_secret="yyy"
        ))

        # 자격증명 로드 (복호화)
        cred = crypto.load_credential("my_password", "binance")
        print(cred.api_key)

    보안:
        - PBKDF2 with SHA256, 480,000 iterations (OWASP 2024 권장)
        - 16바이트 랜덤 Salt
        - Fernet = AES-128-CBC + HMAC-SHA256
        - 비밀번호 틀리면 InvalidToken 예외
    """

    # OWASP 2024 권장값
    PBKDF2_ITERATIONS = 480_000
    SALT_LENGTH = 16

    def __init__(self, data_dir: Path):
        """
        Args:
            data_dir: 데이터 저장 디렉토리 (예: Path("data"))
        """
        self.data_dir = Path(data_dir)
        self.salt_file = self.data_dir / ".salt"
        self.credentials_file = self.data_dir / "credentials.enc"
        self._salt: Optional[bytes] = None

    def _ensure_dir(self):
        """데이터 디렉토리 생성"""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _load_or_create_salt(self) -> bytes:
        """Salt 로드 또는 생성"""
        if self._salt is not None:
            return self._salt

        if self.salt_file.exists():
            self._salt = self.salt_file.read_bytes()
        else:
            self._ensure_dir()
            self._salt = os.urandom(self.SALT_LENGTH)
            self.salt_file.write_bytes(self._salt)
            # 파일 권한 설정 (소유자만 읽기/쓰기)
            try:
                os.chmod(self.salt_file, 0o600)
            except OSError:
                pass  # Windows에서는 무시

        return self._salt

    def _derive_key(self, password: str) -> bytes:
        """
        비밀번호에서 Fernet 키 유도 (PBKDF2)

        Args:
            password: 사용자 비밀번호

        Returns:
            32바이트 키 (base64 인코딩됨)
        """
        salt = self._load_or_create_salt()

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
        )

        key = kdf.derive(password.encode("utf-8"))
        return base64.urlsafe_b64encode(key)

    def _get_fernet(self, password: str) -> Fernet:
        """비밀번호로 Fernet 인스턴스 생성"""
        key = self._derive_key(password)
        return Fernet(key)

    def _load_all_credentials(self, password: str) -> Dict[str, dict]:
        """
        모든 자격증명 로드 (내부용)

        Raises:
            InvalidToken: 비밀번호 틀림
            FileNotFoundError: 파일 없음
        """
        if not self.credentials_file.exists():
            return {}

        encrypted_data = self.credentials_file.read_bytes()
        fernet = self._get_fernet(password)

        try:
            decrypted = fernet.decrypt(encrypted_data)
            return json.loads(decrypted.decode("utf-8"))
        except InvalidToken:
            raise InvalidToken("비밀번호가 틀렸습니다")

    def _save_all_credentials(self, password: str, credentials: Dict[str, dict]):
        """모든 자격증명 저장 (내부용)"""
        self._ensure_dir()

        json_str = json.dumps(credentials, ensure_ascii=False, indent=2)
        fernet = self._get_fernet(password)
        encrypted = fernet.encrypt(json_str.encode("utf-8"))

        self.credentials_file.write_bytes(encrypted)

        # 파일 권한 설정
        try:
            os.chmod(self.credentials_file, 0o600)
        except OSError:
            pass

    # ==================== Public API ====================

    def is_initialized(self) -> bool:
        """초기화 여부 (Salt 파일 존재 확인)"""
        return self.salt_file.exists()

    def has_credentials(self) -> bool:
        """저장된 자격증명 존재 여부"""
        return self.credentials_file.exists()

    def verify_password(self, password: str) -> bool:
        """
        비밀번호 검증

        Returns:
            True: 비밀번호 맞음
            False: 비밀번호 틀림 또는 파일 없음
        """
        try:
            self._load_all_credentials(password)
            return True
        except (InvalidToken, FileNotFoundError):
            return False

    def save_credential(self, password: str, name: str, credential: Credential) -> bool:
        """
        자격증명 저장 (암호화)

        Args:
            password: 암호화 비밀번호
            name: 식별자 (예: "binance", "bybit", "standx_wallet")
            credential: 자격증명 데이터

        Returns:
            성공 여부

        Note:
            기존 자격증명이 있으면 덮어씀
        """
        try:
            # 기존 데이터 로드 (없으면 빈 딕셔너리)
            try:
                all_creds = self._load_all_credentials(password)
            except (InvalidToken, FileNotFoundError):
                all_creds = {}

            # 새 자격증명 추가/업데이트
            all_creds[name] = asdict(credential)

            # 저장
            self._save_all_credentials(password, all_creds)
            return True

        except Exception as e:
            print(f"[ERROR] 자격증명 저장 실패: {e}")
            return False

    def load_credential(self, password: str, name: str) -> Optional[Credential]:
        """
        자격증명 로드 (복호화)

        Args:
            password: 암호화 비밀번호
            name: 식별자

        Returns:
            Credential 또는 None

        Raises:
            InvalidToken: 비밀번호 틀림
        """
        all_creds = self._load_all_credentials(password)

        if name not in all_creds:
            return None

        data = all_creds[name]
        return Credential(**data)

    def list_credentials(self, password: str) -> List[str]:
        """
        저장된 자격증명 이름 목록

        Returns:
            이름 목록 (예: ["binance", "bybit"])

        Raises:
            InvalidToken: 비밀번호 틀림
        """
        all_creds = self._load_all_credentials(password)
        return list(all_creds.keys())

    def delete_credential(self, password: str, name: str) -> bool:
        """
        자격증명 삭제

        Returns:
            성공 여부
        """
        try:
            all_creds = self._load_all_credentials(password)

            if name not in all_creds:
                return False

            del all_creds[name]
            self._save_all_credentials(password, all_creds)
            return True

        except InvalidToken:
            raise
        except Exception:
            return False

    def change_password(self, old_password: str, new_password: str) -> bool:
        """
        비밀번호 변경

        Args:
            old_password: 기존 비밀번호
            new_password: 새 비밀번호

        Returns:
            성공 여부

        Note:
            모든 데이터를 새 비밀번호로 재암호화
        """
        try:
            # 기존 비밀번호로 복호화
            all_creds = self._load_all_credentials(old_password)

            # 새 Salt 생성
            self._salt = os.urandom(self.SALT_LENGTH)
            self.salt_file.write_bytes(self._salt)

            # 새 비밀번호로 재암호화
            self._save_all_credentials(new_password, all_creds)
            return True

        except InvalidToken:
            raise
        except Exception as e:
            print(f"[ERROR] 비밀번호 변경 실패: {e}")
            return False

    def export_to_env_format(self, password: str, name: str) -> Optional[str]:
        """
        환경변수 형식으로 내보내기 (디버깅/마이그레이션용)

        Returns:
            환경변수 문자열 또는 None
        """
        cred = self.load_credential(password, name)
        if not cred:
            return None

        lines = []
        prefix = name.upper()

        if cred.api_key:
            lines.append(f"{prefix}_API_KEY={cred.api_key}")
        if cred.api_secret:
            lines.append(f"{prefix}_API_SECRET={cred.api_secret}")
        if cred.passphrase:
            lines.append(f"{prefix}_PASSPHRASE={cred.passphrase}")
        if cred.private_key:
            lines.append(f"{prefix}_PRIVATE_KEY={cred.private_key}")
        if cred.address:
            lines.append(f"{prefix}_ADDRESS={cred.address}")

        return "\n".join(lines)


# ==================== 편의 함수 ====================

def quick_encrypt(password: str, data: str, data_dir: Path = Path("data")) -> str:
    """
    단순 문자열 암호화 (빠른 사용용)

    Args:
        password: 비밀번호
        data: 암호화할 문자열
        data_dir: Salt 저장 디렉토리

    Returns:
        암호화된 문자열 (base64)
    """
    crypto = PasswordCrypto(data_dir)
    fernet = crypto._get_fernet(password)
    encrypted = fernet.encrypt(data.encode("utf-8"))
    return base64.urlsafe_b64encode(encrypted).decode("utf-8")


def quick_decrypt(password: str, encrypted: str, data_dir: Path = Path("data")) -> str:
    """
    단순 문자열 복호화

    Args:
        password: 비밀번호
        encrypted: 암호화된 문자열 (base64)
        data_dir: Salt 저장 디렉토리

    Returns:
        복호화된 문자열

    Raises:
        InvalidToken: 비밀번호 틀림
    """
    crypto = PasswordCrypto(data_dir)
    fernet = crypto._get_fernet(password)
    encrypted_bytes = base64.urlsafe_b64decode(encrypted.encode("utf-8"))
    return fernet.decrypt(encrypted_bytes).decode("utf-8")


# ==================== 테스트 ====================

if __name__ == "__main__":
    import tempfile
    import shutil

    print("=" * 50)
    print("PasswordCrypto 테스트")
    print("=" * 50)

    # 임시 디렉토리에서 테스트
    test_dir = Path(tempfile.mkdtemp())

    try:
        crypto = PasswordCrypto(test_dir)
        test_password = "test_password_123"

        # 1. 자격증명 저장
        print("\n[1] 자격증명 저장 테스트")
        cred = Credential(
            api_key="my_api_key_12345",
            api_secret="my_api_secret_67890",
            passphrase="my_passphrase"
        )
        result = crypto.save_credential(test_password, "binance", cred)
        print(f"  저장 결과: {'성공' if result else '실패'}")

        # 2. 자격증명 로드
        print("\n[2] 자격증명 로드 테스트")
        loaded = crypto.load_credential(test_password, "binance")
        if loaded:
            print(f"  API Key: {loaded.api_key}")
            print(f"  API Secret: {loaded.api_secret}")
            print(f"  일치 여부: {loaded.api_key == cred.api_key}")

        # 3. 잘못된 비밀번호
        print("\n[3] 잘못된 비밀번호 테스트")
        try:
            crypto.load_credential("wrong_password", "binance")
            print("  실패: 예외가 발생해야 함")
        except InvalidToken as e:
            print(f"  성공: {e}")

        # 4. 목록 조회
        print("\n[4] 목록 조회 테스트")
        crypto.save_credential(test_password, "bybit", Credential(api_key="bybit_key"))
        names = crypto.list_credentials(test_password)
        print(f"  저장된 자격증명: {names}")

        # 5. 비밀번호 변경
        print("\n[5] 비밀번호 변경 테스트")
        new_password = "new_password_456"
        result = crypto.change_password(test_password, new_password)
        print(f"  변경 결과: {'성공' if result else '실패'}")

        # 새 비밀번호로 로드
        loaded = crypto.load_credential(new_password, "binance")
        print(f"  새 비밀번호로 로드: {'성공' if loaded else '실패'}")

        # 6. 삭제
        print("\n[6] 삭제 테스트")
        result = crypto.delete_credential(new_password, "bybit")
        print(f"  삭제 결과: {'성공' if result else '실패'}")
        names = crypto.list_credentials(new_password)
        print(f"  남은 자격증명: {names}")

        print("\n" + "=" * 50)
        print("모든 테스트 완료!")
        print("=" * 50)

    finally:
        # 테스트 디렉토리 정리
        shutil.rmtree(test_dir, ignore_errors=True)
