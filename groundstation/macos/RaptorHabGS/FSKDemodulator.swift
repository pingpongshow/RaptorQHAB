//
//  FSKDemodulator.swift
//  RaptorHabGS
//
//  FSK Demodulation for RaptorHab telemetry
//  Implements quadrature demodulation, clock recovery, and bit synchronization
//

import Foundation
import Accelerate

// MARK: - FSK Demodulator

class FSKDemodulator {
    
    // MARK: - Debug Logging
    
    /// Off by default: this runs per sample batch on the RTL-SDR path.
    static var debugEnabled = false
    private var sampleBatchCount = 0
    private var totalSamplesProcessed = 0
    private var totalBitsRecovered = 0
    private var lastDebugTime = Date()
    private var signalMin: Double = 0
    private var signalMax: Double = 0
    private var signalSum: Double = 0
    private var signalCount = 0
    
    private func debugLog(_ message: @autoclosure () -> String) {
        #if DEBUG
        if FSKDemodulator.debugEnabled { print("[FSK] \(message())") }
        #endif
    }
    
    private func periodicStats() {
        let now = Date()
        if now.timeIntervalSince(lastDebugTime) >= 2.0 {
            let avgSignal = signalCount > 0 ? signalSum / Double(signalCount) : 0
            debugLog("=== Periodic Stats ===")
            debugLog("  Samples processed: \(totalSamplesProcessed)")
            debugLog("  Bits recovered: \(totalBitsRecovered)")
            debugLog("  Bit buffer size: \(bitBuffer.count)")
            debugLog("  Signal range: [\(String(format: "%.4f", signalMin)), \(String(format: "%.4f", signalMax))], avg: \(String(format: "%.4f", avgSignal))")
            debugLog("  Sync words found: \(syncWordsFound)")
            debugLog("  Packets detected: \(packetsDetected)")
            debugLog("  Packet in progress: \(packetInProgress)")
            if packetInProgress {
                debugLog("  Packet bits collected: \(packetBits.count)/\(expectedPacketBits)")
            }
            lastDebugTime = now
            signalMin = 0
            signalMax = 0
            signalSum = 0
            signalCount = 0
        }
    }
    
    // MARK: - Configuration
    
    struct Config {
        var sampleRate: Int = 1000000      // RTL-SDR sample rate
        var bitRate: Int = 96000           // Symbol/bit rate
        var frequencyDev: Int = 50000      // FSK deviation
        var syncWord: [UInt8] = RaptorProtocol.syncWord
        var decimationFactor: Int = 4      // Reduce sample rate for processing
        
        var samplesPerBit: Double {
            return Double(sampleRate / decimationFactor) / Double(bitRate)
        }
        
        var effectiveSampleRate: Int {
            return sampleRate / decimationFactor
        }
    }
    
    // MARK: - Properties
    
    private let config: Config
    private var iqBuffer: [Complex] = []
    private var demodBuffer: [Double] = []
    private var bitBuffer: [UInt8] = []
    private var byteBuffer: [UInt8] = []
    
    // Clock recovery state
    private var clockPhase: Double = 0
    private var clockFreq: Double = 0
    private var lastSample: Double = 0
    
    // Low-pass filter state
    private var lpfState: [Double] = []
    private var lpfCoeffs: [Double] = []
    
    // Packet detection
    private var syncWordBits: [UInt8] = []
    private var packetInProgress = false
    private var packetBits: [UInt8] = []
    private var expectedPacketBits = 0
    
    // Statistics
    var packetsDetected = 0
    var syncWordsFound = 0
    var bitErrors = 0
    
    // Callback for detected packets
    var onPacketDetected: ((Data) -> Void)?
    
    // MARK: - Initialization
    
    init(config: Config = Config()) {
        self.config = config
        self.clockFreq = 1.0 / config.samplesPerBit
        
        // Convert sync word bytes to bits
        for byte in config.syncWord {
            for i in (0..<8).reversed() {
                syncWordBits.append((byte >> i) & 1)
            }
        }
        
        // Initialize low-pass filter
        initLowPassFilter()
        
        debugLog("Initialized with config:")
        debugLog("  Sample rate: \(config.sampleRate) Hz")
        debugLog("  Effective sample rate: \(config.effectiveSampleRate) Hz (after \(config.decimationFactor)x decimation)")
        debugLog("  Bit rate: \(config.bitRate) bps")
        debugLog("  Samples per bit: \(String(format: "%.2f", config.samplesPerBit))")
        debugLog("  Freq deviation: \(config.frequencyDev) Hz")
        debugLog("  Sync word: \(config.syncWord.map { String(format: "%02X", $0) }.joined(separator: " "))")
        debugLog("  Sync word bits: \(syncWordBits.map { String($0) }.joined())")
    }
    
    // MARK: - Low-Pass Filter
    
    private func initLowPassFilter() {
        // Design a simple FIR low-pass filter
        // Cutoff at ~2x bit rate
        let numTaps = 31
        let cutoff = Double(config.bitRate * 2) / Double(config.effectiveSampleRate)
        
        lpfCoeffs = [Double](repeating: 0, count: numTaps)
        lpfState = [Double](repeating: 0, count: numTaps)
        
        // Windowed sinc filter
        let center = numTaps / 2
        for i in 0..<numTaps {
            if i == center {
                lpfCoeffs[i] = 2.0 * cutoff
            } else {
                let n = Double(i - center)
                lpfCoeffs[i] = sin(2.0 * .pi * cutoff * n) / (.pi * n)
            }
            
            // Hamming window
            let window = 0.54 - 0.46 * cos(2.0 * .pi * Double(i) / Double(numTaps - 1))
            lpfCoeffs[i] *= window
        }
        
        // Normalize
        let sum = lpfCoeffs.reduce(0, +)
        for i in 0..<numTaps {
            lpfCoeffs[i] /= sum
        }
    }
    
    private func applyLowPassFilter(_ sample: Double) -> Double {
        // Shift state
        for i in stride(from: lpfState.count - 1, to: 0, by: -1) {
            lpfState[i] = lpfState[i - 1]
        }
        lpfState[0] = sample
        
        // Convolve
        var output: Double = 0
        for i in 0..<lpfCoeffs.count {
            output += lpfState[i] * lpfCoeffs[i]
        }
        
        return output
    }
    
    // MARK: - Sample Processing
    
    /// Process raw IQ samples from RTL-SDR
    /// - Parameter samples: Interleaved I/Q bytes (unsigned 8-bit, 0-255)
    func processSamples(_ samples: [UInt8]) {
        sampleBatchCount += 1
        totalSamplesProcessed += samples.count / 2
        
        if sampleBatchCount == 1 || sampleBatchCount % 100 == 0 {
            debugLog("Processing batch #\(sampleBatchCount): \(samples.count) bytes (\(samples.count/2) IQ samples)")
        }
        
        // Convert to complex samples and decimate
        var decimatedIQ: [Complex] = []
        
        for i in stride(from: 0, to: samples.count - 1, by: 2 * config.decimationFactor) {
            // Convert unsigned to signed float (-1 to 1)
            let iSample = (Double(samples[i]) - 127.5) / 127.5
            let qSample = (Double(samples[i + 1]) - 127.5) / 127.5
            decimatedIQ.append(Complex(real: iSample, imag: qSample))
        }
        
        // FM demodulation (frequency discrimination)
        let demodulated = fmDemodulate(decimatedIQ)
        
        // Track signal levels
        for sample in demodulated {
            signalMin = min(signalMin, sample)
            signalMax = max(signalMax, sample)
            signalSum += abs(sample)
            signalCount += 1
        }
        
        // Clock recovery and bit decisions
        let bits = recoverBits(demodulated)
        totalBitsRecovered += bits.count
        
        if sampleBatchCount == 1 || sampleBatchCount % 100 == 0 {
            debugLog("  Decimated to \(decimatedIQ.count) IQ -> \(demodulated.count) demod samples -> \(bits.count) bits")
        }
        
        // Detect packets
        detectPackets(bits)
        
        // Periodic stats
        periodicStats()
    }
    
    // MARK: - FM Demodulation
    
    private func fmDemodulate(_ iq: [Complex]) -> [Double] {
        guard iq.count > 1 else { return [] }
        
        var demodulated: [Double] = []
        
        // Quadrature demodulation: phase difference between consecutive samples
        for i in 1..<iq.count {
            let prev = iq[i - 1]
            let curr = iq[i]
            
            // Conjugate multiply: curr * conj(prev)
            let conjMult = Complex(
                real: curr.real * prev.real + curr.imag * prev.imag,
                imag: curr.imag * prev.real - curr.real * prev.imag
            )
            
            // Phase difference (atan2)
            let phase = atan2(conjMult.imag, conjMult.real)
            
            // Low-pass filter
            let filtered = applyLowPassFilter(phase)
            demodulated.append(filtered)
        }
        
        return demodulated
    }
    
    // MARK: - Clock Recovery (Mueller-Muller)
    
    private func recoverBits(_ samples: [Double]) -> [UInt8] {
        var bits: [UInt8] = []
        
        let samplesPerBit = config.samplesPerBit
        var sampleIndex: Double = 0
        
        while sampleIndex < Double(samples.count - 1) {
            let idx = Int(sampleIndex)
            guard idx >= 0 && idx < samples.count else { break }
            
            // Interpolate sample at optimal timing point
            let frac = sampleIndex - Double(idx)
            let sample: Double
            if idx + 1 < samples.count {
                sample = samples[idx] * (1.0 - frac) + samples[idx + 1] * frac
            } else {
                sample = samples[idx]
            }
            
            // Bit decision (threshold at 0)
            let bit: UInt8 = sample > 0 ? 1 : 0
            bits.append(bit)
            
            // Mueller-Muller timing error detector
            if bits.count >= 2 {
                let prevBit = bits[bits.count - 2]
                let currBit = bits[bits.count - 1]
                
                // Timing error estimate
                let timingError = Double(currBit == prevBit ? 0 : 1) * (sample - lastSample)
                
                // Loop filter (PI controller)
                let kp: Double = 0.01  // Proportional gain
                let ki: Double = 0.001 // Integral gain
                
                clockPhase += kp * timingError
                clockFreq += ki * timingError
                
                // Clamp frequency adjustment
                let nominalFreq = 1.0 / samplesPerBit
                clockFreq = max(nominalFreq * 0.95, min(nominalFreq * 1.05, clockFreq))
            }
            
            lastSample = sample
            sampleIndex += samplesPerBit + clockPhase
            clockPhase = 0  // Reset phase correction after applying
        }
        
        return bits
    }
    
    // MARK: - Packet Detection
    
    private func detectPackets(_ newBits: [UInt8]) {
        bitBuffer.append(contentsOf: newBits)
        
        // Limit buffer size
        if bitBuffer.count > 10000 {
            bitBuffer.removeFirst(bitBuffer.count - 5000)
        }
        
        while bitBuffer.count >= syncWordBits.count {
            if packetInProgress {
                // Continue collecting packet bits
                let bitsNeeded = expectedPacketBits - packetBits.count
                let bitsAvailable = min(bitsNeeded, bitBuffer.count)
                
                packetBits.append(contentsOf: bitBuffer.prefix(bitsAvailable))
                bitBuffer.removeFirst(bitsAvailable)
                
                if packetBits.count >= expectedPacketBits {
                    debugLog("Packet collection complete: \(packetBits.count) bits")
                    // Convert bits to bytes and validate
                    let packetData = bitsToBytes(packetBits)
                    validateAndDeliverPacket(packetData)
                    
                    packetInProgress = false
                    packetBits.removeAll()
                }
            } else {
                // Look for sync word
                if let syncIndex = findSyncWord(in: bitBuffer) {
                    syncWordsFound += 1
                    debugLog("Starting packet collection (sync #\(syncWordsFound))")
                    
                    // Remove bits before sync word
                    if syncIndex > 0 {
                        bitBuffer.removeFirst(syncIndex)
                    }
                    
                    // Start packet collection
                    // We'll collect max packet size worth of bits
                    packetInProgress = true
                    expectedPacketBits = RaptorProtocol.maxPacketSize * 8
                    packetBits.removeAll()
                    
                    // Include sync word in packet
                    packetBits.append(contentsOf: bitBuffer.prefix(syncWordBits.count))
                    bitBuffer.removeFirst(syncWordBits.count)
                } else {
                    // No sync word found, remove oldest bit
                    bitBuffer.removeFirst(1)
                }
            }
        }
    }
    
    private func findSyncWord(in bits: [UInt8]) -> Int? {
        guard bits.count >= syncWordBits.count else { return nil }
        
        // Correlate sync word with bit buffer
        let maxOffset = min(bits.count - syncWordBits.count, 100)  // Only check first 100 positions for efficiency
        
        var bestMatches = 0
        var bestOffset = -1
        
        for offset in 0...maxOffset {
            var matches = 0
            for i in 0..<syncWordBits.count {
                if bits[offset + i] == syncWordBits[i] {
                    matches += 1
                }
            }
            
            if matches > bestMatches {
                bestMatches = matches
                bestOffset = offset
            }
            
            // Allow up to 2 bit errors in sync word detection
            if matches >= syncWordBits.count - 2 {
                debugLog("SYNC FOUND at offset \(offset): \(matches)/\(syncWordBits.count) bits match")
                if matches < syncWordBits.count {
                    // Show which bits didn't match
                    var errors: [Int] = []
                    for i in 0..<syncWordBits.count {
                        if bits[offset + i] != syncWordBits[i] {
                            errors.append(i)
                        }
                    }
                    debugLog("  Bit errors at positions: \(errors)")
                }
                return offset
            }
        }
        
        // Log best correlation if we didn't find it (once per many attempts)
        if sampleBatchCount % 500 == 0 && bestMatches > 20 {
            debugLog("Best sync correlation: \(bestMatches)/\(syncWordBits.count) at offset \(bestOffset)")
            // Show the bits at that position
            let foundBits = Array(bits[bestOffset..<min(bestOffset+syncWordBits.count, bits.count)])
            debugLog("  Found: \(foundBits.map { String($0) }.joined())")
            debugLog("  Expect: \(syncWordBits.map { String($0) }.joined())")
        }
        
        return nil
    }
    
    private func bitsToBytes(_ bits: [UInt8]) -> Data {
        var bytes = Data()
        
        for i in stride(from: 0, to: bits.count - 7, by: 8) {
            var byte: UInt8 = 0
            for j in 0..<8 {
                byte = (byte << 1) | bits[i + j]
            }
            bytes.append(byte)
        }
        
        return bytes
    }
    
    // Valid packet types from the protocol
    private static let validPacketTypes: Set<UInt8> = [
        0x00, // telemetry
        0x01, // imageMeta
        0x02, // imageData
        0x03, // textMessage
        0x10, // commandAck
        0x80, // cmdPing
        0x81, // cmdSetParam
        0x82, // cmdCapture
        0x83, // cmdReboot
    ]
    
    private func validateAndDeliverPacket(_ data: Data) {
        debugLog("Validating packet: \(data.count) bytes")
        debugLog("  Hex: \(data.prefix(25).map { String(format: "%02X", $0) }.joined(separator: " "))\(data.count > 25 ? "..." : "")")
        
        // Verify minimum size
        guard data.count >= RaptorProtocol.headerSize + RaptorProtocol.crcSize else {
            debugLog("  REJECTED: Too short (\(data.count) < \(RaptorProtocol.headerSize + RaptorProtocol.crcSize))")
            return
        }
        
        let syncData = data.prefix(RaptorProtocol.syncSize)
        guard syncData.elementsEqual(RaptorProtocol.syncWord) else {
            debugLog("  REJECTED: Sync word mismatch")
            return
        }
        
        // Check for SX1262 variable-length packet format:
        // [RAPT][LEN][RAPT][TYPE][SEQ_HI][SEQ_LO][FLAGS][PAYLOAD][CRC32]
        // The radio sends RF sync "RAPT" + length byte, then payload starts with protocol sync "RAPT"
        
        let lengthByte = data[RaptorProtocol.syncSize]
        
        // Check if this looks like: RAPT + length + RAPT (SX1262 variable-length format)
        let sx1262HeaderSize = RaptorProtocol.syncSize + 1 + RaptorProtocol.syncSize  // RAPT + len + RAPT = 9 bytes
        if data.count >= sx1262HeaderSize {
            let secondSyncStart = RaptorProtocol.syncSize + 1  // After first RAPT and length byte
            let potentialSecondSync = data.subdata(in: secondSyncStart..<(secondSyncStart + RaptorProtocol.syncSize))
            
            if potentialSecondSync.elementsEqual(RaptorProtocol.syncWord) {
                // This is SX1262 format
                let payloadLength = Int(lengthByte)
                debugLog("  SX1262 variable-length format detected:")
                debugLog("    RF sync: RAPT, Length byte: \(payloadLength)")
                debugLog("    Protocol sync at offset \(secondSyncStart)")
                
                // Strategy: Try exact length first, then nearby lengths, then full scan
                // The length byte itself might have bit errors
                
                let fullPacketData = Data(data.suffix(from: secondSyncStart))
                
                // Try exact length from length byte
                if payloadLength <= fullPacketData.count && payloadLength >= RaptorProtocol.headerSize + RaptorProtocol.crcSize {
                    let exactData = Data(fullPacketData.prefix(payloadLength))
                    debugLog("    Trying exact length: \(payloadLength) bytes")
                    if let result = tryValidatePacket(exactData) {
                        onPacketDetected?(result)
                        return
                    }
                }
                
                // Try lengths around the length byte value (±2 for bit errors)
                for offset in [1, -1, 2, -2] {
                    let tryLength = payloadLength + offset
                    if tryLength > 0 && tryLength <= fullPacketData.count && tryLength >= RaptorProtocol.headerSize + RaptorProtocol.crcSize {
                        let tryData = Data(fullPacketData.prefix(tryLength))
                        if let result = tryValidatePacket(tryData) {
                            debugLog("    *** CRC VALID at length \(tryLength) (length byte was \(payloadLength))")
                            onPacketDetected?(result)
                            return
                        }
                    }
                }
                
                // Fall back to full scan of all lengths
                debugLog("    Exact length failed, trying full length scan...")
                if let result = tryValidatePacketAllLengths(fullPacketData) {
                    onPacketDetected?(result)
                    return
                }
                
                // All failed
                debugLog("  CRC FAILED for SX1262 packet (length byte: \(payloadLength))")
                showCRCDiagnostics(fullPacketData, expectedLength: payloadLength)
                return
            }
        }
        
        // Not SX1262 format - try direct protocol packet validation
        debugLog("  Direct protocol packet (no SX1262 wrapper)")
        if let result = tryValidatePacketAllLengths(data) {
            onPacketDetected?(result)
            return
        }
        
        debugLog("  CRC FAILED for direct packet")
        showCRCDiagnostics(data, expectedLength: nil)
    }
    
    private func tryValidatePacket(_ data: Data) -> Data? {
        guard data.count >= RaptorProtocol.headerSize + RaptorProtocol.crcSize else {
            return nil
        }
        
        // Verify protocol sync
        let syncData = data.prefix(RaptorProtocol.syncSize)
        guard syncData.elementsEqual(RaptorProtocol.syncWord) else {
            return nil
        }
        
        if CRC32.verify(packet: data) {
            packetsDetected += 1
            let packetType = data[RaptorProtocol.syncSize]
            let seqHi = data[RaptorProtocol.syncSize + 1]
            let seqLo = data[RaptorProtocol.syncSize + 2]
            let seqNum = (UInt16(seqHi) << 8) | UInt16(seqLo)
            debugLog("  *** CRC VALID: \(data.count) bytes, type=0x\(String(format: "%02X", packetType)), seq=\(seqNum)")
            debugLog("  Packet delivered successfully!")
            return data
        }
        return nil
    }
    
    private func tryValidatePacketAllLengths(_ data: Data) -> Data? {
        guard data.count >= RaptorProtocol.headerSize + RaptorProtocol.crcSize else {
            return nil
        }
        
        // Verify protocol sync
        let syncData = data.prefix(RaptorProtocol.syncSize)
        guard syncData.elementsEqual(RaptorProtocol.syncWord) else {
            return nil
        }
        
        let packetType = data[RaptorProtocol.syncSize]
        let seqHi = data[RaptorProtocol.syncSize + 1]
        let seqLo = data[RaptorProtocol.syncSize + 2]
        let seqNum = (UInt16(seqHi) << 8) | UInt16(seqLo)
        debugLog("  Scanning all lengths for type=0x\(String(format: "%02X", packetType)), seq=\(seqNum)")
        
        // Try known packet sizes first (most likely)
        let knownSizes = [
            RaptorProtocol.headerSize + RaptorProtocol.telemetryPayloadSize + RaptorProtocol.crcSize,  // 48 bytes telemetry
            RaptorProtocol.headerSize + 4 + RaptorProtocol.crcSize,  // 16 bytes minimal
        ]
        
        for size in knownSizes {
            if size <= data.count {
                let candidate = Data(data.prefix(size))
                if CRC32.verify(packet: candidate) {
                    packetsDetected += 1
                    debugLog("  *** CRC VALID at known size \(size)")
                    debugLog("  Packet delivered successfully!")
                    return candidate
                }
            }
        }
        
        // Full scan from longest to shortest
        var attempts = 0
        for length in stride(from: data.count, through: RaptorProtocol.headerSize + RaptorProtocol.crcSize, by: -1) {
            let candidate = Data(data.prefix(length))
            attempts += 1
            
            if CRC32.verify(packet: candidate) {
                packetsDetected += 1
                debugLog("  *** CRC VALID at length \(length) (after \(attempts) attempts)")
                debugLog("  Packet delivered successfully!")
                return candidate
            }
        }
        
        debugLog("  No valid CRC found after \(attempts) length attempts")
        return nil
    }
    
    private func showCRCDiagnostics(_ data: Data, expectedLength: Int?) {
        if let expected = expectedLength {
            debugLog("  Expected length from header: \(expected)")
        }
        if data.count >= 4 {
            let rcvdCrc = data.suffix(4).map { String(format: "%02X", $0) }.joined()
            debugLog("  Last 4 bytes: \(rcvdCrc)")
            let withoutCrc = data.prefix(data.count - 4)
            let computed = CRC32.calculate(data: Data(withoutCrc))
            debugLog("  Computed CRC for \(withoutCrc.count) bytes: \(String(format: "%08X", computed))")
        }
    }
    
    // MARK: - Reset
    
    func reset() {
        iqBuffer.removeAll()
        demodBuffer.removeAll()
        bitBuffer.removeAll()
        byteBuffer.removeAll()
        packetInProgress = false
        packetBits.removeAll()
        clockPhase = 0
        clockFreq = 1.0 / config.samplesPerBit
        lpfState = [Double](repeating: 0, count: lpfCoeffs.count)
    }
    
    func getStats() -> [String: Any] {
        return [
            "packetsDetected": packetsDetected,
            "syncWordsFound": syncWordsFound,
            "bitBufferSize": bitBuffer.count,
            "packetInProgress": packetInProgress
        ]
    }
}

// MARK: - Complex Number Type

struct Complex {
    var real: Double
    var imag: Double
    
    static func + (lhs: Complex, rhs: Complex) -> Complex {
        return Complex(real: lhs.real + rhs.real, imag: lhs.imag + rhs.imag)
    }
    
    static func * (lhs: Complex, rhs: Complex) -> Complex {
        return Complex(
            real: lhs.real * rhs.real - lhs.imag * rhs.imag,
            imag: lhs.real * rhs.imag + lhs.imag * rhs.real
        )
    }
    
    var magnitude: Double {
        return sqrt(real * real + imag * imag)
    }
    
    var phase: Double {
        return atan2(imag, real)
    }
    
    func conjugate() -> Complex {
        return Complex(real: real, imag: -imag)
    }
}

// MARK: - DSP Helpers

extension FSKDemodulator {
    
    /// Apply a moving average filter
    static func movingAverage(_ samples: [Double], windowSize: Int) -> [Double] {
        guard samples.count >= windowSize else { return samples }
        
        var result = [Double]()
        result.reserveCapacity(samples.count)
        
        var sum: Double = 0
        
        // Initial window
        for i in 0..<windowSize {
            sum += samples[i]
        }
        result.append(sum / Double(windowSize))
        
        // Sliding window
        for i in windowSize..<samples.count {
            sum += samples[i] - samples[i - windowSize]
            result.append(sum / Double(windowSize))
        }
        
        return result
    }
    
    /// Compute the power spectrum using FFT
    static func powerSpectrum(_ samples: [Double]) -> [Double] {
        let n = samples.count
        guard n > 0 && (n & (n - 1)) == 0 else {
            // Not power of 2, pad
            let nextPow2 = 1 << Int(ceil(log2(Double(n))))
            var padded = samples
            padded.append(contentsOf: [Double](repeating: 0, count: nextPow2 - n))
            return powerSpectrum(padded)
        }
        
        // Simple DFT (for small sizes, use Accelerate for larger)
        var spectrum = [Double](repeating: 0, count: n / 2)
        
        for k in 0..<n/2 {
            var realSum: Double = 0
            var imagSum: Double = 0
            
            for i in 0..<n {
                let angle = -2.0 * .pi * Double(k * i) / Double(n)
                realSum += samples[i] * cos(angle)
                imagSum += samples[i] * sin(angle)
            }
            
            spectrum[k] = sqrt(realSum * realSum + imagSum * imagSum) / Double(n)
        }
        
        return spectrum
    }
}
