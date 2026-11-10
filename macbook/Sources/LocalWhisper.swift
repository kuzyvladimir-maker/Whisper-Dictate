import Foundation

// Optional offline engine: whisper.cpp `whisper-cli` + ggml-small. Used for "только локально"
// and when Gemini is unreachable or out of quota. Raw transcript, no cleanup pass.
// whisper-cli only reads files, so the audio goes to a 0600 temp file that is deleted the
// moment the process exits.

enum LocalWhisperError: Error, LocalizedError {
    case notInstalled
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .notInstalled: return "Локальный движок не установлен (bash install.sh --with-local)"
        case .failed(let m): return "whisper.cpp: \(m)"
        }
    }
}

final class LocalWhisper {
    private let queue = DispatchQueue(label: "local-whisper")

    func locate() -> (cli: String, model: String)? {
        let s = Settings.shared
        let fm = FileManager.default
        let cliCandidates = [s.whisperCli,
                             Paths.localDir.appendingPathComponent("bin/whisper-cli").path,
                             "/usr/local/bin/whisper-cli",
                             "/opt/homebrew/bin/whisper-cli"]
        let modelCandidates = [s.whisperModel,
                               Paths.localDir.appendingPathComponent("ggml-small.bin").path,
                               Paths.localDir.appendingPathComponent("ggml-small-q5_1.bin").path,
                               Paths.localDir.appendingPathComponent("ggml-base.bin").path]
        guard let cli = cliCandidates.first(where: { !$0.isEmpty && fm.isExecutableFile(atPath: $0) }),
              let model = modelCandidates.first(where: { !$0.isEmpty && fm.fileExists(atPath: $0) }) else {
            return nil
        }
        return (cli, model)
    }

    var isAvailable: Bool { locate() != nil }

    func transcribe(samples: [Int16], language: String, hint: String,
                    completion: @escaping (Result<String, LocalWhisperError>) -> Void) {
        guard let (cli, model) = locate() else {
            completion(.failure(.notInstalled))
            return
        }
        queue.async {
            let tmp = FileManager.default.temporaryDirectory
                .appendingPathComponent("wdl-\(UUID().uuidString).wav")
            defer { try? FileManager.default.removeItem(at: tmp) }
            guard FileManager.default.createFile(atPath: tmp.path, contents: Audio.wav(samples),
                                                 attributes: [.posixPermissions: 0o600]) else {
                completion(.failure(.failed("cannot write temp file")))
                return
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: cli)
            let threads = min(8, max(2, ProcessInfo.processInfo.activeProcessorCount))
            var args = ["-m", model, "-f", tmp.path, "-l", language, "-nt", "-np", "-t", "\(threads)"]
            if !hint.isEmpty { args += ["--prompt", hint] }
            p.arguments = args
            let out = Pipe()
            let err = Pipe()
            p.standardOutput = out
            p.standardError = err
            do {
                try p.run()
            } catch {
                completion(.failure(.failed(error.localizedDescription)))
                return
            }
            var errData = Data()
            let errDone = DispatchSemaphore(value: 0)
            DispatchQueue.global().async {
                errData = err.fileHandleForReading.readDataToEndOfFile()
                errDone.signal()
            }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            errDone.wait()
            p.waitUntilExit()
            try? FileManager.default.removeItem(at: tmp)
            guard p.terminationStatus == 0 else {
                let msg = String(data: errData, encoding: .utf8)?
                    .split(separator: "\n").last.map(String.init) ?? "exit \(p.terminationStatus)"
                completion(.failure(.failed(msg)))
                return
            }
            let text = (String(data: data, encoding: .utf8) ?? "")
                .components(separatedBy: .newlines)
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty && !($0.hasPrefix("[") && $0.hasSuffix("]")) }
                .joined(separator: " ")
            completion(.success(LocalWhisper.isHallucination(text) ? "" : text))
        }
    }

    /// Whisper invents these on silence or noise. A result is dropped only when it is nothing but
    /// such phrases (plus a name or two); real sentences containing the words are kept.
    static func isHallucination(_ text: String) -> Bool {
        let lower = text.lowercased()
        guard lower.count < 80 else { return false }
        let markers = ["dimatorzok", "amara.org", "продолжение следует", "спасибо за просмотр",
                       "субтитры сделал", "субтитры делал", "субтитры создавал", "редактор субтитров",
                       "корректор субтитров", "подписывайтесь на канал", "thank you for watching",
                       "thanks for watching"]
        var rest = lower
        for m in markers { rest = rest.replacingOccurrences(of: m, with: "") }
        guard rest != lower else { return false }
        let letters = rest.unicodeScalars.filter { CharacterSet.letters.contains($0) }.count
        return letters < 12
    }
}
