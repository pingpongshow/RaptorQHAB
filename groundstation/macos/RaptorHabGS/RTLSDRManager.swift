//
//  RTLSDRManager.swift
//  RaptorHabGS
//
//  RTL-SDR Interface using librtlsdr
//  Provides async sample streaming for FSK demodulation
//

import Foundation
import Combine

// MARK: - RTL-SDR C Interface

// These would normally be imported from a bridging header with librtlsdr
// For now, we define the interface here and use dynamic loading

typealias rtlsdr_dev_t = OpaquePointer

// RTL-SDR function signatures
typealias rtlsdr_get_device_count_fn = @convention(c) () -> UInt32
typealias rtlsdr_get_device_name_fn = @convention(c) (UInt32) -> UnsafePointer<CChar>?
typealias rtlsdr_open_fn = @convention(c) (UnsafeMutablePointer<rtlsdr_dev_t?>, UInt32) -> Int32
typealias rtlsdr_close_fn = @convention(c) (rtlsdr_dev_t) -> Int32
typealias rtlsdr_set_sample_rate_fn = @convention(c) (rtlsdr_dev_t, UInt32) -> Int32
typealias rtlsdr_get_sample_rate_fn = @convention(c) (rtlsdr_dev_t) -> UInt32
typealias rtlsdr_set_center_freq_fn = @convention(c) (rtlsdr_dev_t, UInt32) -> Int32
typealias rtlsdr_get_center_freq_fn = @convention(c) (rtlsdr_dev_t) -> UInt32
typealias rtlsdr_set_tuner_gain_mode_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32
typealias rtlsdr_set_tuner_gain_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32
typealias rtlsdr_get_tuner_gain_fn = @convention(c) (rtlsdr_dev_t) -> Int32
typealias rtlsdr_set_freq_correction_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32
typealias rtlsdr_reset_buffer_fn = @convention(c) (rtlsdr_dev_t) -> Int32
typealias rtlsdr_read_sync_fn = @convention(c) (rtlsdr_dev_t, UnsafeMutableRawPointer, Int32, UnsafeMutablePointer<Int32>) -> Int32
typealias rtlsdr_read_async_fn = @convention(c) (rtlsdr_dev_t, (@convention(c) (UnsafeMutablePointer<UInt8>?, UInt32, UnsafeMutableRawPointer?) -> Void)?, UnsafeMutableRawPointer?, UInt32, UInt32) -> Int32
typealias rtlsdr_cancel_async_fn = @convention(c) (rtlsdr_dev_t) -> Int32
typealias rtlsdr_set_agc_mode_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32
typealias rtlsdr_set_direct_sampling_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32
typealias rtlsdr_set_bias_tee_fn = @convention(c) (rtlsdr_dev_t, Int32) -> Int32

// MARK: - RTL-SDR Device Info

struct RTLSDRDeviceInfo: Identifiable {
    let id: UInt32
    let name: String
}

// MARK: - RTL-SDR Manager

@MainActor
class RTLSDRManager: ObservableObject {
    
    // MARK: - Published Properties
    
    @Published var isConnected = false
    @Published var isStreaming = false
    @Published var availableDevices: [RTLSDRDeviceInfo] = []
    @Published var selectedDeviceIndex: UInt32 = 0
    @Published var errorMessage: String?
    @Published var signalStrength: Double = 0.0
    @Published var currentFrequency: Double = 915.0
    @Published var currentSampleRate: Int = 1000000
    @Published var currentGain: Int = 40
    
    // MARK: - Private Properties
    
    private var device: rtlsdr_dev_t?
    private var libHandle: UnsafeMutableRawPointer?
    private var streamingTask: Task<Void, Never>?
    private var sampleBuffer = CircularBuffer<UInt8>(capacity: 1024 * 1024)  // 1MB buffer
    
    // Sample callback for async processing
    var onSamplesReceived: (([UInt8]) -> Void)?
    
    // Function pointers
    private var rtlsdr_get_device_count: rtlsdr_get_device_count_fn?
    private var rtlsdr_get_device_name: rtlsdr_get_device_name_fn?
    private var rtlsdr_open: rtlsdr_open_fn?
    private var rtlsdr_close: rtlsdr_close_fn?
    private var rtlsdr_set_sample_rate: rtlsdr_set_sample_rate_fn?
    private var rtlsdr_get_sample_rate: rtlsdr_get_sample_rate_fn?
    private var rtlsdr_set_center_freq: rtlsdr_set_center_freq_fn?
    private var rtlsdr_get_center_freq: rtlsdr_get_center_freq_fn?
    private var rtlsdr_set_tuner_gain_mode: rtlsdr_set_tuner_gain_mode_fn?
    private var rtlsdr_set_tuner_gain: rtlsdr_set_tuner_gain_fn?
    private var rtlsdr_get_tuner_gain: rtlsdr_get_tuner_gain_fn?
    private var rtlsdr_set_freq_correction: rtlsdr_set_freq_correction_fn?
    private var rtlsdr_reset_buffer: rtlsdr_reset_buffer_fn?
    private var rtlsdr_read_sync: rtlsdr_read_sync_fn?
    private var rtlsdr_cancel_async: rtlsdr_cancel_async_fn?
    private var rtlsdr_set_agc_mode: rtlsdr_set_agc_mode_fn?
    private var rtlsdr_set_bias_tee: rtlsdr_set_bias_tee_fn?
    
    // MARK: - Initialization
    
    init() {
        loadLibrary()
    }
    
    deinit {
        // Cancel async operations
        streamingTask?.cancel()
        
        // Close device if open (nonisolated access to raw pointers is safe in deinit)
        if let dev = device, let closeFn = rtlsdr_close {
            if let cancelFn = rtlsdr_cancel_async {
                _ = cancelFn(dev)
            }
            _ = closeFn(dev)
        }
        
        // Close library handle
        if let handle = libHandle {
            dlclose(handle)
        }
    }
    
    // MARK: - Library Loading
    
    private func loadLibrary() {
        // Try common library paths
        let libraryPaths = [
            "/usr/local/lib/librtlsdr.dylib",
            "/opt/homebrew/lib/librtlsdr.dylib",
            "/opt/local/lib/librtlsdr.dylib",
            "librtlsdr.dylib"
        ]
        
        for path in libraryPaths {
            if let handle = dlopen(path, RTLD_NOW) {
                libHandle = handle
                loadFunctions()
                print("Loaded librtlsdr from: \(path)")
                return
            }
        }
        
        errorMessage = "Failed to load librtlsdr. Install with: brew install librtlsdr"
        print("Error: \(errorMessage ?? "")")
    }
    
    private func loadFunctions() {
        guard let handle = libHandle else { return }
        
        rtlsdr_get_device_count = unsafeBitCast(dlsym(handle, "rtlsdr_get_device_count"), to: rtlsdr_get_device_count_fn?.self)
        rtlsdr_get_device_name = unsafeBitCast(dlsym(handle, "rtlsdr_get_device_name"), to: rtlsdr_get_device_name_fn?.self)
        rtlsdr_open = unsafeBitCast(dlsym(handle, "rtlsdr_open"), to: rtlsdr_open_fn?.self)
        rtlsdr_close = unsafeBitCast(dlsym(handle, "rtlsdr_close"), to: rtlsdr_close_fn?.self)
        rtlsdr_set_sample_rate = unsafeBitCast(dlsym(handle, "rtlsdr_set_sample_rate"), to: rtlsdr_set_sample_rate_fn?.self)
        rtlsdr_get_sample_rate = unsafeBitCast(dlsym(handle, "rtlsdr_get_sample_rate"), to: rtlsdr_get_sample_rate_fn?.self)
        rtlsdr_set_center_freq = unsafeBitCast(dlsym(handle, "rtlsdr_set_center_freq"), to: rtlsdr_set_center_freq_fn?.self)
        rtlsdr_get_center_freq = unsafeBitCast(dlsym(handle, "rtlsdr_get_center_freq"), to: rtlsdr_get_center_freq_fn?.self)
        rtlsdr_set_tuner_gain_mode = unsafeBitCast(dlsym(handle, "rtlsdr_set_tuner_gain_mode"), to: rtlsdr_set_tuner_gain_mode_fn?.self)
        rtlsdr_set_tuner_gain = unsafeBitCast(dlsym(handle, "rtlsdr_set_tuner_gain"), to: rtlsdr_set_tuner_gain_fn?.self)
        rtlsdr_get_tuner_gain = unsafeBitCast(dlsym(handle, "rtlsdr_get_tuner_gain"), to: rtlsdr_get_tuner_gain_fn?.self)
        rtlsdr_set_freq_correction = unsafeBitCast(dlsym(handle, "rtlsdr_set_freq_correction"), to: rtlsdr_set_freq_correction_fn?.self)
        rtlsdr_reset_buffer = unsafeBitCast(dlsym(handle, "rtlsdr_reset_buffer"), to: rtlsdr_reset_buffer_fn?.self)
        rtlsdr_read_sync = unsafeBitCast(dlsym(handle, "rtlsdr_read_sync"), to: rtlsdr_read_sync_fn?.self)
        rtlsdr_cancel_async = unsafeBitCast(dlsym(handle, "rtlsdr_cancel_async"), to: rtlsdr_cancel_async_fn?.self)
        rtlsdr_set_agc_mode = unsafeBitCast(dlsym(handle, "rtlsdr_set_agc_mode"), to: rtlsdr_set_agc_mode_fn?.self)
        rtlsdr_set_bias_tee = unsafeBitCast(dlsym(handle, "rtlsdr_set_bias_tee"), to: rtlsdr_set_bias_tee_fn?.self)
    }
    
    // MARK: - Device Management
    
    func scanDevices() {
        guard let getCount = rtlsdr_get_device_count,
              let getName = rtlsdr_get_device_name else {
            errorMessage = "Library not loaded"
            return
        }
        
        availableDevices.removeAll()
        
        let count = getCount()
        for i in 0..<count {
            if let namePtr = getName(i) {
                let name = String(cString: namePtr)
                availableDevices.append(RTLSDRDeviceInfo(id: i, name: name))
            } else {
                availableDevices.append(RTLSDRDeviceInfo(id: i, name: "Unknown Device \(i)"))
            }
        }
        
        if availableDevices.isEmpty {
            errorMessage = "No RTL-SDR devices found"
        } else {
            errorMessage = nil
        }
    }
    
    func connect(deviceIndex: UInt32 = 0, config: RadioConfig) -> Bool {
        guard let openFn = rtlsdr_open else {
            errorMessage = "Library not loaded"
            return false
        }
        
        // Close existing connection
        if isConnected {
            disconnect()
        }
        
        var dev: rtlsdr_dev_t?
        let result = openFn(&dev, deviceIndex)
        
        if result != 0 || dev == nil {
            errorMessage = "Failed to open RTL-SDR device (error: \(result))"
            return false
        }
        
        device = dev
        selectedDeviceIndex = deviceIndex
        
        // Configure device
        if !configure(config) {
            disconnect()
            return false
        }
        
        isConnected = true
        errorMessage = nil
        return true
    }
    
    func disconnect() {
        stopStreaming()
        
        if let dev = device, let closeFn = rtlsdr_close {
            _ = closeFn(dev)
        }
        
        device = nil
        isConnected = false
    }
    
    // MARK: - Configuration
    
    func configure(_ config: RadioConfig) -> Bool {
        guard let dev = device else { return false }
        
        // Set sample rate
        if let setSampleRate = rtlsdr_set_sample_rate {
            let result = setSampleRate(dev, UInt32(config.sampleRate))
            if result != 0 {
                errorMessage = "Failed to set sample rate"
                return false
            }
            currentSampleRate = config.sampleRate
        }
        
        // Set center frequency
        let freqHz = UInt32(config.frequencyMHz * 1_000_000)
        if let setFreq = rtlsdr_set_center_freq {
            let result = setFreq(dev, freqHz)
            if result != 0 {
                errorMessage = "Failed to set frequency"
                return false
            }
            currentFrequency = config.frequencyMHz
        }
        
        // Set gain mode (manual = 1, auto = 0)
        if let setGainMode = rtlsdr_set_tuner_gain_mode {
            let mode: Int32 = config.gain > 0 ? 1 : 0
            _ = setGainMode(dev, mode)
        }
        
        // Set gain (in tenths of dB)
        if config.gain > 0, let setGain = rtlsdr_set_tuner_gain {
            let gainTenths = Int32(config.gain * 10)
            _ = setGain(dev, gainTenths)
            currentGain = config.gain
        }
        
        // Set AGC mode
        if let setAGC = rtlsdr_set_agc_mode {
            _ = setAGC(dev, config.gain == 0 ? 1 : 0)
        }
        
        // Reset buffer
        if let resetBuffer = rtlsdr_reset_buffer {
            _ = resetBuffer(dev)
        }
        
        return true
    }
    
    func setFrequency(_ mhz: Double) {
        guard let dev = device, let setFreq = rtlsdr_set_center_freq else { return }
        let freqHz = UInt32(mhz * 1_000_000)
        if setFreq(dev, freqHz) == 0 {
            currentFrequency = mhz
        }
    }
    
    func setGain(_ gain: Int) {
        guard let dev = device else { return }
        
        if let setGainMode = rtlsdr_set_tuner_gain_mode {
            _ = setGainMode(dev, gain > 0 ? 1 : 0)
        }
        
        if gain > 0, let setGain = rtlsdr_set_tuner_gain {
            let gainTenths = Int32(gain * 10)
            if setGain(dev, gainTenths) == 0 {
                currentGain = gain
            }
        } else {
            currentGain = 0
        }
    }
    
    // MARK: - Streaming
    
    func startStreaming() {
        guard let dev = device, !isStreaming else { return }
        
        isStreaming = true
        
        // Start read loop in background
        streamingTask = Task.detached(priority: .high) { [weak self] in
            await self?.readLoop(device: dev)
        }
    }
    
    func stopStreaming() {
        guard isStreaming else { return }
        
        isStreaming = false
        streamingTask?.cancel()
        streamingTask = nil
        
        if let dev = device, let cancelAsync = rtlsdr_cancel_async {
            _ = cancelAsync(dev)
        }
    }
    
    private func readLoop(device: rtlsdr_dev_t) async {
        guard let readSync = rtlsdr_read_sync else {
            return
        }
        
        let bufferSize: Int32 = 16384  // 16KB chunks
        var buffer = [UInt8](repeating: 0, count: Int(bufferSize))
        var bytesRead: Int32 = 0
        
        var readCount = 0
        var totalBytes: UInt64 = 0
        var lastLogTime = Date()
        var errorCount = 0
        
        
        while isStreaming && !Task.isCancelled {
            let result = buffer.withUnsafeMutableBufferPointer { ptr -> Int32 in
                readSync(device, ptr.baseAddress!, bufferSize, &bytesRead)
            }
            
            if result == 0 && bytesRead > 0 {
                readCount += 1
                totalBytes += UInt64(bytesRead)
                
                let samples = Array(buffer.prefix(Int(bytesRead)))
                
                // Calculate signal strength from IQ samples
                let strength = calculateSignalStrength(samples)
                
                // Reset periodic counters
                let now = Date()
                if now.timeIntervalSince(lastLogTime) >= 2.0 {
                    lastLogTime = now
                    totalBytes = 0
                }
                
                await MainActor.run {
                    self.signalStrength = strength
                    self.onSamplesReceived?(samples)
                }
            } else if result != 0 {
                errorCount += 1
                
                if errorCount > 10 {
                    // Error or device disconnected
                    await MainActor.run {
                        self.isStreaming = false
                        self.errorMessage = "Read error: \(result)"
                    }
                    break
                }
            }
            
            // Small yield to prevent blocking
            await Task.yield()
        }
        
    }
    
    private func calculateSignalStrength(_ samples: [UInt8]) -> Double {
        // Calculate RMS power from IQ samples
        // Samples are interleaved I, Q, I, Q, ...
        var sumSquares: Double = 0
        let count = samples.count / 2
        
        for i in stride(from: 0, to: samples.count - 1, by: 2) {
            let iSample = Double(samples[i]) - 127.5
            let qSample = Double(samples[i + 1]) - 127.5
            sumSquares += iSample * iSample + qSample * qSample
        }
        
        let rms = sqrt(sumSquares / Double(count))
        
        // Convert to dB relative to full scale
        let dbfs = 20.0 * log10(rms / 127.5)
        
        // Normalize to 0-100 range for display
        return max(0, min(100, (dbfs + 40) * 2.5))
    }
}

// MARK: - Circular Buffer

class CircularBuffer<T> {
    private var buffer: [T?]
    private var readIndex = 0
    private var writeIndex = 0
    private let capacity: Int
    private let lock = NSLock()
    
    init(capacity: Int) {
        self.capacity = capacity
        self.buffer = [T?](repeating: nil, count: capacity)
    }
    
    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return (writeIndex - readIndex + capacity) % capacity
    }
    
    var isEmpty: Bool {
        return count == 0
    }
    
    func write(_ element: T) {
        lock.lock()
        defer { lock.unlock() }
        
        buffer[writeIndex] = element
        writeIndex = (writeIndex + 1) % capacity
        
        // Overwrite oldest if full
        if writeIndex == readIndex {
            readIndex = (readIndex + 1) % capacity
        }
    }
    
    func write(_ elements: [T]) {
        for element in elements {
            write(element)
        }
    }
    
    func read() -> T? {
        lock.lock()
        defer { lock.unlock() }
        
        guard readIndex != writeIndex else { return nil }
        
        let element = buffer[readIndex]
        buffer[readIndex] = nil
        readIndex = (readIndex + 1) % capacity
        return element
    }
    
    func read(_ count: Int) -> [T] {
        var result: [T] = []
        for _ in 0..<count {
            if let element = read() {
                result.append(element)
            } else {
                break
            }
        }
        return result
    }
    
    func clear() {
        lock.lock()
        defer { lock.unlock() }
        
        readIndex = 0
        writeIndex = 0
        buffer = [T?](repeating: nil, count: capacity)
    }
}
