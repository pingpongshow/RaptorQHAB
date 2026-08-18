//
//  RawSerialPort.swift
//  RaptorHabGS
//
//  A plain raw-mode serial port with a per-connection baud rate.
//
//  The existing SerialPortManager hardcodes 921600 as a static and bakes in
//  the ground modem's framing. The payload gadget and a Meshtastic node need
//  different rates and entirely different framing, so they get a transport
//  that does nothing but move bytes.
//

import Foundation

enum SerialPortError: LocalizedError {
    case openFailed(String, Int32)
    case configurationFailed(String)
    case notOpen

    var errorDescription: String? {
        switch self {
        case .openFailed(let path, let code):
            let reason = String(cString: strerror(code))
            return "Could not open \(path): \(reason)"
        case .configurationFailed(let detail):
            return "Could not configure the port: \(detail)"
        case .notOpen:
            return "The port is not open"
        }
    }
}

/// Raw byte transport over a serial device.
///
/// Reads happen on a dedicated thread and arrive via `onData`. Writes are
/// serialised and block until drained, because a dropped write on a request
/// protocol leaves the caller waiting forever.
final class RawSerialPort {
    private(set) var path: String?
    private var fileDescriptor: Int32 = -1
    private var readThread: Thread?
    private let writeLock = NSLock()
    private var shouldStop = false

    /// Called on the read thread. Handlers must not block.
    var onData: ((Data) -> Void)?

    /// Called when the far end disappears -- the usual case is unplugging.
    var onDisconnect: ((String?) -> Void)?

    var isOpen: Bool { fileDescriptor >= 0 }

    // MARK: - Lifecycle

    func open(path: String, baudRate: speed_t) throws {
        close()

        // O_NONBLOCK on open, so a port with no carrier does not hang here.
        let descriptor = Darwin.open(path, O_RDWR | O_NOCTTY | O_NONBLOCK)
        guard descriptor >= 0 else {
            throw SerialPortError.openFailed(path, errno)
        }

        do {
            try configure(descriptor: descriptor, baudRate: baudRate)
        } catch {
            Darwin.close(descriptor)
            throw error
        }

        fileDescriptor = descriptor
        self.path = path
        shouldStop = false

        let thread = Thread { [weak self] in self?.readLoop() }
        thread.name = "RawSerialPort \(path)"
        thread.qualityOfService = .userInitiated
        thread.start()
        readThread = thread
    }

    private func configure(descriptor: Int32, baudRate: speed_t) throws {
        // Exclusive access, so two managers cannot fight over one port.
        if ioctl(descriptor, TIOCEXCL) == -1 {
            throw SerialPortError.configurationFailed("port is already in use")
        }

        var options = termios()
        guard tcgetattr(descriptor, &options) == 0 else {
            throw SerialPortError.configurationFailed("tcgetattr failed")
        }

        cfmakeraw(&options)

        // VMIN 0 / VTIME 0: a read returns immediately with whatever is there.
        // Blocking behaviour comes from select(), not from the line discipline.
        options.c_cc.16 = 0  // VMIN
        options.c_cc.17 = 0  // VTIME

        options.c_cflag |= tcflag_t(CREAD | CLOCAL)
        options.c_cflag &= ~tcflag_t(CRTSCTS)   // no hardware flow control
        options.c_cflag &= ~tcflag_t(PARENB)    // 8N1
        options.c_cflag &= ~tcflag_t(CSTOPB)
        options.c_cflag &= ~tcflag_t(CSIZE)
        options.c_cflag |= tcflag_t(CS8)

        cfsetispeed(&options, baudRate)
        cfsetospeed(&options, baudRate)

        guard tcsetattr(descriptor, TCSANOW, &options) == 0 else {
            throw SerialPortError.configurationFailed("tcsetattr failed")
        }

        // Non-standard rates need IOSSIOSPEED, which must come after
        // tcsetattr or it is overwritten.
        var speed = baudRate
        _ = ioctl(descriptor, 0x80085402, &speed)  // IOSSIOSPEED

        tcflush(descriptor, TCIOFLUSH)
    }

    func close() {
        shouldStop = true

        let descriptor = fileDescriptor
        fileDescriptor = -1

        if descriptor >= 0 {
            Darwin.close(descriptor)
        }

        // The read thread notices the closed descriptor and exits on its own;
        // joining here would deadlock if close() is called from onData.
        readThread = nil
        path = nil
    }

    // MARK: - Transfer

    /// Write everything, waiting for the buffer to drain.
    ///
    /// Returns false if the port closed or the write could not complete
    /// within the timeout. Dropping bytes silently would hang a caller that
    /// is waiting on a reply.
    @discardableResult
    func write(_ data: Data, timeout: TimeInterval = 5.0) -> Bool {
        writeLock.lock()
        defer { writeLock.unlock() }

        guard fileDescriptor >= 0 else { return false }
        let descriptor = fileDescriptor

        var written = 0
        let deadline = Date().addingTimeInterval(timeout)

        return data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return false }

            while written < data.count {
                if Date() > deadline { return false }

                let result = Darwin.write(descriptor, base + written, data.count - written)
                if result > 0 {
                    written += result
                    continue
                }

                if result == -1 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                    var writeSet = fd_set()
                    fdZero(&writeSet)
                    fdSet(descriptor, &writeSet)
                    var wait = timeval(tv_sec: 0, tv_usec: 100_000)
                    _ = select(descriptor + 1, nil, &writeSet, nil, &wait)
                    continue
                }

                return false
            }
            return true
        }
    }

    private func readLoop() {
        var buffer = [UInt8](repeating: 0, count: 8192)

        while !shouldStop {
            let descriptor = fileDescriptor
            guard descriptor >= 0 else { break }

            var readSet = fd_set()
            fdZero(&readSet)
            fdSet(descriptor, &readSet)
            var wait = timeval(tv_sec: 0, tv_usec: 200_000)

            let ready = select(descriptor + 1, &readSet, nil, nil, &wait)
            if ready < 0 {
                if errno == EINTR { continue }
                break
            }
            if ready == 0 { continue }

            let count = Darwin.read(descriptor, &buffer, buffer.count)

            if count > 0 {
                onData?(Data(buffer[0..<count]))
            } else if count == 0 {
                break  // the far end went away
            } else if errno != EAGAIN && errno != EINTR {
                break
            }
        }

        if !shouldStop {
            let reason = "the device was disconnected"
            DispatchQueue.main.async { [weak self] in
                self?.onDisconnect?(reason)
            }
        }
    }

    // MARK: - fd_set helpers
    //
    // fd_set is a fixed-size C struct of tuples in Swift, with no bit
    // manipulation exposed, so the macros have to be reimplemented.

    private func fdZero(_ set: inout fd_set) {
        withUnsafeMutableBytes(of: &set.fds_bits) { raw in
            _ = raw.initializeMemory(as: Int32.self, repeating: 0)
        }
    }

    private func fdSet(_ descriptor: Int32, _ set: inout fd_set) {
        let intOffset = Int(descriptor) / 32
        let bitOffset = Int(descriptor) % 32
        withUnsafeMutableBytes(of: &set.fds_bits) { raw in
            let words = raw.bindMemory(to: Int32.self)
            guard intOffset < words.count else { return }
            words[intOffset] |= Int32(1 << bitOffset)
        }
    }

    deinit {
        close()
    }
}
