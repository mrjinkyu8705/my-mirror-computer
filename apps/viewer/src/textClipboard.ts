import {
  CLIPBOARD_TEXT_MAX_CHARS, CLIPBOARD_TEXT_MAX_BYTES, CLIPBOARD_TEXT_CHUNK_BYTES,
  CLIPBOARD_TEXT_RECEIVE_MAX_CHARS,
  validateTextClipboardMessage,
} from '@mirror/protocol'
import { sha256Hex } from './fileUpload'
import { BULK_CHUNK_BYTES, waitForBulkCapacity, type BulkTransferContext } from './bulkTransfer'

type Progress = (sent: number, total: number) => void
interface Options {
  readonly channel: RTCDataChannel
  readonly sessionId: string
  readonly nextSequence: () => number
  readonly isActive: () => boolean
  readonly bulkContext?: BulkTransferContext
}
function deferred() {
  let resolve!: () => void
  let reject!: (error: Error) => void
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no })
  // A close/timeout can reject while preparation/streaming is still in progress.
  void promise.catch(() => undefined)
  return { promise, resolve, reject }
}
interface Pending {
  readonly id: string
  readonly accepted: ReturnType<typeof deferred>
  readonly applied: ReturnType<typeof deferred>
  cancelled: boolean
  failure: Error | null
  timer: ReturnType<typeof setTimeout> | null
  phase: 'offered' | 'streaming' | 'complete'
}

/** Clipboard-only bulk channel. Control keys are sent by the UI after applied. */
export class TextClipboardSender {
  private pending: Pending | null = null
  private closed = false
  private lastSequence = 0
  constructor(private readonly options: Options) {}

  private message(event: string, data: Record<string, unknown>): void {
    if (this.closed || this.options.channel.readyState !== 'open') throw new Error('DISCONNECTED')
    this.options.channel.send(JSON.stringify({ data, event, sequence: this.options.nextSequence(),
      sessionId: this.options.sessionId, timestamp: Date.now(), version: 1 }))
  }
  private fail(pending: Pending, code: string): void {
    pending.cancelled = true
    pending.failure ??= new Error(code)
    pending.accepted.reject(pending.failure)
    pending.applied.reject(pending.failure)
    if (pending.timer !== null) clearTimeout(pending.timer)
    pending.timer = null
  }
  receive(raw: unknown): void {
    if (typeof raw !== 'string' || raw.length > 4096) return
    try {
      const validation = validateTextClipboardMessage(JSON.parse(raw))
      if (!validation.ok) return
      const message = validation.value
      if (message.sessionId !== this.options.sessionId || message.sequence <= this.lastSequence ||
          Math.abs(message.timestamp - Date.now()) > 30_000) return
      this.lastSequence = message.sequence
      const pending = this.pending
      if (!pending || pending.cancelled || pending.id !== message.data.transferId) return
      if (message.event === 'clipboard.text-error') this.fail(pending, message.data.code)
      else if (message.event === 'clipboard.text-accept' && pending.phase === 'offered') pending.accepted.resolve()
      else if (message.event === 'clipboard.text-applied' && pending.phase === 'complete') pending.applied.resolve()
    } catch { /* Malformed or stale acknowledgements cannot authorize a paste. */ }
  }
  close(): void {
    this.closed = true
    if (this.pending) this.fail(this.pending, 'DISCONNECTED')
  }
  async send(text: string, onProgress: Progress = () => undefined): Promise<void> {
    if (!text || text.length > CLIPBOARD_TEXT_MAX_CHARS || text.includes('\0')) throw new Error('INVALID_TEXT')
    if (this.closed || this.options.channel.readyState !== 'open' || !this.options.isActive()) throw new Error('CONTROL_INACTIVE')
    if (this.pending) throw new Error('BUSY')
    const pending: Pending = { id: crypto.randomUUID(), accepted: deferred(), applied: deferred(),
      cancelled: false, failure: null, timer: null, phase: 'offered' }
    this.pending = pending // Lock before asynchronous hashing, not after it.
    try {
      const data = new TextEncoder().encode(text)
      if (data.byteLength > CLIPBOARD_TEXT_MAX_BYTES || new TextDecoder().decode(data) !== text) throw new Error('INVALID_TEXT')
      const digest = await sha256Hex(data.buffer as ArrayBuffer)
      if (pending.cancelled || !this.options.isActive()) throw pending.failure ?? new Error('DISCONNECTED')
      pending.timer = setTimeout(() => this.fail(pending, 'TIMEOUT'), 60_000)
      this.message('clipboard.text-offer', { transferId: pending.id, encoding: 'utf-8',
        chars: text.length, size: data.byteLength, sha256: digest })
      await pending.accepted.promise
      pending.phase = 'streaming'
      onProgress(0, data.byteLength)
      const chunkBytes = Math.min(CLIPBOARD_TEXT_CHUNK_BYTES, BULK_CHUNK_BYTES)
      for (let offset = 0; offset < data.byteLength; offset += chunkBytes) {
        if (pending.cancelled || this.closed || !this.options.isActive()) throw pending.failure ?? new Error('DISCONNECTED')
        const end = Math.min(offset + chunkBytes, data.byteLength)
        if (!(await waitForBulkCapacity(this.options.channel, end - offset,
          () => pending.cancelled || this.closed || !this.options.isActive(), this.options.bulkContext))) {
          throw pending.failure ?? new Error('DISCONNECTED')
        }
        this.options.channel.send(data.slice(offset, end).buffer)
        onProgress(end, data.byteLength)
      }
      pending.phase = 'complete'
      this.message('clipboard.text-complete', { transferId: pending.id })
      await pending.applied.promise
      if (pending.cancelled || this.closed || !this.options.isActive()) throw pending.failure ?? new Error('DISCONNECTED')
    } catch (error) {
      try { this.message('clipboard.text-cancel', { transferId: pending.id }) } catch { /* Closed channel. */ }
      throw error
    } finally {
      if (pending.timer !== null) clearTimeout(pending.timer)
      if (this.pending === pending) this.pending = null
    }
  }
}

/** Host -> viewer staging only; never writes to the browser's system clipboard. */
export class TextClipboardDownloadReceiver {
  private incoming: { id: string; chars: number; size: number; sha256: string;
    bytes: Uint8Array; received: number; verifying: boolean; timer: ReturnType<typeof setTimeout> } | null = null
  private lastSequence = 0
  private closed = false
  constructor(private readonly options: Options & { readonly onText: (text: string) => void }) {}

  private reply(event: string, id: string, code?: string): void {
    if (this.closed || this.options.channel.readyState !== 'open') return
    try {
      this.options.channel.send(JSON.stringify({ event, data: { transferId: id, ...(code ? { code } : {}) },
        sessionId: this.options.sessionId, sequence: this.options.nextSequence(), timestamp: Date.now(), version: 1 }))
    } catch { /* A closed channel cannot authorize staging. */ }
  }
  private discard(code?: string): void {
    const incoming = this.incoming
    this.incoming = null
    if (!incoming) return
    clearTimeout(incoming.timer)
    if (code) this.reply('clipboard.text-error', incoming.id, code)
  }
  close(): void {
    this.closed = true
    this.discard()
  }
  receive(raw: unknown): void {
    if (this.closed) return
    if (raw instanceof ArrayBuffer) {
      const incoming = this.incoming
      if (!incoming) return
      if (!this.options.isActive()) { this.discard('CONTROL_INACTIVE'); return }
      if (incoming.verifying || raw.byteLength < 1 || raw.byteLength > CLIPBOARD_TEXT_CHUNK_BYTES ||
          incoming.received + raw.byteLength > incoming.size) { this.discard('SIZE_MISMATCH'); return }
      incoming.bytes.set(new Uint8Array(raw), incoming.received)
      incoming.received += raw.byteLength
      return
    }
    if (typeof raw !== 'string' || raw.length > 4096) return
    let message
    try {
      const validation = validateTextClipboardMessage(JSON.parse(raw))
      if (!validation.ok) return
      message = validation.value
    } catch { return }
    if (message.sessionId !== this.options.sessionId || message.sequence <= this.lastSequence ||
        Math.abs(message.timestamp-Date.now()) > 30_000) return
    if (message.event !== 'clipboard.text-offer' && message.event !== 'clipboard.text-complete' &&
        message.event !== 'clipboard.text-cancel') return
    this.lastSequence = message.sequence
    const id = message.data.transferId
    if (message.event === 'clipboard.text-offer') {
      if (!this.options.isActive()) { this.reply('clipboard.text-error', id, 'CONTROL_INACTIVE'); return }
      if (this.incoming) { this.reply('clipboard.text-error', id, 'BUSY'); return }
      if (message.data.chars > CLIPBOARD_TEXT_RECEIVE_MAX_CHARS ||
          message.data.size > CLIPBOARD_TEXT_RECEIVE_MAX_CHARS*4) {
        this.reply('clipboard.text-error', id, 'INVALID_TEXT'); return
      }
      this.incoming = { id, chars: message.data.chars, size: message.data.size, sha256: message.data.sha256,
        bytes: new Uint8Array(message.data.size), received: 0, verifying: false,
        timer: setTimeout(() => this.discard('TIMEOUT'), 60_000) }
      this.reply('clipboard.text-accept', id)
    } else if (this.incoming?.id === id) {
      if (message.event === 'clipboard.text-cancel') this.discard()
      else if (!this.incoming.verifying) void this.complete(this.incoming)
    }
  }
  private async complete(incoming: NonNullable<TextClipboardDownloadReceiver['incoming']>): Promise<void> {
    incoming.verifying = true
    if (incoming.received !== incoming.size) { this.discard('SIZE_MISMATCH'); return }
    try {
      const digest = await sha256Hex(incoming.bytes.buffer as ArrayBuffer)
      if (this.incoming !== incoming || this.closed) return
      if (!this.options.isActive()) { this.discard('CONTROL_INACTIVE'); return }
      if (digest !== incoming.sha256) { this.discard('HASH_MISMATCH'); return }
      const text = new TextDecoder('utf-8', { fatal: true }).decode(incoming.bytes)
      if (text.length !== incoming.chars || text.includes('\0')) { this.discard('INVALID_TEXT'); return }
      this.options.onText(text)
      this.discard()
      this.reply('clipboard.text-applied', incoming.id)
    } catch {
      if (this.incoming === incoming) this.discard('INVALID_TEXT')
    }
  }
}
