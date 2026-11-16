// Viewer -> agent file upload helpers (M4, ADR-014). Files travel as raw binary
// chunks on the dedicated file-v1 DataChannel between file.offer and
// file.complete; these helpers compute the integrity digest and stream the
// bytes with backpressure so a fast sender cannot blow up the channel buffer.

import { BULK_CHUNK_BYTES, waitForBulkCapacity, type BulkTransferContext } from './bulkTransfer'
import { FILE_SEND_BYTES, type FileFlow } from './fileFlow'

// Keep each SCTP message <=16KiB even when message interleaving is unavailable.
export const FILE_CHUNK_BYTES = BULK_CHUNK_BYTES
export const FILE_MAX_BYTES = 500 * 1024 * 1024

/** Lowercase hex SHA-256 of a buffer (matches the agent's hashlib digest). */
export async function sha256Hex(data: BufferSource): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', data)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * Stream a file's bytes over the channel in ordered chunks, honoring
 * backpressure. The channel is ordered, so the caller can enqueue file.complete
 * immediately after this returns; SCTP delivers it only after every binary
 * chunk. Waiting for bufferedAmount to become exactly zero can hang forever in
 * some Chromium/WebRTC builds even after all bytes have been handed off.
 * `isCancelled` is polled so a disconnect or user cancel stops the send;
 * `onProgress` reports cumulative bytes handed to the channel.
 */
export async function streamFileChunks(
  channel: RTCDataChannel,
  data: ArrayBuffer,
  onProgress: (sent: number) => void,
  isCancelled: () => boolean,
  bulkContext?: BulkTransferContext,
  flow?: FileFlow,
): Promise<boolean> {
  let offset = 0
  while (offset < data.byteLength) {
    if (isCancelled() || channel.readyState !== 'open') {
      return false
    }
    const end = Math.min(offset + (flow ? Math.min(FILE_CHUNK_BYTES, FILE_SEND_BYTES) : FILE_CHUNK_BYTES), data.byteLength)
    if (flow && !(await flow.wait(end - offset, () => isCancelled() || channel.readyState !== 'open'))) return false
    if (!(await waitForBulkCapacity(channel, end - offset, isCancelled, bulkContext))) return false
    channel.send(data.slice(offset, end))
    flow?.sent(end - offset)
    offset = end
    onProgress(offset)
  }
  // Do not wait for bufferedAmount === 0 here. file.complete is sent on this
  // same ordered channel and therefore cannot overtake the chunks.
  return !isCancelled() && channel.readyState === 'open'
}
