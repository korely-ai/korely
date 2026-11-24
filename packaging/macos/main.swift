// The executable of Korely Installer.app (packaging/macos): it runs the
// app's AppleScript, Resources/Scripts/main.scpt (installer.applescript,
// compiled by scripts/make_mac_installer.sh), in its own process, and quits.
//
// Script Editor's applet stub would do the same, but it is built for the Mac
// it is compiled on (macOS 27 asks for 27), so this one is compiled with
// swiftc for both architectures and macOS 13 on. Nothing else: the dialogs
// are the script's, and the install is install.sh's.
import AppKit

final class Installer: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.activate(ignoringOtherApps: true)
        var error: NSDictionary?
        var ran = false
        if let url = Bundle.main.url(forResource: "main", withExtension: "scpt",
                                     subdirectory: "Scripts"),
           let script = NSAppleScript(contentsOf: url, error: &error) {
            ran = true
            script.executeAndReturnError(&error)
        }
        let number = error?[NSAppleScript.errorNumber] as? Int
        // -128 is the user's Quit.
        if !ran || (error != nil && number != -128) {
            let message = (error?[NSAppleScript.errorMessage] as? String)
                ?? "The app's script could not be read: download it again."
            FileHandle.standardError.write("Korely Installer: \(message) (\(number ?? 0))\n"
                .data(using: .utf8)!)
            let alert = NSAlert()
            alert.messageText = "Korely Installer stopped"
            alert.informativeText = message
            alert.runModal()
        }
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let installer = Installer()
app.delegate = installer
app.setActivationPolicy(.regular)
app.run()
