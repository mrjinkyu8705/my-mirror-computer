import type { ConnectionState } from '@mirror/protocol'

export const SIGNALING_RECOVERY_INITIAL_DELAY_MS = 2_000
export const SIGNALING_RECOVERY_MAX_DELAY_MS = 30_000

/**
 * Signaling is no longer required after negotiation while the peer and its
 * control channel are still usable. Preserve that session and recover only the
 * WebSocket; a failed/closed peer still follows the normal full reconnect path.
 */
export function canRecoverSignalingWithoutPeerRestart(
  state: ConnectionState,
  peerState: RTCPeerConnectionState | null,
  controlState: RTCDataChannelState | null,
): boolean {
  return (
    (state === 'view-active' || state === 'control-active') &&
    (peerState === 'connected' || peerState === 'disconnected') &&
    controlState === 'open'
  )
}

export function nextSignalingRecoveryDelay(currentDelayMs: number): number {
  return Math.min(
    Math.max(currentDelayMs, SIGNALING_RECOVERY_INITIAL_DELAY_MS) * 2,
    SIGNALING_RECOVERY_MAX_DELAY_MS,
  )
}
