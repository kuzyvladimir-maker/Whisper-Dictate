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
        var command = ""
        var i = 0
        while i < args.count {
            let a = args[i]
            switch a {
            case "--bench", "--list-models", "--selftest": command = a
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
            default: files.append(a)
            }
            i += 1
        }
        switch command {
        case "--selftest": return selftest()
        case "--list-models": return listModels()
        case "--bench" where useLocal: return benchLocal(files: files)
        case "--bench": return bench(files: files, models: models, mode: mode, cold: cold, format: format)
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

    private static func bench(files: [String], models: [String], mode: DictationMode, cold: Bool,
                              format: String) -> Int32 {
        guard let c = client() else { return 2 }
        let system = Prompts.system(mode: mode, dictionary: UserDictionary.load())
        let route = models.isEmpty ? "auto (\(Settings.shared.fastModel) < \(Int(Settings.shared.longAudioSeconds))s ≤ \(Settings.shared.model))" : models.joined(separator: ",")
        print("arch=\(machineArch()) keys=\(c.keyCount) models=\(route) mode=\(mode.rawValue) format=\(format) warm=\(!cold)")
        var totals: [Double] = []
        for path in files {
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
                    line = String(format: "%@\taudio %.1fs\tFAILED after %.2fs: %@", url.lastPathComponent,
                                  seconds, dt, e.localizedDescription)
                }
                done.signal()
            }
            done.wait()
            print(line)
            if !text.isEmpty { print("   " + text.replacingOccurrences(of: "\n", with: "\n   ")) }
        }
        if !totals.isEmpty {
            let sorted = totals.sorted()
            print(String(format: "release→text: median %.2fs, min %.2fs, max %.2fs (n=%d)",
                         sorted[sorted.count / 2], sorted.first!, sorted.last!, sorted.count))
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
        let hint = Prompts.whisperHint(dictionary: UserDictionary.load())
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

        let dict = UserDictionary.parse("# c\nSalutem\nVeeqo = вику, вико\n\nStill Frozen # brand\n")
        check("dictionary parse", dict.count == 3 && dict[1].soundsLike == ["вику", "вико"] && dict[2].term == "Still Frozen")
        let sys = Prompts.system(mode: .code, dictionary: dict)
        check("prompt has dictionary", sys.contains("Veeqo (может звучать как: вику, вико)"))
        check("clean quotes", Prompts.clean("«Привет»", mode: .normal) == "Привет")
        check("clean fence", Prompts.clean("```\nhello\n```", mode: .normal) == "hello")
        check("clean silence", Prompts.clean("(тишина)", mode: .normal) == "")
        let short = Settings.shared.models(mode: .normal, seconds: 5)
        let long = Settings.shared.models(mode: .normal, seconds: 60)
        let email = Settings.shared.models(mode: .email, seconds: 5)
        check("model routing", short.first == Settings.shared.fastModel && long.first == Settings.shared.model
              && email.first == Settings.shared.model && !short.contains(where: Settings.isRetired),
              "короткая → \(short.joined(separator: " → ")); длинная/письмо → \(long.joined(separator: " → "))")
        check("model unavailable detection",
              GeminiClient.modelUnavailable("models/gemini-2.5-flash is no longer available to new users"))
        check("whisper hallucination filter", LocalWhisper.isHallucination("Продолжение следует...")
              && !LocalWhisper.isHallucination("Спасибо за просмотр, давай теперь обсудим цену и сроки поставки для Walmart"))

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
