export const CONTROL_BUFFER_HIGH = 4 * 1024
export const CONTROL_BUFFER_LIMIT = 256 * 1024
const MAX_MOVE_AGE_MS = 200

/** Only disposable positions are coalesced. Keys/buttons keep their ordering. */
export class PendingPointerMove {
  private pending: { x: number; y: number; at: number } | null = null

  offer(x: number, y: number, bufferedBytes: number, now: number): boolean {
    if (bufferedBytes >= CONTROL_BUFFER_HIGH) {
      this.pending = { x, y, at: now }
      return false
    }
    this.clear()
    return true
  }

  take(now: number, beforeAction = false): { x: number; y: number } | null {
    const point = this.pending
    this.clear()
    // A click/drag release must use its preceding position even after a stall.
    if (!point || (!beforeAction && now - point.at > MAX_MOVE_AGE_MS)) return null
    return { x: point.x, y: point.y }
  }

  clear(): void { this.pending = null }
}
