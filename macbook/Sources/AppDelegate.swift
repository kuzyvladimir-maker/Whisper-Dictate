import AVFoundation
import Cocoa

// Menu bar app: state machine for push-to-talk / tap-to-toggle, the processing pipeline
// (Gemini, local fallback), and the menu.
//
//   key down            -> start recording
//   key up after >=0.35s -> push-to-talk: stop and paste
//   key up sooner        -> toggle mode: keeps recording until the next key press
//   Esc                  -> cancel recording (or abandon a slow request; audio kept for retry)

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let settings = Settings.shared
    private let recorder = Recorder()
    private let gemini = GeminiClient()
    private let local = LocalWhisper()
    private let paster = Paster()
    private let hotkey = HotkeyMonitor()
    private let audioQueue = DispatchQueue(label: "audio")

    private enum State { case idle, holding, toggled, processing }
    private var state: State = .idle
    private var session = 0
    private var pressedAt = Date()
    private var processingSince = Date()
    private let dictionary = UserDictionary.shared
    private let hearing = HearingAlert()
    private var context: String?          // bundle id of the app where dictation started
    private var lastContext: String?
    private var statusItem: NSStatusItem!
    private var tick: Timer?
    private var permissionTimer: Timer?
    private var lastText: String?
    private var pendingAudio: [Int16]?
    private var failedAudio: [Int16]?
    private var lastInfo = ""
    private var flash: String?
    private var sounds: [String: NSSound] = [:]

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        let bundleID = Bundle.main.bundleIdentifier ?? "com.whisperdictate.lite"
        let me = ProcessInfo.processInfo.processIdentifier
        if NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .contains(where: { $0.processIdentifier != me }) {
            Log.write("another instance is running, exiting")
            NSApp.terminate(nil)
            return
        }
        reload()
        loadSounds()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        hotkey.choice = settings.hotkey
        hotkey.onDown = { [weak self] in self?.hotkeyDown() }
        hotkey.onUp = { [weak self] in self?.hotkeyUp() }
        hotkey.onCombo = { [weak self] what in self?.combo(what) }
        hotkey.onKey = { [weak self] code in self?.keyPressed(code) ?? false }

        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                DispatchQueue.main.async { self.updateIcon() }
            }
        }
        ensureHotkey(prompt: true)
        updateIcon()
        Log.write("started: keys=\(gemini.keyCount) models=\(settings.fastModel)/\(settings.model) local=\(local.isAvailable) "
                  + "hotkey=\(settings.hotkey.rawValue) arch=\(machineArch())")
    }

    private func reload() {
        gemini.setKeys(Env.loadKeys())
        dictionary.refresh()
        recorder.idleCloseSeconds = settings.micIdleCloseSeconds
    }

    private func ensureHotkey(prompt: Bool) {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary
        if AXIsProcessTrustedWithOptions(options) && hotkey.start() {
            permissionTimer?.invalidate()
            permissionTimer = nil
            Log.write("hotkey tap active")
            updateIcon()
            return
        }
        if permissionTimer == nil {
            permissionTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
                self?.ensureHotkey(prompt: false)
            }
        }
        updateIcon()
    }

    // MARK: - Hotkey state machine

    private var isRecording: Bool { state == .holding || state == .toggled }

    private func hotkeyDown() {
        Log.write("hotkey down (state=\(state))")
        switch state {
        case .idle: startRecording()
        case .toggled: finishRecording()
        case .holding, .processing: break
        }
    }

    private func hotkeyUp() {
        Log.write("hotkey up (state=\(state), held \(fmt(Date().timeIntervalSince(pressedAt)))s)")
        guard state == .holding else { return }
        if Date().timeIntervalSince(pressedAt) >= settings.holdThreshold {
            finishRecording()
        } else {
            state = .toggled
            updateIcon()
        }
    }

    private func combo(_ what: String) {
        if state == .holding {
            Log.write("combo with hotkey (\(what)): recording cancelled")
            cancelRecording(silent: true)
        }
    }

    private func keyPressed(_ code: Int64) -> Bool {
        guard code == 53 else { return false }  // Esc
        if isRecording {
            cancelRecording(silent: false)
            return true
        }
        if state == .processing && Date().timeIntervalSince(processingSince) > 2.5 {
            session += 1
            state = .idle
            failedAudio = pendingAudio
            pendingAudio = nil
            note("Обработка прервана — запись можно отправить снова из меню")
            play("cancel")
            updateIcon()
            return true
        }
        return false
    }

    private func startRecording() {
        let mic = AVCaptureDevice.authorizationStatus(for: .audio)
        if mic == .denied || mic == .restricted {
            showError("Нет доступа к микрофону: Системные настройки → Конфиденциальность → Микрофон")
            return
        }
        session += 1
        let my = session
        state = .holding
        pressedAt = Date()
        flash = nil
        context = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
        if context != nil { lastContext = context }
        if !settings.localOnly && gemini.keyCount > 0 { gemini.warmUp() }
        audioQueue.async {
            var failure: Error?
            do { try self.recorder.start() } catch { failure = error }
            DispatchQueue.main.async {
                if let failure = failure {
                    Log.write("recorder start failed: \(failure)")
                    if my == self.session && self.isRecording {
                        self.session += 1
                        self.state = .idle
                        self.stopTick()
                        self.hearing.stop()
                    }
                    self.showError("Микрофон: \(failure.localizedDescription)")
                    return
                }
                Log.write("recording started")
                if self.settings.hearingAlert && my == self.session && self.isRecording {
                    self.hearing.start { [weak self] in
                        guard let self = self else { return (1, Date()) }
                        return (self.recorder.currentLevel, self.recorder.lastCallbackAt)
                    }
                }
                // Short delay so Option+key typing (which cancels) does not beep.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                    if my == self.session && self.isRecording { self.play("start") }
                }
            }
        }
        startTick()
        updateIcon()
    }

    private func finishRecording() {
        guard isRecording else { return }
        state = .processing
        stopTick()
        hearing.stop()
        let released = Date()
        play("stop")
        updateIcon()
        let my = session
        audioQueue.async {
            let (samples, peak) = self.recorder.stop()
            Log.write("recording stopped: \(fmt(Double(samples.count) / Recorder.sampleRate))s, peak \(fmt(Double(peak)))")
            DispatchQueue.main.async {
                guard my == self.session else { return }
                self.process(samples: samples, peak: peak, released: released)
            }
        }
    }

    private func cancelRecording(silent: Bool) {
        guard isRecording else { return }
        Log.write("recording cancelled\(silent ? " (combo)" : " (Esc)")")
        session += 1
        state = .idle
        stopTick()
        hearing.stop()
        audioQueue.async { _ = self.recorder.stop() }
        if !silent {
            play("cancel")
            note("Запись отменена")
        }
        updateIcon()
    }

    // MARK: - Pipeline

    private func process(samples: [Int16], peak: Float, released: Date) {
        let seconds = Double(samples.count) / Recorder.sampleRate
        if seconds < 0.3 {
            state = .idle
            updateIcon()
            return
        }
        if peak < 0.006 {
            state = .idle
            Log.write("silence: peak=\(peak) audio=\(fmt(seconds))s")
            note("Тишина — микрофон ничего не услышал")
            play("cancel")
            updateIcon()
            return
        }
        state = .processing
        processingSince = Date()
        pendingAudio = samples
        updateIcon()
        let token = session
        let mode = settings.mode

        if settings.localOnly {
            runLocal(samples, released: released, seconds: seconds, token: token, reason: nil)
            return
        }
        if gemini.keyCount == 0 {
            if settings.localFallback && local.isAvailable {
                runLocal(samples, released: released, seconds: seconds, token: token, reason: "нет ключа Gemini")
            } else {
                fail(samples, GeminiError.noKeys.localizedDescription)
            }
            return
        }
        dictionary.refresh()
        let app = context
        let system = Prompts.system(mode: mode, dictionary: dictionary.entries(for: app))
        let upload = Audio.encodeForUpload(samples, format: settings.audioFormat)
        gemini.transcribe(audio: upload.data, mimeType: upload.mime, audioSeconds: seconds, system: system,
                          user: Prompts.user, models: settings.models(mode: mode, seconds: seconds)) { result in
            DispatchQueue.main.async {
                guard token == self.session, self.state == .processing else { return }
                switch result {
                case .success(let r):
                    let engine = r.attempts > 1 ? "\(r.model), попыток \(r.attempts)" : r.model
                    self.deliver(Prompts.clean(r.text, mode: mode), mode: mode, released: released,
                                 seconds: seconds, engine: engine)
                case .failure(let e):
                    Log.write("gemini failed: \(e.localizedDescription)")
                    if e.allowsLocalFallback && self.settings.localFallback && self.local.isAvailable {
                        self.runLocal(samples, released: released, seconds: seconds, token: token,
                                      reason: e.localizedDescription)
                    } else {
                        self.fail(samples, e.localizedDescription)
                    }
                }
            }
        }
    }

    private func runLocal(_ samples: [Int16], released: Date, seconds: Double, token: Int, reason: String?) {
        if let reason = reason { Log.write("local fallback: \(reason)") }
        local.transcribe(samples: samples, language: settings.language,
                         hint: Prompts.whisperHint(dictionary: dictionary.entries(for: context))) { result in
            DispatchQueue.main.async {
                guard token == self.session, self.state == .processing else { return }
                switch result {
                case .success(let text):
                    let engine = reason == nil ? "whisper.cpp" : "whisper.cpp (фолбэк)"
                    self.deliver(text, mode: self.settings.mode, released: released, seconds: seconds, engine: engine)
                    if let reason = reason { self.note("Gemini недоступен (\(reason)) — текст без причёсывания") }
                case .failure(let e):
                    self.fail(samples, e.localizedDescription)
                }
            }
        }
    }

    private func deliver(_ text: String, mode: DictationMode, released: Date, seconds: Double, engine: String) {
        state = .idle
        pendingAudio = nil
        failedAudio = nil
        guard !text.isEmpty else {
            Log.write("empty result: audio=\(fmt(seconds))s engine=\(engine)")
            note("Речь не распознана")
            play("cancel")
            updateIcon()
            return
        }
        var out = dictionary.apply(text, bundleID: context)
        if settings.appendSpace && mode != .code { out += " " }
        lastText = out
        if settings.paste {
            paster.paste(out, restore: settings.restoreClipboard, restoreDelay: settings.restoreDelay)
        } else {
            paster.copy(out)
        }
        let latency = Date().timeIntervalSince(released)
        lastInfo = String(format: "%.1f с после отпускания · %@ · запись %.0f с", latency, engine, seconds)
        Log.write("done: audio=\(fmt(seconds))s release->paste=\(fmt(latency))s engine=\(engine) chars=\(out.count)")
        updateIcon()
    }

    private func fail(_ samples: [Int16], _ message: String) {
        state = .idle
        pendingAudio = nil
        failedAudio = samples  // memory only; "Повторить" in the menu
        showError(message + " — запись можно отправить снова из меню")
    }

    // MARK: - Feedback

    private func loadSounds() {
        let names = ["start": "Ping", "stop": "Pop", "error": "Basso", "cancel": "Funk"]
        for (key, name) in names {
            if let s = NSSound(named: NSSound.Name(name)) { sounds[key] = s }
        }
    }

    private func play(_ name: String) {
        guard settings.sounds, let s = sounds[name] else { return }
        s.stop()
        s.play()
    }

    private func note(_ message: String) {
        flash = message
        updateIcon()
    }

    private func showError(_ message: String) {
        Log.write("error: \(message)")
        flash = message
        play("error")
        updateIcon()
        let script = "display notification \(appleScriptString(message)) with title \"Whisper Dictate Lite\""
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        try? p.run()
    }

    private func appleScriptString(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    private func startTick() {
        tick?.invalidate()
        tick = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if self.settings.maxRecordingSeconds > 0,
               Date().timeIntervalSince(self.pressedAt) >= self.settings.maxRecordingSeconds {
                self.note("Достигнут лимит длины записи")
                self.finishRecording()
            }
            self.updateIcon()
        }
    }

    private func stopTick() {
        tick?.invalidate()
        tick = nil
    }

    private var needsAttention: Bool {
        !hotkey.isRunning || (!settings.localOnly && gemini.keyCount == 0 && !local.isAvailable)
            || AVCaptureDevice.authorizationStatus(for: .audio) == .denied
    }

    private func updateIcon() {
        guard let button = statusItem?.button else { return }
        var symbol = settings.localOnly ? "mic.circle" : "mic"
        var fallback = "🎙"
        var tint: NSColor?
        var title = ""
        switch state {
        case .idle:
            if needsAttention { symbol = "exclamationmark.triangle"; fallback = "⚠️" }
        case .holding, .toggled:
            symbol = "mic.fill"
            fallback = "🔴"
            tint = .systemRed
            let t = Int(Date().timeIntervalSince(pressedAt))
            title = String(format: " %d:%02d", t / 60, t % 60)
        case .processing:
            symbol = "ellipsis.circle"
            fallback = "⏳"
        }
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "Whisper Dictate Lite") {
            img.isTemplate = true
            button.image = img
            button.imagePosition = title.isEmpty ? .imageOnly : .imageLeft
            button.title = title
        } else {
            button.image = nil
            button.title = fallback + title
        }
        button.contentTintColor = tint
    }

    // MARK: - Menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let status: String
        switch state {
        case .idle: status = "Готов · держите \(settings.hotkey.title)"
        case .holding: status = "Запись… отпустите клавишу, чтобы вставить"
        case .toggled: status = "Запись… нажмите клавишу ещё раз (Esc — отмена)"
        case .processing: status = "Распознаю… (Esc — прервать)"
        }
        menu.addItem(info(status))
        if settings.localOnly {
            menu.addItem(info(local.isAvailable ? "Только локально · whisper.cpp" : "⚠️ Только локально, но whisper.cpp не установлен"))
        } else if gemini.keyCount == 0 {
            menu.addItem(info("⚠️ Нет ключа Gemini (~/.config/whisper-dictate/env)"))
        } else {
            menu.addItem(info("Gemini · \(settings.fastModel) / \(settings.model) · ключей: \(gemini.keyCount)"))
        }
        if !lastInfo.isEmpty { menu.addItem(info("Последняя: \(lastInfo)")) }
        if let f = flash { menu.addItem(info("⚠️ \(f)")) }
        if !hotkey.isRunning {
            menu.addItem(item("⚠️ Дать доступ «Универсальный доступ»…", #selector(openAccessibility)))
        }
        if AVCaptureDevice.authorizationStatus(for: .audio) == .denied {
            menu.addItem(item("⚠️ Дать доступ к микрофону…", #selector(openMicrophone)))
        }

        menu.addItem(.separator())
        menu.addItem(info("Режим"))
        for m in DictationMode.allCases {
            let it = item("   " + m.title, #selector(selectMode(_:)))
            it.representedObject = m.rawValue
            it.state = settings.mode == m ? .on : .off
            menu.addItem(it)
        }

        menu.addItem(.separator())
        let keyItem = NSMenuItem(title: "Клавиша: \(settings.hotkey.title)", action: nil, keyEquivalent: "")
        let keyMenu = NSMenu()
        for h in HotkeyChoice.allCases {
            let it = item(h.title, #selector(selectHotkey(_:)))
            it.representedObject = h.rawValue
            it.state = settings.hotkey == h ? .on : .off
            keyMenu.addItem(it)
        }
        keyItem.submenu = keyMenu
        menu.addItem(keyItem)
        let localItem = item("Только локально (без Google)", #selector(toggleLocalOnly))
        localItem.state = settings.localOnly ? .on : .off
        menu.addItem(localItem)
        let soundItem = item("Звуки", #selector(toggleSounds))
        soundItem.state = settings.sounds ? .on : .off
        menu.addItem(soundItem)

        menu.addItem(.separator())
        if failedAudio != nil {
            menu.addItem(item("Повторить отправку последней записи", #selector(retryFailed)))
        }
        if lastText != nil {
            menu.addItem(item("Скопировать последний текст", #selector(copyLast)))
        }
        menu.addItem(item("Добавить слово из буфера", #selector(addWordFromClipboard)))
        menu.addItem(item(dictionary.usesICloud ? "Словарь (iCloud, общий)…" : "Словарь…", #selector(openDictionary)))
        if let app = lastContext, let name = NSRunningApplication.runningApplications(withBundleIdentifier: app).first?.localizedName {
            menu.addItem(item("Словарь для «\(name)»…", #selector(openAppDictionary)))
        }
        menu.addItem(item("Папка настроек…", #selector(openConfigDir)))
        menu.addItem(item("Перечитать ключи и словарь", #selector(reloadAll)))
        menu.addItem(item("Журнал…", #selector(openLog)))
        menu.addItem(.separator())
        menu.addItem(item("Выйти", #selector(quit)))
    }

    private func info(_ title: String) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        it.isEnabled = false
        return it
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: "")
        it.target = self
        return it
    }

    @objc private func selectMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let m = DictationMode(rawValue: raw) else { return }
        settings.mode = m
        settings.save()
    }

    @objc private func selectHotkey(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let h = HotkeyChoice(rawValue: raw) else { return }
        if isRecording { cancelRecording(silent: true) }
        settings.hotkey = h
        settings.save()
        hotkey.choice = h
        hotkey.reset()
        if h == .fn {
            alert("Клавиша Fn",
                  "Чтобы Fn не переключал раскладку и не открывал эмодзи, выберите в Системных настройках → "
                  + "Клавиатура → «Нажатие клавиши fn/🌐»: «Ничего не делать».")
        }
        updateIcon()
    }

    @objc private func toggleLocalOnly() {
        if !settings.localOnly && !local.isAvailable {
            alert("Локальный движок не установлен",
                  "Установите whisper.cpp и модель small (~470 МБ):\n\nbash install.sh --with-local\n\n"
                  + "Без него режим «только локально» работать не будет.")
            return
        }
        settings.localOnly.toggle()
        settings.save()
        updateIcon()
    }

    @objc private func toggleSounds() {
        settings.sounds.toggle()
        settings.save()
    }

    @objc private func retryFailed() {
        guard state == .idle, let samples = failedAudio else { return }
        session += 1
        state = .processing
        process(samples: samples, peak: Audio.peak(samples), released: Date())
    }

    @objc private func copyLast() {
        if let t = lastText { paster.copy(t.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }

    @objc private func openDictionary() {
        let url = dictionary.usesICloud ? UserDictionary.master : UserDictionary.cache
        if !FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            var template = UserDictionary.header
            if let bundled = Bundle.main.url(forResource: "dictionary.example", withExtension: "txt"),
               let text = try? String(contentsOf: bundled, encoding: .utf8) {
                template = text
            }
            try? template.write(to: url, atomically: true, encoding: .utf8)
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func openAppDictionary() {
        guard let key = UserDictionary.appKey(lastContext) else { return }
        let dir = dictionary.usesICloud ? UserDictionary.masterAppsDir : UserDictionary.cacheAppsDir
        let url = dir.appendingPathComponent(key + ".txt")
        if !FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let text = "# Словарь для приложения: \(lastContext ?? key)\n# Формат: что_услышал | вариант = Как писать\n"
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func addWordFromClipboard() {
        let clip = NSPasteboard.general.string(forType: .string) ?? ""
        do {
            let line = try dictionary.add(clip)
            note("В словарь добавлено: \(line)")
            Log.write("dictionary: word added (\(line.count) chars)")
        } catch {
            showError(error.localizedDescription)
        }
    }

    @objc private func openConfigDir() {
        try? FileManager.default.createDirectory(at: Paths.configDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Paths.configDir)
    }

    @objc private func reloadAll() {
        reload()
        settings.load()
        hotkey.choice = settings.hotkey
        note("Ключей: \(gemini.keyCount), слов в словаре: \(dictionary.count)")
        Log.write("reloaded: keys=\(gemini.keyCount) dictionary=\(dictionary.count)")
    }

    @objc private func openLog() {
        if FileManager.default.fileExists(atPath: Paths.logFile.path) { NSWorkspace.shared.open(Paths.logFile) }
    }

    @objc private func openAccessibility() {
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openMicrophone() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func quit() {
        recorder.shutdown()
        NSApp.terminate(nil)
    }

    private func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }
}

func machineArch() -> String {
    var info = utsname()
    uname(&info)
    return withUnsafePointer(to: &info.machine) {
        $0.withMemoryRebound(to: CChar.self, capacity: 256) { String(cString: $0) }
    }
}
