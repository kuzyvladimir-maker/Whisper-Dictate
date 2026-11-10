import Cocoa

// Command-line entry points (no menu bar):
//   --bench [--model M] [--mode normal|email|code] [--cold] file...   latency + text per file
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
            default: files.append(a)
            }
            i += 1
        }
        switch command {
        case "--selftest": return selftest()
        case "--list-models": return listModels()
        case "--bench": return bench(files: files, models: models, mode: mode, cold: cold)
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

    private static func bench(files: [String], models: [String], mode: DictationMode, cold: Bool) -> Int32 {
        guard let c = client() else { return 2 }
        let chain = models.isEmpty ? Settings.shared.models : models
        let system = Prompts.system(mode: mode, dictionary: UserDictionary.load())
        print("arch=\(machineArch()) keys=\(c.keyCount) models=\(chain.joined(separator: ",")) mode=\(mode.rawValue) warm=\(!cold)")
        var totals: [Double] = []
        for path in files {
            let url = URL(fileURLWithPath: path)
            let samples: [Int16]
            do { samples = try Audio.load(url) } catch {
                print("\(url.lastPathComponent): не читается (\(error.localizedDescription))")
                continue
            }
            let seconds = Double(samples.count) / Recorder.sampleRate
            if !cold {
                c.warmUp()
                Thread.sleep(forTimeInterval: 1.0)
            }
            // Timed from "key release": WAV encoding + request + cleanup, as in the app.
            let t0 = Date()
            let wav = Audio.wav(samples)
            let done = DispatchSemaphore(value: 0)
            var line = ""
            var text = ""
            c.transcribe(wav: wav, audioSeconds: seconds, system: system, user: Prompts.user, models: chain) { r in
                let dt = Date().timeIntervalSince(t0)
                switch r {
                case .success(let res):
                    text = Prompts.clean(res.text, mode: mode)
                    totals.append(dt)
                    line = String(format: "%@\taudio %.1fs\twav %dKB\t→ %.2fs\t%@ key#%d tries=%d",
                                  url.lastPathComponent, seconds, wav.count / 1024, dt, res.model,
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

    private static func selftest() -> Int32 {
        var ok = true
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            print("\(cond ? "ok  " : "FAIL") \(name)\(detail.isEmpty ? "" : " — \(detail)")")
            if !cond { ok = false }
        }
        print("arch=\(machineArch()) macOS=\(ProcessInfo.processInfo.operatingSystemVersionString)")

        let tone = (0..<16000).map { Int16(8000 * sin(Double($0) * 2 * .pi * 440 / 16000)) }
        let wav = Audio.wav(tone)
        check("wav header", wav.count == 44 + 32000 && wav.prefix(4) == Data("RIFF".utf8))
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("wdl-selftest-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: tmp) }
        if (try? wav.write(to: tmp)) != nil, let back = try? Audio.load(tmp) {
            check("wav round trip", abs(back.count - tone.count) < 64, "\(back.count) samples")
        } else {
            check("wav round trip", false)
        }

        let dict = UserDictionary.parse("# c\nSalutem\nVeeqo = вику, вико\n\nStill Frozen # brand\n")
        check("dictionary parse", dict.count == 3 && dict[1].soundsLike == ["вику", "вико"] && dict[2].term == "Still Frozen")
        let sys = Prompts.system(mode: .code, dictionary: dict)
        check("prompt has dictionary", sys.contains("Veeqo (может звучать как: вику, вико)"))
        check("clean quotes", Prompts.clean("«Привет»", mode: .normal) == "Привет")
        check("clean fence", Prompts.clean("```\nhello\n```", mode: .normal) == "hello")
        check("clean silence", Prompts.clean("(тишина)", mode: .normal) == "")

        let keys = Env.loadKeys()
        check("gemini keys", !keys.isEmpty, keys.isEmpty ? "нет (\(Paths.envFile.path))" : "найдено: \(keys.count)")
        let local = LocalWhisper().locate()
        print("info whisper.cpp fallback: \(local.map { "\($0.cli) + \(($0.model as NSString).lastPathComponent)" } ?? "не установлен")")
        print("info accessibility granted: \(AXIsProcessTrusted())")
        return ok ? 0 : 1
    }
}
