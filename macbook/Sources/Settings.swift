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
    var micIdleCloseSeconds = 30.0
    var hearingAlert = true
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
        if let v = obj["mic_idle_close_seconds"] as? Double { micIdleCloseSeconds = v }
        if let v = obj["hearing_alert"] as? Bool { hearingAlert = v }
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
            "mic_idle_close_seconds": micIdleCloseSeconds,
            "hearing_alert": hearingAlert,
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
    let term: String          // how it must be written
    let variants: [String]    // how Whisper / Gemini may mis-hear it
}

/// Shared dictionary, same file and format as the iMac version:
///   `услышал | ещё вариант = Как писать`   replacement + spelling hint
///   `Как писать`                           spelling hint only
/// Master copy lives in iCloud Drive (synced between Macs, private); a local cache
/// covers an evicted or not-yet-downloaded file. Per-app dictionaries sit next to it
/// in `dictionaries/<bundle id>.txt`.
final class UserDictionary {
    static let shared = UserDictionary()

    static let iCloudDir = Paths.home.appendingPathComponent(
        "Library/Mobile Documents/com~apple~CloudDocs/WhisperDictate", isDirectory: true)
    static let master = iCloudDir.appendingPathComponent("dictionary.txt")
    static let masterAppsDir = iCloudDir.appendingPathComponent("dictionaries", isDirectory: true)
    static let cache = Paths.configDir.appendingPathComponent("dictionary.cache.txt")
    static let cacheAppsDir = Paths.configDir.appendingPathComponent("dictionaries.cache", isDirectory: true)
    static let legacy = Paths.dictionaryFile   // old Lite-only file (`Term = sounds, like`)

    private var global: [DictEntry] = []
    private var globalStamp = ""
    private var perApp: [String: (stamp: String, entries: [DictEntry])] = [:]
    private let lock = NSLock()

    var count: Int { lock.lock(); defer { lock.unlock() }; return global.count }
    var usesICloud: Bool { FileManager.default.fileExists(atPath: UserDictionary.iCloudDir.path) }

    /// Cheap: re-parses only when the master file changed.
    func refresh() {
        let (text, stamp) = UserDictionary.read(master: UserDictionary.master, cache: UserDictionary.cache,
                                                legacy: UserDictionary.legacy)
        lock.lock()
        defer { lock.unlock() }
        if stamp != globalStamp {
            global = text.map(UserDictionary.parse) ?? []
            globalStamp = stamp
        }
    }

    func entries(for bundleID: String?) -> [DictEntry] {
        lock.lock()
        var out = global
        lock.unlock()
        if let key = UserDictionary.appKey(bundleID) {
            let (text, stamp) = UserDictionary.read(
                master: UserDictionary.masterAppsDir.appendingPathComponent(key + ".txt"),
                cache: UserDictionary.cacheAppsDir.appendingPathComponent(key + ".txt"), legacy: nil)
            lock.lock()
            if perApp[key]?.stamp != stamp { perApp[key] = (stamp, text.map(UserDictionary.parse) ?? []) }
            out += perApp[key]?.entries ?? []
            lock.unlock()
        }
        return out
    }

    /// Post-processing replacements (longer variants first), like the iMac Vocabulary.apply.
    func apply(_ text: String, bundleID: String?) -> String {
        return UserDictionary.apply(text, entries: entries(for: bundleID))
    }

    static func apply(_ text: String, entries: [DictEntry]) -> String {
        var pairs: [(String, String)] = []
        for e in entries { for v in e.variants { pairs.append((v, e.term)) } }
        pairs.sort { $0.0.count > $1.0.count }
        var out = text
        for (variant, term) in pairs {
            let words = variant.split(whereSeparator: { $0.isWhitespace }).map {
                NSRegularExpression.escapedPattern(for: String($0))
            }
            guard !words.isEmpty else { continue }
            let pattern = "(?<![\\p{L}\\p{N}_])" + words.joined(separator: "\\s+") + "(?![\\p{L}\\p{N}_])"
            guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { continue }
            out = re.stringByReplacingMatches(in: out, range: NSRange(out.startIndex..., in: out),
                                              withTemplate: NSRegularExpression.escapedTemplate(for: term))
        }
        return out
    }

    /// "Добавить слово из буфера": `Term` or a full `услышал = Как писать` line.
    func add(_ raw: String) throws -> String {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: .newlines).first?.trimmingCharacters(in: .whitespaces) ?? ""
        guard !line.isEmpty, line.count <= 120, !line.hasPrefix("#") else {
            throw NSError(domain: "dictionary", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "В буфере нет подходящего слова (одна строка, до 120 символов)"])
        }
        let target = usesICloud ? UserDictionary.master : UserDictionary.cache
        let fm = FileManager.default
        try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        var text = (try? String(contentsOf: target, encoding: .utf8)) ?? UserDictionary.header
        if text.components(separatedBy: .newlines).contains(where: { $0.trimmingCharacters(in: .whitespaces) == line }) {
            return line
        }
        if !text.hasSuffix("\n") { text += "\n" }
        text += line + "\n"
        try text.write(to: target, atomically: true, encoding: .utf8)
        refresh()
        return line
    }

    static let header = """
    # Словарь Whisper Dictate — общий для iMac и MacBook (iCloud Drive).
    # Формат:  что_услышал | ещё_вариант = Как писать
    #          Как писать            (только подсказка написания, без замены)
    # Строки с # и пустые игнорируются.

    """

    static func appKey(_ bundleID: String?) -> String? {
        guard let id = bundleID, !id.isEmpty else { return nil }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
        var key = String(id.unicodeScalars.map { allowed.contains($0) ? Character($0) : "_" })
        key = key.trimmingCharacters(in: CharacterSet(charactersIn: "._"))
        return key.isEmpty ? nil : String(key.prefix(120))
    }

    /// Reads the master; refreshes the cache from it; falls back to the cache when the
    /// iCloud file is evicted / missing (and asks iCloud to download it).
    static func read(master: URL, cache: URL, legacy: URL?) -> (String?, String) {
        let fm = FileManager.default
        if let text = try? String(contentsOf: master, encoding: .utf8) {
            if (try? String(contentsOf: cache, encoding: .utf8)) != text {
                try? fm.createDirectory(at: cache.deletingLastPathComponent(), withIntermediateDirectories: true)
                try? text.write(to: cache, atomically: true, encoding: .utf8)
            }
            return (text, "m:" + stamp(master))
        }
        let placeholder = master.deletingLastPathComponent()
            .appendingPathComponent("." + master.lastPathComponent + ".icloud")
        if fm.fileExists(atPath: placeholder.path) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/brctl")
            p.arguments = ["download", master.path]
            try? p.run()
        }
        if let text = try? String(contentsOf: cache, encoding: .utf8) {
            return (text, "c:" + stamp(cache))
        }
        if let legacy = legacy, let text = try? String(contentsOf: legacy, encoding: .utf8) {
            return (convertLegacy(text), "l:" + stamp(legacy))
        }
        return (nil, "none")
    }

    private static func stamp(_ url: URL) -> String {
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        let date = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? NSNumber)?.intValue ?? 0
        return "\(date)-\(size)"
    }

    /// Old Lite file wrote `Term = sounds, like`; the shared format is `sounds | like = Term`.
    static func convertLegacy(_ text: String) -> String {
        guard text.contains("Whisper Dictate Lite") else { return text }
        return text.components(separatedBy: .newlines).map { line -> String in
            let s = line.trimmingCharacters(in: .whitespaces)
            guard !s.hasPrefix("#"), let eq = s.firstIndex(of: "=") else { return line }
            let term = s[..<eq].trimmingCharacters(in: .whitespaces)
            let variants = s[s.index(after: eq)...].components(separatedBy: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            return variants.isEmpty ? term : variants.joined(separator: " | ") + " = " + term
        }.joined(separator: "\n")
    }

    static func parse(_ text: String) -> [DictEntry] {
        var out: [DictEntry] = []
        for line in text.components(separatedBy: .newlines) {
            let s = line.trimmingCharacters(in: .whitespaces)
            if s.isEmpty || s.hasPrefix("#") { continue }
            if let eq = s.lastIndex(of: "=") {
                let term = s[s.index(after: eq)...].trimmingCharacters(in: .whitespaces)
                let variants = s[..<eq].components(separatedBy: "|")
                    .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                if !term.isEmpty { out.append(DictEntry(term: term, variants: variants)) }
            } else {
                out.append(DictEntry(term: s, variants: []))
            }
        }
        // One entry per term: merge variants of duplicates.
        var merged: [String: Int] = [:]
        var result: [DictEntry] = []
        for e in out {
            if let i = merged[e.term.lowercased()] {
                let vs = result[i].variants + e.variants.filter { v in !result[i].variants.contains(v) }
                result[i] = DictEntry(term: result[i].term, variants: vs)
            } else {
                merged[e.term.lowercased()] = result.count
                result.append(e)
            }
        }
        return result
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
