"""
RaptorHab Airborne - Command Handler Module

Processes commands received from ground station and generates acknowledgments.
Supports ping, parameter setting, forced capture, and reboot commands.
"""

import logging
import struct
import time
import os
import signal
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Callable, Dict, Any, Tuple

from common.constants import PacketType, CommandParam
from common.protocol import (
    CommandPayload,
    CommandAckPayload,
    build_packet,
    parse_packet,
)
from airborne.config import AirborneConfig

logger = logging.getLogger(__name__)


class CommandStatus(IntEnum):
    """Command acknowledgment status codes."""
    SUCCESS = 0x00
    INVALID_COMMAND = 0x01
    INVALID_PARAM = 0x02
    INVALID_VALUE = 0x03
    EXECUTION_FAILED = 0x04
    NOT_SUPPORTED = 0x05
    BUSY = 0x06


@dataclass
class CommandResult:
    """Result of command execution."""
    success: bool
    status: CommandStatus
    message: str = ""
    data: Optional[bytes] = None


class CommandHandler:
    """
    Handles commands received from ground station.
    
    Processes incoming command packets and dispatches them to appropriate
    handlers. Generates acknowledgment packets for transmission back.
    """
    
    def __init__(
        self,
        config: AirborneConfig,
        capture_callback: Optional[Callable[[], None]] = None,
        set_power_callback: Optional[Callable[[int], bool]] = None,
        get_status_callback: Optional[Callable[[], Dict[str, Any]]] = None,
        camera_settings_callback: Optional[Callable[[str, int], bool]] = None,
    ):
        """
        Initialize command handler.
        
        Args:
            config: Airborne configuration
            capture_callback: Function to trigger image capture
            set_power_callback: Function to set TX power (returns success)
            get_status_callback: Function to get current status dict
            camera_settings_callback: Function to set camera setting (name, value) -> success
        """
        self.config = config
        self._capture_callback = capture_callback
        self._set_power_callback = set_power_callback
        self._get_status_callback = get_status_callback
        self._camera_settings_callback = camera_settings_callback
        
        # Command sequence tracking for duplicate detection
        self._last_cmd_seq: Dict[int, int] = {}  # cmd_type -> last_seq
        self._seq_window = 100  # Accept commands within this sequence window
        
        # Pending ACK queue
        self._pending_acks: list[Tuple[int, bytes]] = []  # (priority, ack_packet)
        
        # Statistics
        self.stats = {
            "commands_received": 0,
            "commands_executed": 0,
            "commands_rejected": 0,
            "acks_generated": 0,
            "duplicates_ignored": 0,
        }
        
        # Reboot flag
        self._reboot_requested = False
        self._reboot_delay = 2.0  # seconds
        
        logger.info("CommandHandler initialized")
    
    def process_packet(self, packet_data: bytes) -> Optional[bytes]:
        """
        Process a received packet that may contain a command.
        
        Args:
            packet_data: Raw packet bytes
            
        Returns:
            ACK packet bytes if command processed, None otherwise
        """
        try:
            parsed = parse_packet(packet_data)
            if parsed is None:
                return None
            
            packet_type, seq, flags, payload = parsed
            
            # Check if this is a command packet
            if packet_type not in (
                PacketType.CMD_PING,
                PacketType.CMD_SETPARAM,
                PacketType.CMD_CAPTURE,
                PacketType.CMD_REBOOT,
            ):
                return None
            
            self.stats["commands_received"] += 1
            logger.info(f"Received command: type={packet_type.name}, seq={seq}")
            
            # Check for duplicate
            if self._is_duplicate(packet_type, seq):
                self.stats["duplicates_ignored"] += 1
                logger.debug(f"Ignoring duplicate command seq={seq}")
                # Still generate ACK for duplicates
                return self._generate_ack(packet_type, seq, CommandStatus.SUCCESS)
            
            # Update sequence tracking
            self._last_cmd_seq[packet_type] = seq
            
            # Dispatch to handler
            result = self._dispatch_command(packet_type, payload)
            
            if result.success:
                self.stats["commands_executed"] += 1
            else:
                self.stats["commands_rejected"] += 1
            
            # Generate ACK
            ack_packet = self._generate_ack(
                packet_type, seq, result.status, result.data
            )
            self.stats["acks_generated"] += 1
            
            return ack_packet
            
        except Exception as e:
            logger.error(f"Error processing command packet: {e}")
            return None
    
    def _is_duplicate(self, cmd_type: PacketType, seq: int) -> bool:
        """Check if command is a duplicate based on sequence number."""
        if cmd_type not in self._last_cmd_seq:
            return False
        
        last_seq = self._last_cmd_seq[cmd_type]
        
        # Handle sequence wraparound
        if abs(seq - last_seq) < self._seq_window:
            return seq <= last_seq
        
        # Large gap - assume wraparound, not duplicate
        return False
    
    def _dispatch_command(
        self, cmd_type: PacketType, payload: bytes
    ) -> CommandResult:
        """Dispatch command to appropriate handler."""
        handlers = {
            PacketType.CMD_PING: self._handle_ping,
            PacketType.CMD_SETPARAM: self._handle_setparam,
            PacketType.CMD_CAPTURE: self._handle_capture,
            PacketType.CMD_REBOOT: self._handle_reboot,
        }
        
        handler = handlers.get(cmd_type)
        if handler is None:
            return CommandResult(
                success=False,
                status=CommandStatus.NOT_SUPPORTED,
                message=f"Unknown command type: {cmd_type}",
            )
        
        try:
            return handler(payload)
        except Exception as e:
            logger.error(f"Command handler error: {e}")
            return CommandResult(
                success=False,
                status=CommandStatus.EXECUTION_FAILED,
                message=str(e),
            )
    
    def _handle_ping(self, payload: bytes) -> CommandResult:
        """Handle ping command - just acknowledge."""
        logger.info("Processing PING command")
        
        # Include status in response if callback available
        response_data = None
        if self._get_status_callback:
            try:
                status = self._get_status_callback()
                # Pack minimal status: uptime (4B), free_mem_kb (2B), cpu_temp (2B)
                uptime = int(status.get("uptime", 0))
                free_mem = int(status.get("free_memory_kb", 0)) & 0xFFFF
                cpu_temp = int(status.get("cpu_temp", 0) * 100) & 0xFFFF
                response_data = struct.pack(">IHH", uptime, free_mem, cpu_temp)
            except Exception as e:
                logger.warning(f"Could not get status for ping response: {e}")
        
        return CommandResult(
            success=True,
            status=CommandStatus.SUCCESS,
            message="Pong",
            data=response_data,
        )
    
    def _handle_setparam(self, payload: bytes) -> CommandResult:
        """Handle parameter setting command."""
        if len(payload) < 3:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_PARAM,
                message="Payload too short",
            )
        
        try:
            cmd = CommandPayload.from_bytes(payload)
        except Exception as e:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_PARAM,
                message=f"Parse error: {e}",
            )
        
        logger.info(f"Processing SETPARAM: param={cmd.param_id}, value={cmd.value}")
        
        # Handle specific parameters
        if cmd.param_id == CommandParam.TX_POWER:
            return self._set_tx_power(cmd.value)
        elif cmd.param_id == CommandParam.IMAGE_QUALITY:
            return self._set_image_quality(cmd.value)
        elif cmd.param_id == CommandParam.CAPTURE_INTERVAL:
            return self._set_capture_interval(cmd.value)
        elif cmd.param_id == CommandParam.TELEMETRY_RATE:
            return self._set_telemetry_rate(cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_BRIGHTNESS:
            return self._set_camera_setting('brightness', cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_CONTRAST:
            return self._set_camera_setting('contrast', cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_SATURATION:
            return self._set_camera_setting('saturation', cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_SHARPNESS:
            return self._set_camera_setting('sharpness', cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_EXPOSURE_COMP:
            return self._set_camera_setting('exposure_comp', cmd.value)
        elif cmd.param_id == CommandParam.CAMERA_AWB_MODE:
            return self._set_camera_setting('awb_mode', cmd.value)
        else:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_PARAM,
                message=f"Unknown parameter: {cmd.param_id}",
            )
    
    def _set_camera_setting(self, setting_name: str, value: int) -> CommandResult:
        """Set a camera setting via callback."""
        if self._camera_settings_callback is None:
            return CommandResult(
                success=False,
                status=CommandStatus.NOT_SUPPORTED,
                message="Camera settings callback not configured",
            )
        
        try:
            if self._camera_settings_callback(setting_name, value):
                return CommandResult(
                    success=True,
                    status=CommandStatus.SUCCESS,
                    message=f"Camera {setting_name} set to {value}",
                )
            else:
                return CommandResult(
                    success=False,
                    status=CommandStatus.INVALID_VALUE,
                    message=f"Invalid value {value} for {setting_name}",
                )
        except Exception as e:
            return CommandResult(
                success=False,
                status=CommandStatus.EXECUTION_FAILED,
                message=f"Failed to set {setting_name}: {e}",
            )
    
    def _set_tx_power(self, value: int) -> CommandResult:
        """Set transmit power."""
        # Validate range (0-22 dBm)
        if not 0 <= value <= 22:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_VALUE,
                message=f"TX power must be 0-22 dBm, got {value}",
            )
        
        if self._set_power_callback:
            if self._set_power_callback(value):
                self.config.tx_power_dbm = value
                return CommandResult(
                    success=True,
                    status=CommandStatus.SUCCESS,
                    message=f"TX power set to {value} dBm",
                )
            else:
                return CommandResult(
                    success=False,
                    status=CommandStatus.EXECUTION_FAILED,
                    message="Failed to set TX power",
                )
        else:
            # No callback, just update config
            self.config.tx_power_dbm = value
            return CommandResult(
                success=True,
                status=CommandStatus.SUCCESS,
                message=f"TX power config updated to {value} dBm",
            )
    
    def _set_image_quality(self, value: int) -> CommandResult:
        """Set WebP image quality."""
        if not 1 <= value <= 100:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_VALUE,
                message=f"Quality must be 1-100, got {value}",
            )
        
        self.config.webp_quality = value
        return CommandResult(
            success=True,
            status=CommandStatus.SUCCESS,
            message=f"Image quality set to {value}",
        )
    
    def _set_capture_interval(self, value: int) -> CommandResult:
        """Set image capture interval in seconds."""
        if not 5 <= value <= 300:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_VALUE,
                message=f"Interval must be 5-300 seconds, got {value}",
            )
        
        self.config.capture_interval_sec = value
        return CommandResult(
            success=True,
            status=CommandStatus.SUCCESS,
            message=f"Capture interval set to {value}s",
        )
    
    def _set_telemetry_rate(self, value: int) -> CommandResult:
        """Set telemetry packet interval."""
        if not 1 <= value <= 100:
            return CommandResult(
                success=False,
                status=CommandStatus.INVALID_VALUE,
                message=f"Rate must be 1-100 packets, got {value}",
            )
        
        self.config.telemetry_interval_packets = value
        return CommandResult(
            success=True,
            status=CommandStatus.SUCCESS,
            message=f"Telemetry interval set to every {value} packets",
        )
    
    def _handle_capture(self, payload: bytes) -> CommandResult:
        """Handle forced image capture command."""
        logger.info("Processing CAPTURE command")
        
        if self._capture_callback is None:
            return CommandResult(
                success=False,
                status=CommandStatus.NOT_SUPPORTED,
                message="Capture callback not configured",
            )
        
        try:
            self._capture_callback()
            return CommandResult(
                success=True,
                status=CommandStatus.SUCCESS,
                message="Capture triggered",
            )
        except Exception as e:
            return CommandResult(
                success=False,
                status=CommandStatus.EXECUTION_FAILED,
                message=f"Capture failed: {e}",
            )
    
    def _handle_reboot(self, payload: bytes) -> CommandResult:
        """Handle reboot command."""
        logger.warning("Processing REBOOT command")
        
        # Verify magic bytes if present
        if len(payload) >= 4:
            magic = struct.unpack(">I", payload[:4])[0]
            expected_magic = 0xDEADBEEF
            if magic != expected_magic:
                return CommandResult(
                    success=False,
                    status=CommandStatus.INVALID_PARAM,
                    message="Invalid reboot magic",
                )
        
        self._reboot_requested = True
        
        return CommandResult(
            success=True,
            status=CommandStatus.SUCCESS,
            message=f"Reboot scheduled in {self._reboot_delay}s",
        )
    
    def _generate_ack(
        self,
        cmd_type: PacketType,
        cmd_seq: int,
        status: CommandStatus,
        data: Optional[bytes] = None,
    ) -> bytes:
        """Generate acknowledgment packet."""
        ack_payload = CommandAckPayload(
            acked_type=cmd_type,
            acked_seq=cmd_seq,
            status=status,
            data=data or b"",
        )
        
        # ACK packets use CMD_ACK type
        ack_packet = build_packet(
            PacketType.CMD_ACK,
            cmd_seq,  # Echo the command sequence
            ack_payload.to_bytes(),
            flags=0,
        )
        
        return ack_packet
    
    def check_reboot(self) -> bool:
        """
        Check if reboot was requested.
        
        Returns:
            True if reboot should be performed
        """
        return self._reboot_requested
    
    def execute_reboot(self) -> None:
        """Execute system reboot."""
        logger.critical("Executing system reboot...")
        
        # Give time for ACK to be transmitted
        time.sleep(self._reboot_delay)
        
        # Try graceful reboot first
        try:
            os.system("sudo reboot")
        except Exception:
            # Force reboot via signal
            os.kill(1, signal.SIGTERM)
    
    def queue_ack(self, ack_packet: bytes, priority: int = 0) -> None:
        """
        Queue an ACK packet for transmission.
        
        Args:
            ack_packet: ACK packet bytes
            priority: Higher priority = transmitted first
        """
        self._pending_acks.append((priority, ack_packet))
        # Sort by priority (descending)
        self._pending_acks.sort(key=lambda x: x[0], reverse=True)
    
    def get_pending_ack(self) -> Optional[bytes]:
        """
        Get next pending ACK packet.
        
        Returns:
            ACK packet bytes or None if queue empty
        """
        if self._pending_acks:
            _, packet = self._pending_acks.pop(0)
            return packet
        return None
    
    def has_pending_acks(self) -> bool:
        """Check if there are pending ACKs."""
        return len(self._pending_acks) > 0
    
    def get_stats(self) -> Dict[str, int]:
        """Get command handler statistics."""
        return self.stats.copy()
    
    def reset_stats(self) -> None:
        """Reset statistics counters."""
        for key in self.stats:
            self.stats[key] = 0


class CommandParser:
    """
    Utility class for parsing command payloads.
    """
    
    @staticmethod
    def parse_setparam(payload: bytes) -> Tuple[int, int]:
        """
        Parse SETPARAM payload.
        
        Args:
            payload: Raw payload bytes
            
        Returns:
            Tuple of (param_id, value)
        """
        if len(payload) < 3:
            raise ValueError("SETPARAM payload too short")
        
        param_id = payload[0]
        value = struct.unpack(">H", payload[1:3])[0]
        return param_id, value
    
    @staticmethod
    def build_setparam(param_id: int, value: int) -> bytes:
        """
        Build SETPARAM payload.
        
        Args:
            param_id: Parameter ID
            value: 16-bit value
            
        Returns:
            Payload bytes
        """
        return struct.pack(">BH", param_id, value & 0xFFFF)


# For testing
if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/home/claude/raptorhab")
    
    from airborne.config import AirborneConfig
    
    logging.basicConfig(level=logging.DEBUG)
    
    config = AirborneConfig()
    
    def test_capture():
        print("Capture triggered!")
    
    def test_set_power(power: int) -> bool:
        print(f"Setting power to {power} dBm")
        return True
    
    def test_get_status():
        return {
            "uptime": 3600,
            "free_memory_kb": 128000,
            "cpu_temp": 45.5,
        }
    
    handler = CommandHandler(
        config,
        capture_callback=test_capture,
        set_power_callback=test_set_power,
        get_status_callback=test_get_status,
    )
    
    # Test ping command
    from common.protocol import build_packet
    from common.constants import PacketType
    
    ping_packet = build_packet(PacketType.CMD_PING, 1, b"", flags=0)
    ack = handler.process_packet(ping_packet)
    print(f"Ping ACK: {ack.hex() if ack else None}")
    
    # Test capture command
    capture_packet = build_packet(PacketType.CMD_CAPTURE, 2, b"", flags=0)
    ack = handler.process_packet(capture_packet)
    print(f"Capture ACK: {ack.hex() if ack else None}")
    
    print(f"Stats: {handler.get_stats()}")
