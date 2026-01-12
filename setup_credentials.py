"""
API 키 암호화 설정 CLI 도구

사용법:
  python setup_credentials.py                    # 대화형 메뉴
  python setup_credentials.py --setup            # 최초 설정
  python setup_credentials.py --add binance      # 거래소 추가
  python setup_credentials.py --list             # 저장된 목록
  python setup_credentials.py --export binance   # 환경변수 형식 출력
  python setup_credentials.py --change-password  # 비밀번호 변경
"""
import argparse
import getpass
import sys
from pathlib import Path

# 모듈 경로 추가
sys.path.insert(0, str(Path(__file__).parent))

from utils.password_crypto import PasswordCrypto, Credential, InvalidToken


# 지원하는 거래소/서비스 목록
SUPPORTED_SERVICES = {
    # CEX 거래소
    "binance": {"name": "Binance", "needs_passphrase": False},
    "bybit": {"name": "Bybit", "needs_passphrase": False},
    "okx": {"name": "OKX", "needs_passphrase": True},
    "gate": {"name": "Gate.io", "needs_passphrase": False},
    "bitget": {"name": "Bitget", "needs_passphrase": True},
    "mexc": {"name": "MEXC", "needs_passphrase": False},
    "kucoin": {"name": "KuCoin", "needs_passphrase": True},
    "htx": {"name": "HTX (Huobi)", "needs_passphrase": False},
    "upbit": {"name": "Upbit", "needs_passphrase": False},
    "bithumb": {"name": "Bithumb", "needs_passphrase": False},

    # DEX / 지갑
    "standx": {"name": "StandX", "needs_wallet": True},
    "hyperliquid": {"name": "Hyperliquid", "needs_wallet": True},

    # 기타 서비스
    "telegram": {"name": "Telegram Bot", "custom_fields": ["bot_token", "chat_id"]},
}


def get_password(prompt: str = "비밀번호: ", confirm: bool = False) -> str:
    """비밀번호 입력 (확인 옵션)"""
    password = getpass.getpass(prompt)

    if confirm:
        password2 = getpass.getpass("비밀번호 확인: ")
        if password != password2:
            print("[ERROR] 비밀번호가 일치하지 않습니다.")
            sys.exit(1)

    return password


def print_header(title: str):
    """헤더 출력"""
    print()
    print("=" * 50)
    print(f"  {title}")
    print("=" * 50)


def cmd_setup(crypto: PasswordCrypto):
    """최초 설정"""
    print_header("API 키 암호화 설정")

    if crypto.is_initialized():
        print("\n[WARNING] 이미 초기화되어 있습니다.")
        response = input("새로 설정하시겠습니까? (y/N): ").strip().lower()
        if response != "y":
            print("취소되었습니다.")
            return

    print("\n암호화에 사용할 마스터 비밀번호를 설정합니다.")
    print("이 비밀번호는 모든 API 키를 암호화/복호화하는 데 사용됩니다.")
    print("[주의] 비밀번호를 잊으면 저장된 모든 키를 잃게 됩니다!")
    print()

    password = get_password("새 비밀번호: ", confirm=True)

    # 빈 자격증명으로 초기화 (Salt 생성)
    crypto.save_credential(password, "_init", Credential())
    crypto.delete_credential(password, "_init")

    print("\n[OK] 설정이 완료되었습니다!")
    print(f"  Salt 파일: {crypto.salt_file}")
    print(f"  자격증명 파일: {crypto.credentials_file}")


def cmd_add(crypto: PasswordCrypto, service_name: str):
    """서비스 추가"""
    service_name = service_name.lower()

    if service_name not in SUPPORTED_SERVICES:
        print(f"[ERROR] 지원하지 않는 서비스: {service_name}")
        print(f"지원 서비스: {', '.join(SUPPORTED_SERVICES.keys())}")
        return

    service = SUPPORTED_SERVICES[service_name]
    print_header(f"{service['name']} API 키 등록")

    # 처음 등록하는 경우: 비밀번호 확인 필요
    if not crypto.has_credentials():
        print("\n[최초 설정] 마스터 비밀번호를 설정합니다.")
        print("[주의] 이 비밀번호를 잊으면 저장된 모든 키를 잃게 됩니다!")
        print()
        password = get_password("새 비밀번호: ", confirm=True)
    else:
        # 기존 자격증명이 있으면 비밀번호 검증
        password = get_password()
        if not crypto.verify_password(password):
            print("[ERROR] 비밀번호가 틀렸습니다.")
            return

    cred = Credential()

    # 서비스별 입력 필드
    if service.get("needs_wallet"):
        # 지갑 기반 서비스
        print(f"\n{service['name']} 지갑 정보를 입력하세요:")
        cred.address = input("  지갑 주소: ").strip()
        cred.private_key = getpass.getpass("  개인키 (0x...): ").strip()
    elif service.get("custom_fields"):
        # 커스텀 필드
        print(f"\n{service['name']} 설정을 입력하세요:")
        for field in service["custom_fields"]:
            if "token" in field.lower() or "secret" in field.lower():
                value = getpass.getpass(f"  {field}: ").strip()
            else:
                value = input(f"  {field}: ").strip()
            setattr(cred, field if hasattr(cred, field) else "api_key", value)
    else:
        # 일반 거래소
        print(f"\n{service['name']} API 키를 입력하세요:")
        cred.api_key = input("  API Key: ").strip()
        cred.api_secret = getpass.getpass("  API Secret: ").strip()

        if service.get("needs_passphrase"):
            cred.passphrase = getpass.getpass("  Passphrase: ").strip()

    # 유효성 검사
    if service.get("needs_wallet"):
        if not cred.address or not cred.private_key:
            print("[ERROR] 지갑 주소와 개인키를 모두 입력해야 합니다.")
            return
    elif not cred.api_key or not cred.api_secret:
        print("[ERROR] API Key와 Secret을 모두 입력해야 합니다.")
        return

    # 저장
    if crypto.save_credential(password, service_name, cred):
        print(f"\n[OK] {service['name']} 자격증명이 암호화되어 저장되었습니다!")
    else:
        print("\n[ERROR] 저장에 실패했습니다.")


def cmd_list(crypto: PasswordCrypto):
    """저장된 자격증명 목록"""
    print_header("저장된 자격증명")

    if not crypto.has_credentials():
        print("\n저장된 자격증명이 없습니다.")
        print("'python setup_credentials.py --add <서비스명>'으로 추가하세요.")
        return

    password = get_password()

    try:
        names = crypto.list_credentials(password)
    except InvalidToken:
        print("[ERROR] 비밀번호가 틀렸습니다.")
        return

    if not names:
        print("\n저장된 자격증명이 없습니다.")
        return

    print(f"\n총 {len(names)}개의 자격증명이 저장되어 있습니다:\n")
    for name in names:
        service = SUPPORTED_SERVICES.get(name, {})
        display_name = service.get("name", name.upper())
        print(f"  - {name} ({display_name})")


def cmd_export(crypto: PasswordCrypto, service_name: str):
    """환경변수 형식으로 출력"""
    service_name = service_name.lower()
    print_header(f"{service_name} 환경변수 출력")

    if not crypto.has_credentials():
        print("\n저장된 자격증명이 없습니다.")
        return

    password = get_password()

    try:
        result = crypto.export_to_env_format(password, service_name)
    except InvalidToken:
        print("[ERROR] 비밀번호가 틀렸습니다.")
        return

    if not result:
        print(f"\n[ERROR] '{service_name}' 자격증명을 찾을 수 없습니다.")
        return

    print(f"\n# {service_name.upper()} 환경변수")
    print(result)
    print("\n[WARNING] 이 내용을 안전하지 않은 곳에 저장하지 마세요!")


def cmd_delete(crypto: PasswordCrypto, service_name: str):
    """자격증명 삭제"""
    service_name = service_name.lower()
    print_header(f"{service_name} 자격증명 삭제")

    if not crypto.has_credentials():
        print("\n저장된 자격증명이 없습니다.")
        return

    password = get_password()

    try:
        if crypto.delete_credential(password, service_name):
            print(f"\n[OK] '{service_name}' 자격증명이 삭제되었습니다.")
        else:
            print(f"\n[ERROR] '{service_name}' 자격증명을 찾을 수 없습니다.")
    except InvalidToken:
        print("[ERROR] 비밀번호가 틀렸습니다.")


def cmd_change_password(crypto: PasswordCrypto):
    """비밀번호 변경"""
    print_header("비밀번호 변경")

    if not crypto.has_credentials():
        print("\n저장된 자격증명이 없습니다. 먼저 --setup을 실행하세요.")
        return

    print("\n현재 비밀번호와 새 비밀번호를 입력하세요.")

    old_password = get_password("현재 비밀번호: ")
    new_password = get_password("새 비밀번호: ", confirm=True)

    try:
        if crypto.change_password(old_password, new_password):
            print("\n[OK] 비밀번호가 변경되었습니다!")
        else:
            print("\n[ERROR] 비밀번호 변경에 실패했습니다.")
    except InvalidToken:
        print("[ERROR] 현재 비밀번호가 틀렸습니다.")


def cmd_interactive(crypto: PasswordCrypto):
    """대화형 메뉴"""
    while True:
        print_header("API 키 암호화 관리")
        print()
        print("  1. 최초 설정 (새 비밀번호 생성)")
        print("  2. 거래소/서비스 추가")
        print("  3. 저장된 목록 보기")
        print("  4. 환경변수 형식 출력")
        print("  5. 자격증명 삭제")
        print("  6. 비밀번호 변경")
        print("  0. 종료")
        print()

        choice = input("선택: ").strip()

        if choice == "0":
            print("\n종료합니다.")
            break
        elif choice == "1":
            cmd_setup(crypto)
        elif choice == "2":
            print("\n지원 서비스:")
            for key, val in SUPPORTED_SERVICES.items():
                print(f"  - {key}: {val['name']}")
            service = input("\n서비스명 입력: ").strip()
            if service:
                cmd_add(crypto, service)
        elif choice == "3":
            cmd_list(crypto)
        elif choice == "4":
            service = input("서비스명 입력: ").strip()
            if service:
                cmd_export(crypto, service)
        elif choice == "5":
            service = input("삭제할 서비스명 입력: ").strip()
            if service:
                cmd_delete(crypto, service)
        elif choice == "6":
            cmd_change_password(crypto)
        else:
            print("\n잘못된 선택입니다.")

        input("\n계속하려면 Enter를 누르세요...")


def main():
    parser = argparse.ArgumentParser(
        description="API 키 암호화 설정 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python setup_credentials.py                    # 대화형 메뉴
  python setup_credentials.py --setup            # 최초 설정
  python setup_credentials.py --add binance      # Binance 추가
  python setup_credentials.py --add standx       # StandX 지갑 추가
  python setup_credentials.py --list             # 목록 보기
  python setup_credentials.py --export binance   # 환경변수 출력
  python setup_credentials.py --delete bybit     # 삭제
  python setup_credentials.py --change-password  # 비밀번호 변경

지원 서비스:
  거래소: binance, bybit, okx, gate, bitget, mexc, kucoin, htx, upbit, bithumb
  지갑: standx, hyperliquid
  기타: telegram
        """
    )

    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="데이터 저장 디렉토리 (기본: ./data)")
    parser.add_argument("--setup", action="store_true",
                        help="최초 설정 (마스터 비밀번호 생성)")
    parser.add_argument("--add", metavar="SERVICE",
                        help="서비스 추가 (예: binance, bybit, standx)")
    parser.add_argument("--list", action="store_true",
                        help="저장된 자격증명 목록")
    parser.add_argument("--export", metavar="SERVICE",
                        help="환경변수 형식으로 출력")
    parser.add_argument("--delete", metavar="SERVICE",
                        help="자격증명 삭제")
    parser.add_argument("--change-password", action="store_true",
                        help="마스터 비밀번호 변경")

    args = parser.parse_args()

    crypto = PasswordCrypto(args.data_dir)

    # 명령 실행
    if args.setup:
        cmd_setup(crypto)
    elif args.add:
        cmd_add(crypto, args.add)
    elif args.list:
        cmd_list(crypto)
    elif args.export:
        cmd_export(crypto, args.export)
    elif args.delete:
        cmd_delete(crypto, args.delete)
    elif args.change_password:
        cmd_change_password(crypto)
    else:
        # 인자 없으면 대화형 메뉴
        cmd_interactive(crypto)


if __name__ == "__main__":
    main()
