import AVFoundation

// Microphone capture straight into memory as 16 kHz mono Int16 — the format both Gemini
// and whisper.cpp want. Nothing is written to disk.

enum RecorderError: Error, LocalizedError {
    case noInput
    case converter

    var errorDescription: String? {
        switch self {
        case .noInput: return "Микрофон не найден или нет доступа"
        case .converter: return "Не удалось настроить конвертер аудио"
        }
    }
}

final class Recorder {
    static let sampleRate = 16000.0
    static let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate,
                                            channels: 1, interleaved: true)!

    // One engine stays running between dictations and is closed only after a pause
    // (lesson from the iMac version: opening/closing CoreAudio on every dictation is where
    // hangs and crashes happen; Lite once crashed inside AudioUnitInitialize on a restart).
    // Frames are kept only while capturing; the level meter runs whenever the mic is open.
    private var engine: AVAudioEngine?
    private var converter: AVAudioConverter?
    private var samples: [Int16] = []
    private var peak: Int16 = 0
    private var capturing = false
    private var idleClose: DispatchWorkItem?
    private var configObserver: NSObjectProtocol?
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "recorder-engine")

    /// Seconds the mic stays open after a dictation (0 = close right away).
    var idleCloseSeconds = 30.0
    /// Smoothed live peak (0...1) and the time of the last audio callback, for the
    /// "тебя не слышно" warning.
    private var level: Float = 0
    private var callbackAt = Date()
    var currentLevel: Float { lock.lock(); defer { lock.unlock() }; return level }
    var lastCallbackAt: Date { lock.lock(); defer { lock.unlock() }; return callbackAt }

    func start() throws {
        try queue.sync {
            idleClose?.cancel()
            idleClose = nil
            lock.lock()
            samples = []
            samples.reserveCapacity(Int(Recorder.sampleRate) * 30)
            peak = 0
            lock.unlock()
            if engine == nil || engine?.isRunning == false {
                try openEngine()
            }
            lock.lock()
            capturing = true
            callbackAt = Date()
            lock.unlock()
        }
    }

    /// Returns the captured samples and the peak level (0...1). Safe to call when idle.
    func stop() -> (samples: [Int16], peak: Float) {
        return queue.sync {
            lock.lock()
            capturing = false
            let out = samples
            let p = Float(peak) / Float(Int16.max)
            samples = []
            peak = 0
            lock.unlock()
            scheduleIdleClose()
            return (out, p)
        }
    }

    /// Close the mic now (app quit, settings change).
    func shutdown() {
        queue.sync { closeEngine() }
    }

    private func openEngine() throws {
        closeEngine()
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.channelCount > 0, inFormat.sampleRate > 0 else { throw RecorderError.noInput }
        guard let conv = AVAudioConverter(from: inFormat, to: Recorder.targetFormat) else {
            throw RecorderError.converter
        }
        conv.downmix = true
        lock.lock()
        converter = conv
        lock.unlock()
        input.installTap(onBus: 0, bufferSize: 2048, format: inFormat) { [weak self] buffer, _ in
            self?.append(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
        self.engine = engine
        // Input device changed (AirPods, display mic): rebuild with the new format.
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            guard let self = self else { return }
            self.queue.async {
                Log.write("audio device changed, reopening mic")
                let wasCapturing: Bool = { self.lock.lock(); defer { self.lock.unlock() }; return self.capturing }()
                if wasCapturing {
                    try? self.openEngine()
                } else {
                    self.closeEngine()
                }
            }
        }
        Log.write("mic opened: \(Int(inFormat.sampleRate)) Hz, \(inFormat.channelCount) ch")
    }

    private func closeEngine() {
        if let obs = configObserver {
            NotificationCenter.default.removeObserver(obs)
            configObserver = nil
        }
        guard let engine = engine else { return }
        self.engine = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock()
        converter = nil
        level = 0
        lock.unlock()
        Log.write("mic closed")
    }

    private func scheduleIdleClose() {
        idleClose?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            let busy: Bool = { self.lock.lock(); defer { self.lock.unlock() }; return self.capturing }()
            if !busy { self.closeEngine() }
        }
        idleClose = work
        queue.asyncAfter(deadline: .now() + max(0, idleCloseSeconds), execute: work)
    }

    private func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        callbackAt = Date()
        guard let conv = converter, let converted = Audio.convert(buffer, with: conv, endOfStream: false) else {
            return
        }
        var bufPeak: Int16 = 0
        for s in converted {
            let a = s == Int16.min ? Int16.max : abs(s)
            if a > bufPeak { bufPeak = a }
        }
        level = max(Float(bufPeak) / Float(Int16.max), level * 0.9)
        guard capturing else { return }
        samples.append(contentsOf: converted)
        if bufPeak > peak { peak = bufPeak }
    }
}

enum Audio {
    static func convert(_ buffer: AVAudioPCMBuffer, with conv: AVAudioConverter, endOfStream: Bool) -> [Int16]? {
        let ratio = Recorder.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
        guard let out = AVAudioPCMBuffer(pcmFormat: Recorder.targetFormat, frameCapacity: capacity) else { return nil }
        var supplied = false
        var error: NSError?
        let status = conv.convert(to: out, error: &error) { _, inputStatus in
            if supplied {
                inputStatus.pointee = endOfStream ? .endOfStream : .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, let data = out.int16ChannelData else { return nil }
        return Array(UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
    }

    /// Reads any audio file macOS can decode and returns 16 kHz mono Int16 (used by --bench).
    static func load(_ url: URL) throws -> [Int16] {
        let file = try AVAudioFile(forReading: url)
        let format = file.processingFormat
        guard let conv = AVAudioConverter(from: format, to: Recorder.targetFormat) else { throw RecorderError.converter }
        conv.downmix = true
        var out: [Int16] = []
        let chunk: AVAudioFrameCount = 16384
        while file.framePosition < file.length {
            guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunk) else { break }
            try file.read(into: buf, frameCount: chunk)
            if buf.frameLength == 0 { break }
            let last = file.framePosition >= file.length
            if let part = convert(buf, with: conv, endOfStream: last) { out.append(contentsOf: part) }
        }
        return out
    }

    static func peak(_ samples: [Int16]) -> Float {
        var p: Int16 = 0
        for s in samples {
            let a = s == Int16.min ? Int16.max : abs(s)
            if a > p { p = a }
        }
        return Float(p) / Float(Int16.max)
    }

    static func wav(_ samples: [Int16], sampleRate: Int = 16000) -> Data {
        var d = Data(capacity: 44 + samples.count * 2)
        func u32(_ v: UInt32) { var x = v.littleEndian; withUnsafeBytes(of: &x) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { var x = v.littleEndian; withUnsafeBytes(of: &x) { d.append(contentsOf: $0) } }
        let dataBytes = UInt32(samples.count * 2)
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + dataBytes)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1)
        u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        samples.withUnsafeBufferPointer { p in
            p.baseAddress.map { base in
                base.withMemoryRebound(to: UInt8.self, capacity: samples.count * 2) {
                    d.append($0, count: samples.count * 2)
                }
            }
        }
        return d
    }

    /// Upload encoding for Gemini: AAC when requested and the encoder works, WAV otherwise.
    static func encodeForUpload(_ samples: [Int16], format: String) -> (data: Data, mime: String) {
        if format == "aac", let d = aac(samples) { return (d, "audio/aac") }
        return (wav(samples), "audio/wav")
    }

    /// AAC-LC in ADTS framing ("audio/aac"), encoded in memory. About 8x smaller than WAV,
    /// which matters for upload time on long dictations. Returns nil if the encoder refuses.
    static func aac(_ samples: [Int16], bitRate: Int = 32000) -> Data? {
        guard !samples.isEmpty else { return nil }
        var desc = AudioStreamBasicDescription(mSampleRate: Recorder.sampleRate, mFormatID: kAudioFormatMPEG4AAC,
                                               mFormatFlags: 0, mBytesPerPacket: 0, mFramesPerPacket: 1024,
                                               mBytesPerFrame: 0, mChannelsPerFrame: 1, mBitsPerChannel: 0,
                                               mReserved: 0)
        guard let outFormat = AVAudioFormat(streamDescription: &desc),
              let conv = AVAudioConverter(from: Recorder.targetFormat, to: outFormat),
              let input = AVAudioPCMBuffer(pcmFormat: Recorder.targetFormat,
                                           frameCapacity: AVAudioFrameCount(samples.count)),
              let dst = input.int16ChannelData else { return nil }
        if let rates = conv.applicableEncodeBitRates?.map({ $0.intValue }), !rates.isEmpty {
            conv.bitRate = rates.min(by: { abs($0 - bitRate) < abs($1 - bitRate) })!
        }
        input.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { src in
            _ = memcpy(dst[0], src.baseAddress!, samples.count * 2)
        }
        let maxPacket = max(conv.maximumOutputPacketSize, 2048)
        var out = Data(capacity: samples.count / 4)
        var fed = false
        while true {
            let buf = AVAudioCompressedBuffer(format: outFormat, packetCapacity: 64, maximumPacketSize: maxPacket)
            var error: NSError?
            let status = conv.convert(to: buf, error: &error) { _, inputStatus in
                if fed {
                    inputStatus.pointee = .endOfStream
                    return nil
                }
                fed = true
                inputStatus.pointee = .haveData
                return input
            }
            if status == .error { return nil }
            if let descs = buf.packetDescriptions {
                for i in 0..<Int(buf.packetCount) {
                    let size = Int(descs[i].mDataByteSize)
                    out.append(contentsOf: adtsHeader(payload: size))
                    out.append(buf.data.advanced(by: Int(descs[i].mStartOffset)).assumingMemoryBound(to: UInt8.self),
                               count: size)
                }
            }
            if status == .endOfStream || buf.packetCount == 0 { break }
        }
        return out.isEmpty ? nil : out
    }

    private static func adtsHeader(payload: Int) -> [UInt8] {
        let length = payload + 7
        let profile: UInt8 = 1        // AAC LC (object type 2) - 1
        let freqIndex: UInt8 = 8      // 16000 Hz
        let channels: UInt8 = 1
        return [
            0xFF, 0xF1,
            (profile << 6) | (freqIndex << 2) | (channels >> 2),
            ((channels & 3) << 6) | UInt8((length >> 11) & 0x03),
            UInt8((length >> 3) & 0xFF),
            UInt8((length & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    }
}
