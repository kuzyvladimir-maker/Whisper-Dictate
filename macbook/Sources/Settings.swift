import Foundation

// Settings, paths, API keys and the custom dictionary.
// Everything user-editable lives in ~/.config/whisper-dictate/ so it survives reinstalls
// and is never inside the repo.

enum DictationMode: String, CaseIterable {
    case normal
    case email
    case code

    var title: String {
        switch self {
        case .normal: return "Обычный"
        case .email: return "Письмо"
        case .code: return "Код / промпт для ИИ"
        }
    }
}

enum HotkeyChoice: String, CaseIterable {
    case rightOption = "right_option"
    case fn = "fn"
    case rightCommand = "right_command"

    var title: String {
        switch self {
        case .rightOption: return "Правый ⌥ Option"
        case .fn: return "Fn / 🌐"
        case .rightCommand: return "Правый ⌘ Command"
        }
    }

    // Virtual key codes (Carbon kVK_*).
    var keyCode: Int64 {
        switch self {
        case .rightOption: return 61
        case .fn: return 63
        case .rightCommand: return 54
        }
    }
}

enum Paths {
    static let home: URL = {
        if let h = ProcessInfo.processInfo.environment["HOME"], !h.isEmpty { return URL(fileURLWithPath: h, isDirectory: true) }
        return FileManager.default.homeDirectoryForCurrentUser
    }()
    static let configDir = home.appendingPathComponent(".config/whisper-dictate", isDirectory: true)
    static let envFile = configDir.appendingPathComponent("env")
    static let settingsFile = configDir.appendingPathComponent("lite.json")
    static let dictionaryFile = configDir.appendingPathComponent("dictionary.txt")
    static let localDir = home.appendingPathComponent(".local/share/whisper-dictate", isDirectory: true)
    static let logFile = home.appendingPathComponent("Library/Logs/WhisperDictateLite.log")
}

final class Settings {
    static let shared = Settings()

    var mode: DictationMode = .normal
    var hotkey: HotkeyChoice = .fn
    var localOnly = false
    var localFallback = true
    var sounds = true
    var appendSpace = true
    var paste = true          // false: only copy to the clipboard
    var restoreClipboard = true
    var restoreDelay = 0.8
    var holdThreshold = 0.35
    var maxRecordingSeconds = 600.0
    // Free tier (Nov 2026): 3.6-flash 5/min, 20/day per project; 3.5-flash-lite 15/min, 500/day.
    // So flash-lite carries everyday short phrases; flash is spent on long dictations and on the
    // "письмо" / "код" modes where the extra quality matters.
    static let defaultModel = "gemini-3.6-flash"
    static let defaultFastModel = "gemini-3.5-flash-lite"
    var model = Settings.defaultModel           // long audio, email and code modes
    var fastModel = Settings.defaultFastModel   // short phrases in normal mode
    var longAudioSeconds = 30.0
    var fallbackModels: [String] = []
    var language = "ru"
    var audioFormat = "aac"   // "aac" (smaller upload) or "wav"
    var whisperCli = ""
    var whisperModel = ""

    private init() { load() }

    /// Request order for one dictation. A 429 moves to the next key, then down this list;
    /// a 404 ("no longer available") skips the model. Defaults go last so a stale config
    /// still ends up on a working model.
    func models(mode: DictationMode, seconds: Double) -> [String] {
        let wantsQuality = mode != .normal || seconds >= longAudioSeconds
        var chain = wantsQuality ? [model, fastModel] : [fastModel, model]
        chain += fallbackModels
        chain += wantsQuality ? [Settings.defaultModel, Settings.defaultFastModel]
                              : [Settings.defaultFastModel, Settings.defaultModel]
        var out: [String] = []
        for m in chain where !m.isEmpty && !Settings.isRetired(m) && !out.contains(m) { out.append(m) }
        return out
    }

    /// Gemini 2.x answers 404 "no longer available to new users" on new keys.
    static func isRetired(_ model: String) -> Bool {
        model.hasPrefix("gemini-2.") || model.hasPrefix("gemini-1.")
    }

    func load() {
        guard let data = try? Data(contentsOf: Paths.settingsFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let v = obj["mode"] as? String, let m = DictationMode(rawValue: v) { mode = m }
        if let v = obj["hotkey"] as? String, let h = HotkeyChoice(rawValue: v) { hotkey = h }
        if let v = obj["local_only"] as? Bool { localOnly = v }
        if let v = obj["local_fallback"] as? Bool { localFallback = v }
        if let v = obj["sounds"] as? Bool { sounds = v }
        if let v = obj["append_space"] as? Bool { appendSpace = v }
        if let v = obj["paste"] as? Bool { paste = v }
        if let v = obj["restore_clipboard"] as? Bool { restoreClipboard = v }
        if let v = obj["restore_delay_seconds"] as? Double { restoreDelay = v }
        if let v = obj["hold_threshold_seconds"] as? Double { holdThreshold = v }
        if let v = obj["max_recording_seconds"] as? Double { maxRecordingSeconds = v }
        if let v = obj["model"] as? String, !v.isEmpty, !Settings.isRetired(v) { model = v }
        if let v = obj["fast_model"] as? String, !v.isEmpty, !Settings.isRetired(v) { fastModel = v }
        if let v = obj["long_audio_seconds"] as? Double { longAudioSeconds = v }
        if let v = obj["fallback_models"] as? [String] { fallbackModels = v.filter { !Settings.isRetired($0) } }
        if let v = obj["language"] as? String, !v.isEmpty { language = v }
        if let v = obj["audio_format"] as? String, v == "aac" || v == "wav" { audioFormat = v }
        if let v = obj["whisper_cli"] as? String { whisperCli = v }
        if let v = obj["whisper_model"] as? String { whisperModel = v }
    }

    func save() {
        let obj: [String: Any] = [
            "mode": mode.rawValue,
            "hotkey": hotkey.rawValue,
            "local_only": localOnly,
            "local_fallback": localFallback,
            "sounds": sounds,
            "append_space": appendSpace,
            "paste": paste,
            "restore_clipboard": restoreClipboard,
            "restore_delay_seconds": restoreDelay,
            "hold_threshold_seconds": holdThreshold,
            "max_recording_seconds": maxRecordingSeconds,
            "model": model,
            "fast_model": fastModel,
            "long_audio_seconds": longAudioSeconds,
            "fallback_models": fallbackModels,
            "language": language,
            "audio_format": audioFormat,
            "whisper_cli": whisperCli,
            "whisper_model": whisperModel,
        ]
        do {
            try FileManager.default.createDirectory(at: Paths.configDir, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: Paths.settingsFile, options: .atomic)
        } catch {
            Log.write("settings save failed: \(error.localizedDescription)")
        }
    }
}

enum Env {
    /// Gemini keys from the process environment (GEMINI_API_KEY / GEMINI_API_KEYS) or from
    /// ~/.config/whisper-dictate/env. Several keys may be separated by commas or spaces.
    static func loadKeys() -> [String] {
        var raw: [String] = []
        let env = ProcessInfo.processInfo.environment
        for name in ["GEMINI_API_KEY", "GEMINI_API_KEYS"] {
            if let v = env[name], !v.isEmpty { raw.append(v) }
        }
        if raw.isEmpty, let text = try? String(contentsOf: Paths.envFile, encoding: .utf8) {
            for line in text.components(separatedBy: .newlines) {
                var s = line.trimmingCharacters(in: .whitespaces)
                if s.isEmpty || s.hasPrefix("#") { continue }
                if s.hasPrefix("export ") { s = String(s.dropFirst(7)) }
                guard let eq = s.firstIndex(of: "=") else { continue }
                let name = s[..<eq].trimmingCharacters(in: .whitespaces)
                guard name == "GEMINI_API_KEY" || name == "GEMINI_API_KEYS" else { continue }
                var value = s[s.index(after: eq)...].trimmingCharacters(in: .whitespaces)
                value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
                raw.append(value)
            }
        }
        var keys: [String] = []
        for chunk in raw {
            for k in chunk.components(separatedBy: CharacterSet(charactersIn: ", ;\t")) {
                let t = k.trimmingCharacters(in: .whitespaces)
                if !t.isEmpty && !keys.contains(t) { keys.append(t) }
            }
        }
        return keys
    }
}

struct DictEntry {
    let term: String
    let soundsLike: [String]
}

enum UserDictionary {
    /// Lines: `Term` or `Term = how it may sound, another variant`. `#` starts a comment.
    static func load() -> [DictEntry] {
        guard let text = try? String(contentsOf: Paths.dictionaryFile, encoding: .utf8) else { return [] }
        return parse(text)
    }

    static func parse(_ text: String) -> [DictEntry] {
        var out: [DictEntry] = []
        for line in text.components(separatedBy: .newlines) {
            var s = line
            if let hash = s.firstIndex(of: "#") { s = String(s[..<hash]) }
            s = s.trimmingCharacters(in: .whitespaces)
            if s.isEmpty { continue }
            if let eq = s.firstIndex(of: "=") {
                let term = s[..<eq].trimmingCharacters(in: .whitespaces)
                let variants = s[s.index(after: eq)...]
                    .components(separatedBy: ",")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                if !term.isEmpty { out.append(DictEntry(term: term, soundsLike: variants)) }
            } else {
                out.append(DictEntry(term: s, soundsLike: []))
            }
        }
        return out
    }
}

enum Log {
    private static let queue = DispatchQueue(label: "log")

    /// Timings and errors only. Dictated text and audio are never logged.
    static func write(_ message: String) {
        let line = "\(stamp()) \(message)\n"
        queue.async {
            let url = Paths.logFile
            let fm = FileManager.default
            try? fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let attrs = try? fm.attributesOfItem(atPath: url.path),
               let size = attrs[.size] as? NSNumber, size.intValue > 1_000_000 {
                let old = url.deletingPathExtension().appendingPathExtension("old.log")
                try? fm.removeItem(at: old)
                try? fm.moveItem(at: url, to: old)
            }
            if !fm.fileExists(atPath: url.path) {
                fm.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile()
                h.write(line.data(using: .utf8) ?? Data())
                h.closeFile()
            }
        }
    }

    private static func stamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        return f.string(from: Date())
    }
}
