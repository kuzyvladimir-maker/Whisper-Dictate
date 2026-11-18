import Foundation

// Keeps the last few recordings on disk (WAV, 16 kHz mono) so a crash or restart never loses
// what was just dictated and "Повторить отправку" survives a relaunch. The folder lives in
// ~/Library/Application Support — outside the repo and outside iCloud — and files are 0600.
enum RecordingStore {
    static let dir = Paths.recordingsDir

    /// Writes the samples as a timestamped WAV and prunes old files. Returns the file URL.
    @discardableResult
    static func save(_ samples: [Int16], keep: Int, in dir: URL = RecordingStore.dir, name: String? = nil) -> URL? {
        guard !samples.isEmpty else { return nil }
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true,
                                   attributes: [.posixPermissions: 0o700])
            let url = dir.appendingPathComponent("rec-\(name ?? stamp()).wav")
            try Audio.wav(samples).write(to: url, options: .atomic)
            try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            prune(keep: keep, in: dir)
            return url
        } catch {
            Log.write("recording save failed: \(error.localizedDescription)")
            return nil
        }
    }

    /// Newest saved recording, if any (used by "Повторить отправку").
    static func latest(in dir: URL = RecordingStore.dir) -> URL? {
        return sorted(in: dir).last
    }

    static func hasAny(in dir: URL = RecordingStore.dir) -> Bool {
        return !sorted(in: dir).isEmpty
    }

    /// Deletes everything but the newest `keep` recordings.
    static func prune(keep: Int, in dir: URL = RecordingStore.dir) {
        let files = sorted(in: dir)
        guard files.count > keep, keep >= 0 else { return }
        for url in files.prefix(files.count - keep) {
            try? FileManager.default.removeItem(at: url)
        }
    }

    /// Recording files sorted oldest → newest (by filename, which is the timestamp).
    private static func sorted(in dir: URL) -> [URL] {
        let fm = FileManager.default
        let items = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
        return items.filter { $0.pathExtension == "wav" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    private static func stamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmmss-SSS"
        return f.string(from: Date())
    }
}
