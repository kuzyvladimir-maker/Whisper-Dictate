import Cocoa

// "Тебя не слышно" warning while recording — port of the iMac version's hearing_alert.
// If the mic level stays near-silent (or the audio callback stalls), a big red panel is
// shown over all windows so a long monologue does not go into a dead mic. Recording keeps
// going; the panel hides as soon as speech is heard again.

final class HearingAlert {
    var level: Float = 0.03          // speech threshold ceiling
    var minLevel: Float = 0.008      // threshold floor
    var alertAfter = 6.0             // silence before any speech was heard
    var pauseAlertAfter = 20.0       // silence after speech (a natural pause is fine)

    private var timer: Timer?
    private var panel: NSPanel?
    private var startedAt = Date()
    private var quietSince: Date?
    private var noiseFloor: Float?
    private var heardSpeech = false
    private var shown = false

    /// Starts watching. `probe` returns (current level, last audio callback time).
    func start(probe: @escaping () -> (Float, Date)) {
        stop()
        startedAt = Date()
        quietSince = nil
        noiseFloor = nil
        heardSpeech = false
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.tick(probe())
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        hide()
    }

    private func tick(_ sample: (Float, Date)) {
        let (current, lastCallback) = sample
        let now = Date()
        // Let the mic spin up (AirPods take about a second to switch on).
        guard now.timeIntervalSince(startedAt) >= 1.0 else { return }
        if current > 0 { noiseFloor = min(noiseFloor ?? current, current) }
        var threshold = level
        if let floor = noiseFloor { threshold = min(level, max(minLevel, floor * 4)) }
        let stalled = now.timeIntervalSince(lastCallback) > 1.5
        if current >= threshold && !stalled {
            heardSpeech = true
            quietSince = nil
            if shown { hide() }
            return
        }
        if quietSince == nil { quietSince = now }
        var wait = heardSpeech ? pauseAlertAfter : alertAfter
        if stalled { wait = min(wait, 2.0) }
        if !shown, let since = quietSince, now.timeIntervalSince(since) >= wait {
            Log.write(String(format: "mic near-silent (level=%.4f threshold=%.4f stalled=%@) — showing warning",
                             current, threshold, stalled ? "yes" : "no"))
            show()
        }
    }

    private func show() {
        shown = true
        let w: CGFloat = 680
        let h: CGFloat = 96
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: w, height: h),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false
            p.backgroundColor = .clear
            p.level = .screenSaver
            p.ignoresMouseEvents = true
            p.hidesOnDeactivate = false
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
            let content = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
            content.wantsLayer = true
            content.layer?.cornerRadius = 18
            content.layer?.backgroundColor = NSColor(calibratedRed: 0.86, green: 0.12, blue: 0.12, alpha: 0.95).cgColor
            let label = NSTextField(labelWithString: "🔇  Тебя не слышно — говори громче (запись идёт)")
            label.frame = NSRect(x: 10, y: 22, width: w - 20, height: 52)
            label.alignment = .center
            label.textColor = .white
            label.font = NSFont.boldSystemFont(ofSize: 22)
            content.addSubview(label)
            p.contentView = content
            panel = p
        }
        if let screen = NSScreen.main {
            let f = screen.frame
            panel?.setFrameOrigin(NSPoint(x: f.origin.x + (f.width - w) / 2, y: f.origin.y + f.height - h - 120))
        }
        panel?.orderFrontRegardless()
    }

    private func hide() {
        shown = false
        panel?.orderOut(nil)
    }
}
