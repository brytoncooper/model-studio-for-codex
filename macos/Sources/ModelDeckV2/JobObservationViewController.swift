import AppKit
import Foundation
import ModelDeckClient
import ModelDeckContracts

/// Observable controller for one tracked job.
///
/// Owns the polling loop and the UI for a single ``jobID``. Renders the
/// generic ``JobSnapshot`` returned by ``engine.v1.jobs.get`` without
/// inspecting plugin-specific output. Cancellation UI keeps showing
/// "requested" until a subsequent poll observes a terminal state.
@MainActor
final class JobObservationViewController: NSViewController {
    /// Polling interval while the job is active. Terminal exit triggers a
    /// final ``get`` call and the controller stops polling itself.
    static let pollInterval: TimeInterval = 0.4

    private let jobID: String
    private let service: JobObservationService
    private let onDismiss: (() -> Void)?

    private let titleLabel = NSTextField(labelWithString: "")
    private let stateLabel = NSTextField(labelWithString: "Pending first observation…")
    private let progressIndicator = NSProgressIndicator()
    private let cancelButton = NSButton(title: "Request cancel", target: nil, action: nil)
    private let outputTextView = NSTextView()
    private let dismissButton = NSButton(title: "Dismiss", target: nil, action: nil)

    private var pollTimer: Timer?
    private var cancelRequested: Bool = false
    private var terminalReached: Bool = false
    private var pollInFlight: Bool = false
    /// Idempotency key for cancel request. Stable per observation so the
    /// engine treats repeated clicks as one logical cancellation.
    private let cancelIdempotencyKey: String = UUID().uuidString.lowercased()

    init(
        jobID: String,
        service: JobObservationService,
        onDismiss: (() -> Void)? = nil
    ) {
        self.jobID = jobID
        self.service = service
        self.onDismiss = onDismiss
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("unsupported") }

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 8
        root.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        root.translatesAutoresizingMaskIntoConstraints = false

        titleLabel.stringValue = "Job \(jobID)"
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        root.addArrangedSubview(titleLabel)

        stateLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(stateLabel)

        progressIndicator.isIndeterminate = false
        progressIndicator.minValue = 0
        progressIndicator.maxValue = 1
        progressIndicator.doubleValue = 0
        progressIndicator.controlSize = .small
        progressIndicator.widthAnchor.constraint(equalToConstant: 220).isActive = true
        root.addArrangedSubview(progressIndicator)

        cancelButton.target = self
        cancelButton.action = #selector(cancelTapped)
        cancelButton.bezelStyle = .rounded
        root.addArrangedSubview(cancelButton)

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        outputTextView.isEditable = false
        outputTextView.isSelectable = true
        outputTextView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        outputTextView.textContainerInset = NSSize(width: 8, height: 8)
        outputTextView.autoresizingMask = [.width]
        scrollView.documentView = outputTextView
        scrollView.heightAnchor.constraint(equalToConstant: 220).isActive = true
        scrollView.widthAnchor.constraint(equalToConstant: 360).isActive = true
        root.addArrangedSubview(scrollView)

        dismissButton.target = self
        dismissButton.action = #selector(dismissTapped)
        dismissButton.bezelStyle = .rounded
        root.addArrangedSubview(dismissButton)

        view = root
        refreshButtonsForCancellationUI()
    }

    override func viewWillDisappear() {
        super.viewWillDisappear()
        stopPolling()
    }

    func beginObservation() {
        startPolling()
    }

    func applySnapshotForTest(_ snapshot: JobSnapshot) {
        apply(snapshot: snapshot)
    }

    /// True when the controller has received a terminal snapshot. Tests
    /// inspect this to assert UI terminalization.
    var isTerminal: Bool { terminalReached }

    @objc func cancelTapped() {
        guard !cancelRequested, !terminalReached else { return }
        cancelRequested = true
        refreshButtonsForCancellationUI()
        let service = self.service
        let jobID = self.jobID
        let idempotencyKey = cancelIdempotencyKey
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                _ = try service.cancelJob(
                    jobID: jobID,
                    idempotencyKey: idempotencyKey
                )
            } catch {
                DispatchQueue.main.async {
                    self?.cancelRequested = false
                    self?.stateLabel.stringValue = "Cancel request failed: \(error)"
                    self?.refreshButtonsForCancellationUI()
                }
            }
        }
    }

    @objc func dismissTapped() {
        stopPolling()
        onDismiss?()
    }

    private func startPolling() {
        pollTimer?.invalidate()
        pollOnce()
        pollTimer = Timer.scheduledTimer(
            withTimeInterval: Self.pollInterval,
            repeats: true
        ) { [weak self] _ in
            Task { @MainActor in self?.pollOnce() }
        }
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func pollOnce() {
        guard !pollInFlight, !terminalReached else { return }
        pollInFlight = true
        let service = self.service
        let jobID = self.jobID
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let snapshot = try service.getJob(jobID: jobID)
                DispatchQueue.main.async {
                    self?.pollInFlight = false
                    self?.apply(snapshot: snapshot)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.pollInFlight = false
                    self?.stateLabel.stringValue = "Polling error: \(error)"
                }
            }
        }
    }

    private func apply(snapshot: JobSnapshot) {
        if let progress = snapshot.progress {
            progressIndicator.doubleValue = progress
        } else {
            progressIndicator.doubleValue = 0
        }
        let stateText: String
        switch snapshot.state {
        case .queued:
            stateText = "Queued"
        case .running:
            stateText = cancelRequested ? "Running (cancel requested)" : "Running"
        case .completed:
            stateText = "Completed"
        case .failed:
            stateText = "Failed"
        case .cancelled:
            stateText = "Cancelled"
        case .interrupted:
            stateText = "Interrupted"
        case .unknown:
            stateText = "Unknown state: \(snapshot.state)"
        }
        stateLabel.stringValue = stateText

        if snapshot.state == .completed {
            renderCompletedOutput(snapshot: snapshot)
            terminalReached = true
            stopPolling()
        } else if snapshot.state == .failed || snapshot.state == .cancelled || snapshot.state == .interrupted {
            renderTerminalEnvelope(snapshot: snapshot)
            terminalReached = true
            stopPolling()
        }
        refreshButtonsForCancellationUI()
    }

    private func renderCompletedOutput(snapshot: JobSnapshot) {
        // We never parse plugin-specific output. Render the persisted envelope
        // verbatim so the user sees whatever the plugin emitted.
        guard snapshot.outputPresent, let output = snapshot.output else {
            outputTextView.string = "No result."
            return
        }
        outputTextView.string = JSONCanonicalText.render(output)
    }

    private func renderTerminalEnvelope(snapshot: JobSnapshot) {
        outputTextView.string = snapshot.outputPresent
            ? JSONCanonicalText.render(snapshot.output ?? .null)
            : "No result."
    }

    private func refreshButtonsForCancellationUI() {
        if terminalReached {
            cancelButton.isEnabled = false
            cancelButton.title = "Cancel"
        } else if cancelRequested {
            cancelButton.isEnabled = false
            cancelButton.title = "Cancel requested…"
        } else {
            cancelButton.isEnabled = true
            cancelButton.title = "Request cancel"
        }
    }
}

/// Canonical text rendering for ``JSONValue`` without plugin-specific
/// parsing. Reuses the same encoder the client uses for outbound frames
/// so users see familiar JSON.
enum JSONCanonicalText {
    static func render(_ value: JSONValue) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let text = String(data: data, encoding: .utf8) else {
            return "<unrenderable output>"
        }
        return text
    }
}
