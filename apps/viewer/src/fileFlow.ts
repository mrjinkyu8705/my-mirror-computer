// File-only pacing. The existing 16KiB message / 64KiB aggregate limits remain.
// bufferedAmount is not a receipt ACK. Negotiated cumulative ACKs bound data
// already handed to SCTP as well as JS queues, without changing media budgets.
export const FILE_WINDOW_BYTES = 8 * 1024
export const FILE_SEND_BYTES = 4 * 1024
export const FILE_PACE_BYTES_PER_SECOND = 500_000
const STALL_MS = 30_000

export class FileFlow {
  private credit = false
  private sentBytes = 0
  private receivedBytes = 0
  private nextAt = 0
  private peak = 0
  enable(): void { this.credit = true }
  acknowledge(received: number): void {
    if (Number.isSafeInteger(received) && received >= this.receivedBytes && received <= this.sentBytes)
      this.receivedBytes = received
  }
  sent(bytes: number): void {
    this.sentBytes += bytes
    this.peak = Math.max(this.peak, this.sentBytes - this.receivedBytes)
    this.nextAt = performance.now() + bytes * 1000 / FILE_PACE_BYTES_PER_SECOND
  }
  get stats() { return { credit: this.credit, sent: this.sentBytes, acknowledged: this.receivedBytes, peakInFlight: this.peak } }
  async wait(bytes: number, cancelled: () => boolean): Promise<boolean> {
    const deadline = performance.now() + STALL_MS
    while (!cancelled()) {
      const now = performance.now()
      const blocked = this.credit && this.sentBytes + bytes - this.receivedBytes > FILE_WINDOW_BYTES
      const delay = Math.max(0, this.nextAt - now)
      if (!blocked && delay <= 0) return true
      if (now >= deadline) return false
      await new Promise<void>(resolve => window.setTimeout(resolve, blocked ? 10 : Math.max(1, Math.min(10, Math.ceil(delay)))))
    }
    return false
  }
}
