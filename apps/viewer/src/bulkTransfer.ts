// Smaller SCTP messages plus bounded aggregate queues reduce head-of-line
// interference with control/RTP. This is cooperative admission control, not
// transport-level priority or a guarantee about Internet latency (RFC 8831 6.6).
export const BULK_CHUNK_BYTES = 16 * 1024
export const BULK_BUFFER_BYTES = 64 * 1024
const CONTROL_BUSY_BYTES = 2 * 1024
const BULK_YIELD_FALLBACK_MS = 4
const BULK_POLL_MS = 10
const BULK_STALL_MS = 30_000

export interface BulkTransferContext {
  readonly bufferedBytes: () => number
  readonly controlBufferedBytes: () => number
}

const pause = (ms: number) => new Promise<void>(resolve => window.setTimeout(resolve, ms))

function yieldToBrowser(): Promise<void> {
  if (typeof window.MessageChannel !== 'function') return pause(BULK_YIELD_FALLBACK_MS)
  return new Promise(resolve => {
    const ports = new window.MessageChannel()
    ports.port1.onmessage = () => {
      ports.port1.close()
      ports.port2.close()
      resolve()
    }
    ports.port2.postMessage(null)
  })
}

/** Call for each <=16KiB chunk; send synchronously once this returns true. */
export async function waitForBulkCapacity(
  channel: RTCDataChannel,
  bytes: number,
  isCancelled: () => boolean,
  context?: BulkTransferContext,
): Promise<boolean> {
  if (bytes < 1 || bytes > BULK_CHUNK_BYTES) throw new Error('INVALID_CHUNK')
  if (isCancelled() || channel.readyState !== 'open') return false
  // Even fast channels must yield between chunks so wheel/key/render events
  // and other transfers can run; do not enqueue megabytes in a single JS task.
  await yieldToBrowser()
  const deadline = Date.now() + BULK_STALL_MS
  while (true) {
    if (isCancelled() || channel.readyState !== 'open') return false
    const buffered = Math.max(channel.bufferedAmount, context?.bufferedBytes() ?? 0)
    if (buffered + bytes <= BULK_BUFFER_BYTES &&
        (context?.controlBufferedBytes() ?? 0) < CONTROL_BUSY_BYTES) return true
    if (Date.now() >= deadline) return false
    await pause(BULK_POLL_MS)
  }
}
