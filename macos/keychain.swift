import Foundation
import Security

let arguments = Array(CommandLine.arguments.dropFirst())
let command = arguments.first ?? ""
let service = arguments.count > 1 ? arguments[1] : "local.botrieltor.omniroute"
let account = "api-key"
let base: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account,
]

func fail(_ status: OSStatus) -> Never {
    FileHandle.standardError.write(Data("Keychain error \(status)\n".utf8))
    exit(1)
}

switch command {
case "get":
    var query = base
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    if status == errSecItemNotFound { exit(0) }
    if status != errSecSuccess { fail(status) }
    if let data = item as? Data { FileHandle.standardOutput.write(data) }
case "set":
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty else { exit(2) }
    let update = [kSecValueData as String: data]
    let status = SecItemUpdate(base as CFDictionary, update as CFDictionary)
    if status == errSecItemNotFound {
        var add = base
        add[kSecValueData as String] = data
        let addStatus = SecItemAdd(add as CFDictionary, nil)
        if addStatus != errSecSuccess { fail(addStatus) }
    } else if status != errSecSuccess { fail(status) }
case "delete":
    let status = SecItemDelete(base as CFDictionary)
    if status != errSecSuccess && status != errSecItemNotFound { fail(status) }
default:
    exit(2)
}
