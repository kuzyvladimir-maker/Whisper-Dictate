import Cocoa

let arguments = Array(CommandLine.arguments.dropFirst())
if let first = arguments.first, first.hasPrefix("--") {
    exit(CLI.run(arguments))
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
