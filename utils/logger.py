"""
로깅 유틸리티
"""
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 컬러 코드 (Windows 터미널 호환)
class Colors:
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'


class ColoredFormatter(logging.Formatter):
    """컬러 로그 포매터"""

    LEVEL_COLORS = {
        logging.DEBUG: Colors.GRAY,
        logging.INFO: Colors.GREEN,
        logging.WARNING: Colors.YELLOW,
        logging.ERROR: Colors.RED,
        logging.CRITICAL: Colors.MAGENTA,
    }

    def format(self, record):
        # 레벨 컬러
        level_color = self.LEVEL_COLORS.get(record.levelno, Colors.WHITE)

        # 시간
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S.%f')[:-3]

        # 레벨명 (고정 폭)
        level_name = f"{record.levelname:<8}"

        # 로거명 (모듈명)
        logger_name = f"[{record.name}]"

        # 최종 포맷
        formatted = (
            f"{Colors.GRAY}{timestamp}{Colors.RESET} "
            f"{level_color}{level_name}{Colors.RESET} "
            f"{Colors.CYAN}{logger_name:<20}{Colors.RESET} "
            f"{record.getMessage()}"
        )

        return formatted


class FileFormatter(logging.Formatter):
    """파일 로그 포매터 (컬러 없음)"""

    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        level_name = f"{record.levelname:<8}"
        logger_name = f"[{record.name}]"

        return f"{timestamp} {level_name} {logger_name:<20} {record.getMessage()}"


# 글로벌 로거 저장소
_loggers: dict[str, logging.Logger] = {}
_initialized = False


def setup_logger(
    name: str = "standx_bot",
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    console: bool = True
) -> logging.Logger:
    """
    로거 초기화

    Args:
        name: 로거 이름
        level: 로그 레벨
        log_file: 로그 파일 경로 (선택)
        console: 콘솔 출력 여부

    Returns:
        설정된 로거
    """
    global _initialized

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 기존 핸들러 제거
    logger.handlers.clear()

    # 콘솔 핸들러
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(ColoredFormatter())
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    # 파일 핸들러
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(FileFormatter())
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    _loggers[name] = logger
    _initialized = True

    return logger


def get_logger(name: str = "standx_bot") -> logging.Logger:
    """
    로거 가져오기

    Args:
        name: 로거 이름

    Returns:
        로거 인스턴스
    """
    if name in _loggers:
        return _loggers[name]

    # 부모 로거 찾기
    parts = name.split('.')
    for i in range(len(parts) - 1, 0, -1):
        parent_name = '.'.join(parts[:i])
        if parent_name in _loggers:
            # 자식 로거 생성
            child_logger = logging.getLogger(name)
            _loggers[name] = child_logger
            return child_logger

    # 기본 로거 반환 (미초기화 시)
    if not _initialized:
        return setup_logger(name)

    return logging.getLogger(name)


# 편의 함수
def debug(msg: str, *args, **kwargs):
    get_logger().debug(msg, *args, **kwargs)

def info(msg: str, *args, **kwargs):
    get_logger().info(msg, *args, **kwargs)

def warning(msg: str, *args, **kwargs):
    get_logger().warning(msg, *args, **kwargs)

def error(msg: str, *args, **kwargs):
    get_logger().error(msg, *args, **kwargs)

def critical(msg: str, *args, **kwargs):
    get_logger().critical(msg, *args, **kwargs)
