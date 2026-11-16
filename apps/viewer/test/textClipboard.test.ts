import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CLIPBOARD_TEXT_MAX_CHARS, CLIPBOARD_TEXT_MAX_BYTES, CLIPBOARD_TEXT_CHUNK_BYTES, validateTextClipboardMessage } from '@mirror/protocol'
import { TextClipboardSender } from '../src/textClipboard'

beforeEach(() => { vi.stubGlobal('crypto', webcrypto) })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

function setup(autoApply = true, autoAccept = true) {
  const sent: (string | ArrayBuffer)[] = []
  let sequence = 0, replySequence = 0
  const channel = { readyState: 'open', bufferedAmount: 0, send(data: string | ArrayBuffer) {
    sent.push(data)
    if (typeof data !== 'string') return
    const message = JSON.parse(data)
    if (message.event === 'clipboard.text-offer' && autoAccept) reply('clipboard.text-accept', message.data.transferId)
    if (message.event === 'clipboard.text-complete' && autoApply) reply('clipboard.text-applied', message.data.transferId)
  }}
  const sender = new TextClipboardSender({ channel: channel as unknown as RTCDataChannel,
    sessionId: 'session_0123456789abcdef', nextSequence: () => ++sequence, isActive: () => true })
  function reply(event: string, transferId: string, overrides: Record<string, unknown> = {}) {
    sender.receive(JSON.stringify({ event, data: { transferId }, sequence: ++replySequence,
      sessionId: 'session_0123456789abcdef', timestamp: Date.now(), version: 1, ...overrides }))
  }
  const meta = () => sent.filter((item): item is string => typeof item === 'string').map(item => JSON.parse(item))
  return { sender, sent, meta, reply, channel }
}

describe('large text clipboard transport', () => {
  it('sends 85,000 Korean characters in bounded UTF-8 chunks, then waits for applied', async () => {
    const fixture = setup(false)
    const text = '한'.repeat(85_000)
    let applied = false
    const completion = fixture.sender.send(text).then(() => { applied = true })
    await vi.waitFor(() => expect(fixture.meta().some(m => m.event === 'clipboard.text-complete')).toBe(true))
    const chunks = fixture.sent.filter((item): item is ArrayBuffer => typeof item !== 'string')
    expect(chunks.length).toBeGreaterThan(1)
    expect(chunks.every(chunk => chunk.byteLength <= CLIPBOARD_TEXT_CHUNK_BYTES)).toBe(true)
    const merged = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0))
    let offset = 0
    for (const chunk of chunks) { merged.set(new Uint8Array(chunk), offset); offset += chunk.byteLength }
    expect(new TextDecoder('utf-8', { fatal: true }).decode(merged)).toBe(text)
    expect(fixture.meta()[0].data.chars).toBe(85_000)
    expect(fixture.meta()[0].data.size).toBe(255_000)
    expect(applied).toBe(false)
    fixture.reply('clipboard.text-applied', fixture.meta()[0].data.transferId)
    await completion
    expect(applied).toBe(true)
  })

  it('accepts exactly 1,048,576 units and rejects over-limit, empty and NUL text', async () => {
    const fixture = setup()
    await fixture.sender.send('가'.repeat(CLIPBOARD_TEXT_MAX_CHARS))
    expect(CLIPBOARD_TEXT_MAX_CHARS).toBe(1_048_576)
    expect(fixture.meta()[0].data.chars).toBe(1_048_576)
    expect(fixture.meta()[0].data.size).toBe(3_145_728)
    await expect(fixture.sender.send('a'.repeat(CLIPBOARD_TEXT_MAX_CHARS + 1))).rejects.toThrow('INVALID_TEXT')
    await expect(fixture.sender.send('')).rejects.toThrow('INVALID_TEXT')
    await expect(fixture.sender.send('a\0b')).rejects.toThrow('INVALID_TEXT')
    await expect(fixture.sender.send('\ud800')).rejects.toThrow('INVALID_TEXT')
  })

  it('does not stream before acceptance, permits only one upload and rejects disconnects', async () => {
    const fixture = setup(false, false)
    const completion = fixture.sender.send('large text')
    const failed = expect(completion).rejects.toThrow('DISCONNECTED')
    await vi.waitFor(() => expect(fixture.meta().length).toBe(1))
    expect(fixture.sent.every(item => typeof item === 'string')).toBe(true)
    await expect(fixture.sender.send('second')).rejects.toThrow('BUSY')
    fixture.sender.close()
    await failed
  })

  it('ignores wrong-session and premature applied acknowledgements', async () => {
    const fixture = setup(false)
    let done = false
    const completion = fixture.sender.send('hello').then(() => { done = true })
    await vi.waitFor(() => expect(fixture.meta().some(m => m.event === 'clipboard.text-complete')).toBe(true))
    const id = fixture.meta()[0].data.transferId
    fixture.reply('clipboard.text-applied', id, { sessionId: 'session_wrong_0123456789' })
    fixture.reply('clipboard.text-applied', 'wrong_transfer_0123456789')
    await Promise.resolve()
    expect(done).toBe(false)
    fixture.reply('clipboard.text-applied', id)
    await completion
  })

  it('cancels on an acknowledgement timeout without reporting success', async () => {
    vi.useFakeTimers()
    const fixture = setup(false)
    const completion = fixture.sender.send('hello')
    const failed = expect(completion).rejects.toThrow('TIMEOUT')
    await vi.waitFor(() => expect(fixture.meta().some(m => m.event === 'clipboard.text-complete')).toBe(true))
    await vi.advanceTimersByTimeAsync(60_001)
    await failed
    expect(fixture.meta().at(-1).event).toBe('clipboard.text-cancel')
  })

  it('validates metadata caps and rejects unexpected properties', () => {
    const valid = { event: 'clipboard.text-offer', data: { transferId: 'transfer_0123456789abcdef',
      encoding: 'utf-8', chars: 85_000, size: 255_000, sha256: 'a'.repeat(64) },
      sequence: 1, sessionId: 'session_0123456789abcdef', timestamp: Date.now(), version: 1 }
    expect(validateTextClipboardMessage(valid).ok).toBe(true)
    expect(validateTextClipboardMessage({ ...valid, data: { ...valid.data, chars: CLIPBOARD_TEXT_MAX_CHARS + 1 } }).ok).toBe(false)
    expect(validateTextClipboardMessage({ ...valid, data: { ...valid.data, size: CLIPBOARD_TEXT_MAX_BYTES + 1 } }).ok).toBe(false)
    expect(validateTextClipboardMessage({ ...valid, command: 'not-allowed' }).ok).toBe(false)
  })
})
