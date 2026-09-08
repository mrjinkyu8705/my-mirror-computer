import {
  ACTIVE_STATE_ORDER,
  CLIPBOARD_IMAGE_MAX_BYTES,
  type ConnectionState,
  type ControlMessage,
  type FileMessage,
  type ImageClipboardMessage,
  type SignalingMessage,
  deriveActiveState,
  transitionConnectionState,
  validateControlMessage,
  validateFileMessage,
  validateImageClipboardMessage,
  validateSignalingMessage,
} from '@mirror/protocol'
import { useEffect, useRef, useState } from 'react'

import { FILE_MAX_BYTES, sha256Hex, streamFileChunks } from './fileUpload'

import {
  CONTROL_PING_INTERVAL_MS,
  controlChannelNeedsRecovery,
} from './controlChannelHealth'

import { describeConnectionIssue } from './connectionErrors'
import {
  type DevelopmentConnectionConfig,
  preserveDevelopmentConfig,
  readDevelopmentConfig,
} from './developmentConfig'
import { isProductionHost, requestSessionConfig } from './productionSession'
import {
  SIGNALING_RECOVERY_INITIAL_DELAY_MS,
  canRecoverSignalingWithoutPeerRestart,
  nextSignalingRecoveryDelay,
} from './signalingRecovery'
import {
  EMPTY_CONNECTION_ROUTE,
  EMPTY_VIDEO_TELEMETRY,
  computeVideoTelemetry,
  readConnectionRoute,
  type ConnectionRouteTelemetry,
  type VideoProfile,
  type VideoStatsSample,
  type VideoTelemetry,
} from './viewerMetrics'

const ICE_GATHERING_TIMEOUT_MS = 10_000
const MAX_CLIPBOARD_ENTRIES = 20
const VIDEO_STATS_INTERVAL_MS = 1_000

interface InboundVideoStatsRecord {
  readonly bytesReceived?: number
  readonly framesDecoded?: number
  readonly framesPerSecond?: number
  readonly frameHeight?: number
  readonly frameWidth?: number
  readonly isRemote?: boolean
  readonly jitter?: number
  readonly kind?: string
  readonly mediaType?: string
  readonly packetsLost?: number
  readonly packetsReceived?: number
  readonly qpSum?: number
  readonly timestamp?: number
  readonly type?: string
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readInboundVideoSample(report: RTCStatsReport): VideoStatsSample | null {
  let selected: InboundVideoStatsRecord | null = null
  report.forEach((entry: RTCStats) => {
    const candidate = entry as InboundVideoStatsRecord
    if (
      selected === null &&
      candidate.type === 'inbound-rtp' &&
      !candidate.isRemote &&
      (candidate.kind === 'video' || candidate.mediaType === 'video')
    ) {
      selected = candidate
    }
  })
  if (!selected) {
    return null
  }
  const stats = selected as InboundVideoStatsRecord
  return {
    bytesReceived: finiteNumber(stats.bytesReceived) ?? 0,
    framesDecoded: finiteNumber(stats.framesDecoded) ?? 0,
    framesPerSecond: finiteNumber(stats.framesPerSecond),
    frameHeight: finiteNumber(stats.frameHeight),
    frameWidth: finiteNumber(stats.frameWidth),
    jitterSeconds: finiteNumber(stats.jitter),
    packetsLost: finiteNumber(stats.packetsLost) ?? 0,
    packetsReceived: finiteNumber(stats.packetsReceived) ?? 0,
    qpSum: finiteNumber(stats.qpSum),
    timestampMs: finiteNumber(stats.timestamp) ?? Date.now(),
  }
}

export interface ClipboardEntry {
  readonly id: number
  readonly receivedAt: number
  readonly text: string
}

export interface ClipboardImageEntry {
  readonly blob: Blob
  readonly id: number
  readonly receivedAt: number
}

interface ClipboardImageUpload {
  cancelled: boolean
  readonly reject: (error: Error) => void
  readonly resolve: () => void
  readonly transferId: string
}

interface IncomingClipboardImage {
  readonly chunks: Uint8Array[]
  received: number
  readonly sha256: string
  readonly size: number
  readonly transferId: string
}

export type FileTransferStatus =
  | 'preparing'
  | 'sending'
  | 'verifying'
  | 'done'
  | 'error'

export interface FileTransferState {
  readonly errorCode: string | null
  readonly fileName: string
  readonly progress: number // 0..1
  readonly status: FileTransferStatus
}

interface FileUpload {
  cancelled: boolean
  onAccept: (() => void) | null
  onDone: (() => void) | null
  onFail: ((code: string) => void) | null
  readonly transferId: string
}

export interface CatalogEntry {
  readonly name: string
  readonly size: number
}

export type FileDownloadStatus = 'downloading' | 'verifying' | 'done' | 'error'

export interface FileDownloadState {
  readonly errorCode: string | null
  readonly fileName: string
  readonly progress: number // 0..1
  readonly status: FileDownloadStatus
}

interface FileDownload {
  readonly transferId: string
  readonly name: string
  size: number
  received: number
  lastPercent: number
  readonly chunks: Uint8Array[]
}

function newTransferId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(18))
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  const encoded = btoa(binary)
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/u, '')
  return `transfer_${encoded}`
}
const VIDEO_PROFILE_TIMEOUT_MS = 10_000
const VIDEO_PROFILE_STABILIZE_TIMEOUT_MS = 4_000
const VIDEO_PROFILE_DIMENSIONS: Readonly<
  Record<VideoProfile, { readonly height: number; readonly width: number }>
> = Object.freeze({
  balanced: { height: 900, width: 1600 },
  high: { height: 1080, width: 1920 },
  low: { height: 720, width: 1280 },
})
export const CONNECTION_ESTABLISHMENT_TIMEOUT_MS = 30_000
// Self-heal an unexpected drop (WebRTC failed / signaling closed) without making
// the user click "reconnect". Bounded so a genuinely dead path stops retrying and
// surfaces the error; the counter resets once a connection reaches 'connected'.
const AUTO_RECONNECT_MAX_ATTEMPTS = 5
const AUTO_RECONNECT_DELAY_MS = 2_000

// Preferred receive codec order: H.264 first (hardware-friendly, matches the
// agent's preference), VP8 as fallback, everything else after.
const PREFERRED_VIDEO_CODECS = ['video/h264', 'video/vp8']

function applyPreferredVideoCodecs(transceiver: RTCRtpTransceiver): void {
  if (typeof RTCRtpReceiver === 'undefined') {
    return
  }
  const capabilities = RTCRtpReceiver.getCapabilities?.('video')
  if (!capabilities || typeof transceiver.setCodecPreferences !== 'function') {
    return
  }

  const rank = (mimeType: string): number => {
    const index = PREFERRED_VIDEO_CODECS.indexOf(mimeType.toLowerCase())
    return index === -1 ? PREFERRED_VIDEO_CODECS.length : index
  }
  const ordered = [...capabilities.codecs].sort(
    (a, b) => rank(a.mimeType) - rank(b.mimeType),
  )

  try {
    transceiver.setCodecPreferences(ordered)
  } catch {
    // Browser rejected the preference list; fall back to default negotiation.
  }
}

type PointerButton = 'left' | 'right' | 'middle'
type KeyAction = 'down' | 'up'

interface RemoteSessionState {
  readonly canConnect: boolean
  readonly connect: () => void
  readonly connectionState: ConnectionState
  readonly controlGranted: boolean
  readonly controlLocked: boolean
  readonly controlPolicyEnabled: boolean
  readonly deviceId: string | null
  readonly disconnect: () => void
  readonly errorMessage: string | null
  readonly errorAction: string | null
  readonly canRetry: boolean
  readonly clipboardEntries: readonly ClipboardEntry[]
  readonly dismissClipboardEntry: (id: number) => void
  readonly clipboardImageEntries: readonly ClipboardImageEntry[]
  readonly dismissClipboardImageEntry: (id: number) => void
  readonly canSendClipboardImage: boolean
  readonly sendRemoteClipboardImage: (image: Blob) => Promise<void>
  readonly fileTransfer: FileTransferState | null
  readonly canSendFiles: boolean
  readonly sendFile: (file: File) => void
  readonly clearFileTransfer: () => void
  readonly downloadableFiles: readonly CatalogEntry[]
  readonly requestFileList: () => void
  readonly downloadFile: (name: string) => void
  readonly fileDownload: FileDownloadState | null
  readonly clearFileDownload: () => void
  readonly isControlActive: boolean
  readonly mediaStream: MediaStream | null
  readonly releaseRemoteInput: () => void
  readonly roundTripTimeMs: number | null
  readonly connectionRoute: ConnectionRouteTelemetry
  readonly videoTelemetry: VideoTelemetry
  readonly setVideoProfile: (profile: VideoProfile) => void
  readonly sendKey: (code: string, action: KeyAction) => void
  readonly setRemoteClipboard: (text: string) => void
  readonly sendPointerButton: (button: PointerButton, action: KeyAction) => void
  readonly sendPointerMove: (x: number, y: number) => void
  readonly sendPointerWheel: (deltaX: number, deltaY: number) => void
  readonly videoProfile: VideoProfile
  readonly videoProfileError: string | null
  readonly videoProfilePending: boolean
  readonly videoProfileStabilizing: boolean
}

function waitForIceGathering(
  peer: RTCPeerConnection,
  timeoutMs = ICE_GATHERING_TIMEOUT_MS,
): Promise<void> {
  if (peer.iceGatheringState === 'complete') {
    return Promise.resolve()
  }

  return new Promise((resolve) => {
    let timer: number | null = null

    const finish = () => {
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
      peer.removeEventListener('icegatheringstatechange', handleStateChange)
      resolve()
    }

    const handleStateChange = () => {
      if (peer.iceGatheringState === 'complete') {
        finish()
      }
    }

    // If gathering stalls (some network/browser conditions never reach
    // 'complete'), proceed with whatever candidates were gathered instead of
    // leaving the viewer stuck in 'negotiating'. Mirrors the host agent's 10s cap.
    timer = window.setTimeout(finish, timeoutMs)
    peer.addEventListener('icegatheringstatechange', handleStateChange)
  })
}

export function useRemoteSession(): RemoteSessionState {
  const config = useRef<DevelopmentConnectionConfig | null>(null)
  // In production the config is fetched per connect via /session/ticket; in dev
  // it comes from the URL/history. Resolved once on first render.
  const isProduction = useRef<boolean | null>(null)
  // True while a production ticket fetch is in flight (before any socket opens),
  // so a second connect() can't start and disconnect() can cancel it.
  const isConnecting = useRef(false)
  const webSocket = useRef<WebSocket | null>(null)
  const peerConnection = useRef<RTCPeerConnection | null>(null)
  const controlChannel = useRef<RTCDataChannel | null>(null)
  const fileChannel = useRef<RTCDataChannel | null>(null)
  const imageClipboardChannel = useRef<RTCDataChannel | null>(null)
  const fileUpload = useRef<FileUpload | null>(null)
  const fileDownload = useRef<FileDownload | null>(null)
  const clipboardImageUpload = useRef<ClipboardImageUpload | null>(null)
  const incomingClipboardImage = useRef<IncomingClipboardImage | null>(null)
  const signalingSequence = useRef(0)
  const controlSequence = useRef(0)
  const pingTimer = useRef<number | null>(null)
  const videoStatsTimer = useRef<number | null>(null)
  const previousVideoStats = useRef<VideoStatsSample | null>(null)
  const missedControlPongs = useRef(0)
  const videoProfileTimer = useRef<number | null>(null)
  const videoProfileStabilizeTimer = useRef<number | null>(null)
  const stabilizingVideoProfile = useRef<VideoProfile | null>(null)
  const connectionEstablishmentTimer = useRef<number | null>(null)
  const signalingRecoveryTimer = useRef<number | null>(null)
  const signalingRecoveryDelay = useRef(SIGNALING_RECOVERY_INITIAL_DELAY_MS)
  const signalingRecoveryInFlight = useRef(false)
  const stateRef = useRef<ConnectionState>('offline')
  const isControlChannelOpen = useRef(false)
  const isVideoTrackReady = useRef(false)
  const grantedControlRef = useRef(false)
  // Monotonic id for the current connect/disconnect cycle. Every event handler
  // captures the cycle it was registered in; a teardown request from a
  // superseded cycle is ignored so a late 'close'/'connectionstatechange' event
  // from an old connection can never tear down a freshly started one.
  const activeCycle = useRef(0)
  // Auto-reconnect bookkeeping: a pending retry timer, how many consecutive
  // retries we've made, and whether the last teardown was the user's choice
  // (in which case we must not reconnect).
  const autoReconnectTimer = useRef<number | null>(null)
  const autoReconnectAttempts = useRef(0)
  const userDisconnected = useRef(false)
  const [connectionState, setConnectionState] =
    useState<ConnectionState>('offline')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [errorAction, setErrorAction] = useState<string | null>(null)
  const [canRetry, setCanRetry] = useState(false)
  const [mediaStream, setMediaStream] = useState<MediaStream | null>(null)
  const [roundTripTimeMs, setRoundTripTimeMs] = useState<number | null>(null)
  const [videoTelemetry, setVideoTelemetry] = useState<VideoTelemetry>(
    EMPTY_VIDEO_TELEMETRY,
  )
  const [connectionRoute, setConnectionRoute] =
    useState<ConnectionRouteTelemetry>(EMPTY_CONNECTION_ROUTE)
  const [controlGranted, setControlGranted] = useState(false)
  const [controlLocked, setControlLocked] = useState(false)
  const [controlPolicyEnabled, setControlPolicyEnabled] = useState(false)
  const [videoProfile, setVideoProfileState] =
    useState<VideoProfile>('balanced')
  const [videoProfileError, setVideoProfileError] = useState<string | null>(null)
  const [videoProfilePending, setVideoProfilePending] = useState(false)
  const [videoProfileStabilizing, setVideoProfileStabilizing] = useState(false)
  const [clipboardEntries, setClipboardEntries] = useState<
    readonly ClipboardEntry[]
  >([])
  const clipboardIdRef = useRef(0)
  const clipboardImageIdRef = useRef(0)
  const [clipboardImageEntries, setClipboardImageEntries] = useState<
    readonly ClipboardImageEntry[]
  >([])
  const [canSendClipboardImage, setCanSendClipboardImage] = useState(false)
  const [fileTransfer, setFileTransfer] = useState<FileTransferState | null>(null)
  const [canSendFiles, setCanSendFiles] = useState(false)
  const [downloadableFiles, setDownloadableFiles] = useState<
    readonly CatalogEntry[]
  >([])
  const [fileDownloadState, setFileDownloadState] =
    useState<FileDownloadState | null>(null)

  if (isProduction.current === null) {
    isProduction.current = isProductionHost()
  }
  if (config.current === null && !isProduction.current) {
    config.current = readDevelopmentConfig()
  }

  function nextSignalingSequence(): number {
    signalingSequence.current += 1
    return signalingSequence.current
  }

  function moveTo(next: ConnectionState): void {
    const transitioned = transitionConnectionState(stateRef.current, next)
    stateRef.current = transitioned
    setConnectionState(transitioned)
  }

  function finishOffline(): void {
    if (stateRef.current !== 'offline' && stateRef.current !== 'closing') {
      moveTo('closing')
    }
    if (stateRef.current === 'closing') {
      moveTo('offline')
    }
  }

  function updateActiveState(): void {
    const currentIndex = ACTIVE_STATE_ORDER.indexOf(
      stateRef.current as (typeof ACTIVE_STATE_ORDER)[number],
    )
    if (currentIndex === -1) {
      // Not in an active phase (e.g. closing/offline); readiness flags no longer
      // drive transitions.
      return
    }

    const target = deriveActiveState({
      isControlChannelOpen: isControlChannelOpen.current,
      isVideoTrackReady: isVideoTrackReady.current,
    })
    const targetIndex = ACTIVE_STATE_ORDER.indexOf(target)

    // Advance monotonically one legal step at a time toward the derived target.
    // Order-independent: whichever of track/channel becomes ready first, the
    // same readiness flags always yield the same target.
    for (let index = currentIndex + 1; index <= targetIndex; index += 1) {
      const nextState = ACTIVE_STATE_ORDER[index]
      if (nextState) {
        moveTo(nextState)
      }
    }
  }

  function nextControlSequence(): number {
    controlSequence.current += 1
    return controlSequence.current
  }

  function sendSignaling(message: SignalingMessage): void {
    if (webSocket.current?.readyState !== WebSocket.OPEN) {
      throw new Error('시그널링 WebSocket이 열려 있지 않습니다.')
    }
    webSocket.current.send(JSON.stringify(message))
  }

  function stopPing(): void {
    if (pingTimer.current !== null) {
      window.clearInterval(pingTimer.current)
      pingTimer.current = null
    }
    missedControlPongs.current = 0
  }

  function stopVideoStats(reset = true): void {
    if (videoStatsTimer.current !== null) {
      window.clearInterval(videoStatsTimer.current)
      videoStatsTimer.current = null
    }
    previousVideoStats.current = null
    if (reset) {
      setVideoTelemetry(EMPTY_VIDEO_TELEMETRY)
      setConnectionRoute(EMPTY_CONNECTION_ROUTE)
    }
  }

  function clearVideoProfileStabilizing(): void {
    if (videoProfileStabilizeTimer.current !== null) {
      window.clearTimeout(videoProfileStabilizeTimer.current)
      videoProfileStabilizeTimer.current = null
    }
    stabilizingVideoProfile.current = null
    setVideoProfileStabilizing(false)
  }

  function finishVideoProfileStabilizingIfDecoded(
    telemetry: VideoTelemetry,
  ): void {
    const profile = stabilizingVideoProfile.current
    if (!profile) {
      return
    }
    const expected = VIDEO_PROFILE_DIMENSIONS[profile]
    if (
      telemetry.frameWidth === expected.width &&
      telemetry.frameHeight === expected.height
    ) {
      clearVideoProfileStabilizing()
    }
  }

  function sendReceiverReport(
    channel: RTCDataChannel,
    telemetry: VideoTelemetry,
    route: ConnectionRouteTelemetry,
  ): void {
    const activeConfig = config.current
    if (
      !activeConfig ||
      channel.readyState !== 'open' ||
      telemetry.receivedBitrateMbps === null ||
      (telemetry.packetLossPercent === null && telemetry.jitterMs === null)
    ) {
      return
    }
    const message: ControlMessage = {
      data: {
        jitterMs: telemetry.jitterMs,
        packetLossPercent: telemetry.packetLossPercent,
        receivedBitrateMbps: telemetry.receivedBitrateMbps,
        route: route.route,
      },
      event: 'video.receiver-report',
      sequence: nextControlSequence(),
      sessionId: activeConfig.sessionId,
      timestamp: Date.now(),
      version: 1,
    }
    try {
      channel.send(JSON.stringify(message))
    } catch {
      // The heartbeat watchdog owns channel recovery. Telemetry is best-effort
      // and must never tear down an otherwise usable media path by itself.
    }
  }

  async function sampleVideoStats(
    peer: RTCPeerConnection,
    cycle: number,
  ): Promise<void> {
    if (
      cycle !== activeCycle.current ||
      peerConnection.current !== peer ||
      typeof peer.getStats !== 'function'
    ) {
      return
    }
    try {
      const report = await peer.getStats()
      const sample = readInboundVideoSample(report)
      if (
        sample === null ||
        cycle !== activeCycle.current ||
        peerConnection.current !== peer
      ) {
        return
      }
      const telemetry = computeVideoTelemetry(previousVideoStats.current, sample)
      const route = readConnectionRoute(report)
      setVideoTelemetry(telemetry)
      setConnectionRoute(route)
      finishVideoProfileStabilizingIfDecoded(telemetry)
      const channel = controlChannel.current
      if (channel) {
        sendReceiverReport(channel, telemetry, route)
      }
      previousVideoStats.current = sample
    } catch {
      // Telemetry is diagnostic only; a browser stats failure must not disturb
      // a healthy media/control session.
    }
  }

  function startVideoStats(peer: RTCPeerConnection, cycle: number): void {
    stopVideoStats(false)
    void sampleVideoStats(peer, cycle)
    videoStatsTimer.current = window.setInterval(() => {
      void sampleVideoStats(peer, cycle)
    }, VIDEO_STATS_INTERVAL_MS)
  }

  function releaseResources(): void {
    isConnecting.current = false
    cancelAutoReconnect()
    cancelSignalingRecovery()
    stopPing()
    stopVideoStats()
    clearConnectionEstablishmentTimer()
    clearVideoProfileTimer()
    clearVideoProfileStabilizing()
    controlChannel.current?.close()
    controlChannel.current = null
    if (clipboardImageUpload.current) {
      clipboardImageUpload.current.cancelled = true
      clipboardImageUpload.current.reject(new Error('DISCONNECTED'))
      clipboardImageUpload.current = null
    }
    incomingClipboardImage.current = null
    imageClipboardChannel.current?.close()
    imageClipboardChannel.current = null
    setCanSendClipboardImage(false)
    // Fail any in-flight upload so its awaiting promise settles, then drop the
    // channel. The final error state stays visible in the UI.
    if (fileUpload.current) {
      fileUpload.current.cancelled = true
      fileUpload.current.onFail?.('DISCONNECTED')
      fileUpload.current = null
    }
    // A download in flight loses its channel; surface it and drop the buffer so
    // partial bytes are never assembled into a file.
    if (fileDownload.current) {
      const name = fileDownload.current.name
      fileDownload.current = null
      setFileDownloadState({
        errorCode: 'DISCONNECTED',
        fileName: name,
        progress: 0,
        status: 'error',
      })
    }
    setDownloadableFiles([])
    fileChannel.current?.close()
    fileChannel.current = null
    setCanSendFiles(false)
    peerConnection.current?.close()
    peerConnection.current = null
    webSocket.current?.close(1000, 'Viewer closed')
    webSocket.current = null
    setMediaStream(null)
    setRoundTripTimeMs(null)
    isControlChannelOpen.current = false
    isVideoTrackReady.current = false
    grantedControlRef.current = false
    setControlGranted(false)
    setVideoProfilePending(false)
    setVideoProfileError(null)
  }

  function clearVideoProfileTimer(): void {
    if (videoProfileTimer.current !== null) {
      window.clearTimeout(videoProfileTimer.current)
      videoProfileTimer.current = null
    }
  }

  function clearConnectionEstablishmentTimer(): void {
    if (connectionEstablishmentTimer.current !== null) {
      window.clearTimeout(connectionEstablishmentTimer.current)
      connectionEstablishmentTimer.current = null
    }
  }

  function startConnectionEstablishmentTimer(cycle: number): void {
    clearConnectionEstablishmentTimer()
    connectionEstablishmentTimer.current = window.setTimeout(() => {
      connectionEstablishmentTimer.current = null
      if (cycle !== activeCycle.current || userDisconnected.current) {
        return
      }
      const peer = peerConnection.current
      if (
        peer?.connectionState === 'connected' &&
        isVideoTrackReady.current
      ) {
        return
      }
      applyConnectionIssue('CONNECTION_TIMEOUT')
      teardownForCycle(cycle)
      scheduleAutoReconnect()
    }, CONNECTION_ESTABLISHMENT_TIMEOUT_MS)
  }

  function applyConnectionIssue(code: string, retryable?: boolean): void {
    const issue = describeConnectionIssue(code, retryable)
    setErrorMessage(issue.message)
    setErrorAction(issue.action)
    setCanRetry(issue.retryable)
  }

  function cancelAutoReconnect(): void {
    if (autoReconnectTimer.current !== null) {
      window.clearTimeout(autoReconnectTimer.current)
      autoReconnectTimer.current = null
    }
  }

  function canPreservePeerDuringSignalingRecovery(): boolean {
    return canRecoverSignalingWithoutPeerRestart(
      stateRef.current,
      peerConnection.current?.connectionState ?? null,
      controlChannel.current?.readyState ?? null,
    )
  }

  function cancelSignalingRecovery(): void {
    if (signalingRecoveryTimer.current !== null) {
      window.clearTimeout(signalingRecoveryTimer.current)
      signalingRecoveryTimer.current = null
    }
    signalingRecoveryInFlight.current = false
    signalingRecoveryDelay.current = SIGNALING_RECOVERY_INITIAL_DELAY_MS
  }

  function scheduleSignalingRecovery(cycle: number): void {
    if (
      cycle !== activeCycle.current ||
      userDisconnected.current ||
      signalingRecoveryTimer.current !== null ||
      signalingRecoveryInFlight.current ||
      !canPreservePeerDuringSignalingRecovery()
    ) {
      return
    }

    const delay = signalingRecoveryDelay.current
    signalingRecoveryDelay.current = nextSignalingRecoveryDelay(delay)
    signalingRecoveryTimer.current = window.setTimeout(() => {
      signalingRecoveryTimer.current = null
      if (
        cycle !== activeCycle.current ||
        userDisconnected.current ||
        !canPreservePeerDuringSignalingRecovery()
      ) {
        return
      }

      const activeConfig = config.current
      if (!activeConfig) {
        return
      }
      if (!isProduction.current) {
        startConnection(activeConfig, cycle, true)
        return
      }

      signalingRecoveryInFlight.current = true
      void requestSessionConfig('control', activeConfig.sessionId)
        .then((fetched) => {
          signalingRecoveryInFlight.current = false
          if (
            cycle !== activeCycle.current ||
            userDisconnected.current ||
            !canPreservePeerDuringSignalingRecovery()
          ) {
            return
          }
          startConnection(fetched, cycle, true)
        })
        .catch(() => {
          signalingRecoveryInFlight.current = false
          scheduleSignalingRecovery(cycle)
        })
    }, delay)
  }

  /**
   * Schedule one auto-reconnect after an unexpected drop. No-op when the user
   * closed the session, when a retry is already pending (so the WebRTC-failed and
   * socket-close events for the same drop count once), or once the attempt budget
   * is spent (the error stays visible for a manual retry).
   */
  function scheduleAutoReconnect(): void {
    if (
      userDisconnected.current ||
      autoReconnectTimer.current !== null ||
      autoReconnectAttempts.current >= AUTO_RECONNECT_MAX_ATTEMPTS
    ) {
      return
    }
    autoReconnectAttempts.current += 1
    autoReconnectTimer.current = window.setTimeout(() => {
      autoReconnectTimer.current = null
      if (userDisconnected.current || stateRef.current !== 'offline') {
        return
      }
      connectInternal(true)
    }, AUTO_RECONNECT_DELAY_MS)
  }

  function teardownForCycle(cycle: number): void {
    // Ignore teardown requests belonging to a superseded connection cycle.
    if (cycle !== activeCycle.current) {
      return
    }
    // "offline" can still have a production ticket request or WebSocket
    // handshake in flight because those happen before the first state
    // transition. Tear those resources down; only a truly idle offline cycle is
    // a no-op.
    if (
      stateRef.current === 'offline' &&
      !isConnecting.current &&
      webSocket.current === null &&
      peerConnection.current === null
    ) {
      return
    }
    // Advance the cycle so any other pending handler for this same cycle becomes
    // stale — releaseResources()/finishOffline() therefore run exactly once.
    activeCycle.current += 1

    const activeConfig = config.current
    if (activeConfig && webSocket.current?.readyState === WebSocket.OPEN) {
      try {
        sendSignaling({
          payload: { reason: 'USER_REQUEST' },
          sequence: nextSignalingSequence(),
          sessionId: activeConfig.sessionId,
          type: 'session.close',
          version: 1,
        })
      } catch {
        // Socket already closing; nothing to notify.
      }
    }

    if (stateRef.current !== 'offline') {
      // Move through 'closing' unless already there.
      if (stateRef.current !== 'closing') {
        moveTo('closing')
      }
    }
    releaseResources()
    finishOffline()
  }

  function disconnect(): void {
    // A user-initiated close must never trigger auto-reconnect.
    userDisconnected.current = true
    autoReconnectAttempts.current = 0
    cancelAutoReconnect()
    teardownForCycle(activeCycle.current)
  }

  function recoverControlChannel(channel: RTCDataChannel, cycle: number): void {
    if (
      cycle !== activeCycle.current ||
      controlChannel.current !== channel ||
      userDisconnected.current
    ) {
      return
    }

    isControlChannelOpen.current = false
    stopPing()
    setRoundTripTimeMs(null)
    // Video RTP and control SCTP can fail independently. Re-negotiate the whole
    // peer because the existing signaling protocol has no safe in-place channel
    // replacement, then reuse the normal bounded reconnect path.
    teardownForCycle(cycle)
    scheduleAutoReconnect()
  }

  function startControlPing(channel: RTCDataChannel, cycle: number): void {
    const activeConfig = config.current
    if (!activeConfig) {
      return
    }

    const sendPing = () => {
      if (
        controlChannelNeedsRecovery(
          channel.readyState,
          missedControlPongs.current,
        )
      ) {
        recoverControlChannel(channel, cycle)
        return
      }
      const message: ControlMessage = {
        data: {},
        event: 'session.ping',
        sequence: nextControlSequence(),
        sessionId: activeConfig.sessionId,
        timestamp: Date.now(),
        version: 1,
      }
      missedControlPongs.current += 1
      try {
        channel.send(JSON.stringify(message))
      } catch {
        recoverControlChannel(channel, cycle)
      }
    }

    missedControlPongs.current = 0
    sendPing()
    pingTimer.current = window.setInterval(sendPing, CONTROL_PING_INTERVAL_MS)
  }

  function emitControl(event: string, data: Record<string, unknown>): void {
    // Only inject once control is fully active and the agent granted control.
    if (stateRef.current !== 'control-active' || !grantedControlRef.current) {
      return
    }
    const activeConfig = config.current
    const channel = controlChannel.current
    if (!activeConfig || channel?.readyState !== 'open') {
      return
    }
    try {
      channel.send(
        JSON.stringify({
          data,
          event,
          sequence: nextControlSequence(),
          sessionId: activeConfig.sessionId,
          timestamp: Date.now(),
          version: 1,
        }),
      )
    } catch {
      // readyState can change between the check and send(). Re-negotiate the
      // control path instead of leaking an event-handler exception.
      recoverControlChannel(channel, activeCycle.current)
    }
  }

  function sendPointerMove(x: number, y: number): void {
    emitControl('pointer.move', { x, y })
  }

  function sendPointerButton(button: PointerButton, action: KeyAction): void {
    emitControl('pointer.button', { action, button })
  }

  function sendPointerWheel(deltaX: number, deltaY: number): void {
    emitControl('pointer.wheel', { deltaX, deltaY })
  }

  function sendKey(code: string, action: KeyAction): void {
    emitControl(action === 'down' ? 'key.down' : 'key.up', { code })
  }

  function setRemoteClipboard(text: string): void {
    // Write the host clipboard so the user can Ctrl+V typed/pasted text on the
    // home PC. Mirrors CLIPBOARD_TEXT_MAX_LENGTH in the protocol schema.
    if (text.length > 0) {
      emitControl('clipboard.set', { text: text.slice(0, 16_384) })
    }
  }

  function releaseRemoteInput(): void {
    emitControl('control.release-all', {})
  }

  function sendImageClipboardMessage(
    event:
      | 'clipboard.image-offer'
      | 'clipboard.image-complete'
      | 'clipboard.image-applied'
      | 'clipboard.image-error',
    data: Record<string, unknown>,
  ): boolean {
    const activeConfig = config.current
    const channel = imageClipboardChannel.current
    if (!activeConfig || channel?.readyState !== 'open') {
      return false
    }
    try {
      channel.send(
        JSON.stringify({
          data,
          event,
          sequence: nextControlSequence(),
          sessionId: activeConfig.sessionId,
          timestamp: Date.now(),
          version: 1,
        }),
      )
    } catch {
      return false
    }
    return true
  }

  async function finishIncomingClipboardImage(
    incoming: IncomingClipboardImage,
  ): Promise<void> {
    if (incomingClipboardImage.current !== incoming) {
      return
    }
    incomingClipboardImage.current = null
    if (incoming.received !== incoming.size) {
      return
    }
    const merged = new Uint8Array(incoming.size)
    let offset = 0
    for (const chunk of incoming.chunks) {
      merged.set(chunk, offset)
      offset += chunk.byteLength
    }
    const data = merged.buffer
    if ((await sha256Hex(data)) !== incoming.sha256) {
      return
    }
    const blob = new Blob([data], { type: 'image/png' })
    const entry: ClipboardImageEntry = {
      blob,
      id: (clipboardImageIdRef.current += 1),
      receivedAt: Date.now(),
    }
    setClipboardImageEntries((previous) => [entry, ...previous].slice(0, 6))
  }

  function handleImageClipboardMessage(message: ImageClipboardMessage): void {
    if (message.event === 'clipboard.image-offer') {
      incomingClipboardImage.current = {
        chunks: [],
        received: 0,
        sha256: message.data.sha256,
        size: message.data.size,
        transferId: message.data.transferId,
      }
      return
    }
    if (message.event === 'clipboard.image-complete') {
      const incoming = incomingClipboardImage.current
      if (incoming?.transferId === message.data.transferId) {
        void finishIncomingClipboardImage(incoming)
      }
      return
    }
    const upload = clipboardImageUpload.current
    if (!upload || upload.transferId !== message.data.transferId) {
      return
    }
    clipboardImageUpload.current = null
    if (message.event === 'clipboard.image-applied') {
      upload.resolve()
    } else {
      upload.reject(new Error(message.data.code))
    }
  }

  function handleImageClipboardChunk(data: ArrayBuffer): void {
    const incoming = incomingClipboardImage.current
    if (!incoming) {
      return
    }
    const chunk = new Uint8Array(data)
    if (
      chunk.byteLength > 256 * 1024 ||
      incoming.received + chunk.byteLength > incoming.size
    ) {
      incomingClipboardImage.current = null
      return
    }
    incoming.chunks.push(chunk)
    incoming.received += chunk.byteLength
  }

  async function sendRemoteClipboardImage(image: Blob): Promise<void> {
    const channel = imageClipboardChannel.current
    if (
      image.type !== 'image/png' ||
      image.size <= 0 ||
      image.size > CLIPBOARD_IMAGE_MAX_BYTES
    ) {
      throw new Error('INVALID_IMAGE')
    }
    if (
      stateRef.current !== 'control-active' ||
      !grantedControlRef.current ||
      channel?.readyState !== 'open'
    ) {
      throw new Error('CONTROL_INACTIVE')
    }
    if (clipboardImageUpload.current) {
      throw new Error('BUSY')
    }

    const data = await image.arrayBuffer()
    const transferId = newTransferId()
    const digest = await sha256Hex(data)
    let timeoutId: number | null = null
    let upload!: ClipboardImageUpload
    const completion = new Promise<void>((resolve, reject) => {
      const clearTimer = () => {
        if (timeoutId !== null) {
          window.clearTimeout(timeoutId)
          timeoutId = null
        }
      }
      upload = {
        cancelled: false,
        reject: (error) => {
          clearTimer()
          reject(error)
        },
        resolve: () => {
          clearTimer()
          resolve()
        },
        transferId,
      }
      timeoutId = window.setTimeout(() => {
        if (clipboardImageUpload.current === upload) {
          clipboardImageUpload.current = null
          upload.cancelled = true
          upload.reject(new Error('TIMEOUT'))
        }
      }, 20_000)
    })
    clipboardImageUpload.current = upload

    try {
      if (
        !sendImageClipboardMessage('clipboard.image-offer', {
          mimeType: 'image/png',
          sha256: digest,
          size: data.byteLength,
          transferId,
        })
      ) {
        throw new Error('DISCONNECTED')
      }
      const sent = await streamFileChunks(
        channel,
        data,
        () => undefined,
        () => upload.cancelled,
      )
      if (
        !sent ||
        !sendImageClipboardMessage('clipboard.image-complete', { transferId })
      ) {
        throw new Error('DISCONNECTED')
      }
    } catch (error) {
      if (clipboardImageUpload.current === upload) {
        clipboardImageUpload.current = null
        upload.cancelled = true
        upload.reject(error instanceof Error ? error : new Error('SEND_FAILED'))
      }
    }
    return completion
  }

  function sendFileMessage(
    event:
      | 'file.offer'
      | 'file.complete'
      | 'file.cancel'
      | 'file.list-request'
      | 'file.download',
    data: Record<string, unknown>,
  ): void {
    const activeConfig = config.current
    const channel = fileChannel.current
    if (!activeConfig || channel?.readyState !== 'open') {
      return
    }
    channel.send(
      JSON.stringify({
        data,
        event,
        sequence: nextControlSequence(),
        sessionId: activeConfig.sessionId,
        timestamp: Date.now(),
        version: 1,
      }),
    )
  }

  function handleFileMessage(message: FileMessage): void {
    if (message.event === 'file.list') {
      setDownloadableFiles(message.data.files)
      return
    }
    // Every remaining event the viewer handles carries a transferId.
    if (
      message.event !== 'file.download-offer' &&
      message.event !== 'file.download-complete' &&
      message.event !== 'file.accept' &&
      message.event !== 'file.done' &&
      message.event !== 'file.error'
    ) {
      return
    }
    const transferId = message.data.transferId

    const download = fileDownload.current
    if (download && transferId === download.transferId) {
      if (message.event === 'file.download-offer') {
        download.size = message.data.size
        setFileDownloadState({
          errorCode: null,
          fileName: download.name,
          progress: message.data.size === 0 ? 1 : 0,
          status: 'downloading',
        })
      } else if (message.event === 'file.download-complete') {
        void finishDownload(download, message.data.sha256)
      } else if (message.event === 'file.error') {
        fileDownload.current = null
        setFileDownloadState({
          errorCode: message.data.code,
          fileName: download.name,
          progress: 0,
          status: 'error',
        })
      }
      return
    }

    const upload = fileUpload.current
    if (!upload || transferId !== upload.transferId) {
      return
    }
    if (message.event === 'file.accept') {
      upload.onAccept?.()
    } else if (message.event === 'file.done') {
      upload.onDone?.()
    } else if (message.event === 'file.error') {
      upload.onFail?.(message.data.code)
    }
  }

  function handleDownloadChunk(chunk: Uint8Array): void {
    const download = fileDownload.current
    if (!download) {
      return
    }
    download.chunks.push(chunk)
    download.received += chunk.byteLength
    const progress =
      download.size === 0 ? 1 : Math.min(1, download.received / download.size)
    const percent = Math.floor(progress * 100)
    if (percent !== download.lastPercent) {
      download.lastPercent = percent
      setFileDownloadState({
        errorCode: null,
        fileName: download.name,
        progress,
        status: 'downloading',
      })
    }
  }

  async function finishDownload(
    download: FileDownload,
    expectedSha: string,
  ): Promise<void> {
    fileDownload.current = null
    setFileDownloadState({
      errorCode: null,
      fileName: download.name,
      progress: 1,
      status: 'verifying',
    })
    const total = download.chunks.reduce((sum, part) => sum + part.byteLength, 0)
    const failWith = (code: string) =>
      setFileDownloadState({
        errorCode: code,
        fileName: download.name,
        progress: 0,
        status: 'error',
      })
    if (total !== download.size) {
      failWith('SIZE_MISMATCH')
      return
    }
    const merged = new Uint8Array(total)
    let offset = 0
    for (const part of download.chunks) {
      merged.set(part, offset)
      offset += part.byteLength
    }
    const actualSha = await sha256Hex(merged)
    if (actualSha !== expectedSha) {
      failWith('DIGEST_MISMATCH')
      return
    }
    triggerBrowserDownload(download.name, merged)
    setFileDownloadState({
      errorCode: null,
      fileName: download.name,
      progress: 1,
      status: 'done',
    })
  }

  function triggerBrowserDownload(name: string, bytes: Uint8Array): void {
    const url = URL.createObjectURL(new Blob([bytes as BlobPart]))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = name
    document.body.append(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }

  function requestFileList(): void {
    sendFileMessage('file.list-request', {})
  }

  function downloadFile(name: string): void {
    if (fileDownload.current || fileChannel.current?.readyState !== 'open') {
      return
    }
    const download: FileDownload = {
      chunks: [],
      lastPercent: -1,
      name,
      received: 0,
      size: 0,
      transferId: newTransferId(),
    }
    fileDownload.current = download
    setFileDownloadState({
      errorCode: null,
      fileName: name,
      progress: 0,
      status: 'downloading',
    })
    sendFileMessage('file.download', { name, transferId: download.transferId })
  }

  function clearFileDownload(): void {
    if (!fileDownload.current) {
      setFileDownloadState(null)
    }
  }

  function clearFileTransfer(): void {
    // Only clear a finished/failed banner, never an in-flight transfer.
    if (!fileUpload.current) {
      setFileTransfer(null)
    }
  }

  function sendFile(file: File): void {
    if (fileUpload.current || fileChannel.current?.readyState !== 'open') {
      return
    }
    if (file.size > FILE_MAX_BYTES) {
      setFileTransfer({
        errorCode: 'TOO_LARGE',
        fileName: file.name,
        progress: 0,
        status: 'error',
      })
      return
    }
    const upload: FileUpload = {
      cancelled: false,
      onAccept: null,
      onDone: null,
      onFail: null,
      transferId: newTransferId(),
    }
    fileUpload.current = upload
    setFileTransfer({
      errorCode: null,
      fileName: file.name,
      progress: 0,
      status: 'preparing',
    })
    void runUpload(file, upload).finally(() => {
      if (fileUpload.current === upload) {
        fileUpload.current = null
      }
    })
  }

  function awaitSignal(
    assign: (onOk: () => void, onFail: (code: string) => void) => void,
    timeoutMs: number,
  ): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('TIMEOUT')), timeoutMs)
      assign(
        () => {
          window.clearTimeout(timer)
          resolve()
        },
        (code) => {
          window.clearTimeout(timer)
          reject(new Error(code))
        },
      )
    })
  }

  async function runUpload(file: File, upload: FileUpload): Promise<void> {
    const channel = fileChannel.current
    if (!channel) {
      return
    }
    const setState = (status: FileTransferStatus, progress: number, errorCode: string | null) =>
      setFileTransfer({ errorCode, fileName: file.name, progress, status })
    try {
      const data = await file.arrayBuffer()
      const sha256 = await sha256Hex(data)
      // Phase 1: offer, wait for the agent to accept (or reject).
      await awaitSignal((onOk, onFail) => {
        upload.onAccept = onOk
        upload.onFail = onFail
        sendFileMessage('file.offer', {
          name: file.name,
          sha256,
          size: file.size,
          transferId: upload.transferId,
        })
      }, 30_000)
      // Phase 2: stream the bytes (progress throttled to whole-percent changes).
      setState('sending', 0, null)
      let lastPercent = -1
      const completed = await streamFileChunks(
        channel,
        data,
        (sent) => {
          const progress = data.byteLength === 0 ? 1 : sent / data.byteLength
          const percent = Math.floor(progress * 100)
          if (percent !== lastPercent) {
            lastPercent = percent
            setState('sending', progress, null)
          }
        },
        () => upload.cancelled || channel.readyState !== 'open',
      )
      if (!completed) {
        throw new Error('CANCELLED')
      }
      // Phase 3: signal completion, wait for the verified result. Bytes have
      // already drained, so this only covers the agent's hash + rename.
      setState('verifying', 1, null)
      await awaitSignal((onOk, onFail) => {
        upload.onDone = onOk
        upload.onFail = onFail
        sendFileMessage('file.complete', { transferId: upload.transferId })
      }, 30_000)
      setState('done', 1, null)
    } catch (error) {
      const code = error instanceof Error ? error.message : 'FAILED'
      setState('error', 0, code)
    }
  }

  async function createPeerConnection(cycle: number): Promise<void> {
    const activeConfig = config.current
    if (!activeConfig) {
      throw new Error('M0 개발 연결 설정이 없습니다.')
    }

    // STUN gives server-reflexive candidates so the viewer and agent can meet
    // across networks (phone LTE, office). TURN relay servers (from /turn, when
    // configured) are added on top so firewalled/UDP-blocked networks still
    // connect over TCP/TLS 443 (M3-05, Cloudflare Realtime TURN per ADR-016).
    const peer = new RTCPeerConnection({
      iceServers: [
        { urls: 'stun:stun.cloudflare.com:3478' },
        ...(activeConfig.iceServers ?? []),
      ],
    })
    peerConnection.current = peer
    const videoTransceiver = peer.addTransceiver('video', {
      direction: 'recvonly',
    })
    applyPreferredVideoCodecs(videoTransceiver)
    const channel = peer.createDataChannel('control-v1', { ordered: true })
    controlChannel.current = channel

    // Every handler ignores events from a superseded cycle so a late event from
    // an already-torn-down peer/channel can never mutate the current cycle's
    // readiness flags or connection state (see the cycle invariant above).
    channel.addEventListener('open', () => {
      if (cycle !== activeCycle.current) {
        return
      }
      isControlChannelOpen.current = true
      updateActiveState()
      startControlPing(channel, cycle)
    })
    channel.addEventListener('message', (event) => {
      if (cycle !== activeCycle.current || typeof event.data !== 'string') {
        return
      }

      try {
        const validation = validateControlMessage(JSON.parse(event.data))
        if (!validation.ok) {
          return
        }
        if (validation.value.event === 'session.pong') {
          missedControlPongs.current = 0
          setRoundTripTimeMs(Math.max(0, Date.now() - validation.value.timestamp))
        } else if (validation.value.event === 'clipboard.text') {
          const text = validation.value.data.text
          const entry: ClipboardEntry = {
            id: (clipboardIdRef.current += 1),
            receivedAt: Date.now(),
            text,
          }
          // Staged only — never written to the local clipboard automatically.
          setClipboardEntries((previous) =>
            [entry, ...previous].slice(0, MAX_CLIPBOARD_ENTRIES),
          )
        }
      } catch {
        setErrorMessage('잘못된 DataChannel 메시지를 받았습니다.')
      }
    })
    channel.addEventListener('close', () => {
      if (cycle !== activeCycle.current) {
        return
      }
      clearVideoProfileTimer()
      recoverControlChannel(channel, cycle)
    })
    // Screenshot/image clipboard bytes use their own ordered channel so a large
    // PNG cannot queue ahead of keyboard or pointer messages on control-v1.
    const clipboardImages = peer.createDataChannel('clipboard-v1', {
      ordered: true,
    })
    clipboardImages.binaryType = 'arraybuffer'
    imageClipboardChannel.current = clipboardImages
    clipboardImages.addEventListener('open', () => {
      if (cycle === activeCycle.current) {
        setCanSendClipboardImage(true)
      }
    })
    clipboardImages.addEventListener('message', (event) => {
      if (cycle !== activeCycle.current) {
        return
      }
      if (event.data instanceof ArrayBuffer) {
        handleImageClipboardChunk(event.data)
        return
      }
      if (typeof event.data !== 'string') {
        return
      }
      try {
        const validation = validateImageClipboardMessage(JSON.parse(event.data))
        if (validation.ok) {
          handleImageClipboardMessage(validation.value)
        }
      } catch {
        // Ignore malformed image clipboard metadata.
      }
    })
    clipboardImages.addEventListener('close', () => {
      if (cycle !== activeCycle.current) {
        return
      }
      setCanSendClipboardImage(false)
      incomingClipboardImage.current = null
      const upload = clipboardImageUpload.current
      if (upload) {
        clipboardImageUpload.current = null
        upload.cancelled = true
        upload.reject(new Error('DISCONNECTED'))
      }
    })
    // Dedicated ordered channel for file transfer (ADR-014): bulk bytes never
    // share control-v1 and so never starve input.
    const files = peer.createDataChannel('file-v1', { ordered: true })
    // Download chunks (agent -> viewer) arrive as raw binary; take them as
    // ArrayBuffers rather than Blobs so they can be appended synchronously.
    files.binaryType = 'arraybuffer'
    fileChannel.current = files
    files.addEventListener('open', () => {
      if (cycle === activeCycle.current) {
        setCanSendFiles(true)
      }
    })
    files.addEventListener('message', (event) => {
      if (cycle !== activeCycle.current) {
        return
      }
      if (event.data instanceof ArrayBuffer) {
        handleDownloadChunk(new Uint8Array(event.data))
        return
      }
      if (typeof event.data !== 'string') {
        return
      }
      try {
        const validation = validateFileMessage(JSON.parse(event.data))
        if (validation.ok) {
          handleFileMessage(validation.value)
        }
      } catch {
        // Ignore malformed agent file messages.
      }
    })
    files.addEventListener('close', () => {
      if (cycle === activeCycle.current) {
        setCanSendFiles(false)
      }
    })
    peer.addEventListener('track', (event) => {
      if (cycle !== activeCycle.current) {
        return
      }
      const [stream] = event.streams
      setMediaStream(stream ?? new MediaStream([event.track]))
      startVideoStats(peer, cycle)
      isVideoTrackReady.current = true
      updateActiveState()
      if (peer.connectionState === 'connected') {
        clearConnectionEstablishmentTimer()
      }
    })
    peer.addEventListener('connectionstatechange', () => {
      if (peer.connectionState === 'connected') {
        // A healthy connection clears the retry budget for the next drop.
        autoReconnectAttempts.current = 0
        if (isVideoTrackReady.current) {
          clearConnectionEstablishmentTimer()
        }
      } else if (peer.connectionState === 'failed') {
        applyConnectionIssue('WEBRTC_FAILED')
        teardownForCycle(cycle)
        scheduleAutoReconnect()
      }
    })

    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    await waitForIceGathering(peer)
    const localDescription = peer.localDescription
    if (!localDescription?.sdp) {
      throw new Error('WebRTC offer를 생성하지 못했습니다.')
    }

    sendSignaling({
      payload: { sdp: localDescription.sdp },
      sequence: nextSignalingSequence(),
      sessionId: activeConfig.sessionId,
      type: 'webrtc.offer',
      version: 1,
    })
  }

  async function handleSignalingMessage(
    message: SignalingMessage,
    cycle: number,
    resumeConfig: DevelopmentConnectionConfig | null = null,
  ): Promise<void> {
    if (message.type === 'session.accept') {
      // The agent may downgrade a control request to view-only per its local
      // policy; only inject input when it actually granted control.
      const granted = message.payload.permission === 'control'
      grantedControlRef.current = granted
      setControlGranted(granted)
      setControlPolicyEnabled(granted)
      setControlLocked(false)
      if (message.payload.videoProfile) {
        setVideoProfileState(message.payload.videoProfile)
      }
      if (resumeConfig) {
        if (canPreservePeerDuringSignalingRecovery()) {
          config.current = resumeConfig
          cancelSignalingRecovery()
        }
        return
      }
      moveTo('negotiating')
      await createPeerConnection(cycle)
      return
    }

    if (message.type === 'webrtc.answer') {
      await peerConnection.current?.setRemoteDescription({
        sdp: message.payload.sdp,
        type: 'answer',
      })
      return
    }

    if (message.type === 'webrtc.ice') {
      await peerConnection.current?.addIceCandidate(message.payload)
      return
    }

    if (message.type === 'session.configured') {
      clearVideoProfileTimer()
      setVideoProfileState(message.payload.videoProfile)
      setVideoProfilePending(false)
      setVideoProfileError(null)
      clearVideoProfileStabilizing()
      stabilizingVideoProfile.current = message.payload.videoProfile
      setVideoProfileStabilizing(true)
      videoProfileStabilizeTimer.current = window.setTimeout(() => {
        videoProfileStabilizeTimer.current = null
        stabilizingVideoProfile.current = null
        setVideoProfileStabilizing(false)
      }, VIDEO_PROFILE_STABILIZE_TIMEOUT_MS)
      return
    }

    if (message.type === 'session.policy') {
      setControlPolicyEnabled(message.payload.controlEnabled)
      setControlLocked(message.payload.locked)
      if (
        !message.payload.controlEnabled ||
        !message.payload.controlGranted ||
        message.payload.locked
      ) {
        releaseRemoteInput()
        grantedControlRef.current = false
        setControlGranted(false)
      }
      return
    }

    if (message.type === 'agent.online') {
      // A reconnect before negotiation completed has no healthy peer to keep.
      // Restart that incomplete cycle; active sessions simply retain WebRTC.
      if (
        stateRef.current === 'reserved' ||
        stateRef.current === 'negotiating'
      ) {
        teardownForCycle(cycle)
        scheduleAutoReconnect()
      }
      return
    }

    if (message.type === 'session.reject' || message.type === 'error') {
      const code = message.payload.code
      if (
        message.type === 'error' &&
        code === 'PEER_OFFLINE' &&
        canPreservePeerDuringSignalingRecovery()
      ) {
        clearVideoProfileTimer()
        setVideoProfilePending(false)
        setVideoProfileError(
          '시그널링을 복구하는 중입니다. 현재 영상과 입력 연결은 유지됩니다.',
        )
        return
      }
      applyConnectionIssue(
        code,
        message.type === 'error' ? message.payload.retryable : undefined,
      )
      teardownForCycle(cycle)
      return
    }

    if (message.type === 'session.close' || message.type === 'agent.offline') {
      applyConnectionIssue('AGENT_OFFLINE')
      teardownForCycle(cycle)
    }
  }

  function connectInternal(isAuto: boolean): void {
    // Guard on stateRef (synchronous source of truth) rather than the React
    // connectionState closure, which can be stale between a disconnect and the
    // next render and let a second connect start mid-teardown. isConnecting
    // additionally blocks re-entry while a production ticket fetch is in flight.
    if (isConnecting.current || stateRef.current !== 'offline') {
      return
    }
    // A fresh user-driven connect re-arms auto-reconnect and its budget; an
    // automatic retry keeps counting toward the cap.
    userDisconnected.current = false
    if (!isAuto) {
      autoReconnectAttempts.current = 0
      cancelAutoReconnect()
    }

    setErrorMessage(null)
    setErrorAction(null)
    setCanRetry(false)
    // Start a new cycle; handlers below capture it so teardowns (and a
    // superseded ticket fetch) stay scoped to this connection.
    const cycle = (activeCycle.current += 1)
    startConnectionEstablishmentTimer(cycle)

    if (!isProduction.current) {
      const activeConfig = config.current
      if (!activeConfig) {
        clearConnectionEstablishmentTimer()
        return
      }
      try {
        startConnection(activeConfig, cycle)
      } catch {
        clearConnectionEstablishmentTimer()
        applyConnectionIssue('SIGNALING_HANDSHAKE_FAILED')
      }
      return
    }

    // Production: exchange the Access session for a fresh session ticket, then
    // open the socket. A disconnect or newer connect during the fetch advances
    // the cycle so this resolution is dropped.
    isConnecting.current = true
    void requestSessionConfig('control')
      .then((fetched) => {
        isConnecting.current = false
        if (cycle !== activeCycle.current || stateRef.current !== 'offline') {
          return
        }
        config.current = fetched
        try {
          startConnection(fetched, cycle)
        } catch {
          clearConnectionEstablishmentTimer()
          applyConnectionIssue('SIGNALING_HANDSHAKE_FAILED')
          if (isAuto) {
            scheduleAutoReconnect()
          }
        }
      })
      .catch(() => {
        isConnecting.current = false
        if (cycle !== activeCycle.current) {
          return
        }
        clearConnectionEstablishmentTimer()
        applyConnectionIssue('SESSION_TICKET_FAILED')
        if (isAuto) {
          scheduleAutoReconnect()
        }
      })
  }

  function startConnection(
    activeConfig: DevelopmentConnectionConfig,
    cycle: number,
    resumeExistingPeer = false,
  ): void {
    let receivedServerIssue = false
    // A replacement is an intentional transfer of ownership to another tab or
    // device. The displaced viewer may offer a manual retry, but must not
    // automatically reconnect and evict the new owner in return.
    let suppressAutoReconnect = false
    const url = new URL(activeConfig.webSocketUrl)
    url.searchParams.set('ticket', activeConfig.ticket)
    const socket = new WebSocket(url)
    webSocket.current = socket

    socket.addEventListener('open', () => {
      if (cycle !== activeCycle.current) {
        socket.close(1000, 'Superseded connection')
        return
      }
      if (
        resumeExistingPeer &&
        !canPreservePeerDuringSignalingRecovery()
      ) {
        socket.close(1000, 'Peer no longer active')
        return
      }
      if (!resumeExistingPeer) {
        moveTo('online')
      }
      try {
        sendSignaling({
          payload: {
            deviceId: activeConfig.deviceId,
            permission: 'control',
            videoProfile,
          },
          sequence: nextSignalingSequence(),
          sessionId: activeConfig.sessionId,
          type: 'session.request',
          version: 1,
        })
        if (!resumeExistingPeer) {
          moveTo('reserved')
        }
      } catch {
        if (cycle !== activeCycle.current) {
          return
        }
        applyConnectionIssue('SIGNALING_HANDSHAKE_FAILED')
        teardownForCycle(cycle)
        scheduleAutoReconnect()
      }
    })
    socket.addEventListener('message', (event) => {
      if (cycle !== activeCycle.current || typeof event.data !== 'string') {
        return
      }

      // Parse/validation failures must NOT tear down an established session: an
      // unrecognized or malformed signaling frame (e.g. a newer field arriving
      // during a rolling deploy / cache skew, or a stray non-JSON frame) is
      // ignored rather than treated as fatal. Only a genuine failure while
      // *processing* a valid message (WebRTC negotiation) tears down. The control
      // DataChannel has its own independent validation, so ignoring here is safe.
      let parsed: unknown
      try {
        parsed = JSON.parse(event.data)
      } catch {
        return
      }
      const validation = validateSignalingMessage(parsed)
      if (!validation.ok) {
        return
      }
      if (
        validation.value.type === 'error' ||
        validation.value.type === 'session.reject'
      ) {
        receivedServerIssue = true
        suppressAutoReconnect =
          validation.value.payload.code === 'SESSION_REPLACED'
      }
      if (
        resumeExistingPeer &&
        validation.value.type === 'error' &&
        validation.value.payload.code === 'AGENT_OFFLINE'
      ) {
        return
      }
      void handleSignalingMessage(
        validation.value,
        cycle,
        resumeExistingPeer ? activeConfig : null,
      ).catch(() => {
        if (cycle !== activeCycle.current) {
          return
        }
        applyConnectionIssue('WEBRTC_NEGOTIATION_FAILED')
        teardownForCycle(cycle)
        scheduleAutoReconnect()
      })
    })
    socket.addEventListener('error', () => {
      if (cycle !== activeCycle.current) {
        return
      }
      if (!receivedServerIssue && !canPreservePeerDuringSignalingRecovery()) {
        applyConnectionIssue('SIGNALING_HANDSHAKE_FAILED')
      }
    })
    socket.addEventListener('close', () => {
      if (webSocket.current === socket) {
        webSocket.current = null
      }
      if (
        cycle !== activeCycle.current ||
        userDisconnected.current
      ) {
        if (!suppressAutoReconnect && stateRef.current === 'offline') {
          scheduleAutoReconnect()
        }
        return
      }
      if (canPreservePeerDuringSignalingRecovery()) {
        scheduleSignalingRecovery(cycle)
        return
      }
      teardownForCycle(cycle)
      // During initial negotiation there is no peer to preserve, so use the
      // existing bounded full-reconnect path.
      if (!suppressAutoReconnect) {
        scheduleAutoReconnect()
      }
    })
  }

  function changeVideoProfile(profile: VideoProfile): void {
    if (profile === videoProfile || videoProfilePending || videoProfileStabilizing) {
      return
    }
    setVideoProfileError(null)
    const activeConfig = config.current
    const socket = webSocket.current
    if (
      !activeConfig ||
      socket?.readyState !== WebSocket.OPEN ||
      (stateRef.current !== 'view-active' && stateRef.current !== 'control-active')
    ) {
      setVideoProfileState(profile)
      return
    }
    setVideoProfilePending(true)
    setVideoProfileError(null)
    sendSignaling({
      payload: { videoProfile: profile },
      sequence: nextSignalingSequence(),
      sessionId: activeConfig.sessionId,
      type: 'session.configure',
      version: 1,
    })
    clearVideoProfileTimer()
    videoProfileTimer.current = window.setTimeout(() => {
      videoProfileTimer.current = null
      setVideoProfilePending(false)
      setVideoProfileError('화질 변경 확인이 지연되고 있습니다. 다시 선택해 주세요.')
    }, VIDEO_PROFILE_TIMEOUT_MS)
  }

  useEffect(() => {
    if (config.current) {
      preserveDevelopmentConfig(config.current)
    }

    return () => {
      userDisconnected.current = true
      activeCycle.current += 1
      releaseResources()
    }
  }, [])

  return {
    canConnect: isProduction.current === true || config.current !== null,
    canSendClipboardImage,
    canRetry,
    clipboardEntries,
    clipboardImageEntries,
    connect: () => connectInternal(false),
    connectionState,
    controlGranted,
    controlLocked,
    controlPolicyEnabled,
    deviceId: config.current?.deviceId ?? null,
    dismissClipboardEntry: (id: number) =>
      setClipboardEntries((previous) => previous.filter((entry) => entry.id !== id)),
    dismissClipboardImageEntry: (id: number) =>
      setClipboardImageEntries((previous) =>
        previous.filter((entry) => entry.id !== id),
      ),
    fileTransfer,
    canSendFiles,
    sendFile,
    clearFileTransfer,
    downloadableFiles,
    requestFileList,
    downloadFile,
    fileDownload: fileDownloadState,
    clearFileDownload,
    disconnect,
    errorMessage,
    errorAction,
    isControlActive: connectionState === 'control-active' && controlGranted,
    mediaStream,
    releaseRemoteInput,
    roundTripTimeMs,
    connectionRoute,
    videoTelemetry,
    setVideoProfile: changeVideoProfile,
    sendKey,
    sendRemoteClipboardImage,
    setRemoteClipboard,
    sendPointerButton,
    sendPointerMove,
    sendPointerWheel,
    videoProfile,
    videoProfileError,
    videoProfilePending,
    videoProfileStabilizing,
  }
}
