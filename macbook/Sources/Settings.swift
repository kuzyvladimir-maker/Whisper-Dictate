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
    static let home = FileManager.default.homeDirectoryForCurrentUser
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
    var hotkey: HotkeyChoice = .rightOption
    var localOnly = false
    var localFallback = true
    var sounds = true
    var appendSpace = true
    var restoreClipboard = true
    var restoreDelay = 0.8
    var holdThreshold = 0.35
    var maxRecordingSeconds = 600.0
    var model = "gemini-2.5-flash"
    var fallbackModels = ["gemini-2.5-flash-lite"]
    var language = "ru"
    var whisperCli = ""
    var whisperModel = ""

    private init() { load() }

    var models: [String] {
        var out = [model]
        for m in fallbackModels where !m.isEmpty && !out.contains(m) { out.append(m) }
        return out
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
        if let v = obj["restore_clipboard"] as? Bool { restoreClipboard = v }
        if let v = obj["restore_delay_seconds"] as? Double { restoreDelay = v }
        if let v = obj["hold_threshold_seconds"] as? Double { holdThreshold = v }
        if let v = obj["max_recording_seconds"] as? Double { maxRecordingSeconds = v }
        if let v = obj["model"] as? String, !v.isEmpty { model = v }
        if let v = obj["fallback_models"] as? [String] { fallbackModels = v }
        if let v = obj["language"] as? String, !v.isEmpty { language = v }
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
            "restore_clipboard": restoreClipboard,
            "restore_delay_seconds": restoreDelay,
            "hold_threshold_seconds": holdThreshold,
            "max_recording_seconds": maxRecordingSeconds,
            "model": model,
            "fallback_models": fallbackModels,
            "language": language,
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
