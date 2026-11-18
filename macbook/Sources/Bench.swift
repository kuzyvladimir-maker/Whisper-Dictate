import AVFoundation
import Cocoa

// Command-line entry points (no menu bar):
//   --bench [--model M] [--mode normal|email|code] [--format aac|wav] [--cold] file...
//   --bench --local file...     same through whisper.cpp
//   --list-models                                                     models available to the key
//   --selftest                                                        offline checks
// The bench runs the same GeminiClient the app uses. Without --cold it warms the connection
// first, exactly like the app does when recording starts.

enum CLI {
    static func run(_ args: [String]) -> Int32 {
        var files: [String] = []
        var models: [String] = []
        var mode = DictationMode.normal
        var cold = false
        var useLocal = false
        var format = Settings.shared.audioFormat
        var opts = BenchOptions()
        var command = ""
        var i = 0
        while i < args.count {
            let a = args[i]
            switch a {
            case "--bench", "--list-models", "--selftest", "--mic-test", "--aac-check": command = a
            case "--model":
                i += 1
                if i < args.count { models.append(args[i]) }
            case "--mode":
                i += 1
                if i < args.count, let m = DictationMode(rawValue: args[i]) { mode = m }
            case "--cold": cold = true
            case "--local": useLocal = true
            case "--format":
                i += 1
                if i < args.count { format = args[i] }
            case "--no-hedge": opts.hedge = false
            case "--no-dict": opts.dictionary = false
            case "--quiet": opts.quiet = true
            case "--thinking":
                i += 1
                if i < args.count { opts.thinking = args[i] }
            case "--repeat":
                i += 1
                if i < args.count, let n = Int(args[i]) { opts.repeats = max(1, n) }
            case "--pause":
                i += 1
                if i < args.count, let p = Double(args[i]) { opts.pause = max(0, p) }
            default: files.append(a)
            }
            i += 1
        }
        switch command {
        case "--selftest": return selftest()
        case "--mic-test": return micTest()
        case "--aac-check": return aacCheck(files)
        case "--list-models": return listModels()
        case "--bench" where useLocal: return benchLocal(files: files)
        case "--bench": return bench(files: files, models: models, mode: mode, cold: cold, format: format, opts: opts)
        default:
            print("usage: WhisperDictateLite --bench [--model M] [--mode normal|email|code] [--cold] file...")
            print("       WhisperDictateLite --list-models | --selftest")
            return 2
        }
    }

    private static func client() -> GeminiClient? {
        let keys = Env.loadKeys()
        guard !keys.isEmpty else {
            print("Нет ключа: GEMINI_API_KEY в окружении или в \(Paths.envFile.path)")
            return nil
        }
        let c = GeminiClient()
        c.setKeys(keys)
        return c
    }

    private static func listModels() -> Int32 {
        guard let c = client() else { return 2 }
        let done = DispatchSemaphore(value: 0)
        c.listModels { names in
            names.filter { $0.contains("flash") || $0.contains("gemini-3") }.forEach { print($0) }
            done.signal()
        }
        done.wait()
        return 0
    }

    struct BenchOptions {
        var hedge = true
        var dictionary = true
        var thinking: String?
        var repeats = 1
        var pause = 1.0
        var quiet = false
    }

    private static func bench(files: [String], models: [String], mode: DictationMode, cold: Bool,
                              format: String, opts: BenchOptions = BenchOptions()) -> Int32 {
        guard let c = client() else { return 2 }
        c.hedgeEnabled = opts.hedge
        c.thinkingOverride = opts.thinking
        c.timing.onTiming = { line in print("   · " + line) }
        UserDictionary.shared.refresh()
        let dict = opts.dictionary ? UserDictionary.shared.entries(for: nil) : []
        let system = Prompts.system(mode: mode, dictionary: dict)
        print("hedge=\(opts.hedge) dictionary=\(dict.count) terms thinking=\(opts.thinking ?? "default") repeats=\(opts.repeats)")
        var failures = 0
        let route = models.isEmpty ? "auto (\(Settings.shared.fastModel) во всех режимах; \(Settings.shared.model) запасная)" : models.joined(separator: ",")
        print("arch=\(machineArch()) keys=\(c.keyCount) models=\(route) mode=\(mode.rawValue) format=\(format) warm=\(!cold)")
        var totals: [Double] = []
        for path in files.flatMap({ Array(repeating: $0, count: opts.repeats) }) {
            let url = URL(fileURLWithPath: path)
            let samples: [Int16]
            do { samples = try Audio.load(url) } catch {
                print("\(url.lastPathComponent): не читается (\(error.localizedDescription))")
                continue
            }
            let seconds = Double(samples.count) / Recorder.sampleRate
            let chain = models.isEmpty ? Settings.shared.models(mode: mode, seconds: seconds) : models
            if !cold {
                c.warmUp()
                Thread.sleep(forTimeInterval: 1.0)
            }
            // Timed from "key release": encoding + request + cleanup, as in the app.
            let t0 = Date()
            let upload = Audio.encodeForUpload(samples, format: format)
            let done = DispatchSemaphore(value: 0)
            var line = ""
            var text = ""
            c.transcribe(audio: upload.data, mimeType: upload.mime, audioSeconds: seconds, system: system,
                         user: Prompts.user, models: chain) { r in
                let dt = Date().timeIntervalSince(t0)
                switch r {
                case .success(let res):
                    text = Prompts.clean(res.text, mode: mode)
                    totals.append(dt)
                    line = String(format: "%@\taudio %.1fs\t%@ %dKB\t→ %.2fs\t%@ key#%d tries=%d",
                                  url.lastPathComponent, seconds, upload.mime, upload.data.count / 1024, dt, res.model,
                                  res.keyIndex + 1, res.attempts)
                case .failure(let e):
                    failures += 1
                    line = String(format: "%@\taudio %.1fs\tFAILED after %.2fs: %@", url.lastPathComponent,
                                  seconds, dt, e.localizedDescription)
                }
                done.signal()
            }
            done.wait()
            Thread.sleep(forTimeInterval: 0.2)   // let the timing delegate report
            print(line)
            if !text.isEmpty && !opts.quiet { print("   " + text.replacingOccurrences(of: "\n", with: "\n   ")) }
            if opts.pause > 0 { Thread.sleep(forTimeInterval: opts.pause) }
        }
        if !totals.isEmpty {
            let sorted = totals.sorted()
            let p90 = sorted[min(sorted.count - 1, Int(Double(sorted.count) * 0.9))]
            print(String(format: "release→text: ok %d, failed %d; median %.2fs, p90 %.2fs, min %.2fs, max %.2fs",
                         sorted.count, failures, sorted[sorted.count / 2], p90, sorted.first!, sorted.last!))
        } else {
            print("release→text: ok 0, failed \(failures)")
        }
        return 0
    }

    private static func benchLocal(files: [String]) -> Int32 {
        let local = LocalWhisper()
        guard let found = local.locate() else {
            print(LocalWhisperError.notInstalled.localizedDescription)
            return 2
        }
        print("arch=\(machineArch()) whisper.cpp=\(found.cli) model=\((found.model as NSString).lastPathComponent)")
        UserDictionary.shared.refresh()
        let hint = Prompts.whisperHint(dictionary: UserDictionary.shared.entries(for: nil))
        for path in files {
            let url = URL(fileURLWithPath: path)
            guard let samples = try? Audio.load(url) else { continue }
            let t0 = Date()
            let done = DispatchSemaphore(value: 0)
            var line = ""
            local.transcribe(samples: samples, language: Settings.shared.language, hint: hint) { r in
                switch r {
                case .success(let text):
                    line = String(format: "%@\taudio %.1fs\t→ %.2fs\n   %@", url.lastPathComponent,
                                  Double(samples.count) / Recorder.sampleRate, Date().timeIntervalSince(t0), text)
                case .failure(let e):
                    line = "\(url.lastPathComponent)\tFAILED: \(e.localizedDescription)"
                }
                done.signal()
            }
            done.wait()
            print(line)
        }
        return 0
    }

    /// Does AAC encoding keep the END of the audio? Encodes in memory, decodes back and
    /// compares the energy of the last 50 ms windows (numbers only, no audio leaves).
    private static func aacCheck(_ files: [String]) -> Int32 {
        func tailRMS(_ s: [Int16], windows: Int) -> [String] {
            let win = 800
            return (1...windows).reversed().map { k -> String in
                let end = s.count - (k - 1) * win, start = max(0, end - win)
                guard end > start else { return "-" }
                var acc = 0.0
                for v in s[start..<end] { acc += Double(v) * Double(v) }
                return String(format: "%.3f", (acc / Double(end - start)).squareRoot() / 32768)
            }
        }
        for path in files {
            guard let pcm = try? Audio.load(URL(fileURLWithPath: path)), let aac = Audio.aac(pcm) else { continue }
            let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("wdl-aac-\(UUID().uuidString).aac")
            defer { try? FileManager.default.removeItem(at: tmp) }
            guard (try? aac.write(to: tmp)) != nil, let back = try? Audio.load(tmp) else { continue }
            print(String(format: "%@: pcm %d samples, aac-decoded %d (+%d)", (path as NSString).lastPathComponent,
                         pcm.count, back.count, back.count - pcm.count))
            print("   pcm  last 6x50ms: " + tailRMS(pcm, windows: 6).joined(separator: " "))
            // Decoded stream = encoder priming (2112) + audio + padding: align on the priming offset.
            let aligned = Array(back.dropFirst(2112).prefix(pcm.count))
            print("   aac  last 6x50ms: " + tailRMS(aligned, windows: 6).joined(separator: " ")
                  + String(format: "  (aligned %d of %d)", aligned.count, pcm.count))
        }
        return 0
    }

    /// Two short captures in a row: the second must reuse the open engine (no "mic opened"
    /// in between). No hotkeys involved.
    private static func micTest() -> Int32 {
        let r = Recorder()
        r.idleCloseSeconds = 2
        for i in 1...2 {
            do { try r.start() } catch {
                print("start failed: \(error.localizedDescription)")
                return 1
            }
            Thread.sleep(forTimeInterval: 1.5)
            let (samples, peak) = r.stop()
            print(String(format: "capture %d: %.2fs, peak %.3f, live level %.3f, audio lag at stop %.0f ms", i,
                         Double(samples.count) / Recorder.sampleRate, peak, r.currentLevel, r.lastDrain.lagMs))
            Thread.sleep(forTimeInterval: 0.3)
        }
        Thread.sleep(forTimeInterval: 2.5)   // idle close fires
        print("done (see ~/Library/Logs/WhisperDictateLite.log for mic opened/closed)")
        return 0
    }

    private static func selftest() -> Int32 {
        var ok = true
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            print("\(cond ? "ok  " : "FAIL") \(name)\(detail.isEmpty ? "" : " — \(detail)")")
            if !cond { ok = false }
        }
        print("arch=\(machineArch()) macOS=\(ProcessInfo.processInfo.operatingSystemVersionString)")

        var tone = [Int16](repeating: 0, count: 16000)
        for n in 0..<tone.count {
            let phase: Double = Double(n) * 2.0 * Double.pi * 440.0 / 16000.0
            tone[n] = Int16(8000.0 * Foundation.sin(phase))
        }
        let wav = Audio.wav(tone)
        check("wav header", wav.count == 44 + 32000 && wav.prefix(4) == Data("RIFF".utf8))
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("wdl-selftest-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: tmp) }
        if (try? wav.write(to: tmp)) != nil, let back = try? Audio.load(tmp) {
            check("wav round trip", abs(back.count - tone.count) < 64, "\(back.count) samples")
        } else {
            check("wav round trip", false)
        }

        if let aac = Audio.aac(tone) {
            let aacFile = FileManager.default.temporaryDirectory.appendingPathComponent("wdl-selftest-\(UUID().uuidString).aac")
            defer { try? FileManager.default.removeItem(at: aacFile) }
            let decoded = (try? aac.write(to: aacFile)).flatMap { _ in try? Audio.load(aacFile) } ?? []
            check("aac encode/decode", decoded.count > 15000 && decoded.count < 20000,
                  "\(aac.count) bytes vs wav \(wav.count), decoded \(decoded.count) samples")
        } else {
            check("aac encode", false)
        }

        let dict = UserDictionary.parse("# c\nSalutem\nвику | вико = Veeqo\n\nстил фрозен = Still Frozen\n= Michael\n")
        check("dictionary parse (iMac format)", dict.count == 4 && dict[1].variants == ["вику", "вико"]
              && dict[1].term == "Veeqo" && dict[2].term == "Still Frozen" && dict[3].term == "Michael")
        let sys = Prompts.system(mode: .code, dictionary: dict)
        check("prompt has dictionary", sys.contains("Veeqo (может прозвучать как: вику, вико)"))
        check("dictionary replacements", UserDictionary.apply("Проверь вику и стил   фрозен, но не викунья", entries: dict)
              == "Проверь Veeqo и Still Frozen, но не викунья")
        check("legacy Lite format converted", UserDictionary.convertLegacy("# Whisper Dictate Lite\nVeeqo = вику, вико\nSalutem")
              == "# Whisper Dictate Lite\nвику | вико = Veeqo\nSalutem")
        check("clean quotes", Prompts.clean("«Привет»", mode: .normal) == "Привет")
        check("clean fence", Prompts.clean("```\nhello\n```", mode: .normal) == "hello")
        check("clean silence", Prompts.clean("(тишина)", mode: .normal) == "")
        let short = Settings.shared.models(mode: .normal, seconds: 5)
        let long = Settings.shared.models(mode: .normal, seconds: 60)
        let email = Settings.shared.models(mode: .email, seconds: 5)
        let longEmail = Settings.shared.models(mode: .email, seconds: 60)
        // Длинное аудио (в любом режиме) ведёт flash-lite — 3.6-flash на длинном висит;
        // короткое письмо/код — качественной моделью. Тяжёлая модель остаётся в хвосте запасной.
        check("model routing", short.first == Settings.shared.fastModel && long.first == Settings.shared.fastModel
              && email.first == Settings.shared.fastModel && longEmail.first == Settings.shared.fastModel
              && long.contains(Settings.shared.model) && !short.contains(where: Settings.isRetired),
              "короткая → \(short.joined(separator: " → ")); длинная → \(long.joined(separator: " → ")); письмо → \(email.joined(separator: " → "))")
        check("model unavailable detection",
              GeminiClient.modelUnavailable("models/gemini-2.5-flash is no longer available to new users"))
        check("whisper hallucination filter", LocalWhisper.isHallucination("Продолжение следует...")
              && !LocalWhisper.isHallucination("Спасибо за просмотр, давай теперь обсудим цену и сроки поставки для Walmart"))

        // RecordingStore: keeps the newest N on disk, latest() returns the newest.
        let recDir = FileManager.default.temporaryDirectory.appendingPathComponent("wdl-rec-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: recDir) }
        let clip = [Int16](repeating: 100, count: 1600)
        for i in 1...5 { _ = RecordingStore.save(clip, keep: 3, in: recDir, name: String(format: "2026-%02d", i)) }
        let kept = ((try? FileManager.default.contentsOfDirectory(at: recDir, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "wav" }
        check("recordings prune keeps N", kept.count == 3, "осталось \(kept.count)")
        check("recordings latest is newest", RecordingStore.latest(in: recDir)?.lastPathComponent == "rec-2026-05.wav",
              RecordingStore.latest(in: recDir)?.lastPathComponent ?? "nil")

        if let latest = RecordingStore.latest(in: recDir) {
            let attrs = try? FileManager.default.attributesOfItem(atPath: latest.path)
            check("recording mode 0600", (attrs?[.posixPermissions] as? NSNumber)?.intValue == 0o600)
            let restored = try? Audio.load(latest)
            check("recording reload after restart", restored == clip)
        }
        let attrs = try? FileManager.default.attributesOfItem(atPath: recDir.path)
        check("recording directory mode 0700", (attrs?[.posixPermissions] as? NSNumber)?.intValue == 0o700)
        _ = RecordingStore.save(clip, keep: 0, in: recDir)
        check("recording retention disabled", !RecordingStore.hasAny(in: recDir))
        for (name, passed) in SelftestURLProtocol.run() { check(name, passed) }

        let keys = Env.loadKeys()
        check("gemini keys", !keys.isEmpty, keys.isEmpty ? "нет (\(Paths.envFile.path))" : "найдено: \(keys.count)")
        let local = LocalWhisper().locate()
        print("info whisper.cpp fallback: \(local.map { "\($0.cli) + \(($0.model as NSString).lastPathComponent)" } ?? "не установлен")")
        print("info accessibility granted: \(AXIsProcessTrusted())")
        let mic: String
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: mic = "разрешён"
        case .denied: mic = "запрещён"
        case .restricted: mic = "ограничен"
        default: mic = "ещё не спрашивали"
        }
        print("info microphone: \(mic)")
        return ok ? 0 : 1
    }
}
