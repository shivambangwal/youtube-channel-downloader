import AppKit
import Darwin
import WebKit

final class ChannelDownloaderApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var port = 8765
    private var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var pollTimer: Timer?
    private var startedAt = Date()
    private var isReady = false
    private var isTerminating = false
    private var backendLog = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        buildWindow()
        showStartup("Starting Channel Downloader…", detail: "Preparing the private download engine.")
        port = findAvailablePort()
        do {
            let support = try prepareAppFiles()
            backendLog = support.appendingPathComponent("channel-downloader.log").path
            guard let python = findPython() else {
                showFailure("Python 3.11 or newer is required.", details: "Install Python from python.org, then reopen Channel Downloader. Setup details: \(backendLog)")
                return
            }
            launchBackend(python: python, support: support)
            beginReadinessPolling()
        } catch {
            showFailure("Channel Downloader could not prepare its files.", details: error.localizedDescription)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard backend?.isRunning == true, !isTerminating else { return .terminateNow }
        isTerminating = true
        let alert = NSAlert()
        alert.messageText = "Quit Channel Downloader?"
        alert.informativeText = "Closing the app stops its local engine. Any active download will stop; completed files stay in Downloads."
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Keep Open")
        let choice = alert.runModal()
        isTerminating = false
        if choice == .alertFirstButtonReturn {
            stopBackend()
            return .terminateNow
        }
        return .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        stopBackend()
    }

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.setValue(false, forKey: "drawsBackground")
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Channel Downloader"
        window.minSize = NSSize(width: 780, height: 640)
        window.center()
        window.contentView = webView
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "About Channel Downloader", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: ""))
        let updateItem = NSMenuItem(title: "Check for Updates…", action: #selector(checkForUpdates(_:)), keyEquivalent: "")
        updateItem.target = self
        appMenu.addItem(updateItem)
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Quit Channel Downloader", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)

        let windowItem = NSMenuItem(title: "Window", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(NSMenuItem(title: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m"))
        windowMenu.addItem(NSMenuItem(title: "Zoom", action: #selector(NSWindow.zoom(_:)), keyEquivalent: ""))
        windowItem.submenu = windowMenu
        mainMenu.addItem(windowItem)
        NSApp.mainMenu = mainMenu
    }

    @objc private func checkForUpdates(_ sender: Any?) {
        webView.evaluateJavaScript("document.querySelector('#update-button')?.click()") { _, error in
            if error != nil {
                NSWorkspace.shared.open(URL(string: "https://channel-downloader-updates.vercel.app/")!)
            }
        }
    }

    private func prepareAppFiles() throws -> URL {
        let fm = FileManager.default
        let support = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("Channel Downloader", isDirectory: true)
            .appendingPathComponent("App", isDirectory: true)
        let appEntry = support.appendingPathComponent("app.py")
        guard let bundled = Bundle.main.resourceURL?.appendingPathComponent("ChannelDownloader", isDirectory: true),
              fm.fileExists(atPath: bundled.appendingPathComponent("app.py").path) else {
            throw NSError(domain: "ChannelDownloader", code: 1, userInfo: [NSLocalizedDescriptionKey: "The app bundle is missing its download engine."])
        }
        let bundledVersion = (try? String(contentsOf: bundled.appendingPathComponent("VERSION"), encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "0"
        let installedVersion = (try? String(contentsOf: support.appendingPathComponent("VERSION"), encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "0"
        if !fm.fileExists(atPath: appEntry.path) || isVersion(bundledVersion, newerThan: installedVersion) {
            try fm.createDirectory(at: support, withIntermediateDirectories: true)
            for name in ["app.py", "README.md", "VERSION", "Start Downloader.command"] {
                let source = bundled.appendingPathComponent(name)
                let destination = support.appendingPathComponent(name)
                if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
                if fm.fileExists(atPath: source.path) { try fm.copyItem(at: source, to: destination) }
            }
            let web = bundled.appendingPathComponent("web", isDirectory: true)
            let installedWeb = support.appendingPathComponent("web", isDirectory: true)
            if fm.fileExists(atPath: installedWeb.path) { try fm.removeItem(at: installedWeb) }
            if fm.fileExists(atPath: web.path) { try fm.copyItem(at: web, to: installedWeb) }
        }
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        try fm.createDirectory(at: support.appendingPathComponent(".runtime", isDirectory: true), withIntermediateDirectories: true)
        return support
    }

    private func isVersion(_ candidate: String, newerThan current: String) -> Bool {
        let left = candidate.split(separator: ".").map { Int($0) ?? 0 }
        let right = current.split(separator: ".").map { Int($0) ?? 0 }
        for index in 0..<max(left.count, right.count) {
            let a = index < left.count ? left[index] : 0
            let b = index < right.count ? right[index] : 0
            if a != b { return a > b }
        }
        return false
    }

    private func findPython() -> URL? {
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: path)
            probe.arguments = ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"]
            probe.standardOutput = FileHandle.nullDevice
            probe.standardError = FileHandle.nullDevice
            do { try probe.run(); probe.waitUntilExit(); if probe.terminationStatus == 0 { return URL(fileURLWithPath: path) } } catch { continue }
        }
        return nil
    }

    private func findAvailablePort() -> Int {
        for candidate in 8765...8795 {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            guard fd >= 0 else { continue }
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = in_port_t(candidate).bigEndian
            address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
            let bindResult = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            close(fd)
            if bindResult == 0 { return candidate }
        }
        return 8765
    }

    private func launchBackend(python: URL, support: URL) {
        let process = Process()
        process.executableURL = python
        process.arguments = [support.appendingPathComponent("app.py").path]
        process.currentDirectoryURL = support
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["YT_DOWNLOADER_HOST"] = "127.0.0.1"
        environment["YT_DOWNLOADER_PORT"] = String(port)
        environment["YT_DOWNLOADER_DOWNLOAD_DIR"] = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads").path
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        let logURL = URL(fileURLWithPath: backendLog)
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            _ = try? handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
        } else {
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
        }
        process.terminationHandler = { [weak self] child in
            DispatchQueue.main.async {
                guard let self, !self.isReady else { return }
                let details = self.readRecentLog()
                self.showFailure("The download engine stopped while starting.", details: details.isEmpty ? "Setup details are saved in \(self.backendLog)." : details)
            }
        }
        do { try process.run(); backend = process }
        catch { showFailure("Channel Downloader could not start its download engine.", details: error.localizedDescription) }
    }

    private func beginReadinessPolling() {
        startedAt = Date()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            if Date().timeIntervalSince(self.startedAt) > 300 {
                timer.invalidate()
                self.showFailure("Channel Downloader is taking longer than expected to start.", details: self.readRecentLog())
                return
            }
            if Date().timeIntervalSince(self.startedAt) > 18 {
                self.showStartup("Setting up for first use…", detail: "Downloading the private video tools. This usually happens once.")
            }
            var request = URLRequest(url: self.baseURL.appendingPathComponent("api/status"))
            request.timeoutInterval = 1.5
            URLSession.shared.dataTask(with: request) { data, response, _ in
                guard let data, (response as? HTTPURLResponse)?.statusCode == 200,
                      (try? JSONSerialization.jsonObject(with: data)) != nil else { return }
                DispatchQueue.main.async {
                    guard !self.isReady else { return }
                    self.isReady = true
                    timer.invalidate()
                    self.webView.load(URLRequest(url: self.baseURL))
                }
            }.resume()
        }
    }

    private func showStartup(_ title: String, detail: String) {
        webView.loadHTMLString("""
        <!doctype html><meta name="viewport" content="width=device-width"><style>
        *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f3f6fb;font:16px -apple-system,BlinkMacSystemFont,sans-serif}main{width:min(520px,90vw);padding:42px;border:1px solid #303846;border-radius:22px;background:#131923;text-align:center}i{display:inline-block;width:34px;height:34px;border:3px solid #462126;border-top-color:#ff3038;border-radius:50%;animation:s .8s linear infinite}h1{font-size:25px;margin:20px 0 8px}p{color:#a9b2c0;line-height:1.5;margin:0}@keyframes s{to{transform:rotate(360deg)}}
        </style><main><i></i><h1>\(title)</h1><p>\(detail)</p></main>
        """, baseURL: nil)
    }

    private func showFailure(_ title: String, details: String) {
        let safeTitle = title.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        let safeDetail = details.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("""
        <!doctype html><meta name="viewport" content="width=device-width"><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f3f6fb;font:16px -apple-system,BlinkMacSystemFont,sans-serif}main{width:min(640px,90vw);padding:40px;border:1px solid #303846;border-radius:22px;background:#131923}h1{font-size:24px}p{color:#a9b2c0;line-height:1.5;white-space:pre-wrap}a{color:#ff5258}</style><main><h1>\(safeTitle)</h1><p>\(safeDetail)</p><p>Logs are saved in your Channel Downloader application support folder.</p><a href="https://channel-downloader-updates.vercel.app/">Open the update and help page</a></main>
        """, baseURL: nil)
    }

    private func readRecentLog() -> String {
        guard let contents = try? String(contentsOfFile: backendLog, encoding: .utf8) else { return "" }
        return String(contents.suffix(2200))
    }

    private func stopBackend() {
        pollTimer?.invalidate()
        guard let process = backend, process.isRunning else { return }
        process.terminate()
        process.waitUntilExit()
        backend = nil
    }
}

let application = NSApplication.shared
let delegate = ChannelDownloaderApp()
application.delegate = delegate
application.run()
