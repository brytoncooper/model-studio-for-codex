import Foundation

public struct ProviderPreset: Decodable, Sendable {
    public let id: String
    public let name: String
    public let base_url: String
    public let wire: String
    public let billing: String
    public let billing_note: String
    public let symbol: String
    public let color: String
    public let key_url: String
    public let default_models: [String]
    public static func bundled() -> [ProviderPreset] {
        bundled(bundle: .module)
    }

    public static func bundled(bundle: Bundle) -> [ProviderPreset] {
        struct Document: Decodable { let version: Int; let providers: [ProviderPreset] }
        guard let url = bundle.url(forResource: "provider_presets", withExtension: "json"),
              let bytes = try? Data(contentsOf: url),
              let document = try? JSONDecoder().decode(Document.self, from: bytes), document.version == 1 else { return [] }
        return document.providers
    }

    public static func bundledFromMainBundle() -> [ProviderPreset] {
        bundled(bundle: .main)
    }
}
