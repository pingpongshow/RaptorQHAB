"""
RaptorHab Utilities
Logging, helpers, and common utilities
"""

import os
import sys
import time
import logging
import subprocess
from datetime import datetime
from typing import Optional
from functools import wraps


def setup_logging(
    log_path: str,
    level: int = logging.INFO,
    debug: bool = False,
    name: str = "raptorhab"
) -> logging.Logger:
    """
    Setup logging with console and file handlers
    
    Args:
        log_path: Directory for log files
        level: Logging level
        debug: Enable debug mode (more verbose)
        name: Logger name
        
    Returns:
        Configured logger
    """
    os.makedirs(log_path, exist_ok=True)
    
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if debug else level)
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else level)
    console_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # Also configure root logger to ensure all modules get output
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.addHandler(console_handler)
        root_logger.setLevel(logging.DEBUG if debug else level)
    
    # File handler (daily rotation)
    log_filename = datetime.now().strftime("raptorhab_%Y%m%d.log")
    log_filepath = os.path.join(log_path, log_filename)
    
    file_handler = logging.FileHandler(log_filepath)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    return logger


def get_cpu_temperature() -> float:
    """
    Get Raspberry Pi CPU temperature
    
    Returns:
        Temperature in Celsius
    """
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = int(f.read().strip()) / 1000.0
            return temp
    except (IOError, ValueError):
        return 0.0


def get_battery_voltage() -> int:
    """
    Get battery voltage (placeholder - implement based on your ADC setup)
    
    Returns:
        Voltage in millivolts
    """
    # This is a placeholder - implement based on your ADC circuit
    # For example, using MCP3008 ADC or INA219 power monitor
    return 4200  # Placeholder: 4.2V


def reboot_system(delay_sec: int = 5):
    """
    Reboot the system
    
    Args:
        delay_sec: Delay before reboot
    """
    logging.warning(f"System reboot scheduled in {delay_sec} seconds")
    time.sleep(delay_sec)
    subprocess.run(['sudo', 'reboot'], check=True)


def sync_time_from_gps(gps_time: int):
    """
    Set system time from GPS
    
    Args:
        gps_time: Unix timestamp from GPS
    """
    try:
        # Format for date command
        dt = datetime.utcfromtimestamp(gps_time)
        date_str = dt.strftime('%Y-%m-%d %H:%M:%S')
        
        subprocess.run(
            ['sudo', 'date', '-u', '-s', date_str],
            check=True,
            capture_output=True
        )
        logging.info(f"System time set from GPS: {date_str} UTC")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to set system time: {e}")


def get_disk_usage(path: str = '/') -> dict:
    """
    Get disk usage statistics
    
    Args:
        path: Path to check
        
    Returns:
        Dict with total, used, free in bytes and percent
    """
    try:
        stat = os.statvfs(path)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        percent = (used / total) * 100 if total > 0 else 0
        
        return {
            'total': total,
            'used': used,
            'free': free,
            'percent': percent
        }
    except OSError:
        return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}


def get_memory_usage() -> dict:
    """
    Get memory usage statistics
    
    Returns:
        Dict with total, used, free in bytes and percent
    """
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        
        meminfo = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(':')
                value = int(parts[1]) * 1024  # Convert KB to bytes
                meminfo[key] = value
        
        total = meminfo.get('MemTotal', 0)
        available = meminfo.get('MemAvailable', 0)
        used = total - available
        percent = (used / total) * 100 if total > 0 else 0
        
        return {
            'total': total,
            'used': used,
            'free': available,
            'percent': percent
        }
    except (IOError, ValueError):
        return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}


def cleanup_old_files(
    directory: str,
    max_files: int,
    pattern: str = '*'
) -> int:
    """
    Remove oldest files to stay under max_files limit
    
    Args:
        directory: Directory to clean
        max_files: Maximum number of files to keep
        pattern: File pattern to match
        
    Returns:
        Number of files removed
    """
    import glob
    
    files = glob.glob(os.path.join(directory, pattern))
    
    if len(files) <= max_files:
        return 0
    
    # Sort by modification time (oldest first)
    files.sort(key=os.path.getmtime)
    
    # Remove oldest files
    to_remove = files[:len(files) - max_files]
    removed = 0
    
    for filepath in to_remove:
        try:
            os.remove(filepath)
            removed += 1
            logging.debug(f"Removed old file: {filepath}")
        except OSError as e:
            logging.warning(f"Failed to remove {filepath}: {e}")
    
    return removed


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,)
):
    """
    Decorator for retrying function with exponential backoff
    
    Args:
        max_retries: Maximum number of retries
        base_delay: Initial delay between retries
        max_delay: Maximum delay between retries
        exceptions: Tuple of exceptions to catch
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logging.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}): {e}"
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)
            
            logging.error(f"{func.__name__} failed after {max_retries + 1} attempts")
            raise last_exception
        
        return wrapper
    return decorator


class Watchdog:
    """
    Software watchdog timer
    
    Monitors for system hangs and triggers recovery
    """
    
    def __init__(self, timeout_sec: int = 60, callback=None):
        """
        Initialize watchdog
        
        Args:
            timeout_sec: Timeout in seconds
            callback: Function to call on timeout (default: reboot)
        """
        self.timeout_sec = timeout_sec
        self.callback = callback or self._default_callback
        self.last_feed = time.time()
        self._running = False
        self._thread = None
    
    def _default_callback(self):
        """Default timeout action: reboot"""
        logging.critical("Watchdog timeout - rebooting system")
        reboot_system(delay_sec=2)
    
    def feed(self):
        """Reset the watchdog timer"""
        self.last_feed = time.time()
    
    def pet(self):
        """Alias for feed() - reset the watchdog timer"""
        self.feed()
    
    def start(self):
        """Start the watchdog"""
        import threading
        
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        logging.info(f"Watchdog started with {self.timeout_sec}s timeout")
    
    def stop(self):
        """Stop the watchdog"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
    
    def _monitor(self):
        """Watchdog monitoring loop"""
        while self._running:
            time.sleep(1.0)
            
            elapsed = time.time() - self.last_feed
            if elapsed > self.timeout_sec:
                logging.critical(f"Watchdog timeout after {elapsed:.1f}s")
                self._running = False
                self.callback()
                break


class RateLimiter:
    """
    Simple rate limiter for packet transmission
    """
    
    def __init__(self, max_per_second: float):
        """
        Initialize rate limiter
        
        Args:
            max_per_second: Maximum operations per second
        """
        self.min_interval = 1.0 / max_per_second
        self.last_time = 0.0
    
    def wait(self):
        """Wait if necessary to maintain rate limit"""
        now = time.time()
        elapsed = now - self.last_time
        
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        
        self.last_time = time.time()
    
    def can_proceed(self) -> bool:
        """Check if we can proceed without waiting"""
        return (time.time() - self.last_time) >= self.min_interval


def format_bytes(size: int) -> str:
    """Format bytes as human-readable string"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    """Format duration as human-readable string"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds / 3600:.1f}h"
