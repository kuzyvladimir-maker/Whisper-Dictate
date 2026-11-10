import Cocoa

// Global hotkey through a CGEvent tap. A single modifier key (Fn by default)
// works as the trigger: macOS has no "modifier alone" hotkey API, but flagsChanged events
// carry the device-specific left/right bits. An active (not listen-only) tap needs only
// the Accessibility permission, which pasting needs anyway, and lets us swallow Esc
// while recording.

final class HotkeyMonitor {
    var choice: HotkeyChoice = .fn
    var onDown: (() -> Void)?
    var onUp: (() -> Void)?
    /// Any other key or modifier while the hotkey is held (Option+key typing, Fn+F1, ...).
    var onCombo: ((String) -> Void)?
    /// Key down while not holding the hotkey. Return true to swallow the event.
    var onKey: ((Int64) -> Bool)?

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private(set) var isHeld = false

    var isRunning: Bool { tap != nil }

    func start() -> Bool {
        if tap != nil { return true }
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                                          options: .defaultTap, eventsOfInterest: CGEventMask(mask),
                                          callback: hotkeyTapCallback,
                                          userInfo: Unmanaged.passUnretained(self).toOpaque()) else {
            return false
        }
        let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        self.tap = tap
        self.source = src
        return true
    }

    fileprivate func handle(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let tap = tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        case .flagsChanged:
            let code = event.getIntegerValueField(.keyboardEventKeycode)
            let foreign = isForeign(event)
            if code == choice.keyCode {
                let down = pressed(event.flags)
                if down && !isHeld {
                    isHeld = true
                    onDown?()
                } else if !down && isHeld {
                    isHeld = false
                    onUp?()
                }
            } else if isHeld && !foreign {
                onCombo?("modifier \(code) flags 0x\(String(event.flags.rawValue, radix: 16))")
            }
        case .keyDown:
            if event.getIntegerValueField(.keyboardEventAutorepeat) != 0 { break }
            let code = event.getIntegerValueField(.keyboardEventKeycode)
            if isHeld && !isForeign(event) {
                onCombo?("key \(code) flags 0x\(String(event.flags.rawValue, radix: 16))")
            } else if let onKey = onKey, onKey(code) {
                return nil
            }
        default:
            break
        }
        return Unmanaged.passUnretained(event)
    }

    private func pressed(_ flags: CGEventFlags) -> Bool {
        // Device-dependent bits from IOLLEvent.h.
        let raw = flags.rawValue
        switch choice {
        case .rightOption: return raw & 0x40 != 0      // NX_DEVICERALTKEYMASK
        case .rightCommand: return raw & 0x10 != 0     // NX_DEVICERCMDKEYMASK
        case .fn: return flags.contains(.maskSecondaryFn)
        }
    }

    /// Synthetic events posted by other apps. Punto Switcher, for one, answers a lone Option
    /// release with Cmd+C; that must not count as "the user typed Option+key".
    /// Hardware (and Karabiner's virtual keyboard) events carry source pid 0.
    private func isForeign(_ event: CGEvent) -> Bool {
        let pid = event.getIntegerValueField(.eventSourceUnixProcessID)
        return pid != 0 && pid != Int64(getpid())
    }

    /// Called after a hotkey switch so a half-pressed old key cannot leave us "held".
    func reset() { isHeld = false }
}

private func hotkeyTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                               refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let refcon = refcon else { return Unmanaged.passUnretained(event) }
    let monitor = Unmanaged<HotkeyMonitor>.fromOpaque(refcon).takeUnretainedValue()
    return monitor.handle(type: type, event: event)
}
