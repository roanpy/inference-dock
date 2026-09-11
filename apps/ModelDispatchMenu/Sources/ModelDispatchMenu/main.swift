import AppKit
import Foundation
import ServiceManagement

private final class TopAlignedView: NSView {
    override var isFlipped: Bool { true }
}
import Darwin

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

struct PruneModelsResult: Decodable {
    let removed: [String]
}

struct RuntimeMetadata: Decodable {
    let id: String?
    let displayName: String?
    let path: String?
    let version: String?
    let source: String?
    let endpoint: String?
    let mode: String?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case path, version, source, endpoint, mode, status
    }
}

struct ModelEntry: Decodable {
    let id: String
    // Older cores did not expose backend_model; the public id is a safe fallback.
    let backendModel: String?
    let state: String
    let observedState: String?
    let lastConfirmedState: String?
    let lastConfirmedAt: Double?
    let adapter: String
    let displayName: String?
    let contextWindow: Int?
    let maxOutputTokens: Int?
    let reasoningLevels: [String]
    let runtimeSummary: [String]?
    let reportedContextWindow: Int?
    let reportedMaxOutputTokens: Int?
    let reportedRuntimeSummary: [String]
    let resourceGroup: String?
    let exclusiveGroups: [String]
    let keepResident: Bool
    let estimatedMemoryGB: Double?
    let configuredMemoryGB: Double?
    let observedMemoryGB: Double?
    let observedMemorySource: String?
    let capabilities: [String: Bool]
    let platform: RuntimeMetadata?
    let installation: RuntimeMetadata?
    let instance: RuntimeMetadata?
    let serverStatus: String?
    let loaded: Bool?
    let activeRequests: Int?
    let advertise: Bool
    let enabled: Bool
    let available: Bool?
    let catalogStatus: String?
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
        case observedState = "observed_state"
        case lastConfirmedState = "last_confirmed_state"
        case lastConfirmedAt = "last_confirmed_at"
        case adapter
        case displayName = "display_name"
        case contextWindow = "context_window"
        case maxOutputTokens = "max_output_tokens"
        case reasoningLevels = "reasoning_levels"
        case runtimeSummary = "runtime_summary"
        case reportedContextWindow = "reported_context_window"
        case reportedMaxOutputTokens = "reported_max_output_tokens"
        case reportedRuntimeSummary = "reported_runtime_summary"
        case resourceGroup = "resource_group"
        case exclusiveGroups = "exclusive_groups"
        case keepResident = "keep_resident"
        case estimatedMemoryGB = "estimated_memory_gb"
        case configuredMemoryGB = "configured_memory_gb"
        case observedMemoryGB = "observed_memory_gb"
        case observedMemorySource = "observed_memory_source"
        case capabilities
        case platform, installation, instance
        case serverStatus = "server_status"
        case loaded
        case activeRequests = "active_requests"
        case advertise, enabled, available, catalogStatus = "catalog_status", canonical, aliases
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
        observedState = try values.decodeIfPresent(String.self, forKey: .observedState)
        lastConfirmedState = try values.decodeIfPresent(String.self, forKey: .lastConfirmedState)
        lastConfirmedAt = try values.decodeIfPresent(Double.self, forKey: .lastConfirmedAt)
        adapter = try values.decodeIfPresent(String.self, forKey: .adapter) ?? "unknown"
        displayName = try values.decodeIfPresent(String.self, forKey: .displayName)
        contextWindow = try values.decodeIfPresent(Int.self, forKey: .contextWindow)
        maxOutputTokens = try values.decodeIfPresent(Int.self, forKey: .maxOutputTokens)
        reasoningLevels = try values.decodeIfPresent([String].self, forKey: .reasoningLevels) ?? []
        runtimeSummary = try values.decodeIfPresent([String].self, forKey: .runtimeSummary)
        reportedContextWindow = try values.decodeIfPresent(Int.self, forKey: .reportedContextWindow)
        reportedMaxOutputTokens = try values.decodeIfPresent(Int.self, forKey: .reportedMaxOutputTokens)
        reportedRuntimeSummary = try values.decodeIfPresent([String].self, forKey: .reportedRuntimeSummary) ?? []
        resourceGroup = try values.decodeIfPresent(String.self, forKey: .resourceGroup)
        exclusiveGroups = try values.decodeIfPresent([String].self, forKey: .exclusiveGroups) ?? []
        keepResident = try values.decodeIfPresent(Bool.self, forKey: .keepResident) ?? false
        estimatedMemoryGB = try values.decodeIfPresent(Double.self, forKey: .estimatedMemoryGB)
        configuredMemoryGB = try values.decodeIfPresent(Double.self, forKey: .configuredMemoryGB)
        observedMemoryGB = try values.decodeIfPresent(Double.self, forKey: .observedMemoryGB)
        observedMemorySource = try values.decodeIfPresent(String.self, forKey: .observedMemorySource)
        capabilities = try values.decodeIfPresent([String: Bool].self, forKey: .capabilities) ?? [:]
        platform = try values.decodeIfPresent(RuntimeMetadata.self, forKey: .platform)
        installation = try values.decodeIfPresent(RuntimeMetadata.self, forKey: .installation)
        instance = try values.decodeIfPresent(RuntimeMetadata.self, forKey: .instance)
        serverStatus = try values.decodeIfPresent(String.self, forKey: .serverStatus)
        loaded = try values.decodeIfPresent(Bool.self, forKey: .loaded)
        activeRequests = try values.decodeIfPresent(Int.self, forKey: .activeRequests)
        advertise = try values.decodeIfPresent(Bool.self, forKey: .advertise) ?? true
        enabled = try values.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
        available = try values.decodeIfPresent(Bool.self, forKey: .available)
        catalogStatus = try values.decodeIfPresent(String.self, forKey: .catalogStatus)
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
        if available == false { return T("state.unavailable") }
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
        if available == false {
            label = T("state.unavailable")
        } else {
            switch state {
            case "ready", "loaded": label = T("state.ready")
            case "loading": label = T("state.loading")
            case "generating", "busy": label = T("state.generating")
            case "failed": label = T("state.failed")
            case "external", "managed": label = T("state.external")
            case "unknown": label = T("unknown")
            default: label = T("state.unloaded")
            }
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

    func reload() async throws {
        _ = try await send(path: "/v1/reload", method: "POST", body: [:])
    }

    func pruneUnavailableModels() async throws -> PruneModelsResult {
        let data = try await send(path: "/v1/prune-models", method: "POST", body: [:])
        return try JSONDecoder().decode(PruneModelsResult.self, from: data)
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
    private var settingsWindow: NSWindow?
    private weak var settingsStatusLabel: NSTextField?
    private weak var settingsPathField: NSTextField?
    private weak var settingsPluginTemplatePopup: NSPopUpButton?
    private weak var settingsSmartScheduling: NSButton?
    private weak var settingsMemoryField: NSTextField?
    private weak var settingsIdleField: NSTextField?
    private weak var settingsLoginLaunch: NSButton?
    private weak var settingsModelsTextView: NSTextView?
    private weak var settingsPruneModelsButton: NSButton?
    private var settingsResidentChecks: [NSButton] = []
    private var settingsExclusivePopups: [(String, NSPopUpButton)] = []

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

        // Keep a missing model visible only while its runtime is still loaded,
        // regardless of which adapter supplied the status row.
        let menuModels = (latestStatus?.models ?? []).filter {
            ($0.enabled && $0.available != false) || modelIsLoaded($0)
        }
        let canonicalModels = canonicalModels(menuModels)
        let groupedModels = Dictionary(grouping: canonicalModels, by: \.groupName)
        for server in groupedModels.keys.sorted() {
            let submenu = NSMenu()
            for model in groupedModels[server, default: []].sorted(by: { $0.menuTitle < $1.menuTitle }) {
                let item = NSMenuItem(title: selectionTitle(for: model), action: #selector(activateModel(_:)), keyEquivalent: "")
                item.representedObject = model.id
                item.target = self
                item.state = isLoaded(model) ? .on : .off
                let state = displayState(for: model)
                item.isEnabled = model.enabled && model.available != false && !["busy", "generating", "loading", "unloading"].contains(state)
                item.image = statusDot(state)
                item.toolTip = selectionTitle(for: model)
                submenu.addItem(item)
            }
            let serverItem = NSMenuItem(title: server, action: nil, keyEquivalent: "")
            serverItem.submenu = submenu
            menu.addItem(serverItem)
        }
        menu.addItem(.separator())

        let settingsRoot = NSMenuItem(title: T("menu.systemSettings"), action: #selector(openSettingsWindow), keyEquivalent: ",")
        settingsRoot.target = self
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
        let stopCoreItem = NSMenuItem(title: T("menu.stopCore"), action: #selector(stopCoreNow), keyEquivalent: "")
        stopCoreItem.target = self
        stopCoreItem.isEnabled = coreProcess?.isRunning == true
        stopCoreItem.toolTip = T("menu.stopCore.tooltip")
        menu.addItem(stopCoreItem)
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
            let loaded = modelIsLoaded(model)
            let existingLoaded = modelIsLoaded(existing)
            if modelIsActive || (!existingIsActive && (loaded && !existingLoaded || (loaded == existingLoaded && model.id < existing.id))) {
                byBackend[key] = model
            }
        }
        return byBackend.values.sorted { $0.menuTitle < $1.menuTitle }
    }

    private func displayState(for model: ModelEntry) -> String {
        if model.available == false { return "unavailable" }
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
        case "ready", "loaded": return T("state.ready")
        case "loading": return T("state.loading")
        case "unloading": return T("state.unloading")
        case "failed": return T("state.failed")
        case "generating", "busy": return T("state.generating")
        case "unloaded": return T("state.unloaded")
        case "unavailable": return T("state.unavailable")
        case "external", "external-owned", "observe", "http-managed", "managed": return T("state.external")
        default: return T("unknown")
        }
    }

    private func selectionTitle(for model: ModelEntry) -> String {
        let context = model.contextWindow.map { "\($0 / 1024)K" } ?? "?K"
        let vision = model.capabilities["vision"] == true ? " · \(T("capability.vision"))" : ""
        let label: String
        if !model.enabled {
            label = T("state.disabled")
        } else if model.available == false {
            label = T("state.unavailable")
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
        modelIsLoaded(model)
    }

    private func modelIsLoaded(_ model: ModelEntry) -> Bool {
        if let loaded = model.loaded { return loaded }
        return ["ready", "loaded", "generating", "busy"].contains(model.state)
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

    @objc private func openSettingsWindow() {
        if let settingsWindow {
            settingsWindow.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = T("settings.window.title")
        window.isReleasedWhenClosed = false
        let tabs = NSTabView(frame: .zero)
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.general"), view: generalSettingsView()))
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.server"), view: serverSettingsView()))
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.models"), view: modelSettingsView()))
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.policy"), view: policySettingsView()))
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.agents"), view: agentSettingsView()))
        tabs.addTabViewItem(settingsTab(title: T("settings.tab.diagnostics"), view: updateSettingsView()))
        window.contentView = tabs
        settingsWindow = window
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func settingsTab(title: String, view: NSView) -> NSTabViewItem {
        let item = NSTabViewItem(identifier: title)
        item.label = title
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.documentView = view
        view.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            view.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            view.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            view.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor)
        ])
        item.view = scroll
        return item
    }

    private func generalSettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.general.heading"), in: stack)
        let login = NSButton(checkboxWithTitle: T("settings.general.loginLaunch"), target: self, action: #selector(toggleLoginItem))
        if #available(macOS 13.0, *) {
            login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        } else {
            login.isEnabled = false
            login.toolTip = T("menu.requiresMacOS13")
        }
        settingsLoginLaunch = login
        stack.addArrangedSubview(login)
        stack.addArrangedSubview(settingsHint(T("settings.general.loginLaunchDescription")))
        settingsHeading(T("settings.general.scheduling"), in: stack)
        let smart = NSButton(checkboxWithTitle: T("menu.smartScheduling"), target: self, action: #selector(savePolicySettings))
        smart.state = coreSettings?.smartScheduling == true ? .on : .off
        settingsSmartScheduling = smart
        stack.addArrangedSubview(smart)
        let memory = NSTextField(string: coreSettings?.memoryLimitGB.map { String($0) } ?? "")
        memory.placeholderString = T("settings.policy.memoryPlaceholder")
        settingsMemoryField = memory
        stack.addArrangedSubview(labeledField(T("settings.policy.memory"), field: memory))
        let idle = NSTextField(string: coreSettings?.idleUnloadSeconds.map { String($0) } ?? "")
        idle.placeholderString = T("settings.policy.idlePlaceholder")
        settingsIdleField = idle
        stack.addArrangedSubview(labeledField(T("settings.policy.idle"), field: idle))
        stack.addArrangedSubview(settingsButton(T("settings.save"), action: #selector(savePolicySettings)))
        return settingsContainer(stack)
    }

    private func settingsStack() -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 18, left: 20, bottom: 18, right: 20)
        return stack
    }

    /// Adds a section heading with extra space above it so groups stay readable.
    private func settingsHeading(_ text: String, in stack: NSStackView) {
        if let previous = stack.arrangedSubviews.last {
            stack.setCustomSpacing(22, after: previous)
        }
        let heading = settingsLabel(text, bold: true)
        stack.addArrangedSubview(heading)
        stack.setCustomSpacing(6, after: heading)
    }

    /// Secondary, smaller text for descriptions and notices.
    private func settingsHint(_ text: String) -> NSTextField {
        let label = settingsLabel(text)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .secondaryLabelColor
        return label
    }

    private func settingsPreview(_ text: String, height: CGFloat) -> NSScrollView {
        let view = NSTextView()
        view.isEditable = false
        view.drawsBackground = false
        view.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        view.string = text
        view.autoresizingMask = [.width]
        view.frame = NSRect(x: 0, y: 0, width: 640, height: height)
        let scroll = settingsScroll(view)
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: height).isActive = true
        return scroll
    }

    /// Makes a row span the page content width so previews and fields line up.
    private func settingsFullWidth(_ view: NSView, in stack: NSStackView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -40).isActive = true
    }

    private func settingsLabel(_ text: String, bold: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        if bold { label.font = .boldSystemFont(ofSize: 13) }
        label.maximumNumberOfLines = 0
        label.lineBreakMode = .byWordWrapping
        return label
    }

    private func settingsButton(_ title: String, action: Selector?, enabled: Bool = true) -> NSButton {
        let button = NSButton(title: title, target: action == nil ? nil : self, action: action)
        button.isEnabled = enabled
        return button
    }

    private func settingsScroll(_ view: NSView) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = view
        return scroll
    }

    private func presentScrollableAlert(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 600, height: 280))
        scroll.hasVerticalScroller = true
        scroll.borderType = .lineBorder
        let textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.string = text
        textView.autoresizingMask = [.width, .height]
        scroll.documentView = textView
        alert.accessoryView = scroll
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    private func serverSettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.server.heading"), in: stack)
        stack.addArrangedSubview(settingsHint(T("settings.server.template")))
        let template = NSPopUpButton()
        ["ds4", "mlx-serve", "mtplx", "llama-cpp", "ollama", "omlx", "lm-studio", "mlx-lm", "mlx-vlm", "vllm-mlx"].forEach { template.addItem(withTitle: $0) }
        template.identifier = NSUserInterfaceItemIdentifier("inferencedock.plugin.template")
        settingsPluginTemplatePopup = template
        template.translatesAutoresizingMaskIntoConstraints = false
        template.widthAnchor.constraint(equalToConstant: 220).isActive = true
        let endpointPreview = settingsButton(T("settings.server.previewEndpoint"), action: #selector(importEndpointPreview))
        let pathPreview = settingsButton(T("settings.server.previewPath"), action: #selector(importPathPreview))
        let importControls = NSStackView(views: [template, endpointPreview, pathPreview])
        importControls.spacing = 8
        stack.addArrangedSubview(importControls)
        stack.addArrangedSubview(settingsHint(T("settings.server.importNotice")))
        let path = NSTextField(string: latestConfig?.path ?? configPath().path)
        path.placeholderString = T("settings.server.pathPlaceholder")
        settingsPathField = path
        stack.addArrangedSubview(path)
        settingsFullWidth(path, in: stack)
        let pathButtons = NSStackView(views: [
            settingsButton(T("settings.check"), action: #selector(checkSettingsConfig)),
            settingsButton(T("settings.reload"), action: #selector(reloadSettingsConfig)),
            settingsButton(T("settings.reconcile"), action: #selector(reconcileSettings))
        ])
        pathButtons.spacing = 8
        stack.addArrangedSubview(pathButtons)
        let status = settingsHint(T("settings.server.status", latestConfig?.path ?? configPath().path))
        settingsStatusLabel = status
        stack.addArrangedSubview(status)
        settingsHeading(T("settings.server.instancePreview"), in: stack)
        let previewScroll = settingsPreview(modelPreviewText(), height: 130)
        stack.addArrangedSubview(previewScroll)
        settingsFullWidth(previewScroll, in: stack)
        let actions = NSStackView(views: [
            settingsButton(T("settings.save"), action: #selector(saveSettingsConfig), enabled: false),
            settingsButton(T("settings.disable"), action: nil, enabled: false),
            settingsButton(T("settings.delete"), action: nil, enabled: false)
        ])
        actions.spacing = 8
        stack.addArrangedSubview(actions)
        stack.addArrangedSubview(settingsHint(T("settings.server.unsupportedActions")))
        settingsHeading(T("settings.capabilities.heading"), in: stack)
        let capabilityScroll = settingsPreview(capabilityPreviewText(), height: 120)
        stack.addArrangedSubview(capabilityScroll)
        settingsFullWidth(capabilityScroll, in: stack)
        stack.addArrangedSubview(settingsButton(T("settings.refresh"), action: #selector(refreshSettingsData)))
        let container = TopAlignedView()
        container.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.topAnchor.constraint(equalTo: container.topAnchor),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        ])
        return container
    }

    private func policySettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.policy.heading"), in: stack)
        settingsExclusivePopups.removeAll()
        settingsResidentChecks.removeAll()
        stack.addArrangedSubview(settingsLabel(T("settings.policy.exclusiveTitle"), bold: true))
        let models = latestStatus?.models.filter(\.advertise) ?? []
        let adapters = Array(Set(models.map(\.adapter))).sorted()
        let groups = Array(Set(models.flatMap { $0.exclusiveGroups })).sorted()
        for adapter in adapters {
            let popup = NSPopUpButton()
            popup.addItem(withTitle: T("menu.noExclusiveGroup"))
            popup.addItems(withTitles: groups)
            let selected = coreSettings?.adapterPolicies[adapter]?.exclusiveGroups?.first
                ?? models.first(where: { $0.adapter == adapter })?.exclusiveGroups.first
            if let selected, let index = popup.itemTitles.firstIndex(of: selected) { popup.selectItem(at: index) }
            settingsExclusivePopups.append((adapter, popup))
            stack.addArrangedSubview(labeledField(adapter, field: popup))
        }
        stack.addArrangedSubview(settingsHint(T("settings.policy.exclusive")))
        stack.addArrangedSubview(settingsLabel(T("settings.policy.residentTitle"), bold: true))
        for model in models.sorted(by: { $0.id < $1.id }) {
            let check = NSButton(checkboxWithTitle: model.displayName ?? model.id, target: nil, action: nil)
            check.state = model.keepResident ? .on : .off
            check.toolTip = model.id
            settingsResidentChecks.append(check)
            stack.addArrangedSubview(check)
        }
        stack.addArrangedSubview(settingsHint(T("settings.policy.resident")))
        stack.addArrangedSubview(settingsButton(T("settings.save"), action: #selector(savePolicySettings)))
        let container = TopAlignedView()
        container.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor), stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.topAnchor.constraint(equalTo: container.topAnchor), stack.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        ])
        return container
    }

    private func modelSettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.models.heading"), in: stack)
        stack.addArrangedSubview(settingsHint(T("settings.models.description")))
        let scroll = settingsPreview(modelDetailsText(), height: 300)
        settingsModelsTextView = scroll.documentView as? NSTextView
        stack.addArrangedSubview(scroll)
        settingsFullWidth(scroll, in: stack)
        stack.addArrangedSubview(settingsButton(T("settings.refresh"), action: #selector(refreshSettingsData)))
        let missing = unavailableModels()
        let prune = settingsButton(T("settings.models.prune", missing.count), action: #selector(pruneUnavailableModels), enabled: !missing.isEmpty)
        settingsPruneModelsButton = prune
        stack.addArrangedSubview(prune)
        stack.addArrangedSubview(settingsHint(T("settings.models.pruneHint")))
        return settingsContainer(stack)
    }

    /// One form row: fixed label column, fixed control column, shared baseline.
    private func labeledField(_ title: String, field: NSView, labelWidth: CGFloat = 160, fieldWidth: CGFloat = 220) -> NSStackView {
        let label = settingsLabel(title)
        label.alignment = .right
        label.lineBreakMode = .byTruncatingMiddle
        label.toolTip = title
        label.translatesAutoresizingMaskIntoConstraints = false
        label.widthAnchor.constraint(equalToConstant: labelWidth).isActive = true
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(equalToConstant: fieldWidth).isActive = true
        let row = NSStackView(views: [label, field])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 10
        return row
    }

    private func agentSettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.agents.heading"), in: stack)
        stack.addArrangedSubview(settingsHint(T("settings.agents.description")))
        let scroll = settingsPreview(agentPreviewText(), height: 300)
        stack.addArrangedSubview(scroll)
        settingsFullWidth(scroll, in: stack)
        stack.addArrangedSubview(settingsButton(T("settings.agents.validate"), action: #selector(validateAgents)))
        stack.addArrangedSubview(settingsHint(T("settings.agents.unsupported")))
        return settingsContainer(stack)
    }

    private func updateSettingsView() -> NSView {
        let stack = settingsStack()
        settingsHeading(T("settings.updates.heading"), in: stack)
        stack.addArrangedSubview(settingsHint(T("settings.updates.description")))
        stack.addArrangedSubview(settingsButton(T("menu.checkUpdates"), action: #selector(checkUpdates)))
        stack.addArrangedSubview(settingsButton(T("settings.updates.rollback"), action: nil, enabled: false))
        stack.addArrangedSubview(settingsHint(T("settings.updates.rollbackUnsupported")))
        return settingsContainer(stack)
    }

    private func settingsContainer(_ stack: NSStackView) -> NSView {
        let container = TopAlignedView()
        container.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor), stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.topAnchor.constraint(equalTo: container.topAnchor), stack.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        ])
        return container
    }

    private func modelPreviewText() -> String {
        let models = canonicalModels(latestStatus?.models ?? [])
        guard !models.isEmpty else { return T("settings.none") }
        return models.sorted { $0.id < $1.id }.map { "\($0.id) → \($0.backendModel ?? $0.id) [\($0.groupName)]" }.joined(separator: "\n")
    }

    private func modelDetailsText() -> String {
        // This page is the configuration ledger: retain hidden and unavailable
        // rows so a missing local file is visible for recovery.
        let models = latestStatus?.models ?? []
        guard !models.isEmpty else { return T("settings.none") }
        return models.sorted { $0.id < $1.id }.map { model in
            // One line per model: only known facts, so the list stays scannable.
            var head = [model.displayName ?? model.id]
            if let window = model.contextWindow { head.append("\(window / 1024)K") }
            head.append(statusLabel(displayState(for: model)))
            var facts: [String] = []
            if let configured = model.configuredMemoryGB ?? model.estimatedMemoryGB {
                facts.append("\(T("settings.models.configured")) \(String(format: "%.1fGB", configured))")
            }
            if let observed = model.observedMemoryGB {
                let source = model.observedMemorySource.map { " (\($0))" } ?? ""
                facts.append("\(T("settings.models.observed")) \(String(format: "%.1fGB", observed))\(source)")
            }
            if let confirmed = model.lastConfirmedAt {
                let stamp = Date(timeIntervalSince1970: confirmed).formatted(date: .abbreviated, time: .shortened)
                facts.append("\(T("settings.models.confirmed")) \(stamp)")
            }
            return facts.isEmpty ? head.joined(separator: " · ") : "\(head.joined(separator: " · "))\n    \(facts.joined(separator: " · "))"
        }.joined(separator: "\n")
    }

    private func unavailableModels() -> [ModelEntry] {
        (latestStatus?.models ?? []).filter { $0.available == false && !modelIsLoaded($0) }
    }

    private func capabilityPreviewText() -> String {
        let models = canonicalModels(latestStatus?.models ?? [])
        guard !models.isEmpty else { return T("settings.none") }
        return models.sorted { $0.id < $1.id }.map { model in
            let caps = model.capabilities.isEmpty ? T("unknown") : model.capabilities.keys.sorted().map { "\($0)=\(model.capabilities[$0] == true ? T("value.true") : T("value.false"))" }.joined(separator: ", ")
            return "\(model.id): \(caps)"
        }.joined(separator: "\n")
    }

    private func agentPreviewText() -> String {
        let endpoint = client.baseURL.appendingPathComponent("v1").absoluteString
        let models = canonicalModels((latestStatus?.models ?? []).filter { $0.enabled && $0.available != false })
        let preview = models.isEmpty
            ? T("settings.none")
            : models.sorted { $0.id < $1.id }.map { "\($0.id) → \($0.backendModel ?? $0.id) [\($0.groupName)]" }.joined(separator: "\n")
        return "endpoint: \(endpoint)\nmodels:\n\(preview)\n\n\(T("settings.agents.previewOnly"))"
    }

    @objc private func checkSettingsConfig() {
        Task { [weak self] in
            guard let self else { return }
            do {
                let config = try await self.client.config()
                await MainActor.run { self.latestConfig = config; self.settingsStatusLabel?.stringValue = T("settings.server.status", config.path) }
            } catch { self.presentError(error) }
        }
    }

    @objc private func reloadSettingsConfig() {
        Task { [weak self] in
            guard let self else { return }
            do { try await self.client.reload(); self.refresh() } catch { self.presentError(error) }
        }
    }

    @objc private func reconcileSettings() { refreshNow() }
    @objc private func refreshSettingsData() { refresh() }

    @objc private func pruneUnavailableModels() {
        let models = unavailableModels()
        guard !models.isEmpty else { return }
        let alert = NSAlert()
        alert.messageText = T("settings.models.pruneConfirmTitle")
        alert.informativeText = T("settings.models.pruneConfirmBody", models.map { $0.displayName ?? $0.id }.joined(separator: "\n"))
        alert.addButton(withTitle: T("settings.models.pruneAction"))
        alert.addButton(withTitle: T("dialog.cancel"))
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task { [weak self] in
            guard let self else { return }
            do {
                let result = try await self.client.pruneUnavailableModels()
                await MainActor.run {
                    self.presentScrollableAlert(title: T("settings.models.pruneDone"), text: result.removed.joined(separator: "\n"))
                    self.refresh()
                }
            } catch { self.presentError(error) }
        }
    }
    @objc private func saveSettingsConfig() { checkSettingsConfig() }

    @objc private func importEndpointPreview(_ sender: NSButton) {
        guard let template = settingsPluginTemplatePopup,
              let pluginID = template.selectedItem?.title else { return }
        let prompt = NSAlert()
        prompt.messageText = T("settings.server.endpointPrompt")
        let field = NSTextField(string: "http://127.0.0.1:")
        field.frame = NSRect(x: 0, y: 0, width: 320, height: 24)
        prompt.accessoryView = field
        prompt.addButton(withTitle: T("settings.server.preview"))
        prompt.addButton(withTitle: T("dialog.cancel"))
        guard prompt.runModal() == .alertFirstButtonReturn, !field.stringValue.isEmpty else { return }
        runPluginImportPreview(pluginID: pluginID, option: "--endpoint", value: field.stringValue)
    }

    @objc private func importPathPreview(_ sender: NSButton) {
        guard let template = settingsPluginTemplatePopup,
              let pluginID = template.selectedItem?.title else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = T("settings.server.pathPrompt")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let configExtensions = Set(["json", "yaml", "yml", "toml", "plist"])
        let option = url.hasDirectoryPath ? "--source-dir" : (configExtensions.contains(url.pathExtension.lowercased()) ? "--config" : "--executable")
        runPluginImportPreview(pluginID: pluginID, option: option, value: url.path)
    }

    private func runPluginImportPreview(pluginID: String, option: String, value: String) {
        let script = Bundle.main.bundleURL.appendingPathComponent("Contents/scripts/plugin_registry.py")
        let plugins = Bundle.main.bundleURL.appendingPathComponent("Contents/plugins").path
        guard FileManager.default.isReadableFile(atPath: script.path) else {
            presentErrorMessage(T("settings.server.scriptMissing"))
            return
        }
        let worker = Task.detached(priority: .utility) {
            Self.executePluginImportPreview(script: script, plugins: plugins, pluginID: pluginID, option: option, value: value)
        }
        Task { @MainActor [weak self] in
            let result = await worker.value
            self?.presentImportPreview(result.text, success: result.success)
        }
    }

    private static func executePluginImportPreview(script: URL, plugins: String, pluginID: String, option: String, value: String) -> (text: String, success: Bool) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", script.path, "import-instance", "--plugins-dir", plugins, "--id", pluginID, option, value, "--pretty"]
        let stdout = Pipe(); let stderr = Pipe()
        process.standardOutput = stdout; process.standardError = stderr
        var captured = Data(); let lock = NSLock(); let group = DispatchGroup()
        do {
            try process.run()
            group.enter(); DispatchQueue.global(qos: .utility).async { let data = stdout.fileHandleForReading.readDataToEndOfFile(); lock.lock(); captured = data; lock.unlock(); group.leave() }
            group.enter(); DispatchQueue.global(qos: .utility).async { _ = stderr.fileHandleForReading.readDataToEndOfFile(); group.leave() }
            pollProcess(process, timeout: 8)
            _ = group.wait(timeout: .now() + 1)
            return (String(data: captured, encoding: .utf8).map { String($0.prefix(12000)) } ?? "error", process.terminationStatus == 0)
        } catch { return ("error", false) }
    }

    private static func pollProcess(_ process: Process, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        guard process.isRunning else { return }
        process.terminate()
        let grace = Date().addingTimeInterval(0.3)
        while process.isRunning && Date() < grace { Thread.sleep(forTimeInterval: 0.02) }
        if process.isRunning {
            _ = Darwin.kill(process.processIdentifier, SIGKILL)
            let killDeadline = Date().addingTimeInterval(0.5)
            while process.isRunning && Date() < killDeadline { Thread.sleep(forTimeInterval: 0.02) }
        }
    }

    @MainActor
    private func presentImportPreview(_ text: String, success: Bool) {
        presentScrollableAlert(title: T("settings.server.previewTitle"), text: (success ? T("settings.server.notSaved") + "\n" : "") + text)
    }

    private func presentErrorMessage(_ message: String) {
        let alert = NSAlert()
        alert.messageText = T("settings.server.previewTitle")
        alert.informativeText = message
        alert.addButton(withTitle: T("dialog.close"))
        alert.runModal()
    }

    @objc private func validateAgents() {
        let fixedPaths: [(String, [String])] = [
            ("pi", ["~/.pi/agent/models.json", "~/.pi/agent/settings.json"]),
            ("hermes", ["~/.hermes/config.yaml", "~/.hermes/config.json"]),
            ("opencode", ["~/.config/opencode/opencode.json"]),
            ("opencodex", ["~/.opencodex/config.json", "~/.config/opencodex/config.json"]),
            ("zcode", ["~/.zcode/v2/config.json", "~/.config/zcode/config.json"]),
            ("dsh", ["~/.dsh/settings.yaml", "~/.config/dsh/config.json"])
        ]
        let script = Bundle.main.bundleURL.appendingPathComponent("Contents/scripts/agent_config.py")
        let worker = Task.detached(priority: .utility) {
            fixedPaths.map { Self.validateAgentPath(agent: $0.0, candidates: $0.1, script: script) }
        }
        Task { @MainActor [weak self] in
            let rows = await worker.value
            self?.presentAgentValidation(rows)
        }
    }

    private static func validateAgentPath(agent: String, candidates: [String], script: URL) -> String {
        guard let path = candidates.lazy.map({ NSString(string: $0).expandingTildeInPath }).map({ URL(fileURLWithPath: $0) }).first(where: { FileManager.default.isReadableFile(atPath: $0.path) }) else { return "\(agent): skipped" }
        guard FileManager.default.isReadableFile(atPath: script.path) else { return "\(agent): script-missing" }
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/env"); process.arguments = ["python3", script.path, "validate", agent, path.path]
        let stdout = Pipe(); let stderr = Pipe(); process.standardOutput = stdout; process.standardError = stderr
        do {
            try process.run()
            let drain = DispatchGroup()
            drain.enter(); DispatchQueue.global(qos: .utility).async { _ = stdout.fileHandleForReading.readDataToEndOfFile(); drain.leave() }
            drain.enter(); DispatchQueue.global(qos: .utility).async { _ = stderr.fileHandleForReading.readDataToEndOfFile(); drain.leave() }
            pollProcess(process, timeout: 8)
            _ = drain.wait(timeout: .now() + 1)
            return "\(agent): \(process.isRunning ? "timeout" : (process.terminationStatus == 0 ? "pass" : "fail")) (\(path.lastPathComponent))"
        } catch { return "\(agent): fail (\(path.lastPathComponent))" }
    }

    @MainActor
    private func presentAgentValidation(_ rows: [String]) {
        let localized = rows.map { row -> String in
            let parts = row.split(separator: ":", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { return row }
            let agent = parts[0]
            let detail = parts[1].trimmingCharacters(in: .whitespaces)
            let status: String
            if detail.hasPrefix("skipped") { status = T("settings.agents.skipped") }
            else if detail.hasPrefix("script-missing") { status = T("settings.agents.scriptMissing") }
            else if detail.hasPrefix("timeout") { status = T("settings.agents.timeout") }
            else if detail.hasPrefix("pass") { status = T("settings.agents.pass") }
            else { status = T("settings.agents.fail") }
            let file = detail.split(separator: "(", maxSplits: 1).dropFirst().first.map { String($0).replacingOccurrences(of: ")", with: "") }
            return file.map { "\(agent): \(status) (\($0))" } ?? "\(agent): \(status)"
        }
        presentScrollableAlert(title: T("settings.agents.validate"), text: localized.joined(separator: "\n"))
    }

    @objc private func savePolicySettings() {
        var patch: [String: Any] = ["smart_scheduling": settingsSmartScheduling?.state == .on]
        let memoryText = settingsMemoryField?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if memoryText.isEmpty {
            patch["memory_limit_gb"] = NSNull()
        } else if let value = Double(memoryText), value.isFinite, value > 0 {
            patch["memory_limit_gb"] = value
        }
        let idleText = settingsIdleField?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if idleText.isEmpty {
            patch["idle_unload_seconds"] = NSNull()
        } else if let value = Double(idleText), value.isFinite, value > 0 {
            patch["idle_unload_seconds"] = value
        }
        var adapterPolicies: [String: Any] = [:]
        for (adapter, popup) in settingsExclusivePopups {
            let title = popup.titleOfSelectedItem ?? T("menu.noExclusiveGroup")
            adapterPolicies[adapter] = ["exclusive_groups": title == T("menu.noExclusiveGroup") ? [] : [title]]
        }
        if !adapterPolicies.isEmpty { patch["adapter_policies"] = adapterPolicies }
        var modelPolicies: [String: Any] = [:]
        for check in settingsResidentChecks {
            if let model = check.toolTip { modelPolicies[model] = ["keep_resident": check.state == .on] }
        }
        if !modelPolicies.isEmpty { patch["model_policies"] = modelPolicies }
        updateSettings(patch)
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
                    self.settingsModelsTextView?.string = self.modelDetailsText()
                    let missing = self.unavailableModels()
                    self.settingsPruneModelsButton?.title = T("settings.models.prune", missing.count)
                    self.settingsPruneModelsButton?.isEnabled = !missing.isEmpty
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
        case "unavailable": color = .systemRed
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
            // One continuous path (connector, stem, top, bowl, bottom, stem) keeps
            // the template image a single uniform tone. Two separate strokes used
            // to overlap at the connector, which read as a darker second colour.
            NSColor.labelColor.setStroke()
            let outline = NSBezierPath()
            outline.lineWidth = 2.6
            outline.lineCapStyle = .round
            outline.lineJoinStyle = .round
            outline.move(to: NSPoint(x: 2.0, y: 9.0))
            outline.line(to: NSPoint(x: 5.0, y: 9.0))
            outline.line(to: NSPoint(x: 5.0, y: 3.2))
            outline.line(to: NSPoint(x: 8.2, y: 3.2))
            outline.curve(to: NSPoint(x: 14.8, y: 9.0),
                         controlPoint1: NSPoint(x: 12.0, y: 3.2),
                         controlPoint2: NSPoint(x: 14.8, y: 5.8))
            outline.curve(to: NSPoint(x: 8.2, y: 14.8),
                         controlPoint1: NSPoint(x: 14.8, y: 12.2),
                         controlPoint2: NSPoint(x: 12.0, y: 14.8))
            outline.line(to: NSPoint(x: 5.0, y: 14.8))
            outline.line(to: NSPoint(x: 5.0, y: 9.0))
            outline.stroke()
            return rect.width == 18 && rect.height == 18
        }
        image.isTemplate = true
        return image
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Quitting the menu bar is intentionally independent from stopping the
        // dispatcher. The owned core can continue serving clients headlessly.
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
                    self.presentScrollableAlert(title: T("dialog.discovery.title"), text: discoveryText + "\n\n" + self.discoveryText(discovery))
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
        presentScrollableAlert(title: T("dialog.discovery.candidatesTitle"), text: discoveryText(latestDiscovery))
    }

    @objc private func showImportPreview() {
        guard let latestDiscovery else { return }
        presentScrollableAlert(title: T("dialog.discovery.importTitle"), text: T("dialog.discovery.importBody", discoveryText(latestDiscovery)))
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
        settingsLoginLaunch?.state = SMAppService.mainApp.status == .enabled ? .on : .off
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
        presentScrollableAlert(title: T("dialog.runHistory.title"), text: runHistoryText())
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
        let title = model.displayName ?? model.id
        var lines = [T("details.server", model.serverName ?? model.adapter), T("details.adapter", model.serverID ?? model.adapter), T("details.backend", model.backendModel ?? model.id)]
        lines.append(T("details.source", model.pluginID ?? model.adapter))
        lines.append(T("details.configPath", latestConfig?.path ?? configPath().path))
        lines.append(T("details.state", statusLabel(displayState(for: model))))
        lines.append(T("details.serverStatus", model.serverStatus ?? T("unknown")))
        lines.append(T("details.loaded", model.loaded.map { $0 ? T("value.true") : T("value.false") } ?? T("unknown")))
        if let context = model.contextWindow { lines.append(T("details.context", context / 1024)) }
        if let runtime = model.runtimeSummary, !runtime.isEmpty { lines.append(T("details.runtime", runtime.joined(separator: ", "))) }
        if model.reportedContextWindow != nil || model.reportedMaxOutputTokens != nil || !model.reportedRuntimeSummary.isEmpty {
            lines.append(T("details.reported", model.reportedContextWindow.map(String.init) ?? T("unknown"), model.reportedMaxOutputTokens.map(String.init) ?? T("unknown"), model.reportedRuntimeSummary.isEmpty ? T("unknown") : model.reportedRuntimeSummary.joined(separator: ", ")))
        }
        lines.append(T("details.configured", model.contextWindow.map(String.init) ?? T("unknown"), model.maxOutputTokens.map(String.init) ?? T("unknown"), model.reasoningLevels.isEmpty ? T("unknown") : model.reasoningLevels.joined(separator: ",")))
        lines.append(T("details.capabilitiesConfigured", capabilitySummary(model.capabilities)))
        lines.append(T("details.capabilitiesReported", T("unknown")))
        lines.append(T("details.capabilitiesVerified", T("unknown")))
        lines.append(T("details.effective"))
        if !model.aliases.isEmpty { lines.append(T("details.aliases", model.aliases.joined(separator: ", "))) }
        presentScrollableAlert(title: title, text: lines.joined(separator: "\n"))
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

    @objc private func stopCoreNow() {
        guard let process = coreProcess, process.isRunning else {
            coreProcess = nil
            rebuildMenu()
            return
        }
        process.terminate()
        coreProcess = nil
        latestStatus = nil
        coreSettings = nil
        latestConfig = nil
        latestMetrics = nil
        rebuildMenu()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
