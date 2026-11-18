import Foundation

// An isolated URLSession transport: never contacts Google or loads real keys.
final class SelftestURLProtocol: URLProtocol {
    static let lock = NSLock()
    static var started: [String] = []
    static var cancelled = 0
    static var firstFails = false
    static var failStatus = 429
    private var work: DispatchWorkItem?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let key = request.value(forHTTPHeaderField: "x-goog-api-key") ?? ""
        Self.lock.lock()
        Self.started.append(key)
        let fails = Self.firstFails && key == "test-A"
        Self.lock.unlock()
        // A is slow (no answer for 4 s) and B, raced at once on another key, answers fast;
        // or A fails at once (429 / 402) and B takes over. A must be cancelled on B's win.
        let delay = key == "test-A" ? (fails ? 0.01 : 4.0) : 0.08
        let status = fails ? Self.failStatus : 200
        let work = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status,
                                           httpVersion: "HTTP/1.1", headerFields: nil)!
            let body = fails ? "{\"error\":{\"message\":\"quota\"}}" :
                "{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"test answer\"}]},\"finishReason\":\"STOP\"}]}"
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Data(body.utf8))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        self.work = work
        DispatchQueue.global().asyncAfter(deadline: .now() + delay, execute: work)
    }
    override func stopLoading() {
        work?.cancel()
        Self.lock.lock(); Self.cancelled += 1; Self.lock.unlock()
    }

    static func run() -> [(String, Bool)] {
        var checks: [(String, Bool)] = []
        for (fails, status) in [(false, 200), (true, 429), (true, 402)] {
            lock.lock(); started = []; cancelled = 0; firstFails = fails; failStatus = status; lock.unlock()
            let cfg = URLSessionConfiguration.ephemeral
            cfg.protocolClasses = [SelftestURLProtocol.self]
            let client = GeminiClient(configuration: cfg)
            client.setKeys(["test-A", "test-B", "test-A"])
            let done = DispatchSemaphore(value: 0)
            var result: GeminiResult?
            let t = Date()
            client.transcribe(audio: Data([0, 1]), mimeType: "audio/wav", audioSeconds: 1,
                              system: "test", user: "test", models: ["test-flash-lite"]) {
                if case .success(let r) = $0 { result = r }
                done.signal()
            }
            let completed = done.wait(timeout: .now() + 6) == .success
            let elapsed = Date().timeIntervalSince(t)
            // URLSession cancellation is asynchronous; bound the wait rather than relying
            // on ordering between completion and stopLoading().
            if !fails {
                for _ in 0..<100 {
                    lock.lock(); let stopped = cancelled > 0; lock.unlock()
                    if stopped { break }
                    Thread.sleep(forTimeInterval: 0.005)
                }
            }
            lock.lock(); let sent = started; let stopped = cancelled; lock.unlock()
            let ok = completed && result?.keyIndex == 1 && result?.attempts == 2
                && Set(sent) == Set(["test-A", "test-B"]) && sent.count == 2
            if !fails {
                // Second request goes out at once on another key; B wins long before A's 4 s.
                checks.append((String(format: "immediate two-key race (%.2fs)", elapsed), ok && elapsed < 1))
                checks.append(("loser cancelled", stopped > 0))
            } else {
                checks.append(("\(status) on one key -> next key", ok && elapsed < 1))
            }
            checks.append(("duplicate keys removed", client.keyCount == 2))
        }
        return checks
    }
}
