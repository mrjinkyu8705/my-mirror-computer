import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CLIPBOARD_TEXT_RECEIVE_MAX_CHARS } from '@mirror/protocol'
import { TextClipboardDownloadReceiver } from '../src/textClipboard'

const SESSION = 'session_0123456789abcdef', ID = 'transfer_0123456789abcdef'
beforeEach(() => vi.stubGlobal('crypto', webcrypto))
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

function fixture() {
  let sequence = 0, outgoing = 0, active = true
  const replies: { event: string; data: { code?: string } }[] = []
  const onText = vi.fn()
  const channel = { readyState: 'open', send: (raw: string) => replies.push(JSON.parse(raw)) }
  const receiver = new TextClipboardDownloadReceiver({ channel: channel as unknown as RTCDataChannel, sessionId: SESSION,
    nextSequence: () => ++outgoing, isActive: () => active, onText })
  function meta(event: string, data: object, overrides = {}) {
    receiver.receive(JSON.stringify({ event, data: { transferId: ID, ...data }, sequence: ++sequence,
      sessionId: SESSION, version: 1, timestamp: Date.now(), ...overrides }))
  }
  async function offer(text: string, overrides = {}, envelope = {}) {
    const bytes = new TextEncoder().encode(text)
    const digest = await webcrypto.subtle.digest('SHA-256', bytes)
    const sha256 = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2,'0')).join('')
    meta('clipboard.text-offer', { encoding: 'utf-8', chars: text.length, size: bytes.length, sha256, ...overrides }, envelope)
    return bytes
  }
  function stream(bytes: Uint8Array) {
    for (let offset=0;offset<bytes.length;offset+=16384) {
      // TextEncoder is Node's in jsdom; create the browser realm's ArrayBuffer.
      const chunk=bytes.slice(offset,offset+16384), buffer=new ArrayBuffer(chunk.length)
      new Uint8Array(buffer).set(chunk)
      receiver.receive(buffer)
    }
  }
  return { receiver, onText, replies, meta, offer, stream, deactivate: () => { active=false } }
}

describe('bounded host text download', () => {
  it.each(['한'.repeat(65_536), '😀'.repeat(32_768)])('stages full UTF-16 limit case %# only after validation', async text => {
    const f=fixture()
    expect(CLIPBOARD_TEXT_RECEIVE_MAX_CHARS).toBe(65_536)
    const bytes=await f.offer(text)
    expect(f.replies[0]?.event).toBe('clipboard.text-accept')
    f.stream(bytes)
    expect(f.onText).not.toHaveBeenCalled()
    f.meta('clipboard.text-complete', {})
    f.meta('clipboard.text-complete', {})
    await vi.waitFor(() => expect(f.replies.at(-1)?.event).not.toBe('clipboard.text-accept'))
    expect(f.replies.at(-1)?.data.code).toBeUndefined()
    expect(f.onText).toHaveBeenCalledExactlyOnceWith(text)
    expect(f.replies.at(-1)?.event).toBe('clipboard.text-applied')
    f.receiver.close()
  })
  it('rejects oversized metadata before accepting or staging', async () => {
    const f=fixture()
    await f.offer('a', { chars: 65_537 })
    expect(f.replies.at(-1)?.data.code).toBe('INVALID_TEXT')
    expect(f.onText).not.toHaveBeenCalled()
    f.receiver.close()
  })
  it.each(['hash','characters','NUL','incomplete'])('does not stage %s-corrupted data', async kind => {
    const f=fixture(), text=kind==='NUL' ? 'a\0b' : 'abc'
    const bytes=await f.offer(text, kind==='hash' ? {sha256:'0'.repeat(64)} : kind==='characters' ? {chars:2} : {})
    f.stream(kind==='incomplete' ? bytes.slice(0,2) : bytes)
    f.meta('clipboard.text-complete', {})
    await vi.waitFor(() => expect(f.replies.at(-1)?.event).toBe('clipboard.text-error'))
    expect(f.onText).not.toHaveBeenCalled()
    f.receiver.close()
  })
  it('ignores wrong-session metadata and requires acceptance before chunks', async () => {
    const f=fixture()
    const bytes=await f.offer('abc', {}, {sessionId:'session_wrong_0123456789'})
    f.stream(bytes); f.meta('clipboard.text-complete', {})
    expect(f.replies).toEqual([])
    expect(f.onText).not.toHaveBeenCalled()
    f.receiver.close()
  })
  it('does not stage after disconnect/cancellation or changed permissions', async () => {
    for (const stop of ['close','cancel','permission']) {
      const f=fixture(), bytes=await f.offer('hello')
      f.stream(bytes)
      if(stop==='close') f.receiver.close()
      else if(stop==='cancel') f.meta('clipboard.text-cancel', {})
      else f.deactivate()
      f.meta('clipboard.text-complete', {})
      await new Promise(resolve => setTimeout(resolve, 5))
      expect(f.onText).not.toHaveBeenCalled()
      f.receiver.close()
    }
  })
  it('enforces a single pending download and bounded chunks', async () => {
    const f=fixture()
    await f.offer('abc')
    await f.offer('other')
    expect(f.replies.at(-1)?.data.code).toBe('BUSY')
    f.receiver.receive(new ArrayBuffer(32769))
    expect(f.replies.at(-1)?.data.code).toBe('SIZE_MISMATCH')
    f.receiver.close()
  })
  it('expires an unfinished receive without staging any partial text', async () => {
    const f=fixture()
    vi.useFakeTimers()
    await f.offer('hello')
    await vi.advanceTimersByTimeAsync(60_001)
    expect(f.replies.at(-1)?.data.code).toBe('TIMEOUT')
    expect(f.onText).not.toHaveBeenCalled()
    f.receiver.close()
  })
})
