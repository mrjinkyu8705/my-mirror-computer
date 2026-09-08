export type DisplayMode = 'fit' | 'actual'
export type VideoProfile = 'low' | 'balanced' | 'high'
export type ConnectionRoute = 'direct' | 'turn' | 'unknown'
export type QualityTone = 'neutral' | 'good' | 'fair' | 'poor'

export interface ConnectionQuality {
  readonly label: string
  readonly tone: QualityTone
}

export interface VideoStatsSample {
  readonly bytesReceived: number
  readonly framesDecoded: number
  readonly framesPerSecond: number | null
  readonly frameHeight: number | null
  readonly frameWidth: number | null
  readonly jitterSeconds: number | null
  readonly packetsLost: number
  readonly packetsReceived: number
  readonly qpSum: number | null
  readonly timestampMs: number
}

export interface VideoTelemetry {
  readonly averageQp: number | null
  readonly decodedFramesPerSecond: number | null
  readonly frameHeight: number | null
  readonly frameWidth: number | null
  readonly jitterMs: number | null
  readonly packetLossPercent: number | null
  readonly receivedBitrateMbps: number | null
}

export interface ConnectionRouteTelemetry {
  readonly protocol: string | null
  readonly route: ConnectionRoute
}

interface IceStatsRecord {
  readonly candidateType?: string
  readonly id?: string
  readonly localCandidateId?: string
  readonly nominated?: boolean
  readonly protocol?: string
  readonly remoteCandidateId?: string
  readonly selected?: boolean
  readonly selectedCandidatePairId?: string
  readonly state?: string
  readonly type?: string
}

export const EMPTY_VIDEO_TELEMETRY: VideoTelemetry = Object.freeze({
  averageQp: null,
  decodedFramesPerSecond: null,
  frameHeight: null,
  frameWidth: null,
  jitterMs: null,
  packetLossPercent: null,
  receivedBitrateMbps: null,
})

export const EMPTY_CONNECTION_ROUTE: ConnectionRouteTelemetry = Object.freeze({
  protocol: null,
  route: 'unknown',
})

export function readConnectionRoute(
  report: Pick<RTCStatsReport, 'forEach'>,
): ConnectionRouteTelemetry {
  const entries = new Map<string, IceStatsRecord>()
  let transportSelectedPairId: string | null = null
  const candidatePairs: IceStatsRecord[] = []

  report.forEach((entry: RTCStats) => {
    const record = entry as IceStatsRecord
    if (record.id) {
      entries.set(record.id, record)
    }
    if (record.type === 'transport' && record.selectedCandidatePairId) {
      transportSelectedPairId = record.selectedCandidatePairId
    } else if (record.type === 'candidate-pair') {
      candidatePairs.push(record)
    }
  })

  const selectedPair =
    (transportSelectedPairId
      ? entries.get(transportSelectedPairId)
      : undefined) ??
    candidatePairs.find((pair) => pair.selected === true) ??
    candidatePairs.find(
      (pair) => pair.nominated === true && pair.state === 'succeeded',
    ) ??
    candidatePairs.find((pair) => pair.state === 'succeeded')

  if (!selectedPair?.localCandidateId || !selectedPair.remoteCandidateId) {
    return EMPTY_CONNECTION_ROUTE
  }
  const localCandidate = entries.get(selectedPair.localCandidateId)
  const remoteCandidate = entries.get(selectedPair.remoteCandidateId)
  if (!localCandidate || !remoteCandidate) {
    return EMPTY_CONNECTION_ROUTE
  }

  return {
    protocol: localCandidate.protocol ?? remoteCandidate.protocol ?? null,
    route:
      localCandidate.candidateType === 'relay' ||
      remoteCandidate.candidateType === 'relay'
        ? 'turn'
        : 'direct',
  }
}

function rounded(value: number, digits: number): number {
  const scale = 10 ** digits
  return Math.round(value * scale) / scale
}

export function computeVideoTelemetry(
  previous: VideoStatsSample | null,
  current: VideoStatsSample,
): VideoTelemetry {
  const elapsedSeconds = previous
    ? (current.timestampMs - previous.timestampMs) / 1_000
    : 0
  const bytesDelta = previous ? current.bytesReceived - previous.bytesReceived : -1
  const receivedDelta = previous
    ? current.packetsReceived - previous.packetsReceived
    : -1
  const lostDelta = previous ? current.packetsLost - previous.packetsLost : -1
  const frameDelta = previous ? current.framesDecoded - previous.framesDecoded : -1
  const qpDelta =
    previous && current.qpSum !== null && previous.qpSum !== null
      ? current.qpSum - previous.qpSum
      : -1
  const packetDelta = receivedDelta + lostDelta

  return {
    averageQp:
      frameDelta > 0 && qpDelta >= 0 ? rounded(qpDelta / frameDelta, 1) : null,
    decodedFramesPerSecond:
      current.framesPerSecond !== null
        ? rounded(current.framesPerSecond, 1)
        : elapsedSeconds > 0 && frameDelta >= 0
          ? rounded(frameDelta / elapsedSeconds, 1)
          : null,
    frameHeight: current.frameHeight,
    frameWidth: current.frameWidth,
    jitterMs:
      current.jitterSeconds !== null
        ? rounded(current.jitterSeconds * 1_000, 1)
        : null,
    packetLossPercent:
      packetDelta > 0 && lostDelta >= 0
        ? rounded((lostDelta / packetDelta) * 100, 1)
        : null,
    receivedBitrateMbps:
      elapsedSeconds > 0 && bytesDelta >= 0
        ? rounded((bytesDelta * 8) / elapsedSeconds / 1_000_000, 2)
        : null,
  }
}

export function getConnectionQuality(
  roundTripTimeMs: number | null,
  video: VideoTelemetry = EMPTY_VIDEO_TELEMETRY,
): ConnectionQuality {
  if (
    (video.packetLossPercent !== null && video.packetLossPercent >= 5) ||
    (video.averageQp !== null && video.averageQp >= 38)
  ) {
    return { label: '화질 저하', tone: 'poor' }
  }
  if (
    (video.packetLossPercent !== null && video.packetLossPercent >= 1) ||
    (video.jitterMs !== null && video.jitterMs >= 30) ||
    (video.averageQp !== null && video.averageQp >= 32)
  ) {
    return { label: '불안정', tone: 'fair' }
  }
  if (roundTripTimeMs === null || !Number.isFinite(roundTripTimeMs)) {
    return { label: '측정 중', tone: 'neutral' }
  }
  if (roundTripTimeMs <= 80) {
    return { label: '원활', tone: 'good' }
  }
  if (roundTripTimeMs <= 180) {
    return { label: '보통', tone: 'fair' }
  }
  return { label: '지연', tone: 'poor' }
}

export function formatSessionDuration(elapsedSeconds: number): string {
  const safeSeconds = Number.isFinite(elapsedSeconds)
    ? Math.max(0, Math.floor(elapsedSeconds))
    : 0
  const hours = Math.floor(safeSeconds / 3_600)
  const minutes = Math.floor((safeSeconds % 3_600) / 60)
  const seconds = safeSeconds % 60
  const minuteSecond = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  return hours > 0
    ? `${String(hours).padStart(2, '0')}:${minuteSecond}`
    : minuteSecond
}

export function getDisplayModeLabel(mode: DisplayMode): string {
  return mode === 'fit' ? '원본 크기' : '화면 맞춤'
}
