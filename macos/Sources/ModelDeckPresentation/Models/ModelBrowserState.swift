import Foundation

public struct ModelBrowserState: Sendable {
    public private(set) var route = ""
    public private(set) var generation = UUID()
    public var entries: [CatalogModel] = []
    public var selected = Set<String>()
    public var query = ""
    public var visible: [CatalogModel] { entries.filter { CatalogModel.matches(query, text: $0.id + " " + $0.name) } }
    public init() {}
    public mutating func begin(route nextRoute: String) -> UUID {
        if route != nextRoute { entries = []; selected = []; query = "" }
        route = nextRoute
        generation = UUID()
        return generation
    }
    public mutating func receive(_ models: [CatalogModel], generation responseGeneration: UUID) -> Bool {
        guard generation == responseGeneration else { return false }
        var seen = Set<String>()
        entries = models.filter { !$0.id.isEmpty && seen.insert($0.id).inserted }.sorted {
            $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
        return true
    }
    public mutating func toggle(_ id: String) {
        if selected.contains(id) { selected.remove(id) } else { selected.insert(id) }
    }
}
