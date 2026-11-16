import { afterEach, describe, expect, it, vi } from 'vitest'
import { FileFlow, FILE_WINDOW_BYTES } from '../src/fileFlow'
import { streamFileChunks } from '../src/fileUpload'
import { validateFileMessage } from '@mirror/protocol'
afterEach(() => vi.useRealTimers())

describe('file-only receipt credit and pacing', () => {
  it('bounds already handed-off bytes even if bufferedAmount is zero', async () => {
    vi.useFakeTimers()
    const flow = new FileFlow(); flow.enable()
    flow.sent(FILE_WINDOW_BYTES)
    let ready = false
    const wait = flow.wait(4096, () => false).then(ok => { ready = ok; return ok })
    await vi.advanceTimersByTimeAsync(100)
    expect(ready).toBe(false)
    flow.acknowledge(FILE_WINDOW_BYTES)
    await vi.advanceTimersByTimeAsync(10)
    expect(await wait).toBe(true)
    expect(flow.stats.peakInFlight).toBe(FILE_WINDOW_BYTES)
  })
  it('rejects regressing, negative, fractional or unsent receipt credits', () => {
    const flow = new FileFlow(); flow.enable(); flow.sent(100)
    flow.acknowledge(50)
    for (const value of [49, -1, NaN, Infinity, 50.5, 101]) flow.acknowledge(value)
    expect(flow.stats.acknowledged).toBe(50)
  })
  it('paces legacy transfers too, without requiring unsupported ACKs', async () => {
    vi.useFakeTimers()
    const flow = new FileFlow(); flow.sent(4096)
    let ready = false
    const wait = flow.wait(4096, () => false).then(ok => { ready = ok; return ok })
    await vi.advanceTimersByTimeAsync(5); expect(ready).toBe(false)
    await vi.advanceTimersByTimeAsync(10); expect(await wait).toBe(true)
    expect(flow.stats.credit).toBe(false)
  })
  it('stops on cancellation or missing receipt credit without unlimited queue growth', async () => {
    vi.useFakeTimers()
    const flow = new FileFlow(); flow.enable(); flow.sent(FILE_WINDOW_BYTES)
    expect(await flow.wait(1, () => true)).toBe(false)
    const waiting = flow.wait(1, () => false)
    await vi.advanceTimersByTimeAsync(30010)
    expect(await waiting).toBe(false)
  })
  it('keeps file bytes exact while delayed receipts force real backpressure', async () => {
    vi.useFakeTimers()
    const flow = new FileFlow(); flow.enable()
    const source = Uint8Array.from({ length: 131077 }, (_, i) => i % 251)
    const chunks: Uint8Array[] = []
    let received = 0
    const channel = { readyState: 'open', bufferedAmount: 0, send(data: ArrayBuffer) {
      chunks.push(new Uint8Array(data)); received += data.byteLength
      const receipt = received
      window.setTimeout(() => flow.acknowledge(receipt), 80)
    } } as unknown as RTCDataChannel
    const waiting = streamFileChunks(channel, source.buffer, () => {}, () => false, undefined, flow)
    await vi.advanceTimersByTimeAsync(4000)
    expect(await waiting).toBe(true)
    const merged = new Uint8Array(received); let offset = 0
    for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length }
    expect(merged).toEqual(source)
    expect(flow.stats.peakInFlight).toBeLessThanOrEqual(FILE_WINDOW_BYTES)
  })
  it('negotiates only optional known flowControl and accepts cached legacy peers', () => {
    const envelope = { event: 'file.accept', sequence: 1, sessionId: 'session_0123456789abcdef', timestamp: Date.now(), version: 1 }
    const data = { transferId: 'transfer_0123456789abcdef' }
    expect(validateFileMessage({ ...envelope, data }).ok).toBe(true)
    expect(validateFileMessage({ ...envelope, data: { ...data, flowControl: 'credit-v1' } }).ok).toBe(true)
    expect(validateFileMessage({ ...envelope, data: { ...data, flowControl: 'unknown' } }).ok).toBe(false)
    expect(validateFileMessage({ ...envelope, data: { ...data, secret: 'forbidden' } }).ok).toBe(false)
  })
})
