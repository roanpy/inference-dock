import AppKit
import Foundation
import ServiceManagement

private func T(_ key: String, _ arguments: CVarArg...) -> String {
    let localized = Bundle.main.localizedString(forKey: key, value: key, table: "Localizable")
    guard !arguments.isEmpty else { return localized }
    return String(format: localized, locale: Locale.current, arguments: arguments)
}

struct StatusPayload: Decodable {
    let state: String
    let activeModel: String?
    let activeModels: [String]
    let activeRequests: Int
    let lastError: String?
    let latestRequest: RequestMetric?
    let models: [ModelEntry]

    enum CodingKeys: String, CodingKey {
        case state
        case activeModel = "active_model"
        case activeModels = "active_models"
        case activeRequests = "active_requests"
        case lastError = "last_error"
        case latestRequest = "latest_request"
        case models
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        state = try values.decodeIfPresent(String.self, forKey: .state) ?? "unknown"
        activeModel = try values.decodeIfPresent(String.self, forKey: .activeModel)
        activeModels = try values.decodeIfPresent([String].self, forKey: .activeModels)
            ?? activeModel.map { [$0] } ?? []
        activeRequests = try values.decodeIfPresent(Int.self, forKey: .activeRequests) ?? 0
        lastError = try values.decodeIfPresent(String.self, forKey: .lastError)
        latestRequest = try values.decodeIfPresent(RequestMetric.self, forKey: .latestRequest)
        models = try values.decodeIfPresent([ModelEntry].self, forKey: .models) ?? []
    }
}

struct RequestMetric: Decodable {
    let model: String
    let adapter: String?
    let finishReason: String?
    let coldLoadMs: Double?
    let queueMs: Double?
    let ttftMs: Double?
    let tokensPerSecond: Double?
    let prefillTokensPerSecond: Double?
    let cachedTokens: Int?

    enum CodingKeys: String, CodingKey {
        case model
        case adapter
        case finishReason = "finish_reason"
        case coldLoadMs = "cold_load_ms"
        case loadMs = "load_ms"
        case queueMs = "queue_ms"
        case ttftMs = "ttft_ms"
        case tokensPerSecond = "tokens_per_second"
        case prefillTokensPerSecond = "prefill_tokens_per_second"
        case cachedTokens = "cached_tokens"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        model = try values.decodeIfPresent(String.self, forKey: .model) ?? "unknown"
        adapter = try values.decodeIfPresent(String.self, forKey: .adapter)
        finishReason = try values.decodeIfPresent(String.self, forKey: .finishReason)
        coldLoadMs = try values.decodeIfPresent(Double.self, forKey: .coldLoadMs)
            ?? values.decodeIfPresent(Double.self, forKey: .loadMs)
        queueMs = try values.decodeIfPresent(Double.self, forKey: .queueMs)
        ttftMs = try values.decodeIfPresent(Double.self, forKey: .ttftMs)
        tokensPerSecond = try values.decodeIfPresent(Double.self, forKey: .tokensPerSecond)
        prefillTokensPerSecond = try values.decodeIfPresent(Double.self, forKey: .prefillTokensPerSecond)
        cachedTokens = try values.decodeIfPresent(Int.self, forKey: .cachedTokens)
    }
}

struct MetricSeries: Decodable {
    let max: Double?
    let mean: Double?
    let median: Double?

    enum CodingKeys: String, CodingKey { case max, mean, average, median, maxTokens = "max_tokens", meanTokens = "mean_tokens" }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        max = try values.decodeIfPresent(Double.self, forKey: .max)
            ?? values.decodeIfPresent(Double.self, forKey: .maxTokens)
        mean = try values.decodeIfPresent(Double.self, forKey: .mean)
            ?? values.decodeIfPresent(Double.self, forKey: .average)
            ?? values.decodeIfPresent(Double.self, forKey: .meanTokens)
        median = try values.decodeIfPresent(Double.self, forKey: .median)
    }
}

struct ModelMetricSummary: Decodable {
    let model: String
    let windowStart: String?
    let windowEnd: String?
    let totalRequests: Int?
    let successfulRequests: Int?
    let excludedRequests: Int?
    let decode: MetricSeries?
    let prefill: MetricSeries?
    let ttftMs: MetricSeries?
    let cache: MetricSeries?

    enum CodingKeys: String, CodingKey {
        case model
        case windowStart = "window_start"
        case windowEnd = "window_end"
        case totalRequests = "total_requests"
        case successfulRequests = "successful_requests"
        case excludedRequests = "excluded_requests"
        case decode, prefill, cache
        case ttftMs = "ttft_ms"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        model = try values.decodeIfPresent(String.self, forKey: .model) ?? "unknown"
        windowStart = try values.decodeIfPresent(String.self, forKey: .windowStart)
        windowEnd = try values.decodeIfPresent(String.self, forKey: .windowEnd)
        totalRequests = try values.decodeIfPresent(Int.self, forKey: .totalRequests)
        successfulRequests = try values.decodeIfPresent(Int.self, forKey: .successfulRequests)
        excludedRequests = try values.decodeIfPresent(Int.self, forKey: .excludedRequests)
        decode = try values.decodeIfPresent(MetricSeries.self, forKey: .decode)
        prefill = try values.decodeIfPresent(MetricSeries.self, forKey: .prefill)
        ttftMs = try values.decodeIfPresent(MetricSeries.self, forKey: .ttftMs)
        cache = try values.decodeIfPresent(MetricSeries.self, forKey: .cache)
    }
}

struct MetricsPayload: Decodable {
    let requests: [RequestMetric]
    let summaries: [ModelMetricSummary]

    enum CodingKeys: String, CodingKey { case requests, summary, summaries }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        requests = try values.decodeIfPresent([RequestMetric].self, forKey: .requests) ?? []
        if let many = try? values.decode([ModelMetricSummary].self, forKey: .summary) {
            summaries = many
        } else if let one = try? values.decode(ModelMetricSummary.self, forKey: .summary) {
            summaries = [one]
        } else if let many = try? values.decode([ModelMetricSummary].self, forKey: .summaries) {
            summaries = many
        } else {
            summaries = []
        }
    }
}

struct DiscoveryItem: Decodable {
    let id: String
    let source: String?

    enum CodingKeys: String, CodingKey { case id, source }

    init(from decoder: Decoder) throws {
        if let value = try? decoder.singleValueContainer().decode(String.self) {
            id = value
            source = nil
            return
        }
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        source = try values.decodeIfPresent(String.self, forKey: .source)
    }
}

struct DiscoveryResult: Decodable {
    let adapter: String
    let status: String
    let configured: [DiscoveryItem]
    let discovered: [String]
    let newCandidates: [DiscoveryItem]
    let missingCandidates: [DiscoveryItem]

    enum CodingKeys: String, CodingKey {
        case adapter, status, configured, discovered
        case newCandidates = "new_candidates"
        case missingCandidates = "missing_candidates"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        adapter = try values.decodeIfPresent(String.self, forKey: .adapter) ?? "unknown"
        status = try values.decodeIfPresent(String.self, forKey: .status) ?? "unknown"
        configured = try values.decodeIfPresent([DiscoveryItem].self, forKey: .configured) ?? []
        discovered = try values.decodeIfPresent([String].self, forKey: .discovered) ?? []
        newCandidates = try values.decodeIfPresent([DiscoveryItem].self, forKey: .newCandidates) ?? []
        missingCandidates = try values.decodeIfPresent([DiscoveryItem].self, forKey: .missingCandidates) ?? []
    }
}

struct DiscoveryPayload: Decodable {
    let results: [DiscoveryResult]
    let diagnostics: [DiscoveryDiagnostic]

    enum CodingKeys: String, CodingKey { case results, diagnostics }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        results = try values.decodeIfPresent([DiscoveryResult].self, forKey: .results) ?? []
        diagnostics = try values.decodeIfPresent([DiscoveryDiagnostic].self, forKey: .diagnostics) ?? []
    }
}

struct DiscoveryDiagnostic: Decodable {
    let type: String
    let adapter: String?
    let backendModel: String?
    let model: String?
    let alias: String?
    let configuredAdapter: String?
    let models: [String]?

    enum CodingKeys: String, CodingKey {
        case type, adapter, model, alias, models
        case backendModel = "backend_model"
        case configuredAdapter = "configured_adapter"
    }
}

struct ConfigReport: Decodable {
    let path: String
}

struct ModelEntry: Decodable {
    let id: String
    // Older cores did not expose backend_model; the public id is a safe fallback.
    let backendModel: String?
    let state: String
    let adapter: String
    let displayName: String?
    let contextWindow: Int?
    let maxOutputTokens: Int?
    let reasoningLevels: [String]
    let runtimeSummary: [String]?
    let resourceGroup: String?
    let exclusiveGroups: [String]
    let keepResident: Bool
    let estimatedMemoryGB: Double?
    let capabilities: [String: Bool]
    let serverStatus: String?
    let loaded: Bool?
    let activeRequests: Int?
    let advertise: Bool
    let enabled: Bool
    let canonical: String?
    let aliases: [String]
    let serverID: String?
    let serverType: String?
    let serverName: String?
    let menuGroup: String?
    let pluginID: String?

    enum CodingKeys: String, CodingKey {
        case id
        case backendModel = "backend_model"
        case state
        case adapter
        case displayName = "display_name"
        case contextWindow = "context_window"
        case maxOutputTokens = "max_output_tokens"
        case reasoningLevels = "reasoning_levels"
        case runtimeSummary = "runtime_summary"
        case resourceGroup = "resource_group"
        case exclusiveGroups = "exclusive_groups"
        case keepResident = "keep_resident"
        case estimatedMemoryGB = "estimated_memory_gb"
        case capabilities
        case serverStatus = "server_status"
        case loaded
        case activeRequests = "active_requests"
        case advertise, enabled, canonical, aliases
        case serverID = "server_id"
        case serverType = "server_type"
        case serverName = "server_name"
        case menuGroup = "menu_group"
        case pluginID = "plugin_id"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decodeIfPresent(String.self, forKey: .id) ?? "unknown"
        backendModel = try values.decodeIfPresent(String.self, forKey: .backendModel)
        state = try values.decodeIfPresent(String.self, forKey: .state) ?? "unknown"
        adapter = try values.decodeIfPresent(String.self, forKey: .adapter) ?? "unknown"
        displayName = try values.decodeIfPresent(String.self, forKey: .displayName)
        contextWindow = try values.decodeIfPresent(Int.self, forKey: .contextWindow)
        maxOutputTokens = try values.decodeIfPresent(Int.self, forKey: .maxOutputTokens)
        reasoningLevels = try values.decodeIfPresent([String].self, forKey: .reasoningLevels) ?? []
        runtimeSummary = try values.decodeIfPresent([String].self, forKey: .runtimeSummary)
        resourceGroup = try values.decodeIfPresent(String.self, forKey: .resourceGroup)
        exclusiveGroups = try values.decodeIfPresent([String].self, forKey: .exclusiveGroups) ?? []
        keepResident = try values.decodeIfPresent(Bool.self, forKey: .keepResident) ?? false
        estimatedMemoryGB = try values.decodeIfPresent(Double.self, forKey: .estimatedMemoryGB)
        capabilities = try values.decodeIfPresent([String: Bool].self, forKey: .capabilities) ?? [:]
        serverStatus = try values.decodeIfPresent(String.self, forKey: .serverStatus)
        loaded = try values.decodeIfPresent(Bool.self, forKey: .loaded)
        activeRequests = try values.decodeIfPresent(Int.self, forKey: .activeRequests)
        advertise = try values.decodeIfPresent(Bool.self, forKey: .advertise) ?? true
        enabled = try values.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
        canonical = try values.decodeIfPresent(String.self, forKey: .canonical)
        aliases = try values.decodeIfPresent([String].self, forKey: .aliases) ?? []
        serverID = try values.decodeIfPresent(String.self, forKey: .serverID)
        serverType = try values.decodeIfPresent(String.self, forKey: .serverType)
        serverName = try values.decodeIfPresent(String.self, forKey: .serverName)
        menuGroup = try values.decodeIfPresent(String.self, forKey: .menuGroup)
        pluginID = try values.decodeIfPresent(String.self, forKey: .pluginID)
    }

    var groupName: String { menuGroup ?? serverName ?? adapter }

    var menuTitle: String {
        let name = displayName ?? id
        let context = contextWindow.map { "\($0 / 1024)K" } ?? "?K"
        let memory = estimatedMemoryGB.map { String(format: " · %.0fGB", $0) } ?? " · ?GB"
        let vision = capabilities["vision"] == true ? " · \(T("capability.vision"))" : ""
        let thinking = reasoningLevels.isEmpty ? "" : " · \(T("capability.thinking")) \(reasoningLevels.joined(separator: "/"))"
        let runtimeItems = runtimeSummary ?? []
        let runtime = runtimeItems.isEmpty ? "" : " · \(runtimeItems.joined(separator: ", "))"
        return "\(name) [\(stateLabel)] · \(context)\(memory)\(vision)\(thinking)\(runtime)"
    }

    private var stateLabel: String {
        if !enabled { return T("state.disabled") }
        switch state {
        case "ready", "loaded": return T("state.ready")
        case "loading": return T("state.loading")
        case "generating", "busy": return T("state.generating")
        case "failed": return T("state.failed")
        case "external", "managed": return T("state.external")
        case "unknown": return T("unknown")
        default: return T("state.unloaded")
        }
    }

    var selectionTitle: String {
        let context = contextWindow.map { "\($0 / 1024)K" } ?? "?K"
        let vision = capabilities["vision"] == true ? " · \(T("capability.vision"))" : ""
        let label: String
        switch state {
        case "ready", "loaded": label = T("state.ready")
        case "loading": label = T("state.loading")
        case "generating", "busy": label = T("state.generating")
        case "failed": label = T("state.failed")
        case "external", "managed": label = T("state.external")
        case "unknown": label = T("unknown")
        default: label = T("state.unloaded")
        }
        return "\(displayName ?? id) · \(context)\(vision) · [\(label)]"
    }
}

struct PolicySettings: Codable {
    var exclusiveGroups: [String]?
    var keepResident: Bool?
    var estimatedMemoryGB: Double?

    enum CodingKeys: String, CodingKey {
        case exclusiveGroups = "exclusive_groups"
        case keepResident = "keep_resident"
        case estimatedMemoryGB = "estimated_memory_gb"
    }

    var jsonObject: [String: Any] {
        var result: [String: Any] = [:]
        if let exclusiveGroups { result["exclusive_groups"] = exclusiveGroups }
        if let keepResident { result["keep_resident"] = keepResident }
        if let estimatedMemoryGB { result["estimated_memory_gb"] = estimatedMemoryGB }
        return result
    }
}

struct CoreSettings: Codable {
    var smartScheduling: Bool
    var memoryLimitGB: Double?
    var idleUnloadSeconds: Double?
    var adapterPolicies: [String: PolicySettings]
    var modelPolicies: [String: PolicySettings]

    enum CodingKeys: String, CodingKey {
        case smartScheduling = "smart_scheduling"
        case memoryLimitGB = "memory_limit_gb"
        case idleUnloadSeconds = "idle_unload_seconds"
        case adapterPolicies = "adapter_policies"
        case modelPolicies = "model_policies"
    }
}

final class CoreClient {
    let baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    func status() async throws -> StatusPayload {
        let data = try await send(path: "/v1/status", method: "GET")
        return try JSONDecoder().decode(StatusPayload.self, from: data)
    }

    func settings() async throws -> CoreSettings {
        let data = try await send(path: "/v1/settings", method: "GET")
        return try JSONDecoder().decode(CoreSettings.self, from: data)
    }

    func metrics(limit: Int = 100) async throws -> MetricsPayload {
        let data = try await send(path: "/v1/metrics", method: "GET", query: [URLQueryItem(name: "limit", value: String(limit))])
        return try JSONDecoder().decode(MetricsPayload.self, from: data)
    }

    func updateSettings(_ patch: [String: Any]) async throws -> CoreSettings {
        let data = try await send(path: "/v1/settings", method: "POST", body: patch)
        return try JSONDecoder().decode(CoreSettings.self, from: data)
    }

    func activate(_ model: String) async throws {
        _ = try await send(path: "/v1/switch", method: "POST", body: ["model": model])
    }

    func unload(model: String? = nil) async throws {
        _ = try await send(path: "/v1/unload", method: "POST", body: model.map { ["model": $0] } ?? [:])
    }

    func cancel() async throws {
        _ = try await send(path: "/v1/cancel", method: "POST", body: [:])
    }

    func reconcile() async throws -> Data {
        try await send(path: "/v1/reconcile", method: "POST", body: [:])
    }

    func config() async throws -> ConfigReport {
        let data = try await send(path: "/v1/config", method: "GET")
        return try JSONDecoder().decode(ConfigReport.self, from: data)
    }

    private func send(path: String, method: String, body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> Data {
        var url = baseURL.appending(path: path)
        if !query.isEmpty, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.queryItems = query
            url = components.url ?? url
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw NSError(domain: "ModelDispatch", code: 1, userInfo: [NSLocalizedDescriptionKey: T("error.noHTTP", path)])
        }
        guard (200...299).contains(http.statusCode) else {
            throw NSError(domain: "ModelDispatch", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: errorMessage(data: data, status: http.statusCode, path: path)])
        }
        return data
    }

    private func errorMessage(data: Data, status: Int, path: String) -> String {
        if let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = payload["error"] as? [String: Any],
           let message = error["message"] as? String,
           !message.isEmpty {
            return T("error.httpMessage", path, status, message)
        }
        return T("error.http", path, status)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let client = CoreClient(baseURL: URL(string: "http://127.0.0.1:18800")!)
    private var timer: Timer?
    private var latestStatus: StatusPayload?
    private var latestMetrics: MetricsPayload?
    private var latestDiscovery: DiscoveryPayload?
    private var latestConfig: ConfigReport?
    private var coreSettings: CoreSettings?
    private var coreProcess: Process?
    private var lastCoreStart = Date.distantPast
    private var updateCheckInFlight = false
    private var lastUpdateSummary: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem.button?.image = statusIconImage()
        statusItem.button?.imagePosition = .imageOnly
        statusItem.button?.imageScaling = .scaleNone
        statusItem.button?.setAccessibilityLabel(T("app.name"))
        statusItem.button?.toolTip = T("tooltip.active", T("none"))
        ensureCore()
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        let title: String
        if let status = latestStatus {
            title = T("status.summary", statusLabel(status.state), status.activeModels.isEmpty ? T("none") : status.activeModels.joined(separator: ", "), status.activeRequests)
        } else {
            title = T("status.offline")
        }
        let titleItem = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        titleItem.isEnabled = false
        menu.addItem(titleItem)
        if let metric = latestStatus?.latestRequest {
            var parts = [T("metric.latest", metric.model)]
            parts.append(T("metric.ttft", metric.ttftMs.map { String(format: "%.0fms", $0) } ?? T("unknown")))
            parts.append(T("metric.decode", metric.tokensPerSecond.map { String(format: "%.1f tok/s", $0) } ?? T("unknown")))
            parts.append(T("metric.prefill", metric.prefillTokensPerSecond.map { String(format: "%.1f tok/s", $0) } ?? T("unknown")))
            parts.append(T("metric.cache", metric.cachedTokens.map(String.init) ?? T("unknown")))
            let metricItem = NSMenuItem(title: parts.joined(separator: " · "), action: nil, keyEquivalent: "")
            metricItem.isEnabled = false
            menu.addItem(metricItem)
        }
        menu.addItem(.separator())

        let canonicalModels = canonicalModels(latestStatus?.models ?? [])
        let groupedModels = Dictionary(grouping: canonicalModels, by: \.groupName)
        for server in groupedModels.keys.sorted() {
            let submenu = NSMenu()
            for model in groupedModels[server, default: []].sorted(by: { $0.menuTitle < $1.menuTitle }) {
                let item = NSMenuItem(title: selectionTitle(for: model), action: #selector(activateModel(_:)), keyEquivalent: "")
                item.representedObject = model.id
                item.target = self
                item.state = isLoaded(model) ? .on : .off
                let state = displayState(for: model)
                item.isEnabled = model.enabled && !["busy", "generating", "loading", "unloading"].contains(state)
                item.image = statusDot(state)
                item.toolTip = selectionTitle(for: model)
                submenu.addItem(item)
            }
            let serverItem = NSMenuItem(title: server, action: nil, keyEquivalent: "")
            serverItem.submenu = submenu
            menu.addItem(serverItem)
        }
        menu.addItem(.separator())

        let modelMenu = NSMenu()
        let configItem = NSMenuItem(title: T("menu.configPath", latestConfig?.path ?? configPath().path), action: nil, keyEquivalent: "")
        configItem.isEnabled = false
        modelMenu.addItem(configItem)
        let refreshItem = NSMenuItem(title: T("menu.refreshModels"), action: #selector(refreshNow), keyEquivalent: "r")
        refreshItem.target = self
        modelMenu.addItem(refreshItem)
        let discoveryItem = NSMenuItem(title: T("menu.discoveryCandidates"), action: #selector(showDiscoveryCandidates), keyEquivalent: "")
        discoveryItem.target = self
        discoveryItem.isEnabled = latestDiscovery != nil
        modelMenu.addItem(discoveryItem)
        let importItem = NSMenuItem(title: T("menu.importPreview"), action: #selector(showImportPreview), keyEquivalent: "")
        importItem.target = self
        importItem.isEnabled = latestDiscovery != nil
        modelMenu.addItem(importItem)
        modelMenu.addItem(.separator())
        if latestStatus != nil {
            let managementGroups = Dictionary(grouping: canonicalModels, by: \.groupName)
            let availableGroups = Set(canonicalModels.flatMap { $0.exclusiveGroups + [$0.resourceGroup].compactMap { $0 } }).sorted()
            for server in managementGroups.keys.sorted() {
                let submenu = NSMenu()
                let firstModel = managementGroups[server]?.first
                let adapterIDs = Array(Set(managementGroups[server, default: []].map(\.adapter))).sorted()
                let configuredGroups = adapterIDs.compactMap { coreSettings?.adapterPolicies[$0]?.exclusiveGroups }.first
                let currentGroups = configuredGroups ?? firstModel?.exclusiveGroups ?? []
                let groupMenu = NSMenu()
                let none = NSMenuItem(title: T("menu.noExclusiveGroup"), action: #selector(serverPolicy(_:)), keyEquivalent: "")
                none.representedObject = ["adapters": adapterIDs, "exclusive_group": NSNull()]
                none.target = self
                none.state = currentGroups.isEmpty ? .on : .off
                groupMenu.addItem(none)
                for group in availableGroups {
                    let item = NSMenuItem(title: group, action: #selector(serverPolicy(_:)), keyEquivalent: "")
                    item.representedObject = ["adapters": adapterIDs, "exclusive_group": group]
                    item.target = self
                    item.state = currentGroups.contains(group) ? .on : .off
                    groupMenu.addItem(item)
                }
                let groupRoot = NSMenuItem(title: T("menu.exclusiveGroup"), action: nil, keyEquivalent: "")
                groupRoot.submenu = groupMenu
                submenu.addItem(groupRoot)

                let policyMenu = NSMenu()
                for model in managementGroups[server, default: []].sorted(by: { $0.menuTitle < $1.menuTitle }) {
                    let modelMenu = NSMenu()
                    let details = NSMenuItem(title: T("menu.runtimeDetails"), action: #selector(showModelDetails(_:)), keyEquivalent: "")
                    details.representedObject = model.id
                    details.target = self
                    modelMenu.addItem(details)
                    let resident = NSMenuItem(title: T("menu.keepResident"), action: #selector(modelPolicy(_:)), keyEquivalent: "")
                    resident.representedObject = ["id": model.id, "keep_resident": !model.keepResident]
                    resident.target = self
                    resident.state = model.keepResident ? .on : .off
                    modelMenu.addItem(resident)
                    let unload = NSMenuItem(title: T("menu.unloadThisModel"), action: #selector(unloadModel(_:)), keyEquivalent: "")
                    unload.representedObject = model.id
                    unload.target = self
                    unload.isEnabled = isLoaded(model) && (model.activeRequests ?? 0) == 0
                    modelMenu.addItem(unload)
                    let modelRoot = NSMenuItem(title: model.displayName ?? model.id, action: nil, keyEquivalent: "")
                    modelRoot.submenu = modelMenu
                    policyMenu.addItem(modelRoot)
                }
                let policyRoot = NSMenuItem(title: T("menu.modelPolicy"), action: nil, keyEquivalent: "")
                policyRoot.submenu = policyMenu
                submenu.addItem(policyRoot)
                let stop = NSMenuItem(title: T("menu.stopServiceUnavailable"), action: nil, keyEquivalent: "")
                stop.isEnabled = false
                stop.toolTip = T("menu.stopServiceUnavailable.tooltip")
                submenu.addItem(stop)
                let groupItem = NSMenuItem(title: server, action: nil, keyEquivalent: "")
                groupItem.submenu = submenu
                modelMenu.addItem(groupItem)
            }
        }
        let modelRoot = NSMenuItem(title: T("menu.modelManagement"), action: nil, keyEquivalent: "")
        modelRoot.submenu = modelMenu
        menu.addItem(modelRoot)

        let settingsRoot = NSMenuItem(title: T("menu.systemSettings"), action: nil, keyEquivalent: "")
        settingsRoot.submenu = settingsMenu()
        menu.addItem(settingsRoot)
        menu.addItem(.separator())

        let unloadItem = NSMenuItem(title: T("menu.unloadModel"), action: #selector(unloadNow), keyEquivalent: "u")
        unloadItem.target = self
        unloadItem.isEnabled = !(latestStatus?.activeModels.isEmpty ?? true) && latestStatus?.activeRequests == 0
        menu.addItem(unloadItem)

        let cancelItem = NSMenuItem(title: T("menu.cancelRequest"), action: #selector(cancelNow), keyEquivalent: "")
        cancelItem.target = self
        cancelItem.isEnabled = (latestStatus?.activeRequests ?? 0) > 0
        menu.addItem(cancelItem)

        let historyItem = NSMenuItem(title: T("menu.runHistory"), action: #selector(showRunHistory), keyEquivalent: "")
        historyItem.target = self
        menu.addItem(historyItem)

        if let error = latestStatus?.lastError, !error.isEmpty {
            let errorItem = NSMenuItem(title: T("menu.error", error), action: nil, keyEquivalent: "")
            errorItem.isEnabled = false
            menu.addItem(errorItem)
        }

        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: T("menu.quitInterface"), action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
    }

    private func canonicalModels(_ models: [ModelEntry]) -> [ModelEntry] {
        let visible = models.filter(\.advertise)
        var byBackend: [String: ModelEntry] = [:]
        for model in visible {
            let key = "\(model.adapter)\u{0}\(model.canonical ?? model.backendModel ?? model.id)"
            guard let existing = byBackend[key] else {
                byBackend[key] = model
                continue
            }
            // Keep the active/loaded row; otherwise choose the stable public ID.
            let modelIsActive = latestStatus?.activeModels.contains(model.id) == true
            let existingIsActive = latestStatus?.activeModels.contains(existing.id) == true
            if modelIsActive || (!existingIsActive && model.id < existing.id) {
                byBackend[key] = model
            }
        }
        return byBackend.values.sorted { $0.menuTitle < $1.menuTitle }
    }

    private func displayState(for model: ModelEntry) -> String {
        let activeRequests = model.activeRequests ?? (latestStatus?.activeModel == model.id ? latestStatus?.activeRequests ?? 0 : 0)
        if activeRequests > 0 || (latestStatus?.activeModel == model.id && model.state == "ready" && model.loaded != false && (latestStatus?.activeRequests ?? 0) > 0) {
            return "generating"
        }
        if ["external", "observe", "http-managed"].contains(model.serverType) {
            return "external"
        }
        if model.loaded == true && model.activeRequests == 0 { return "loaded" }
        return model.state
    }

    private func statusLabel(_ state: String) -> String {
        switch state {
        case "ready": return T("state.ready")
        case "loading": return T("state.loading")
        case "unloading": return T("state.unloading")
        case "failed": return T("state.failed")
        case "generating", "busy": return T("state.generating")
        case "unloaded": return T("state.unloaded")
        default: return T("unknown")
        }
    }

    private func selectionTitle(for model: ModelEntry) -> String {
        let context = model.contextWindow.map { "\($0 / 1024)K" } ?? "?K"
        let vision = model.capabilities["vision"] == true ? " · \(T("capability.vision"))" : ""
        let label: String
        if !model.enabled {
            label = T("state.disabled")
        } else {
            switch displayState(for: model) {
            case "ready", "loaded": label = T("state.ready")
            case "loading": label = T("state.loading")
            case "generating", "busy": label = T("state.generating")
            case "failed": label = T("state.failed")
            case "external", "managed": label = T("state.external")
            case "unknown": label = T("unknown")
            default: label = T("state.unloaded")
            }
        }
        return "\(model.displayName ?? model.id) · \(context)\(vision) · [\(label)]"
    }

    private func isLoaded(_ model: ModelEntry) -> Bool {
        ["ready", "loaded", "generating", "busy"].contains(displayState(for: model))
    }

    private func settingsMenu() -> NSMenu {
        let menu = NSMenu()
        let login = NSMenuItem(title: T("menu.loginLaunch"), action: #selector(toggleLoginItem), keyEquivalent: "")
        login.target = self
        if #available(macOS 13.0, *) {
            login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        } else {
            login.isEnabled = false
            login.toolTip = T("menu.requiresMacOS13")
        }
        menu.addItem(login)
        let update = NSMenuItem(title: updateCheckInFlight ? T("menu.updateChecking") : T("menu.checkUpdates"), action: #selector(checkUpdates), keyEquivalent: "")
        update.target = self
        update.isEnabled = !updateCheckInFlight
        menu.addItem(update)
        if let lastUpdateSummary {
            let last = NSMenuItem(title: T("menu.lastUpdate", lastUpdateSummary), action: nil, keyEquivalent: "")
            last.isEnabled = false
            menu.addItem(last)
        }
        menu.addItem(.separator())
        let smart = NSMenuItem(title: T("menu.smartScheduling"), action: #selector(toggleSmartScheduling), keyEquivalent: "")
        smart.target = self
        smart.state = coreSettings?.smartScheduling == true ? .on : .off
        smart.isEnabled = coreSettings != nil
        smart.toolTip = coreSettings == nil ? T("settings.unavailable") : T("settings.savedImmediately")
        menu.addItem(smart)
        let memory = NSMenuItem(title: T("menu.memoryLimit"), action: nil, keyEquivalent: "")
        memory.toolTip = coreSettings == nil ? T("settings.unavailable") : T("settings.savedImmediately")
        memory.isEnabled = coreSettings != nil
        memory.submenu = memoryMenu()
        menu.addItem(memory)
        let idle = NSMenuItem(title: T("menu.idleUnload"), action: nil, keyEquivalent: "")
        idle.toolTip = coreSettings == nil ? T("settings.unavailable") : T("settings.savedImmediately")
        idle.isEnabled = coreSettings != nil
        idle.submenu = idleMenu()
        menu.addItem(idle)
        return menu
    }

    private func memoryMenu() -> NSMenu {
        let menu = NSMenu()
        for (title, value) in [(T("memory.auto"), nil), ("64 GB", 64), ("96 GB", 96), ("112 GB", 112), ("120 GB", 120)] {
            let item = NSMenuItem(title: title, action: #selector(setMemoryLimit(_:)), keyEquivalent: "")
            item.representedObject = value as Any
            item.target = self
            item.state = coreSettings?.memoryLimitGB == value.map(Double.init) ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    private func idleMenu() -> NSMenu {
        let menu = NSMenu()
        for (title, value) in [(T("idle.off"), nil), (T("idle.5m"), 300), (T("idle.15m"), 900), (T("idle.30m"), 1800)] {
            let item = NSMenuItem(title: title, action: #selector(setIdleUnload(_:)), keyEquivalent: "")
            item.representedObject = value as Any
            item.target = self
            item.state = coreSettings?.idleUnloadSeconds == value.map(Double.init) ? .on : .off
            menu.addItem(item)
        }
        return menu
    }


    private func refresh() {
        Task { [weak self] in
            guard let self else { return }
            do {
                let status = try await self.client.status()
                let settings = try await self.client.settings()
                // Metrics are optional so older cores remain usable; the UI falls
                // back to latest_request when /v1/metrics is not implemented.
                let metrics = try? await self.client.metrics(limit: 100)
                let config = try? await self.client.config()
                await MainActor.run {
                    self.latestStatus = status
                    self.coreSettings = settings
                    self.latestMetrics = metrics
                    self.latestConfig = config
                    self.statusItem.button?.toolTip = T("tooltip.active", status.activeModels.isEmpty ? T("none") : status.activeModels.joined(separator: ", "))
                    self.rebuildMenu()
                }
            } catch {
                await MainActor.run {
                    self.latestStatus = nil
                    self.coreSettings = nil
                    self.latestConfig = nil
                    self.statusItem.button?.toolTip = T("tooltip.offline")
                    self.rebuildMenu()
                    if self.coreProcess?.isRunning == false {
                        self.coreProcess = nil
                    }
                    self.ensureCore()
                }
            }
        }
    }

    private func ensureCore() {
        Task { [weak self] in
            guard let self else { return }
            if await self.coreHealthy() {
                return
            }
            await MainActor.run {
                self.startCoreIfNeeded()
            }
        }
    }

    private func coreHealthy() async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:18800/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.5
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return false
            }
            return payload["service"] as? String == "model-dispatch"
                && payload["dispatcher_run"] is String
        } catch {
            return false
        }
    }

    private func startCoreIfNeeded() {
        guard coreProcess == nil, Date().timeIntervalSince(lastCoreStart) >= 10 else { return }
        lastCoreStart = Date()
        guard let executable = pythonExecutable() else {
            presentError(NSError(domain: "ModelDispatch", code: 4, userInfo: [NSLocalizedDescriptionKey: T("error.python")]))
            return
        }
        let core = Bundle.main.bundleURL
            .appendingPathComponent("Contents/MacOS/model-dispatch-core.py")
        let config = configPath()
        guard FileManager.default.fileExists(atPath: core.path),
              FileManager.default.fileExists(atPath: config.path) else {
            presentError(NSError(domain: "ModelDispatch", code: 5, userInfo: [NSLocalizedDescriptionKey: T("error.bundle")]))
            return
        }
        let process = Process()
        process.executableURL = executable
        process.arguments = [core.path, "--config", config.path]
        process.currentDirectoryURL = Bundle.main.bundleURL.deletingLastPathComponent()
        do {
            try process.run()
            coreProcess = process
        } catch {
            presentError(error)
        }
    }

    private func configPath() -> URL {
        let fileManager = FileManager.default
        if let support = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            let userConfig = support.appendingPathComponent("InferenceDock/engines.yaml")
            if fileManager.fileExists(atPath: userConfig.path) {
                return userConfig
            }
        }
        return Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/model-dispatch-config/engines.yaml")
    }

    private func pythonExecutable() -> URL? {
        if let configured = ProcessInfo.processInfo.environment["MODEL_DISPATCH_PYTHON"], !configured.isEmpty {
            return resolveExecutable(configured)
        }
        return resolveExecutable("python3")
    }

    private func resolveExecutable(_ command: String) -> URL? {
        let fm = FileManager.default
        if command.contains("/") {
            return fm.isExecutableFile(atPath: command) ? URL(fileURLWithPath: command) : nil
        }
        let pathDirectories = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":")
            .map(String.init) + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
        for directory in pathDirectories {
            let candidate = "\(directory)/\(command)"
            if fm.isExecutableFile(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        return nil
    }

    private func statusDot(_ state: String) -> NSImage {
        let color: NSColor
        switch state {
        case "ready", "loaded": color = .systemGreen
        case "loading", "unloading": color = .systemOrange
        case "generating", "busy": color = .systemBlue
        case "external": color = .systemYellow
        case "failed": color = .systemRed
        default: color = .tertiaryLabelColor
        }
        return NSImage(size: NSSize(width: 8, height: 8), flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1)).fill()
            return true
        }
    }

    private func statusIconImage() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            // A thin D outline and one centered connector stay legible at menu-bar size.
            NSColor.labelColor.setStroke()
            let outline = NSBezierPath()
            outline.lineWidth = 1.2
            outline.lineCapStyle = .round
            outline.lineJoinStyle = .round
            outline.move(to: NSPoint(x: 5.0, y: 3.2))
            outline.line(to: NSPoint(x: 8.2, y: 3.2))
            outline.curve(to: NSPoint(x: 14.8, y: 9.0),
                         controlPoint1: NSPoint(x: 12.0, y: 3.2),
                         controlPoint2: NSPoint(x: 14.8, y: 5.8))
            outline.curve(to: NSPoint(x: 8.2, y: 14.8),
                         controlPoint1: NSPoint(x: 14.8, y: 12.2),
                         controlPoint2: NSPoint(x: 12.0, y: 14.8))
            outline.line(to: NSPoint(x: 5.0, y: 14.8))
            outline.close()
            outline.stroke()

            let prongs = NSBezierPath()
            prongs.lineWidth = 1.2
            prongs.lineCapStyle = .round
            prongs.move(to: NSPoint(x: 2.0, y: 9.0))
            prongs.line(to: NSPoint(x: 5.0, y: 9.0))
            prongs.stroke()
            return rect.width == 18 && rect.height == 18
        }
        image.isTemplate = true
        return image
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard let process = coreProcess, process.isRunning else { return }
        process.terminate()
        process.waitUntilExit()
    }

    @objc private func refreshNow() {
        Task { [weak self] in
            guard let self else { return }
            do {
                let data = try await self.client.reconcile()
                let discovery = try JSONDecoder().decode(DiscoveryPayload.self, from: data)
                let added = discovery.results.reduce(0) { $0 + $1.newCandidates.count }
                let missing = discovery.results.reduce(0) { $0 + $1.missingCandidates.count }
                let unavailable = discovery.results.filter { $0.status != "ok" }.count
                let discoveryText = T("dialog.discovery.summary", added, missing, unavailable)
                await MainActor.run {
                    self.latestDiscovery = discovery
                    let alert = NSAlert()
                    alert.messageText = T("dialog.discovery.title")
                    alert.informativeText = discoveryText + "\n\n" + self.discoveryText(discovery)
                    alert.runModal()
                    self.rebuildMenu()
                }
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    @objc private func showDiscoveryCandidates() {
        guard let latestDiscovery else { return }
        let alert = NSAlert()
        alert.messageText = T("dialog.discovery.candidatesTitle")
        alert.informativeText = discoveryText(latestDiscovery)
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    @objc private func showImportPreview() {
        guard let latestDiscovery else { return }
        let alert = NSAlert()
        alert.messageText = T("dialog.discovery.importTitle")
        alert.informativeText = T("dialog.discovery.importBody", discoveryText(latestDiscovery))
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    private func discoveryText(_ payload: DiscoveryPayload) -> String {
        guard !payload.results.isEmpty || !payload.diagnostics.isEmpty else { return T("dialog.discovery.none") }
        var sections = payload.results.sorted { $0.adapter < $1.adapter }.map { result in
            let configured = result.configured.isEmpty ? T("none") : result.configured.map(\.id).joined(separator: ", ")
            let discovered = result.discovered.isEmpty ? T("none") : result.discovered.joined(separator: ", ")
            let candidates = result.newCandidates.isEmpty ? T("none") : result.newCandidates.map { item in
                item.source.map { "\(item.id) [\($0)]" } ?? item.id
            }.joined(separator: ", ")
            let missing = result.missingCandidates.isEmpty ? T("none") : result.missingCandidates.map(\.id).joined(separator: ", ")
            return T("dialog.discovery.server", result.adapter, result.status, configured, discovered, candidates, missing)
        }
        if !payload.diagnostics.isEmpty {
            let diagnostics = payload.diagnostics.map { diagnostic in
                [diagnostic.type, diagnostic.adapter, diagnostic.backendModel, diagnostic.model,
                 diagnostic.alias, diagnostic.configuredAdapter, diagnostic.models?.joined(separator: ", ")]
                    .compactMap { $0 }.joined(separator: " · ")
            }.joined(separator: "\n")
            sections.append(T("dialog.discovery.diagnostics", diagnostics))
        }
        return sections.joined(separator: "\n\n")
    }

    @objc private func toggleLoginItem() {
        guard #available(macOS 13.0, *) else { return }
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            UserDefaults.standard.set(SMAppService.mainApp.status == .enabled, forKey: "loginLaunchPreferred")
        } catch {
            presentError(error)
        }
        rebuildMenu()
    }

    @objc private func toggleSmartScheduling() {
        guard let current = coreSettings else {
            settingsUnavailable()
            return
        }
        updateSettings(["smart_scheduling": !current.smartScheduling])
    }

    @objc private func setMemoryLimit(_ sender: NSMenuItem) {
        guard coreSettings != nil else {
            settingsUnavailable()
            return
        }
        let value: Any = (sender.representedObject as? Int) ?? NSNull()
        updateSettings(["memory_limit_gb": value])
    }

    @objc private func setIdleUnload(_ sender: NSMenuItem) {
        guard coreSettings != nil else {
            settingsUnavailable()
            return
        }
        let value: Any = (sender.representedObject as? Int) ?? NSNull()
        updateSettings(["idle_unload_seconds": value])
    }

    @objc private func serverPolicy(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any],
              let adapters = payload["adapters"] as? [String],
              !adapters.isEmpty,
              let current = coreSettings else {
            settingsUnavailable()
            return
        }
        let exclusiveGroup = payload["exclusive_group"] as? String
        var policies = current.adapterPolicies
        for adapter in adapters {
            var policy = policies[adapter] ?? PolicySettings()
            policy.exclusiveGroups = exclusiveGroup.map { [$0] } ?? []
            policies[adapter] = policy
        }
        updateSettings(["adapter_policies": policies.mapValues { $0.jsonObject }])
    }

    @objc private func modelPolicy(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any],
              let model = payload["id"] as? String,
              let keepResident = payload["keep_resident"] as? Bool,
              let current = coreSettings else {
            settingsUnavailable()
            return
        }
        var policies = current.modelPolicies
        var policy = policies[model] ?? PolicySettings()
        policy.keepResident = keepResident
        policies[model] = policy
        updateSettings(["model_policies": policies.mapValues { $0.jsonObject }])
    }

    private func updateSettings(_ patch: [String: Any]) {
        Task { [weak self] in
            guard let self else { return }
            do {
                let updated = try await self.client.updateSettings(patch)
                await MainActor.run {
                    self.coreSettings = updated
                    self.rebuildMenu()
                }
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    private func settingsUnavailable() {
        presentError(NSError(domain: "ModelDispatch", code: 503, userInfo: [NSLocalizedDescriptionKey: T("error.settings")]))
    }

    @objc private func checkUpdates() {
        guard !updateCheckInFlight else { return }
        guard let python = pythonExecutable() else {
            presentError(NSError(domain: "ModelDispatch", code: 4, userInfo: [NSLocalizedDescriptionKey: T("error.pythonUpdates")]))
            return
        }
        let contents = Bundle.main.bundleURL.appendingPathComponent("Contents")
        let script = contents.appendingPathComponent("scripts/update_monitor.py")
        let plugins = contents.appendingPathComponent("plugins")
        guard FileManager.default.fileExists(atPath: script.path),
              FileManager.default.fileExists(atPath: plugins.path) else {
            presentError(NSError(domain: "ModelDispatch", code: 6, userInfo: [NSLocalizedDescriptionKey: T("error.monitorBundle")]))
            return
        }
        updateCheckInFlight = true
        rebuildMenu()
        Task { [weak self] in
            guard let self else { return }
            do {
                let result = try await self.runUpdateMonitor(python: python, script: script, plugins: plugins)
                let summary = self.updateSummary(data: result.output, exitCode: result.exitCode, stderr: result.errorOutput)
                let details = String(data: result.output, encoding: .utf8) ?? result.errorOutput
                await MainActor.run {
                    self.updateCheckInFlight = false
                    self.lastUpdateSummary = summary
                    self.rebuildMenu()
                    self.presentUpdateReport(summary: summary, details: details)
                }
            } catch {
                await MainActor.run {
                    self.updateCheckInFlight = false
                    self.rebuildMenu()
                    self.presentError(error)
                }
            }
        }
    }

    private func runUpdateMonitor(python: URL, script: URL, plugins: URL) async throws -> (output: Data, errorOutput: String, exitCode: Int32) {
        try await Task.detached {
            let process = Process()
            let output = Pipe()
            let errorOutput = Pipe()
            process.executableURL = python
            process.arguments = [script.path, "--plugins-dir", plugins.path, "--pretty"]
            process.standardOutput = output
            process.standardError = errorOutput
            try process.run()
            let stdout = output.fileHandleForReading.readDataToEndOfFile()
            let stderrData = errorOutput.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (stdout, String(data: stderrData, encoding: .utf8) ?? "", process.terminationStatus)
        }.value
    }

    private func updateSummary(data: Data, exitCode: Int32, stderr: String) -> String {
        guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let results = payload["results"] as? [[String: Any]] else {
            return stderr.isEmpty ? T("update.failed", exitCode) : T("update.failedWithError", stderr)
        }
        var current = 0
        var updates = 0
        var ahead = 0
        var diverged = 0
        var unreachable = 0
        var errors = (payload["manifest_errors"] as? [[String: Any]])?.count ?? 0
        var unknown = 0
        for plugin in results {
            for repo in plugin["repos"] as? [[String: Any]] ?? [] {
                if repo["error"] != nil {
                    errors += 1
                } else if repo["status"] as? String == "unreachable" {
                    unreachable += 1
                } else if repo["relation"] as? String == "behind" {
                    updates += 1
                } else if repo["relation"] as? String == "ahead" {
                    ahead += 1
                } else if repo["relation"] as? String == "diverged" {
                    diverged += 1
                } else if repo["relation"] as? String == "current" {
                    current += 1
                } else {
                    unknown += 1
                }
            }
        }
        return T("update.summary", current, updates, ahead, diverged, unreachable, unknown, errors)
    }

    private func presentUpdateReport(summary: String, details: String) {
        let alert = NSAlert()
        alert.messageText = T("menu.checkUpdates")
        alert.informativeText = summary
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 280))
        scroll.hasVerticalScroller = true
        scroll.borderType = .lineBorder
        let textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.string = details
        textView.autoresizingMask = [.width, .height]
        scroll.documentView = textView
        alert.accessoryView = scroll
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    @objc private func activateModel(_ sender: NSMenuItem) {
        guard let model = sender.representedObject as? String else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.client.activate(model)
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    @objc private func unloadNow() {
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.client.unload()
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    @objc private func unloadModel(_ sender: NSMenuItem) {
        guard let model = sender.representedObject as? String else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.client.unload(model: model)
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    @objc private func cancelNow() {
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.client.cancel()
            } catch {
                self.presentError(error)
            }
            self.refresh()
        }
    }

    @objc private func showRunHistory() {
        let alert = NSAlert()
        alert.messageText = T("dialog.runHistory.title")
        alert.informativeText = runHistoryText()
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    private func runHistoryText() -> String {
        var lines = [T("dialog.history.policy")]
        if let metrics = latestMetrics, !metrics.summaries.isEmpty {
            for summary in metrics.summaries.sorted(by: { $0.model < $1.model }) {
                lines.append(metricSummaryText(summary))
            }
            return lines.joined(separator: "\n\n")
        }
        let requests = latestMetrics?.requests ?? []
        if !requests.isEmpty {
            let grouped = Dictionary(grouping: requests, by: \.model)
            for model in grouped.keys.sorted() {
                lines.append(computedSummaryText(model: model, requests: grouped[model] ?? []))
            }
        } else if let metric = latestStatus?.latestRequest {
            lines.append(T("dialog.history.latest", metric.model))
        } else {
            lines.append(T("dialog.noHistory"))
        }
        return lines.joined(separator: "\n\n")
    }

    private func metricSummaryText(_ summary: ModelMetricSummary) -> String {
        let total = summary.totalRequests.map(String.init) ?? T("unknown")
        let successful = summary.successfulRequests.map(String.init) ?? T("unknown")
        let excluded = summary.excludedRequests.map(String.init) ?? T("unknown")
        let start = summary.windowStart ?? T("unknown")
        let end = summary.windowEnd ?? T("unknown")
        return T("dialog.history.summary", summary.model, successful, total, excluded, start, end,
                  series(summary.decode), series(summary.decode, field: .mean), series(summary.decode, field: .median),
                  series(summary.prefill), series(summary.prefill, field: .mean), series(summary.prefill, field: .median),
                  series(summary.ttftMs, field: .median, suffix: "ms"), series(summary.cache, field: .mean, suffix: "tokens"), series(summary.cache, field: .max, suffix: "tokens"))
    }

    private enum SeriesField { case max, mean, median }

    private func series(_ value: MetricSeries?, field: SeriesField = .max, suffix: String = "tok/s") -> String {
        guard let value else { return T("unknown") }
        let number: Double?
        switch field {
        case .max: number = value.max
        case .mean: number = value.mean
        case .median: number = value.median
        }
        guard let number, number.isFinite else { return T("unknown") }
        return String(format: "%.1f %@", number, suffix)
    }

    private func computedSummaryText(model: String, requests: [RequestMetric]) -> String {
        let completed = requests.filter { completedMetric($0) }
        let rates = completed.compactMap { positive($0.tokensPerSecond) }
        let prefills = completed.compactMap { positive($0.prefillTokensPerSecond) }
        let ttfts = completed.compactMap(\.ttftMs).filter { $0.isFinite && $0 >= 0 }
        let caches = completed.compactMap(\.cachedTokens)
        let excluded = requests.count - completed.count
        return T("dialog.history.computed", model, rates.count, requests.count, excluded,
                  stat(rates, field: .max), stat(rates, field: .mean), stat(rates, field: .median),
                  stat(prefills, field: .max), stat(prefills, field: .mean), stat(prefills, field: .median),
                  stat(ttfts, field: .median, suffix: "ms"), stat(caches.map(Double.init), field: .mean, suffix: "tokens"), stat(caches.map(Double.init), field: .max, suffix: "tokens"))
    }

    private func completedMetric(_ metric: RequestMetric) -> Bool {
        ["stop", "tool_calls", "length", "eos", "complete"].contains(metric.finishReason?.lowercased() ?? "")
    }

    private func positive(_ value: Double?) -> Double? {
        guard let value, value.isFinite, value > 0 else { return nil }
        return value
    }

    private func stat(_ values: [Double], field: SeriesField, suffix: String = "tok/s") -> String {
        guard !values.isEmpty else { return T("unknown") }
        let sorted = values.sorted()
        let number: Double
        switch field {
        case .max: number = sorted.last!
        case .mean: number = sorted.reduce(0, +) / Double(sorted.count)
        case .median:
            let middle = sorted.count / 2
            number = sorted.count.isMultiple(of: 2) ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
        }
        return String(format: "%.1f %@", number, suffix)
    }

    @objc private func showModelDetails(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let model = latestStatus?.models.first(where: { $0.id == id }) else { return }
        let alert = NSAlert()
        alert.messageText = model.displayName ?? model.id
        var lines = [T("details.server", model.serverName ?? model.adapter), T("details.adapter", model.serverID ?? model.adapter), T("details.backend", model.backendModel ?? model.id)]
        lines.append(T("details.source", model.pluginID ?? model.adapter))
        lines.append(T("details.configPath", latestConfig?.path ?? configPath().path))
        lines.append(T("details.state", statusLabel(displayState(for: model))))
        lines.append(T("details.serverStatus", model.serverStatus ?? T("unknown")))
        lines.append(T("details.loaded", model.loaded.map { $0 ? T("value.true") : T("value.false") } ?? T("unknown")))
        if let context = model.contextWindow { lines.append(T("details.context", context / 1024)) }
        if let runtime = model.runtimeSummary, !runtime.isEmpty { lines.append(T("details.runtime", runtime.joined(separator: ", "))) }
        lines.append(T("details.configured", model.contextWindow.map(String.init) ?? T("unknown"), model.maxOutputTokens.map(String.init) ?? T("unknown"), model.reasoningLevels.isEmpty ? T("unknown") : model.reasoningLevels.joined(separator: ",")))
        lines.append(T("details.capabilitiesConfigured", capabilitySummary(model.capabilities)))
        lines.append(T("details.capabilitiesReported", T("unknown")))
        lines.append(T("details.capabilitiesVerified", T("unknown")))
        lines.append(T("details.effective"))
        if !model.aliases.isEmpty { lines.append(T("details.aliases", model.aliases.joined(separator: ", "))) }
        alert.informativeText = lines.joined(separator: "\n")
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    private func capabilitySummary(_ capabilities: [String: Bool]) -> String {
        guard !capabilities.isEmpty else { return T("unknown") }
        return capabilities.keys.sorted().map { key in
            "\(key)=\(capabilities[key] == true ? T("value.true") : T("value.false"))"
        }.joined(separator: ", ")
    }

    private func presentError(_ error: Error) {
        DispatchQueue.main.async {
            let nsError = error as NSError
            // NSApp renders NSDetailedErrorsKey as a collapsed native disclosure.
            let wrapped = NSError(domain: nsError.domain, code: nsError.code, userInfo: [
                NSLocalizedDescriptionKey: T("error.operation"),
                NSLocalizedFailureReasonErrorKey: nsError.localizedDescription,
                NSDetailedErrorsKey: [nsError.localizedDescription],
                NSLocalizedRecoverySuggestionErrorKey: T("error.recovery")
            ])
            NSApp.presentError(wrapped)
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
