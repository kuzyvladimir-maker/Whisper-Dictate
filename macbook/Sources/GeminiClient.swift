import Foundation

// One generateContent call per dictation: audio in, finished text out.
// Free-tier keys hit 429 quickly, so every (model, key) pair has its own cooldown and a
// request walks the pairs in round-robin order: next key first, then the fallback model
// (which has a separate quota), then one short pause if a cooldown is about to expire.

struct GeminiResult {
    let text: String
    let model: String
    let keyIndex: Int
    let attempts: Int
    let seconds: Double
}

enum GeminiError: Error, LocalizedError {
    case noKeys
    case rateLimited
    case badKeys
    case network(String)
    case http(Int, String)
    case blocked(String)

    var errorDescription: String? {
        switch self {
        case .noKeys: return "Нет ключа Gemini (~/.config/whisper-dictate/env)"
        case .rateLimited: return "Лимит Gemini исчерпан на всех ключах"
        case .badKeys: return "Ключи Gemini не принимаются"
        case .network(let m): return "Нет связи с Gemini: \(m)"
        case .http(let code, let m): return "Gemini \(code): \(m)"
        case .blocked(let m): return "Gemini отказался обработать запись: \(m)"
        }
    }

    /// Worth switching to the local engine (as opposed to a request Gemini will never accept).
    var allowsLocalFallback: Bool {
        switch self {
        case .noKeys, .rateLimited, .badKeys, .network: return true
        case .http(let code, _): return code >= 500
        case .blocked: return false
        }
    }
}

final class GeminiClient {
    private let session: URLSession
    private let q = DispatchQueue(label: "gemini")
    private var keys: [String] = []
    private var roundRobin = 0
    private var cooldown: [String: Date] = [:]      // "model#keyIndex" -> until
    private var keyBanned: [Int: Date] = [:]        // keyIndex -> until (invalid key, permission)
    private var missingModels = Set<String>()
    private var noThinkingConfig = Set<String>()

    let host = "https://generativelanguage.googleapis.com"

    init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.httpMaximumConnectionsPerHost = 4
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 600
        cfg.waitsForConnectivity = false
        cfg.urlCache = nil
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: cfg)
    }

    var keyCount: Int { q.sync { keys.count } }

    func setKeys(_ newKeys: [String]) {
        q.sync {
            if newKeys != keys {
                keys = newKeys
                roundRobin = 0
                cooldown.removeAll()
                keyBanned.removeAll()
            }
        }
    }

    /// Opens DNS + TCP + TLS while the user is still talking, so the real request on key
    /// release reuses a hot HTTP/2 connection.
    func warmUp() {
        guard let url = URL(string: host + "/") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "HEAD"
        req.timeoutInterval = 5
        session.dataTask(with: req).resume()
    }

    func transcribe(wav: Data, audioSeconds: Double, system: String, user: String, models: [String],
                    completion: @escaping (Result<GeminiResult, GeminiError>) -> Void) {
        q.async {
            if self.keys.isEmpty {
                completion(.failure(.noKeys))
                return
            }
            let job = Job(client: self, b64: wav.base64EncodedString(), audioSeconds: audioSeconds,
                          system: system, user: user, models: models, completion: completion)
            job.next()
        }
    }

    // MARK: - Job (runs on q)

    private final class Job {
        let client: GeminiClient
        let b64: String
        let audioSeconds: Double
        let system: String
        let user: String
        let models: [String]
        let completion: (Result<GeminiResult, GeminiError>) -> Void
        let started = Date()
        var tried = Set<String>()
        var attempts = 0
        var paused = false
        var lastError: GeminiError = .rateLimited

        init(client: GeminiClient, b64: String, audioSeconds: Double, system: String, user: String,
             models: [String], completion: @escaping (Result<GeminiResult, GeminiError>) -> Void) {
            self.client = client
            self.b64 = b64
            self.audioSeconds = audioSeconds
            self.system = system
            self.user = user
            self.models = models
            self.completion = completion
        }

        func next() {
            let c = client
            let n = c.keys.count
            let now = Date()
            for model in models where !c.missingModels.contains(model) {
                for off in 0..<n {
                    let idx = (c.roundRobin + off) % n
                    let id = "\(model)#\(idx)"
                    if tried.contains(id) { continue }
                    if let until = c.keyBanned[idx], until > now { continue }
                    if let until = c.cooldown[id], until > now { continue }
                    tried.insert(id)
                    attempts += 1
                    c.send(model: model, keyIndex: idx, job: self)
                    return
                }
            }
            // Everything is cooling down. Wait once if a pair frees up soon.
            if !paused, let wait = c.soonestFree(models: models, now: now), wait <= 8 {
                paused = true
                tried.removeAll()
                Log.write("gemini: all pairs busy, pausing \(String(format: "%.1f", wait))s")
                c.q.asyncAfter(deadline: .now() + wait + 0.1) { self.next() }
                return
            }
            completion(.failure(lastError))
        }

        func finish(text: String, model: String, keyIndex: Int) {
            client.roundRobin = (keyIndex + 1) % max(client.keys.count, 1)
            completion(.success(GeminiResult(text: text, model: model, keyIndex: keyIndex, attempts: attempts,
                                             seconds: Date().timeIntervalSince(started))))
        }
    }

    private func soonestFree(models: [String], now: Date) -> Double? {
        var best: Double?
        for model in models where !missingModels.contains(model) {
            for idx in 0..<keys.count {
                if let b = keyBanned[idx], b > now { continue }
                let until = cooldown["\(model)#\(idx)"] ?? now
                let wait = max(0, until.timeIntervalSince(now))
                if best == nil || wait < best! { best = wait }
            }
        }
        return best
    }

    private func thinkingConfig(for model: String) -> [String: Any]? {
        if noThinkingConfig.contains(model) { return nil }
        if model.hasPrefix("gemini-2.5-pro") || model.contains("-pro") { return nil }
        if model.hasPrefix("gemini-2.5") { return ["thinkingBudget": 0] }
        if model.hasPrefix("gemini-3") { return ["thinkingLevel": "minimal"] }
        return ["thinkingBudget": 0]
    }

    private func body(model: String, job: Job) -> Data? {
        var generation: [String: Any] = ["temperature": 0, "maxOutputTokens": 8192]
        if let t = thinkingConfig(for: model) { generation["thinkingConfig"] = t }
        let safety = ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                      "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
            .map { ["category": $0, "threshold": "BLOCK_NONE"] }
        let obj: [String: Any] = [
            "systemInstruction": ["parts": [["text": job.system]]],
            "contents": [[
                "role": "user",
                "parts": [
                    ["inlineData": ["mimeType": "audio/wav", "data": job.b64]],
                    ["text": job.user],
                ],
            ]],
            "generationConfig": generation,
            "safetySettings": safety,
        ]
        return try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes])
    }

    private func send(model: String, keyIndex: Int, job: Job) {
        guard let url = URL(string: "\(host)/v1beta/models/\(model):generateContent"),
              let payload = body(model: model, job: job) else {
            job.completion(.failure(.http(0, "bad request")))
            return
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(keys[keyIndex], forHTTPHeaderField: "x-goog-api-key")
        req.timeoutInterval = 25 + job.audioSeconds * 0.5
        req.httpBody = payload
        let t0 = Date()
        session.dataTask(with: req) { data, response, error in
            self.q.async {
                let dt = Date().timeIntervalSince(t0)
                self.handle(model: model, keyIndex: keyIndex, job: job, data: data,
                            response: response as? HTTPURLResponse, error: error, seconds: dt)
            }
        }.resume()
    }

    private func handle(model: String, keyIndex: Int, job: Job, data: Data?, response: HTTPURLResponse?,
                        error: Error?, seconds: Double) {
        let tag = "\(model) key#\(keyIndex + 1)"
        if let error = error {
            let code = (error as NSError).code
            Log.write("gemini \(tag): network error \(code) after \(fmt(seconds))s")
            // A stale pooled connection dies with "connection lost": just try the next pair.
            if code == NSURLErrorNetworkConnectionLost {
                job.lastError = .network(error.localizedDescription)
                job.next()
            } else {
                job.completion(.failure(.network(error.localizedDescription)))
            }
            return
        }
        let status = response?.statusCode ?? 0
        let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        let err = json["error"] as? [String: Any]
        let message = (err?["message"] as? String) ?? ""

        switch status {
        case 200:
            let (text, finish, blockReason) = parse(json)
            Log.write("gemini \(tag): 200 in \(fmt(seconds))s, \(text.count) chars, finish=\(finish ?? "-")")
            if text.isEmpty, let reason = blockReason ?? (finish == "STOP" || finish == nil ? nil : finish) {
                job.lastError = .blocked(reason)
                job.next()
                return
            }
            job.finish(text: text, model: model, keyIndex: keyIndex)
        case 429:
            let wait = retryDelay(err) ?? 30
            let perDay = (data.flatMap { String(data: $0, encoding: .utf8) } ?? "").contains("PerDay")
            let cool = perDay ? max(wait, 3600) : wait
            cooldown["\(model)#\(keyIndex)"] = Date().addingTimeInterval(cool)
            Log.write("gemini \(tag): 429, cooldown \(Int(cool))s\(perDay ? " (daily quota)" : "")")
            job.lastError = .rateLimited
            job.next()
        case 400 where message.lowercased().contains("thinking") && !noThinkingConfig.contains(model):
            noThinkingConfig.insert(model)
            job.tried.remove("\(model)#\(keyIndex)")
            Log.write("gemini \(tag): thinking config rejected, retrying without it")
            job.next()
        case 400 where message.contains("API key") || message.contains("API_KEY"),
             401, 403:
            keyBanned[keyIndex] = Date().addingTimeInterval(6 * 3600)
            Log.write("gemini key#\(keyIndex + 1): rejected (\(status)), disabled for 6h")
            job.lastError = .badKeys
            job.next()
        case 404:
            missingModels.insert(model)
            Log.write("gemini \(model): not found, skipping model")
            job.lastError = .http(404, message)
            job.next()
        case 500...599:
            Log.write("gemini \(tag): \(status) \(message.prefix(120))")
            job.lastError = .http(status, message)
            job.next()
        default:
            Log.write("gemini \(tag): \(status) \(message.prefix(200))")
            job.completion(.failure(.http(status, message)))
        }
    }

    private func parse(_ json: [String: Any]) -> (String, String?, String?) {
        let block = (json["promptFeedback"] as? [String: Any])?["blockReason"] as? String
        guard let first = (json["candidates"] as? [[String: Any]])?.first else { return ("", nil, block) }
        let finish = first["finishReason"] as? String
        let parts = ((first["content"] as? [String: Any])?["parts"] as? [[String: Any]]) ?? []
        let text = parts.filter { ($0["thought"] as? Bool) != true }
            .compactMap { $0["text"] as? String }
            .joined()
        return (text, finish, block)
    }

    private func retryDelay(_ err: [String: Any]?) -> Double? {
        guard let details = err?["details"] as? [[String: Any]] else { return nil }
        for d in details {
            if let s = d["retryDelay"] as? String {
                let digits = s.trimmingCharacters(in: CharacterSet(charactersIn: "s"))
                if let v = Double(digits) { return max(1, v) }
            }
        }
        return nil
    }

    /// Model ids that support generateContent (for --list-models).
    func listModels(completion: @escaping ([String]) -> Void) {
        let key = q.sync { keys.first }
        guard let key = key, let url = URL(string: "\(host)/v1beta/models?pageSize=200") else {
            completion([])
            return
        }
        var req = URLRequest(url: url)
        req.setValue(key, forHTTPHeaderField: "x-goog-api-key")
        session.dataTask(with: req) { data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
            let models = (json["models"] as? [[String: Any]]) ?? []
            let names = models.filter { (($0["supportedGenerationMethods"] as? [String]) ?? []).contains("generateContent") }
                .compactMap { ($0["name"] as? String)?.replacingOccurrences(of: "models/", with: "") }
            completion(names)
        }.resume()
    }
}

func fmt(_ v: Double) -> String { String(format: "%.2f", v) }
