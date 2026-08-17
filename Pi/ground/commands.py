"""
RaptorHab Ground Station - Command Transmitter
Sends commands to the airborne payload and handles acknowledgments

Features channel monitoring to detect when the airborne unit is in RX mode,
allowing commands to be sent during transmission gaps.
"""

import logging
import time
import struct
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, List
from enum import IntEnum, auto
from queue import Queue, Empty

from common.constants import PacketType, CommandParam, PacketFlags
from common.protocol import build_packet, CommandPayload, CommandAckPayload

logger = logging.getLogger(__name__)


class CommandStatus(IntEnum):
    """Status of a sent command"""
    PENDING = auto()
    QUEUED = auto()  # Waiting for channel gap
    SENT = auto()
    ACKED = auto()
    TIMEOUT = auto()
    FAILED = auto()


@dataclass
class PendingCommand:
    """A command waiting for acknowledgment"""
    command_type: PacketType
    sequence: int
    payload: bytes
    sent_at: float
    retries: int = 0
    status: CommandStatus = CommandStatus.PENDING
    ack_data: Optional[bytes] = None
    callback: Optional[Callable] = None


class ChannelMonitor:
    """
    Monitors the radio channel to detect transmission gaps.
    
    The airborne unit operates in cycles:
    - TX period (default 10s): Airborne transmits packets
    - RX period (default 10s): Airborne listens for commands
    
    This monitor detects when the airborne unit has stopped transmitting
    (entered RX mode) so commands can be sent during this window.
    """
    
    def __init__(
        self,
        gap_threshold_sec: float = 0.5,
        tx_period_sec: float = 10.0,
        rx_period_sec: float = 10.0
    ):
        """
        Initialize channel monitor
        
        Args:
            gap_threshold_sec: Seconds without packets to consider channel clear
                               (should be less than the shortest expected RX window)
            tx_period_sec: Expected airborne TX period duration
            rx_period_sec: Expected airborne RX period duration
        """
        self.gap_threshold_sec = gap_threshold_sec
        self.tx_period_sec = tx_period_sec
        self.rx_period_sec = rx_period_sec
        
        self._last_packet_time: float = 0
        self._last_tx_start: float = 0  # Estimated start of last TX period
        self._packets_in_burst: int = 0
        self._lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'packets_observed': 0,
            'gaps_detected': 0,
            'tx_bursts_detected': 0,
        }
    
    def packet_received(self, timestamp: float = None):
        """
        Notify monitor that a packet was received
        
        Args:
            timestamp: Packet receive time (defaults to now)
        """
        now = timestamp or time.time()
        
        with self._lock:
            # Check if this starts a new TX burst
            if now - self._last_packet_time > self.gap_threshold_sec:
                # Gap detected - new burst starting
                self._last_tx_start = now
                self._packets_in_burst = 0
                self.stats['tx_bursts_detected'] += 1
            
            self._last_packet_time = now
            self._packets_in_burst += 1
            self.stats['packets_observed'] += 1
    
    def is_channel_clear(self) -> bool:
        """
        Check if the channel appears to be clear (airborne in RX mode)
        
        Returns:
            True if no recent packets (channel likely clear)
        """
        with self._lock:
            if self._last_packet_time == 0:
                # No packets ever received - channel state unknown
                # Allow transmission but be cautious
                return True
            
            elapsed = time.time() - self._last_packet_time
            return elapsed >= self.gap_threshold_sec
    
    def get_time_until_clear(self) -> float:
        """
        Estimate time until channel will be clear
        
        Returns:
            Estimated seconds until channel clear (0 if already clear)
        """
        with self._lock:
            if self._last_packet_time == 0:
                return 0
            
            elapsed = time.time() - self._last_packet_time
            remaining = self.gap_threshold_sec - elapsed
            return max(0, remaining)
    
    def get_time_in_rx_window(self) -> float:
        """
        Estimate how long we've been in the RX window
        
        Returns:
            Seconds since channel became clear (0 if not clear)
        """
        with self._lock:
            if not self.is_channel_clear():
                return 0
            
            elapsed = time.time() - self._last_packet_time
            return elapsed - self.gap_threshold_sec
    
    def get_remaining_rx_window(self) -> float:
        """
        Estimate remaining time in RX window before next TX burst
        
        Returns:
            Estimated seconds remaining in RX window
        """
        with self._lock:
            if self._last_packet_time == 0:
                return self.rx_period_sec
            
            # Time since last packet
            elapsed = time.time() - self._last_packet_time
            
            # If still in TX period, return 0
            if elapsed < self.gap_threshold_sec:
                return 0
            
            # Estimate remaining RX window
            time_in_rx = elapsed - self.gap_threshold_sec
            remaining = self.rx_period_sec - time_in_rx
            return max(0, remaining)
    
    def wait_for_gap(self, timeout_sec: float = 15.0, poll_interval: float = 0.1) -> bool:
        """
        Wait for a transmission gap
        
        Args:
            timeout_sec: Maximum time to wait
            poll_interval: How often to check
            
        Returns:
            True if gap detected, False if timeout
        """
        deadline = time.time() + timeout_sec
        
        while time.time() < deadline:
            if self.is_channel_clear():
                with self._lock:
                    self.stats['gaps_detected'] += 1
                return True
            time.sleep(poll_interval)
        
        return False
    
    def get_stats(self) -> dict:
        """Get monitoring statistics"""
        with self._lock:
            return {
                **self.stats,
                'last_packet_age': time.time() - self._last_packet_time if self._last_packet_time > 0 else None,
                'channel_clear': self.is_channel_clear(),
                'packets_in_burst': self._packets_in_burst,
            }


class CommandTransmitter:
    """
    Handles command transmission to airborne payload
    
    Features:
    - Channel monitoring - waits for TX gaps before sending
    - Command queuing
    - Automatic retransmission
    - ACK tracking
    - Timeout handling
    """
    
    def __init__(
        self,
        transmit_func: Callable[[bytes], bool],
        ack_timeout_sec: float = 5.0,
        max_retries: int = 3,
        retry_delay_sec: float = 2.0,
        gap_threshold_sec: float = 0.5,
        wait_for_gap: bool = True,
        gap_wait_timeout_sec: float = 15.0
    ):
        """
        Initialize command transmitter
        
        Args:
            transmit_func: Function to transmit packet bytes
            ack_timeout_sec: Timeout waiting for ACK
            max_retries: Maximum retransmission attempts
            retry_delay_sec: Delay between retries
            gap_threshold_sec: Seconds without packets to consider channel clear
                               (should be less than the shortest expected RX window)
            wait_for_gap: Whether to wait for channel gap before transmitting
            gap_wait_timeout_sec: Max time to wait for a gap
        """
        self.transmit_func = transmit_func
        self.ack_timeout_sec = ack_timeout_sec
        self.max_retries = max_retries
        self.retry_delay_sec = retry_delay_sec
        self.wait_for_gap = wait_for_gap
        self.gap_wait_timeout_sec = gap_wait_timeout_sec
        
        # Channel monitor for gap detection
        self.channel_monitor = ChannelMonitor(
            gap_threshold_sec=gap_threshold_sec
        )
        
        # Command tracking
        self._sequence: int = 0
        self._pending: Dict[int, PendingCommand] = {}  # seq -> PendingCommand
        self._lock = threading.Lock()
        
        # Command queue for deferred transmission
        self._tx_queue: Queue = Queue(maxsize=100)
        self._queue_worker_running = False
        self._queue_worker_thread: Optional[threading.Thread] = None
        
        # Statistics
        self.stats = {
            'commands_sent': 0,
            'commands_acked': 0,
            'commands_timeout': 0,
            'commands_failed': 0,
            'commands_queued': 0,
            'retransmissions': 0,
            'gaps_waited': 0,
        }
    
    def start_queue_worker(self):
        """Start background worker that waits for gaps and sends queued commands"""
        if self._queue_worker_running:
            return
        
        self._queue_worker_running = True
        self._queue_worker_thread = threading.Thread(
            target=self._queue_worker_loop,
            daemon=True,
            name="CommandQueueWorker"
        )
        self._queue_worker_thread.start()
        logger.info("Command queue worker started")
    
    def stop_queue_worker(self):
        """Stop the queue worker"""
        self._queue_worker_running = False
        if self._queue_worker_thread:
            self._queue_worker_thread.join(timeout=5.0)
            self._queue_worker_thread = None
        logger.info("Command queue worker stopped")
    
    def _queue_worker_loop(self):
        """Background worker that processes command queue during gaps"""
        while self._queue_worker_running:
            try:
                # Wait for a command in queue (with timeout to check running flag)
                try:
                    cmd_type, seq, packet, pending = self._tx_queue.get(timeout=0.1)
                except Empty:
                    continue
                
                # Wait for channel to be clear (with periodic checks for shutdown)
                if self.wait_for_gap:
                    logger.debug(f"Waiting for channel gap to send {cmd_type.name} seq={seq}")
                    waited = 0.0
                    max_wait = self.gap_wait_timeout_sec
                    gap_found = False
                    
                    while waited < max_wait and self._queue_worker_running:
                        if self.channel_monitor.is_channel_clear():
                            self.stats['gaps_waited'] += 1
                            logger.info(f"Channel clear - sending {cmd_type.name} seq={seq}")
                            gap_found = True
                            break
                        time.sleep(0.1)
                        waited += 0.1
                    
                    if not gap_found and self._queue_worker_running:
                        logger.warning(f"Timeout waiting for channel gap - sending anyway")
                
                # Transmit
                success = self._do_transmit(packet, seq)
                
                if success:
                    with self._lock:
                        pending.status = CommandStatus.SENT
                        pending.sent_at = time.time()  # Update send time
                    self.stats['commands_sent'] += 1
                    logger.info(f"Sent command {cmd_type.name} seq={seq}")
                else:
                    with self._lock:
                        pending.status = CommandStatus.FAILED
                        if seq in self._pending:
                            del self._pending[seq]
                    self.stats['commands_failed'] += 1
                    logger.error(f"Failed to send command {cmd_type.name}")
                
                self._tx_queue.task_done()
                
            except Exception as e:
                logger.error(f"Queue worker error: {e}")
    
    def notify_packet_received(self, timestamp: float = None):
        """
        Notify that a packet was received from airborne unit.
        Call this for every received packet to enable gap detection.
        
        Args:
            timestamp: Packet receive time (defaults to now)
        """
        self.channel_monitor.packet_received(timestamp)
    
    def _next_sequence(self) -> int:
        """Get next sequence number"""
        with self._lock:
            seq = self._sequence
            self._sequence = (self._sequence + 1) % 65536
            return seq
    
    def send_ping(self, callback: Optional[Callable] = None) -> int:
        """
        Send ping command
        
        Args:
            callback: Called when ACK received
            
        Returns:
            Sequence number
        """
        import sys
        print(">>> CommandTransmitter.send_ping() called", file=sys.stderr, flush=True)
        return self._send_command(PacketType.CMD_PING, b"", callback)
    
    def send_capture(self, callback: Optional[Callable] = None) -> int:
        """
        Send image capture command
        
        Args:
            callback: Called when ACK received
            
        Returns:
            Sequence number
        """
        return self._send_command(PacketType.CMD_CAPTURE, b"", callback)
    
    def send_set_param(
        self,
        param: CommandParam,
        value: int,
        callback: Optional[Callable] = None
    ) -> int:
        """
        Send parameter setting command
        
        Args:
            param: Parameter ID
            value: Parameter value (16-bit)
            callback: Called when ACK received
            
        Returns:
            Sequence number
        """
        payload = struct.pack(">BH", param, value & 0xFFFF)
        return self._send_command(PacketType.CMD_SETPARAM, payload, callback)
    
    def send_set_tx_power(self, power_dbm: int, callback: Optional[Callable] = None) -> int:
        """Set TX power"""
        return self.send_set_param(CommandParam.TX_POWER, power_dbm, callback)
    
    def send_set_image_quality(self, quality: int, callback: Optional[Callable] = None) -> int:
        """Set image quality (1-100)"""
        return self.send_set_param(CommandParam.IMAGE_QUALITY, quality, callback)
    
    def send_set_capture_interval(self, interval_sec: int, callback: Optional[Callable] = None) -> int:
        """Set capture interval in seconds"""
        return self.send_set_param(CommandParam.CAPTURE_INTERVAL, interval_sec, callback)
    
    # Camera image adjustment methods (values 0-200, 100 = neutral/default)
    def send_set_brightness(self, value: int, callback: Optional[Callable] = None) -> int:
        """Set camera brightness (0-200, 100=normal)"""
        return self.send_set_param(CommandParam.CAMERA_BRIGHTNESS, value, callback)
    
    def send_set_contrast(self, value: int, callback: Optional[Callable] = None) -> int:
        """Set camera contrast (0-200, 100=normal)"""
        return self.send_set_param(CommandParam.CAMERA_CONTRAST, value, callback)
    
    def send_set_saturation(self, value: int, callback: Optional[Callable] = None) -> int:
        """Set camera saturation (0-200, 100=normal)"""
        return self.send_set_param(CommandParam.CAMERA_SATURATION, value, callback)
    
    def send_set_sharpness(self, value: int, callback: Optional[Callable] = None) -> int:
        """Set camera sharpness (0-200, 100=normal)"""
        return self.send_set_param(CommandParam.CAMERA_SHARPNESS, value, callback)
    
    def send_set_exposure_comp(self, value: int, callback: Optional[Callable] = None) -> int:
        """Set exposure compensation (0-200, 100=0EV)"""
        return self.send_set_param(CommandParam.CAMERA_EXPOSURE_COMP, value, callback)
    
    def send_set_awb_mode(self, mode: int, callback: Optional[Callable] = None) -> int:
        """Set auto white balance mode (0=auto, 1=daylight, 2=cloudy, etc.)"""
        return self.send_set_param(CommandParam.CAMERA_AWB_MODE, mode, callback)
    
    def send_reboot(self, callback: Optional[Callable] = None) -> int:
        """
        Send reboot command
        
        Args:
            callback: Called when ACK received
            
        Returns:
            Sequence number
        """
        # Reboot requires magic bytes
        magic = struct.pack(">I", 0xDEADBEEF)
        return self._send_command(PacketType.CMD_REBOOT, magic, callback)
    
    def _send_command(
        self,
        cmd_type: PacketType,
        payload: bytes,
        callback: Optional[Callable] = None,
        immediate: bool = False
    ) -> int:
        """
        Send a command packet
        """
        import sys
        print(f">>> _send_command({cmd_type.name}) ENTER", file=sys.stderr, flush=True)
        
        seq = self._next_sequence()
        print(f">>> seq={seq}", file=sys.stderr, flush=True)
        
        # Build packet
        packet = build_packet(cmd_type, seq, payload, PacketFlags.NONE)
        print(f">>> packet built, size={len(packet)}", file=sys.stderr, flush=True)
        
        # Create pending command
        pending = PendingCommand(
            command_type=cmd_type,
            sequence=seq,
            payload=payload,
            sent_at=time.time(),
            callback=callback,
            status=CommandStatus.PENDING
        )
        
        with self._lock:
            self._pending[seq] = pending
        print(f">>> pending added", file=sys.stderr, flush=True)
        
        # Transmit immediately - no channel checking
        print(f">>> calling transmit_func...", file=sys.stderr, flush=True)
        
        if self.transmit_func is None:
            print(f">>> ERROR: transmit_func is None!", file=sys.stderr, flush=True)
            return seq
            
        try:
            success = self.transmit_func(packet)
            print(f">>> transmit_func returned {success}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f">>> EXCEPTION: {e}", file=sys.stderr, flush=True)
            success = False
        
        if success:
            with self._lock:
                pending.status = CommandStatus.SENT
            self.stats['commands_sent'] += 1
        else:
            with self._lock:
                pending.status = CommandStatus.FAILED
            self.stats['commands_failed'] += 1
        
        print(f">>> _send_command EXIT, seq={seq}", file=sys.stderr, flush=True)
        return seq
    
    def _do_transmit(self, packet: bytes, seq: int) -> bool:
        """Actually transmit a packet"""
        import sys
        print(f">>> _do_transmit() called, seq={seq}", file=sys.stderr, flush=True)
        try:
            if self.transmit_func is None:
                print(">>> ERROR: transmit_func is None!", file=sys.stderr, flush=True)
                return False
            result = self.transmit_func(packet)
            print(f">>> transmit_func returned {result}", file=sys.stderr, flush=True)
            return result
        except Exception as e:
            print(f">>> EXCEPTION in _do_transmit: {e}", file=sys.stderr, flush=True)
            return False
    
    def _transmit(self, packet: bytes, seq: int) -> bool:
        """Transmit a packet (legacy method, calls _do_transmit)"""
        return self._do_transmit(packet, seq)
    
    def process_ack(self, ack_payload: CommandAckPayload, packet_seq: int) -> bool:
        """
        Process a received ACK packet
        
        Args:
            ack_payload: Decoded ACK payload
            packet_seq: Sequence number from packet
            
        Returns:
            True if ACK matched a pending command
        """
        acked_seq = ack_payload.acked_seq
        
        with self._lock:
            if acked_seq not in self._pending:
                logger.debug(f"ACK for unknown sequence {acked_seq}")
                return False
            
            pending = self._pending[acked_seq]
            pending.status = CommandStatus.ACKED
            pending.ack_data = ack_payload.data
            
            del self._pending[acked_seq]
        
        self.stats['commands_acked'] += 1
        logger.info(
            f"Received ACK for {pending.command_type.name} seq={acked_seq} "
            f"status={ack_payload.status}"
        )
        
        # Call callback
        if pending.callback:
            try:
                pending.callback(acked_seq, ack_payload.status, ack_payload.data)
            except Exception as e:
                logger.error(f"ACK callback error: {e}")
        
        return True
    
    def check_timeouts(self) -> List[int]:
        """
        Check for timed-out commands and retransmit if needed.
        Retransmissions respect channel state if enabled.
        
        Returns:
            List of timed-out sequence numbers
        """
        now = time.time()
        timed_out = []
        to_retransmit = []
        
        with self._lock:
            for seq, pending in list(self._pending.items()):
                if pending.status == CommandStatus.SENT:
                    if now - pending.sent_at > self.ack_timeout_sec:
                        if pending.retries < self.max_retries:
                            to_retransmit.append((seq, pending))
                        else:
                            pending.status = CommandStatus.TIMEOUT
                            timed_out.append(seq)
                            del self._pending[seq]
        
        # Retransmit - check channel state but don't block
        if to_retransmit:
            channel_clear = self.channel_monitor.is_channel_clear()
            if self.wait_for_gap and not channel_clear:
                logger.debug("Channel busy, deferring retransmissions")
                return timed_out  # Don't retransmit yet, will retry next check
        
        for seq, pending in to_retransmit:
            pending.retries += 1
            pending.sent_at = time.time()
            
            packet = build_packet(
                pending.command_type,
                pending.sequence,
                pending.payload,
                PacketFlags.RETRANSMIT
            )
            
            logger.info(f"Retransmitting {pending.command_type.name} seq={seq} (attempt {pending.retries})")
            self._do_transmit(packet, seq)
            self.stats['retransmissions'] += 1
        
        # Mark timeouts
        for seq in timed_out:
            self.stats['commands_timeout'] += 1
            logger.warning(f"Command seq={seq} timed out after {self.max_retries} retries")
        
        return timed_out
    
    def get_pending_count(self) -> int:
        """Get number of pending commands"""
        with self._lock:
            return len(self._pending)
    
    def get_pending_commands(self) -> List[Dict]:
        """Get list of pending commands"""
        with self._lock:
            return [
                {
                    'sequence': p.sequence,
                    'type': p.command_type.name,
                    'status': p.status.name,
                    'sent_at': p.sent_at,
                    'retries': p.retries,
                }
                for p in self._pending.values()
            ]
    
    def get_stats(self) -> Dict:
        """Get command statistics including channel monitor info"""
        channel_stats = self.channel_monitor.get_stats()
        return {
            **self.stats,
            'pending': self.get_pending_count(),
            'queue_size': self._tx_queue.qsize(),
            'wait_for_gap': self.wait_for_gap,
            'channel_clear': channel_stats.get('channel_clear', True),
            'last_packet_age': channel_stats.get('last_packet_age'),
            'gaps_detected': channel_stats.get('gaps_detected', 0),
        }
    
    def set_wait_for_gap(self, enabled: bool):
        """Enable or disable waiting for channel gaps"""
        self.wait_for_gap = enabled
        logger.info(f"Wait for gap {'enabled' if enabled else 'disabled'}")
    
    def is_channel_clear(self) -> bool:
        """Check if channel is currently clear"""
        return self.channel_monitor.is_channel_clear()
    
    def clear_pending(self):
        """Clear all pending commands"""
        with self._lock:
            self._pending.clear()


class CommandQueue:
    """
    Queue for commands to be sent during TX windows
    
    Useful when ground station has limited TX opportunities
    """
    
    def __init__(self, max_size: int = 100):
        """
        Initialize command queue
        
        Args:
            max_size: Maximum queued commands
        """
        self._queue: Queue = Queue(maxsize=max_size)
    
    def queue_ping(self):
        """Queue a ping command"""
        self._queue.put(('ping', {}))
    
    def queue_capture(self):
        """Queue a capture command"""
        self._queue.put(('capture', {}))
    
    def queue_set_param(self, param: CommandParam, value: int):
        """Queue a parameter change"""
        self._queue.put(('set_param', {'param': param, 'value': value}))
    
    def queue_reboot(self):
        """Queue a reboot command"""
        self._queue.put(('reboot', {}))
    
    def process_queue(self, transmitter: CommandTransmitter) -> int:
        """
        Process queued commands
        
        Args:
            transmitter: CommandTransmitter to use
            
        Returns:
            Number of commands processed
        """
        count = 0
        
        while not self._queue.empty():
            try:
                cmd_type, params = self._queue.get_nowait()
                
                if cmd_type == 'ping':
                    transmitter.send_ping()
                elif cmd_type == 'capture':
                    transmitter.send_capture()
                elif cmd_type == 'set_param':
                    transmitter.send_set_param(params['param'], params['value'])
                elif cmd_type == 'reboot':
                    transmitter.send_reboot()
                
                count += 1
                
            except Empty:
                break
        
        return count
    
    def size(self) -> int:
        """Get queue size"""
        return self._queue.qsize()
    
    def clear(self):
        """Clear the queue"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Empty:
                break
