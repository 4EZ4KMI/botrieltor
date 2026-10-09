import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var outputBuffer = ""
    private var localURL: URL?
    private var shuttingDown = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1190, height: 830),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "BotRieltor — Нячанг"
        window.minSize = NSSize(width: 860, height: 650)
        window.center()
        window.isReleasedWhenClosed = false
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "botrieltor")
        webView = WKWebView(frame: window.contentView!.bounds, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window.contentView!.addSubview(webView)
        showMessage("BotRieltor", "Открываем локальное рабочее пространство…")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startBackend()
    }

    func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Завершить BotRieltor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem(title: "Правка", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(withTitle: "Отменить", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Выбрать всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        let zaloItem = NSMenuItem(title: "Zalo", action: nil, keyEquivalent: "")
        let zaloMenu = NSMenu(title: "Zalo")
        zaloMenu.addItem(withTitle: "Показать управляемое окно Zalo", action: #selector(openZalo), keyEquivalent: "l")
        zaloItem.submenu = zaloMenu
        menu.addItem(zaloItem)
        NSApp.mainMenu = menu
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        // Only our local dashboard can call the native bridge.
        guard message.webView === webView, message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.host == "127.0.0.1",
              message.frameInfo.securityOrigin.port == localURL?.port,
              let body = message.body as? [String: String], body["action"] == "openZalo" else { return }
        openZalo()
    }

    @objc func openZalo() {
        // Route through the dashboard button, which calls /api/zalo and lets
        // ZaloAgentCollector open/focus its managed CDP Chrome profile. Opening
        // chat.zalo.me via NSWorkspace here used the user's ordinary Chrome
        // profile, which is a different session from the one the collector reads.
        webView.evaluateJavaScript("document.querySelector('[data-open-zalo]')?.click()") { _, error in
            if let error {
                NSLog("Could not open the managed Zalo window: %@", error.localizedDescription)
            }
        }
    }

    func showMessage(_ title: String, _ text: String) {
        let safe = text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("<html lang='ru'><body style='font:15px -apple-system;background:#f7f8f6;color:#375442;padding:60px'><h1>\(title)</h1><p>\(safe)</p></body></html>", baseURL: nil)
    }

    func startBackend() {
        guard let resources = Bundle.main.resourceURL,
              let python = Bundle.main.object(forInfoDictionaryKey: "BotRieltorPython") as? String else {
            showMessage("Не удалось запустить", "Не найдены файлы приложения. Выполните повторную сборку.")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "botrieltor.server", "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        process.currentDirectoryURL = resources
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        // Errors stay local and are not sent to Zalo, Telegram or a model provider.
        let dataPath = environment["BOTRIELTOR_DATA_DIR"] ?? NSHomeDirectory() + "/Library/Application Support/BotRieltor"
        try? FileManager.default.createDirectory(atPath: dataPath, withIntermediateDirectories: true)
        let logPath = dataPath + "/backend.log"
        if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath: logPath, contents: nil) }
        if let log = FileHandle(forWritingAtPath: logPath) { log.seekToEndOfFile(); process.standardError = log }
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            guard let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.receive(text) }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.shuttingDown { NSApp.reply(toApplicationShouldTerminate: true) }
                else { self.showMessage("Приложение остановлено", "Закройте BotRieltor и откройте снова. Данные остаются в локальной базе. Подробности — в backend.log.") }
            }
        }
        backend = process
        do { try process.run() }
        catch { showMessage("Не удалось запустить", "Не найден Python для этой сборки. Пересоберите приложение на этом Mac.") }
    }

    func receive(_ text: String) {
        outputBuffer += text
        while let newline = outputBuffer.firstIndex(of: "\n") {
            let line = String(outputBuffer[..<newline])
            outputBuffer = String(outputBuffer[outputBuffer.index(after: newline)...])
            guard let data = line.data(using: .utf8),
                  let event = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { continue }
            if let error = event["error"] { showMessage("Не удалось открыть", error) }
            if let ready = event["ready"], let url = URL(string: ready), url.host == "127.0.0.1", url.scheme == "http" {
                localURL = url
                webView.load(URLRequest(url: url))
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || (url.host == "127.0.0.1" && url.port == localURL?.port && url.scheme == "http") {
            decisionHandler(.allow)
        } else { decisionHandler(.cancel) }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "BotRieltor"
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "BotRieltor"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Подтвердить")
        alert.addButton(withTitle: "Отмена")
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = backend, process.isRunning else { return .terminateNow }
        shuttingDown = true
        process.terminate()
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
