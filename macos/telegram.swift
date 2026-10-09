import Foundation

let allowedMethods = Set(["getMe", "getChat", "getChatMember", "sendMessage", "sendPhoto", "sendMediaGroup", "editMessageText", "editMessageCaption"])
let input = FileHandle.standardInput.readDataToEndOfFile()

guard
    let envelope = try? JSONSerialization.jsonObject(with: input) as? [String: Any],
    let token = envelope["token"] as? String,
    let method = envelope["method"] as? String,
    let parameters = envelope["parameters"] as? [String: Any],
    !token.isEmpty,
    token.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: ":_-")).contains($0) }),
    allowedMethods.contains(method)
else {
    exit(2)
}

let files = envelope["files"] as? [String: String] ?? [:]
let temporaryURL = FileManager.default.temporaryDirectory
    .appendingPathComponent("botrieltor-telegram-\(UUID().uuidString)")
defer { try? FileManager.default.removeItem(at: temporaryURL) }

do {
    try FileManager.default.createDirectory(at: temporaryURL, withIntermediateDirectories: false,
                                            attributes: [.posixPermissions: 0o700])
} catch {
    exit(3)
}

// curl on macOS uses the machine's configured trust chain. The URL containing
// the token is supplied through stdin, so it never appears in process argv.
var config = "silent\nshow-error\nmax-time = 30\nrequest = POST\nheader = \"Accept: application/json\"\n"
if files.isEmpty {
    guard let body = try? JSONSerialization.data(withJSONObject: parameters) else { exit(2) }
    let payloadURL = temporaryURL.appendingPathComponent("body.json")
    do { try body.write(to: payloadURL) } catch { exit(3) }
    config += "header = \"Content-Type: application/json\"\ndata-binary = \"@\(payloadURL.path)\"\n"
} else {
    let safeName = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_-"))
    for (index, item) in parameters.sorted(by: { $0.key < $1.key }).enumerated() {
        guard item.key.unicodeScalars.allSatisfy({ safeName.contains($0) }) else { exit(2) }
        let valueData: Data
        if let string = item.value as? String {
            valueData = Data(string.utf8)
        } else if let data = try? JSONSerialization.data(withJSONObject: item.value) {
            valueData = data
        } else { exit(2) }
        let valueURL = temporaryURL.appendingPathComponent("field-\(index)")
        do { try valueData.write(to: valueURL) } catch { exit(3) }
        config += "form = \"\(item.key)=<\(valueURL.path)\"\n"
    }
    for (name, path) in files.sorted(by: { $0.key < $1.key }) {
        guard name.unicodeScalars.allSatisfy({ safeName.contains($0) }),
              FileManager.default.fileExists(atPath: path),
              !path.contains("\n"), !path.contains("\"") else { exit(2) }
        config += "form = \"\(name)=@\(path)\"\n"
    }
}
config += "url = \"https://api.telegram.org/bot\(token)/\(method)\"\n"

let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
process.arguments = ["--config", "-"]
let configPipe = Pipe()
process.standardInput = configPipe
process.standardOutput = FileHandle.standardOutput
process.standardError = FileHandle.standardError

do {
    try process.run()
    configPipe.fileHandleForWriting.write(Data(config.utf8))
    try? configPipe.fileHandleForWriting.close()
    process.waitUntilExit()
} catch {
    exit(4)
}
exit(process.terminationStatus)
