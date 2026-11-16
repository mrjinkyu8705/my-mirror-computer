export const CONTROL_PING_INTERVAL_MS = 1_000
export const CONTROL_MAX_MISSED_PONGS = 8

/**
 * A closed channel is immediately unhealthy. An open channel is allowed several
 * unanswered heartbeats so short congestion and background-tab timer throttling
 * do not cause a needless reconnect.
 */
export function controlChannelNeedsRecovery(
  readyState: RTCDataChannelState,
  missedPongs: number,
): boolean {
  return readyState !== 'open' || missedPongs >= CONTROL_MAX_MISSED_PONGS
}
